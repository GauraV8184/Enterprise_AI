# Supabase setup (Phases 4–6)

Everything the application needs from Supabase lives in this folder: the database
schema, the vector index, the Row Level Security policies, the private storage
bucket for uploaded files, and the demo dataset behind the SQL analytics tool.

Five files, in order:

| File | Creates |
| --- | --- |
| `migrations/0001_documents_and_chunks.sql` | `documents`, `chunks` (+ pgvector, HNSW index, RLS policies, the similarity-search RPC) |
| `migrations/0002_storage_bucket.sql` | the private `documents` bucket and its access policies |
| `migrations/0003_tighten_privileges.sql` | revokes privileges that bypass RLS, drops a redundant index |
| `migrations/0004_search_ready_documents.sql` | replaces `match_document_chunks` so only `ready` documents rank, with a 0.1 similarity floor |
| `migrations/0005_demo_analytics.sql` | `demo_products`, `demo_sales_orders` (384 seeded rows) and the read-only `run_demo_query()` function |

0003 exists because 0001 was wrong in a way that only the live catalog revealed:
Supabase grants `ALL` on new `public` tables to `authenticated` by default, and
0001's `GRANT SELECT, INSERT, UPDATE, DELETE` *added* to that rather than replacing
it. The leftover `TRUNCATE` was the problem - Postgres does not apply row security
to `TRUNCATE`, so it would have let any signed-in user empty every tenant's rows.
PostgREST never exposes `TRUNCATE`, so it was not reachable through the API, but
the privilege should not have been there at all. `scripts/check_supabase.py` now
audits this (and the other RLS invariants) on every run.

0005 supports the Phase 6 SQL analytics tool. It creates two **clearly labelled
demonstration tables** - not business records - and a function that executes one
caller-supplied `SELECT` against them. The function is `SECURITY INVOKER` (so
RLS applies inside it), `STABLE` (so Postgres itself refuses writes inside it),
caps the result at 200 rows, cancels after 5 seconds, and repeats the
application's keyword rules so the database refuses what the Python guard
refuses. An `authenticated` caller may read the demo rows; nobody may write them;
`anon` cannot execute the function at all. Replace the tables and this function
before pointing the route at real data.

All files are **idempotent** — re-running them is safe, so a failure halfway
through can be fixed by simply running the file again.

---

## Applying them

### Option A — from this machine (recommended)

Gives a real Postgres error message if anything is wrong, and records what was
applied so later runs skip it.

1. In the Supabase dashboard: **Project → Connect → Session pooler → Connection
   string (URI)**. It looks like:

   ```
   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```

   Use the **pooler**, not the direct `db.<ref>.supabase.co` host: current
   projects make the direct host IPv6-only, which many networks cannot reach.

2. Add it to the repository-root `.env` (never committed):

   ```
   SUPABASE_DB_URL=postgresql://...
   ```

3. Run:

   ```powershell
   cd backend
   .\.venv\Scripts\python.exe scripts\apply_migrations.py
   .\.venv\Scripts\python.exe scripts\check_supabase.py
   ```

   `--status` lists what has been applied without changing anything.

### Option B — Supabase SQL editor

No password needs to leave the dashboard. Open **SQL Editor**, paste the whole of
`0001_documents_and_chunks.sql`, press **Run**, then do the same with the four
later files in order (`0002` … `0005`).

The trade-off is that there is no record of which files have been applied, so
`scripts/apply_migrations.py --status` will report nothing. Re-running the files
is still harmless.

---

## What the schema looks like

```
auth.users
    │  id (uuid)
    │
    ├──< documents ── owner_id, filename, content_type, extension, size_bytes,
    │                 status, error, page_count, chunk_count, created_at,
    │                 updated_at, indexed_at
    │       │
    │       └──< chunks ── document_id, owner_id, chunk_index, content,
    │                      page, section, embedding vector(768), created_at
    │
    └──< storage.objects ── key: <owner_id>/<document_id>/<filename>

demo_products        (demo data; read by any authenticated caller)
    │  id, name, category, unit_price
    │
    └──< demo_sales_orders ── order_date, region, product_id, quantity, revenue
```

`documents` and `chunks` are a faithful mirror of the SQLite development store
that Phase 3 built, so the same repository contract is satisfied by either. Two
columns exist only in Postgres: `updated_at` (maintained by a trigger) and the
`vector` type itself, which replaces the SQLite `BLOB` + `embedding_dim` pair.

---

## The security model, in one paragraph

