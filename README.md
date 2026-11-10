# Keystone

Shared context infrastructure for the AI-native world. Keystone connects AI tools (Claude, ChatGPT, and later Cursor, GitHub, etc.) through a continuously updated understanding of your projects, decisions, and workflows.

## Pitch

Unlike traditional AI memory solutions that simply store and retrieve information, Keystone reconstructs the current state of your work, resolves conflicting information, and delivers verified, task-specific context to any AI agent. As businesses adopt multi-agent workflows, Keystone becomes the intelligence layer connecting them: one source of truth, powering every AI.

## What the MVP must prove

> Does an AI agent perform better when Keystone gives it an accurate, continuously maintained understanding of a project?

If that can't be demonstrated, the rest of the architecture doesn't matter. The MVP is a developer-first context engine, not a universal AI memory platform.

## Core principles

- **Events are the source of truth.** Events are append-only (what happened, when, from where). Entities are a derived, rebuildable projection of current state. Never silently overwrite history; new decisions create new versions and supersede old ones.
- **Provenance everywhere.** Every extracted fact links back to its source event so Keystone can explain where its understanding came from.
- **Separate proposals from reality.** Proposed decision ≠ approved decision ≠ implemented change. An AI-generated claim never becomes verified state automatically.
- **Hybrid context management.** Automatic ingestion, human approval for important state changes. An LLM should not silently rewrite a project's source of truth.
- **Everything scoped to a project** (isolation, retrieval, RLS).
- **Keep MCP tools simple;** the intelligence lives in the backend.

## Decisions made

| Area | Decision |
|---|---|
| Customer | Individual developers using multiple AI tools; personal projects first, design for teams later |
| MVP AI clients | **Claude and ChatGPT only** (Cursor, others deferred) |
| Interface | MCP server mounted in the FastAPI backend (+ REST/OpenAPI, usable as ChatGPT Actions) |
| Database | Supabase PostgreSQL |
| Semantic search | pgvector in Supabase (not ChromaDB) |
| DB access | `supabase-py`, no ORM; schema via Supabase SQL migrations |
| Embeddings | **Local, via Ollama** (`nomic-embed-text`, 768 dims). Changing model means changing `vector(768)` |
| What gets embedded | Both structured entities and supporting source text |
| Background jobs | `jobs` table in Supabase + worker inside FastAPI (atomic `claim_job()` RPC, retries, stale-job recovery). No Celery/Redis |
| Backend | FastAPI + Python, modular monolith (no microservices) |
| Frontend (later) | Next.js + TypeScript dashboard |
| Deliberately excluded | ChromaDB, SQLAlchemy, Redis, Celery, Kafka, graph DB, autonomous agents, team collaboration |

## Architecture

```
AI clients (Claude, ChatGPT)
        │  MCP / REST
FastAPI backend ── ingestion → context engine (extract/resolve) → retrieval
        │                         ▲ job worker (jobs table)
Supabase: Postgres + pgvector (events, entities, entity_versions,
          relationships, embeddings, jobs)
Ollama (local): embeddings
```

Repo layout:

```
supabase/migrations/   SQL schema, RLS, claim_job(), match_embeddings()
backend/app/
  api/                 REST routes + API-key auth
  context_engine.py    events -> entities/versions/relationships + embeddings
  jobs/worker.py       job loop
  mcp/server.py        MCP tools
  service.py           shared operations (REST + MCP)
  embeddings.py        Ollama client
```

MCP tools: `get_project_state`, `search_context`, `get_decisions`, `get_open_tasks`, `record_decision`.

Example of what the engine should produce from "We're replacing MongoDB with PostgreSQL because our data relationships are getting complex": a new active decision (subject `database`, value PostgreSQL, with reason and source), while the MongoDB decision becomes `superseded` and linked, but stays queryable as history.

## Status

Code is written but **has not been run yet** (only syntax-checked). No Supabase project exists yet.

## Run

```
ollama pull nomic-embed-text
supabase link --project-ref <ref> && supabase db push
cd backend && python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt   # needs Python 3.10+
cp .env.example .env   # fill in
uvicorn app.main:app --reload
```

- Claude: add `http://localhost:8000/mcp/` as a remote MCP server (Bearer `KEYSTONE_API_KEY`).
- ChatGPT: needs a public HTTPS URL (e.g. ngrok); connect `/mcp/` as a connector, or import `/openapi.json` as GPT Actions.

## TODO

**Get it running (Milestone 1)**
- [ ] Create Supabase project; create an Auth user and note its UUID
- [ ] Install Supabase CLI; `supabase link` and `supabase db push`
- [ ] Install Python 3.10+, create venv, install requirements
- [ ] Fill in `backend/.env`
- [ ] Start server; verify create project → record decision → worker processes job → search returns it
- [ ] Fix whatever breaks on first run
- [ ] Add tests (supersession logic, proposed vs approved rules, job claiming)

**Connect clients**
- [ ] Connect Claude via `/mcp/` and test all five tools
- [ ] Expose via HTTPS (ngrok) and connect ChatGPT (connector or Actions)

**Context engine (Milestone 2)**
- [ ] LLM extraction of decisions/tasks/constraints/components/observations from free text (hybrid: proposals need human approval)
- [ ] Contradiction detection and confidence scoring in state resolution
- [ ] Approval flow for proposed → approved → implemented
- [ ] Rebuild entities from events (reprocessing when logic improves)

**Retrieval (Milestone 3)**
- [ ] Hybrid search (vector + full-text), ranking by relevance, authority, recency, confidence
- [ ] Task-specific context packages that exclude superseded facts, with source attribution

**Later**
- [ ] GitHub ingestion (webhooks for PRs, commits, issues, README) as evidence of implementation
- [ ] Next.js dashboard: project overview, decision history, open tasks, context inspection ("what Keystone knows and where it learned it")
- [ ] Replace shared API key with real Supabase Auth (JWT) and per-user access
- [ ] Cursor and other clients; team projects
- [ ] Benchmark local embedding models; evaluate whether agents actually perform better with Keystone (the core thesis)
- [ ] Deploy (Railway or Azure)
