-- =============================================================================
-- 0001 - documents, chunks (pgvector) and Row Level Security
-- =============================================================================
-- Phase 4 of the Enterprise AI Intelligence Platform.
--
-- This file is safe to run repeatedly: every statement is idempotent, so a
-- partial failure can be fixed and the whole file re-run.
--
-- Apply it one of two ways:
--   * Supabase dashboard -> SQL Editor -> paste -> Run (simplest)
--   * python backend/scripts/apply_migrations.py   (needs SUPABASE_DB_URL)
--
-- -----------------------------------------------------------------------------
-- Design decisions worth knowing about
-- -----------------------------------------------------------------------------
-- 1. ISOLATION IS ENFORCED BY POSTGRES, NOT BY PYTHON.
--    Every row carries owner_id and every policy compares it to auth.uid().
--    The API deliberately sends the *caller's* access token to PostgREST rather
--    than the service-role key, so a bug in the application layer cannot leak
--    another tenant's rows - the database refuses them. Application-level
--    "WHERE owner_id = ..." filters stay in place as a second layer.
--
-- 2. chunks.owner_id IS DENORMALISED ON PURPOSE.
--    Vector search filters on it directly, so the policy check costs nothing at
--    query time and never needs a join back to documents.
--
-- 3. ids ARE uuid, THE API STILL SPEAKS HEX.
--    Phase 3 generated 32-character hex ids. The repository converts at the
--    boundary so both stores satisfy the same contract. Malformed ids therefore
--    cannot match anything, which turns a hostile input into a clean 404.
--
-- 4. VECTOR WIDTH IS FIXED AT 768 (matches nomic-embed-text).
--    pgvector requires a fixed width. If EMBEDDING_MODEL changes to a model with
--    a different output size you must add a migration, for example:
--        alter table public.chunks alter column embedding type vector(1536);
--    and re-index: existing vectors cannot be reinterpreted.
-- =============================================================================

create extension if not exists vector;

-- -----------------------------------------------------------------------------
-- updated_at maintenance
-- -----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

comment on function public.set_updated_at() is
    'Trigger helper: stamps updated_at on every UPDATE.';


-- -----------------------------------------------------------------------------
-- Table: documents
-- -----------------------------------------------------------------------------
create table if not exists public.documents (
    id            uuid        primary key default gen_random_uuid(),
    owner_id      uuid        not null references auth.users (id) on delete cascade,
    filename      text        not null check (length(filename) between 1 and 255),
    content_type  text        not null,
    extension     text        not null,
    size_bytes    bigint      not null check (size_bytes >= 0),
    status        text        not null default 'pending'
                              check (status in ('pending', 'processing', 'ready', 'failed')),
    error         text,
    page_count    integer     check (page_count is null or page_count >= 0),
    chunk_count   integer     not null default 0 check (chunk_count >= 0),
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    indexed_at    timestamptz
);

comment on table public.documents is
    'One row per uploaded file. Holds metadata only - never the file contents.';
comment on column public.documents.filename is
    'Display name only. It never touches a filesystem, so it cannot traverse paths.';
comment on column public.documents.status is
    'pending -> processing -> ready | failed. A failed row is kept so the user can see why.';

drop trigger if exists documents_set_updated_at on public.documents;
create trigger documents_set_updated_at
    before update on public.documents
    for each row
    execute function public.set_updated_at();


-- -----------------------------------------------------------------------------
-- Table: chunks
-- -----------------------------------------------------------------------------
create table if not exists public.chunks (
    id           uuid        primary key default gen_random_uuid(),
    document_id  uuid        not null references public.documents (id) on delete cascade,
    owner_id     uuid        not null references auth.users (id) on delete cascade,
    chunk_index  integer     not null check (chunk_index >= 0),
    content      text        not null check (length(content) > 0),
    page         integer     check (page is null or page >= 1),
    section      text,
    embedding    vector(768) not null,
    created_at   timestamptz not null default now(),

    -- Re-indexing a document replaces its chunks. This makes that idempotent
    -- instead of silently duplicating every passage.
    constraint chunks_document_chunk_index_key unique (document_id, chunk_index)
);

