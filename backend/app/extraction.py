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
            "title": {"type": "string"},
            "value": {"type": "string"},
            # kind after title/value: the model names the item before classifying it
            "kind": {"type": "string", "enum": ["decision", "task", "constraint"]},
            "subject": {"type": "string"},
            "reason": {"type": "string"},
            "stated_by": {"type": "string", "enum": ["user", "assistant"]},
            "user_quote": {"type": "string"},
        },
        "required": ["title", "value", "kind", "subject", "reason", "stated_by", "user_quote"],
    }}},
    "required": ["items"],
}

PROMPT = """You extract project knowledge from a conversation between a USER and an AI ASSISTANT about a software project.

Return only items that are firm and worth remembering. Kinds:
- decision: a committed choice about the project (technology, design, approach, process, where something is hosted). value = the choice itself, e.g. "PostgreSQL". "let's put X on Y" / "we'll use Y" is a decision, not a task.
- constraint: a requirement or limitation the project must respect, e.g. "must run fully offline", "hosting under $20/month". value = the constraint.
- task: concrete work someone committed to doing or says needs doing ("I'll do X", "I need to do X"), e.g. "write migration for users table". value = what needs to be done. A deadline ("before launch") does not make it a constraint.

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
    return _chat(PROMPT, f"Existing subjects:\n{known_txt}\n\nConversation:\n\n{render(messages)}", SCHEMA)["items"]


def _chat(system: str, user: str, schema: dict) -> dict:
    r = httpx.post(
        f"{settings.OLLAMA_URL}/api/chat",
        json={"model": settings.EXTRACTION_MODEL, "stream": False, "format": schema,
              "options": {"temperature": 0},
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=300,
    )
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


CONFLICT_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "contradicts": {"type": "boolean"}},
    "required": ["reason", "contradicts"],
}

CONFLICT_PROMPT = """You check a software project's recorded decisions and constraints for contradictions.

You get two items, A and B. Give a one-sentence reason, then contradicts = true if A and B cannot both hold
(doing one breaks or rules out the other), else false.

Contradictions (true):
- constraint "Windows support required" vs decision "use a macOS-only UI toolkit".
- constraint "no paid services" vs decision "use Datadog for monitoring".
- decision "store files on S3" vs constraint "all data stays on-premises".

Not contradictions (false):
- different topics that can both hold, e.g. "use MySQL" vs "deploy on Heroku".
- related but compatible, e.g. "use Django" vs "use Postgres".

Judge the actual meaning: a choice that costs money, sends data elsewhere, adds a service, or uses a different
language or license can break a constraint even if the words differ. Take what an item says about itself at
face value: if it says "locally", "self-hosted" or "offline", it does not use the network or a cloud service.
If your reason contradicts what one of the items states, answer false."""


def _describe(e: dict) -> str:
    return f"{e['type']} ({e['subject']}): {e['title']}. {e['current_state'].get('value') or ''}".strip()


def find_conflicts(new: dict, existing: list[dict]) -> list[tuple[dict, str]]:
    """Existing entities (other subjects) that logically contradict `new`, with a reason each.
    One call per pair: batching several items into one call made a 7B model mix them up."""
    res = []
    for e in existing:
        out = _chat(CONFLICT_PROMPT, f"A: {_describe(new)}\nB: {_describe(e)}", CONFLICT_SCHEMA)
        if out.get("contradicts") is True:
            res.append((e, out.get("reason", "")))
    return res


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_is_verbatim(quote: str, messages: list[dict]) -> bool:
    q = _norm(quote or "")
    return bool(q) and any(q in _norm(m["content"]) for m in messages if m["role"] == "user")


def to_events(items: list[dict], messages: list[dict]) -> list[tuple[str, dict]]:
    """Apply the verbatim-quote guard and map items to entity event types. One item per (kind, subject):
    a later item on the same subject would supersede the earlier one, e.g. "Drop OpenAI, we'll use Ollama"
    extracted as both "use Ollama" and "drop OpenAI" left "drop OpenAI" as the decision. The first wins."""
    out, seen = [], set()
    for it in items:
        if not it.get("title", "").strip():
            continue
        key = (it["kind"], (it.get("subject") or it["title"]).strip().lower())
        if key in seen:
            continue
        seen.add(key)
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
