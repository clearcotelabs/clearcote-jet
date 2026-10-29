"""`clearcote-jet worker` with a stand-in for the decision model, for check_worker.py: the real worker, run as its own
process, with only the model's HTTP client swapped (so every request still goes through model.post_json, secrets
swept out). Every request body is appended to JET_E2E_LOG, one JSON line each. JET_E2E_PLAN is the list of
[operation, label] the stand-in answers in turn; with JET_E2E_MODEL_OFF=1 any request fails the task.

A field's value is chosen among the goal's own words: an email address for Email, the {{password}} token for Password.
"""

import json
import os
import sys
from pathlib import Path

from clearcote_jet import model, worker

PLAN = json.loads(os.environ.get("JET_E2E_PLAN") or "[]")
LOG = Path(os.environ["JET_E2E_LOG"])


def dist(keys, pick):
    return {k: float(k == pick) for k in keys}


def answer(body):
    if os.environ.get("JET_E2E_MODEL_OFF") == "1":
        raise AssertionError("a model was called during a replay")
    questions = body["questions"]
    if "blocks" in body["state"]:
        return {"answers": {q: {"noul": 0.9} for q in questions}}
    if "value" in questions:
        crit, field = questions["value"]["criteria"], body["state"]["field"]["label"]
        want = (lambda v: v.startswith("{{")) if field == "Password" else (lambda v: "@" in v and " " not in v)
        pick = next((k for k, v in crit.items() if want(v)), "NONE")
        return {"answers": {"value": {"choice": pick, "confidence": 1.0, "probabilities": dist(crit, pick)}}}
    op, label = PLAN.pop(0)
    crit = questions["operation"]["criteria"]
    answers = {"operation": {"choice": op, "confidence": 1.0, "probabilities": dist(crit, op)}}
    for name, q in questions.items():
        if name != "operation":
            ids = list(q["criteria"])
            line = lambda i: next((e for e in body["state"]["elements"] if e.startswith(f"[{i}] ")), "")  # noqa: E731
            hit = next((i for i in ids if label and f'"{label}"' in line(i)), ids[0])
            answers[name] = {"choice": hit, "confidence": 1.0, "probabilities": dist(ids, hit)}
    return {"answers": answers}


class Response:
    status_code, is_error, text = 200, False, ""

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Client:
    async def post(self, url, json=None, headers=None):
        with LOG.open("a", encoding="utf-8") as f:
            f.write(_dumps(json) + "\n")
        return Response(answer(json))


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    model.CLIENT = Client()
    os.environ.setdefault("TYPESAFE_API_KEY", "stand-in")
    os.environ.pop("TEXT_MODEL_API_KEY", None)
    sys.argv = ["clearcote-jet", "worker"]
    worker.main()
