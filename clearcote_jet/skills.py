"""Skills: a finished task written down, so it can be done again with no model.

A run that ends done leaves a trace: what was clicked, typed, chosen, scrolled. A skill keeps those steps with how to
find each control again (its role and label, and its place among controls labelled alike), how the run ended (the
address, and lines that appeared then) and how its list of results is read. A replay finds every control again from
that description and makes no model call. When the page no longer matches (a control is gone, or the run ends
somewhere else), the normal loop takes over from that point with the steps so far as its history, and the skill is
written again from the new run.

When the results came from a JSON request, the skill keeps that request too: a replay first reads the rows with it,
from inside the page, and only clicks through the steps if that fails.

A goal with {{slots}} and values for them (slots.py) is learned as a template: the skill is keyed on the template and
the start page, not on the values, and a value typed (or seen in a label, or in a line the run ended on) is kept as
its slot's token. A replay fills the tokens from its own values, so one skill serves every set of values; a skill
whose slot has no value this time ends with status needs_input before anything is done.
"""

import asyncio
import hashlib
import inspect
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .agent import ask, needs_values, run
from .browser import StalePage
from .describe import CONSENT, Quote, Said, irreversible, named, norm, plain, shape, target_info
from .model import screen_anchor, split_chunks
from .shortcut import rows_from_json
from .slots import SLOT, MissingValue, Values, canonical, each_string, slots

VERSION = 1
TEMPLATE_VERSION = 2  # a skill with {{slots}}: an older Jet, which would type the tokens as they are, skips it
DEFAULT_DIR = Path.home() / ".clearcote-jet" / "skills"


def skill_key(goal, start_url):
    """The same goal on the same page: case, spacing, the query string and a trailing slash don't matter."""
    u = urlsplit(start_url or "")
    return hashlib.sha256(f"{norm(goal)}\n{u.netloc.lower()}{u.path.rstrip('/')}".encode()).hexdigest()[:16]


