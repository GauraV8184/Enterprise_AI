-- =============================================================================
-- 0006 - restrict generated SQL to the demo dataset
-- =============================================================================
-- Phase 7 hardening. 0005 ran generated SQL with the caller's own privileges
-- (SECURITY INVOKER) and relied on RLS plus a keyword blocklist. That stopped
-- writes, but not reads: a generated query could `select * from public.documents`
-- and the caller's RLS policy would return their own document text, which the
-- analytics route would then summarise as if it were sales data. Nothing in the
-- query text has to look dangerous for that to happen.
--
-- Three changes, each independent of the others:
--
-- 1. A dedicated `demo_reader` role that can read the two demo tables and
--    nothing else. `run_demo_query` now runs as that role (SECURITY DEFINER),
--    so a query naming `documents`, `chunks`, or any future table fails with
--    "permission denied" no matter how it is written. The role is NOLOGIN,
--    NOSUPERUSER, NOBYPASSRLS: it is a privilege set, not an account.
--
-- 2. An EXPLAIN-based allowlist inside the function. The planner resolves every
--    relation before the statement runs; the function rejects any plan that
--    mentions a relation outside the two demo tables or a schema other than
--    `public`. This closes catalog reads (`pg_roles`, `pg_stat_activity`), which
--    the role can otherwise reach because PostgreSQL grants PUBLIC read on
--    catalogs.
--
-- 3. SELECT policies for `demo_reader` on the demo tables. RLS stays enabled and
--    the role is not the table owner, so without a policy it would correctly
--    see zero rows.
--
-- The keyword blocklist, the 200-row cap and the 5s statement timeout are
-- unchanged. Safe to re-run.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. The role: read access to the demo tables, and nothing else
-- -----------------------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'demo_reader') then
        create role demo_reader
            nologin
            nosuperuser
            nocreatedb
            nocreaterole
            noinherit
            noreplication
            nobypassrls;
    end if;
end;
$$;

revoke all on schema public from demo_reader;
grant usage on schema public to demo_reader;

revoke all on public.demo_products from demo_reader;
revoke all on public.demo_sales_orders from demo_reader;
grant select on public.demo_products to demo_reader;
grant select on public.demo_sales_orders to demo_reader;

drop policy if exists demo_products_read_demo on public.demo_products;
create policy demo_products_read_demo on public.demo_products
    for select to demo_reader using (true);

drop policy if exists demo_sales_orders_read_demo on public.demo_sales_orders;
create policy demo_sales_orders_read_demo on public.demo_sales_orders
    for select to demo_reader using (true);


-- -----------------------------------------------------------------------------
-- 2. The function: validate, plan-check, then run
-- -----------------------------------------------------------------------------

