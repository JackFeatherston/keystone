"""Thesis A/B: does an agent answer project questions better with Keystone's context?

    cd backend && python -m evals.retrieval.run_ab_eval [--model qwen2.5:7b-instruct] [--judge <model>] [-v]

Seeds the fixture project, then a local model answers each question in ab_cases.json under three conditions:
- none:      only the project name
- raw:       top 10 raw vector-search chunks (what search_context returned before Milestone 3)
- context:   the get_context package
An LLM judge compares each answer with the reference and the known-outdated answer:
correct = gives the reference answer; outdated = presents the outdated answer as current.
"""
import argparse
import json
import pathlib

import httpx

from app import service
from app.config import settings
from app.db import supabase as sb
from app.embeddings import embed_query

from . import seed

CASES = json.loads((pathlib.Path(__file__).parent / "ab_cases.json").read_text())
AGENT = ("You are a coding assistant on the project '{project}'. Answer the developer's question in one or two "
         "sentences, specific to this project. If the project context doesn't tell you, say you don't know "
         "rather than guessing.")
JUDGE = ("You grade an assistant's answer to a question about a software project. Compare it with the reference "
         "answer. correct: the answer gives the reference answer's substance (wording can differ; extra detail is "
         "fine). outdated: the answer presents the OUTDATED answer as the current choice (mentioning it as history "
         "or as rejected is not outdated). Saying something was only suggested, not decided, is not outdated. "
         "First write a one-sentence reason, then the verdicts. Reply as JSON.")
JUDGE_SCHEMA = {"type": "object", "properties": {"reason": {"type": "string"}, "correct": {"type": "boolean"},
                                                 "outdated": {"type": "boolean"}},
                "required": ["reason", "correct", "outdated"]}


def chat(model, system, user, schema=None) -> str:
    body = {"model": model, "stream": False, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if schema:
        body["format"] = schema
    r = httpx.post(f"{settings.OLLAMA_URL}/api/chat", json=body, timeout=300)
    r.raise_for_status()
    return r.json()["message"]["content"]


def context_for(condition, pid, question) -> str:
    if condition == "none":
        return ""
    if condition == "raw":
        rows = sb.rpc("match_embeddings", {"query_embedding": embed_query(question), "target_project_id": pid,
                                           "match_count": 10}).execute().data
        return "Project context (search results):\n" + "\n".join(f"- {r['content']}" for r in rows)
    return "Project context (Keystone get_context):\n" + json.dumps(service.get_context(pid, question), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=settings.EXTRACTION_MODEL)
    ap.add_argument("--judge", default=None, help="judge model (default: same as --model)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    judge = a.judge or a.model

    pid, _ = seed.seed()
    try:
        totals = {c: {"correct": 0, "outdated": 0} for c in ("none", "raw", "context")}
        for case in CASES:
            for cond in totals:
                ctx = context_for(cond, pid, case["question"])
                answer = chat(a.model, AGENT.format(project=seed.FIXTURE["project"]),
                              f"{ctx}\n\nQuestion: {case['question']}".strip())
                grade = json.loads(chat(judge, JUDGE, f"Question: {case['question']}\nReference answer: {case['answer']}\n"
                                        f"OUTDATED answer: {case['outdated'] or '(none)'}\n\nAssistant's answer: {answer}",
                                        JUDGE_SCHEMA))
                grade["outdated"] = bool(case["outdated"]) and grade["outdated"]
                for k in ("correct", "outdated"):
                    totals[cond][k] += grade[k]
                if a.v:
                    print(f"[{cond}] {case['question']}\n    {answer.strip()[:200]}\n    -> {grade}")
        n = len(CASES)
        print(f"{n} questions, agent {a.model}, judge {judge}")
        print("| condition | correct | presents outdated answer |\n|---|---|---|")
        for cond, t in totals.items():
            print(f"| {cond} | {t['correct']}/{n} | {t['outdated']}/{n} |")
    finally:
        seed.drop(pid)


if __name__ == "__main__":
    main()
