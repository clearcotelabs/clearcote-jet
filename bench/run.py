"""Run the stuck-task benchmark: every task in a fresh tab of a real Clearcote browser, judged by its own check.

    python -I bench/run.py --label baseline                 # all tasks, 3 browsers at a time
    python -I bench/run.py --label t1 --only hk- dq-        # tasks whose id contains any of these
    python -I bench/run.py --label base --local --repeat 2  # local fixtures only, each task twice

Each run uses the agent loop directly (no saved skills), so it measures working a task out, not replaying one.
The model key comes from TYPESAFE_API_KEY, else ./.env, else the Profile Manager's saved key (read here, never
printed). Results go to bench/results/<label>.json; a summary is printed at the end.
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from clearcote_jet.agent import run  # noqa: E402
from clearcote_jet.browser import Session  # noqa: E402
from clearcote_jet.cli import load_env_file  # noqa: E402

TASK_TIMEOUT = int(os.environ.get("BENCH_TIMEOUT") or 300)


class Ctx:
    def __init__(self, result, page, record, base):
        self.result, self.page, self.record, self.base = result, page, record, base


def model_key():
    if os.environ.get("TYPESAFE_API_KEY"):
        return
    load_env_file(HERE.parent / ".env")
    if os.environ.get("TYPESAFE_API_KEY"):
        return
    for folder in ("clearcote-profile-manager", "Clearcote Profile Manager"):
        p = Path(os.environ.get("APPDATA", "")) / folder / "settings.json"
        try:
            key = (json.loads(p.read_text(encoding="utf-8")).get("jetApiKey") or "").strip()
        except (OSError, ValueError):
            continue
        if key:
            os.environ["TYPESAFE_API_KEY"] = key
            return
    sys.exit("No model key: set TYPESAFE_API_KEY or save one in the Profile Manager (Settings > Tasks).")


def load_tasks(which):
    tasks = []
    if which in ("all", "local"):
        from tasks_local import TASKS as local
        tasks += [{**t, "kind": "local"} for t in local]
    if which in ("all", "real"):
        from tasks_real import TASKS as real
        tasks += [{**t, "kind": "real"} for t in real]
    return tasks


def brief_trace(trace):
    return [{k: s.get(k) for k in ("step", "kind", "action", "text", "probability", "page_changed", "url", "led_to")}
            for s in trace]


async def record_of(base, task_id):
    if not base:
        return None
    import urllib.request
    loop = asyncio.get_running_loop()

    def get():
        with urllib.request.urlopen(f"{base}/api/done/{task_id}", timeout=10) as r:
            return json.loads(r.read().decode("utf-8") or "null")
    return await loop.run_in_executor(None, get)


async def one(session, task, base, attempt):
    url = base + task["url"] if task["kind"] == "local" else task["url"]
    started = time.perf_counter()
    out = {"id": task["id"], "attempt": attempt, "tags": task.get("tags", []), "goal": task["goal"], "start": url}
    if task["kind"] == "local":
        import server as fixture_server
        with fixture_server._lock:
            fixture_server._records.pop(task["id"], None)  # judge this run, not an earlier attempt's record
    page = await asyncio.wait_for(session.new_tab(url), 60)
    try:
        job = asyncio.ensure_future(run(session, task["goal"], page=page, max_steps=task.get("max_steps", 30)))
        done, _ = await asyncio.wait({job}, timeout=TASK_TIMEOUT)
        if not done:
            if os.environ.get("BENCH_STACK"):
                c = job.get_coro()  # where the run hangs: the chain of awaits, innermost last
                while c is not None:
                    f = getattr(c, "cr_frame", None) or getattr(c, "gi_frame", None)
                    if f is not None:
                        print(f"   at {f.f_code.co_filename.split(chr(92))[-1]}:{f.f_lineno} {f.f_code.co_name}")
                    c = getattr(c, "cr_await", None) or getattr(c, "gi_yieldfrom", None)
            job.cancel()
            raise asyncio.TimeoutError
        result = job.result()
    except asyncio.TimeoutError:
        result = {"status": "timeout", "detail": f"over {TASK_TIMEOUT}s", "trace": [], "url": page.url,
                  "markdown": "", "items": []}
    except Exception as e:  # a crash is a result too
        result = {"status": "crash", "detail": f"{type(e).__name__}: {str(e)[:300]}", "trace": [], "url": page.url,
                  "markdown": "", "items": [], "tb": traceback.format_exc()[-1500:]}
    if getattr(page, "_ca_dialog", None) is not None:  # a dialog left open pauses the page: the check could not read it
        try:
            await asyncio.wait_for(page._ca_dialog.dismiss(), 5)
        except Exception:
            pass
        page._ca_dialog = None
    try:
        record = await record_of(base, task["id"]) if task["kind"] == "local" else None
        passed, why = await task["check"](Ctx(result, page, record, base))
    except Exception as e:
        passed, why = False, f"check raised {type(e).__name__}: {str(e)[:200]}"
    usage = result.get("usage") or {}
    out.update({
        "passed": bool(passed), "why": why, "status": result.get("status"), "detail": str(result.get("detail") or ""),
        "final_url": result.get("url"), "actions": result.get("actions", len(result.get("trace") or [])),
        "decisions": result.get("decisions"), "usd": usage.get("estimated_usd"),
        "seconds": round(time.perf_counter() - started, 1), "stale": len(result.get("stale") or []),
        "trace": brief_trace(result.get("trace") or []), "answer": (result.get("markdown") or "")[:600],
        **({"tb": result["tb"]} if "tb" in result else {}),
    })
    try:
        await asyncio.wait_for(page.close(), 10)
    except Exception:
        pass
    return out


async def worker(queue, results, base, n):
    profile = Path(tempfile.mkdtemp(prefix=f"jet-bench-{n}-"))
    session = None
    try:
        session = await Session.launch(profile=profile, headless=True, humanize=True)
        while True:
            try:
                task, attempt = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                r = await one(session, task, base, attempt)
            except Exception:  # the tab never opened: start a new browser for this worker and try once more
                try:
                    await asyncio.wait_for(session.close(), 15)
                except Exception:
                    pass
                session = None
                try:
                    session = await Session.launch(profile=profile, headless=True, humanize=True)
                    r = await one(session, task, base, attempt)
                except Exception as e:  # twice: this worker stops here
                    r = {"id": task["id"], "attempt": attempt, "tags": task.get("tags", []), "goal": task["goal"],
                         "passed": False, "why": f"no tab: {type(e).__name__}", "status": "crash",
                         "detail": str(e)[:300], "actions": 0, "seconds": 0, "usd": None, "trace": []}
                    results.append(r)
                    print(f"[{len(results):>3}] FAIL {r['id']:<42} crash      browser gone, worker {n} stops",
                          flush=True)
                    return
            results.append(r)
            save(results)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{len(results):>3}] {mark} {r['id']:<42} {r['status']:<10} {r['actions']:>2} acts "
                  f"{r['seconds']:>5}s  {r['why'][:70]}", flush=True)
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


OUT = None


def save(results, summary=None):
    """Written after every run, so a run that is stopped or hangs still leaves what it did."""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary or {"partial": True, "done": len(results)},
                               "results": sorted(results, key=lambda r: (r["id"], r["attempt"]))},
                              indent=1, ensure_ascii=False), encoding="utf-8")


def summarize(results):
    total = len(results)
    passed = sum(r["passed"] for r in results)
    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    false_done = [r["id"] for r in results if r["status"] == "done" and not r["passed"]]
    stuck = [r["id"] for r in results if r["status"] in ("blocked", "budget", "timeout", "needs_input", "crash")]
    usd = sum(r["usd"] or 0 for r in results)
    print(f"\n== {passed}/{total} passed ({100 * passed / max(total, 1):.0f}%)  statuses {by_status}  cost ${usd:.4f}")
    print(f"   done but check failed ({len(false_done)}): {', '.join(false_done)}")
    print(f"   stuck/ended early ({len(stuck)}): {', '.join(stuck)}")
    return {"passed": passed, "total": total, "by_status": by_status, "false_done": false_done, "stuck": stuck,
            "usd": round(usd, 5)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()
    global OUT
    OUT = HERE / "results" / f"{args.label}.json"
    model_key()
    which = "local" if args.local and not args.real else "real" if args.real and not args.local else "all"
    tasks = [t for t in load_tasks(which) if not args.only or any(s in t["id"] for s in args.only)]
    server, base = None, None
    if any(t["kind"] == "local" for t in tasks):
        import server as fixture_server
        server, base = fixture_server.start(0)
    queue = asyncio.Queue()
    for attempt in range(1, args.repeat + 1):
        for t in tasks:
            queue.put_nowait((t, attempt))
    results = []
    print(f"{queue.qsize()} runs, {args.concurrency} browsers", flush=True)
    t0 = time.perf_counter()
    await asyncio.gather(*(worker(queue, results, base, n) for n in range(args.concurrency)))
    summary = summarize(results)
    summary["seconds"] = round(time.perf_counter() - t0)
    save(results, summary)
    print(f"   saved {OUT}")
    if server is not None:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