-- EXPLAIN is not allowed inside a STABLE function ("EXPLAIN is not allowed in a
-- non-volatile function"), so planning needs its own VOLATILE helper. The
-- executor below stays STABLE, which is what makes Postgres refuse
-- data-modifying statements in the statement it finally runs.
--
-- This helper is owned by demo_reader and executable only by its owner: the
-- revokes at the bottom remove the default PUBLIC and service_role grants, and
-- no other role is granted it. It only explains, never executes.
create or replace function public.explain_demo_query(statement text)
returns json
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
set statement_timeout = '5s'
as $$
declare
    plan json;
begin
    execute 'explain (format json, verbose) ' || statement into plan;
    return plan;
end;
$$;

comment on function public.explain_demo_query(text) is
    'Phase 7: plans one statement as demo_reader. VOLATILE only because EXPLAIN '
    'is forbidden in a STABLE function; called by run_demo_query, not by '
    'application roles.';

create or replace function public.run_demo_query(
    sql text,
    max_rows integer default 100
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
set statement_timeout = '5s'
as $$
declare
    statement text := btrim(sql);
    row_cap integer := least(greatest(coalesce(max_rows, 100), 1), 200);
    plan_json json;
    relation_name text;
    relation_schema text;
    result jsonb;
begin
    if statement = '' then
        raise exception 'The generated query was empty.' using errcode = 'P0001';
    end if;

    if position(';' in statement) > 0 then
        raise exception 'Only a single statement is allowed; remove the semicolon.'
            using errcode = 'P0001';
    end if;

    if statement !~* '^(select|with)\y' then
        raise exception 'Only SELECT statements are allowed (WITH ... SELECT is accepted).'
            using errcode = 'P0001';
    end if;

    if statement ~* '\y(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|merge|execute|do|vacuum|reindex|refresh|reassign|discard|comment|security|begin|commit|rollback|savepoint|set|reset|lock|listen|notify|prepare)\y' then
        raise exception 'The query contains a disallowed keyword.' using errcode = 'P0001';
    end if;

    -- Plan the statement without running it. The planner resolves every
    -- relation, including tables hidden behind a view or a CTE, so this check
    -- cannot be evaded by aliasing or by nesting. It also runs under the 5s
    -- statement timeout, so planning a pathological query is bounded too.
    begin
        plan_json := public.explain_demo_query(statement);
    exception
        when insufficient_privilege then
            raise exception 'That query reads a table outside the demo dataset. Query only demo_products and demo_sales_orders.'
                using errcode = 'P0001';
    end;

    for relation_schema, relation_name in
        select node ->> 'Schema', node ->> 'Relation Name'
        from jsonb_path_query(
            plan_json::jsonb,
            'strict $.** ? (@."Relation Name" != null)'
        ) as node
    loop
        if relation_name not in ('demo_products', 'demo_sales_orders') then
            raise exception 'Table % is not part of the demo dataset. Query only demo_products and demo_sales_orders.',
                relation_name using errcode = 'P0001';
        end if;
        if relation_schema is distinct from 'public' then
            raise exception 'Schema % is not available to the analytics tool. Use the public demo tables.',
                relation_schema using errcode = 'P0001';
        end if;
    end loop;

    execute format(
        'select coalesce(jsonb_agg(row_to_json(wrapped)), ''[]''::jsonb) '
        'from (select * from (%s) as candidate limit %s) as wrapped',
        statement,
        row_cap
    ) into result;

    return result;
end;
$$;

-- The function must run as the restricted role, not as the role that applied
-- this migration (on Supabase that is `postgres`).
--
-- PostgreSQL only lets a non-superuser give ownership to a role it can SET ROLE
-- to. The migration runner on the Supabase session pooler is `postgres` but not
-- a superuser, so grant it membership in the role it just created. The role is
-- NOLOGIN and holds only SELECT on the two demo tables; `postgres` owns those
-- tables already, so this adds no privilege. Without this, applying the file
-- fails with "must be able to SET ROLE demo_reader".
do $$
begin
    execute format('grant demo_reader to %I', current_user);
end;
$$;

-- Changing an object's owner also requires the new owner to hold CREATE on the
-- containing schema. Grant it for the ALTER only: ownership persists after the
-- revoke, and demo_reader has no business creating objects in `public`.
grant create on schema public to demo_reader;

alter function public.run_demo_query(text, integer) owner to demo_reader;
alter function public.explain_demo_query(text) owner to demo_reader;

revoke create on schema public from demo_reader;

comment on function public.run_demo_query(text, integer) is
    'DEMO/Phase 7: runs one validated read-only SELECT against the demo tables '
    'as the restricted demo_reader role. Rejects any plan that touches another '
    'relation or schema. Caps rows at 200 and cancels after 5s.';

revoke execute on function public.explain_demo_query(text) from public;
revoke execute on function public.explain_demo_query(text) from anon;
revoke execute on function public.explain_demo_query(text) from authenticated;
revoke execute on function public.explain_demo_query(text) from service_role;

revoke execute on function public.run_demo_query(text, integer) from public;
revoke execute on function public.run_demo_query(text, integer) from anon;
grant execute on function public.run_demo_query(text, integer) to authenticated;
grant execute on function public.run_demo_query(text, integer) to service_role;
