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
- When a decision/constraint becomes approved, the LLM checks it against approved ones on *other* subjects;
  conflicts become a 'contradicts' relationship (flag only, nothing is superseded) until the user resolves it
  by changing one, or dismisses it (contradiction_dismissed).
- confidence is rule-based from provenance (see _confidence), recomputed on every change.
"""
from . import extraction
from .db import supabase as sb
from .embeddings import embed

ACTIVE = ["proposed", "approved", "implemented", "open"]
SETTLED = ["approved", "implemented"]  # decision/constraint states that can contradict each other
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


def process_event(event: dict, queue: bool = True, reuse_id=None) -> list[dict] | None:
    """Apply one event. queue=False: extraction returns its derived events instead of queueing jobs
    (used by rebuild). reuse_id(kind, subject, value) -> id or None: id for a newly created entity."""
    p, project_id = event["payload"], event["project_id"]
    etype = event["event_type"]

    if etype in ("proposal_confirmed", "proposal_rejected", "entity_implemented"):
        return _review(event)
    if etype == "contradiction_dismissed":
        return _dismiss(event)

    # Source-text chunk (retrievable even if extraction finds nothing)
    _embed_row(project_id, p.get("text") or f"{p.get('title', '')} {p.get('reason', '')}".strip(),
               event_id=event["id"])

    if etype == "context_logged":
        return _extract(event, queue)
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
    if status != "proposed":
        state["basis"] = "manual" if event["source"] == "manual" else "verbatim_quote"

    active = (sb.table("entities").select("*").eq("project_id", project_id)
              .eq("type", kind).eq("subject", subject).in_("status", ACTIVE)
              .order("created_at", desc=True).execute().data)
    same = next((e for e in active if kind == "task" or e["current_state"].get("value") == p.get("value")), None)

    if same and RANK[status] <= RANK[same["status"]]:
        # Restatement: nothing new. A user restating it is more evidence (once per event, so retries are no-ops).
        st = same["current_state"]
        if status != "proposed" and event["id"] not in st.get("restated_by", []):
            st["restated_by"] = st.get("restated_by", []) + [event["id"]]
            sb.table("entities").update({"current_state": st}).eq("id", same["id"]).execute()
            _refresh_confidence(same["id"])
        return

    # LLM call before any write, so a failure retries cleanly. Supersession only touches this subject,
    # so the other-subject candidates are the same before and after.
    conflicts = []
    if kind != "task" and status == "approved":
        conflicts = _find_conflicts(project_id, {"type": kind, "subject": subject, "title": p["title"],
                                                 "current_state": state})

    if same:
        entity_id, version = same["id"], _next_version(same["id"])
        sb.table("entities").update(
            {"status": status, "current_state": state, "updated_at": "now()"}
        ).eq("id", entity_id).execute()
    else:
        row = {"project_id": project_id, "type": kind, "subject": subject,
               "title": p["title"], "current_state": state, "status": status}
        if reuse_id and (rid := reuse_id(kind, subject, p.get("value"))):
            row["id"] = rid
        ent = sb.table("entities").insert(row).execute().data[0]
        entity_id, version = ent["id"], 1

    sb.table("entity_versions").insert(
        {"entity_id": entity_id, "version": version, "state": state,
         "status": status, "event_id": event["id"]}
    ).execute()
    if kind != "task":
        _supersede(project_id, entity_id, status, [e for e in active if e["id"] != entity_id], event["id"])
    _link_conflicts(project_id, entity_id, conflicts, event["id"])
    _refresh_confidence(entity_id)

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
        for other, _ in _contradictions(prior["id"]):  # its conflicts are moot now
            _refresh_confidence(other)


def _extract(event: dict, queue: bool = True) -> list[dict]:
    # Idempotent on retry: derived events already written for this excerpt -> done.
    if sb.table("events").select("id").eq("source_ref", event["id"]).limit(1).execute().data:
        return []
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
        if queue:
            sb.table("jobs").insert([{"event_id": r["id"]} for r in rows]).execute()
        return rows
    return []


def _review(event: dict) -> None:
    p, etype = event["payload"], event["event_type"]
    ent = sb.table("entities").select("*").eq("id", p["entity_id"]).single().execute().data
    state = dict(ent["current_state"])
    conflicts = []
    if etype == "proposal_confirmed":
        if ent["status"] != "proposed":
            return
        status = "open" if ent["type"] == "task" else "approved"
        state["user_quote"] = p["user_quote"]
        state["basis"] = "review"  # client-reported quote; the server can't verify it
        if ent["type"] != "task":
            conflicts = _find_conflicts(ent["project_id"], ent)
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
    _link_conflicts(ent["project_id"], ent["id"], conflicts, event["id"])
    _refresh_confidence(ent["id"])
    for other, _ in _contradictions(ent["id"]):  # e.g. a rejected entity no longer conflicts
        _refresh_confidence(other)


# ---- contradictions ----
def _find_conflicts(project_id: str, new: dict) -> list[tuple[dict, str]]:
    others = (sb.table("entities").select("*").eq("project_id", project_id)
              .in_("type", ["decision", "constraint"]).in_("status", SETTLED)
              .neq("subject", new["subject"]).execute().data)
    return extraction.find_conflicts(new, others)


def _link_conflicts(project_id, entity_id, conflicts, event_id):
    linked = {o for o, _ in _contradictions(entity_id, include_dismissed=True)}
    for other, reason in conflicts:
        if other["id"] in linked:
            continue
        sb.table("relationships").insert(
            {"project_id": project_id, "source_entity_id": entity_id, "target_entity_id": other["id"],
             "relationship_type": "contradicts", "metadata": {"reason": reason, "event_id": event_id}}
        ).execute()
        _refresh_confidence(other["id"])


def _contradictions(entity_id: str, include_dismissed: bool = False) -> list[tuple[str, dict]]:
    """(other entity id, relationship) for each contradiction this entity is part of."""
    rows = (sb.table("relationships").select("*").eq("relationship_type", "contradicts")
            .or_(f"source_entity_id.eq.{entity_id},target_entity_id.eq.{entity_id}").execute().data)
    return [(r["target_entity_id"] if r["source_entity_id"] == entity_id else r["source_entity_id"], r)
            for r in rows if include_dismissed or not r["metadata"].get("dismissed")]


def _dismiss(event: dict) -> None:
    """The user says both items hold: keep the link as history, marked dismissed."""
    p = event["payload"]
    for other, rel in _contradictions(p["source_entity_id"]):
        if other == p["target_entity_id"]:
            meta = {**rel["metadata"], "dismissed": {"user_quote": p.get("user_quote", ""), "event_id": event["id"]}}
            sb.table("relationships").update({"metadata": meta}).eq("id", rel["id"]).execute()
            _refresh_confidence(p["source_entity_id"])
            _refresh_confidence(p["target_entity_id"])


# ---- confidence ----
BASIS = {"manual": 0.95, "verbatim_quote": 0.9, "review": 0.8}


def _confidence(status: str, state: dict, contradicted: bool) -> float:
    """Rule-based, from provenance: how the item got its status, restatements, open contradictions."""
    if status == "proposed":
        c = 0.3
    elif status in ("implemented", "done"):
        c = 0.95
    else:
        c = BASIS.get(state.get("basis"), 0.8)
    c = min(c + 0.05 * len(state.get("restated_by", [])), 0.99)
    if contradicted:
        c -= 0.2
    return round(max(c, 0.1), 2)


def _refresh_confidence(entity_id: str) -> None:
    ent = sb.table("entities").select("status,current_state").eq("id", entity_id).single().execute().data
    if ent["status"] in ("superseded", "rejected"):
        return  # history: keep the confidence it had
    others = [o for o, _ in _contradictions(entity_id)]
    contradicted = bool(others) and ent["status"] in SETTLED and bool(
        sb.table("entities").select("id").in_("id", others).in_("status", SETTLED).execute().data)
    sb.table("entities").update(
        {"confidence": _confidence(ent["status"], ent["current_state"], contradicted)}).eq("id", entity_id).execute()


def _next_version(entity_id: str) -> int:
    rows = (sb.table("entity_versions").select("version").eq("entity_id", entity_id)
            .order("version", desc=True).limit(1).execute().data)
    return (rows[0]["version"] + 1) if rows else 1
