# Architecture — Enterprise AI Intelligence Platform

This note explains how the backend is put together and why, focusing on what
exists after **Phase 1**. It is written to be read by someone learning the
stack, so it favors plain language over jargon.

## 1. High-level shape

The platform is a three-layer system:

1. **Frontend** — React + TypeScript (Vite). Not built yet (Phase 8). It will
   only ever talk to the backend over HTTP.
2. **Backend** — FastAPI (Python). This is where all logic, credentials, LLM
   calls, database access, and permission checks live.
3. **Data & services** — Supabase (PostgreSQL, pgvector, Auth, Storage) plus the
   LLM provider and a web-search provider. Added in Phases 2–4.

**Why the backend owns everything privileged:** the browser is untrusted. If the
Supabase service-role key or the LLM API key reached the frontend, anyone could
read or delete any user's data. Keeping secrets server-side and enforcing
permissions there (not in the UI) is the core security rule of this project.

## 2. Backend package layout

```
app/
  main.py        Creates the FastAPI app: middleware, error handlers, routers.
  core/
    config.py    Typed, validated settings (single source of truth).
    logging.py   JSON logging + per-request correlation id.
    errors.py    AppError base: stable code + client-safe message.
    vectors.py   Cosine similarity, used by the SQLite store.
  llm/
    factory.py   Provider -> chat model AND embedding model.
    service.py   Calls + response/error normalisation.
  rag/
    loaders.py   Per-format text extraction with provenance.
    chunking.py  Splitting that preserves page and section.
    ingest.py    Parse -> chunk -> embed -> store.
    retriever.py Search + grounded answering, written against the store contract.
  database/
    repositories.py        The store contract + the SQLite implementation.
    supabase_repository.py PostgREST-backed store; ranking runs in pgvector.
    postgrest.py           Per-caller HTTP transport: token, headers, error mapping.
    errors.py              Store failures with stable codes.
  security/
    file_validation.py  Extension, size and magic-byte checks.
    identity.py         Who is asking: Supabase JWT, or the development stub.
    supabase_auth.py    Local JWT verification against the project's JWKS.
  api/
    deps.py      Shared dependencies.
    health.py    GET / and GET /health.
    router.py    Aggregates feature routers under one prefix.
  schemas/       Pydantic models describing request/response shapes.
```

Feature packages (`agents/`, `graph/`, `tools/`) are created only in the phase
that implements them. We deliberately avoid shipping empty folders for every
imagined feature — empty scaffolding looks complete but tests nothing.

## 3. Request lifecycle

Every request passes through this path:

```
client
  -> CORSMiddleware            (only configured origins may call from a browser)
  -> request_context middleware:
        - read or generate X-Request-ID
        - store it in a ContextVar so all logs for this request share it
        - time the request and log one "request_completed" line
  -> route handler            (e.g. health.py)
  -> response                  (X-Request-ID header added)
```

If a handler raises, the middleware logs `request_failed` (with the id and
duration) and the global exception handler returns a JSON 500 that includes the
`request_id`, so a user-facing error can be matched to a log line.

### Why a `ContextVar` for the request id?
Under async concurrency many requests are in flight at once. A plain global
variable would let one request's id leak into another's logs. A `ContextVar` is
isolated per task, which is exactly the right scope. See
`app/core/logging.py::request_id_ctx`.

## 4. Configuration

`app/core/config.py` defines a `Settings` class using `pydantic-settings`:

- Field names map case-insensitively to environment variables
  (`app_name` ← `APP_NAME`).
- Values are validated at startup. An invalid `ENVIRONMENT` value fails the
  process immediately with a clear error rather than causing odd behavior later.
- `get_settings()` is cached, so the `.env` file is parsed once per process.

### The bug we hit and the fix (worth understanding)
Initially the health endpoints depended on `get_settings()` — the global,
cached settings built from the real environment. Tests, however, build an app
with a *test* `Settings` object. Because the endpoints ignored the app's
settings and read the global, the test saw `environment="development"` instead
of `"test"` and failed.

