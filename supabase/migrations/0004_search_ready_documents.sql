-- =============================================================================
-- 0004 - search only ready documents, with a non-zero similarity floor
-- =============================================================================
-- Two search defects, both invisible to the unit tests because they live in the
-- SQL function rather than the Python caller:
--
-- 1. A document's chunks are insertable while the document is still `pending`
--    or `processing`. The old function ranked every chunk regardless of the
--    parent document's status, so a half-indexed document could answer a
--    question even though the API never marked it ready.
--
-- 2. `min_similarity` defaulted to 0.0. The application now passes an explicit
--    floor, but a direct RPC call (or any future caller that forgets to pass
--    one) still ranked near-orthogonal noise. The default is now 0.1, matching
--    DEFAULT_MIN_SCORE in app/rag/retriever.py and RETRIEVAL_MIN_SCORE.
--
-- SECURITY INVOKER is unchanged and must stay that way: running as the caller
-- is what keeps Row Level Security in charge of which rows exist, so another
-- tenant's chunks can never be ranked.
--
-- Safe to re-run.
-- =============================================================================

create or replace function public.match_document_chunks(
    query_embedding     vector(768),
    match_count         integer default 8,
    min_similarity      double precision default 0.1,
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
    where d.status = 'ready'
      and (filter_document_ids is null or c.document_id = any (filter_document_ids))
      and 1 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding <=> query_embedding
    limit least(greatest(coalesce(match_count, 8), 1), 100);
$$;

comment on function public.match_document_chunks is
    'Cosine similarity search over the caller''s own, fully indexed chunks. Runs '
    'as the caller so Row Level Security applies: it can never return another '
    'tenant''s rows. Chunks of documents that are not `ready` are excluded.';

revoke all on function public.match_document_chunks(vector, integer, double precision, uuid[]) from anon;
grant execute on function public.match_document_chunks(vector, integer, double precision, uuid[]) to authenticated;
grant execute on function public.match_document_chunks(vector, integer, double precision, uuid[]) to service_role;

notify pgrst, 'reload schema';
