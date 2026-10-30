"""Print what Jet is offered on a page: its actions as the model sees them, optionally after clicking some of them.

    python bench/peek.py settings-dropdowns.html                     # a fixture (path) or a full URL
    python bench/peek.py settings-dropdowns.html --click "Language"  # click the first action whose label has this
    python bench/peek.py https://demoqa.com/text-box --grep Submit    # only actions whose label has this
"""

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from clearcote_jet.browser import Session  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--click", action="append", default=[])
    ap.add_argument("--grep", default="")
    ap.add_argument("--scroll", type=int, default=0, help="scroll down this many screens first")
    ap.add_argument("--js", default="", help="print the value of this expression on the page")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    server = None
    url = args.url
    if not url.startswith("http"):
        import server as fixture_server
        server, base = fixture_server.start(0)
        url = base + "/" + url.lstrip("/")
    profile = Path(tempfile.mkdtemp(prefix="jet-peek-"))
    session = await Session.launch(profile=profile, headless=True, humanize=False)
    try:
        page = await session.new_tab(url)
        await asyncio.sleep(1.5)
        for _ in range(args.scroll):
            await page.mouse.wheel(0, 700)
            await asyncio.sleep(0.4)
        if args.js:
            print("-- js:", await page.evaluate(args.js))
        state = await session.observe(page)
        for want in args.click:
            action = next(a for a in state["actions"] if want.lower() in a["label"].lower())
            print(f"-- click {action['label']!r}")
            await session.act(page, state, action)
            await asyncio.sleep(0.8)
            state = await session.observe(page)
        for a in state["actions"]:
            if args.grep.lower() in a["label"].lower():
                extra = {k: a[k] for k in ("covered", "menu", "panel", "value", "frame") if a.get(k) not in (None, "")}
                print(f"{a['id']:>5} {a['kind']:<7} {a.get('role', ''):<10} {a['label'][:110]} {extra or ''}")
        print(f"-- {len(state['actions'])} actions; text starts: {state['text'][:200]!r}")
    finally:
        await session.close()
        shutil.rmtree(profile, ignore_errors=True)
        if server:
            server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