The fix: `create_app()` stores the settings on `app.state.settings`, and
handlers read them through the `SettingsDep` dependency
(`app/api/deps.py`). Now each app instance uses its own settings, and the tests
are exercising the same configuration the app was built with. This is also why
we prefer the `Annotated[..., Depends(...)]` style: it keeps dependencies out of
function-default values (which some lint rules flag) and is the current FastAPI
recommendation.

## 5. Logging

- Default format is **JSON, one object per line** (`LOG_JSON=true`), which log
  aggregators can index. Set `LOG_JSON=false` for readable local output.
- Each line carries `timestamp`, `level`, `logger`, `message`, and `request_id`.
- Anything passed via `logger.info("msg", extra={...})` is merged in as
  structured fields.
- `redact_secrets()` recursively blanks values whose key looks sensitive
  (`key`, `secret`, `token`, `password`, `authorization`, `cookie`). This is a
  safety net, **not** a license to log secrets — the real rule is don't.

## 6. Error handling

- Unhandled exceptions return HTTP 500 with `{"detail", "request_id"}` — never a
  raw traceback (which can leak internals).
- In production (`ENVIRONMENT=production`), `/docs`, `/redoc`, and
  `/openapi.json` are disabled.

## 7. What is intentionally NOT here yet

- No agents, tools, or LangGraph workflow — Phases 5–6.
- No download endpoint for original files, and no signed URLs. Uploads and deletes
  use Storage; reading an original back is only done internally for indexing.
- No janitor for an object left behind if a delete fails after its rows are gone.
- No OCR for scanned PDFs — they fail with a clear error instead.
- No background worker; indexing runs inline in the request.
- No rate limiting — Phase 7.

Anything not implemented is marked as such in the README roadmap rather than
mocked, so nothing pretends to work when it does not.

## 8. Testing approach

- `tests/conftest.py` builds a fresh app per test with explicit test settings
  and `_env_file=None`, so tests never depend on a developer's local `.env`.
- `httpx`-backed `TestClient` drives the ASGI app in-process (fast, no real
  network), asserting status codes, response bodies, headers, and OpenAPI
  availability.
- `test_config.py` covers settings parsing edge cases (comma-separated vs JSON
  CORS lists, invalid values, caching).

## 9. The LLM layer (Phase 2)

Phase 2 adds three modules under `app/llm/`, deliberately separated:

| Module | Responsibility | Why separate |
|---|---|---|
| `factory.py` | settings → chat model | The only file that knows OpenAI exists. Swapping providers is one branch here. |
| `prompts.py` | system prompts | Prompts are reviewable in isolation; the no-fabrication and untrusted-content rules live in one place. |
| `service.py` | call + normalise + translate errors | Routers stay thin, and every LLM call has the same error contract. |
| `schemas.py` | structured-output models | The same Pydantic model validates the model's reply *and* documents the API contract. |

### The model is built once

A `lifespan` context manager builds the client at startup and stores it on
`app.state.chat_model`. One HTTP connection pool, and configuration problems
appear in the startup logs. Routes read it through the `ChatModelDep`
dependency.

Note the honest limitation: **building a client does not prove the key works.**
Only a real request does. That is exactly why `scripts/check_llm.py` exists — it
makes real calls so you find out immediately rather than on a user's first
request.

### Structured output

`classify_intent` uses LangChain's `with_structured_output(TaskIntent)`, which
makes the model emit JSON matching the schema and validates it with Pydantic.
The success path is `TaskIntent`, never raw text. This is the routing input the
Phase 5 supervisor will consume, so it is real functionality rather than a demo.

### Error translation

Provider exceptions are mapped to typed `AppError` subclasses in
`service.map_exception`. Order matters and is easy to get wrong:
`AuthenticationError`, `RateLimitError` and `PermissionDeniedError` are all
`APIStatusError` subclasses, and `APITimeoutError` is an `APIConnectionError`
subclass — so specific checks must come first. There is a test pinning this.

Public messages never contain the provider's response body (it can echo request
content). Only the numeric status code is recorded.

## 10. Bugs found while building Phase 2

Both were real and are now covered by tests or guarded by code:

**1. `extra={"message": ...}` crashed the logger.** `message` is a built-in
`LogRecord` attribute, so Python's logging raised `KeyError` *inside* the
logging call. Because that happened in the exception handler, an intended 503
became an unhandled 500. Fixed with `safe_extra()`, which renames collisions
instead of losing data.

