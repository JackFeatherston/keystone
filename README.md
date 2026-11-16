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
- **Separate proposals from reality.** Proposed decision ≠ approved decision ≠ implemented change. An AI-*inferred* claim never becomes verified state automatically; a decision the user states in conversation, relayed with their exact words as provenance, does.
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
supabase/migrations/   SQL schema, RLS, claim_job(), match_embeddings(), hybrid_search()
backend/app/
  api/                 REST routes + API-key auth
  context_engine.py    events -> entities/versions/relationships + embeddings; confidence; contradictions
  extraction.py        excerpt -> decisions/constraints/tasks (Ollama) + verbatim-quote guard; contradiction check
  rebuild.py           rebuild a project's entities from its events (re-runs extraction)
  retrieval.py         hybrid search -> status-aware ranked results; get_context packages
  reembed.py           recompute all stored vectors (after an embedding change)
  jobs/worker.py       job loop
  mcp/server.py        MCP tools
  service.py           shared operations (REST + MCP)
  embeddings.py        Ollama client (nomic task prefixes: embed / embed_query)
backend/evals/         extraction eval (cases.json, run_eval.py), contradiction eval (conflict_cases.json, run_conflict_eval.py),
                       retrieval/ (fixture.json seed project, cases.json + run_retrieval_eval.py, ab_cases.json + run_ab_eval.py)
