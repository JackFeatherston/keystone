from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import service
from ..config import settings
from ..db import supabase as sb


def require_key(authorization: str = Header(default="")):
    if authorization != f"Bearer {settings.KEYSTONE_API_KEY}":
        raise HTTPException(401, "Invalid API key")


router = APIRouter(dependencies=[Depends(require_key)])


class ProjectIn(BaseModel):
    name: str
    description: str | None = None


class EventIn(BaseModel):
    source: Literal["claude", "chatgpt", "manual"]
    event_type: str
    payload: dict
    source_ref: str | None = None


class DecisionIn(BaseModel):
    source: Literal["claude", "chatgpt", "manual"]
    title: str
    subject: str | None = None
    value: str | None = None
    reason: str | None = None
    approved: bool = False  # honoured only for source == manual


@router.post("/projects", operation_id="create_project")
def create_project(body: ProjectIn):
    return sb.table("projects").insert({**body.model_dump(), "owner_id": settings.KEYSTONE_OWNER_ID}).execute().data[0]


@router.get("/projects", operation_id="list_projects")
def list_projects():
    return sb.table("projects").select("*").eq("owner_id", settings.KEYSTONE_OWNER_ID).execute().data


@router.post("/projects/{project_id}/events", operation_id="add_event")
def add_event(project_id: str, body: EventIn):
    return service.add_event(project_id, body.source, body.event_type, body.payload, body.source_ref)


@router.get("/projects/{project_id}/state", operation_id="get_project_state")
def get_state(project_id: str):
    return service.get_project_state(project_id)


@router.get("/projects/{project_id}/search", operation_id="search_context")
def search(project_id: str, q: str, limit: int = 10):
    return service.search_context(project_id, q, limit)


@router.get("/projects/{project_id}/decisions", operation_id="get_decisions")
def decisions(project_id: str, include_history: bool = False):
    return service.get_decisions(project_id, include_history)


@router.get("/projects/{project_id}/tasks", operation_id="get_open_tasks")
def tasks(project_id: str):
    return service.get_open_tasks(project_id)


@router.post("/projects/{project_id}/decisions", operation_id="record_decision")
def record_decision(project_id: str, body: DecisionIn):
    kind = "decision_approved" if body.approved else "decision_proposed"
    return service.add_event(project_id, body.source, kind, body.model_dump(exclude={"source", "approved"}))