comment on table public.chunks is
    'One row per embedded passage. content is the exact text the model is shown.';
comment on column public.chunks.owner_id is
    'Copy of documents.owner_id. Denormalised so the RLS check needs no join.';
comment on column public.chunks.embedding is
    'Cosine-normalised space. Retrieval uses the <=> (cosine distance) operator.';


-- -----------------------------------------------------------------------------
-- Indexes
-- -----------------------------------------------------------------------------
-- Listing a user's documents newest-first.
create index if not exists documents_owner_created_idx
    on public.documents (owner_id, created_at desc);

-- Chunk lookups scoped to an owner, and per-document replacement.
create index if not exists chunks_owner_idx
    on public.chunks (owner_id);
create index if not exists chunks_document_idx
    on public.chunks (document_id, chunk_index);

-- Approximate nearest-neighbour index for similarity search.
-- HNSW is chosen over ivfflat because it needs no training pass: it is correct
-- on a table with ten rows and on one with ten million, whereas ivfflat built
-- before there is data produces useless lists. The cost is a slower write path,
-- which is the right trade for a read-heavy retrieval workload.
create index if not exists chunks_embedding_hnsw_idx
    on public.chunks using hnsw (embedding vector_cosine_ops);


-- -----------------------------------------------------------------------------
-- Row Level Security
-- -----------------------------------------------------------------------------
-- Note the (select auth.uid()) form rather than a bare auth.uid(). Both work,
-- but wrapping it lets the planner evaluate the function once per query instead
-- of once per row - a large difference on a sequential scan.
--
-- FORCE ROW LEVEL SECURITY is deliberately NOT set: it would also subject the
-- table owner (the `postgres` role used by the dashboard) to these policies.
-- The service_role bypasses RLS by attribute, which is what admin scripts need.
-- -----------------------------------------------------------------------------
alter table public.documents enable row level security;
alter table public.chunks    enable row level security;

-- documents ------------------------------------------------------------------
drop policy if exists documents_select_own on public.documents;
create policy documents_select_own on public.documents
    for select to authenticated
    using (owner_id = (select auth.uid()));

drop policy if exists documents_insert_own on public.documents;
create policy documents_insert_own on public.documents
    for insert to authenticated
    with check (owner_id = (select auth.uid()));

drop policy if exists documents_update_own on public.documents;
create policy documents_update_own on public.documents
    for update to authenticated
    using (owner_id = (select auth.uid()))
    with check (owner_id = (select auth.uid()));

drop policy if exists documents_delete_own on public.documents;
create policy documents_delete_own on public.documents
    for delete to authenticated
    using (owner_id = (select auth.uid()));

-- chunks ---------------------------------------------------------------------
-- The insert/update checks carry an extra EXISTS: owner_id alone would let a
-- caller insert a chunk they own that points at someone else's document, and a
-- later join would then surface a foreign filename. Requiring the parent
-- document to be visible under RLS closes that off.
drop policy if exists chunks_select_own on public.chunks;
create policy chunks_select_own on public.chunks
    for select to authenticated
    using (owner_id = (select auth.uid()));

drop policy if exists chunks_insert_own on public.chunks;
create policy chunks_insert_own on public.chunks
    for insert to authenticated
    with check (
        owner_id = (select auth.uid())
        and exists (
            select 1
            from public.documents d
            where d.id = document_id
              and d.owner_id = (select auth.uid())
        )
    );

drop policy if exists chunks_update_own on public.chunks;
create policy chunks_update_own on public.chunks
    for update to authenticated
    using (owner_id = (select auth.uid()))
    with check (
        owner_id = (select auth.uid())
        and exists (
            select 1
            from public.documents d
            where d.id = document_id
              and d.owner_id = (select auth.uid())
        )
    );

drop policy if exists chunks_delete_own on public.chunks;
create policy chunks_delete_own on public.chunks
    for delete to authenticated
    using (owner_id = (select auth.uid()));


