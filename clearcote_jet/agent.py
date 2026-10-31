"""The loop: observe → the decision model picks a step → (a value for TYPE_TEXT) → humanized act → repeat."""

import inspect
import re
import time

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserClosed, StalePage
from .describe import Quote, Said, best_list, end_lines, irreversible, plain, same_control, target_info
from .model import (MAX_RANKED_BLOCKS, NeedsInput, add_usage, choose, estimate_usd, field_context, field_text,
                    page_view, rank_blocks, resolve, resolve_redirects, screen_anchor, split_chunks)
from .questions import MAX_SCROLL_STREAK, MAX_STEPS, MENU_SCROLL_STREAK
from .shortcut import find_shortcut
from .slots import SECRETS, MissingValue, Values, each_string, slots, token

TOP_BLOCKS = 8
# Before a run ends as blocked, the page gets this long to change (results loading, a control turning on).
BLOCKED_WAIT_SECONDS = 6
# A BLOCKED becomes the model's best untried click when clicking had at least this much of its weight (P(CLICK) times
# the target's probability): a real candidate, not noise.
FALLBACK_MIN = 0.02


def _hidden(action):
    """A control offered only when the run is stuck: a link in a closed menu, or one scrolled away in a scroll box."""
    return action.get("menu") is not None or action.get("panel") is not None


KEYS = {"enter": "Enter", "return": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab",
        "backspace": "Backspace", "delete": "Delete", "space": " ", "spacebar": " ", "up arrow": "ArrowUp",
        "arrow up": "ArrowUp", "down arrow": "ArrowDown", "arrow down": "ArrowDown", "left arrow": "ArrowLeft",
        "arrow left": "ArrowLeft", "right arrow": "ArrowRight", "arrow right": "ArrowRight", "page down": "PageDown",
        "page up": "PageUp", "home": "Home", "end": "End"}


def goal_keys(goal):
    """Keys the goal asks to press ("press Escape in the field", "hit the Enter key"), as steps the model can choose.
    Only when the goal speaks of pressing or keys: "enter your name" names no key."""
    low = (goal or "").lower()
    out, seen = [], set()
    for word, key in KEYS.items():
        w = re.escape(word)
        if key not in seen and re.search(rf"\b(?:press|hit|tap)\s+(?:the\s+)?{w}\b|\b{w}\s+key\b", low):
            seen.add(key)
            name = "Space" if key == " " else key
            out.append({"id": f"key_{name.lower()}", "kind": "key", "key": key,
                        "label": f"Press the {name} key (in the field or page that has focus)"})
    return out


SEARCHLIKE = re.compile(r"\b(search|find|query|look ?up|filter|zoek\w*|such(?:e|en)|buscar|recherch\w*)\b", re.I)


def searchlike(step):
    """A field Enter submits: a search box, a combobox, or a one-line field named for searching. Not a form's address
    or message field, where Enter adds a line or does nothing."""
    return step.get("role") in ("searchbox", "combobox") or bool(SEARCHLIKE.search(step.get("action") or ""))


NUMBERS = {"once": 1, "twice": 2, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10}


