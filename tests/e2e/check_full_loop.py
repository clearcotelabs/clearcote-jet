"""Full agent loop on real Clearcote (headed) with a scripted stand-in for the model HTTP calls.

Everything except the two remote models is real: agent.run, choose() request building and answer validation,
observe/act on Clearcote, field_text parsing, DONE freshness check, rank_blocks, markdown. The stand-in answers
in the decision API's response shape, so a wrong request (e.g. a missing target head) fails here the way it would live.
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from clearcote_jet import model
from clearcote_jet.agent import run
from clearcote_jet.browser import Session

HERE = Path(__file__).parent
FIXTURE = (HERE / "fixture.html").resolve().as_uri()
PLAN = [("TYPE_TEXT", "Reader number"), ("SELECT", "Airport kiosk"), ("CLICK", "Send request"), ("DONE", None)]
requests = []


def dist(keys, pick):
    return {k: (1.0 if k == pick else 0.0) for k in keys}


async def fake_post(url, key, body, headers=None):
    requests.append((url, body))
    if url.endswith("/chat/completions"):
        assert "Reader number" in body["messages"][1]["content"]
        return {"choices": [{"message": {"content": '{"text": "A-4417"}'}}]}
    questions = body["questions"]
    if "blocks" in body["state"]:  # rank_blocks: one noul per block
        return {"answers": {q: {"noul": 0.9 if "Requested" in body["state"]["blocks"][int(q[1:])] else 0.1}
                            for q in questions}}
    op, label = PLAN[sum(1 for u, b in requests if "operation" in b.get("questions", {})) - 1]
    criteria = questions["operation"]["criteria"]
    assert op in criteria, f"{op} not offered: {list(criteria)}"
    answers = {"operation": {"choice": op, "confidence": 0.97, "probabilities": dist(criteria, op)}}
    for name, q in questions.items():
        if name == "operation":
            continue
        ids = list(q["criteria"])
        hit = next((i for i, text in q["criteria"].items() if label and label in text), ids[0])
        answers[name] = {"choice": hit, "confidence": 0.9, "probabilities": dist(ids, hit)}
    return {"answers": answers}


async def main():
    model.post_json = fake_post
    import os
    os.environ.setdefault("TYPESAFE_API_KEY", "stand-in")
    os.environ.setdefault("TEXT_MODEL_API_KEY", "stand-in")
    os.environ.setdefault("TEXT_MODEL_BASE_URL", "https://llm.invalid/v1")  # never reached: post_json is the stand-in
    os.environ.setdefault("TEXT_MODEL", "stand-in")
    profile = Path(tempfile.mkdtemp(prefix="ccagent-loop-", dir=HERE))
    session, code = None, 1
    try:
        session = await Session.launch(profile=profile, headless=False, humanize=True, show_cursor=True)
        page = await session.new_tab(FIXTURE)
        steps = []
        result = await run(session, "Request a loan for reader A-4417, collected from the airport kiosk.", page=page,
                           on_step=lambda s: (steps.append(s), print(f"  step {s['step']}: {s['kind']} "
                                                                     f"{s['action']!r} text={s['text']!r}")))
        print("  status:", result["status"], "| actions:", result["actions"], "| decisions:", result["decisions"],
              "| text calls:", result["text_calls"], "| ms:", result["elapsed_ms"], "| stale:", result["stale"])
        print("  markdown:", result["markdown"][:200].replace("\n", " / "))
        rec = await page.evaluate("window.rec")
        ok = (result["status"] == "done" and [s["kind"] for s in steps] == ["fill", "select", "click"]
              and "Requested: A-4417 / airport" in result["markdown"] and result["text_calls"] == 1
              and all(e["trusted"] for e in rec["events"]) and rec["qsa"] == 0 and not rec["cacheSeen"])
        print("  page-side: events trusted:", all(e["trusted"] for e in rec["events"]),
              "| page saw agent reads:", rec["qsa"], "| element cache seen:", rec["cacheSeen"])
        code = 0 if ok else 1
        await asyncio.sleep(1.5)
    finally:
        if session:
            await session.close()
        for _ in range(20):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            await asyncio.sleep(0.5)
        print("  profile removed:", not profile.exists())
    print("RESULT:", "PASS" if code == 0 else "FAIL")
    return code


sys.exit(asyncio.run(main()))
