-- Milestone 3: full-text search over embedded content, fused with vector search (reciprocal rank fusion).
alter table embeddings add column fts tsvector generated always as (to_tsvector('english', content)) stored;
create index on embeddings using gin (fts);

-- Candidates for retrieval: top match_count by vector and by full text within one project, fused with
-- RRF (k = 60). Full text ORs the query's lexemes so natural-language questions still match on any term.
-- iterative_scan keeps the HNSW scan going until enough rows pass the project filter (pgvector >= 0.8).
create function hybrid_search(
  query_embedding vector(768), query_text text, target_project_id uuid, match_count int default 50
) returns table (id uuid, entity_id uuid, event_id uuid, content text, similarity float,
                 vector_rank bigint, text_rank bigint, score float)
language sql stable
set hnsw.iterative_scan = 'relaxed_order'
as $$
  with q as (
    select replace(plainto_tsquery('english', query_text)::text, ' & ', ' | ')::tsquery as t
  ), vec as (
    select s.id, row_number() over (order by s.d) as r
      from (select e.id, e.embedding <=> query_embedding as d
              from embeddings e where e.project_id = target_project_id
             order by d limit match_count) s
  ), txt as (
    select s.id, row_number() over (order by s.rank desc) as r
      from (select e.id, ts_rank_cd(e.fts, q.t) as rank
              from embeddings e, q where e.project_id = target_project_id and e.fts @@ q.t
             order by rank desc limit match_count) s
  )
  select e.id, e.entity_id, e.event_id, e.content, 1 - (e.embedding <=> query_embedding),
         vec.r, txt.r, coalesce(1.0 / (60 + vec.r), 0) + coalesce(1.0 / (60 + txt.r), 0)
    from vec full outer join txt on txt.id = vec.id
    join embeddings e on e.id = coalesce(vec.id, txt.id)
   order by 8 desc
   limit match_count;
$$;