def goal_times(goal):
    """How many times the goal asks for a step: "three times", "5 times", "twice"; None when it does not say."""
    m = re.search(r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s+times\b|\b(twice)\b", goal or "", re.I)
    if not m:
        return None
    word = (m.group(1) or m.group(2)).lower()
    return int(word) if word.isdigit() else NUMBERS[word]


def _short_of_count(goal, history, state):
    """The step to do again when the goal asks for the last click N times ("click Add Element exactly three times":
    the count right after that control's name), it was done fewer than N times in a row, it is still offered and it
    is not irreversible; else None."""
    if not history or history[-1]["kind"] != "click":
        return None
    label = history[-1]["action"]
    near = re.search(re.escape(label) + r"[^.,;]{0,25}?\b((?:\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s+times"
                     r"|twice)\b", goal or "", re.I)
    times = near and goal_times(near.group(1))
    if not times:
        return None
    done = 0
    for h in reversed(history):
        if h["kind"] != "click" or h["action"] != label:
            break
        done += 1
    if done >= times:
        return None
    return next((a for a in state["actions"] if a.get("kind") == "click" and a.get("label") == label
                 and not irreversible(a)), None)


def _typed_suggestion(step, state):
    """The suggestion offered for the field just typed into whose text is exactly what was typed ("From › Amsterdam
    Centraal" after typing "Amsterdam Centraal" into From), else None."""
    typed = (step.get("text") or "").strip().lower()
    if not typed:
        return None
    want = f"{step['action']} › {typed}".lower()
    return next((a for a in state["actions"] if a.get("kind") == "click" and a.get("label", "").lower() == want), None)


def _feed_grows(history, state):
    """Scroll down again rather than stop: the last scroll made the page taller (more loaded), the page goes on below,
    and fewer than MAX_SCROLL_STREAK scrolls were made in a row. The Scroll down action, or None."""
    if not history or history[-1]["kind"] != "scroll" or not history[-1].get("grew"):
        return None
    streak = 0
    for h in reversed(history):
        if h["kind"] != "scroll":
            break
        streak += 1
    if streak >= MAX_SCROLL_STREAK - 1:
        return None
    return next((a for a in state["actions"] if a.get("id") == "scroll_down"), None)


def _tried_here(action, state, history):
    """This control was already used on this page (same label, same URL)."""
    here = state["url"]
    return any(h.get("action") == action.get("label") and h.get("url") == here for h in history)


def _useless_key(action, state):
    """A control that changed nothing: this control (its node) on this page."""
    return action.get("kind"), action.get("label"), action.get("node"), _page_of(state["url"])


def _is_useless(action, state, useless):
    return _useless_key(action, state) in useless


def _page_of(url):
    """A page, for the menus rule: its URL without the #fragment (a jump within the page is the same page)."""
    return (url or "").split("#", 1)[0]


def stopped(page, status, detail, title="", trace=None, elapsed=0):
    """The result of a task that ended before it began (or was stopped from outside): no answer read."""
    return {"status": status, "detail": detail, "url": page.url, "title": title, "elapsed_ms": elapsed,
            "actions": len(trace or []), "decisions": 0, "text_calls": 0,
            "timing": {"decide_ms": 0, "value_ms": 0, "browser_ms": elapsed},
            "usage": {"input_tokens": 0, "output_tokens": 0, "requests": 0, "estimated_usd": 0.0,
                      "text_model": {"input_tokens": 0, "output_tokens": 0, "requests": 0}},
            "stale": [], "trace": list(trace or []), "markdown": "", "block_scores": [], "items": []}


def needs_values(page, names):
    """A template run missing values for `names`: nothing is done."""
    return stopped(page, "needs_input", "No value given for " + ", ".join(token(n) for n in names))


async def ask(approve, pending, detail):
    """The caller's answer about a click that can't be taken back: True goes ahead."""
    answer = approve(pending, str(detail))
    return bool(await answer if inspect.isawaitable(answer) else answer)


async def run(session, goal, url=None, page=None, on_step=None, history=None, confirm=False, learn=False,
              said=False, values=None, secret=(), approve=None, max_steps=None, live=None):
    """Run one goal in a tab. Returns a result dict; the tab is left open for the caller to close.

    history: steps already done in this tab (a replayed skill that lost its way), so the loop carries on from them.
    confirm: stop before a click that can't be taken back (send, buy, book, delete...), with status
        needs_confirmation and the click in `pending`; nothing is clicked.
    approve: with confirm, ask instead of stopping: approve(pending, detail) (it may be async) is called before such
        a click. True clicks it and the run goes on (the next such click is asked about again); False ends the run
        with status cancelled.
    learn: also note what a skill needs: how the run ended, how the results list is read, and the JSON request
        behind the list if there is one (result["learned"]).
    values, secret: `goal` is a template with {{slots}} (see slots.py). The model gets it with the plain slots
        filled in and the `secret` ones left as tokens; a token it types is typed as its value. A slot with no value
        ends the run at once with status needs_input. values=None: the goal is taken as it is written.
    max_steps: at most this many actions (default questions.MAX_STEPS).
    live: {slot: 2FA secret} (totp.py): that slot types the code of the moment it is typed, and is secret.
    A finished run carries `items`: the rows of the page's list of results, read without a model (empty if none).
    """
    page = page or await session.new_tab(url)
    fill = Values(values, secret, live)
    templated = values is not None
    if templated:
        if missing := fill.missing(slots(goal)):
            return needs_values(page, missing)
        goal = fill.for_model(goal)
    secrets = fill.secrets() if templated else ()
    hidden = SECRETS.set(secrets)  # model.post_json takes these out of every request
    try:
        result = await _run(session, goal, page, on_step, history, confirm, learn, approve, max_steps or MAX_STEPS,
                            fill if templated else None)
    finally:
        SECRETS.reset(hidden)
    if secrets:  # whatever the page showed of a secret stays out of the result
        result = each_string(result, fill.mask)
    return result if said else plain(result)  # said: the messages as describe.Said, for the MCP server


async def _run(session, goal, page, on_step, history, confirm, learn, approve, max_steps, fill):
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000)  # noqa: E731
    timing = {"decide_ms": 0, "value_ms": 0}
    # What this run costs: the decision model bills input tokens; a text model, if used, bills separately.
    decide_usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    text_usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    history, decisions, text_calls, pending_text, stale = list(history or []), 0, 0, None, []
    status, detail, verdict, window, scores = "blocked", None, None, [], []
    items, learned, pending, approved = [], None, None, None
    capture = session.capture_json(page) if learn else None
    # Pages where the links inside closed menus (and the controls scrolled away inside a scroll box of their own) are
    # offered too, because the run got stuck there: the model chose BLOCKED, or scrolled MENU_SCROLL_STREAK times in a
    # row. Elsewhere they stay out, as every offered link costs tokens in every decision.
    menus_on = set()
    # Page states the run already waited on before taking BLOCKED: a page still loading (a spinner, a control about to
    # turn on) looks stuck for a few seconds; one wait per state tells that apart from a real dead end.
    # Pages (not page states: a clock or a carousel changes those every second) where a BLOCKED already waited, or
    # was already decided again without its useless steps.
    waited_on = set()
    retried_on = set()
    fell_back = set()  # pages where a BLOCKED was already turned into the model's best untried click
    picked_suggestions = set()  # suggestions chosen because they match what was typed (once each)
    # (kind, label, text) of an action that changed nothing, with the URL it was tried on: not done again there.
    useless = set()

    keys = goal_keys(goal)

    async def look(after=None):
        if _page_of(page.url) in menus_on:
            state = await session.observe(page, after=after, menus=True)
        else:
            state = await session.observe(page, after=after)
        extra = list(keys)
        last = history[-1] if history else None
        if last and last["kind"] == "fill" and searchlike(last) and not any(k["key"] == "Enter" for k in keys):
            label = last["action"]
            extra.append({"id": "key_enter", "kind": "key", "key": "Enter",
                          "label": f"Press Enter in {label!r} (submits it, as a search box's Search button does)"})
        return {**state, "actions": state["actions"] + extra} if extra else state

    try:
        state = await look()
        first_text = state["text"]
        while True:
            if decisions >= max_steps * 2 or len(history) >= max_steps:
                status, detail = "budget", f"{len(history)} actions / {decisions} decisions"
                break
            decision = await choose(state, goal, history)
            decisions += 1
            timing["decide_ms"] += decision["latency_ms"]
            add_usage(decide_usage, decision["usage"])
            action, operation = decision["action"], decision["operation"]
            if operation == "BLOCKED" and _page_of(state["url"]) not in menus_on:
                # Stuck: before giving up, look inside the page's closed menus and scroll boxes too (the page the goal
                # needs is often one link away, under a menu such as "Docs"). Once per page, and only if it has any.
                menus_on.add(_page_of(state["url"]))
                wider = await session.observe(page, menus=True)
                if any(_hidden(a) for a in wider["actions"]):
                    state, verdict = wider, None
                    continue
            if operation == "BLOCKED" and useless and _page_of(state["url"]) not in retried_on:
                # Blocked right after a step that changed nothing (a link that goes nowhere): that step is no way
                # forward, but another control may be. Decide once more without it.
                retried_on.add(_page_of(state["url"]))
                narrower = {**state, "actions": [a for a in state["actions"] if not _is_useless(a, state, useless)]}
                if any(a.get("kind") in ("click", "fill", "select") for a in narrower["actions"]):
                    state, verdict = narrower, None
                    continue
            if history and history[-1]["kind"] == "fill" and (pick := _typed_suggestion(history[-1], state)) \
                    and decision["action"].get("label") != pick["label"] and pick["label"] not in picked_suggestions \
                    and not irreversible(pick):
                # Typed "Amsterdam Centraal" and the list offers exactly that: choose it, as a person does. Many
                # forms take only a value picked from their list.
                picked_suggestions.add(pick["label"])
                action, operation = pick, "CLICK"
                decision = {**decision, "action": action, "operation": operation}
            if operation == "DONE" and (again := _short_of_count(goal, history, state)):
                # "Click Add exactly three times": counting repeats on a page is easy to get wrong, the history is not.
                action, operation = again, "CLICK"
                decision = {**decision, "action": action, "operation": operation}
            if operation == "BLOCKED" and (more := _feed_grows(history, state)):
                # A feed that loads more as it scrolls: the last scroll brought new content and the page goes on.
                action, operation = more, "SCROLL_DOWN"
                decision = {**decision, "action": action, "operation": operation}
            fallback = next((f for f in decision.get("fallback") or [] if f["probability"] >= FALLBACK_MIN
                             and not _tried_here(f["action"], state, history) and not irreversible(f["action"])
                             and not _is_useless(f["action"], state, useless)), None) if operation == "BLOCKED" else None
            if fallback and _page_of(state["url"]) not in fell_back:
                # BLOCKED while a click the model itself rated was never tried on this page (a row's menu next to a
                # link that went nowhere, a pager): try that click once before giving up. Once per page.
                fell_back.add(_page_of(state["url"]))
                action, operation = fallback["action"], "CLICK"
                decision = {**decision, "action": action, "operation": operation,
                            "probability": fallback["probability"]}
            if operation == "BLOCKED" and _page_of(state["url"]) not in waited_on:
                waited_on.add(_page_of(state["url"]))
                wait = getattr(session, "wait_for_change", None)  # a scripted test session may not have it
                if wait and await wait(page, state, BLOCKED_WAIT_SECONDS):
                    state, verdict = await look(), None
                    continue
            if operation in {"DONE", "BLOCKED"}:
                # The same verdict twice in a row on the same URL stands even if the page is still changing (late
                # widgets, live counters): deciding again would cost another full request for the same answer.
                if verdict != (operation, state["url"]) and not await session.fresh(page, state):
                    verdict = (operation, state["url"])
                    stale.append(Said(f"{ms()}ms {operation}: page changed before completion [",
                                      Quote(getattr(session, "last_diff", ""), bare=True), "]"))  # what changed
                    state = await look()
                    continue
                status = operation.lower()
                break
            verdict = None
            if _is_useless(action, state, useless):
                # The same step again, where it changed nothing last time: offer the page without it, once.
                state = {**state, "actions": [a for a in state["actions"] if a.get("label") != action["label"]
                                              or a.get("kind") != action["kind"]]}
                if any(a.get("kind") in ("click", "fill", "select") for a in state["actions"]):
                    continue
            if confirm and irreversible(action) and not (approved and same_control(approved, [action])):
                asked = {"kind": action["kind"], "label": action["label"], "role": action.get("role")}
                why = Said("stopped before ", Quote(action["label"][:80]),
                           ", which can't be taken back: confirm to go ahead")
                if approve is None:
                    status, pending, detail = "needs_confirmation", asked, why
                    break
                if not await ask(approve, asked, why):
                    status, detail = "cancelled", Said("not confirmed: ", Quote(action["label"][:80]))
                    break
                # The page may have moved on while the person decided: look again and find the same control.
                approved = {**asked, "nth": target_info(action, state["actions"])["nth"]}
                state = await look()
                action = same_control(approved, state["actions"])
                if action is None:
                    continue  # decided again on the new page; this control is not asked about twice
            text = text_info = None
            try:
                if action["kind"] in ("fill", "find"):
                    context = field_context(goal, action, state, history)
                    # The value comes from the goal, the field and the steps so far, not from the rest of the page:
                    # when the step is decided again because the page changed (a field that opened), reuse it.
                    key = {k: v for k, v in context.items() if k != "page"}
                    if pending_text and pending_text[0] == key:
                        text, text_info = pending_text[1], pending_text[2]
                    else:
                        text, text_info = await field_text(context)
                        text_calls += 1
                        timing["value_ms"] += text_info["latency_ms"]
                        add_usage(decide_usage if text_info.get("billed") else text_usage,
                                  text_info.get("usage"))
                        pending_text = (key, text, text_info)
                # a {{slot}} the model chose is typed as its value; the trace keeps the token
                await session.act(page, state, action, fill.fill(text) if fill else text)
            except MissingValue as e:
                status, detail = "needs_input", Said("No value given for ", Quote(token(str(e)), bare=True))
                break
            except NeedsInput as e:
                if action["kind"] == "find":  # the goal names no place to jump to: nothing happens, the loop sees that
                    pending_text, text = None, None
                else:
                    status, detail = "needs_input", Said("No value in the goal for field: ", Quote(str(e), bare=True))
                    break
            except StalePage as e:
                stale.append(Said(f"{ms()}ms {action['kind']} ", Quote(action["label"][:40]), f": {e} [",
                                  Quote(getattr(session, "last_diff", ""), bare=True), "]"))  # what changed
                state = await look()
                continue
            pending_text, approved = None, None
            history.append({
                "step": len(history) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "role": action.get("role"),
                "text": text,
                # who wrote `text`: the text model, or the decision model choosing from the goal's words (with its probability)
                "text_source": text_info and {k: text_info[k] for k in ("model", "probability") if k in text_info},
                "probability": decision["probability"],
                "alternatives": decision["alternatives"],
                "confidence": decision["confidence"],
                "decide_ms": decision["latency_ms"],
                "at_ms": ms(),
                "url": state["url"],  # where the action happened; with led_to, the model can see navigation circles
                "target": target_info(action, state["actions"]),  # how a skill finds this control again
            })
            new_state = await look(after=action)
            history[-1]["page_changed"] = new_state["marker"] != state["marker"]
            if action["kind"] == "scroll":
                history[-1]["grew"] = (new_state.get("scroll") or {}).get("height", 0) > \
                    (state.get("scroll") or {}).get("height", 0)
            if not history[-1]["page_changed"] and action["kind"] in ("click", "fill"):
                # Nothing happened: the same step is not tried again on this page (it would change nothing again).
                useless.add(_useless_key(action, state))
            elif history[-1]["page_changed"]:
                useless.clear()
            history[-1]["led_to"] = new_state["url"]
            state = new_state
            if on_step and inspect.isawaitable(reported := on_step(history[-1])):
                await reported
            scrolls = history[-MENU_SCROLL_STREAK:]
            if (len(scrolls) == MENU_SCROLL_STREAK and all(h["kind"] == "scroll" for h in scrolls)
                    and _page_of(state["url"]) not in menus_on):
                # Scrolling on and on is searching: the menus may hold the way there.
                menus_on.add(_page_of(state["url"]))
                state = await look()
            last = history[-3:]
            if len(last) == 3 and all(not h["page_changed"] and h["kind"] != "wait" for h in last):
                status, detail = "blocked", "Three actions in a row did not change the page."
                break
            if len(history) >= MAX_SCROLL_STREAK and all(h["kind"] == "scroll" for h in history[-MAX_SCROLL_STREAK:]):
                status, detail = "blocked", f"Scrolled {MAX_SCROLL_STREAK} times in a row without finding the goal."
                break
        elapsed = ms()
        chunks = split_chunks(await session.settled_markdown(page))
        # The answer is where the run ended: rank the blocks around the final screen. A run that did not finish has
        # no answer to rank, so it returns what was on screen without a ranking request.
        start = screen_anchor(chunks, state["text"])
        window = chunks[start:start + MAX_RANKED_BLOCKS]
        if status == "done":
            scores, rank_usage = await rank_blocks(goal, window)
            add_usage(decide_usage, rank_usage)
        title = "" if getattr(page, "_ca_dialog", None) is not None else await page.title()
        if status == "done":
            found = best_list(await session.lists(page))  # the page's list of results, read with no model
            items = found["rows"] if found else []
            if learn:
                captured = await capture.stop()
                learned = {"end": {"url": page.url, "title": title, "lines": end_lines(first_text, state["text"])},
                           "list": {"item": found["item"], "fields": found["fields"]} if found else None,
                           "shortcut": find_shortcut(captured, found["rows"], page.url) if found else None}
    except (BrowserClosed, PlaywrightError, StalePage) as e:
        # The user (or something else) closed the browser or the tab mid-task: report it, keep the trace.
        if not session.is_gone(page):
            raise
        what = "browser was closed" if session.closed else "tab was closed or crashed"
        status, detail = "error", f"the {what} during the task ({type(e).__name__})"
        elapsed, window, scores, title = ms(), [], [], ""
    finally:
        if capture is not None:
            await capture.stop()  # idempotent: also when the run ended early or with an error
    ranked = sorted(range(len(scores)), key=lambda i: -scores[i])
    keep = [i for i in ranked if scores[i] >= 0.5][:TOP_BLOCKS] or ranked[:3] or list(range(min(3, len(window))))
    result = {
        "status": status,
        "detail": detail,
        "url": page.url,
        "title": title,
        "elapsed_ms": elapsed,
        "actions": len(history),
        "decisions": decisions,
        "text_calls": text_calls,
        "timing": {**timing, "browser_ms": elapsed - timing["decide_ms"] - timing["value_ms"]},
        "usage": {**decide_usage, "estimated_usd": round(estimate_usd(decide_usage["input_tokens"]), 6),
                  "text_model": text_usage},
        "stale": stale,
        "trace": history,
        "markdown": await resolve_redirects(
            "\n\n".join(window[i] for i in sorted(keep)),
            page.context.request if session.direct and not session.is_gone(page) else None,
        ),
        "block_scores": [round(scores[i], 3) for i in sorted(keep)] if scores else [],  # empty: not ranked
        "items": items,
        **({"pending": pending} if pending else {}),
        **({"learned": learned} if learned else {}),
    }
    return result


