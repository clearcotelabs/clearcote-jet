"""The loop: observe → the decision model picks a step → (a value for TYPE_TEXT) → humanized act → repeat."""

import inspect
import time

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserClosed, StalePage
from .model import (MAX_RANKED_BLOCKS, NeedsInput, add_usage, choose, estimate_usd, field_context, field_text,
                    rank_blocks, resolve_redirects, screen_anchor, split_chunks)
from .questions import MAX_SCROLL_STREAK, MAX_STEPS

TOP_BLOCKS = 8


async def run(session, goal, url=None, page=None, on_step=None):
    """Run one goal in a tab. Returns a result dict; the tab is left open for the caller to close."""
    page = page or await session.new_tab(url)
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000)  # noqa: E731
    timing = {"decide_ms": 0, "value_ms": 0}
    # What this run costs: the decision model bills input tokens; a text model, if used, bills separately.
    decide_usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    text_usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    history, decisions, text_calls, pending_text, stale = [], 0, 0, None, []
    status, detail, verdict, window, scores = "blocked", None, None, [], []
    try:
        state = await session.observe(page)
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
            text = text_info = None
            try:
                if action["kind"] == "fill":
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
    except (BrowserClosed, PlaywrightError, StalePage) as e:
        # The user (or something else) closed the browser or the tab mid-task: report it, keep the trace.
        if not session.is_gone(page):
            raise
        what = "browser was closed" if session.closed else "tab was closed or crashed"
        status, detail = "error", f"the {what} during the task ({type(e).__name__})"
        elapsed, window, scores, title = ms(), [], [], ""
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
    }
