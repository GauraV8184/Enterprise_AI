# Architecture — Enterprise AI Intelligence Platform

This note explains how the system is put together and why, focusing on what
exists after **Phase 8**. It is written to be read by someone learning the
stack, so it favors plain language over jargon.

## 1. High-level shape

The platform is a three-layer system:

1. **Frontend** — React 19 + TypeScript (Vite), built in Phase 8. It only ever
   talks to the backend over HTTP and receives only the public Supabase anon key.
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
  graph/
    state.py     The state every workflow node reads and updates.
    workflow.py  The supervisor: classify -> route -> answer.
  agents/
    analyst.py   SQL generation, execution, one repair, then a summary.
    research.py  Query planning, deduplication, cited web answers.
    report.py    Evidence gathering + Markdown report writing.
  tools/
    analytics.py Demo schema + the boundary around generated SQL.
    web_search.py Tavily client (plain httpx).
  database/
    repositories.py        The store contract + the SQLite implementation.
    supabase_repository.py PostgREST-backed store; ranking runs in pgvector.
    postgrest.py           Per-caller HTTP transport: token, headers, error mapping.
    sql_guard.py           Read-only SQL pre-flight rules + table allowlist.
    demo_data.py           The demo sales dataset (same seed for both stores).
    errors.py              Store failures with stable codes.
  security/
    file_validation.py  Extension, size and magic-byte checks.
    identity.py         Who is asking: Supabase JWT, or the development stub.
    supabase_auth.py    Local JWT verification against the project's JWKS.
    rate_limit.py       Per-identity sliding-window request budgets.
    audit.py            Best-effort, append-only audit writes.
    prompt_injection.py Detector + neutralisation of the app's block tags.
  api/
    deps.py      Shared dependencies.
    health.py    GET / and GET /health.
    router.py    Aggregates feature routers under one prefix.
  schemas/       Pydantic models describing request/response shapes.