The API authenticates a caller with Supabase Auth, then forwards that caller's
**own** access token to PostgREST rather than using the service-role key. Because
PostgREST runs each request as the `authenticated` role with the caller's JWT,
every policy below is evaluated by Postgres itself. A bug in the Python layer —
a missing `WHERE owner_id = ...`, an inverted comparison — cannot leak another
tenant's rows, because the database will not return them. The application-level
ownership filters are still there; they are now the second line of defence
instead of the only one.

The **service-role key bypasses all of this** and is used only by admin scripts.
It must never be sent to a browser or used to serve a user request.

### Policies created by 0001

| Table | Operation | Rule |
| --- | --- | --- |
| `documents` | SELECT / UPDATE / DELETE | `owner_id = auth.uid()` |
| `documents` | INSERT | `owner_id = auth.uid()` |
| `chunks` | SELECT / DELETE | `owner_id = auth.uid()` |
| `chunks` | INSERT / UPDATE | `owner_id = auth.uid()` **and** the parent document is also owned by the caller |

The extra clause on `chunks` matters: without it a caller could insert a chunk
they own that points at somebody else's document, and any query joining
`chunks → documents` would then surface a foreign filename.

`anon` (a request with no access token) is granted nothing at all on these
tables, so it is refused rather than merely shown an empty list.

### Policies created by 0002

`storage.objects` policies compare the **first path segment** of the object key
to `auth.uid()`. That is why the key layout is fixed as
`<owner_id>/<document_id>/<filename>` and why the owner segment is written by the
server from the verified token — a filename supplied by the client can only ever
affect the last segment. It is reduced to a bare basename by
`file_validation.normalize_filename`, then to `[A-Za-z0-9._-]` by
`storage_filename`, because Storage rejects characters such as `#` outright. The
display name in `documents.filename` is not changed.

### Policies created by 0005

| Table | Operation | Rule |
| --- | --- | --- |
| `demo_products` | SELECT | `true` for `authenticated` |
| `demo_sales_orders` | SELECT | `true` for `authenticated` |

These two tables are **demonstration data**, so every signed-in caller
deliberately reads the same rows; there is no owner column to scope by. There is
no write policy on either table, and `anon` has no grants at all. The
`run_demo_query()` function that executes generated SQL is `SECURITY INVOKER`
(it cannot read what RLS would hide) and `STABLE` (Postgres refuses
data-modifying statements inside a non-volatile function), caps rows at 200,
cancels after 5 seconds, and is executable by `authenticated` and
`service_role` only.

---

## How the application reaches the database

`DOCUMENT_STORE=auto` (the default) uses Postgres whenever `SUPABASE_URL` and
`SUPABASE_ANON_KEY` are set; otherwise it stays on the SQLite development store.

Every query goes through `app/database/postgrest.py`, which builds a fresh client
per request carrying that caller's `Authorization` header. The service-role key
is not reachable from the application at all: it is read only by
`scripts/apply_migrations.py` and by the live checks below.

Similarity search is a single RPC call, `match_document_chunks`, so ranking runs
next to the data and uses `chunks_embedding_hnsw_idx`. The Python side never
downloads embeddings to compare them.

### Verifying it

| Command | What it proves |
| --- | --- |
| `python scripts/check_supabase.py` | credentials work, the schema and policies exist, and the catalog does not over-grant privileges |
| `python scripts/check_rls.py` | two real users cannot reach each other's rows through the REST API directly |
| `python scripts/check_store.py` | the application's own store works end to end and stays tenant-scoped |
| `python scripts/check_storage.py` | the bucket accepts the caller's own token and refuses a second user's read, write and delete |
| `python scripts/check_analytics.py` | the demo dataset matches `app/database/demo_data.py` exactly, the row cap and 5s timeout hold, and `run_demo_query()` refuses writes, second statements and blocked keywords even when the application's guard is bypassed |

All five create throwaway users through the admin API. They delete the users, the
rows cascade, and the storage objects are removed explicitly: `storage.objects`
has no foreign key to `auth.users`, so deleting a user does **not** delete their
files. `check_rls.py` and `check_storage.py` list the owner's prefix afterwards
to prove the bucket is empty. `check_web_search.py` (Tavily) is the sixth check;
it exits 1 saying "not configured" when no key is set, because a check that
"passed" without ever searching would be a lie.

---

## Changing the embedding model

`chunks.embedding` is `vector(768)`, which matches `nomic-embed-text`. pgvector
requires the width to be fixed at the column level, so switching to a model with
a different output size needs a migration:

```sql
alter table public.chunks alter column embedding type vector(1536);
```

Existing vectors **cannot** be reinterpreted — they were produced by a different
model and must be regenerated. Re-upload the documents, or add a re-index job.
`public.embedding_dimensions()` reports the column's current width so the
application can fail with a clear "dimension mismatch" message rather than an
opaque Postgres cast error; `scripts/check_supabase.py` prints it too.