**2. Redaction was too blunt.** The original rule redacted any key *containing*
a sensitive word, so `total_tokens` would have been masked — destroying useful
data. Now matching is on word boundaries, so `total_tokens` stays readable while
`access_token` is redacted.

**3. A Phase 1 latent bug surfaced.** `CORS_ORIGINS=a,b` in `.env` broke startup
once a `.env` actually existed: `pydantic-settings` tries to JSON-decode list
fields *before* field validators run. Fixed with the documented `NoDecode`
annotation.

**4. Exhausted credits were reported as throttling.** Providers return HTTP 429
both when you are being throttled *and* when the account has no credit left.
The original mapping sent both to `llm_rate_limited`, whose advice is "retry
shortly" — useless when the balance is empty. `_classify_rate_limit` now
inspects the provider's error code and returns a distinct `llm_quota_exhausted`
(HTTP 402) pointing at billing.

**5. The first fix for #4 didn't work, and the test caught it.** The openai SDK
only populates `exc.code` / `exc.type` when the error came from a real HTTP
response, so classification silently fell through for constructed errors.
`_provider_error_fields` now falls back to reading the parsed body, which is
also more resilient to SDK differences.

**6. A wrapper error hid the real diagnosis.** The live structured-output test
failed with `RuntimeError("v2 stream finished without producing a message")` -
LangChain's streaming accumulator replacing a failed request. The real cause
(`Event loop is closed`) was buried in the exception chain. `map_exception` now
walks `__cause__`/`__context__` to recover the original provider error, so a
wrapped failure is still classified correctly.

**7. `langchain-openai` shares one global async HTTP client.** It is a
module-level client, so a fresh `ChatOpenAI` does *not* get its own connection
pool - the client binds to the first event loop that uses it. With
pytest-asyncio's default of one loop per test, the second live test failed.
Fixed by configuring a session-scoped event loop, which also mirrors production:
Uvicorn serves every request from a single loop.

## 11. Security posture so far

Established now:
- Secrets are git-ignored; only `.env.example` is committed.
- The API key is a `SecretStr`: it cannot leak through logs, `repr()`, or
  tracebacks.
- Structured logs redact suspicious field names.
- Errors expose stable codes, not provider internals.
- Prompts treat document/web/tool text as untrusted data, not instructions.
- Permissions are enforced outside the LLM (this becomes essential in Phases
  6–7 when tools are added).

Added in Phase 3:
- Uploads are validated three ways: extension allowlist, size cap, and
  magic-byte/structure sniffing. A `.pdf` that is not a PDF is rejected.
- The client filename **never** reaches the filesystem. Storage paths are
  server-generated UUIDs, so path traversal is removed as a category instead of
  filtered for. The filename is only a display string, with path components and
  control characters stripped.
- Every storage query is filtered by owner id **in SQL**. Tenancy is not a
  post-processing step that a new code path can forget.
- Cross-tenant access returns 404, not 403, so ids do not confirm existence.
- Third-party parser errors are sanitised: library messages embed the storage
  path they were given, so paths are replaced with the display filename before
  the text reaches a response or a stored status.

Not yet established: real authentication/authorization, RLS, SQL validation,
rate limiting, audit logging (Phases 4–7).

**The honest caveat on identity.** Phase 3 has no authentication. The acting user
comes from an `X-User-Id` header, which anyone can set — it is *not* a security
control. What it does buy is that the enforcement points are already correct and
already tested: Phase 4 replaces *how the identity is derived* and nothing else
moves. In `ENVIRONMENT=production` the module refuses to produce an identity at
all, so an accidentally exposed deployment fails closed rather than serving one
tenant's documents to another.

## 12. Testing approach

- Tests run the **real** application code against an in-process fake chat model.
  The fake implements LangChain's `BaseChatModel` interface, so only the network
  call is replaced. Nothing in the app knows it is talking to a fake.
- Provider exceptions in tests are constructed with the same library the SDK
  uses internally (`httpx2` for openai 3.x), so error mapping is tested against
  faithful inputs.
