"""Shared operations used by both the REST API and the MCP tools."""
from .db import supabase as sb
from .embeddings import embed


def add_event(project_id: str, source: str, event_type: str, payload: dict, source_ref: str | None = None) -> dict:
    ev = sb.table("events").insert(
        {"project_id": project_id, "source": source, "event_type": event_type,
         "payload": payload, "source_ref": source_ref}
    ).execute().data[0]
    sb.table("jobs").insert({"event_id": ev["id"]}).execute()
    return ev


def get_decisions(project_id: str, include_history: bool = False) -> list[dict]:
    q = sb.table("entities").select("*").eq("project_id", project_id).eq("type", "decision")
    if not include_history:
        q = q.neq("status", "superseded").neq("status", "rejected")
    return q.order("updated_at", desc=True).execute().data


def get_open_tasks(project_id: str) -> list[dict]:
    return (sb.table("entities").select("*").eq("project_id", project_id)
            .eq("type", "task").eq("status", "open").execute().data)


def get_project_state(project_id: str) -> dict:
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    return {"project": project, "decisions": get_decisions(project_id), "open_tasks": get_open_tasks(project_id)}


def search_context(project_id: str, query: str, limit: int = 10) -> list[dict]:
    return sb.rpc("match_embeddings", {"query_embedding": embed(query),
                                       "target_project_id": project_id, "match_count": limit}).execute().data
