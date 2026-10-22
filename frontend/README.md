# Enterprise AI — frontend

The React 19 + TypeScript client for the Enterprise AI Intelligence Platform.
It is a single-page app built with Vite and talks only to the FastAPI backend.

The browser never receives the Supabase service-role key or any LLM credential.
Supabase Auth supplies a short-lived access token; Row Level Security and the
backend's own checks enforce ownership.

## Screens

| Route | Purpose |
|---|---|
| `/` | Live platform overview: health, documents, model status, and quick actions |
| `/chat` | Direct, explicitly ungrounded conversation with the configured model |
| `/documents` | Upload, index, delete, semantically search, and ask grounded questions |
| `/ask` | Supervisor workflow with routing metadata and evidence panels |
| `/reports` | Markdown reports from documents, analytics, and/or web research |
| `/settings` | Session, identity, API, model, upload limits, and local preferences |
| `*` | 404 screen |

Evidence is rendered next to the answer: document citations `[1]`, web results
`W1`, and the SQL statement plus rows for analytics. Reports can be copied,
downloaded as `.md`, or printed. Markdown is rendered without raw HTML.

## Requirements

- Node.js 20 or newer (developed on 22.17)
- The backend running on `http://127.0.0.1:8000` for the default dev proxy
- A Supabase project with Email auth if you want to use real accounts

## Setup

```powershell
cd frontend
npm install
Copy-Item .env.example .env.local
```

Then edit `frontend/.env.local`:

```dotenv
VITE_API_BASE_URL=/api

# Leave both blank to use the development X-User-Id identity stub.
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_ANON_KEY=<public-anon-key>

VITE_DEV_USER_ID=dev-user
VITE_MAX_UPLOAD_MB=25
```

Only variables prefixed with `VITE_` reach the browser. Never put
`SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `TAVILY_API_KEY`, or
`SUPABASE_DB_URL` in this file.

### Authentication modes

- **Both Supabase values set** — the app uses Supabase Auth. Requests send
  `Authorization: Bearer <access-token>`. The backend must also use Supabase
  Auth, and the account's email must be confirmed if confirmation is enabled.
- **Both values blank** — the app sends `X-User-Id: <VITE_DEV_USER_ID>` or the
  user id saved in Settings. This is a development stub, not a security
  control, and the backend must allow it (`ENVIRONMENT=development` or `test`,
  and a compatible `AUTH_BACKEND`).
- **Only one value set** — the app shows a configuration error instead of
  silently falling back to the development identity.

`VITE_API_BASE_URL` defaults to `/api`. In development Vite proxies `/api`,
`/docs`, and `/openapi.json` to `http://127.0.0.1:8000`, so the browser stays on
one origin. For a separately hosted backend, set it to the absolute API URL
(for example `https://api.example.com/api`) and add the frontend origin to the
backend's `CORS_ORIGINS`.

## Run locally

Start the backend from the repository root first:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Then start the frontend:

```powershell
cd frontend
npx vite --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. `npm run dev` also works; on npm 11, passing
`--host` through `npm run dev` can be parsed by npm rather than Vite, so use
`npx vite` when you need explicit host flags.

## Checks and build

```powershell
npm run typecheck   # tsc project build, no emit
npm run lint        # eslint
npm run build       # typecheck + production bundle in dist/
npm run preview     # serve dist/ on http://127.0.0.1:4173
```

`npm run build` prints the bundle sizes. Routes are lazy-loaded so the initial
payload does not include every screen. The Phase 9 frontend image serves the
generated files through unprivileged nginx; the same files can also be served by
any static host or CDN.

## Validation notes

Phase 8 was exercised in a browser against the live API, Supabase Auth and a
local Ollama `llama3.1:8b`:

- Supabase sign-in, session restore, the signed-out gate, and sign-out worked;
  the duplicate Supabase client warning was gone after moving the client to
  module scope.
- Upload and indexing, semantic search, grounded Q&A with a document citation,
  and deletion worked.
- A documents-only report returned the correct Project Atlas launch date,
  budget, sponsor, gating reviews and evidence.
- A simple analytics report returned the executed SQL and four result rows in
  the analytics evidence panel.
- Web research without `TAVILY_API_KEY` returned the typed
  `503 web_search_not_configured`; the report screen showed one error notice,
  its request id and a retry action.
- Direct chat, lazy route loading, the `/ask?q=…` hand-off auto-running once and
  clearing the query, and Documents → supervisor client-side navigation worked.

Observed limits during that run: the local model classified two analytics-style
supervisor prompts as `report`, and one open-ended analytics report returned the
typed `analytics_query_failed` error instead of writing a report. The UI showed
the actual route or typed error; it did not substitute evidence. A stronger
model is the right fix for those model-quality cases.

## Source layout

```
src/
├── api/
│   ├── client.ts       Typed fetch wrapper and auth-header integration
│   ├── errors.ts       ApiError, response parsing, user-facing messages
│   └── types.ts        Browser-side mirrors of the backend Pydantic schemas
├── auth/
│   ├── AuthProvider.tsx  Supabase session, dev identity, API client lifecycle
│   ├── AuthScreen.tsx    Sign in / create account
│   └── ...
├── components/         Shell, evidence, Markdown, dialogs, shared states
├── hooks/              Health, document title, auto-resize
├── lib/                Runtime config, formatting, file validation
├── pages/              One component per route
├── App.tsx             Auth gate, lazy routes, error boundary
├── main.tsx            React root
└── styles.css          Responsive visual system + print styles
```

## Design choices

- **No UI framework.** Small shared components and CSS keep the dependency
  surface and the bundle smaller, and make the evidence layouts explicit.
- **Typed errors.** Non-2xx responses become `ApiError` values carrying the
  backend's stable `code`, `message`, `details`, `request_id`, and
  `retry_after_seconds`. Screens show actionable notices instead of raw JSON.
- **Server contracts, not guesses.** `api/types.ts` mirrors the public schemas;
  the browser never receives owner ids, storage paths, or credentials.
- **Explicit grounding.** The direct chat screen says it is ungrounded. Document
  and supervisor answers show citations; a failed or empty source set is shown
  as such, not filled in by the model.
- **Safe Markdown.** `react-markdown` + `remark-gfm` render tables and links,
  but raw HTML is deliberately not enabled. External links open with
  `noopener noreferrer`.
- **Responsive shell.** A persistent sidebar on wide screens becomes an
  off-canvas menu on narrow screens; dialogs, tables, evidence cards, and print
  output have their own breakpoints.

## Known limits

- Live web research requires `TAVILY_API_KEY` on the backend. Without it,
  research requests return the typed `503 web_search_not_configured` error,
  which the UI displays with the backend's guidance.
- The development `X-User-Id` identity is not authentication. Use Supabase Auth
  for anything shared or deployed.
- Docker packaging and production hosting are covered in the root
  [`docs/deployment.md`](../docs/deployment.md); the UI itself is still not a
  security boundary.
