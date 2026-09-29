"""What your runs cost: tokens and dollars per saved run, plus a projection per 1,000 tasks.

    python examples/estimate_cost.py                 # every result under runs/
    python examples/estimate_cost.py --rate 0.042    # USD per million input tokens (default: the published rate)

Reads the JSON results that run_task.py, gallery.py and record_demo.py save. Only the decision model is priced;
a text model, if you configured one, bills separately and its tokens are listed on their own.
"""

import argparse
import json
from pathlib import Path

from clearcote_jet.model import USD_PER_MILLION_INPUT_TOKENS, estimate_usd

REPO = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=USD_PER_MILLION_INPUT_TOKENS,
                        help="USD per million input tokens for the decision model")
    parser.add_argument("--runs", default=str(REPO / "runs"))
    args = parser.parse_args()

    rows = []
    for path in sorted(Path(args.runs).rglob("*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        usage = result.get("usage")
        if not usage:
            continue  # saved before token accounting existed
        rows.append((path.stem, result["status"], result["decisions"], usage["input_tokens"],
                     usage.get("text_model", {}).get("input_tokens", 0)))
    if not rows:
        raise SystemExit(f"no results with token usage under {args.runs}")

    print(f"{'run':34} {'status':12} {'decisions':>9} {'input tokens':>13} {'cost':>9}")
    for name, status, decisions, tokens, _ in rows:
        print(f"{name[:34]:34} {status:12} {decisions:>9} {tokens:>13,} {'$%.4f' % estimate_usd(tokens, args.rate):>9}")
    done = [r for r in rows if r[1] == "done"]
    total = sum(r[3] for r in rows)
    print(f"\n{len(rows)} runs, {total:,} input tokens, ${estimate_usd(total, args.rate):.4f} at ${args.rate}/M tokens")
    if done:
        mean = sum(r[3] for r in done) / len(done)
        print(f"a finished task averages {mean:,.0f} tokens: ${estimate_usd(mean, args.rate):.4f} each, "
              f"${estimate_usd(mean, args.rate) * 1000:.2f} per 1,000 tasks")
    text = sum(r[4] for r in rows)
    if text:
        print(f"text model (billed by its own provider): {text:,} input tokens")


if __name__ == "__main__":
    main()