- Live tests are opt-in (`RUN_LIVE_LLM_TESTS=1`) and skipped by default.
- Verified APIs against the installed versions rather than assuming:
  `langchain-openai` 1.7.2 uses `model_name` / `openai_api_base` /
  `request_timeout`, not `model` / `base_url` / `timeout`.

Phase 3 additions:

- **Sample documents are generated, not committed.** Tests build real PDFs with
  `pymupdf`, real DOCX with `python-docx`, and real XLSX with pandas. A binary
  fixture is unreviewable in a pull request, and generating the file proves the
  parser handles genuine library output rather than a byte string that happens
  to parse.
- **The fake embedding is a real `Embeddings` implementation.** `HashingEmbeddings`
  hashes tokens into a 1024-dimension vector, so cosine similarity genuinely
  tracks shared vocabulary and ranking assertions mean something. Its own
  properties are tested, because if the double stopped being faithful every
  ranking test would keep passing while measuring nothing.
- **`test_search_returns_the_other_document_for_a_different_query` initially
  failed, and the failure was informative.** Two documents with the same words
  score identically, so the winner was decided by tie-break order rather than by
  similarity — the test was measuring nothing. Sample texts were made
  deliberately disjoint. The same investigation showed the first version of the
  fake embedding had too few dimensions: hash collisions let an unrelated
  document outscore a related one.
- The suite is run repeatedly (~6×) after ranking changes, because a
  nondeterministic ordering bug passes most of the time.

## 13. The document pipeline (Phase 3)

```
  upload ─► validate ─► parse ─► chunk ─► embed ─► store ─► (status: ready)
             │            │        │        │        │
        extension,    per-type   page +   batched   one
        size, magic   parser     section   call     transaction
        bytes                    kept
```

### Parsing is provenance-first

Every parser returns `TextBlock`s carrying a page number (where the format has
pages) and a section heading. Chunking splits *per block*, so a chunk never spans
two pages and its citation is exact. Splitting the whole document as one string
would produce better-boundary chunks and approximate citations — the wrong
trade for a system whose value is verifiable sourcing.

Two format-specific decisions:

- **DOCX tables are extracted in document order.** `document.paragraphs` and
  `document.tables` are separate collections; reading them separately silently
  moves every table to the end. The XML body is walked instead.
- **CSV/XLSX blocks repeat the header row.** A chunk describing row 4,000 would
  otherwise be a list of values with no column names, matching almost nothing.
- **Nested headings keep their ancestry** (`Operations Guide > Incident
  response`), because "Overview" alone is ambiguous and two documents can both
  have one.

### The store

Development uses SQLite with vectors as raw float32 blobs and NumPy cosine
similarity. This is honest but limited: similarity is a linear scan, so cost
grows with total chunks. It is fine for thousands, not millions — pgvector exists
to move that work into an index, and the repository methods define exactly the
contract Phase 4 implements.

Two deliberate choices:

- **Connections are opened per operation.** FastAPI runs `def` endpoints in a
  worker thread, and SQLite connections are thread-bound. Short-lived connections
  remove that hazard entirely.
- **Deletion removes chunks explicitly** rather than relying on
  `ON DELETE CASCADE`. `PRAGMA foreign_keys` is connection-scoped; if it were
  ever off, a cascade would silently leave orphaned vectors that keep surfacing
  in search results.

### Why upload is synchronous

Indexing runs inline, so a `201` means "already searchable". Large files therefore
mean slow requests. A background worker is the natural next step, and it needs no
API change because `status` (`pending → processing → ready|failed`) already models
the transition.

A document that fails to index is **kept** with `status="failed"` and the reason,
and its id travels with the error response. A silent disappearance is worse than a
visible error, and partial vectors are deleted so a half-indexed document cannot
answer from an incomplete corpus.

### Grounded answering refuses to guess

When retrieval returns nothing, **the chat model is not called**. The response has
`grounded: false` and states that no relevant content exists. Asking a model to
answer from an empty context is an invitation to hallucinate, and the honest
answer is already known.

Citations and context are built together: `format_context` returns the text *and*
the matches that fit inside the character budget, so the model is never shown a
label the response omits, and never cites something the client cannot resolve.
`POST /api/documents/search` exposes retrieval on its own — when an answer is
wrong, the first question is always "were the right chunks retrieved?".

