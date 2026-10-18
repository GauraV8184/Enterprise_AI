# Enterprise AI Intelligence Platform

A web-based enterprise AI assistant that handles complex, multi-step tasks:
it retrieves internal documents (RAG), queries authorized business data safely
(SQL analytics), performs external research, analyzes results, and produces
reports — all coordinated by a LangGraph workflow behind a FastAPI backend and
a React frontend.

> **Status: Phase 2 of 9 complete, verified against a live model.** The
> foundation and the LLM integration work end to end (validated with a local
> Ollama `llama3.1:8b`). Later phases add RAG, Supabase, LangGraph agents,
> tools, security, and the React UI. See [Roadmap](#roadmap).

---

## Architecture at a glance

```
React + TypeScript (Vite)        -> frontend (Phase 8)
        |  HTTPS / JSON
        v
FastAPI backend  (this repo: backend/)
   |-- core/     config + logging + error base      <- Phase 1 ✅
   |-- api/      HTTP routers (health, chat)        <- Phase 1-2 ✅
   |-- llm/      provider factory, prompts, service <- Phase 2 ✅
   |-- agents/   LangGraph specialist agents        <- Phase 6
   |-- graph/    workflow state + orchestration     <- Phase 5
   |-- rag/      loaders, chunking, retrieval       <- Phase 3
   |-- tools/    web search, SQL, docs, reports     <- Phase 6
   |-- security/ guards, SQL validation, audit      <- Phase 7
   `-- database/ repositories                       <- Phase 4
        |
        v
Supabase: PostgreSQL + pgvector + Auth + Storage   <- Phase 4
LLM provider: OpenAI (configurable)                <- Phase 2 ✅
```

The backend is the only component that holds privileged credentials. The
frontend never sees the Supabase service-role key or the LLM API key.

---

## Repository layout

```
Enterprize_AI/
├── backend/
│   ├── app/
│   │   ├── main.py                 # app factory, lifespan, middleware
│   │   ├── core/
│   │   │   ├── config.py           # typed settings
│   │   │   ├── logging.py          # JSON logging, request id, redaction
│   │   │   └── errors.py           # AppError base (status + safe message)
│   │   ├── llm/
│   │   │   ├── factory.py          # provider -> chat model
│   │   │   ├── prompts.py          # system prompts (no-fabrication rules)
│   │   │   ├── schemas.py          # structured-output models (TaskIntent)
│   │   │   ├── service.py          # calls + response/error normalisation
│   │   │   └── errors.py           # LLM error types
│   │   ├── api/
│   │   │   ├── deps.py             # shared dependencies
│   │   │   ├── health.py           # / and /health
│   │   │   ├── chat.py             # /chat, /chat/intent, /chat/status
│   │   │   ├── error_handlers.py   # AppError -> JSON
│   │   │   └── router.py
│   │   └── schemas/
│   │       ├── health.py
│   │       └── chat.py
│   ├── scripts/check_llm.py        # live diagnostic for the LLM setup
│   ├── tests/                      # 67 unit tests + opt-in live tests
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── pyproject.toml
├── docs/architecture.md
├── data/
├── .env.example
└── README.md
```

---

## Prerequisites

| Tool | Version used |
|---|---|
| Python | 3.11+ (developed on 3.13) |
| Node.js | 20+ (needed from Phase 8) |

Docker is not required for local development and is not yet installed on the
development machine, so container files arrive in Phase 9.

---

## Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# Create your environment file in the REPO ROOT (not in backend/).
Copy-Item ..\.env.example ..\.env
```

On macOS/Linux use `.venv/bin/python` instead.

The app reads `.env` from the repository root no matter which directory you
launch from.

---

## LLM setup

The app works with a hosted OpenAI account **or a local Ollama server**. Pick one.

### Option A — hosted OpenAI

Put a key in the root `.env`:

```
OPENAI_API_KEY=<your key>
OPENAI_MODEL=gpt-4o-mini
```

Note that OpenAI accounts need a **credit balance**; a valid key on an empty
account returns HTTP 429 with `insufficient_quota`. The app reports this
separately as `402 llm_quota_exhausted` because retrying cannot fix it.

### Option B — local Ollama (free, no account)

1. Install and start [Ollama](https://ollama.com).
2. Pull a model. Choose one with tool-calling support, because the Phase 5–6
   agents need to select tools and emit structured JSON:

   ```powershell
   ollama pull llama3.1:8b
   ```

3. Set these in the root `.env`:

   ```
   LLM_PROVIDER=ollama
   OPENAI_MODEL=llama3.1:8b
   ```

That is all — `/api/chat/status` will report `"configured": true`. There is **no
API key**: a local server does not need one, and the `ollama` provider does not
ask for one. If your server is not on localhost, add
`OPENAI_BASE_URL=http://<host>:11434/v1`.

Use `LLM_PROVIDER=openai` (plus `OPENAI_BASE_URL` and a key) only when you are
talking to a hosted OpenAI-compatible *service* that requires credentials.

### Verify either option

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\check_llm.py
```

This makes a real chat call and a real structured-output call and prints the
result. A green result means the API will work. It never prints the key.

### Checking that Ollama is running

Ollama runs as a background app on Windows (tray icon) and starts on login by
default. Four checks, fastest first:

```powershell
ollama list                                    # 1. CLI answers with a model table
curl.exe -s http://127.0.0.1:11434/api/tags    # 2. API returns JSON
Get-NetTCPConnection -LocalPort 11434 -State Listen   # 3. port is open
.\.venv\Scripts\python.exe scripts\check_llm.py       # 4. app config + real call
```

Check 4 is the definitive one: it uses your actual `.env` and makes a real
request, so it also catches a wrong `OPENAI_MODEL` or a typo in
`OPENAI_BASE_URL` that the port check would miss.

To start it, launch **Ollama** from the Start menu or run `ollama serve`. If it
is already running, `ollama serve` prints `bind: address already in use`, which
confirms it is up rather than indicating a problem.

When Ollama is unreachable the API returns `502 llm_connection_failed` with a
message naming the configured base URL. `GET /api/chat/status` reports
configuration state without sending a message.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `503 llm_not_configured` | `OPENAI_API_KEY` is empty while `LLM_PROVIDER=openai`. For a local server use `LLM_PROVIDER=ollama` instead — no key needed. |
| Validation error on `llm_provider` | Bad `LLM_PROVIDER` value. Supported: `openai`, `ollama`. |
| `502 llm_connection_failed` | Ollama is not running, or `OPENAI_BASE_URL` is wrong/not set. Check with `ollama list` and open `http://127.0.0.1:11434/api/tags`. |
| `502 llm_authentication_failed` | The key was rejected (wrong, revoked, or another project's). |
| `402 llm_quota_exhausted` | The account has no credits. Add credits — retrying will not help. |
| `429 llm_rate_limited` | Genuine throttling. Wait and retry. |
| Model errors after `ollama pull` | Confirm the `.env` model name matches `ollama list` exactly, including the tag (`llama3.1:8b`, not `llama3.1`). |
| First response is very slow | A local 8B model on CPU loads into memory on first use. Later responses are faster. |

### Choosing a model

Model names change over time, so `OPENAI_MODEL` is configurable. Any model your
provider can serve works; `check_llm.py` tells you immediately if a name is wrong.

### Using a different provider

`app/llm/factory.py` is the only place that knows about the OpenAI API surface.
Any OpenAI-compatible endpoint needs no code change — set `OPENAI_BASE_URL`.
A genuinely different protocol means adding one branch to `build_chat_model`.

---

## Run the API

From the `backend` directory:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

| Endpoint | Purpose |
|---|---|
| `GET /` | service metadata |
| `GET /health` | liveness probe |
| `GET /api/chat/status` | is the LLM configured? (never leaks the key) |
| `POST /api/chat` | send a conversation, get a reply |
| `POST /api/chat/intent` | classify a request (structured output) |
| `GET /docs` | interactive Swagger UI |

Example:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/chat `
  -H "Content-Type: application/json" `
  -d '{\"messages\":[{\"role\":\"user\",\"content\":\"Explain our Q3 process\"}]}'
```

`POST /api/chat/intent` returns a typed classification — the routing input the
LangGraph supervisor consumes in Phase 5:

```json
{
  "intent": {
    "intent": "report",
    "requires_internal_documents": true,
    "requires_business_data": true,
    "requires_web_research": true,
    "complexity": "multi_step",
    "summary": "Analyse Q3 sales against strategy documents and market trends."
  },
  "provider": "openai",
  "model": "gpt-4o-mini",
  "request_id": "..."
}
```

---

## Run the tests and checks

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest          # 73 passed, 2 skipped
.\.venv\Scripts\python.exe -m ruff check .    # lint
.\.venv\Scripts\python.exe -m ruff format --check .
```

The 2 skipped tests are **live** LLM tests (they need a running provider).
They are opt-in:

```powershell
$env:RUN_LIVE_LLM_TESTS="1"; .\.venv\Scripts\python.exe -m pytest tests/integration -v
```

With a provider running the whole suite is green: **75 passed**, including two
real model calls.

The default suite never touches the network: it runs the real application code
against an in-process fake chat model.

---

## Configuration

All settings live in `backend/app/core/config.py`. Field names map
case-insensitively to environment variables (`app_name` ← `APP_NAME`).
See `.env.example` for the full list.

Rules this project follows:

- `.env` is git-ignored; only `.env.example` is committed.
- Credentials are `SecretStr`, so they cannot leak through logs, `repr()`, or
  tracebacks.
- The LLM key and any service-role key stay backend-only.
- Never invent credentials; never commit real ones.

---

## Error handling

Every expected failure has a stable `code` and an actionable message:

| Code | HTTP | When |
|---|---|---|
| `llm_not_configured` | 503 | no API key set |
| `llm_authentication_failed` | 502 | provider rejected the key |
| `llm_quota_exhausted` | 402 | account has no credits left — retrying will not help |
| `llm_rate_limited` | 429 | provider throttled us — retrying later helps |
| `llm_timeout` | 504 | provider did not answer in time |
| `llm_connection_failed` | 502 | network/base-URL problem |
| `llm_provider_error` | 502 | any other provider error |
| `llm_structured_output_failed` | 502 | response did not match the schema |

Note that providers report *exhausted credits* and *throttling* with the same
HTTP status (429). They need opposite responses, so the platform inspects the
provider's error code and returns `402` for an empty balance. Retrying an empty
balance can never succeed, and telling you to retry would be bad advice.

Responses include `request_id`, which also appears in the logs and in the
`X-Request-ID` header — so a user-visible error can be traced to a log line.

---

## Design decisions worth knowing

- **Nothing is faked.** If the model is unavailable, requests fail with a clear
  error. There is no mock path in production code.
- **Prompts forbid fabrication** and treat document/web/tool text as untrusted
  data rather than instructions (the first layer of prompt-injection defence;
  real enforcement happens outside the LLM in Phase 7).
- **Errors are typed.** Anything the API surfaces subclass `AppError`, so adding
  an error type never means editing routers.
- **`safe_extra()` for logging.** Python's logging raises `KeyError` if an
  `extra` field collides with a built-in `LogRecord` attribute — which can turn
  a handled error into a crash. The helper prevents that and redacts suspicious
  field names.
- **Word-boundary redaction.** Keys like `total_tokens` stay readable while
  `access_token` gets redacted.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation: FastAPI, config, logging, tests, docs | ✅ done |
| 2 | LLM integration: chat, structured output, error handling | ✅ done |
| 3 | RAG: upload, parse, chunk, embed, retrieve | ⬜ |
| 4 | Supabase: schema, Auth, Storage, RLS, persistence | ⬜ |
| 5 | LangGraph workflow + state | ⬜ |
| 6 | Tools & specialist agents + report generation | ⬜ |
| 7 | Security, audit logging, rate limits, SQL safety | ⬜ |
| 8 | React + TypeScript frontend | ⬜ |
| 9 | Tests, Docker, deployment docs | ⬜ |

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — components, request flow, and
  the reasoning behind each phase's choices.
