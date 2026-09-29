"""Record live runs as a captioned demo: docs/demo.mp4 and docs/demo.gif.

    python examples/record_demo.py              # record the tasks, then build the video
    python examples/record_demo.py --build-only # rebuild the video from runs/demo/ (e.g. after restyling)

Each task runs in a visible Clearcote window with the cursor drawn while Playwright records the page. Every action is
timestamped, and ffmpeg (must be on PATH) burns in the goal, a caption per step (what was done, the model's
probability, how long the decision took) and a closing card with the run's tokens and cost. Raw recordings and
results go to runs/demo/.
"""

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from clearcote_jet.agent import run
from clearcote_jet.browser import Session
from clearcote_jet.cli import load_env_file

REPO = Path(__file__).resolve().parent.parent
WORK = REPO / "runs" / "demo"
SIZE = (1280, 800)
BAR = 64  # caption bar height, top and bottom
FONT = "C\\:/Windows/Fonts/segoeui.ttf"  # ffmpeg filter syntax; change on macOS/Linux
BOLD = "C\\:/Windows/Fonts/segoeuib.ttf"
TASKS = [
    ("bank-holiday", "https://www.gov.uk/",
     "Find the date of the next bank holiday in England and Wales."),
    ("docs-search", "https://docs.python.org/3/",
     "Search the Python documentation for asyncio.gather and open its entry."),
    ("docs-version", "https://docs.python.org/3/library/asyncio-task.html",
     "Switch this documentation page to Python version 3.12."),
]
VERBS = {"click": "click", "fill": "type into", "select": "choose", "scroll": "scroll", "wait": "wait"}


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def caption(step):
    label = step["action"] if len(step["action"]) <= 58 else step["action"][:57] + "…"
    what = f"{VERBS.get(step['kind'], step['kind'])} “{label}”"
    if step["text"]:
        what += f"  →  “{step['text']}”"
    return f"{step['step']}.  {what}      p = {step['probability']:.2f}   ·   decided in {step['decide_ms']} ms"


async def record_task(session, url, goal, previous=None):
    """One task in its own tab; returns the result, the tab and the timeline (seconds into the tab's video).

    The previous task's tab is closed only once this one is open: a window left without tabs refuses new ones.
    """
    t0 = time.perf_counter()  # the page (and its recording) starts inside new_tab
    page = await session.new_tab(url)
    if previous is not None:
        await previous.close()  # finishes that tab's video
    acts = []
    real_act = session.act

    async def timed_act(pg, state, action, text=None):
        start = time.perf_counter() - t0
        await real_act(pg, state, action, text)
        acts.append(start)  # only successful actions, in trace order

    session.act = timed_act
    await asyncio.sleep(1.0)  # let the first frame settle before the agent looks
    run_start = time.perf_counter() - t0
    try:
        result = await run(session, goal, page=page)
    finally:
        session.act = real_act
    run_end = time.perf_counter() - t0
    await asyncio.sleep(3.0)  # hold on the result
    return result, page, {"run_start": run_start, "run_end": run_end, "acts": acts, "end": time.perf_counter() - t0}