```

MCP tools: `list_projects`, `get_project_state`, `get_context`, `search_context`, `get_decisions`, `get_open_tasks`, `log_context`, `list_proposals`, `confirm_proposal`, `reject_proposal`, `mark_implemented`, `dismiss_contradiction`. All but `list_projects` take an optional `project` (name or ID).

Example of what the engine should produce from "We're replacing MongoDB with PostgreSQL because our data relationships are getting complex": a new active decision (subject `database`, value PostgreSQL, with reason and source), while the MongoDB decision becomes `superseded` and linked, but stays queryable as history.

## Status

Milestone 1 done: runs end to end against a Supabase cloud project (create project → record decision → worker → search). Tests: `cd backend && pytest` (integration tests against the real DB; stop the dev server first, since `claim_job()` is global).

Milestone 2 done (apart from re-testing both clients against the new tools): `log_context` → server-side extraction (Ollama `qwen2.5:7b-instruct`) → guarded entity events → MCP review. Extraction eval: `cd backend && python -m evals.run_eval [--model <ollama model>] [-v]` (needs Ollama running; no DB writes).

| Eval (18 cases) | precision | recall | correct status |
|---|---|---|---|
| qwen2.5:7b, first prompt | 0.69 | 0.69 | 0.69 |
| qwen2.5:7b, current prompt | 0.87 | 0.81 | 0.81 |
| llama3.1:8b, current prompt | 0.72 | 0.81 | 0.75 |
| qwen2.5:7b, 19 cases, before task/decision wording | 0.81 | 0.76 | 0.76 |
| **qwen2.5:7b, 19 cases, current prompt** | **0.94** | **0.88** | **0.88** |
| llama3.1:8b, 19 cases, task/decision wording | 0.74 | 0.82 | 0.76 |
| **qwen2.5:7b, 25 cases (+6 real Claude excerpts), `kind` after `title`/`value` in the schema** | **1.00** | **0.91** | **0.91** |

Runs are deterministic at temperature 0 (two identical runs, 2026-11-15). Remaining misses are in the safe direction (two unanswered assistant suggestions dropped, one extra proposal), not false approvals. Moving `kind` after `title`/`value` in the JSON schema (the model names the item before classifying it) fixed "We're using SQLite" being filed as a constraint. A longer prompt rule for the same thing broke task extraction, so it was dropped. Grow `evals/cases.json` from real excerpts before tuning further; 25 cases is easy to overfit.

Contradiction check: `cd backend && python -m evals.run_conflict_eval [--model ...]` (10 cases).

| Contradiction eval (10 cases) | precision | recall |
|---|---|---|
| qwen2.5:7b, first prompt (return conflicts only) | 0.67 | 0.33 |
| **qwen2.5:7b, current prompt (verdict per item)** | **0.86** | **1.00** |
| llama3.1:8b, verdict per item | 0.50 | 1.00 |
| **qwen2.5:7b, 13 cases (+3 real), one call per pair, "take an item's own words at face value"** | **0.88** | **1.00** |

Batched checks mixed items up (a reason about item C attached to the pair A/B), so each pair is now its own call. Remaining false positive: "PostgreSQL" vs "must run fully offline" (the model thinks Postgres needs the network). False flags are cheap (flag only, dismissable), missed ones aren't, so recall is the priority.

Rebuild: `cd backend && python -m app.rebuild <project> [--yes]` (stop the dev server first).

Milestone 3 done (apart from the live client check): hybrid, status-aware retrieval and `get_context`. Retrieval eval: `cd backend && python -m evals.retrieval.run_retrieval_eval [-v]` (seeds and deletes a throwaway project; needs Ollama).

| Retrieval eval (20 queries, k=5) | recall@5 | MRR | stale rate |
|---|---|---|---|
| vector search before M3 | 0.81 | 0.69 | 0.30 |
| **hybrid + status-aware + ranking** | **0.88** | **0.71–0.75** | **0.05** |

MRR varies run to run (near-ties); the baseline MRR is within that noise, the recall and stale-rate gains are not. The 0.88 was stable over 4 runs.

Thesis A/B: `python -m evals.retrieval.run_ab_eval [--model ...] [--judge ...] [-v]`. qwen2.5:7b answers 10 project questions, qwen2.5:7b judges against a reference and a known-outdated answer (2026-11-15, one run at temperature 0).

| Agent context | correct | presents an outdated choice as current |
|---|---|---|
| none | 0/10 | 1/10 |
| raw vector search (pre-M3 `search_context`) | 6/10 | 3/10 (Auth0, GraphQL, "we decided on Algolia") |
| **`get_context`** | **9/10** | **0/10** |

The one `get_context` miss is a judge error (it listed exactly the three open tasks). The 7B judge is noisy: before it was asked to write a reason first, it misgraded three answers. First evidence for the thesis, on a small synthetic project with a weak agent; the outdated-answer column is the point: raw search hands the agent stale facts with nothing marking them stale.

## Run

```
ollama pull nomic-embed-text && ollama pull qwen2.5:7b-instruct
supabase link --project-ref <ref> && supabase db push
cd backend && python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt   # needs Python 3.10+
cp .env.example .env   # fill in
uvicorn app.main:app --reload
```

- Claude: add `http://localhost:8000/mcp/` as a remote MCP server (Bearer `KEYSTONE_API_KEY`).
- ChatGPT: needs a public HTTPS URL; connect `/mcp/` as a connector, or import `/openapi.json` as GPT Actions.
  - **Tunnel: ngrok** with a free static domain (`ammonia-easing-giggly.ngrok-free.dev`): `ngrok http --url=ammonia-easing-giggly.ngrok-free.dev 8000`. The URL survives restarts, so the ChatGPT connector only needs setting up once. Needs the ngrok authtoken configured (`ngrok config add-authtoken`).
  - cloudflared quick tunnels (`cloudflared tunnel --url http://localhost:8000`) also work but get a new random URL on every start, which forces recreating the ChatGPT connector.
  - The ChatGPT connector can't send headers, so it authenticates with `/mcp/?key=<KEYSTONE_API_KEY>` (no-auth mode). Test-only; rotate the key after use and replace with real auth later.

## TODO

**Get it running (Milestone 1)**
- [x] Create Supabase project; create an Auth user and note its UUID
- [x] Install Supabase CLI; `supabase link` and `supabase db push`
- [x] Install Python 3.10+, create venv, install requirements
- [x] Fill in `backend/.env`
- [x] Start server; verify create project → record decision → worker processes job → search returns it
- [x] Fix whatever breaks on first run
- [x] Add tests (supersession logic, proposed vs approved rules, job claiming)

**Connect clients**
- [x] Connect Claude via `/mcp/` and test all five tools
- [x] Expose via HTTPS (cloudflared) and connect ChatGPT (connector, `?key=` auth)

### Decided: conversational approval

> Milestone 2 replaced the MCP `record_decision` tool with `log_context` + server-side extraction (see below). The user-stated vs. AI-inferred principle is unchanged; the `decision_confirmed` / `decision_proposed` events below are now emitted by the extractor.

The user shouldn't have to re-explain context or name-drop IDs. Saying "yeah we're now using postgres" to any AI should record it. The line is between **user-stated** and **AI-inferred** claims, not between human and AI clients:

