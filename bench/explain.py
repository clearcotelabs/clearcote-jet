"""Run one benchmark task and print every decision: what the model chose, and which offered actions match --grep.

    python bench/explain.py real-dq-text-box --grep submit
    python bench/explain.py local-table-details --grep "1043"
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

from clearcote_jet import agent  # noqa: E402
from clearcote_jet.browser import Session  # noqa: E402
from run import load_tasks, model_key  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--grep", default="")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    model_key()
    task = next(t for t in load_tasks("all") if t["id"] == args.task)
    server, url = None, task["url"]
    if task["kind"] == "local":
        import server as fixture_server
        server, base = fixture_server.start(0)
        url = base + task["url"]
    real_choose = agent.choose

    async def choose(state, goal, history):
        decision = await real_choose(state, goal, history)
        matches = [a["label"][:70] for a in state["actions"] if args.grep and args.grep.lower() in a["label"].lower()]
        print(f"{len(history):>2}. {decision['operation']:<9} {decision['action'].get('label', '')[:70]!r}  "
              f"p={decision.get('probability')}  offered({args.grep}): {matches}  of {len(state['actions'])}")
        return decision
    agent.choose = choose
    profile = Path(tempfile.mkdtemp(prefix="jet-explain-"))
    session = await Session.launch(profile=profile, headless=True, humanize=True)
    try:
        page = await session.new_tab(url)
        result = await agent.run(session, task["goal"], page=page, max_steps=task.get("max_steps", 30))
        print(f"-- {result.get('status')}: {str(result.get('detail') or '')[:200]}")
    finally:
        await session.close()
        shutil.rmtree(profile, ignore_errors=True)
        if server:
            server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
