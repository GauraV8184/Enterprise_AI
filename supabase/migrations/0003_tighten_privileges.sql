-- =============================================================================
-- 0003 - least privilege, and remove a redundant index
-- =============================================================================
-- Both problems were found by inspecting the live catalog after 0001 and 0002
-- had been applied. Reading a migration back to yourself does not reveal either
-- of them, which is the argument for checking the database rather than the file.
--
-- Safe to re-run.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Excess privileges held by `authenticated`
-- -----------------------------------------------------------------------------
-- Supabase grants ALL on every new table in `public` to `anon` and
-- `authenticated`. 0001 revoked `anon` outright, but for `authenticated` it only
-- *added* GRANT SELECT/INSERT/UPDATE/DELETE - and adding privileges does not
-- remove the ones already granted. The result was that `authenticated` still
-- held REFERENCES, TRIGGER and TRUNCATE.
--
-- TRUNCATE is the one that matters. It is NOT subject to Row Level Security:
--   https://www.postgresql.org/docs/current/ddl-rowsecurity.html
-- so a TRUNCATE privilege on `documents` lets any signed-in user empty every
-- tenant's rows in a single statement, policies notwithstanding.
--
-- PostgREST does not expose TRUNCATE, so this was not reachable through the API.
-- That is luck rather than design, and a privilege that is harmless only because
-- the current client happens not to use it is not a privilege worth keeping.
--
-- TRIGGER and REFERENCES are revoked for the same reason: neither is filtered by
-- RLS, and no endpoint needs either. A future endpoint that genuinely does can
-- ask for it in its own migration, where the trade-off will be visible.
--
-- The sequence matters: revoke first, then grant back exactly what is needed.
revoke all on public.documents from authenticated;
revoke all on public.chunks    from authenticated;

grant select, insert, update, delete on public.documents to authenticated;
grant select, insert, update, delete on public.chunks    to authenticated;


-- -----------------------------------------------------------------------------
-- 2. Redundant index on chunks
-- -----------------------------------------------------------------------------
-- The unique constraint `chunks_document_chunk_index_key` on
-- (document_id, chunk_index) already creates a btree over exactly those columns.
-- `chunks_document_idx` was a second, identical copy: it costs disk and write
-- throughput on every insert, and the planner would never choose it over the
-- constraint's index.
drop index if exists public.chunks_document_idx;


notify pgrst, 'reload schema';
