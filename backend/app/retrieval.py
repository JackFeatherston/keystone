"""Retrieval (Milestone 3): hybrid search -> status-aware, ranked results -> task context packages.

Read-only over what the context engine writes. Candidate rows come from hybrid_search() (vector + full text,
fused by RRF). Each row is either an entity embedding or a source chunk (an event's text). A chunk is folded
into the entities that event produced (directly, or via derived events whose source_ref is the excerpt), so an
excerpt inherits its entities' status: an excerpt whose decisions were all superseded is history, not current
state. Chunks that produced no entity are returned as 'source' items.

score = fused relevance (best row per item) x authority(status) x recency.
"""
import json
from datetime import datetime, timezone

from .db import supabase as sb
from .embeddings import embed_query

# Mild on purpose: RRF scores are compressed, so big multipliers would override relevance (a 0.5 weight on
# raw sources buried the one excerpt that answered a question). Verified state wins ties.
AUTHORITY = {"implemented": 1.0, "approved": 1.0, "open": 1.0, "done": 0.95, "proposed": 0.85,
             "source": 0.85, "superseded": 0.5, "rejected": 0.5}
HISTORY = ("superseded", "rejected")
RECENCY_HALF_LIFE_DAYS = 30
CANDIDATES = 50


def _recency(ts: str) -> float:
    """1.0 for now, decaying to 0.8: a tiebreaker, not a filter (old approved decisions still hold)."""
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 86400
    return 0.8 + 0.2 * 0.5 ** (max(age, 0) / RECENCY_HALF_LIFE_DAYS)


def _chunk_entities(event_ids: list[str]) -> dict[str, set[str]]:
    """Chunk event id -> ids of the entities it produced. Versions written by a supersession carry the
    superseding event's id, so those are excluded: a 'PostgreSQL' excerpt didn't produce the MongoDB entity."""
    if not event_ids:
        return {}
    derived = sb.table("events").select("id,source_ref").in_("source_ref", event_ids).execute().data
    chunks_of: dict[str, set[str]] = {e: {e} for e in event_ids}  # event -> chunks it counts for
    for d in derived:
        chunks_of.setdefault(d["id"], set()).add(d["source_ref"])
    versions = (sb.table("entity_versions").select("entity_id,event_id").in_("event_id", list(chunks_of))
                .neq("status", "superseded").execute().data)
    out: dict[str, set[str]] = {}
    for v in versions:
        for c in chunks_of[v["event_id"]]:
            out.setdefault(c, set()).add(v["entity_id"])
    return out


def search(project_id: str, query: str, limit: int = 10, include_history: bool = False) -> list[dict]:
    rows = sb.rpc("hybrid_search", {"query_embedding": embed_query(query), "query_text": query,
                                    "target_project_id": project_id, "match_count": CANDIDATES}).execute().data
    produced = _chunk_entities([r["event_id"] for r in rows if r["event_id"]])
    ids = {r["entity_id"] for r in rows if r["entity_id"]} | {e for s in produced.values() for e in s}
    ents = {e["id"]: e for e in sb.table("entities").select("*").in_("id", list(ids)).execute().data} if ids else {}

    relevance: dict[str, float] = {}  # item key -> best fused score among its rows
    chunks: dict[str, dict] = {}
    for r in rows:
        targets = [r["entity_id"]] if r["entity_id"] else sorted(produced.get(r["event_id"], ()))
        targets = [t for t in targets if t in ents]
        if not targets:
            chunks[r["event_id"]] = r
            targets = [r["event_id"]]
        for t in targets:
            relevance[t] = max(relevance.get(t, 0), r["score"])

    if chunks:
        created = {e["id"]: e["created_at"] for e in sb.table("events").select("id,created_at")
                   .in_("id", list(chunks)).execute().data}
    results = []
    for k, rel in relevance.items():
        if k in ents:
            e = ents[k]
            if e["status"] in HISTORY and not include_history:
                continue
            st = e["current_state"]
            item = {"kind": e["type"], "entity_id": e["id"], "event_id": None, "subject": e["subject"],
                    "title": e["title"], "value": st.get("value"), "reason": st.get("reason"),
                    "status": e["status"], "confidence": e["confidence"], "source": st.get("source"),
                    "user_quote": st.get("user_quote"), "updated_at": e["updated_at"]}
            ts = e["updated_at"]
        else:
            c = chunks[k]
            ts = created[k]
            item = {"kind": "source", "entity_id": None, "event_id": k, "content": c["content"],
                    "status": "source", "updated_at": ts}
        item["score"] = round(rel * AUTHORITY[item["status"]] * _recency(ts), 6)
        results.append(item)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


