"""LLM extraction: conversation excerpt -> decisions / tasks / constraints, via a local Ollama model.

The model only *suggests* who stated what. The verbatim-quote guard (to_events) decides what becomes
approved: an item is user-stated only if its user_quote appears word for word in a user message.
"""
import json
import re

import httpx

from .config import settings

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["decision", "task", "constraint"]},
            "subject": {"type": "string"},
            "title": {"type": "string"},
            "value": {"type": "string"},
            "reason": {"type": "string"},
            "stated_by": {"type": "string", "enum": ["user", "assistant"]},
            "user_quote": {"type": "string"},
        },
        "required": ["kind", "subject", "title", "value", "reason", "stated_by", "user_quote"],
    }}},
    "required": ["items"],
}

PROMPT = """You extract project knowledge from a conversation between a USER and an AI ASSISTANT about a software project.

Return only items that are firm and worth remembering. Kinds:
- decision: a committed choice about the project (technology, design, approach, process). value = the choice itself, e.g. "PostgreSQL".
- constraint: a requirement or limitation the project must respect, e.g. "must run fully offline", "hosting under $20/month". value = the constraint.
- task: concrete work someone committed to doing, e.g. "write migration for users table". value = what needs to be done.

stated_by:
- "user": the USER stated it, or explicitly agreed to it ("yes, let's do that", "go with option 2"). Then user_quote MUST be copied character for character from one USER message: the shortest span that shows the commitment. Never paraphrase it, never quote the ASSISTANT.
- "assistant": the ASSISTANT recommended something concrete and the USER has not agreed to it (yet). Extract it even if the USER ignored it or asked about something else. Only the single main recommendation, as a decision or task; user_quote = "".

Rules:
- One USER message can contain several decisions, constraints and tasks: extract each separately.
- If the USER rejects a suggestion and picks an alternative ("no, we'll use X instead"), extract the alternative as the USER's decision.
- Constraints come only from the USER. Never turn the ASSISTANT's reasons or feature lists into constraints.

Do NOT extract:
- hedges, questions, or options still being weighed ("maybe postgres?", "should we use redis?", "I'm leaning towards X").
- conditionals and possibilities ("if X is slow we might switch to Y", "we could later..."): they are not commitments.
- things the user rejected, or general chit-chat, explanations and facts that are not project choices.
- the same item twice.

subject is the short topic the item is about, lowercase, e.g. "database", "auth provider", "hosting". If an existing subject below covers the same topic, reuse it exactly, so the new item replaces the old one.
title is a short summary, e.g. "Use PostgreSQL for the database". reason is why, if given, else "".

If there is nothing to extract, return {"items": []}."""


def render(messages: list[dict]) -> str:
    return "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)


def extract(messages: list[dict], known: list[dict]) -> list[dict]:
    """Candidate items (unguarded). `known` = active entities, so the model can reuse subjects."""
    known_txt = "\n".join(f"- {e['type']} / {e['subject']}: {e['current_state'].get('value')} ({e['status']})"
                          for e in known) or "(none)"
    r = httpx.post(
        f"{settings.OLLAMA_URL}/api/chat",
        json={"model": settings.EXTRACTION_MODEL, "stream": False, "format": SCHEMA,
              "options": {"temperature": 0},
              "messages": [{"role": "system", "content": PROMPT},
                           {"role": "user", "content": f"Existing subjects:\n{known_txt}\n\nConversation:\n\n{render(messages)}"}]},
        timeout=300,
    )
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])["items"]


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_is_verbatim(quote: str, messages: list[dict]) -> bool:
    q = _norm(quote or "")
    return bool(q) and any(q in _norm(m["content"]) for m in messages if m["role"] == "user")


def to_events(items: list[dict], messages: list[dict]) -> list[tuple[str, dict]]:
    """Apply the verbatim-quote guard and map items to entity event types."""
    out = []
    for it in items:
        if not it.get("title", "").strip():
            continue
        by_user = it.get("stated_by") == "user" and quote_is_verbatim(it.get("user_quote", ""), messages)
        payload = {k: it.get(k, "") for k in ("title", "subject", "value", "reason")}
        if by_user:
            payload["user_quote"] = it["user_quote"]
        kind = it["kind"]
        if kind == "task":
            etype = "task_created" if by_user else "task_proposed"
        else:
            etype = f"{kind}_confirmed" if by_user else f"{kind}_proposed"
        out.append((etype, payload))
    return out
