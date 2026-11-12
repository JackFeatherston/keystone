"""Shared operations used by both the REST API and the MCP tools."""
import uuid

from .config import settings
from .db import supabase as sb
from .embeddings import embed
from .extraction import render


def list_projects() -> list[dict]:
    return sb.table("projects").select("*").eq("owner_id", settings.KEYSTONE_OWNER_ID).execute().data


def resolve_project(project: str = "") -> str:
    """Project ID from an ID, a name (case-insensitive), or nothing (only when exactly one project exists)."""
    projects = list_projects()
    if not project:
        if len(projects) == 1:
            return projects[0]["id"]
        raise ValueError(f"Specify a project; available: {[p['name'] for p in projects]}")
    try:
        uuid.UUID(project)
        matches = [p for p in projects if p["id"] == project]
    except ValueError:
        matches = [p for p in projects if p["name"].lower() == project.strip().lower()]
    if len(matches) != 1:
        raise ValueError(f"No unique project '{project}'; available: {[p['name'] for p in projects]}")
    return matches[0]["id"]


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
    constraints = (sb.table("entities").select("*").eq("project_id", project_id).eq("type", "constraint")
                   .in_("status", ["approved", "implemented"]).execute().data)
    return {"project": project, "decisions": get_decisions(project_id), "constraints": constraints,
            "open_tasks": get_open_tasks(project_id), "pending_proposals": list_proposals(project_id)}


def log_context(project_id: str, source: str, messages: list[dict]) -> dict:
    """Queue a conversation excerpt for extraction. messages: [{role: user|assistant, content}]."""
    messages = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content", "").strip()]
    if not any(m["role"] == "user" for m in messages):
        raise ValueError("messages must include at least one user message")
    if any(m["role"] not in ("user", "assistant") for m in messages):
        raise ValueError("message role must be 'user' or 'assistant'")
    return add_event(project_id, source, "context_logged", {"messages": messages, "text": render(messages)})


def list_proposals(project_id: str) -> list[dict]:
    return (sb.table("entities").select("*").eq("project_id", project_id)
            .eq("status", "proposed").order("created_at", desc=True).execute().data)


REVIEW_FROM = {"proposal_confirmed": ("proposed",), "proposal_rejected": ("proposed",),
               "entity_implemented": ("approved", "open")}


def review(project_id: str, source: str, event_type: str, entity_id: str, **payload) -> dict:
    """Validate now (so the caller gets feedback), apply via the event worker."""
    rows = sb.table("entities").select("id,status").eq("id", entity_id).eq("project_id", project_id).execute().data
    if not rows:
        raise ValueError(f"No entity {entity_id} in this project")
    if rows[0]["status"] not in REVIEW_FROM[event_type]:
        raise ValueError(f"Entity is '{rows[0]['status']}'; expected one of {REVIEW_FROM[event_type]}")
    return add_event(project_id, source, event_type, {"entity_id": entity_id, **payload})


def search_context(project_id: str, query: str, limit: int = 10) -> list[dict]:
    return sb.rpc("match_embeddings", {"query_embedding": embed(query),
                                       "target_project_id": project_id, "match_count": limit}).execute().data
