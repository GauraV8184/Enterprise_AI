-- =============================================================================
-- 0005 - demo sales dataset and the read-only analytics query function
-- =============================================================================
-- Phase 6 of the Enterprise AI Intelligence Platform.
--
-- The SQL analytics tool needs real tables to query. Rather than pointing it at
-- business data that does not exist yet, this migration creates two clearly
-- named demo tables and seeds them deterministically. The SQLite development
-- store seeds the identical 384 rows from `app/database/demo_data.py`, so the
-- same generated query behaves the same on both backends.
--
-- What runs where, and why it is bounded:
--
-- 1. demo_products / demo_sales_orders ARE DEMONSTRATION DATA, not business
--    records. Every authenticated caller may read them; nobody may write them,
--    because RLS has no write policies and anon has no grants at all. Swap in
--    your own tables (and policy) before this route is used for real data.
--
-- 2. run_demo_query() executes a caller-supplied SELECT. It is deliberately:
--      * SECURITY INVOKER - it runs with the caller's own privileges, so Row
--        Level Security applies inside it. It cannot read what the caller
--        could not read directly, and it cannot widen anyone's access.
--      * STABLE - PostgreSQL refuses to run a data-modifying statement inside
--        a non-volatile function, so the database itself enforces read-only
--        even if every keyword check above it were bypassed.
--      * capped - the result is wrapped in `LIMIT <max_rows>` (1..200).
--      * time-bounded - `statement_timeout = 5s` cancels a runaway query.
--    It also repeats the application's guard rules (single statement, SELECT
--    or WITH only, blocked keywords) so the database refuses what the Python
--    guard refuses, instead of trusting the caller to have checked.
--
-- 3. Full SQL hardening and audit logging remain Phase 7 work; this function is
--    deliberately the narrowest thing that makes the Phase 6 route honest.
--
-- Safe to re-run: every statement is idempotent.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. The demo tables
-- -----------------------------------------------------------------------------
create table if not exists public.demo_products (
    id         integer primary key,
    name       text    not null,
    category   text    not null,
    unit_price numeric(10, 2) not null check (unit_price > 0)
);

comment on table public.demo_products is
    'DEMO DATA: product catalogue behind the Phase 6 SQL analytics tool. '
    'Not real business records - replace before production use.';

create table if not exists public.demo_sales_orders (
    id         integer primary key,
    order_date text    not null,
    region     text    not null,
    product_id integer not null references public.demo_products (id),
    quantity   integer not null check (quantity > 0),
    revenue    numeric(12, 2) not null check (revenue >= 0)
);

comment on table public.demo_sales_orders is
    'DEMO DATA: 384 synthetic sales orders (4 products x 4 regions x 24 months '
    'from 2024-01). order_date is an ISO date string; group months with '
    'substr(order_date, 1, 7), which is valid on both Postgres and SQLite.';

create index if not exists idx_demo_sales_orders_month
    on public.demo_sales_orders (order_date);

alter table public.demo_products enable row level security;
alter table public.demo_sales_orders enable row level security;

revoke all on public.demo_products from anon;
revoke all on public.demo_products from authenticated;
revoke all on public.demo_sales_orders from anon;
revoke all on public.demo_sales_orders from authenticated;

grant select on public.demo_products to authenticated;
grant select on public.demo_sales_orders to authenticated;

drop policy if exists demo_products_read_all on public.demo_products;
create policy demo_products_read_all on public.demo_products
    for select to authenticated using (true);

drop policy if exists demo_sales_orders_read_all on public.demo_sales_orders;
create policy demo_sales_orders_read_all on public.demo_sales_orders
    for select to authenticated using (true);


-- -----------------------------------------------------------------------------
-- 2. The seed
-- -----------------------------------------------------------------------------
-- Deterministic, so re-running cannot duplicate or renumber anything. The
-- formula for quantity (1 + (region * 7 + product * 3 + month) % 20) is the
-- same one app/database/demo_data.py applies to the SQLite store.
insert into public.demo_products (id, name, category, unit_price)
values
    (1, 'Aurora Laptop', 'Hardware', 1299.00),
    (2, 'Pulse Headset', 'Hardware', 189.00),
    (3, 'Insight Suite', 'Software', 349.00),
    (4, 'Care Plan', 'Services', 99.00)
on conflict (id) do nothing;

insert into public.demo_sales_orders (id, order_date, region, product_id, quantity, revenue)
select
    row_number() over (order by months.m, regions.r, products.p)::integer,
    (date '2024-01-01' + make_interval(months => months.m))::date::text,
    regions.region,
    products.product_id,
    1 + (regions.r * 7 + products.p * 3 + months.m) % 20,
    round((1 + (regions.r * 7 + products.p * 3 + months.m) % 20) * products.unit_price, 2)
from
    generate_series(0, 23) as months(m),
    (values ('North', 0), ('South', 1), ('East', 2), ('West', 3)) as regions(region, r),
    (
        select
            id as product_id,
            unit_price,
            (row_number() over (order by id) - 1) as p
        from public.demo_products
    ) as products
on conflict (id) do nothing;


-- -----------------------------------------------------------------------------
-- 3. The read-only query function
-- -----------------------------------------------------------------------------
create or replace function public.run_demo_query(
    sql text,
    max_rows integer default 100
)
returns jsonb
language plpgsql
stable
security invoker
set search_path = public, pg_temp
set statement_timeout = '5s'
as $$
declare
    statement text := btrim(sql);
    row_cap integer := least(greatest(coalesce(max_rows, 100), 1), 200);
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

    execute format(
        'select coalesce(jsonb_agg(row_to_json(wrapped)), ''[]''::jsonb) '
        'from (select * from (%s) as candidate limit %s) as wrapped',
        statement,
        row_cap
    ) into result;

    return result;
end;
$$;

comment on function public.run_demo_query(text, integer) is
    'DEMO/Phase 6: runs one validated read-only SELECT against the demo tables '
    'with the caller''s own privileges. Caps rows at 200 and cancels after 5s.';

revoke execute on function public.run_demo_query(text, integer) from public;
revoke execute on function public.run_demo_query(text, integer) from anon;
grant execute on function public.run_demo_query(text, integer) to authenticated;
grant execute on function public.run_demo_query(text, integer) to service_role;
