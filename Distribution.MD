# Distribution

How Keystone gets packaged, sold and set up by customers once the MVP works. Written 2026-11-16.

## Decisions

| Area | Decision |
|---|---|
| Customer | Developers using several AI tools (Claude, ChatGPT, Cursor, etc.) |
| Delivery | **Hosted service.** We run Keystone; customers connect their AI clients to it by URL |
| Data storage | Our cloud (Supabase Postgres + pgvector). Local data storage is dropped for v1 (see [Future: local data](#future-local-data-in-sqlite)) |
| Models | Cloud API models for extraction and embeddings, replacing local Ollama. Many users' hardware gives poor results with local models |
| Model costs | **Included in the plan.** Keystone calls the models with our keys; each tier caps usage |
| Clients at launch | Claude.ai / Claude Desktop, Claude Code, ChatGPT, Cursor / other IDEs |
| Install | Nothing required. Optional single CLI (`npx keystone init`) to configure local clients |
| Dashboard | Minimal: sign up, Connect page, usage, data export/delete. Proposals are still reviewed in chat through MCP tools |

## Business model

- **Now (early access):** free, invite-only. Goal is adoption and feedback, not revenue.
- **Later:** tiered monthly subscription with a free tier.
  - **Free tier:** low usage caps (e.g. a small number of projects and extraction calls per month). Enough to see the value.
  - **Paid tiers:** higher caps on projects, stored items and extraction calls. Possible later tiers for teams (team collaboration is excluded from the MVP).
- **Cost to watch:** every `log_context` triggers paid LLM calls (extraction, plus one contradiction check per pair) and embedding calls. Tier caps must be hard limits, metered per user, so a free user can't run up a bill.
- **Billing:** Stripe, added when paid tiers launch.

## How it's sold

- Sold as a hosted subscription from a marketing site plus dashboard (e.g. `keystone.dev`).
- Sign up with GitHub, since buyers are developers.
- Early access gated by invite code or waitlist.
- Distribution channels to consider: listing in client directories (Claude connector directory, ChatGPT apps, Cursor MCP directory), the npm package for the CLI, developer communities.

## Customer setup (target: under 2 minutes)

1. **Sign up** on the site (GitHub login; invite code during early access).
2. **Connect page:** one card per client.
   - **Claude.ai / Claude Desktop:** Settings → Connectors → add custom connector → paste `https://mcp.keystone.dev/mcp` → log in in the popup → done.
   - **ChatGPT:** same steps, through its connector settings.
   - **Claude Code:** copy button for `claude mcp add --transport http keystone https://mcp.keystone.dev/mcp`.
   - **Cursor / VS Code:** "Add to Cursor" / "Add to VS Code" install links.
   - **Shortcut:** `npx keystone init` finds installed clients on the machine and configures all of them at once.
3. **Check it works:** the page tells them to ask their AI "what does Keystone know about my project?" A starter project created at signup gives them something back immediately.

### OAuth is the key piece

Today the MCP endpoint uses one shared API key (header, or `?key=` in the URL for ChatGPT). With OAuth 2.1 on `/mcp` (part of the MCP auth spec, supported by Claude.ai, ChatGPT, Claude Code and Cursor), a customer pastes a URL, logs in once, and every client works the same way. No keys to copy. This matters more for a smooth setup than anything else here.

### The CLI is optional

Once OAuth exists, every client can be set up from a URL or one command, so the CLI is a convenience. Ship the Connect page first. Build the CLI on npm (`npx`, no install), since Claude Code and Cursor users nearly always have Node.

## Backend changes needed

| # | Change | Why |
|---|---|---|
| 1 | **Multi-user.** Replace `KEYSTONE_API_KEY` / `KEYSTONE_OWNER_ID` with a per-request user taken from the auth token, and filter by owner on every query | One deployment currently serves one person. The backend uses the Supabase service role, which ignores RLS, so the code must filter itself |
| 2 | **OAuth 2.1 on `/mcp`.** Supabase Auth's OAuth server, or an auth provider with MCP support (WorkOS, Stytch, Clerk) | Makes the one-paste setup possible |
| 3 | **Cloud models.** A small, cheap model for extraction (e.g. Claude Haiku) and an embeddings API (Voyage or OpenAI `text-embedding-3-small`; Anthropic has no embeddings API) | Removes Ollama. A new embedding model changes the vector size: migrate `vector(768)` and re-run `app/reembed.py`. Re-run all evals, since prompts were tuned on qwen2.5:7b |
| 4 | **Usage metering per user** (extraction calls, stored items), tied to tiers | We pay for every call |
| 5 | **Deploy:** FastAPI on Fly.io / Render / Railway, Supabase Cloud for the database. Later, run the worker as its own process | `claim_job()` already lets several workers share one queue, so scaling out is easy |
| 6 | **Minimal dashboard:** Next.js on Vercel with Supabase Auth. Sign up, Connect page, usage, data export/delete | Export/delete matters: developers will put sensitive project context into Keystone. Needs a privacy policy too |

## Build order

1. Multi-user + OAuth → **check:** two test accounts can't see each other's projects; Claude.ai connects with only a URL and a login.
2. Cloud models → **check:** extraction, contradiction and retrieval evals match or beat the current qwen2.5 numbers.
3. Dashboard + Connect page → invite early users for free.
4. Usage metering → Stripe tiers → `npx keystone init`.

## Future: local data in SQLite

Considered and deferred: letting users keep their data on their own machine (SQLite) while compute stays in the cloud.

- **Appeal:** "your project data stays on your machine" for privacy-sensitive developers; lower storage cost for us.
- **Main problem:** Claude.ai web and ChatGPT can only reach a public HTTPS URL, so they can't talk to a laptop directly. Options if revisited:
  - **Hosted relay:** the local Keystone opens an outbound tunnel to our relay (like ngrok) and gets a stable URL. Data stays local at rest but passes through our relay in transit.
  - **Local mode = IDE only:** local mode serves Claude Code, Claude Desktop and Cursor over stdio/localhost; using Claude.ai web or ChatGPT requires cloud mode.
- **Other costs:**
  - Postgres-specific pieces (pgvector, `hybrid_search()`, `claim_job()`, RLS) would need SQLite equivalents (e.g. `sqlite-vec` and FTS5), or a storage layer that supports both.
  - Extraction still sends text to cloud models, so "data never leaves your machine" would not be fully true unless local models are also offered.
  - Sync, backups and multi-device use become the user's problem.
- **Install path if built:** the same CLI (`npx keystone`) running a local server and configuring clients.
- **Revisit when:** early users ask for it, or teams/enterprise need self-hosting.
