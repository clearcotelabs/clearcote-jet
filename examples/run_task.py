"""Run one goal on Clearcote in a visible window, with the mouse cursor drawn, and print every decision.

    python examples/run_task.py                                   # the default: a GOV.UK lookup
    python examples/run_task.py "your goal" https://start.page

Needs TYPESAFE_API_KEY in ./.env or the environment. The browser profile is a throwaway directory, deleted at the end;
the full result (every step with its probability and runner-ups) is saved to runs/.
"""

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from clearcote_jet.agent import run
from clearcote_jet.browser import Session
from clearcote_jet.cli import load_env_file

REPO = Path(__file__).resolve().parent.parent
GOAL = "Find the date of the next bank holiday in England and Wales."
URL = "https://www.gov.uk/"


def show(step):
    alts = ", ".join(f"{label!r} {p}" for label, p in step["alternatives"])
    typed = f" = {step['text']!r}" if step["text"] else ""
    print(f"{step['step']:>2}. {step['kind']:6} {step['action'][:60]!r}{typed}\n"
          f"    p={step['probability']}  ({step['decide_ms']} ms)" + (f"   runner-ups: {alts}" if alts else ""), flush=True)


async def main(goal, url):
    load_env_file(REPO / ".env")
    profile = Path(tempfile.mkdtemp(prefix="clearcote-jet-"))
    session = None
    try:
        session = await Session.launch(profile=profile, headless=False, humanize=True, show_cursor=True)
        page = await session.new_tab(url)
        print(f"goal: {goal}\n", flush=True)
        result = await run(session, goal, page=page, on_step=show)
        t = result["timing"]
        print(f"\nstatus: {result['status']}" + (f" ({result['detail']})" if result["detail"] else ""))
        print(f"{result['actions']} actions, {result['decisions']} decisions, {result['elapsed_ms'] / 1000:.1f} s "
              f"(deciding {t['decide_ms'] / 1000:.1f} s, typed values {t['value_ms'] / 1000:.1f} s, "
              f"browser {t['browser_ms'] / 1000:.1f} s)")
        print("page:", result["title"])
        print("content:\n  " + result["markdown"][:900].replace("\n", "\n  "), flush=True)
        out = REPO / "runs" / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({"goal": goal, "url": url, **result}, indent=1, ensure_ascii=False), encoding="utf-8")
        print("saved:", out, "\nleaving the window open for 30 s (close it to finish sooner)", flush=True)
        for _ in range(60):
            if session.closed or page.is_closed():
                break
            await asyncio.sleep(0.5)
    finally:
        if session:  # also after the window was closed by hand: close() is what stops the Playwright driver
            with contextlib.suppress(Exception):
                await session.close()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main(*(sys.argv[1:3] if len(sys.argv) >= 3 else (GOAL, URL))))
