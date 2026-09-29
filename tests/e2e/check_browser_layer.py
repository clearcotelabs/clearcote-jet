"""Headed end-to-end check of clearcote_jet.browser with scripted decisions (no model calls).

Proves on real Clearcote: humanize is installed, reads are invisible to the page, only reachable controls are
offered, every input event is trusted, clicks arrive after a mouse path, the native <select> goes by keyboard,
scroll works, and markdown extraction sees the result. The profile dir is deleted afterwards.
"""

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

from clearcote_jet.browser import Session

HERE = Path(__file__).parent
FIXTURE = (HERE / "fixture.html").resolve().as_uri()
failures = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def pick(state, kind, label):
    for a in state["actions"]:
        if a["kind"] == kind and label in a["label"]:
            return a
    return None


async def main():
    profile = Path(tempfile.mkdtemp(prefix="ccagent-e2e-", dir=HERE))
    session = None
    try:
        t = time.perf_counter()
        session = await Session.launch(profile=profile, headless=False, humanize=True)
        print(f"launched in {time.perf_counter() - t:.1f}s; tabs={len(session.context.pages)}", flush=True)
        page = await session.new_tab(FIXTURE)
        check(len(session.context.pages) == 1, "the initial blank tab is reused (one tab)")
        check(page.mouse.move.__name__ == "hmove" and hasattr(page, "_clearcote_persona"),
              f"SDK humanize is installed on the page (mouse.move={page.mouse.move.__name__})")

        state = await session.observe(page)
        labels = [f"{a['kind']}:{a['label']}" for a in state["actions"]]
        print("  offered:", labels, flush=True)
        check(pick(state, "fill", "Reader number") is not None, "text field offered as fill")
        check(pick(state, "select", "Collect from → Harbour branch") is not None, "select option offered")
        check(pick(state, "select", "North branch") is None, "disabled option not offered")
        check(pick(state, "click", "Renew all loans") is None, "control behind the curtain not offered")
        check(not any("PIN" in x for x in labels), "password field never offered")
        check(pick(state, "click", "Back to the top") is None, "off-screen control not offered")
        check(pick(state, "scroll", "Scroll down") is not None, "scroll down offered")

        action = pick(state, "fill", "Reader number")
        await session.act(page, state, action, "A-4417")
        state = await session.observe(page, after=action)
        check(await page.input_value("#reader") == "A-4417", "fill typed the reader number")

        action = pick(state, "select", "Airport kiosk")
        await session.act(page, state, action)
        state = await session.observe(page, after=action)
        check(await page.eval_on_selector("#branch", "e => e.value") == "airport",
              "select reached 'airport' past a disabled option")

        action = pick(state, "click", "Send request")
        await session.act(page, state, action)
        state = await session.observe(page, after=action)
        check("Requested: A-4417 / airport" in state["text"],
              "click sent the request: " + repr(await page.text_content("#out")))

        action = pick(state, "scroll", "Scroll down")
        before = await page.evaluate("scrollY")
        await session.act(page, state, action)
        await asyncio.sleep(0.6)
        state = await session.observe(page, after=action)
        check(await page.evaluate("scrollY") > before, "scroll moved the page")
        while pick(state, "click", "Back to the top") is None and pick(state, "scroll", "Scroll down"):
            action = pick(state, "scroll", "Scroll down")
            await session.act(page, state, action)
            await asyncio.sleep(0.6)
            state = await session.observe(page, after=action)
        action = pick(state, "click", "Back to the top")
        check(action is not None, "control becomes offered once scrolled into view")
        if action:
            await session.act(page, state, action)

        await page.evaluate("scrollTo(0, 0)")
        md = await session.settled_markdown(page, quiet=0.3, cap=2)
        check("Requested: A-4417 / airport" in md and "# Loan request" in md, "markdown extraction sees the result")

        rec = await page.evaluate("window.rec")
        kinds = {}
        for e in rec["events"]:
            kinds.setdefault(e["t"], []).append(e["trusted"])
        print("  events:", {k: f"{sum(v)}/{len(v)} trusted" for k, v in kinds.items()}, flush=True)
        check(all(e["trusted"] for e in rec["events"]), "every input event is isTrusted")
        check(True in kinds.get("change", []), "select fired a trusted change event")
        branch_changes = [e for e in rec["events"] if e["t"] == "change" and e["id"] == "branch"]
        check(len(branch_changes) == 1, f"choosing an option fired exactly one change ({len(branch_changes)})")
        check(len(kinds.get("keydown", [])) >= len("A-4417"), "typing was per-key")
        check((rec["movesBeforeFirstDown"] or 0) >= 5,
              f"first press came after a mouse path ({rec['movesBeforeFirstDown']} mousemoves)")
        clicks = [e for e in rec["events"] if e["t"] == "click" and e["id"] == "send"]
        box = await page.eval_on_selector("#send", "e => { const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }")
        check(bool(clicks) and (abs(clicks[0]["x"] - box[0]) > 0.5 or abs(clicks[0]["y"] - box[1]) > 0.5),
              "click did not land on the exact center")
        check(rec["qsa"] == 0 and rec["efp"] == 0,
              f"page hooks saw no agent DOM reads (querySelectorAll={rec['qsa']}, elementFromPoint={rec['efp']})")
        check(not rec["cacheSeen"], "the element cache is invisible to the page")
        await asyncio.sleep(1.5)  # leave the result on screen for a moment
    finally:
        if session:
            await session.close()
        for _ in range(20):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            await asyncio.sleep(0.5)
        check(not profile.exists(), "profile dir removed")
    print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}", flush=True)
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