async def step(session, page, state=None, op=None, target=None, instruction=None, text=None):
    """One action outside the loop, for a caller taking over a task (the MCP `act` tool).

    By index: `op` + `target` as shown in `state`, the snapshot the caller looked at. No model call; `text` is
    typed exactly as given. By instruction: the page is observed and the decision model picks one step for
    `instruction`; a field without `text` gets its value the usual way (from the instruction's own words).
    A page that changed since `state` raises StalePage and nothing is done: the caller looks again.
    """
    if (op is None) == (instruction is None):
        raise ValueError("give either op (with target) or instruction")
    usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    decision = None
    if op:
        if state is None:
            raise ValueError("no snapshot of this tab yet: call snapshot first")
        _, targets, controls = page_view(state, [])
        action = resolve(targets, controls, op.upper(), None if target is None else str(target))
        if action["kind"] in ("fill", "find") and text is None:
            raise ValueError(f"{op.upper()} needs text: " + ("the value to type" if action["kind"] == "fill"
                                                                else "the words to jump to"))
    else:
        state = await session.observe(page)
        decision = await choose(state, instruction, [])
        add_usage(usage, decision["usage"])
        action = decision["action"]
        if decision["operation"] in {"DONE", "BLOCKED"}:
            return {"executed": False, "action": action, "decision": decision, "text": None, "state": state,
                    "page_changed": False, "usage": usage}
        if action["kind"] in ("fill", "find") and text is None:
            text, info = await field_text(field_context(instruction, action, state, []))  # NeedsInput propagates
            if info.get("billed"):
                add_usage(usage, info.get("usage"))
    await session.act(page, state, action, text)
    new_state = await session.observe(page, after=action)
    return {"executed": True, "action": action, "decision": decision, "text": text, "state": new_state,
            "page_changed": new_state["marker"] != state["marker"], "usage": usage}
