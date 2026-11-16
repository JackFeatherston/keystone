"""Retrieval eval: seeds the fixture project, runs every query through service.search_context, scores, cleans up.

    cd backend && python -m evals.retrieval.run_retrieval_eval [-k 5] [-v] [--keep]

Each result maps to the fixture items it represents: an entity -> its subject:value key; a source chunk -> the
items extracted from that excerpt (or the note's key). Metrics over the top k:
- recall@k: expected items found (not labeled as history), averaged over cases with expectations
- MRR: 1 / rank of the first expected item
- stale rate: cases where the top k contains a superseded/rejected item (or a stale note) that is NOT labeled
  as history, i.e. an agent would read it as current
"""
import argparse
import json
import pathlib

from app import service

from . import seed

CASES = json.loads((pathlib.Path(__file__).parent / "cases.json").read_text())
HISTORY = {"superseded", "rejected", "history"}


def represents(r, ents, truth):
    if r.get("entity_id") in ents:
        return {ents[r["entity_id"]][0]}
    return truth["event_keys"].get(r.get("event_id"), set())


def score(results, case, ents, truth, k):
    stale_keys = set(case["stale"])
    found, first, stale = set(), None, []
    for rank, r in enumerate(results[:k], 1):
        keys = represents(r, ents, truth)
        if r.get("status") in HISTORY:
            continue  # labeled: the agent can tell it's not current
        if keys & stale_keys:
            stale.append(sorted(keys & stale_keys))
        hits = keys & set(case["expect"])
        found |= hits
        if hits and first is None:
            first = rank
    return found, first, stale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--keep", action="store_true", help="don't delete the seeded project")
    a = ap.parse_args()

    pid, truth = seed.seed()
    try:
        ents = seed.entity_keys(pid)
        recalls, rrs, stale_cases = [], [], 0
        for case in CASES:
            results = service.search_context(pid, case["query"], 10)
            found, first, stale = score(results, case, ents, truth, a.k)
            if case["expect"]:
                recalls.append(len(found) / len(case["expect"]))
                rrs.append(1 / first if first else 0)
            stale_cases += bool(stale)
            if a.v and (stale or (case["expect"] and len(found) < len(case["expect"]))):
                print(f"- {case['query']!r}: missing {sorted(set(case['expect']) - found)}, stale {stale}")
                for r in results[:a.k]:
                    print(f"    {r.get('status', '?'):11} {sorted(represents(r, ents, truth))}  {(r.get('content') or r.get('title') or '')[:70]!r}")
        print(f"{len(CASES)} cases, k={a.k}: recall@{a.k} {sum(recalls) / len(recalls):.2f}  "
              f"MRR {sum(rrs) / len(rrs):.2f}  stale rate {stale_cases / len(CASES):.2f}")
    finally:
        if a.keep:
            print(f"kept project {pid}")
        else:
            seed.drop(pid)


if __name__ == "__main__":
    main()
