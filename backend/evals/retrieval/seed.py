"""Seed a throwaway project from fixture.json through the real context engine (real embeddings via Ollama).

Extraction is replaced by the fixture's labeled items (still passed through the verbatim-quote guard) and the
contradiction check is disabled, so retrieval scores don't move with extraction prompt/model changes.
Events are inserted without jobs and processed directly, so a running dev server's worker can't race us.
"""
import json
import pathlib
import uuid

from app import context_engine, extraction
from app.config import settings
from app.db import supabase as sb

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixture.json").read_text())


def key(subject: str, value) -> str:
    return f"{subject}:{value or ''}"


def _event(pid, etype, payload, source="claude"):
    return sb.table("events").insert(
        {"project_id": pid, "source": source, "event_type": etype, "payload": payload}).execute().data[0]


def _entity_id(pid, k):
    subject, value = k.split(":", 1)
    rows = sb.table("entities").select("id,current_state").eq("project_id", pid).eq("subject", subject).execute().data
    return next(r["id"] for r in rows if (r["current_state"].get("value") or "") == value)


def seed() -> tuple[str, dict]:
    """Returns (project_id, truth). truth: event_keys {event_id: {keys}}, stale_notes {keys}."""
    pid = sb.table("projects").insert({"owner_id": settings.KEYSTONE_OWNER_ID, "name": f"retrieval-eval-{uuid.uuid4().hex[:6]}",
                                       "description": FIXTURE["project"]}).execute().data[0]["id"]
    event_keys, stale_notes = {}, set()
    orig_extract, orig_conflicts = extraction.extract, extraction.find_conflicts
    extraction.find_conflicts = lambda new, existing: []
    try:
        for step in FIXTURE["steps"]:
            messages = step.get("excerpt") or step["note"]
            items = step.get("items", [])
            extraction.extract = lambda m, known, items=items: items
            ev = _event(pid, "context_logged", {"messages": messages, "text": extraction.render(messages)})
            keys = {key(i["subject"], i["value"]) for i in items} | ({step["key"]} if "key" in step else set())
            event_keys[ev["id"]] = keys
            if step.get("stale"):
                stale_notes.add(step["key"])
            for derived in context_engine.process_event(ev, queue=False) or []:
                event_keys[derived["id"]] = {key(derived["payload"]["subject"], derived["payload"]["value"])}
                context_engine.process_event(derived)
            for field, etype, extra in (("implemented", "entity_implemented", {"evidence": "shipped"}),
                                        ("reject", "proposal_rejected", {"user_quote": ""})):
                if field in step:
                    context_engine.process_event(_event(pid, etype, {"entity_id": _entity_id(pid, step[field]), **extra}))
    finally:
        extraction.extract, extraction.find_conflicts = orig_extract, orig_conflicts
    return pid, {"event_keys": event_keys, "stale_notes": stale_notes}


def entity_keys(pid) -> dict:
    """entity id -> (key, status)"""
    rows = sb.table("entities").select("id,subject,status,current_state").eq("project_id", pid).execute().data
    return {r["id"]: (key(r["subject"], r["current_state"].get("value")), r["status"]) for r in rows}


def drop(pid):
    sb.table("entities").delete().eq("project_id", pid).execute()  # versions' event FK has no cascade
    sb.table("projects").delete().eq("id", pid).execute()