```

Feature packages are created only in the phase that implements them: `graph/`
arrived with Phase 5, while `agents/` and `tools/` wait for Phase 6. We
deliberately avoid shipping empty folders for every imagined feature — empty
scaffolding looks complete but tests nothing.

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

- All five supervisor routes exist now (§16–§17), each backed by real code; a
  route whose tool is not configured fails with a typed 503 rather than
  degrading. Phase 7 adds the hardening around them — per-caller rate limits, an
  append-only audit trail, delimiter neutralisation in prompts, and a
  two-table role behind the analytics function (§18).
- No download endpoint for original files, and no signed URLs. Uploads and deletes
  use Storage; reading an original back is only done internally for indexing.
- No janitor for the rare partial delete: if the object is removed and the row
  delete then fails, the document remains listed without its original. A retry
  converges; nothing repairs it in the background.
- No OCR for scanned PDFs — they fail with a clear error instead.
- No background worker; indexing runs inline in the request.
- No shared rate-limit counter: the limiter is per process (§18). Phase 9 adds
  Redis.

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
supervisor (§16) consumes, so it is real functionality rather than a demo.

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

Added in Phase 4:
- Real authentication: Supabase access tokens are verified locally against the
  project JWKS; HS256 and `alg: none` are rejected. Every `/api/*` route requires
  an identity, including the LLM-only chat endpoints.
- Row Level Security is the boundary, and every Supabase query carries the
  caller's token. The service-role key never serves user traffic.
- The `documents` bucket is private, and its policies compare the first key
  segment to `auth.uid()`. Uploads and deletes run as the caller.
- The development `X-User-Id` stub is accepted only in `development` and `test`;
  staging and production refuse to start without Supabase Auth.

Added in Phase 7:
- Per-identity rate limits for standard and model-calling routes, with a 429 and
  `Retry-After` before the handler runs.
- An append-only audit trail whose actor is derived from the token, readable only
  by its owner, with no update/delete path for application roles.
- Prompt-injection hardening: untrusted block tags are escaped, and known
  patterns are logged without blocking.
- Generated SQL is restricted to the two demo tables by a `SECURITY DEFINER`
  function owned by a role with no other grants, plus an EXPLAIN plan check. The
  SQLite store enforces the same allowlist with an authorizer.
- Defensive response headers; production HSTS and host validation.

**The limits are part of the posture.** Rate-limit counters are per process, the
injection detector is a signal rather than a sandbox, and the analytics function
is only as strong as the `demo_reader` role's grants (which migration `0006`
keeps to two tables). These are stated in the README rather than implied away.

**The identity caveat, and how Phase 4 closed it.** Phase 3 shipped without
authentication: the acting user came from an `X-User-Id` header, which anyone can
set — it was *not* a security control. What it bought was that the enforcement
points were already correct and already tested, so Phase 4 only had to replace
*how the identity is derived*; nothing else moved. Outside `development` and
`test` - that is, in staging and production - the module now refuses to produce
an identity at all, so an accidentally exposed deployment fails closed rather
than serving one tenant's documents to another.

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

Every `/api/*` route resolves `CurrentUserDep`, including the chat endpoints that
read no tenant data. Leaving those anonymous would let anyone spend the
deployment's provider quota; `/health` is the anonymous liveness probe. The
development header stub is accepted only in `development` and `test`, and startup
refuses to serve a deployed environment without Supabase Auth.

Verification is local, so a cold cache costs one JWKS request. An unknown `kid` is
unauthenticated input, so the cache refetches at most once per 30-second cooldown
rather than once per request, and the fetch happens outside the state lock so
cached keys keep verifying while a refresh is in flight.

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

Deletion reverses the write order: the object is removed first, then the rows and
vectors. If Storage is unavailable, the request fails and nothing has been deleted,
so the caller can retry. Deleting the rows first would instead leave bytes that no
row points at and that nothing would ever retry - a privacy leak with no owner. If
the row delete fails after the object is gone, the document stays listed with its
indexed chunks, and retrying the delete converges because deleting a missing
object is a no-op.

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

## 16. The supervisor workflow (Phase 5, extended in Phase 6)

Phase 5 adds `app/graph/`, exposed as `POST /api/workflow/run`. It is a LangGraph
`StateGraph` with one decision node and five answer nodes:

```
        START
          |
      classify               reads the request, emits the structured TaskIntent
          |
   route_for_intent          pure function of the intent
      /    |    |    \    \
answer_ answer_ answer_ answer_ write_
directly from_  from_   from_   report
(chat)   docu-  research data    (Markdown from the requested sources)
         ments  ([W1],…) (SQL)
      \    |    |    /    /
        END
```

### The routing rule is a pure function

`route_for_intent(intent)` checks, in order: `report` wins outright (it is the
only route that gathers from more than one source); then a document question or
the internal-documents flag; then research; then analytics; everything else is
`chat`. Because the rule is pure, it is tested directly without a model, and the
conditional edge reads the route the classify node already recorded - the
decision is made once, stored in the state, and never recomputed.

A route only exists with its tool. Each node below depends on real, tested code,
and a missing dependency fails loudly (the research route answers 503 without
`TAVILY_API_KEY`) instead of degrading into a guess.

### State

`WorkflowState` is a `TypedDict` with `total=False`, because LangGraph merges
each node's return value into the running state. Inputs (`question`, `history`,
`owner_id`, `document_ids`), the decision (`intent`, `route`), and the output
(`answer`, `grounded`, `sources`, `searched_chunks`, `web_sources`,
`web_queries`, `analytics`, `usage`) each have exactly one writer, so no reducer
is needed for anything.

### The graph is compiled per request

The Supabase store carries the caller's access token, so the repository is
caller-scoped and the graph is bound to it per request. Compiling is in-memory
work only; no I/O happens until a node runs. The alternative - a long-lived
compiled graph - would have to smuggle a repository through configuration on
every call, which is exactly the coupling the store contract exists to avoid.

### Honesty rules carry over unchanged

- A classification or provider failure raises the same typed `AppError` the
  rest of the app uses. There is no fallback that guesses a route.
- The documents route reuses `answer_question`, including its empty-corpus
  refusal: when nothing is retrieved the model is not called and `grounded` is
  `false`.
- `grounded` is `null` on the chat route rather than a misleading `false`,
  because grounding does not apply there. It is `true`/`false` on the four
  evidence routes, and `false` always means no model was asked to answer.
- The Phase 6 routes extend the same rules (see §17): no search key is a 503,
  no search results means no synthesis call, and a report with no evidence is a
  short Markdown body that says so.

### Testing it

The graph tests drive the fake chat model with a controlled queue of structured
results (classify, then SQL generation, then search planning - the order the app
would make them), so all five routes are exercised deterministically alongside
the real SQLite store, the real demo dataset, and real ranking.
`scripts/check_workflow.py` proves every configured route against the live
provider: a greeting must route to chat; a question over a throwaway document
must route to documents and cite `[1]`; a data question must route to analytics
and return rows; a report request must come back as grounded Markdown; and -
when a search key is present - a research question must cite `[W1]`. The
research leg is skipped honestly, not faked, when no key is configured.

## 17. Tools and specialist agents (Phase 6)

Phase 6 adds `app/tools/` (SQL analytics, web search), `app/agents/` (analyst,
research, report), the three new supervisor routes, and `POST /api/reports`.

### Three agents, three responsibilities

| Module | Does | Deliberately does not |
|---|---|---|
| `analyst.py` | generates one SQL query, runs it, repairs it once, summarises the rows | show raw database errors to the caller - only the repair hint sees them |
| `research.py` | plans queries, runs them, deduplicates results, cites `[W1]`… | invent a source, or call the model when a search returned nothing |
| `report.py` | gathers the evidence it was asked for, writes Markdown with working labels | widen the source list, or write from general knowledge |

Each takes its dependencies (store, model, search provider) as arguments and
returns a plain dataclass, so the agents are testable without FastAPI and
without a network.

### The demo dataset, seeded twice

`demo_products` and `demo_sales_orders` (4 × 4 × 24 = 384 orders) are
deliberately **demonstration data**, marked as such in table comments, the
schema description handed to the model, and the README. Migration `0005` seeds
them in Postgres with `INSERT ... SELECT`; the SQLite store seeds the identical
rows from `app/database/demo_data.py`. The two seeds must agree, so the offline
store tests and the live `check_analytics.py` both compute expected values from
the Python generator and compare them against what the database actually
returned - a drifting seed fails a test instead of silently changing answers.

`order_date` is an ISO `YYYY-MM-DD` **string** on purpose: year and month
grouping become `substr(order_date, 1, 4)` / `substr(order_date, 1, 7)`, which
is valid in both dialects, so generated SQL does not need dialect-specific date
functions the small model would get wrong.

### SQL execution is bounded in three layers

Executing model-generated SQL is the most dangerous thing this platform does, so
the bounds are independent, and each fails closed:

1. **`app/database/sql_guard.py`** - a string-level pre-flight: exactly one
   statement, must start with `SELECT`/`WITH`, a keyword blocklist (including
   `do`, `set`, `lock`, `begin`…), no semicolon anywhere (comments included), a
   4000-character limit, and a table allowlist (`ALLOWED_TABLES`). It is
   conservative and says which rule it broke, because that message is fed back to
   the model as repair feedback. Because SQLite has no roles or RLS, its store
   also installs a connection authorizer: reads of any table outside the two
   demo tables are refused at prepare time, including through aliases and CTEs.
2. **`run_demo_query()` (migrations `0005` + `0006`)** - the guard repeated in
   plpgsql, an outer `LIMIT` capped at 200 (default 100, `ANALYTICS_MAX_ROWS`),
   `statement_timeout = 5s`, and `STABLE`, so PostgreSQL itself refuses
   data-modifying statements. Migration `0006` makes it `SECURITY DEFINER`,
   owned by the non-login `demo_reader` role, and adds the decisive control: it
   runs `EXPLAIN (FORMAT JSON, VERBOSE)`, walks every `"Relation Name"` node in
   the plan, and refuses any relation that is not `public.demo_products` or
   `public.demo_sales_orders` before the query executes.
3. **Role grants and RLS** - `demo_reader` has `SELECT` on exactly those two
   tables, no grant on `documents` or `chunks`, no memberships that would lend
   it more, and read policies for the demo tables. `anon` has no `EXECUTE` on the
   function at all. A caller must still authenticate before PostgREST will run
   it, but the SQL can no longer read with the caller's full privileges.

PostgREST error mapping makes the layers distinguishable, and it keys on the
Postgres SQLSTATE rather than the HTTP status: `P0001` from the function becomes
`AnalyticsQueryRejectedError` (safe to show), while any other non-`PGRST`
code - including `42P01`, which PostgREST returns as 404, and `57014` for the
statement timeout - becomes `AnalyticsQueryFailedError`, whose `repair_hint`
holds the database's complaint for the repair loop and never reaches the client.

### The repair loop stops at one retry

`MAX_QUERY_ATTEMPTS = 2`: one generation plus one repair. Only *rejected* or
*failed* queries are retried, with the last error's `repair_hint or message` as
feedback; anything else (auth, network, provider) propagates immediately rather
than being retried pointlessly. If the second attempt also fails, the last error
is raised - the agent never returns a "best effort" answer built on a query it
could not run.

### Web search is plain `httpx`

Tavily is one `POST` with a JSON body and a JSON reply, so `app/tools/web_search.py`
talks to it directly instead of adding an SDK dependency. The provider is built
once in the lifespan and exposed as `WebSearchDep`, which returns `None` rather
than raising when no key is configured - chat, documents and analytics must keep
working without one. The raised 503 belongs to the moment a search is actually
attempted. Results without a URL are dropped; snippets are whitespace-collapsed
and truncated; `max_results` is capped at 10.

### Structured calls do not report usage

`structured_completion` shares the validation path with `classify_intent`, and
like it does not report token usage - so `usage` in the API responses is the
answering call only. The field description says so, in the schema, where a
caller will read it.

### Reports: evidence in, evidence out

`POST /api/reports` gathers exactly the requested sources (`documents`,
`analytics`, `web`; default `["documents"]`, deduplicated, never widened) and
returns the Markdown **and** the evidence it was written from: document
citations labelled `[1]`, web results labelled `W1`, and the SQL plus rows for
analytics. Labels are strings for web results precisely so a mixed report can
never render `[1]` and `W1` ambiguously.

Requested sources that return nothing are recorded as *gaps* and named in the
prompt ("Requested sources that returned no evidence: …"), so the report says
what it could not cover instead of padding. When **no** requested source
returns evidence, the response is built by code - a short Markdown body stating
that no evidence was found, listing the gaps - and the model is not called at
all. `grounded` is `false` in that case, mirroring RAG's empty-corpus rule.

### What the live checks proved

- `scripts/check_analytics.py` (22 checks, live Supabase): the SQL-seeded rows
  match `demo_data.py` to the cent (total revenue 1,891,260.00 across 384
  orders), a second user reads the same rows, the outer cap holds against an
  inner `limit 1000`, the database itself refuses an `INSERT`, a second
  statement, a `DROP`, and a blocked keyword inside a string literal, an unknown
  column comes back as a repair hint, `pg_sleep(15)` is cancelled after ~5.2s,
  and `anon` is refused with 401. The throwaway users are deleted afterwards.
- `scripts/check_web_search.py` exits 1 with "not configured" on this machine,
  because there is no `TAVILY_API_KEY` here. That is reported as what it is -
  an unverified leg - not as a passing check.
- `scripts/check_workflow.py`: the first live run **failed one check honestly** -
  the local 8B model classified a "which region had the most revenue" question
  as a `report`, which is what the documented priority says must happen (report
  wins outright), so the check's question was reworded to an unambiguous count
  and the classifier probe confirmed it routes to `analytics`. A later run also
  hit the 60s `LLM_TIMEOUT_SECONDS` on the report leg (local-model variance,
  the earlier run had completed); the final run used
  `LLM_TIMEOUT_SECONDS=240` and all 15 checks passed. The research leg skipped
  itself with an explanation. None of this was hidden or silently retried into
  a green result.

## 18. Security hardening (Phase 7)

Phase 7 adds `app/security/rate_limit.py`, `audit.py` and
`prompt_injection.py`, migrations `0006` and `0007`, and the middleware that
makes the production posture explicit. The design rule is the same as in Phase
4: enforcement lives in code and in the database, not in a prompt and not in a
setting an operator can forget.

### Request budgets: per identity, honestly per process

A single token should not be able to turn one account into an unbounded bill or
queue. Two sliding 60-second windows are counted per identity:

- **standard** (default 240/min) for reads, metadata and deletes;
- **model** (default 30/min) for endpoints that call an LLM or an embedding
  model: chat, intent, upload, search, ask, workflow and reports.

A sliding window is used rather than a fixed minute so a caller cannot spend the
whole budget at 12:00:59 and again at 12:01:00. The check is a dependency, so it
runs before the handler: a rejected request costs no model call and writes no
row. The error is `429 rate_limited` with `Retry-After` set from the oldest hit
in the window, and `AppError` grew an optional `headers` field rather than
special-casing 429 in the handler.

The caveat is stated in the code, the README and `.env.example`: the counters
are in the serving process. N workers multiply the effective budget, and a
restart forgets the window. That is acceptable for the current single-instance
deployment, and it is why Phase 9 adds a shared counter. Claiming a global limit
here would be false.

### Audit: the actor is not a parameter

`audit_events` (migration `0007`) records uploads, deletes, document questions,
workflow runs and generated reports. The important choices:

- **One write path.** `record_audit_event()` is a `SECURITY DEFINER` function
  that takes the action and metadata, and derives `actor_id` from `auth.uid()`.
  The RPC has no actor parameter, and the Supabase repository never sends one.
  A direct RPC caller cannot choose whose trail they write to; an unknown extra
  argument does not match the function signature at all.
- **Append-only.** `authenticated` has `SELECT` and `EXECUTE` only. There is no
  `INSERT` grant (the function is the only writer), and no `UPDATE` or `DELETE`
  grant or policy. The service role keeps `DELETE` solely so a retention job can
  remove old events; it is not used by the application.
- **Read your own.** The `SELECT` policy compares `actor_id` to `auth.uid()`, so
  tenant isolation applies to the trail as well.
- **Metadata, not content.** Detail holds counts, route names, flags and source
  labels. It never holds document text, prompts, answers or credentials, and it
  is capped at 8 KiB by a check constraint.
- **No foreign key to `auth.users`.** Deleting a user must not erase the history
  of what that user did; a retention policy decides when events go.

The write is best-effort. `app/security/audit.py` catches any failure, logs
`audit_event_not_recorded` with the action and exception type, and returns. The
action has already happened by then; converting a logging failure into a 500
would invite the caller to retry and duplicate the effect. The opposite choice
- failing the request when the audit write fails - sounds stricter but produces
worse behaviour.

### Prompt injection: structure first, detection second

The prompts already put untrusted text inside labelled blocks (`<context>`,
`<documents>`, `<web_results>`, `<rows>`, `<evidence>`) and instruct the model
to treat those blocks as data. Phase 7 adds the mechanical part:

- **Neutralisation.** Before text is embedded in a prompt, any of the
  application's own block tags inside it are HTML-escaped. A document containing
  `</context>` can no longer end its block and promote the rest of its text to
  instructions. The report agent neutralises each source (documents, analytics
  rows, web snippets) *before* adding its own `<documents>` / `<analytics>` /
  `<web_results>` framing; neutralising the assembled string instead would
  escape the application's own tags and break the prompt.
- **Detection.** A small set of patterns - instruction overrides, prompt
  extraction, role changes, credential exfiltration, delimiter escapes - logs
  `possible_prompt_injection` with the source and marker names. It never logs
  the text, and it never blocks. A phrase match is easy to evade and would drop
  legitimate documents; the log is an operator signal, and the structural
  controls are what bound the damage.

The order matters: detection sees the raw text, then neutralisation makes it
inert. A test asserts that a hostile document's closing tag survives only in
escaped form in the final prompt.

### The analytics function runs as a two-table role

Phase 6's function was `SECURITY INVOKER`: it could not exceed the caller's
privileges, but it *did* run with them. Since every authenticated caller can
read their own `documents` and `chunks`, a generated query that passed the
string guard could read the caller's document text. The string guard cannot
catch `select * from documents` - it is valid, starts with `SELECT`, and uses no
blocked keyword.

Migration `0006` closes that:

- the function is `SECURITY DEFINER`, owned by `demo_reader`, a `NOLOGIN`,
  `NOSUPERUSER`, `NOBYPASSRLS` role;
- `demo_reader` has `SELECT` on the two demo tables and **no** grant on
  `documents`, `chunks` or anything else outside `public` (and only `USAGE` on
  the schema);
- the function plans the statement through a small `VOLATILE` helper,
  `explain_demo_query(text)`, and walks every `"Relation Name"` node. Every
  relation must be `public.demo_products` or `public.demo_sales_orders`; any
  other table, view or schema is a `P0001` rejection. This is a plan check, not
  a regex: aliases, CTEs and subqueries are already resolved to real relation
  names by the planner;
- read policies for `demo_reader` keep RLS meaningful while the function runs as
  that role.

The planning helper is separate because PostgreSQL rejects `EXPLAIN` inside a
`STABLE` function ("EXPLAIN is not allowed in a non-volatile function"), and the
executor must stay `STABLE` so Postgres also refuses data-modifying statements.
Splitting the two keeps both guarantees: planning is `VOLATILE`, execution is
not. The helper is owned by `demo_reader` and executable only by its owner;
the migration removes the default PUBLIC, `anon`, `authenticated` and
`service_role` grants.

The SQLite store cannot use roles, so `DocumentRepository` installs a
`sqlite3` connection authorizer around the generated statement. It sees the real
table name through aliases, CTEs and `count(*)`, and collects denials into the
same client-safe `AnalyticsQueryRejectedError` the Postgres path produces, naming
the offending table so the analyst can repair. The two implementations enforce
the same `ALLOWED_TABLES` set.

### Response and production hygiene

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options:
DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`. The API is
JSON-only, so these are cheap; `no-store` matters because an answer or a report
is tenant data and must not sit in a browser or proxy cache. HSTS is added only
in production: sending it in development would pin `localhost` to HTTPS in the
developer's browser and break the next run.

In production, `ALLOWED_HOSTS` feeds Starlette's `TrustedHostMiddleware`, added
after CORS so it wraps it and a forged `Host` header is rejected before routing.
A wildcard CORS origin or wildcard host list in production is logged as a
warning at startup rather than silently accepted. Rate limiting is on by
default; setting `RATE_LIMIT_ENABLED=false` logs a warning in production.

### What these controls do not claim

- The injection detector is a signal, not a sandbox. Permissions and the
  analytics allowlist are the enforcement.
- Rate limits are per process, not global (above).
- The development `X-User-Id` stub is still not a security control; it is
  refused outside development and test.
- Migration `0006` creates a database role, so the migration runner must have
  the privilege to do that. On Supabase that is the `postgres` connection used
  by `apply_migrations.py`.

## 19. Frontend (Phase 8)

The frontend is a client-side React 19 + TypeScript application built with Vite.
It is deliberately a thin, typed client: it renders state, collects input, and
calls the API. It does not query Postgres, call an LLM directly, or hold the
service-role key.

### Package layout

```
frontend/src/
  api/
    client.ts      typed fetch wrapper; one function per endpoint
    errors.ts      ApiError parsing and user-facing messages
    types.ts       mirrors of the public backend schemas
  auth/
    AuthProvider.tsx  session restore, dev identity, API client lifetime
    AuthScreen.tsx    email/password sign-in and sign-up
  components/      shell, evidence panels, Markdown, dialogs, empty/loading states
  hooks/           API health, document title, textarea auto-resize
  lib/             runtime config, formatting, upload validation
  pages/           overview, chat, documents, ask, reports, settings, 404
  App.tsx          auth gate, lazy routes, scroll reset, error boundary
  styles.css       responsive layout, dark theme, print rules
```

### Authentication and the API boundary

`AuthProvider` creates **one** Supabase client at module scope. Creating it in a
component or state initializer makes React StrictMode build a second client and
triggers Supabase's duplicate-client warning. The provider restores an existing
session, subscribes to auth changes, and refreshes a token that is close to
expiry before a request.

The client sends exactly one of two identities:

- `Authorization: Bearer <access-token>` when both Supabase environment values
  are present;
- `X-User-Id: <id>` only when neither is present, for development.

A half-configured Supabase environment raises a typed configuration error; it
never silently downgrades to the stub. The browser receives only the public
Supabase anon key. RLS and the backend's identity dependency remain the
authority; hiding a button is not a permission check.

`api/client.ts` has a single `fetch` path. It sets the auth header, serialises
JSON or `FormData`, maps network failures to `ApiError`, parses the backend's
stable `{code, message, details, request_id}` shape for non-2xx responses, and
returns typed public models. This is why screens can show `503
web_search_not_configured` and a retry action instead of raw JSON.

### Screens and evidence

- **Overview** requests health, documents and model status in parallel.
- **Chat** is explicitly ungrounded; it is a direct model conversation and does
  not pretend to have citations.
- **Documents** uploads files, polls indexing status, lists/deletes documents,
  runs semantic search, and asks grounded questions. Answers carry numbered
  document citations and the retrieved passages.
- **Ask supervisor** submits one request, shows the route and classification
  badges, and renders document, web or analytics evidence. It does not choose
  or override the backend's route.
- **Reports** can select documents, analytics, web research or a combination.
  The returned Markdown is rendered with `react-markdown` + `remark-gfm`; raw
  HTML is not enabled. Copy, `.md` download and print are client-side actions.
- **Settings** shows the active auth mode, lets a developer change the stub id,
  and exposes model/API information without exposing secrets.

Evidence panels are shared components. Document evidence shows filename, page,
score and excerpt; web evidence shows title, URL and snippet; analytics evidence
shows the executed SQL, row count, truncation state and returned rows. Empty or
failed source sets are shown as empty or failed — the UI does not fill gaps with
model text.

### Routing and responsive behaviour

Routes use `React.lazy`; `AppShell` wraps `<Outlet />` in `Suspense`, so the
navigation shell stays visible while a page chunk loads. `BrowserRouter` keeps
deep links working behind Vite's history fallback. The sidebar is persistent on
wide screens and becomes an off-canvas menu below the mobile breakpoint; tables
scroll horizontally, dialogs trap the viewport, and print styles remove the
navigation.

### What Phase 8 does not claim

- The UI is not a security boundary. Every ownership, upload, SQL and audit
  check still happens in the backend.
- The supervisor screen displays the backend's route decision. A borderline
  request can be classified as `report` instead of `analytics` by the configured
  model; the frontend correctly renders the route it was given.
- Web research still needs `TAVILY_API_KEY`; a missing key is shown as the typed
  `503`, not as an empty answer.
- Docker, container validation, a production web server and CDN cache headers
  are Phase 9 work. The Phase 8 deliverable is the verified static `dist/` build
  and the development proxy.
