"""Event processing: events (source of truth) -> entities (current state) + embeddings.

- context_logged: a conversation excerpt pushed by an AI client. The LLM extractor turns it into derived
  entity events (source_ref = the excerpt event id, so provenance points back to the excerpt).
- <kind>_proposed / <kind>_confirmed / decision_approved / task_created (kind: decision, constraint, task):
  - *_approved -> 'approved', honoured for source 'manual' only
  - *_confirmed -> 'approved' if it carries the user's verbatim words ('user_quote')
  - task_created -> 'open'
  - anything else -> 'proposed': AI-inferred claims are not verified state
- Same subject + same value as an active entity: new version of that entity, but only if the status
  goes up (an AI restating an approved decision never downgrades it).
- A non-proposed decision/constraint supersedes the other active ones on its subject (kept as history,
  linked with 'supersedes'). A proposal only supersedes other proposals: it never displaces approved state.
- proposal_confirmed / proposal_rejected / entity_implemented: review actions on an existing entity.
"""
from . import extraction
from .db import supabase as sb
from .embeddings import embed

ACTIVE = ["proposed", "approved", "implemented", "open"]
RANK = {"proposed": 0, "approved": 1, "open": 1, "implemented": 2, "done": 2}
KIND_EVENTS = {
    "decision_proposed": "decision", "decision_approved": "decision", "decision_confirmed": "decision",
    "constraint_proposed": "constraint", "constraint_confirmed": "constraint",
    "task_proposed": "task", "task_created": "task",
}


def _embed_row(project_id, content, entity_id=None, event_id=None):
    sb.table("embeddings").insert(
        {"project_id": project_id, "entity_id": entity_id, "event_id": event_id,
         "content": content, "embedding": embed(content)}
    ).execute()


def process_event(event: dict) -> None:
    p, project_id = event["payload"], event["project_id"]
    etype = event["event_type"]

    if etype in ("proposal_confirmed", "proposal_rejected", "entity_implemented"):
        return _review(event)

    # Source-text chunk (retrievable even if extraction finds nothing)
    _embed_row(project_id, p.get("text") or f"{p.get('title', '')} {p.get('reason', '')}".strip(),
               event_id=event["id"])

    if etype == "context_logged":
        return _extract(event)
    if etype not in KIND_EVENTS:
        return

    kind = KIND_EVENTS[etype]
    if etype == "task_created":
        status = "open"
    elif etype == "decision_approved" and event["source"] == "manual":
        status = "approved"
    elif etype.endswith("_confirmed") and (p.get("user_quote") or "").strip():
        status = "approved" if kind != "task" else "open"  # user's own words as evidence
    else:
        status = "proposed"

    subject = (p.get("subject") or p["title"]).strip().lower()
    state = {"value": p.get("value"), "reason": p.get("reason"), "source": event["source"]}
    if p.get("user_quote"):
        state["user_quote"] = p["user_quote"]

    active = (sb.table("entities").select("*").eq("project_id", project_id)
              .eq("type", kind).eq("subject", subject).in_("status", ACTIVE)
              .order("created_at", desc=True).execute().data)
    same = next((e for e in active if kind == "task" or e["current_state"].get("value") == p.get("value")), None)

    if same:
        if RANK[status] <= RANK[same["status"]]:
            return  # restatement: nothing new
        entity_id, version = same["id"], _next_version(same["id"])
        sb.table("entities").update(
            {"status": status, "current_state": state, "updated_at": "now()"}
        ).eq("id", entity_id).execute()
    else:
        ent = sb.table("entities").insert(
            {"project_id": project_id, "type": kind, "subject": subject,
             "title": p["title"], "current_state": state, "status": status}
        ).execute().data[0]
        entity_id, version = ent["id"], 1

    sb.table("entity_versions").insert(
        {"entity_id": entity_id, "version": version, "state": state,
         "status": status, "event_id": event["id"]}
    ).execute()
    if kind != "task":
        _supersede(project_id, entity_id, status, [e for e in active if e["id"] != entity_id], event["id"])

    # One embedding per entity (replace stale one)
    sb.table("embeddings").delete().eq("entity_id", entity_id).execute()
    _embed_row(project_id, f"{p['title']}: {p.get('value') or ''}. {p.get('reason') or ''}",
               entity_id=entity_id)


def _supersede(project_id, entity_id, status, others, event_id):
    for prior in others:
        if status == "proposed" and prior["status"] != "proposed":
            continue  # proposals never displace approved/implemented state
        sb.table("entities").update({"status": "superseded"}).eq("id", prior["id"]).execute()
        sb.table("entity_versions").insert(
            {"entity_id": prior["id"], "version": _next_version(prior["id"]),
             "state": prior["current_state"], "status": "superseded", "event_id": event_id}
        ).execute()
        sb.table("relationships").insert(
            {"project_id": project_id, "source_entity_id": entity_id,
             "target_entity_id": prior["id"], "relationship_type": "supersedes"}
        ).execute()


def _extract(event: dict) -> None:
    # Idempotent on retry: derived events already written for this excerpt -> done.
    if sb.table("events").select("id").eq("source_ref", event["id"]).limit(1).execute().data:
        return
    known = (sb.table("entities").select("type,subject,current_state,status")
             .eq("project_id", event["project_id"]).in_("status", ACTIVE).execute().data)
    messages = event["payload"]["messages"]
    events = extraction.to_events(extraction.extract(messages, known), messages)
    if events:
        # Bulk insert: all derived events or none, so a retry can't half-duplicate.
        rows = sb.table("events").insert(
            [{"project_id": event["project_id"], "source": event["source"], "event_type": etype,
              "payload": payload, "source_ref": event["id"]} for etype, payload in events]
        ).execute().data
        sb.table("jobs").insert([{"event_id": r["id"]} for r in rows]).execute()


def _review(event: dict) -> None:
    p, etype = event["payload"], event["event_type"]
    ent = sb.table("entities").select("*").eq("id", p["entity_id"]).single().execute().data
    state = dict(ent["current_state"])
    if etype == "proposal_confirmed":
        if ent["status"] != "proposed":
            return
        status = "open" if ent["type"] == "task" else "approved"
        state["user_quote"] = p["user_quote"]
    elif etype == "proposal_rejected":
        if ent["status"] != "proposed":
            return
        status = "rejected"
    else:  # entity_implemented
        if ent["status"] not in ("approved", "open"):
            return
        status = "done" if ent["type"] == "task" else "implemented"
        if p.get("evidence"):
            state["evidence"] = p["evidence"]

    sb.table("entities").update(
        {"status": status, "current_state": state, "updated_at": "now()"}
    ).eq("id", ent["id"]).execute()
    sb.table("entity_versions").insert(
        {"entity_id": ent["id"], "version": _next_version(ent["id"]), "state": state,
         "status": status, "event_id": event["id"]}
    ).execute()
    if etype == "proposal_confirmed" and ent["type"] != "task":
        others = (sb.table("entities").select("*").eq("project_id", ent["project_id"])
                  .eq("type", ent["type"]).eq("subject", ent["subject"]).in_("status", ACTIVE)
                  .neq("id", ent["id"]).execute().data)
        _supersede(ent["project_id"], ent["id"], status, others, event["id"])


def _next_version(entity_id: str) -> int:
    rows = (sb.table("entity_versions").select("version").eq("entity_id", entity_id)
            .order("version", desc=True).limit(1).execute().data)
    return (rows[0]["version"] + 1) if rows else 1