class SkillStore:
    """Skills as JSON files in one folder: CLEARCOTE_JET_SKILLS, default ~/.clearcote-jet/skills."""

    def __init__(self, folder=None):
        self.folder = Path(folder or os.environ.get("CLEARCOTE_JET_SKILLS") or DEFAULT_DIR)

    def path(self, goal, start_url):
        host = urlsplit(start_url or "").netloc.replace(":", "_") or "local"
        return self.folder / f"{host}-{skill_key(goal, start_url)}.json"

    def get(self, goal, start_url):
        try:
            skill = json.loads(self.path(goal, start_url).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return skill if isinstance(skill, dict) and skill.get("version") in (VERSION, TEMPLATE_VERSION) else None

    def put(self, skill):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.path(skill["goal"], skill["start_url"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(skill, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # a reader never sees a half-written skill
        return path

    def forget(self, goal, start_url):
        try:
            self.path(goal, start_url).unlink()
            return True
        except FileNotFoundError:
            return False

    def all(self):
        out = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out


def skill_from_run(goal, start_url, result, values=None):
    """The skill a finished, learning run leaves; None for any other run.

    values (slots.Values): `goal` is a template; what the run typed or saw of a value is kept as its slot's token.
    """
    if result.get("status") != "done" or not result.get("learned"):
        return None
    steps = []
    for h in result["trace"]:
        t = h.get("target") or {}
        step = {"kind": h["kind"], "role": h.get("role"), "label": h["action"],
                "nth": t.get("nth", 0), "nth_shape": t.get("nth_shape", 0)}
        if h["kind"] == "select":
            step["value"] = t.get("value")
        if h["kind"] in ("fill", "find"):
            step["text"] = h.get("text")
        if h["kind"] == "scroll":
            step["delta"] = t.get("delta") or 560
        if t.get("in_menu"):
            step["in_menu"] = True
        if h["kind"] == "click" and CONSENT.search(h["action"] or ""):
            step["optional"] = True  # a cookie banner shows once, not on every visit
        steps.append(step)
    usage = result.get("usage") or {}
    learned = dict(result["learned"])
    head = {"version": VERSION, "goal": goal, "start_url": start_url}
    if values is not None:
        names = slots(goal)

        def keep(text):  # a value as its token; a secret is never written down, wherever it showed
            return values.mask(values.templatize(text, names))
        steps, learned = each_string(steps, keep), each_string(learned, keep)
        used = slots(json.dumps([steps, learned], ensure_ascii=False))
        head = {"version": TEMPLATE_VERSION, "goal": canonical(goal), "start_url": start_url, "slots": used,
                "secret": sorted(n for n in used if n in values.secret)}
    return {**head, "learned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "learned_with": {"input_tokens": usage.get("input_tokens", 0), "requests": usage.get("requests", 0)},
            "steps": steps, **learned}


def locate(step, actions):
    """The control a recorded step used, on the current page; None when it isn't there.

    In order: the same role and label; the same label; the same role and a label of the same shape (its numbers
    changed, e.g. '87 comments'). Among several, the one at the recorded place.
    """
    kind = step["kind"]
    found = [a for a in actions if a.get("kind") == kind and "node" in a]
    if kind == "select":
        found = [a for a in found if a.get("value") == step.get("value")]
    for same, nth in ((lambda a: a.get("role") == step.get("role") and norm(a.get("label")) == norm(step["label"]),
                       step.get("nth", 0)),
                      (lambda a: norm(a.get("label")) == norm(step["label"]), step.get("nth", 0)),
                      (lambda a: a.get("role") == step.get("role") and shape(a.get("label")) == shape(step["label"]),
                       step.get("nth_shape", 0))):
        hits = [a for a in found if same(a)]
        if hits:
            return hits[min(nth, len(hits) - 1)]
    return None


def already_chosen(step, actions):
    """A dropdown step whose option is already the chosen one (a page that remembers it is not offered again)."""
    if step["kind"] != "select":
        return False
    field, _, option = step["label"].partition(" → ")
    return any(a.get("kind") == "select" and norm(a["label"].partition(" → ")[0]) == norm(field)
               and norm(a.get("current_value")) == norm(option) for a in actions)


def ends_where_learned(end, state):
    """The replay ended where the learned run ended: the same page (path), and if lines appeared then, one of them."""
    if urlsplit(state["url"]).path != urlsplit(end.get("url") or "").path:
        return False
    lines = end.get("lines") or []
    return not lines or any(line in state["text"] for line in lines)


def _control(step, state):
    if step["kind"] == "scroll":
        return {"id": "scroll", "kind": "scroll", "label": step["label"], "delta": step.get("delta") or 560}
    if step["kind"] == "find":
        return {"id": "find_text", "kind": "find", "label": step["label"]}
    return locate(step, state["actions"])


def _row_line(row):
    return "- " + " · ".join(str(row[k]) for k in ("title", "price", "link") if row.get(k))


async def replay(session, skill, page, on_step=None, confirm=False, said=False, values=None, approve=None):
    """Do a skill again with no model. -> a result like run()'s, with status:
    done: it read the results through the shortcut, or found every step and ended where the skill ended;
    needs_confirmation: `confirm` is on and the next step can't be taken back (it is not clicked);
    cancelled: `confirm` is on, `approve` was asked (as in run()) and said no;
    needs_input: the skill has a {{slot}} that `values` (slots.Values) has no value for; nothing is done;
    diverged: the page no longer matches (`detail` says where); `trace` holds the steps done, for the loop.
    """
    fill = values or Values()
    recorded = skill
    if skill.get("slots") is not None:  # learned from a template: its tokens are this replay's values
        if missing := fill.missing(skill["slots"]):
            return needs_values(page, missing)
        try:
            skill = {**skill, **each_string({k: skill[k] for k in ("steps", "end", "list", "shortcut") if k in skill},
                                            fill.fill)}
        except MissingValue as e:
            return needs_values(page, [str(e)])
    else:
        fill = Values()  # a plain skill is replayed as it was written, braces and all
    result = await _replay(session, skill, recorded["steps"], page, on_step, confirm, approve, fill)
    if fill.secret:
        result = each_string(result, fill.mask)
    return result if said else plain(result)  # said: the messages as describe.Said, for the MCP server


# --- the steps of a skill, as recorded or as edited by hand ---------------------------------------------------------
#
# Recorded steps find a control by its role and label (locate()). A skill edited by hand may also have:
#   {"kind": "click"|"fill", "selector": "css", "text"?}       the first element matching a CSS selector
#   {"kind": "goto", "url"}                                   open an address
#   {"kind": "wait", "seconds"?}                              pause (1 s when no seconds are given)
#   {"kind": "key", "key"}                                    press a key: Enter, Tab, Escape, ArrowDown, ...
#   {"kind": "js", "code"}                                    the page's own JavaScript (statements)
#   {"kind": "wait_for", "condition", "seconds"?}             until the condition is true (15 s by default)
#   {"kind": "if", "condition", "then": [...], "else": [...]}
#   {"kind": "while", "condition", "then": [...], "max"?}     at most `max` rounds (10 by default)
# A condition: {"type": "text"|"control"|"url"|"selector"|"js", "value", "not"?}: the page shows this text, offers a
# control with this label, its address contains this, an element matches this selector, this expression is true.

FLOW_KINDS = {"goto", "key", "js", "wait_for", "if", "while"}
STEP_KINDS = {"click", "fill", "select", "scroll", "find", "wait"} | FLOW_KINDS
CONDITION_TYPES = {"text", "control", "url", "selector", "js"}
KEYS = {"enter": "Enter", "tab": "Tab", "escape": "Escape", "space": "Space", "backspace": "Backspace",
        "delete": "Delete", "arrowdown": "ArrowDown", "arrowup": "ArrowUp", "arrowleft": "ArrowLeft",
        "arrowright": "ArrowRight", "pagedown": "PageDown", "pageup": "PageUp", "home": "Home", "end": "End"}
MAX_STEPS, MAX_DEPTH, MAX_ROUNDS = 500, 6, 100


def describe_condition(c):
    what = {"text": "the page shows", "control": "there is a control", "url": "the address contains",
            "selector": "an element matches", "js": "this is true:"}.get(c.get("type"), "?")
    return f"{'not ' if c.get('not') else ''}{what} {str(c.get('value') or '')[:80]!r}"


def check_steps(steps, depth=0, count=None):
    """Raise ValueError naming the first step that cannot run; return how many steps there are (blocks included)."""
    count = count if count is not None else [0]
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    if depth > MAX_DEPTH:
        raise ValueError(f"blocks are nested more than {MAX_DEPTH} deep")
    for i, st in enumerate(steps):
        count[0] += 1
        if count[0] > MAX_STEPS:
            raise ValueError(f"more than {MAX_STEPS} steps")
        where = f"step {i + 1}"
        if not isinstance(st, dict) or st.get("kind") not in STEP_KINDS:
            raise ValueError(f"{where}: unknown kind {st.get('kind') if isinstance(st, dict) else st!r}")
        k = st["kind"]
        if k in ("click", "fill", "select") and not (st.get("selector") or st.get("label")):
            raise ValueError(f"{where}: {k} needs a label or a CSS selector")
        if k == "select" and st.get("selector"):
            raise ValueError(f"{where}: a dropdown is chosen by its label, not a selector")
        if k == "fill" and not isinstance(st.get("text"), str):
            raise ValueError(f"{where}: fill needs the text to type")
        if k == "goto" and not re.match(r"^https?://", str(st.get("url") or "")) and "{{" not in str(st.get("url")):
            raise ValueError(f"{where}: go to needs an http(s) address")
        if k == "key" and str(st.get("key") or "").lower() not in KEYS:
            raise ValueError(f"{where}: unknown key {st.get('key')!r}")
        if k == "js" and not str(st.get("code") or "").strip():
            raise ValueError(f"{where}: JavaScript needs code")
        for n in ("seconds", "max"):
            if n in st and not (isinstance(st[n], (int, float)) and 0 <= st[n] <= (MAX_ROUNDS if n == "max" else 600)):
                raise ValueError(f"{where}: {n} is out of range")
        if k in ("wait_for", "if", "while"):
            c = st.get("condition")
            if not isinstance(c, dict) or c.get("type") not in CONDITION_TYPES or not str(c.get("value") or "").strip():
                raise ValueError(f"{where}: needs a condition (type and value)")
        if k in ("if", "while"):
            check_steps(st.get("then") or [], depth + 1, count)
        if k == "if":
            check_steps(st.get("else") or [], depth + 1, count)
    return count[0]


class _Walk:
    """One replay going through its steps. state: the page as last observed."""

    def __init__(self, session, page, fill, confirm, approve, on_step, trace, ms):
        self.session, self.page, self.fill, self.confirm, self.approve = session, page, fill, confirm, approve
        self.on_step, self.trace, self.ms, self.state = on_step, trace, ms, None

    async def look(self, step=None):  # a link inside a closed menu is only offered when the menus are asked for
        menus = bool(step and step.get("in_menu"))
        self.state = await (self.session.observe(self.page, menus=True) if menus else self.session.observe(self.page))
        return self.state

    async def record(self, kind, label, text=None, action=None, before=None):
        before = before or self.state
        new_state = await self.session.observe(self.page, after=action or {"kind": kind})
        self.trace.append({"step": len(self.trace) + 1, "action": label, "kind": kind,
                           "role": (action or {}).get("role"), "text": text, "text_source": None,
                           "probability": None, "alternatives": [], "confidence": None, "decide_ms": 0,
                           "at_ms": self.ms(), "url": before["url"],
                           "target": target_info(action, before["actions"]) if action and "node" in action else {},
                           "page_changed": new_state["marker"] != before["marker"], "led_to": new_state["url"],
                           "replayed": True})
        self.state = new_state
        if self.on_step and inspect.isawaitable(reported := self.on_step(self.trace[-1])):
            await reported

    async def holds(self, c):
        t, v = c.get("type"), str(c.get("value") or "")
        if t == "text":
            ok = norm(v) in norm(self.state["text"])
        elif t == "control":
            ok = any(norm(a.get("label")) == norm(v) for a in self.state["actions"])
        elif t == "url":
            ok = v in (self.page.url or "")
        elif t == "selector":
            ok = await self.session.has_selector(self.page, v)
        else:
            ok = await self.session.js_true(self.page, v)
        return ok != bool(c.get("not"))

    async def steps(self, steps, recorded, prefix):
        """-> (status, detail, pending): done when every step ran."""
        for i, step in enumerate(steps):
            n = f"{prefix}{i + 1}"
            rec = recorded[i] if i < len(recorded) else step
            try:
                out = await self.one(step, rec, n)
            except StalePage as e:
                out = ("diverged", Said(f"step {n}: {e}"), None)
            except (PlaywrightError, ValueError) as e:  # a selector, an address or the page's JavaScript failed
                out = ("diverged", Said(f"step {n} ({step['kind']}) failed: {str(e).splitlines()[0][:200]}"), None)
            if out is not None:
                return out
        return "done", None, None

    async def one(self, step, rec, n):
        """Do one step. None to carry on, else (status, detail, pending) to stop."""
        kind, s = step["kind"], self.session
        if kind == "wait":
            await asyncio.sleep(float(step.get("seconds", 1.0)))
            await self.look()
            return None
        if kind == "if":
            yes = await self.holds(step["condition"])
            await self.record("if", f"If {describe_condition(step['condition'])}: {'yes' if yes else 'no'}")
            branch = "then" if yes else "else"
            status, detail, pending = await self.steps(step.get(branch) or [], rec.get(branch) or [], f"{n}.")
            return None if status == "done" else (status, detail, pending)
        if kind == "while":
            for _ in range(int(step.get("max", 10))):
                if not await self.holds(step["condition"]):
                    break
                status, detail, pending = await self.steps(step.get("then") or [], rec.get("then") or [], f"{n}.")
                if status != "done":
                    return status, detail, pending
                await self.look()
            return None
        if kind == "wait_for":
            deadline = time.monotonic() + float(step.get("seconds", 15))
            while not await self.holds(step["condition"]):
                if time.monotonic() > deadline:
                    return "diverged", Said(f"step {n}: waited {step.get('seconds', 15)} s and still not "
                                            f"{describe_condition(step['condition'])}"), None
                await asyncio.sleep(0.5)
                await self.look()
            return None
        if kind == "goto":
            before = self.state
            await s.goto(self.page, step["url"])
            await self.record("goto", f"Go to {rec.get('url', step['url'])}", before=before)
            return None
        if kind == "key":
            key = KEYS[str(step["key"]).lower()]
            await s.act(self.page, self.state, {"id": "key", "kind": "key", "key": key, "label": f"Press {key}"})
            await self.record("key", f"Press {key}")
            return None
        if kind == "js":
            await s.run_js(self.page, step["code"])
            await self.record("js", "Run JavaScript")
            return None
        if step.get("selector"):
            label = step.get("label") or step["selector"]
            if self.confirm and irreversible({"kind": kind, "label": label}):
                stop = await self.confirmed({"kind": kind, "label": label, "role": None})
                if stop:
                    return stop
            before = self.state
            await s.act_on_selector(self.page, step["selector"], kind, step.get("text"))
            await self.record(kind, label, self.fill.show(rec.get("text")), before=before)
            return None
        return await self.located(step, rec, n)

    async def confirmed(self, asked):
        """Ask before something that can't be taken back. None to go ahead, else how the replay stops."""
        why = Said("stopped before ", Quote(asked["label"][:80]), ", which can't be taken back: confirm to go ahead")
        if self.approve is None:
            return "needs_confirmation", why, asked
        if not await ask(self.approve, asked, why):
            return "cancelled", Said("not confirmed: ", Quote(asked["label"][:80])), None
        return None

    async def located(self, step, rec, n):
        """A recorded step: find its control again by role and label, and use it."""
        s, state = self.session, self.state
        if step.get("in_menu"):
            state = await self.look(step)
        action = _control(step, state)
        if action is None:  # the page may still be filling in
            await asyncio.sleep(1.0)
            state = await self.look(step)
            action = _control(step, state)
        if action is None:
            if step.get("optional") or already_chosen(step, state["actions"]):
                return None
            return "diverged", Said(f"step {n} ({step['kind']} ", Quote(step["label"][:60]), ") is not on the page"), None
        if self.confirm and irreversible(action):
            stop = await self.confirmed({"kind": action["kind"], "label": action["label"], "role": action.get("role")})
            if stop:
                return stop
            state = await self.look(step)  # the page may have moved on while the person decided
            action = _control(step, state)
            if action is None:
                return "diverged", Said(f"step {n} (", Quote(step["label"][:60]), ") was gone once it was confirmed"), None
        try:
            await s.act(self.page, state, action, step.get("text"))
        except StalePage:  # the page moved under the step: look again, once
            state = await self.look(step)
            action = _control(step, state)
            try:
                if action is None:
                    raise StalePage("gone")
                await s.act(self.page, state, action, step.get("text"))
            except StalePage:
                return "diverged", Said(f"step {n} (", Quote(step["label"][:60]), ") kept changing under the replay"), None
        await self.record(action["kind"], action["label"], self.fill.show(rec.get("text")), action=action, before=state)
        return None


async def _replay(session, skill, recorded, page, on_step, confirm, approve, fill):
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000)  # noqa: E731
    trace, rows, status, detail, pending, via, state = [], [], "done", None, None, "steps", None
    if skill.get("shortcut"):
        body = await session.fetch_json(page, skill["shortcut"])
        rows = rows_from_json(body, skill["shortcut"]) if body is not None else []
        if named(rows):
            via = "shortcut"
    if via == "steps":
        rows = []

        walk = _Walk(session, page, fill, confirm, approve, on_step, trace, ms)
        walk.state = await session.observe(page)
        status, detail, pending = await walk.steps(skill["steps"], recorded, "")
        state = walk.state
        # it must end where the learned run ended (an edited skill ends where its steps end), and read its list there
        if status == "done":
            if not skill.get("edited") and not ends_where_learned(skill.get("end") or {}, state):
                status, detail = "diverged", "the steps ended on a different page than when the skill was learned"
            elif skill.get("list"):
                rows = await session.extract(page, skill["list"])
                if not named(rows):
                    status, detail = "diverged", "the list of results is not where the skill read it"
    if via == "shortcut":
        markdown = "\n".join(_row_line(r) for r in rows)
    else:
        chunks = split_chunks(await session.settled_markdown(page))
        start = screen_anchor(chunks, state["text"])
        markdown = "\n\n".join(chunks[start:start + 3])
        rows = rows if status == "done" else []
    elapsed = ms()
    return {
        "status": status, "detail": detail, "url": page.url, "title": await page.title(), "elapsed_ms": elapsed,
        "actions": len(trace), "decisions": 0, "text_calls": 0,
        "timing": {"decide_ms": 0, "value_ms": 0, "browser_ms": elapsed},
        "usage": {"input_tokens": 0, "output_tokens": 0, "requests": 0, "estimated_usd": 0.0,
                  "text_model": {"input_tokens": 0, "output_tokens": 0, "requests": 0}},
        "stale": [], "trace": trace, "markdown": markdown, "block_scores": [], "items": rows,
        "skill": {"used": "shortcut" if via == "shortcut" else "replayed",
                  "learned_at": skill.get("learned_at"), "learned_with": skill.get("learned_with")},
        **({"pending": pending} if pending else {}),
    }


async def run_with_skill(session, goal, url=None, page=None, store=None, on_step=None, confirm=False, said=False,
                         values=None, secret=(), approve=None, max_steps=None):
    """run(), learned once: the first time a goal is run from a start page it is learned and saved as a skill; later
    times the skill is replayed with no model, and where the page changed the loop takes over and the skill is
    written again. result["skill"]["used"]: learned, replayed, shortcut or repaired.

    values, secret: `goal` is a template with {{slots}} (see run() and slots.py); the skill is keyed on the template,
    so the same skill replays with other values. approve, max_steps: as in run().
    """
    page = page or await session.new_tab(url)
    start_url = url or page.url
    store = store or SkillStore()
    templated = values is not None and bool(SLOT.search(goal or ""))
    fill = Values(values, secret)
    key = canonical(goal) if templated else goal
    skill = store.get(key, start_url)
    if skill and templated and skill.get("slots") is None:
        skill = None  # learned from these words with no values: its braces were typed as they are; learn it again
    # what this run needs values for: the skill's slots, or before it is learned the template's
    need = (skill.get("slots") or []) if skill else (slots(goal) if templated else [])
    if missing := fill.missing(need):
        result = needs_values(page, missing)
        result["skill"] = {"used": "replayed" if skill else None}
        return result if said else plain(result)
    loop = dict(on_step=on_step, confirm=confirm, learn=True, said=said, approve=approve, max_steps=max_steps,
                values=values if templated else None, secret=secret)
    if skill:
        result = await replay(session, skill, page, on_step=on_step, confirm=confirm, said=said, values=fill,
                              approve=approve)
        if result["status"] != "diverged":
            return result
        if skill.get("edited"):
            # steps written by hand are never learned over: the run stops where they stopped, and says why
            result["skill"] = {**result.get("skill", {}), "used": "edited"}
            return result
        why = result["detail"]
        # the page changed since the skill was learned: the loop carries on from here, and the skill is written again
        result = await run(session, goal, page=page, history=result["trace"], **loop)
        result["skill"] = {"used": "repaired", "because": why}
    else:
        result = await run(session, goal, page=page, **loop)
        result["skill"] = {"used": "learned"}
    new = skill_from_run(goal, start_url, result, fill if templated else None)
    if new:
        result["skill"]["saved"] = str(store.put(new))
    return result


# --- reading and writing a task's skill by hand (clearcote-jet skill get|put|forget) --------------------------------

def _key(goal):
    """What run_with_skill keys a goal on: its template when it has {{slots}} (a run then always has values)."""
    templated = bool(SLOT.search(goal or ""))
    return (canonical(goal) if templated else goal), templated


def get_skill(store, goal, start_url):
    """The skill a run of this goal from this page would use, or None (as run_with_skill decides it)."""
    key, templated = _key(goal)
    skill = store.get(key, start_url)
    if skill and templated and skill.get("slots") is None:
        return None
    return skill


def put_edited(store, goal, start_url, steps):
    """Save steps written by hand as the skill for this goal and page. They replay as written: a run that does
    not get through them stops there (it is not learned over), and a run that does ends wherever they end."""
    check_steps(steps)
    key, templated = _key(goal)
    old = get_skill(store, goal, start_url) or {}
    skill = {k: v for k, v in old.items() if k not in ("shortcut", "end")}  # the steps decide what happens now
    skill.update({"version": TEMPLATE_VERSION if templated else VERSION, "goal": key, "start_url": start_url,
                  "steps": steps, "edited": True,
                  "edited_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if templated:
        used = slots(json.dumps([steps, skill.get("list")], ensure_ascii=False))
        skill["slots"] = used
        skill["secret"] = [n for n in old.get("secret") or [] if n in used]
    else:
        skill.pop("slots", None)
        skill.pop("secret", None)
    store.put(skill)
    return skill


def forget_skill(store, goal, start_url):
    key, _ = _key(goal)
    return store.forget(key, start_url)