-- -----------------------------------------------------------------------------
-- Privileges
-- -----------------------------------------------------------------------------
-- Supabase grants anon/authenticated broad access to `public` by default. These
-- statements narrow that: an unauthenticated caller (role `anon`) has no
-- business reading either table, and RLS alone would only make its queries
-- return zero rows rather than fail.
-- -----------------------------------------------------------------------------
revoke all on public.documents from anon;
revoke all on public.chunks    from anon;

grant select, insert, update, delete on public.documents to authenticated;
grant select, insert, update, delete on public.chunks    to authenticated;

grant all on public.documents to service_role;
grant all on public.chunks    to service_role;


-- -----------------------------------------------------------------------------
-- Vector search
-- -----------------------------------------------------------------------------
-- SECURITY INVOKER is the whole point of this function and must not be changed.
-- Running as the caller means Row Level Security applies inside it, so the
-- embedding of another tenant's document can never be ranked. A SECURITY
-- DEFINER version would look identical and quietly return everyone's data.
--
-- min_similarity defaults to 0.0, i.e. "return the nearest passages whatever
-- their score". The retriever applies the real relevance floor in Python so the
-- threshold can be tuned without a migration.
-- -----------------------------------------------------------------------------
create or replace function public.match_document_chunks(
    query_embedding     vector(768),
    match_count         integer default 8,
    min_similarity      double precision default 0.0,
    filter_document_ids uuid[] default null
)
returns table (
    id           uuid,
    document_id  uuid,
    filename     text,
    chunk_index  integer,
    content      text,
    page         integer,
    section      text,
    similarity   double precision
)
language sql
stable
security invoker
set search_path = public
as $$
    select
        c.id,
        c.document_id,
        d.filename,
        c.chunk_index,
        c.content,
        c.page,
        c.section,
        1 - (c.embedding <=> query_embedding) as similarity
    from public.chunks c
    join public.documents d on d.id = c.document_id
    where (filter_document_ids is null or c.document_id = any (filter_document_ids))
      and 1 - (c.embedding <=> query_embedding) >= min_similarity
    -- ORDER BY repeats the distance expression rather than aliasing `similarity`,
    -- because that is what lets the planner use the HNSW index. Ordering by the
    -- computed alias would force a full sort.
    order by c.embedding <=> query_embedding
    limit least(greatest(coalesce(match_count, 8), 1), 100);  -- cap the page size
$$;

comment on function public.match_document_chunks is
    'Cosine similarity search over the caller''s own chunks. Runs as the caller '
    'so Row Level Security applies: it can never return another tenant''s rows.';

revoke all on function public.match_document_chunks(vector, integer, double precision, uuid[]) from anon;
grant execute on function public.match_document_chunks(vector, integer, double precision, uuid[]) to authenticated;
grant execute on function public.match_document_chunks(vector, integer, double precision, uuid[]) to service_role;


-- -----------------------------------------------------------------------------
-- Vector width, readable by the application
-- -----------------------------------------------------------------------------
-- The app compares this against the width its embedding model actually returns
-- so a model change produces a clear "dimension mismatch" error instead of an
-- opaque Postgres cast failure at the first query.
-- -----------------------------------------------------------------------------
create or replace function public.embedding_dimensions()
returns integer
language sql
stable
security invoker
set search_path = public, pg_catalog
as $$
    select coalesce(
        (regexp_match(format_type(a.atttypid, a.atttypmod), '\((\d+)\)'))[1]::integer,
        0
    )
    from pg_attribute a
    where a.attrelid = 'public.chunks'::regclass
      and a.attname = 'embedding'
      and not a.attisdropped;
$$;

comment on function public.embedding_dimensions() is
    'Width of chunks.embedding, so the application can detect a model/column mismatch.';

grant execute on function public.embedding_dimensions() to authenticated;
grant execute on function public.embedding_dimensions() to service_role;


-- -----------------------------------------------------------------------------
-- Refresh PostgREST's schema cache
-- -----------------------------------------------------------------------------
-- PostgREST serves from an in-memory copy of the schema. It normally notices DDL
-- on its own, but announcing it explicitly means the new tables are usable
-- immediately instead of after a puzzling few seconds of PGRST205 errors.
-- -----------------------------------------------------------------------------
notify pgrst, 'reload schema';
