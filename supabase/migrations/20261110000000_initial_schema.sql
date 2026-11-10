-- Keystone initial schema. Personal projects, local embeddings (Ollama nomic-embed-text, 768 dims).
create extension if not exists vector;

create type event_source as enum ('claude', 'chatgpt', 'manual');
create type entity_type as enum ('decision', 'task', 'constraint', 'component', 'observation');
-- proposed -> approved -> implemented; superseded/rejected are terminal
create type entity_status as enum ('proposed', 'approved', 'implemented', 'superseded', 'rejected', 'open', 'done');
create type job_status as enum ('pending', 'processing', 'completed', 'failed');

create table projects (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  description text,
  created_at timestamptz not null default now()
);
create index on projects (owner_id);

-- Append-only source of truth
create table events (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  source event_source not null,
  event_type text not null,            -- e.g. decision_proposed, decision_approved, task_created
  payload jsonb not null,
  source_ref text,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);
create index on events (project_id, occurred_at desc);

-- Current-state projection (rebuildable from events)
create table entities (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  type entity_type not null,
  subject text not null,               -- e.g. "database"; used to detect supersession
  title text not null,
  current_state jsonb not null default '{}',
  status entity_status not null,
  confidence real not null default 0.5,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index on entities (project_id, type, status);
create index on entities (project_id, type, subject);

create table entity_versions (
  id uuid primary key default gen_random_uuid(),
  entity_id uuid not null references entities(id) on delete cascade,
  version int not null,
  state jsonb not null,
  status entity_status not null,
  event_id uuid not null references events(id),
  created_at timestamptz not null default now(),
  unique (entity_id, version)
);

create table relationships (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  source_entity_id uuid not null references entities(id) on delete cascade,
  target_entity_id uuid not null references entities(id) on delete cascade,
  relationship_type text not null,     -- supersedes, depends_on, relates_to
  created_at timestamptz not null default now()
);

-- Entity embeddings and source-text chunk embeddings
create table embeddings (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  entity_id uuid references entities(id) on delete cascade,
  event_id uuid references events(id) on delete cascade,
  content text not null,
  embedding vector(768) not null,
  created_at timestamptz not null default now()
);
create index on embeddings (project_id);
create index on embeddings using hnsw (embedding vector_cosine_ops);

create table jobs (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references events(id) on delete cascade,
  status job_status not null default 'pending',
  attempts int not null default 0,
  error text,
  locked_at timestamptz,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);
create index on jobs (status, created_at);

-- Atomically claim one job; also recovers jobs stuck in 'processing' > 5 minutes. Max 3 attempts.
create function claim_job() returns setof jobs language plpgsql as $$
declare j jobs;
begin
  select * into j from jobs
   where attempts < 3
     and (status = 'pending' or (status = 'processing' and locked_at < now() - interval '5 minutes'))
   order by created_at
   for update skip locked limit 1;
  if not found then return; end if;
  update jobs set status = 'processing', attempts = attempts + 1, locked_at = now()
   where id = j.id returning * into j;
  return next j;
end $$;

create function match_embeddings(
  query_embedding vector(768), target_project_id uuid, match_count int default 10
) returns table (id uuid, entity_id uuid, event_id uuid, content text, similarity float)
language sql stable as $$
  select e.id, e.entity_id, e.event_id, e.content, 1 - (e.embedding <=> query_embedding)
    from embeddings e
   where e.project_id = target_project_id
   order by e.embedding <=> query_embedding
   limit match_count;
$$;

-- RLS: owners access their own projects' data. The backend uses the service role (bypasses RLS)
-- and enforces access itself; these policies protect any direct client access.
alter table projects enable row level security;
alter table events enable row level security;
alter table entities enable row level security;
alter table entity_versions enable row level security;
alter table relationships enable row level security;
alter table embeddings enable row level security;
alter table jobs enable row level security;  -- no policies: service role only

create policy own_projects on projects for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy own_events on events for all using (project_id in (select id from projects where owner_id = auth.uid()));
create policy own_entities on entities for all using (project_id in (select id from projects where owner_id = auth.uid()));
create policy own_versions on entity_versions for all using (entity_id in (select e.id from entities e join projects p on p.id = e.project_id where p.owner_id = auth.uid()));
create policy own_relationships on relationships for all using (project_id in (select id from projects where owner_id = auth.uid()));
create policy own_embeddings on embeddings for all using (project_id in (select id from projects where owner_id = auth.uid()));