# ---- context packages ----
PACKAGE_NOTE = ("Current project state relevant to the task, most relevant first. decisions/constraints/tasks "
                "are confirmed by the user (or marked implemented/done). 'unverified' items are AI suggestions the "
                "user has not confirmed: don't treat them as decided. 'changes' lists what each item replaced; "
                "don't use the old value. 'notes' are raw source excerpts. 'contradictions' need the user's input.")
SECTION = {"decision": "decisions", "constraint": "constraints", "task": "tasks"}


def _tokens(x) -> int:
    return len(json.dumps(x, default=str)) // 4 + 1  # rough estimate, good enough for a budget


def _excerpt_ids(entity_ids: list[str]) -> dict[str, str]:
    """Entity id -> the excerpt (or event) it was first recorded from."""
    v1 = (sb.table("entity_versions").select("entity_id,event_id").in_("entity_id", entity_ids)
          .eq("version", 1).execute().data)
    evs = {e["id"]: e for e in sb.table("events").select("id,source_ref")
           .in_("id", [v["event_id"] for v in v1]).execute().data} if v1 else {}
    return {v["entity_id"]: evs[v["event_id"]]["source_ref"] or v["event_id"] for v in v1 if v["event_id"] in evs}


def _replaced(entity_ids: list[str]) -> dict[str, list[dict]]:
    """Entity id -> the entities it superseded."""
    rels = (sb.table("relationships").select("source_entity_id,target_entity_id").eq("relationship_type", "supersedes")
            .in_("source_entity_id", entity_ids).execute().data)
    if not rels:
        return {}
    old = {e["id"]: e for e in sb.table("entities").select("*")
           .in_("id", [r["target_entity_id"] for r in rels]).execute().data}
    out: dict[str, list[dict]] = {}
    for r in rels:
        if r["target_entity_id"] in old:
            out.setdefault(r["source_entity_id"], []).append(old[r["target_entity_id"]])
    return out


def get_context(project_id: str, task: str, max_tokens: int = 1500, max_items: int = 12,
                contradictions: list[dict] | None = None) -> dict:
    """Token-budgeted context for a task. contradictions: unresolved ones (service.get_contradictions)."""
    hits = search(project_id, task, limit=max_items)
    ent_ids = [h["entity_id"] for h in hits if h["entity_id"]]
    excerpt = _excerpt_ids(ent_ids) if ent_ids else {}
    replaced = _replaced(ent_ids) if ent_ids else {}

    pkg = {"task": task, "note": PACKAGE_NOTE, "decisions": [], "constraints": [], "tasks": [],
           "unverified": [], "changes": [], "notes": [], "contradictions": [], "truncated": False}
    budget = max_tokens - _tokens(pkg)
    for h in hits:
        if h["kind"] == "source":
            section, item = "notes", {"excerpt_id": h["event_id"], "text": h["content"][:500],
                                      "date": h["updated_at"][:10]}
        else:
            section = "unverified" if h["status"] == "proposed" else SECTION[h["kind"]]
            item = {"id": h["entity_id"], "title": h["title"], "value": h["value"], "status": h["status"],
                    "confidence": h["confidence"], "since": h["updated_at"][:10],
                    "source": {"client": h["source"], "user_quote": h["user_quote"],
                               "excerpt_id": excerpt.get(h["entity_id"])}}
            if section == "unverified":
                item["kind"] = h["kind"]
            if h["reason"]:
                item["reason"] = h["reason"]
        changes = [{"subject": h["subject"], "was": o["current_state"].get("value") or o["title"],
                    "now": h["value"] or h["title"], "changed": h["updated_at"][:10]}
                   for o in replaced.get(h["entity_id"], [])]
        cost = _tokens(item) + sum(_tokens(c) for c in changes)
        if cost > budget:
            pkg["truncated"] = True
            break
        budget -= cost
        pkg[section].append(item)
        pkg["changes"] += changes

    included = {i["id"] for s in ("decisions", "constraints", "tasks", "unverified") for i in pkg[s]}
    for c in contradictions or []:
        if c["a"]["id"] in included or c["b"]["id"] in included:
            pkg["contradictions"].append({"id": c["id"], "reason": c["reason"],
                                          "between": [c["a"]["title"], c["b"]["title"]]})
    return {k: v for k, v in pkg.items() if v or k in ("task", "note", "truncated")}
