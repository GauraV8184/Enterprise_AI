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
  api/
    deps.py      Shared dependencies.
    health.py    GET / and GET /health.
    router.py    Aggregates feature routers under one prefix.
  schemas/       Pydantic models describing request/response shapes.
```

Feature packages (`agents/`, `graph/`, `rag/`, `tools/`, `security/`,
`database/`) are created only in the phase that implements them. We deliberately
avoid shipping empty folders for every imagined feature — empty scaffolding
looks complete but tests nothing.

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

- No LLM calls, embeddings, or agents — Phase 2+.
- No database — Phase 4.
- No authentication — Phase 4 (Supabase Auth).
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

## 9. Security posture so far

Established now:
- Secrets are git-ignored; only `.env.example` is committed.
- Settings/credentials stay server-side.
- Structured logs avoid secret leakage by default.

To be added: authentication/authorization, RLS, prompt-injection defenses, SQL
validation, file validation, rate limiting, and audit logging (Phases 4–7).