- `record_decision(decided_by="user", user_quote="<user's exact words>")` → `decision_confirmed` event → **approved**. The quote is stored in the entity state as provenance.
- `record_decision(decided_by="ai_suggestion")` → `decision_proposed` → **proposed** until the user confirms (confirming the same subject + value versions the same entity).
- Tool descriptions tell models to record decisions unprompted, to skip hedged statements, and to call `get_decisions` first and reuse the existing `subject` so supersession works.
- MCP tools take an optional `project` (name or ID; empty if there's only one project), plus a `list_projects` tool.

Tradeoff: this trusts the AI client to report honestly that the user decided. The verbatim quote keeps every approval auditable, and a wrong one is fixed by stating the correction (which supersedes it). Revisit for teams. The manual REST path (`source: "manual"`, `approved: true`) still works as a fallback. Review becomes an audit step rather than a gate (via MCP tools in Milestone 2; see below). Milestone 2 moves this user-stated vs. AI-inferred judgement from the client into server-side extraction.

### Decided: Milestone 2 direction

Focus is **engine depth, specifically extraction quality**.

- **Server-side extraction.** Clients push conversation excerpts via a new MCP tool (`log_context`); a backend LLM extracts decisions/tasks/constraints as entities with provenance back to the excerpt event. Extraction logic lives in one place and can be reprocessed when it improves.
- **Local LLM via Ollama**, sized for a 16GB Apple Silicon machine (7–8B, e.g. `qwen2.5:7b` / `llama3.1:8b`).
- **Single path.** Everything routes through extraction, including user-stated decisions; the direct `record_decision` path is retired once `log_context` works.
- **Verbatim-quote guard.** The extractor only auto-approves a user-stated decision if its `user_quote` appears verbatim in the excerpt; everything else is a proposal. This keeps a small model's mistakes from becoming verified state.
- **Review via MCP**, no UI yet: `list_proposals`, `confirm_proposal`, `mark_implemented`.
- **Measured, not eyeballed.** A labeled eval set (excerpts → expected entities) with a precision/recall script, used to compare prompts and models.

Known tensions: a 7–8B model is weakest at exactly the user-stated vs. AI-inferred distinction, so expect more legitimate decisions to land as proposals than with today's client-side calls. Pushed excerpts still depend on the client choosing to call the tool, so eval cases should look like what clients actually send, not clean transcripts.

### Decided: state resolution (end of Milestone 2)

- **Confidence is rule-based, from provenance**, not self-reported by the LLM (small models calibrate poorly). Proposed 0.3; approved: manual 0.95, verbatim-quote guarded 0.9, confirmed via `confirm_proposal` 0.8 (the quote is client-reported); implemented/done 0.95. +0.05 per later user restatement (once per event, max 0.99); −0.2 while in an unresolved contradiction. Recomputed on every change, so it's rebuildable.
- **Contradictions are cross-subject and flag only.** Same-subject changes are supersession, as before. When a decision/constraint becomes approved, the LLM checks it against approved/implemented items on *other* subjects (e.g. "must run offline" vs "use the OpenAI API"). A hit is a `contradicts` relationship with a reason, listed in `get_project_state` (`contradictions`). Nothing is superseded automatically. It's resolved when one side changes (supersession) or the user dismisses it (`dismiss_contradiction` → `contradiction_dismissed` event; the link is kept, marked dismissed). Proposals are not checked.
- **Rebuild replays and re-extracts.** The projection is wiped. The extractor's derived events are deleted and regenerated from each `context_logged` excerpt with the current prompt/model, and all other events are replayed in order. Derived events are machine output, so regenerating them doesn't break "events are the source of truth": the excerpts are. Rebuilt entities reuse the old id for the same (type, subject, value), so review events keep pointing at the right entity. Reviews whose target disappeared (e.g. a new subject name) are skipped and reported. Not paginated (fine below ~1000 events per project).
- **`confirm_proposal` quote stays unguarded.** The server can't see the client's conversation, so it can't verify the quote. The quote is kept as an audit trail, and these approvals get lower confidence (0.8). Revisit with teams / real auth.

**Context engine (Milestone 2)**
- [x] Eval set + scoring script (fixtures of realistic excerpts with expected entities; precision/recall per entity type) → baseline score
- [x] `log_context` MCP tool → `context_logged` event → extraction job → Ollama extractor producing decisions/tasks/constraints with provenance → eval score reported, tests pass
- [x] Verbatim-quote rule for auto-approval → eval case with a paraphrased quote yields a proposal
- [x] MCP review tools (`list_proposals`, `confirm_proposal`, `reject_proposal`, `mark_implemented`) → integration tests for proposed → approved → implemented
- [x] Retire direct `record_decision` from MCP; all AI clients use `log_context` (REST `record_decision` kept as the manual fallback)
- [x] Resolution fixes surfaced by extraction: an AI proposal never supersedes or downgrades approved state; confirming a proposal supersedes the old approved one
- [x] Re-test ChatGPT connector against the new tools → calls `log_context` unprompted with verbatim user text; skips questions and hedged statements; Memcached superseded Redis; confirm/reject work (tested 2026-11-12 on `mcp-smoke-test`)
- [x] Fix: ChatGPT told the user a decision was a "proposal" when the server had approved it, because `log_context` only returns `queued`. Tool description + response `note` now say the outcome is unknown and to check `get_decisions` etc.
- [x] Re-test Claude (claude.ai or desktop → Settings → Connectors → custom connector at `https://<ngrok domain>/mcp/?key=<KEYSTONE_API_KEY>`). Results below
- [x] Refresh the ChatGPT connector and re-test. Results below
- [x] Commit today's changes (`app/mcp/server.py`, new eval case, README)
- [x] Extraction: "i need to add rate limiting before launch" is now an open task. Runs are deterministic, so no variance to rule out. Sharper decision/task wording fixed it and the Vercel decision-as-task miss: 0.81 / 0.76 / 0.76 → 0.94 / 0.88 / 0.88
- [x] Known gap: `confirm_proposal` only checks that `user_quote` is non-empty. Decided: no guard (see "state resolution" above); such approvals get lower confidence
- [ ] Note: ChatGPT invented an assistant turn in one `log_context` excerpt. Harmless now (only user text is evidence), but keep in mind
- [x] Contradiction detection (cross-subject, flag only) + eval set; rule-based confidence scoring → tests pass
- [x] Rebuild entities from events (`python -m app.rebuild`), re-extracting excerpts → tests for id reuse and skipped reviews
- [x] Re-test Claude (2026-11-15, `recipe-app`). Client behaved well: it called `log_context` unprompted with verbatim user text, skipped the question and the hedge, spotted the OpenAI vs offline conflict itself, and confirm/reject/mark_implemented worked. Extraction did not: SQLite was filed as a constraint, the offline constraint reused subject `database` and superseded it, and "let's use the OpenAI API to ..." became a task, so no contradiction was flagged. All added as eval cases and fixed (above); `recipe-app` was rebuilt with `app.rebuild`
- [ ] Known issue (live replay of `recipe-app`): "Drop OpenAI, we'll generate tags locally with Ollama instead" produced three items: the Ollama decision, a "drop the OpenAI API" decision that superseded it, and a restated offline constraint. The same excerpt passes in the eval, so live output is noisier than the eval. Consider dropping same-subject duplicates within one excerpt, or a bigger model
- [x] ChatGPT, first try: it never called a tool and answered from its own memory (the log showed only the connector refresh). Fixed with MCP server `instructions` + a `list_projects` description saying Keystone is the source of truth for project questions
- [x] ChatGPT retest (2026-11-15, `recipe-app`): answered "what database and why" from Keystone, quoting the recorded user words; listed open tasks, proposals with confidence, and 0 contradictions exactly as stored; logged verbatim user text; "Cloudinary" was flagged against the offline constraint, and "local folder instead" superseded it and cleared the flag; `mark_implemented` worked
- [ ] `dismiss_contradiction` not yet exercised by a real client (in the test the only false flag had already become moot). Covered by integration tests

### Milestone 3 plan: retrieval

**Goal:** an agent starting a task gets the current, correct, attributed context in one call. Measure retrieval quality, and whether that context makes the agent better (first test of the MVP thesis).

Starting point: `search_context` returns `content` + `similarity` only (no type/status), so superseded decisions and old excerpt chunks come back as if current. No full-text search; `nomic-embed-text` task prefixes unused; project filter runs after the global HNSW scan (can return < `limit`); provenance exists (entity → version → event → `source_ref` excerpt) but isn't surfaced.

**Kept separate from M2.** M3 reads what M2 writes and runs at query time; it doesn't change how entities, statuses, versions or relationships are produced.
- Don't touch `context_engine.py`, `extraction.py`, `evals/cases.json`. M3 lives in a new migration and `evals/retrieval/`, plus new functions/tools added to `service.py`, `mcp/server.py` and `embeddings.py` (M2 also edits the first two; add, don't rewrite).
- The retrieval eval seeds its fixture project with structured events (`decision_confirmed`, `task_created`, proposals, supersessions), not `context_logged`, so scores don't move with extraction prompt/model changes.
- No dependence on confidence scoring (M2); rank without it, add later.

