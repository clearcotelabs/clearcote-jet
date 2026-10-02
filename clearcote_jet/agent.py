"""The loop: observe → the decision model picks a step → (a value for TYPE_TEXT) → humanized act → repeat."""

import inspect
import time

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserClosed, StalePage
from .describe import best_list, end_lines, irreversible, target_info
from .model import (MAX_RANKED_BLOCKS, NeedsInput, add_usage, choose, estimate_usd, field_context, field_text,
                    page_view, rank_blocks, resolve, resolve_redirects, screen_anchor, split_chunks)
from .questions import MAX_SCROLL_STREAK, MAX_STEPS
from .shortcut import find_shortcut

TOP_BLOCKS = 8


async def run(session, goal, url=None, page=None, on_step=None, history=None, confirm=False, learn=False):
    """Run one goal in a tab. Returns a result dict; the tab is left open for the caller to close.

    history: steps already done in this tab (a replayed skill that lost its way), so the loop carries on from them.
    confirm: stop before a click that can't be taken back (send, buy, book, delete...), with status
        needs_confirmation and the click in `pending`; nothing is clicked.
    learn: also note what a skill needs: how the run ended, how the results list is read, and the JSON request
        behind the list if there is one (result["learned"]).
    A finished run carries `items`: the rows of the page's list of results, read without a model (empty if none).
    """
    page = page or await session.new_tab(url)
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000)  # noqa: E731
    timing = {"decide_ms": 0, "value_ms": 0}
    # What this run costs: the decision model bills input tokens; a text model, if used, bills separately.
    decide_usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    text_usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    history, decisions, text_calls, pending_text, stale = list(history or []), 0, 0, None, []
    status, detail, verdict, window, scores = "blocked", None, None, [], []
    items, learned, pending = [], None, None
    capture = session.capture_json(page) if learn else None
    try:
        state = await session.observe(page)
        first_text = state["text"]
        while True:
            if decisions >= MAX_STEPS * 2 or len(history) >= MAX_STEPS:
                status, detail = "budget", f"{len(history)} actions / {decisions} decisions"
                break
            decision = await choose(state, goal, history)
            decisions += 1
            timing["decide_ms"] += decision["latency_ms"]
            add_usage(decide_usage, decision["usage"])
            action, operation = decision["action"], decision["operation"]
            if operation in {"DONE", "BLOCKED"}:
                # The same verdict twice in a row on the same URL stands even if the page is still changing (late
                # widgets, live counters): deciding again would cost another full request for the same answer.
                if verdict != (operation, state["url"]) and not await session.fresh(page, state):
                    verdict = (operation, state["url"])
                    stale.append(f"{ms()}ms {operation}: page changed before completion"
                                 f" [{getattr(session, 'last_diff', '')}]")  # what changed
                    state = await session.observe(page)
                    continue
                status = operation.lower()
                break
            verdict = None
            if confirm and irreversible(action):
                status, pending = "needs_confirmation", {"kind": action["kind"], "label": action["label"],
                                                         "role": action.get("role")}
                detail = f"stopped before {action['label'][:80]!r}, which can't be taken back: confirm to go ahead"
                break
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
                await session.act(page, state, action, text)
            except NeedsInput as e:
                if action["kind"] == "find":  # the goal names no place to jump to: nothing happens, the loop sees that
                    pending_text, text = None, None
                else:
                    status, detail = "needs_input", f"No value in the goal for field: {e}"
                    break
            except StalePage as e:
                stale.append(f"{ms()}ms {action['kind']} {action['label'][:40]!r}: {e}"
                             f" [{getattr(session, 'last_diff', '')}]")  # what changed
                state = await session.observe(page)
                continue
            pending_text = None
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
            new_state = await session.observe(page, after=action)
            history[-1]["page_changed"] = new_state["marker"] != state["marker"]
            history[-1]["led_to"] = new_state["url"]
            state = new_state
            if on_step and inspect.isawaitable(reported := on_step(history[-1])):
                await reported
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
        title = await page.title()
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
    return {
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
