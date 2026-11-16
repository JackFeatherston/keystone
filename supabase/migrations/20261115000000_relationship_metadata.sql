-- Per-relationship details, e.g. the reason for a 'contradicts' link.
alter table relationships add column metadata jsonb not null default '{}';
create index on relationships (project_id, relationship_type);