### Changing the embedding model

Stored vectors and query vectors must come from the same model. If
`EMBEDDING_MODEL` changes, the API returns `409 embedding_dimension_mismatch`
naming both dimensions instead of comparing 768-dimensional vectors with
1536-dimensional ones. That comparison is meaningless, and a silently wrong
search result is worse than a clear error.

## 14. Supabase: identity, isolation and the store (Phase 4)

Three separate mechanisms answer three different questions, and keeping them
apart is what makes the security argument checkable:

| Question | Answered by | Where |
| --- | --- | --- |
| Who is this caller? | Signature check against the project JWKS | `security/supabase_auth.py` |
| Which rows may they see? | Row Level Security | `supabase/migrations/0001_*.sql` |
| Which rows did they ask for? | `owner_id` filters | `database/supabase_repository.py` |

The third is a convenience and a second line of defence. It is the *second* one:
if the application's filter and the policy ever disagreed, Postgres would win,
and the result would be fewer rows rather than someone else's.

### The repository is per-request, and that is load-bearing

The SQLite store is a single object on `app.state`. It scopes every query by
`owner_id`, so sharing it is safe.

The Supabase store cannot be shared, because every query carries the caller's
access token. `supabase-py` keeps the session on the client instance, so a
process-wide client calling `.auth(token)` per request would let concurrent
requests act as each other: A sets the token, B overwrites it, A's query runs as
B. Instead `PostgrestClient` takes the token as an immutable constructor
argument, and `get_repository` builds one per request around a shared
connection pool.

`get_repository` depends on `CurrentUserDep` even though it does not use the
result. That is what makes identity resolve - and be verified - *before* a query
is built.

### Ranking moved out of the retriever

Phase 3's `search()` loaded every chunk and compared it with NumPy. Over HTTP
that would mean downloading the whole corpus for every question, which is
precisely what pgvector exists to avoid.

So the store contract grew `search_chunks(query_embedding, ...)` and dropped
`fetch_chunks` from what a store must provide. The retriever embeds the query and
hands the vector to the store; SQLite compares in NumPy, Postgres runs one
`<=>` query against the HNSW index. `searched_chunks` reports the size of the
corpus in scope, not how many vectors the index touched - the index is
approximate, and claiming otherwise would imply an exactness search does not have.

### `auto` is convenient, and one combination is refused

`DOCUMENT_STORE=auto` picks Postgres when credentials are present, and
`AUTH_BACKEND=auto` does the same for identity. They have to agree: a Postgres
store with the development header stub would have no token to forward, and
"fixing" that by falling back to the anon key would return empty results while
looking like an empty account. Startup rejects the combination instead.

### Two bugs that reading the code would not have found

**`TRUNCATE` survived the migration.** Supabase grants `ALL` on new `public`
tables to `authenticated`. The migration revoked it from `anon` but only *added*
the four DML grants for `authenticated`, and `GRANT` does not remove what is
already there. Postgres does not apply row security to `TRUNCATE`, so any signed-in
user could have emptied every tenant's documents with every policy still in place.
PostgREST never exposes `TRUNCATE`, so it was unreachable through the API - luck,
not design. Fixed in `0003_tighten_privileges.sql`; `scripts/check_supabase.py`
now audits it on every run.

**The REST base URL was doubled.** `PostgrestClient` appended `/rest/v1` to a
base that already ended in `/rest/v1`, producing `/rest/v1/rest/v1/documents`
and a bare 404. Mock-transport tests matched on the path *suffix*, so they
passed; only the live check failed. The wiring test now asserts the full URL.

That is the general lesson: a mock proves the code does what you told it to. It
does not prove the server agrees. `scripts/check_store.py` drives the real store
with two signed-in users for exactly that reason.

---

## 15. Storage: bytes, keys and the second boundary (Phase 4)

Row Level Security protected the rows. Nothing protected the files. An upload
created a database row with an `owner_id`, then wrote the bytes to
`data/uploads/<document_id>.pdf` - a path with no owner in it and no policy
behind it. The only thing stopping one tenant reading another's PDF was that
every code path remembered to check `owner_id` first.

