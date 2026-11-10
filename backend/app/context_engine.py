"""Event processing: events (source of truth) -> entities (current state) + embeddings.

Events carry structured payloads, so no LLM extraction is needed yet. Rules:
- decision_proposed  -> entity status 'proposed'
- decision_approved  -> entity status 'approved'
- A new proposed/approved decision on the same subject supersedes the previous active one
  (kept as history, linked with a 'supersedes' relationship).
- AI-submitted claims never start above 'proposed'; only source 'manual' can approve.
"""
from .db import supabase as sb
from .embeddings import embed

ACTIVE = ["proposed", "approved", "implemented"]


def _embed_row(project_id, content, entity_id=None, event_id=None):
    sb.table("embeddings").insert(
        {"project_id": project_id, "entity_id": entity_id, "event_id": event_id,
         "content": content, "embedding": embed(content)}
    ).execute()


def process_event(event: dict) -> None:
    p, project_id = event["payload"], event["project_id"]
    etype = event["event_type"]

    # Source-text chunk (retrievable even if extraction finds nothing)
    _embed_row(project_id, p.get("text") or f"{p.get('title', '')} {p.get('reason', '')}".strip(),
               event_id=event["id"])

    if etype not in ("decision_proposed", "decision_approved", "task_created"):
        return

    is_task = etype == "task_created"
    status = "open" if is_task else ("approved" if etype == "decision_approved" else "proposed")
    if status == "approved" and event["source"] != "manual":
        status = "proposed"  # AI claims are not verified state

    subject = p.get("subject") or p["title"]
    etype_col = "task" if is_task else "decision"
    state = {"value": p.get("value"), "reason": p.get("reason"), "source": event["source"]}

    prior = (sb.table("entities").select("*").eq("project_id", project_id)
             .eq("type", etype_col).eq("subject", subject).in_("status", ACTIVE + ["open"])
             .order("created_at", desc=True).limit(1).execute().data)
    prior = prior[0] if prior else None

    # Same value again (e.g. approval of a proposal): new version of the same entity
    if prior and not is_task and prior["current_state"].get("value") == p.get("value"):
        entity_id, version = prior["id"], _next_version(prior["id"])
        sb.table("entities").update(
            {"status": status, "current_state": state, "updated_at": "now()"}
        ).eq("id", entity_id).execute()
    else:
        ent = sb.table("entities").insert(
            {"project_id": project_id, "type": etype_col, "subject": subject,
             "title": p["title"], "current_state": state, "status": status}
        ).execute().data[0]
        entity_id, version = ent["id"], 1
        if prior and not is_task:
            sb.table("entities").update({"status": "superseded"}).eq("id", prior["id"]).execute()
            sb.table("entity_versions").insert(
                {"entity_id": prior["id"], "version": _next_version(prior["id"]),
                 "state": prior["current_state"], "status": "superseded", "event_id": event["id"]}
            ).execute()
            sb.table("relationships").insert(
                {"project_id": project_id, "source_entity_id": entity_id,
                 "target_entity_id": prior["id"], "relationship_type": "supersedes"}
            ).execute()

    sb.table("entity_versions").insert(
        {"entity_id": entity_id, "version": version, "state": state,
         "status": status, "event_id": event["id"]}
    ).execute()

    # One embedding per entity (replace stale one)
    sb.table("embeddings").delete().eq("entity_id", entity_id).execute()
    _embed_row(project_id, f"{p['title']}: {p.get('value') or ''}. {p.get('reason') or ''}",
               entity_id=entity_id)


def _next_version(entity_id: str) -> int:
    rows = (sb.table("entity_versions").select("version").eq("entity_id", entity_id)
            .order("version", desc=True).limit(1).execute().data)
    return (rows[0]["version"] + 1) if rows else 1
