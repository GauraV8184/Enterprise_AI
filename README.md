# Enterprise AI Intelligence Platform

A web-based enterprise AI assistant that handles complex, multi-step tasks:
it retrieves internal documents (RAG), queries authorized business data safely
(SQL analytics), performs external research, analyzes results, and produces
reports — all coordinated by a LangGraph workflow behind a FastAPI backend and
a React frontend.

> **Status: Phase 7 of 9 complete, verified against live providers.** Phases 1–7
> — foundation, LLM integration, RAG over uploaded documents, Supabase
> persistence with Row Level Security, the LangGraph supervisor, the specialist
> agents (SQL analytics, web research, Markdown reports), and security hardening
> (per-caller rate limits, an append-only audit trail, prompt-injection
> defences, restricted analytics execution, and production response headers) —
> work end to end. Validation used a local Ollama `llama3.1:8b` and the live
> Supabase project. Web research needs `TAVILY_API_KEY`; without it the research
> route answers a typed 503, and `check_web_search.py` exits 1 saying "not
> configured" rather than pretending to pass. Later phases add the React UI and
> deployment. See [Roadmap](#roadmap).

---

## Architecture at a glance

```
React + TypeScript (Vite)        -> frontend (Phase 8)
        |  HTTPS / JSON
        v
FastAPI backend  (this repo: backend/)
   |-- core/     config + logging + error base       <- Phase 1 ✅
   |-- api/      chat, documents, workflow, reports  <- Phase 1-6 ✅
   |-- llm/      provider factory, prompts, service  <- Phase 2 ✅
   |-- graph/    LangGraph supervisor + state        <- Phase 5 ✅
   |-- agents/   analyst, research, report writers   <- Phase 6 ✅
   |-- rag/      loaders, chunking, retrieval        <- Phase 3 ✅
   |-- tools/    Tavily search + SQL analytics       <- Phase 6 ✅
   |-- security/ guards, SQL validation, audit       <- Phase 7 ✅
   `-- database/ repositories                        <- Phase 4 ✅
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
│   │   │   ├── factory.py          # provider -> chat model AND embeddings
│   │   │   ├── prompts.py          # system prompts (no-fabrication, RAG rules)
│   │   │   ├── schemas.py          # structured-output models (TaskIntent)
│   │   │   ├── service.py          # calls + response/error normalisation
│   │   │   └── errors.py           # LLM error types
│   │   ├── rag/
│   │   │   ├── loaders.py          # PDF/DOCX/XLSX/CSV/MD/TXT extraction
│   │   │   ├── chunking.py         # splitting that preserves page + section
│   │   │   ├── ingest.py           # parse -> chunk -> embed -> store pipeline
│   │   │   └── retriever.py        # similarity search + grounded answering
│   │   ├── agents/
│   │   │   ├── analyst.py          # SQL generation, repair loop, summary
│   │   │   ├── research.py         # search planning, dedup, cited answers
│   │   │   └── report.py           # evidence gathering + Markdown reports
│   │   ├── tools/
│   │   │   ├── analytics.py        # demo schema description + SQL execution
│   │   │   └── web_search.py       # Tavily client (plain httpx, no extra SDK)
│   │   ├── database/
│   │   │   ├── repositories.py        # store contract + SQLite implementation
│   │   │   ├── supabase_repository.py # PostgREST store; search via pgvector
│   │   │   ├── postgrest.py           # per-caller transport + error mapping
│   │   │   ├── sql_guard.py           # read-only SQL pre-flight rules
│   │   │   └── demo_data.py           # the demo sales dataset (both seeds)
│   │   ├── storage/
│   │   │   ├── base.py                # file-store contract + key layout
│   │   │   ├── local.py               # filesystem backend (dev + tests)
│   │   │   └── supabase.py            # Storage REST client, per caller
│   │   ├── security/
│   │   │   ├── file_validation.py  # extension, size and magic-byte checks
│   │   │   ├── identity.py         # who is asking (Supabase JWT or dev stub)
│   │   │   ├── supabase_auth.py    # JWT verification against the project JWKS
│   │   │   ├── rate_limit.py       # per-identity sliding-window budgets
│   │   │   ├── audit.py            # best-effort, append-only audit writes
│   │   │   ├── prompt_injection.py # detector + delimiter neutralisation
│   │   │   └── errors.py           # auth error types
│   │   ├── api/
│   │   │   ├── deps.py             # shared dependencies
│   │   │   ├── health.py           # / and /health
│   │   │   ├── chat.py             # /chat, /chat/intent, /chat/status
│   │   │   ├── documents.py        # upload, list, delete, search, ask
│   │   │   ├── workflow.py         # /workflow/run
│   │   │   ├── reports.py          # /reports
│   │   │   ├── error_handlers.py   # AppError -> JSON
│   │   │   └── router.py
│   │   └── schemas/
│   │       ├── health.py
│   │       ├── chat.py
│   │       ├── documents.py
│   │       ├── evidence.py         # web + analytics evidence blocks
│   │       ├── workflow.py
│   │       └── reports.py
│   ├── scripts/
│   │   ├── check_llm.py            # live diagnostic for the LLM setup
│   │   ├── check_rag.py            # live diagnostic for the document pipeline
│   │   ├── check_supabase.py       # credentials, schema and catalog audit
│   │   ├── check_rls.py            # tenant isolation through the REST API
│   │   ├── check_store.py          # the application's store, live
│   │   ├── check_storage.py        # bucket policies, live
│   │   ├── check_workflow.py       # every supervisor route, live
│   │   ├── check_analytics.py      # demo dataset + SQL function, live
│   │   ├── check_audit.py          # audit actor, isolation, append-only, live
│   │   ├── check_web_search.py     # live Tavily search (exits 1 without a key)
│   │   └── apply_migrations.py     # apply supabase/migrations over psycopg
│   ├── tests/                      # 502 offline tests + 5 opt-in live tests
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── pyproject.toml
├── docs/architecture.md
├── supabase/migrations/            # 0001-0007: schema, RLS, search, demo data, security
├── data/                           # git-ignored: uploads + SQLite store
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
2. Pull two models. The chat model needs tool-calling support, because the
   Phase 5–6 agents must select tools and emit structured JSON. The embedding
   model powers document search in Phase 3:

   ```powershell
   ollama pull llama3.1:8b        # chat + tool calling (~4.9 GB)
   ollama pull nomic-embed-text   # embeddings for document search (~274 MB)
   ```

3. Set these in the root `.env`:

   ```
   LLM_PROVIDER=ollama
   OPENAI_MODEL=llama3.1:8b
   EMBEDDING_PROVIDER=ollama
   EMBEDDING_MODEL=nomic-embed-text
   ```

That is all — `/api/chat/status` will report `"configured": true`. There is **no
API key**: a local server does not need one, and the `ollama` provider does not
ask for one. If your server is not on localhost, add
`OPENAI_BASE_URL=http://<host>:11434/v1` (and `EMBEDDING_BASE_URL` if the
embedding server differs).

Use `LLM_PROVIDER=openai` (plus `OPENAI_BASE_URL` and a key) only when you are
talking to a hosted OpenAI-compatible *service* that requires credentials.

### Verify either option

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\check_llm.py        # chat model answers
.\.venv\Scripts\python.exe scripts\check_rag.py        # upload -> search -> cited answer
.\.venv\Scripts\python.exe scripts\check_workflow.py   # classify -> route -> answer
```

`check_llm.py` makes a real chat call and a real structured-output call.
`check_rag.py` runs the whole document pipeline against a throwaway database, so
it never touches your `data/` folder. `check_workflow.py` drives every
supervisor route your configuration supports — chat, documents, analytics and
report, plus research when `TAVILY_API_KEY` is set — against a throwaway
database. Each script prints a clear verdict and never prints a key.

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
| `502` on upload with `llm_connection_failed` | Embeddings use the same server. Pull the model: `ollama pull nomic-embed-text`. |
| `503 embeddings not configured` | `EMBEDDING_PROVIDER` needs a key for `openai`, or `ollama` for a local server. |
| Upload returns `422 no_extractable_text` | The PDF has no text layer (it is a scan). OCR is not implemented. |
| `409 embedding_dimension_mismatch` | `EMBEDDING_MODEL` changed after documents were indexed. Re-upload them, or restore the old model. |
| Search finds nothing at all | Check `searched_chunks` in the response. `0` means nothing is indexed — the upload may have failed. |
| Search finds the wrong passages | Run `POST /api/documents/search` on its own. If retrieval is wrong, the answer will be too — that is a chunking/embedding issue, not a prompting one. |
| Answers ignore the documents | Use `scripts/check_rag.py`. It verifies the figure in the answer came from the file. |

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
| `POST /api/documents` | upload a file, extract, chunk, embed and index it |
| `GET /api/documents` | list your documents and their processing status |
| `GET /api/documents/{id}` | one document's metadata and status |
| `DELETE /api/documents/{id}` | delete a document, its vectors and the stored file |
| `POST /api/documents/search` | semantic search over your own documents |
| `POST /api/documents/ask` | answer a question from your documents, with citations |
| `POST /api/workflow/run` | classify a request and answer it with the right capability |
| `POST /api/reports` | generate an evidence-backed Markdown report |
| `GET /docs` | interactive Swagger UI |

All `/api/*` endpoints require a caller identity. In development the stub supplies
the default user, so the examples below need no header. With Supabase Auth
configured, send `Authorization: Bearer <access-token>`; anonymous callers get
`401`. Outside the API, only `/` and `/health` are anonymous (the docs UI is too,
where it is enabled).

Example:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/chat `
  -H "Content-Type: application/json" `
  -d '{\"messages\":[{\"role\":\"user\",\"content\":\"Explain our Q3 process\"}]}'
```

`POST /api/chat/intent` returns a typed classification — the same structure the
supervisor workflow consumes to route a request:

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

## Document intelligence (Phase 3)

Upload documents and ask questions about them. Answers are generated **only** from
retrieved passages, and every answer carries citations back to a file and page.

### Supported file types

| Type | Extensions | What is extracted |
|---|---|---|
| PDF | `.pdf` | Text per page, with page numbers preserved for citations |
| Word | `.docx` | Paragraphs and tables, in document order, with heading sections |
| Excel | `.xlsx` | Every sheet, header row repeated in each block |
| CSV | `.csv` | Header-prefixed row groups (comma, semicolon and tab detected) |
| Text | `.txt` | Whole file |
| Markdown | `.md`, `.markdown` | Prose, with nested heading sections |

Scanned PDFs have no text layer and **OCR is not implemented**. They fail with
`422 no_extractable_text` rather than indexing as an empty document.

### Try it

```powershell
# Upload (201 means already searchable)
curl.exe -s -X POST http://127.0.0.1:8000/api/documents `
  -F "file=@C:\path\to\handbook.pdf"

# Retrieval only - no model involved. Use this when an answer looks wrong.
curl.exe -s -X POST http://127.0.0.1:8000/api/documents/search `
  -H "Content-Type: application/json" `
  -d '{\"query\":\"how much annual leave do employees get?\",\"top_k\":3}'

# Grounded answer with citations
curl.exe -s -X POST http://127.0.0.1:8000/api/documents/ask `
  -H "Content-Type: application/json" `
  -d '{\"question\":\"how much annual leave do employees get?\"}'
```

An `/ask` response looks like this:

```json
{
  "answer": "According to [1], a full-time employee accrues twenty days of paid annual leave per calendar year.",
  "grounded": true,
  "sources": [
    {
      "label": 1,
      "filename": "handbook.pdf",
      "page": 1,
      "score": 0.8868,
      "snippet": "Every full-time employee accrues twenty days of paid annual leave..."
    }
  ],
  "searched_chunks": 4
}
```

`grounded: false` means nothing relevant was found. In that case **the model is not
called at all** — the response explains that no matching content exists, because
asking a model to answer from an empty context is how hallucinations start.

### Who owns what

With Supabase configured, every request carries a Supabase access token. The token
is verified locally against the project's published keys, and then forwarded to
Postgres on every query, where **Row Level Security** decides which rows exist.
The `owner_id` filters in the application are a second line of defence rather than
the only one.

Without Supabase, a development stub is used:

- `X-User-Id: <id>` names the caller for local multi-user testing. **This is not a
  security control** — anyone can send that header.
- The stub is accepted **only** in `ENVIRONMENT=development` and `test`. Staging
  and production refuse to start without Supabase Auth rather than trust an
  unverified header, so a deployed environment cannot be impersonated by adding
  one.

Cross-user access returns **404, not 403**, so an id never confirms that someone
else's document exists.

### Where the data lives

`DOCUMENT_STORE=auto` (the default) uses Supabase Postgres when credentials are
present, and the local SQLite store otherwise. Both sit behind the same contract,
so the rest of the application does not know which is running:

- **Supabase** — `documents` and `chunks` in Postgres with a `vector(768)` column
  and an HNSW index. Similarity search is one SQL function call, so the vectors
  never leave the database.
- **SQLite** — `data/app.db` with vectors as raw float32 and a NumPy cosine scan.
  Correct, fast enough for thousands of chunks, and it needs no account, which is
  why the offline test suite and the SQLite path still use it.

Original files follow `FILE_STORAGE` (default `auto`, which follows the document
store). In production they go to the private `documents` bucket, keyed
`<owner_id>/<document_id>/<sanitised filename>` and written with the **caller's**
access token, so the bucket's RLS policies - not application code - decide who
can read them. The SQLite path keeps the same key layout under `data/uploads/`,
so the offline suite exercises the same code. `ENVIRONMENT=production` refuses to
start with local file storage: a container filesystem is replaced on every deploy
and is not shared between instances.

---

## The supervisor workflow (Phase 5–6)

`POST /api/workflow/run` is a single endpoint that decides how a request should
be answered, routes it, and answers with the same code the dedicated endpoints
use:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/workflow/run `
  -H "Content-Type: application/json" `
  -d '{\"message\":\"How many days of annual leave do employees get?\"}'
```

The graph has one decision and five routes, each backed by code that is verified
on its own:

| Route | Chosen when | Backed by |
|---|---|---|
| `report` | the classification names a report — the only route asked to combine sources | the report agent behind `POST /api/reports` |
| `documents` | the request is a document question, or the classification says internal documents are required | the retrieval path behind `POST /api/documents/ask` |
| `research` | the request asks for external information, or web research is required | live Tavily search, cited as `[W1]`… (503 without `TAVILY_API_KEY`) |
| `analytics` | the request is a data question, or business data is required | one generated, validated SQL query over the demo dataset |
| `chat` | everything else | the completion path behind `POST /api/chat` |

The priority is deliberate. A report wins outright because it is the only route
that gathers from more than one source; a request that names the internal corpus
is answered from it rather than from the web; and a route whose tool is not
configured fails with a typed `503` instead of quietly answering from a weaker
source.

A response (document route, abridged):

```json
{
  "answer": "According to [1], a full-time employee accrues twenty days of paid annual leave per calendar year.",
  "route": "documents",
  "intent": {
    "intent": "document_question",
    "requires_internal_documents": true,
    "requires_business_data": false,
    "requires_web_research": false,
    "complexity": "simple",
    "summary": "Ask how many days of annual leave a full-time employee accrues."
  },
  "grounded": true,
  "sources": [
    {
      "label": 1,
      "document_id": "5c1b...",
      "filename": "handbook.txt",
      "page": null,
      "section": null,
      "score": 0.8868,
      "snippet": "Every full-time employee accrues twenty days of paid annual leave..."
    }
  ],
  "searched_chunks": 1,
  "web_sources": [],
  "analytics": null,
  "provider": "ollama",
  "model": "llama3.1:8b",
  "usage": {"input_tokens": 412, "output_tokens": 24, "total_tokens": 436},
  "request_id": "..."
}
```

The decision travels back with the answer, so a caller can always see which
capability ran and why. Evidence comes back in three shapes: `sources` are
document passages labelled `[1]`, `[2]` as the model was told to cite them;
`web_sources` are web results labelled `W1`, `W2`; `analytics` carries the SQL
that ran and the rows it returned. The prefixes are load-bearing — in one
response that cites both, `[1]` must never be confusable with `W1`.

`grounded` applies to the four evidence routes: `true` when the source returned
evidence, `false` when it returned nothing — and in that case **the model is not
called at all**, exactly as with `/api/documents/ask`. A report whose requested
sources all come back empty returns a short Markdown body that says so. For the
chat route `grounded` is `null`, because grounding does not apply.

A classification, retrieval, search, SQL, or provider failure is reported as an
error; there is no fallback that answers with a guess.

The graph, the state it carries, and the routing rules live in
`backend/app/graph/`; `check_workflow.py` (see above) proves every configured
route against your configured provider.

---

## Tools and specialist agents (Phase 6)

### SQL analytics over a demo dataset

Data questions are answered by generating one SQL query, running it, and
summarising the returned rows. The dataset is **clearly-labelled demonstration
data**: `demo_products` (4 products) and `demo_sales_orders` (384 orders across
4 regions and 24 months from 2024-01), seeded identically by migration
`0005_demo_analytics.sql` in Postgres and by `app/database/demo_data.py` in the
SQLite development store. `check_analytics.py` compares the two seeds against
each other on the live project.

Executing generated SQL is a security-shaped problem, so it is bounded in three
independent layers, and each one fails closed:

1. **A Python pre-flight guard** (`app/database/sql_guard.py`): exactly one
   statement, `SELECT`/`WITH` only, a keyword blocklist, a length limit, and a
   table allowlist (`ALLOWED_TABLES`). Its refusals are safe to show, and the
   analyst feeds one back to the model as a single repair attempt. The SQLite
   store enforces the same allowlist independently with a connection authorizer,
   because SQLite has no roles or RLS.
2. **A locked-down database function** (`run_demo_query`): `SECURITY DEFINER`,
   owned by the non-login `demo_reader` role (migration `0006`). Before running,
   it asks a small `VOLATILE` planning helper (`explain_demo_query`, owned by the
   same role — `EXPLAIN` is forbidden inside a `STABLE` function) and walks the
   plan: every relation must be `public.demo_products` or
   `public.demo_sales_orders`, and any other table or schema is refused. The
   function is also `STABLE` — Postgres refuses data-modifying statements inside
   a non-volatile function — with its own copy of the keyword rules, an outer
   `LIMIT` capped at 200 rows (`ANALYTICS_MAX_ROWS` defaults to 100), and
   `statement_timeout = 5s`.
3. **Role-level grants and RLS**: `demo_reader` has `SELECT` on exactly the two
   demo tables, read policies for those tables, and no grant on `documents` or
   `chunks` — nor membership in a role that has one. `anon` cannot execute the
   function at all. A caller still has to authenticate before PostgREST will run
   it, but the SQL itself can no longer run with the caller's full privileges.

The analyst agent gets at most two attempts: generate, and — if the query is
rejected or fails — one repair with the database's own complaint as feedback.
Still failing means a typed error, never a made-up answer.

### Web research (Tavily)

With `TAVILY_API_KEY` set, research questions are answered from live search
results, cited as `[W1]`, `[W2]`. The agent plans its queries with a structured
call, deduplicates results across queries, and — when a search returns nothing —
says so without calling the model. Without a key the route answers
`503 web_search_not_configured`; it never substitutes model knowledge for a
search that did not happen. Verify with `scripts/check_web_search.py`, which
exits 1 with "not configured" in that state.

### Markdown reports

`POST /api/reports` is the one endpoint that combines capabilities:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/reports `
  -H "Content-Type: application/json" `
  -d '{\"topic\":\"Annual leave policy and demo sales by region\",\"sources\":[\"documents\",\"analytics\"]}'
```

The response carries the Markdown body **and everything it was written from**,
so a reader can check the report instead of trusting it (abridged):

```json
{
  "report": "# Annual leave policy and demo sales by region\n\nEmployees accrue twenty days [1]...",
  "topic": "Annual leave policy and demo sales by region",
  "sources_requested": ["documents", "analytics"],
  "grounded": true,
  "sources": [{"label": 1, "filename": "handbook.txt", "page": null, "score": 0.87, "snippet": "Every full-time employee..."}],
  "web_sources": [],
  "analytics": {
    "sql": "select region, round(sum(revenue), 2) as revenue from demo_sales_orders group by region",
    "row_count": 4,
    "truncated": false,
    "rows": [{"region": "East", "revenue": 508924.0}]
  },
  "provider": "ollama",
  "model": "llama3.1:8b",
  "usage": {"input_tokens": 612, "output_tokens": 291, "total_tokens": 903},
  "request_id": "..."
}
```

Defaults and honesty rules: `sources` defaults to `["documents"]` and is never
widened silently; duplicates are removed; requesting `web` without a key is a
`503` rather than a report with a quietly missing source. A requested source
that came back empty is named in the prompt as a gap, so the report states what
it could not cover instead of padding. If *no* requested source returns
evidence, `grounded` is `false` and the response explains itself with no model
call at all.

---

## Security hardening (Phase 7)

Security is enforced in the backend, not asked of the model. The controls below
are deliberately small and testable, and each one states its own limit.

### Per-caller request budgets

Two sliding 60-second windows are tracked per identity: **standard** (default
240/min) for reads, metadata and deletes, and **model** (default 30/min) for endpoints
that call an LLM or embedding model — chat, intent, upload, search, ask,
workflow, reports. A rejected request gets `429 rate_limited` with a
`Retry-After` header. The check runs before the handler, so it costs no model
call and writes no row.

The honest caveat: the counters live in the serving process. With N workers the
effective budget is N × the configured value, and a restart forgets the window.
That is acceptable for the current single-instance deployment and is why Phase 9
puts a shared counter (Redis) on the list. `RATE_LIMIT_ENABLED=false` turns the
budgets off; production logs a warning when they are off.

### Append-only audit trail

Five actions write an event: `document.uploaded`, `document.deleted`,
`documents.asked`, `workflow.ran`, `report.generated`. Migration
`0007_audit_events.sql` creates `public.audit_events` with:

- **The actor from the token, not the payload.** The only write path is
  `record_audit_event()`, which derives `actor_id` from `auth.uid()`. The
  Supabase repository never sends an actor id, so a direct caller cannot write
  an event under another user's name.
- **Append-only.** No `UPDATE` or `DELETE` grant or policy exists for
  `authenticated`; the service role keeps `DELETE` for retention jobs.
- **Read your own.** The `SELECT` policy compares `actor_id` to `auth.uid()`.
- **Metadata, not content.** The detail holds counts, routes, flags and source
  labels — never document text, prompts, answers or credentials — and is capped
  at 8 KiB.

Writes are best-effort: if the audit insert fails, the request logs
`audit_event_not_recorded` and still succeeds. The action has already happened;
turning a logging failure into a 500 would invite a retry that duplicates it.
`scripts/check_audit.py` proves the actor, isolation and immutability live.

### Prompt-injection defence

Untrusted text was already labelled (`<context>`, `<documents>`,
`<web_results>`, `<rows>`, `<evidence>`) and the system prompts say labelled
content is data. Phase 7 adds two pieces:

- **Delimiter neutralisation.** The application's own block tags found *inside*
  untrusted text are HTML-escaped before formatting. A document can no longer
  contain `</context>` or `</evidence>` and end its data block early. Report
  evidence is neutralised per source before the framing tags are added.
- **A conservative detector.** Known patterns (instruction overrides, prompt
  extraction, role changes, credential exfiltration, delimiter escapes) log
  `possible_prompt_injection` with the source and marker names — never the
  text. It does not block: a phrase match is easy to evade and would drop
  legitimate documents, so the signal is for an operator and the structural
  controls are what bound the damage.

### Production hygiene

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options:
DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store` — answers
and reports are tenant data and must not sit in a shared cache. Production adds
`Strict-Transport-Security`; development deliberately does not, because pinning
`localhost` to HTTPS would break the next run. In production, `ALLOWED_HOSTS`
feeds `TrustedHostMiddleware`, so a forged `Host` header is rejected before
routing, and a wildcard CORS or host setting is logged as a warning at startup.

### What Phase 7 does not claim

- The detector is a signal, not a sandbox. Permissions and the analytics
  allowlist are the enforcement.
- Rate limits are per process, not global (above).
- The development `X-User-Id` stub is still not a security control; it is
  refused outside development and test.

---

## Run the tests and checks

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest          # 502 passed, 5 skipped
.\.venv\Scripts\python.exe -m ruff check .    # lint
.\.venv\Scripts\python.exe -m ruff format --check .
```

The 5 skipped tests are **live** tests that need a running provider. They are opt-in:

```powershell
$env:RUN_LIVE_LLM_TESTS="1"; .\.venv\Scripts\python.exe -m pytest tests/integration -v
```

With a provider running the whole suite is green: **507 passed**, including real
chat calls, real embeddings, and a full upload → retrieve → cite cycle.

The Supabase side has its own live checks. The checks that need user accounts
create throwaway users with the admin key, run real requests, and delete what
they created:

```powershell
.\.venv\Scripts\python.exe scripts\check_supabase.py   # credentials, schema, catalog audit
.\.venv\Scripts\python.exe scripts\check_rls.py        # tenant isolation via the REST API
.\.venv\Scripts\python.exe scripts\check_store.py      # the application's store, end to end
.\.venv\Scripts\python.exe scripts\check_storage.py    # bucket policies with two real users
.\.venv\Scripts\python.exe scripts\check_analytics.py  # demo dataset + SQL function, end to end
.\.venv\Scripts\python.exe scripts\check_audit.py      # audit actor, isolation, append-only
.\.venv\Scripts\python.exe scripts\check_web_search.py # live search; exits 1 without a key
```

The service-role key is used by those scripts only. The application never reads
it. `check_web_search.py` is honest about absence: without `TAVILY_API_KEY` it
exits 1 saying "not configured", because a green check that never searched
anything would be a lie.

The default suite never touches the network. It runs the real application code —
real parsers, real SQLite store, real ranking — against an in-process fake chat
model and a deterministic fake embedding model. Sample PDF, DOCX, XLSX and CSV
files are *generated* by the tests rather than committed as binaries, so the
parsers are exercised against genuine library output.

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
| `unsupported_file_type` | 415 | extension is not in the allowlist |
| `file_too_large` | 413 | exceeds `MAX_UPLOAD_MB` |
| `invalid_file` | 400 | empty, or the bytes do not match the extension |
| `document_parse_failed` | 422 | the file is the right type but could not be read |
| `no_extractable_text` | 422 | read cleanly but had no text (e.g. a scanned PDF) |
| `document_not_found` | 404 | no such document, or it belongs to another user |
| `embedding_dimension_mismatch` | 409 | `EMBEDDING_MODEL` changed after indexing — re-upload |
| `not_authenticated` | 401 | no caller identity, or an explicitly empty `X-User-Id` in development |
| `invalid_token` | 401 | malformed token, unsupported algorithm, or an unknown signing key |
| `token_expired` | 401 | correctly signed, but past its `exp` |
| `auth_unavailable` | 503 | JWKS unreachable and no cached keys to verify with |
| `authentication_not_configured` | 503 | an `X-User-Id` request outside development/test with no Supabase Auth |
| `web_search_not_configured` | 503 | a research or web-sourced request needs search and `TAVILY_API_KEY` is not set |
| `web_search_failed` | 502 | the search provider failed or returned an unusable response |
| `analytics_query_rejected` | 400 | the generated SQL failed the read-only guard on both attempts |
| `analytics_query_failed` | 400 | the generated SQL was refused or failed in the database on both attempts |
| `rate_limited` | 429 | a per-identity request budget was exhausted; `Retry-After` says when to try again |

Notes on the less obvious ones:

- Providers report *exhausted credits* and *throttling* with the same HTTP status
  (429). They need opposite responses, so the platform inspects the provider's
  error code and returns `402` for an empty balance. Retrying an empty balance can
  never succeed, and telling you to retry would be bad advice.
- `document_not_found` is deliberately also the answer for someone else's
  document. A `403` would confirm that the id exists.
- `embedding_dimension_mismatch` fails rather than comparing 768-dimensional
  stored vectors against, say, 1536-dimensional queries. That comparison is
  meaningless, and a silently wrong search result is worse than a clear error.

Responses include `request_id`, which also appears in the logs and in the
`X-Request-ID` header — so a user-visible error can be traced to a log line.

---

## Design decisions worth knowing

- **Nothing is faked.** If the model is unavailable, requests fail with a clear
  error. There is no mock path in production code.
- **Prompts forbid fabrication** and treat document/web/tool text as untrusted
  data rather than instructions. Phase 7 makes that structural: the application's
  own block tags are escaped inside untrusted text, and the analytics function
  cannot read outside its two-table allowlist even if the model is fully
  persuaded.
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
| 3 | RAG: upload, parse, chunk, embed, retrieve | ✅ done |
| 4 | Supabase: schema, Auth, Storage, RLS, persistence | ✅ done |
| 5 | LangGraph workflow + state | ✅ done |
| 6 | Tools & specialist agents + report generation | ✅ done |
| 7 | Security, audit logging, rate limits, SQL safety | ✅ done |
| 8 | React + TypeScript frontend | ⬜ |
| 9 | Tests, Docker, deployment docs | ⬜ |

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — components, request flow, and
  the reasoning behind each phase's choices.
