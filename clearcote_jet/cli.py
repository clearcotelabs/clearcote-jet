"""clearcote-jet browser  → start a headed Clearcote browser with a CDP port and keep it running.
clearcote-jet run      → run one goal (connects with --cdp, otherwise launches its own browser)."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from .agent import run
from .browser import DEFAULT_PROFILE, Session
from .skills import run_with_skill


def load_env_file(path=".env"):
    """KEY=value lines from ./.env into the environment. Already-set variables win; empty values are skipped."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)


def print_step(step):
    text = f" ← {step['text']!r}" if step["text"] else ""
    how = "replayed" if step.get("replayed") else f"p={step['probability']}"
    print(f"[{step['at_ms']:>6} ms] {step['kind']:6} {step['action'][:70]}{text}  ({how})", flush=True)


async def serve_browser(args):
    session = await Session.launch(profile=args.profile, headless=args.headless, cdp_port=args.port)
    print(f"Clearcote running. Connect with: --cdp http://127.0.0.1:{args.port}   (Ctrl-C to close)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await session.close()


async def run_goal(args):
    humanize = not args.no_humanize
    if args.cdp:
        session = await Session.connect(args.cdp, humanize=humanize)
    else:
        # show_cursor draws the SDK's cursor overlay so you can watch the mouse path. It is injected into the
        # page (visible to page scripts), so it is for demos only.
        session = await Session.launch(profile=args.profile, headless=args.headless, humanize=humanize,
                                       show_cursor=args.show_cursor)
    try:
        page = await session.new_tab(args.url)
        if args.url and not args.no_skill:  # learned once, then replayed with no model (see skills.py)
            result = await run_with_skill(session, args.goal, url=args.url, page=page, on_step=print_step,
                                          confirm=args.confirm)
        else:
            result = await run(session, args.goal, page=page, on_step=print_step, confirm=args.confirm)
        print(json.dumps({k: v for k, v in result.items() if k != "trace"}, indent=2, ensure_ascii=False))
        if args.keep_open and not args.cdp:
            print("Browser kept open (Ctrl-C to close).", flush=True)
            await asyncio.Event().wait()
        # connect mode: the tab stays open in the running browser for inspection.
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(prog="clearcote-jet")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("browser")
    b.add_argument("--port", type=int, default=9222)
    r = sub.add_parser("run")
    r.add_argument("--goal", required=True)
    r.add_argument("--url")
    r.add_argument("--cdp", help="e.g. http://127.0.0.1:9222 (from `clearcote-jet browser`)")
    r.add_argument("--keep-open", action="store_true")
    r.add_argument("--no-humanize", action="store_true", help="instant clicks/typing: faster, less human-looking")
    r.add_argument("--show-cursor", action="store_true", help="demo: draw the mouse cursor (visible to the page)")
    r.add_argument("--confirm", action="store_true",
                   help="stop before a click that can't be taken back (send, buy, book, delete...)")
    r.add_argument("--no-skill", action="store_true",
                   help="work it out step by step even if this task was learned before, and save nothing")
    for p in (b, r):
        p.add_argument("--profile", default=str(DEFAULT_PROFILE))
        p.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    load_env_file()
    try:
        asyncio.run(serve_browser(args) if args.command == "browser" else run_goal(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
