-- =============================================================================
-- 0007 - an append-only audit trail
-- =============================================================================
-- Phase 7. The request log already records every HTTP call, but a log line is
-- not an audit record: it rotates, it is not queryable by the user it concerns,
-- and it does not survive a change of log backend. This table records the
-- actions that change data or produce a lasting artefact - an upload, a delete,
-- a workflow run, a report - with the actor, the resource, and the request id
-- that ties it back to the log.
--
-- Design decisions worth knowing:
--
-- 1. **The actor comes from the token, never from the caller.** The only write
--    path is `record_audit_event()`, a SECURITY DEFINER function that takes no
--    actor argument and uses `auth.uid()`. A caller cannot write an event
--    attributed to someone else.
--
-- 2. **No update or delete for application roles.** `authenticated` may read
--    its own events and nothing more. The service role holds delete for
--    retention; no application role holds update, so a recorded event cannot be
--    rewritten through an application role.
--
-- 3. **No foreign key to auth.users.** Deleting an account must not erase the
--    record of what that account did. Retention is an explicit administrative
--    action, not a side effect of user deletion.
--
-- 4. **Detail is bounded.** The table check caps it at 8 KiB, so a caller
--    cannot turn the audit trail into a storage sink.
--
-- Safe to re-run.
-- =============================================================================

create table if not exists public.audit_events (
    id            uuid primary key default gen_random_uuid(),
    created_at    timestamptz not null default now(),
    actor_id      uuid not null,
    action        text not null check (char_length(action) between 1 and 100),
    resource_type text check (resource_type is null or char_length(resource_type) <= 50),
    resource_id   text check (resource_id is null or char_length(resource_id) <= 200),
    request_id    text check (request_id is null or char_length(request_id) <= 128),
    detail        jsonb not null default '{}'::jsonb
                  check (octet_length(detail::text) <= 8192)
);

comment on table public.audit_events is
    'Append-only audit trail (Phase 7). Written only through record_audit_event(), '
    'which takes the actor from auth.uid(). No update grant exists for application '
    'roles.';

create index if not exists idx_audit_events_actor
    on public.audit_events (actor_id, created_at desc);

create index if not exists idx_audit_events_created
    on public.audit_events (created_at desc);

alter table public.audit_events enable row level security;

revoke all on public.audit_events from public;
revoke all on public.audit_events from anon;
revoke all on public.audit_events from authenticated;
revoke all on public.audit_events from service_role;
grant select on public.audit_events to authenticated;
grant select, delete on public.audit_events to service_role;

drop policy if exists audit_events_select_own on public.audit_events;
create policy audit_events_select_own on public.audit_events
    for select to authenticated using (actor_id = auth.uid());


-- -----------------------------------------------------------------------------
-- The only write path
-- -----------------------------------------------------------------------------
create or replace function public.record_audit_event(
    action text,
    resource_type text default null,
    resource_id text default null,
    request_id text default null,
    detail jsonb default '{}'::jsonb
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    actor uuid := auth.uid();
    event_id uuid;
begin
    if actor is null then
        raise exception 'Authentication is required to record an audit event.'
            using errcode = '42501';
    end if;

    if action is null or btrim(action) = '' then
        raise exception 'An audit action is required.' using errcode = '22023';
    end if;

    insert into public.audit_events
        (actor_id, action, resource_type, resource_id, request_id, detail)
    values (
        actor,
        btrim(action),
        nullif(btrim(resource_type), ''),
        nullif(btrim(resource_id), ''),
        nullif(btrim(request_id), ''),
        coalesce(detail, '{}'::jsonb)
    )
    returning id into event_id;

    return event_id;
end;
$$;

comment on function public.record_audit_event(text, text, text, text, jsonb) is
    'Phase 7: append one audit event for the caller. The actor is auth.uid(); '
    'an unauthenticated call is refused.';

revoke execute on function public.record_audit_event(text, text, text, text, jsonb)
    from public;
revoke execute on function public.record_audit_event(text, text, text, text, jsonb)
    from anon;
grant execute on function public.record_audit_event(text, text, text, text, jsonb)
    to authenticated;
grant execute on function public.record_audit_event(text, text, text, text, jsonb)
    to service_role;

notify pgrst, 'reload schema';
