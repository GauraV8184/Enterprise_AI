# Enterprise AI Intelligence Platform

A web-based enterprise AI assistant that handles complex, multi-step tasks:
it retrieves internal documents (RAG), queries authorized business data safely
(SQL analytics), performs external research, analyzes results, and produces
reports — all coordinated by a LangGraph workflow behind a FastAPI backend and
a React frontend.

> **Status: Phase 1 of 9 complete.** The foundation (config, logging, a working
> FastAPI app, and tests) is in place. Later phases add the LLM, RAG, Supabase,
> LangGraph agents, tools, security, and the React UI. See
> [Roadmap](#roadmap) below for exactly what exists today.

---

## Architecture at a glance

```
React + TypeScript (Vite)        -> frontend (Phase 8)
        |  HTTPS / JSON
        v
FastAPI backend  (this repo: backend/)          <- Phase 1 ✅
   |-- core/     config + structured logging    <- Phase 1 ✅
   |-- api/      HTTP routers                   <- Phase 1 ✅ (health only)
   |-- agents/   LangGraph specialist agents    <- Phase 6
   |-- graph/    workflow state + orchestration <- Phase 5
   |-- rag/      loaders, chunking, retrieval   <- Phase 3
   |-- tools/    web search, SQL, docs, reports <- Phase 6
   |-- security/ guards, SQL validation, audit  <- Phase 7
   `-- database/ repositories                   <- Phase 4
        |
        v
Supabase: PostgreSQL + pgvector + Auth + Storage   <- Phase 4
OpenAI API (configurable provider)                 <- Phase 2
```

The backend is the only component that holds privileged credentials. The
frontend never sees the Supabase service-role key.

---

## Repository layout

```
Enterprize_AI/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory + middleware
│   │   ├── core/
│   │   │   ├── config.py        # typed settings (pydantic-settings)
│   │   │   └── logging.py       # JSON / human logging + request-id context
│   │   ├── api/
│   │   │   ├── deps.py          # shared dependencies
│   │   │   ├── health.py        # / and /health
│   │   │   └── router.py        # aggregates feature routers
│   │   └── schemas/
│   │       └── health.py
│   ├── tests/                   # pytest suite
│   ├── requirements.txt         # runtime deps
│   ├── requirements-dev.txt     # + test/lint tooling
│   └── pyproject.toml           # pytest + ruff config
├── docs/                        # architecture + setup notes
├── data/                        # local dev data (git-ignored contents)
├── .env.example                 # copy to .env, fill in values
├── .gitignore
└── README.md
```

Frontend, Supabase migrations, and Docker files are added in their own phases.

---

## Prerequisites

| Tool | Version used |
|---|---|
| Python | 3.11+ (developed on 3.13) |
| Node.js | 20+ (needed from Phase 8) |
| Git | any recent |

Docker is **not** required for local development and is not yet installed on the
machine this was developed on, so the container files are added in Phase 9 and
cannot be verified locally before then.

---

## Setup (Windows PowerShell)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# Create your environment file (optional for Phase 1 - defaults work).
Copy-Item ..\.env.example ..\.env
```

On macOS/Linux replace the interpreter path with `.venv/bin/python`.

---

## Run the API

From the `backend` directory:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Then open:

- <http://127.0.0.1:8000/> — service metadata
- <http://127.0.0.1:8000/health> — liveness probe
- <http://127.0.0.1:8000/docs> — interactive Swagger UI

Example response:

```json
{ "status": "ok", "service": "Enterprise AI Intelligence Platform",
  "version": "0.1.0", "environment": "development" }
```

---

## Run the tests and checks

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest        # 13 tests
.\.venv\Scripts\python.exe -m ruff check .  # lint
.\.venv\Scripts\python.exe -m ruff format --check .
```

---

## Configuration

All settings live in `backend/app/core/config.py` and are read from environment
variables (or a `.env` file). Field names map case-insensitively to env vars,
so `app_name` ← `APP_NAME`. See `.env.example` for the full list available in
Phase 1.

Rules this project follows:

- `.env` is git-ignored; only `.env.example` is committed.
- Never put real secrets in code or docs — never invent credentials.
- The service-role/secret keys are backend-only and are never sent to the browser.

---

## Design decisions (Phase 1)

- **Typed settings with `pydantic-settings`.** Misconfiguration fails fast at
  startup with a clear message instead of surfacing as a runtime error.
- **Settings on `app.state`, read via a dependency.** A single source of truth
  per app instance, and tests can build apps with their own settings (this
  caught a real bug during development — see `docs/architecture.md`).
- **Structured JSON logging by default** with a per-request correlation id, so
  Phase 6+ agent/tool activity can be traced end to end.
- **No empty scaffolding.** Modules are added only when a phase genuinely needs
  them.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation: FastAPI, config, logging, tests, docs | ✅ done |
| 2 | LLM integration (chat endpoint, structured output) | ⬜ |
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
  the reasoning behind Phase 1 choices.
