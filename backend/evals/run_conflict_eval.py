"""Contradiction-check eval: runs extraction.find_conflicts on labeled cases (no DB writes).

    cd backend && python -m evals.run_conflict_eval [--model qwen2.5:7b-instruct]

Each case: a new item, existing items, and the indices of existing items it should be flagged against.
Items are [type, subject, title, value].
"""
import argparse
import json
import pathlib

from app import extraction
from app.config import settings


def ent(t, i=None):
    kind, subject, title, value = t
    return {"id": i, "type": kind, "subject": subject, "title": title, "current_state": {"value": value}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=settings.EXTRACTION_MODEL)
    args = ap.parse_args()
    settings.EXTRACTION_MODEL = args.model

    cases = json.loads((pathlib.Path(__file__).parent / "conflict_cases.json").read_text())
    tp = fp = fn = 0
    for case in cases:
        existing = [ent(t, i) for i, t in enumerate(case["existing"])]
        got = {e["id"] for e, _ in extraction.find_conflicts(ent(case["new"]), existing)}
        want = set(case["expected"])
        tp, fp, fn = tp + len(got & want), fp + len(got - want), fn + len(want - got)
        print(f"{'✓' if got == want else '✗'} {case['name']}" + ("" if got == want else f"  got {sorted(got)}, want {sorted(want)}"))
    f = lambda a, b: f"{a / b:.2f}" if b else "-"
    print(f"\nmodel={args.model}  cases={len(cases)}  precision={f(tp, tp + fp)}  recall={f(tp, tp + fn)}")


if __name__ == "__main__":
    main()