That is the same class of problem RLS was introduced to remove, one layer down.
The fix is not "be careful"; it is to put the bytes somewhere that enforces the
same rule.

### One contract, two backends

`app/storage/base.py` defines `DocumentFileStore` with three operations:
`put`, `read`, `delete`. The interface is **byte-oriented**, deliberately. The
Supabase backend has no filesystem path to hand out, and making it pretend to
have one would turn the local backend into the only real implementation.

- `LocalFileStore` writes under `data/uploads/` for SQLite development and the
  offline test suite.
- `SupabaseStorage` speaks the Storage REST API with a per-request client.

The router depends on the protocol. It does not import either class.

### The key is doing security work

Objects are stored at:

```
<owner_id>/<document_id>/<filename>
```

The policies in `0002_storage_bucket.sql` compare the first segment to
`auth.uid()`:

```sql
(storage.foldername(name))[1] = (select auth.uid())::text
```

There is no join back to `documents`, so the check cannot drift out of sync with
the rows, and it is one string comparison. The owner segment is written by the
server from the verified token; a client-supplied filename can only affect the
last segment.

The filename is then reduced to `[A-Za-z0-9._-]` by `storage_filename()`. That is
not cosmetic: Supabase Storage rejects a key containing `#` with
`400 Invalid key`. The database keeps the original display name, so
`q3 report #1.txt` is still shown to the user while the key becomes
`q3-report-1.txt`. The function is deterministic, which is what lets delete find
the same object without storing a second column.

### The caller's token, again

`SupabaseStorage` is built per request with the caller's JWT as an immutable
constructor argument. The reasoning is identical to `PostgrestClient`, and so is
the failure it avoids: a shared client with a mutable `.auth(token)` lets
concurrent requests act as each other.

Using the service-role key here would have been simpler and would have made every
policy in `0002` dead code. The bucket would still look private, but ownership
would then rest entirely on this application's checks - exactly the single point
of failure the move was meant to remove.

### Read back before indexing

After `put`, the upload path calls `read` and indexes *those* bytes rather than
the request buffer it still holds. It costs one download (bounded by the 25 MB
upload limit), and buys two things:

1. The write is proven to have landed before any vectors are created.
2. The caller's **select** policy is exercised on every upload. A correct insert
   policy with a broken select policy would otherwise stay invisible until a
   later download.

It is also exactly what a background indexer will have to do, so moving ingestion
off the request thread later does not change the storage code.

### Failure semantics

If the object cannot be written, the document row is already there. It is marked
`failed` with the storage error and returned with its id, so it is visible and
deletable instead of sitting at `pending` forever. A failure to record that status
is logged, not allowed to mask the original error.

Deletion goes the other way: rows and vectors first, then the object. If the
object delete fails, the request still succeeds - the searchable data is gone,
which is what the caller asked for - and the key is deterministic, so an orphan
can be found and removed later. The reverse order would leave a listed document
whose original had already vanished.

### Four things only the live checks found

**`#` is not a legal key character.** The first version percent-encoded the key
correctly and the server still answered `400 Invalid key`, because it decoded the
path and then validated the raw character. Sanitising the filename fixed it.

**Storage reports policy violations as `400`, not `403`.** A foreign insert comes
back as `new row violates row-level security policy`; a foreign delete as
`Access denied`. Both were initially mapped to a generic 500, which would have
turned a correct refusal into an apparent outage. They now map to
`AccessDeniedError`.

**A DELETE must not declare a JSON body.** Cleanup in `check_rls.py` reused a
header builder that always set `Content-Type: application/json`; Fastify rejected
the bodyless request with `Body cannot be empty when content-type is set to
'application/json'`. The application's client never set that header, which is why
only the test harness failed.

**Deleting a user does not delete their objects.** `storage.objects` has no
foreign key to `auth.users`, so `on delete cascade` does not reach the bucket.
The live checks now delete each key explicitly and then list the owner's prefix to
prove the bucket is empty.

### What is not here yet

There is no download endpoint, so no signed URLs either; the API still never
serves the original file. There is no janitor for orphaned objects. Indexing is
still synchronous, now with one extra network round trip.