def clip_filter(i, goal, result, timeline, texts):
    """Filter chain for one clip: trim to the run, add the bars, the goal, step captions and the result card."""
    start = max(0.0, timeline["run_start"] - 0.4)
    end = timeline["end"]
    w, h = SIZE
    u = result["usage"]
    closing = (f"{result['status']}  ·  {plural(result['actions'], 'action')}  ·  "
               f"{plural(result['decisions'], 'decision')}  ·  "
               f"{result['elapsed_ms'] / 1000:.1f} s  ·  {u['input_tokens']:,} input tokens ≈ ${u['estimated_usd']:.4f}")
    texts[f"g{i}.txt"] = "Goal:  " + goal
    texts[f"r{i}.txt"] = closing
    parts = [f"[{i}:v]trim=start={start:.2f}:end={end:.2f},setpts=PTS-STARTPTS,scale={w}:{h},"
             f"pad={w}:{h + 2 * BAR}:0:{BAR}:color=0x0f1115",
             f"drawtext=fontfile='{BOLD}':textfile=g{i}.txt:expansion=none:fontcolor=white:fontsize=24:x=28:y=20"]
    steps = result["trace"]
    for n, (step, at) in enumerate(zip(steps, timeline["acts"])):
        name = f"s{i}_{n}.txt"
        texts[name] = caption(step)
        a = max(0.0, at - start - 0.35)
        b = (timeline["acts"][n + 1] - start - 0.35) if n + 1 < len(timeline["acts"]) else timeline["run_end"] - start
        parts.append(f"drawtext=fontfile='{FONT}':textfile={name}:expansion=none:fontcolor=white:fontsize=22:"
                     f"x=28:y={h + BAR + 18}:enable='between(t,{a:.2f},{b:.2f})'")
    r = timeline["run_end"] - start
    parts.append(f"drawtext=fontfile='{BOLD}':textfile=r{i}.txt:expansion=none:fontcolor=0x7ee2b8:fontsize=22:"
                 f"x=28:y={h + BAR + 18}:enable='gte(t,{r:.2f})'")
    return ",".join(parts) + f"[c{i}]"


def build(recordings):
    texts, chains = {}, []
    for i, (goal, result, _, timeline) in enumerate(recordings):
        chains.append(clip_filter(i, goal, result, timeline, texts))
    for name, text in texts.items():
        (WORK / name).write_text(text, encoding="utf-8")
    concat = "".join(f"[c{i}]" for i in range(len(recordings))) + f"concat=n={len(recordings)}:v=1:a=0[out]"
    (WORK / "graph.txt").write_text(";\n".join(chains + [concat]), encoding="utf-8")
    inputs = sum((["-i", str(v)] for _, _, v, _ in recordings), [])
    docs = REPO / "docs"
    docs.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-/filter_complex", "graph.txt", "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart",
                    str(docs / "demo.mp4")], cwd=WORK, check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(docs / "demo.mp4"), "-vf",
                    "fps=10,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];"
                    "[b][p]paletteuse=dither=bayer:bayer_scale=4", str(docs / "demo.gif")], check=True)
    return docs / "demo.mp4", docs / "demo.gif"


def saved_recordings():
    out = []
    for name, _, _ in TASKS:
        r = json.loads((WORK / f"{name}.json").read_text(encoding="utf-8"))
        out.append((r["goal"], r, Path(r["video"]), r["timeline"]))
    return out


async def main():
    load_env_file(REPO / ".env")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not on PATH")
    if "--build-only" in sys.argv:
        for path in build(saved_recordings()):
            print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")
        return
    WORK.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="clearcote-jet-"))
    session, recordings = None, []
    try:
        session = await Session.launch(profile=profile, headless=False, show_cursor=True,
                                       viewport={"width": SIZE[0], "height": SIZE[1]},
                                       record_video_dir=str(WORK / "raw"),
                                       record_video_size={"width": SIZE[0], "height": SIZE[1]})
        page = None
        for name, url, goal in TASKS:
            result, page, timeline = await record_task(session, url, goal, previous=page)
            video = Path(await page.video.path())  # known now, complete once the tab or the browser closes
            (WORK / f"{name}.json").write_text(json.dumps({"goal": goal, "start_url": url, "timeline": timeline,
                                                          "video": str(video), **result},
                                                         indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"{name}: {result['status']}, {result['actions']} actions, {result['decisions']} decisions, "
                  f"{result['usage']['input_tokens']:,} input tokens ≈ ${result['usage']['estimated_usd']:.4f}",
                  flush=True)
            recordings.append((goal, result, video, timeline))
    finally:
        if session:
            with contextlib.suppress(Exception):
                await session.close()
        shutil.rmtree(profile, ignore_errors=True)
    for path in build(recordings):
        print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    asyncio.run(main())
