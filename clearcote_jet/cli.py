"""clearcote-jet browser  → start a headed Clearcote browser with a CDP port and keep it running.
clearcote-jet run      → run one goal (connects with --cdp, otherwise launches its own browser).
clearcote-jet read     → work out a selector for values described in words, and read them.
clearcote-jet worker   → run one task for another program: a JSON request on stdin, JSON events on stdout
                         (see worker.py)."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from .agent import run
from .browser import DEFAULT_PROFILE, IsolatedWorld, Session
from .read import as_json, read_values
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


async def read_page(args):
    """`read`: attach (or launch), find the page, and answer with the selectors and what they read.

    In connect mode the tab that is already open is the one read — the point is to read the page a
    check has ALREADY loaded, not to fetch it a second time.
    """
    try:
        want = json.loads(args.want)
    except ValueError as e:
        raise SystemExit(f"--want is not JSON: {e}")
    if not isinstance(want, dict) or not isinstance(want.get("fields"), list):
        raise SystemExit('--want needs {"fields": [...]}')
    if args.row:
        want["row"] = args.row

    session = await (Session.connect(args.cdp, humanize=False) if args.cdp
                     else Session.launch(profile=args.profile, headless=args.headless, humanize=False))
    try:
        page = await open_or_reuse(session, args.url or want.get("url"))
        print(as_json(await read_values(session, page, want)))
    finally:
        await session.close()


async def open_or_reuse(session, url):
    """The tab already showing that page, or a new one. A check has loaded it; loading it again
    would cost its megabytes twice and could read a different page than the one being watched."""
    pages = [p for p in session.context.pages if p.url and p.url != "about:blank"]
    if url:
        for p in pages:
            if p.url.split("#")[0] == url.split("#")[0]:
                return prepare(p)
    if pages and not url:
        return prepare(pages[0])
    return await session.new_tab(url)


def prepare(page):
    """A tab this process did not open still needs the isolated world and the frame caches that
    new_tab() installs, or nothing can be evaluated in it."""
    if not hasattr(page, "_ca_world"):
        page.on("crash", lambda *_: setattr(page, "_ca_crashed", True))
        page._ca_world = IsolatedWorld(page)
        page._ca_main, page._ca_frames, page._ca_oopif = None, {}, {}
    return page


def task_values(args):
    """--values / --secret: the goal is a template with {{slots}} (see slots.py). -> (values or None, secret names)"""
    if args.values is None:
        if args.secret:
            raise SystemExit("--secret names a slot of --values: give --values as well")
        return None, []
    try:
        values = json.loads(args.values)
    except ValueError as e:
        raise SystemExit(f"--values is not JSON: {e}")
    if not isinstance(values, dict):
        raise SystemExit('--values needs a JSON object, e.g. {"email": "a@b.example"}')
    return {k: str(v) for k, v in values.items()}, list(args.secret or [])


async def run_goal(args):
    humanize = not args.no_humanize
    values, secret = task_values(args)
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
                                          confirm=args.confirm, values=values, secret=secret)
        else:
            result = await run(session, args.goal, page=page, on_step=print_step, confirm=args.confirm,
                               values=values, secret=secret)
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
    r.add_argument("--values", help='JSON values for the {{slots}} in --goal, e.g. {"email": "a@b.example"}')
    r.add_argument("--secret", action="append", metavar="SLOT",
                   help="a slot whose value the model never sees and nothing writes down (repeat for more)")
    d = sub.add_parser("read", help="work out a selector for values described in words, and read them")
    d.add_argument("--want", required=True,
                   help='JSON: {"fields":[{"name":"price","type":"money","ask":"the monthly price"}],'
                        '"container":false,"note":""}')
    d.add_argument("--cdp", help="attach to a running browser instead of launching one")
    d.add_argument("--url", help="open this first; without it the tab already open is read")
    d.add_argument("--row", help="read inside this repeating row, and give selectors relative to it")
    sub.add_parser("worker", help="run one task for another program: JSON lines on stdin and stdout")
    for p in (b, r, d):
        p.add_argument("--profile", default=str(DEFAULT_PROFILE))
        p.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    load_env_file()
    if args.command == "worker":
        from .worker import main as worker_main
        worker_main()
        return
    try:
        asyncio.run(serve_browser(args) if args.command == "browser"
                    else read_page(args) if args.command == "read"
                    else run_goal(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
