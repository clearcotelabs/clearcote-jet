"""A gallery of goals on ordinary sites, all in one visible window (one tab each), with a summary at the end.

    python examples/gallery.py                 # every task
    python examples/gallery.py news docs-version # just these

Needs TYPESAFE_API_KEY in ./.env or the environment. Results are saved to runs/.
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
# name, start url, goal
TASKS = [
    ("docs-search", "https://docs.python.org/3/",
     "Search the Python documentation for asyncio.gather and open its entry."),
    ("docs-version", "https://docs.python.org/3/library/asyncio-task.html",
     "Switch this documentation page to Python version 3.12."),
    ("bank-holiday", "https://www.gov.uk/",
     "Find the date of the next bank holiday in England and Wales."),
    ("news", "https://news.ycombinator.com/",
     "Open the comments page of the top story."),
]


def show(step):
    alts = ", ".join(f"{label[:40]!r} {p}" for label, p in step["alternatives"])
    typed = f" = {step['text']!r}" if step["text"] else ""
    print(f"   {step['step']:>2}. {step['kind']:6} {step['action'][:55]!r}{typed}  p={step['probability']} "
          f"({step['decide_ms']} ms)" + (f"  runner-ups: {alts}" if alts else ""), flush=True)


async def main(selected):
    load_env_file(REPO / ".env")
    tasks = [t for t in TASKS if not selected or t[0] in selected]
    profile = Path(tempfile.mkdtemp(prefix="clearcote-jet-"))
    session, summary = None, []
    (REPO / "runs").mkdir(exist_ok=True)
    try:
        session = await Session.launch(profile=profile, headless=False, humanize=True, show_cursor=True)
        for name, url, goal in tasks:
            print(f"\n== {name}: {goal}", flush=True)
            page = await session.new_tab(url)
            try:
                result = await run(session, goal, page=page, on_step=show)
            except Exception as e:  # report it and go on with the next task
                print(f"   ERROR {type(e).__name__}: {str(e)[:200]}", flush=True)
                summary.append((name, "error", 0, 0, 0))
                continue
            print(f"   -> {result['status']}" + (f" ({result['detail']})" if result["detail"] else "")
                  + f" | {result['actions']} actions, {result['decisions']} decisions, {result['elapsed_ms'] / 1000:.1f} s"
                  + f" | {result['url'][:80]}", flush=True)
            out = REPO / "runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
            out.write_text(json.dumps({"goal": goal, **result}, indent=1, ensure_ascii=False), encoding="utf-8")
            summary.append((name, result["status"], result["actions"], result["decisions"], result["elapsed_ms"]))
        print("\nsummary")
        for name, status, actions, decisions, ms in summary:
            print(f"  {name:20} {status:12} {actions:>2} actions {decisions:>2} decisions {ms / 1000:5.1f} s")
        print("leaving the window open for 30 s", flush=True)
        for _ in range(60):
            if session.closed:
                break
            await asyncio.sleep(0.5)
    finally:
        if session:
            with contextlib.suppress(Exception):
                await session.close()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main(set(sys.argv[1:])))