**Retrieval (Milestone 3)**
- [x] Retrieval eval + scorer (`evals/retrieval/`): seeded fixture project (27 steps, 5 supersessions, a rejection, proposals, orphan notes), 20 labeled queries; recall@5, MRR, stale rate → baseline 0.81 / 0.69 / 0.30
- [x] Status-aware results: chunks fold into the entities they produced (so excerpts inherit status and each fact appears once); superseded/rejected hidden unless `include_history` → stale rate 0.30 → 0.05 (remaining: an orphan note about the old Mongo schema, which produced no entity to inherit from)
- [x] Embedding fixes: nomic prefixes, `python -m app.reembed` backfill (run 2026-11-15, 39 rows), HNSW iterative scan inside `hybrid_search()`
- [x] Hybrid search: `fts` column + GIN, `hybrid_search()` with RRF; full-text ORs query lexemes so questions still match → exact-term queries (`WCAG`, `Postmark`, `Auth0`) hit
- [x] Ranking: RRF × authority × recency. Authority is deliberately mild (1.0 / 0.95 / 0.85 / 0.5): a 0.5 weight on raw sources buried the one excerpt that answered a question. The eval can't tell flat from mild weights (no case where status should reorder), so weights are a judgement call, not tuned
- [x] `get_context` MCP tool + `GET /projects/{id}/context`: decisions/constraints/tasks with attribution (client, `user_quote`, excerpt id, date), `unverified` proposals, `rejected` suggestions (added after the A/B: without them the agent re-suggested Redis), `changes` ("was MongoDB → now PostgreSQL"), notes, contradictions; token budget (~1500)
- [x] Thesis A/B (results in Status)
- [x] Live check (2026-11-16, recipe-app; 5 prompts per client, one chat each, Keystone not mentioned). When read, Keystone context was used correctly: Claude built on PostgreSQL ("Keystone says you switched from SQLite"), offline, local folder, and called `sharp` "proposed… you haven't confirmed it yet". No stale fact presented as current; no tool calls on the control prompt (15% of 80). But only 4 reads for 6 project prompts across both clients (2 `get_context`, 1 `search_context`, 1 `get_project_state`), so clients don't reliably read before answering. The "no project named" prompt didn't test inference (asked mid-chat, both assumed recipe-app). ChatGPT's answers to prompts 1-3 weren't captured
- [x] Fixed from the live check: `log_context` fired on plain questions and the client's own answers (9 junk proposals + a "task" that was a request done in-chat). Its description now says user commitments only, not questions or requests carried out in the chat. Extraction keeps one item per (kind, subject) per excerpt: 7 thumbnail items from one answer superseded each other (and the user's `sharp` proposal); the earlier "Drop OpenAI, we'll use Ollama" → "drop the OpenAI API" superseding Ollama was the same failure. Extraction eval unchanged (1.00 / 0.91 / 0.91). recipe-app cleaned up with manual events (Ollama re-approved with the original quote, junk rejected, `sharp` re-proposed, the in-chat request marked done)
- [ ] Re-run the live check in a fresh chat per prompt (incl. project inference), and confirm `log_context` now stays quiet on questions. If clients still skip reads, strengthen the `get_context` description
- [x] Merged into main (fast-forward, 2026-11-16)
- [ ] Re-run the A/B with a stronger agent/judge than qwen2.5:7b; grow both eval sets from real projects (20 queries / 10 questions are easy to overfit)
- [ ] Ranking: use M2's confidence once it has settled; orphan excerpts that describe superseded state (needs extraction to link them, or an "as of" date shown to the agent)
- Decided: excerpt chunks inherit entity status (vs. entities only): keeps excerpts that produced no entity (e.g. the trail API limits) retrievable

**Later**
- [ ] GitHub ingestion (webhooks for PRs, commits, issues, README) as evidence of implementation
- [ ] Next.js dashboard: project overview, decision history, open tasks, context inspection ("what Keystone knows and where it learned it")
- [ ] Replace shared API key with real Supabase Auth (JWT) and per-user access
- [ ] Cursor and other clients; team projects
- [ ] Benchmark local embedding models; evaluate whether agents actually perform better with Keystone (the core thesis)
- [ ] Deploy (Railway or Azure)
