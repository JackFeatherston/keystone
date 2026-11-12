"""Extraction eval: runs the real extractor + verbatim-quote guard on labeled excerpts (no DB writes).

    cd backend && python -m evals.run_eval [--model qwen2.5:7b-instruct] [-v]

An expected item matches a predicted one of the same kind whose title/value/subject contains all its
`match` keywords (case-insensitive; "a|b" = either). "found" = matched; "correct" = matched with the right status
(approved / proposed / open), i.e. the guard and stated_by attribution also got it right.
"""
import argparse
import json
import pathlib
import time
from collections import Counter

from app import extraction
from app.config import settings

STATUS = {"decision_confirmed": "approved", "constraint_confirmed": "approved", "task_created": "open",
          "decision_proposed": "proposed", "constraint_proposed": "proposed", "task_proposed": "proposed"}


def predict(case):
    items = extraction.extract(case["messages"], case.get("known", []))
    return [{"kind": e.split("_")[0], "status": STATUS[e], **p} for e, p in extraction.to_events(items, case["messages"])]


def match(expected, predicted):
    """Greedy one-to-one matching. Returns list of (expected, predicted-or-None) and unmatched predictions."""
    left, pairs = list(predicted), []
    for exp in expected:
        hit = next((p for p in left if p["kind"] == exp["kind"] and all(
            any(alt.lower() in f"{p['title']} {p['value']} {p['subject']}".lower() for alt in k.split("|"))
            for k in exp["match"])), None)
        if hit:
            left.remove(hit)
        pairs.append((exp, hit))
    return pairs, left


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=settings.EXTRACTION_MODEL)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    settings.EXTRACTION_MODEL = args.model

    cases = json.loads((pathlib.Path(__file__).parent / "cases.json").read_text())
    c, t0 = Counter(), time.time()
    for case in cases:
        pred = predict(case)
        pairs, extra = match(case["expected"], pred)
        for exp, hit in pairs:
            k = exp["kind"]
            c[f"exp_{k}"] += 1
            if hit:
                c[f"found_{k}"] += 1
                ok = hit["status"] == exp["status"] and hit["subject"] == exp.get("subject", hit["subject"])
                c[f"correct_{k}"] += ok
        for p in pred:
            c[f"pred_{p['kind']}"] += 1
        bad = [(e, h) for e, h in pairs if not h or h["status"] != e["status"]
               or h["subject"] != e.get("subject", h["subject"])]
        if bad or extra:
            print(f"✗ {case['name']}")
            for e, h in bad:
                print(f"    missed {e['kind']} {e['match']} ({e['status']})" if not h else
                      f"    wrong  {e['kind']} {e['match']}: got {h['status']}/{h['subject']!r}, want {e['status']}/{e.get('subject', h['subject'])!r}")
            for p in extra:
                print(f"    extra  {p['kind']} ({p['status']}) {p['title']!r}")
            if args.verbose:
                print("    predicted:", json.dumps(pred, indent=2).replace("\n", "\n    "))
        else:
            print(f"✓ {case['name']}")

    print(f"\nmodel={args.model}  cases={len(cases)}  {time.time() - t0:.0f}s")
    print(f"{'kind':<11}{'precision':>10}{'recall':>8}{'correct':>9}")
    for k in ["decision", "constraint", "task", "all"]:
        get = (lambda n: sum(c[f"{n}_{x}"] for x in ["decision", "constraint", "task"])) if k == "all" else (lambda n: c[f"{n}_{k}"])
        exp, pred, found, correct = get("exp"), get("pred"), get("found"), get("correct")
        f = lambda a, b: f"{a / b:.2f}" if b else "  - "
        print(f"{k:<11}{f(found, pred):>10}{f(found, exp):>8}{f(correct, exp):>9}")
    print("precision = found/predicted, recall = found/expected, correct = found with right status/subject over expected")


if __name__ == "__main__":
    main()
