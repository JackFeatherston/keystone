"""Integration tests against the real Supabase DB (uses backend/.env). Each test gets a throwaway project.
Run with the dev server stopped: claim_job() is global and the server's worker would steal jobs."""
import uuid

import pytest

from app import context_engine, service
from app.config import settings
from app.db import supabase as sb


@pytest.fixture(autouse=True)
def stub_embed(monkeypatch):
    monkeypatch.setattr(context_engine, "embed", lambda text: [0.0] * 768)


@pytest.fixture
def project():
    p = sb.table("projects").insert({"owner_id": settings.KEYSTONE_OWNER_ID, "name": f"test-{uuid.uuid4().hex[:6]}"}).execute().data[0]
    yield p["id"]
    # entity_versions.event_id has no ON DELETE CASCADE, so remove entities (-> versions) before the project
    sb.table("entities").delete().eq("project_id", p["id"]).execute()
    sb.table("projects").delete().eq("id", p["id"]).execute()


def decide(pid, source, etype, value, subject="database"):
    ev = service.add_event(pid, source, etype, {"title": f"Use {value}", "subject": subject, "value": value, "reason": "r"})
    context_engine.process_event(ev)
    sb.table("jobs").delete().eq("event_id", ev["id"]).execute()  # processed directly; don't leave a pending job
    return ev


def entities(pid, **eq):
    q = sb.table("entities").select("*").eq("project_id", pid)
    for k, v in eq.items():
        q = q.eq(k, v)
    return q.execute().data


def test_new_decision_supersedes_old_and_keeps_history(project):
    decide(project, "manual", "decision_approved", "MongoDB")
    decide(project, "manual", "decision_approved", "PostgreSQL")
    by_value = {e["current_state"]["value"]: e for e in entities(project, type="decision")}
    assert by_value["MongoDB"]["status"] == "superseded"
    assert by_value["PostgreSQL"]["status"] == "approved"
    assert [d["current_state"]["value"] for d in service.get_decisions(project)] == ["PostgreSQL"]
    assert len(service.get_decisions(project, include_history=True)) == 2
    rel = sb.table("relationships").select("*").eq("project_id", project).execute().data
    assert len(rel) == 1 and rel[0]["relationship_type"] == "supersedes"
    assert rel[0]["source_entity_id"] == by_value["PostgreSQL"]["id"]
    assert rel[0]["target_entity_id"] == by_value["MongoDB"]["id"]


def test_ai_source_cannot_approve(project):
    decide(project, "claude", "decision_approved", "PostgreSQL")
    assert entities(project, type="decision")[0]["status"] == "proposed"


def test_manual_source_can_approve(project):
    decide(project, "manual", "decision_approved", "PostgreSQL")
    assert entities(project, type="decision")[0]["status"] == "approved"


def test_approving_same_value_versions_existing_entity(project):
    decide(project, "claude", "decision_proposed", "PostgreSQL")
    decide(project, "manual", "decision_approved", "PostgreSQL")
    ents = entities(project, type="decision")
    assert len(ents) == 1 and ents[0]["status"] == "approved"
    versions = sb.table("entity_versions").select("version,status").eq("entity_id", ents[0]["id"]).order("version").execute().data
    assert [(v["version"], v["status"]) for v in versions] == [(1, "proposed"), (2, "approved")]


def test_tasks_are_open_and_listed(project):
    ev = service.add_event(project, "manual", "task_created", {"title": "Write docs"})
    context_engine.process_event(ev)
    sb.table("jobs").delete().eq("event_id", ev["id"]).execute()
    assert [t["title"] for t in service.get_open_tasks(project)] == ["Write docs"]


def test_events_are_append_only_source_of_truth(project):
    decide(project, "manual", "decision_approved", "MongoDB")
    decide(project, "manual", "decision_approved", "PostgreSQL")
    assert len(sb.table("events").select("id").eq("project_id", project).execute().data) == 2


# ---- job claiming ----
def make_jobs(pid, n):
    ids = []
    for i in range(n):
        ev = service.add_event(pid, "manual", "note", {"title": f"n{i}"})
        ids.append(sb.table("jobs").select("id").eq("event_id", ev["id"]).execute().data[0]["id"])
    return ids


def test_claim_job_is_exclusive_and_counts_attempts(project):
    mine = set(make_jobs(project, 2))
    a, b = sb.rpc("claim_job").execute().data[0], sb.rpc("claim_job").execute().data[0]
    assert a["id"] != b["id"] and {a["id"], b["id"]} == mine
    assert a["status"] == "processing" and a["attempts"] == 1
    assert sb.rpc("claim_job").execute().data == []  # nothing left


def test_stale_processing_job_is_recovered(project):
    (jid,) = make_jobs(project, 1)
    assert sb.rpc("claim_job").execute().data[0]["id"] == jid
    assert sb.rpc("claim_job").execute().data == []  # fresh lock: not claimable
    sb.table("jobs").update({"locked_at": "2000-01-01T00:00:00Z"}).eq("id", jid).execute()
    again = sb.rpc("claim_job").execute().data[0]
    assert again["id"] == jid and again["attempts"] == 2


def test_job_not_claimed_after_three_attempts(project):
    (jid,) = make_jobs(project, 1)
    sb.table("jobs").update({"attempts": 3}).eq("id", jid).execute()
    assert sb.rpc("claim_job").execute().data == []
