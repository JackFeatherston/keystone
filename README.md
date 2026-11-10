# Keystone (MVP)

Shared project context for AI tools. MVP clients: **Claude** and **ChatGPT** only.

- Supabase Postgres + pgvector (`supabase/migrations`), accessed via `supabase-py` (no ORM)
- Embeddings: local Ollama `nomic-embed-text` (768 dims)
- FastAPI backend with a DB-backed job worker (`claim_job()` RPC) running in-process
- Events are the append-only source of truth; entities/versions are the derived state
- AI-submitted decisions are stored as `proposed`; only `manual` source can approve

## Run
```
ollama pull nomic-embed-text
supabase link --project-ref <ref> && supabase db push
cd backend && python3.10+ -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # fill in
uvicorn app.main:app --reload
```
- Claude: add `http://localhost:8000/mcp/` as a remote MCP server (Bearer `KEYSTONE_API_KEY`).
- ChatGPT: needs a public HTTPS URL (e.g. ngrok) — connect `/mcp/` as a connector, or import `/openapi.json` as GPT Actions.
