"""Skills: a finished task written down, so it can be done again with no model.

A run that ends done leaves a trace: what was clicked, typed, chosen, scrolled. A skill keeps those steps with how to
find each control again (its role and label, and its place among controls labelled alike), how the run ended (the
address, and lines that appeared then) and how its list of results is read. A replay finds every control again from
that description and makes no model call. When the page no longer matches (a control is gone, or the run ends
somewhere else), the normal loop takes over from that point with the steps so far as its history, and the skill is
written again from the new run.

When the results came from a JSON request, the skill keeps that request too: a replay first reads the rows with it,
from inside the page, and only clicks through the steps if that fails.
"""

import asyncio
import hashlib
import inspect
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .agent import run
from .browser import StalePage
from .describe import CONSENT, irreversible, named, norm, shape, target_info
from .model import screen_anchor, split_chunks
from .shortcut import rows_from_json

VERSION = 1
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
        return skill if isinstance(skill, dict) and skill.get("version") == VERSION else None

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


def skill_from_run(goal, start_url, result):
    """The skill a finished, learning run leaves; None for any other run."""
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
        if h["kind"] == "click" and CONSENT.search(h["action"] or ""):
            step["optional"] = True  # a cookie banner shows once, not on every visit
        steps.append(step)
    usage = result.get("usage") or {}
    return {"version": VERSION, "goal": goal, "start_url": start_url,
            "learned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "learned_with": {"input_tokens": usage.get("input_tokens", 0), "requests": usage.get("requests", 0)},
            "steps": steps, **result["learned"]}


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


async def replay(session, skill, page, on_step=None, confirm=False):
    """Do a skill again with no model. -> a result like run()'s, with status:
    done: it read the results through the shortcut, or found every step and ended where the skill ended;
    needs_confirmation: `confirm` is on and the next step can't be taken back (it is not clicked);
    diverged: the page no longer matches (`detail` says where); `trace` holds the steps done, for the loop.
    """
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
        state = await session.observe(page)
        for i, step in enumerate(skill["steps"]):
            if step["kind"] == "wait":
                await asyncio.sleep(1.0)
                state = await session.observe(page)
                continue
            action = _control(step, state)
            if action is None:  # the page may still be filling in
                await asyncio.sleep(1.0)
                state = await session.observe(page)
                action = _control(step, state)
            if action is None:
                if step.get("optional") or already_chosen(step, state["actions"]):
                    continue
                status, detail = "diverged", f"step {i + 1} ({step['kind']} {step['label'][:60]!r}) is not on the page"
                break
            if confirm and irreversible(action):
                status, pending = "needs_confirmation", {"kind": action["kind"], "label": action["label"],
                                                         "role": action.get("role")}
                detail = f"stopped before {action['label'][:80]!r}, which can't be taken back: confirm to go ahead"
                break
            try:
                await session.act(page, state, action, step.get("text"))
            except StalePage:  # the page moved under the step: look again, once
                state = await session.observe(page)
                action = _control(step, state)
                try:
                    if action is None:
                        raise StalePage("gone")
                    await session.act(page, state, action, step.get("text"))
                except StalePage:
                    status, detail = "diverged", f"step {i + 1} ({step['label'][:60]!r}) kept changing under the replay"
                    break
            new_state = await session.observe(page, after=action)
            trace.append({"step": len(trace) + 1, "action": action["label"], "kind": action["kind"],
                          "role": action.get("role"), "text": step.get("text"), "text_source": None,
                          "probability": None, "alternatives": [], "confidence": None, "decide_ms": 0, "at_ms": ms(),
                          "url": state["url"], "target": target_info(action, state["actions"]),
                          "page_changed": new_state["marker"] != state["marker"], "led_to": new_state["url"],
                          "replayed": True})
            state = new_state
            if on_step and inspect.isawaitable(reported := on_step(trace[-1])):
                await reported
        if status == "done":  # it must end where the learned run ended, and read its list there if it had one
            if not ends_where_learned(skill.get("end") or {}, state):
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


async def run_with_skill(session, goal, url=None, page=None, store=None, on_step=None, confirm=False):
    """run(), learned once: the first time a goal is run from a start page it is learned and saved as a skill; later
    times the skill is replayed with no model, and where the page changed the loop takes over and the skill is
    written again. result["skill"]["used"]: learned, replayed, shortcut or repaired.
    """
    page = page or await session.new_tab(url)
    start_url = url or page.url
    store = store or SkillStore()
    skill = store.get(goal, start_url)
    if skill:
        result = await replay(session, skill, page, on_step=on_step, confirm=confirm)
        if result["status"] != "diverged":
            return result
        why = result["detail"]
        # the page changed since the skill was learned: the loop carries on from here, and the skill is written again
        result = await run(session, goal, page=page, on_step=on_step, history=result["trace"], confirm=confirm,
                           learn=True)
        result["skill"] = {"used": "repaired", "because": why}
    else:
        result = await run(session, goal, page=page, on_step=on_step, confirm=confirm, learn=True)
        result["skill"] = {"used": "learned"}
    new = skill_from_run(goal, start_url, result)
    if new:
        result["skill"]["saved"] = str(store.put(new))
    return result
