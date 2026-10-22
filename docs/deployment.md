# Deployment — Enterprise AI Intelligence Platform

This is the Phase 9 deployment path. It packages the application as two
containers:

- `backend` — FastAPI/uvicorn, one worker, non-root UID 10001.
- `frontend` — the Vite production bundle served by unprivileged nginx on port
  8080, with a same-origin `/api` reverse proxy.

Supabase remains managed. There is no Postgres or Redis container in this stack.
Run Ollama on the host only as an optional local override; it is not part of the
production Compose stack.

## Prerequisites

- Docker Engine/Desktop 29+ with Compose v2 (`docker compose version`).
- A Supabase project with the migrations from [`supabase/README.md`](../supabase/README.md)
  applied, including the `documents` Storage bucket and RLS policies.
- A root `.env` file. It is git-ignored and is read both by Compose for
  interpolation and by the backend container at runtime.
- At least one LLM option:
  - a hosted OpenAI-compatible endpoint (`LLM_PROVIDER=openai`), or
  - Ollama reachable from the container (see the local override below).
- `TAVILY_API_KEY` if web research or reports are required.

Start from [`.env.example`](../.env.example), never from a committed secret.
The values that matter for containers are:

```dotenv
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<public-anon-key>
OPENAI_API_KEY=<hosted-provider-key>          # if LLM_PROVIDER=openai
TAVILY_API_KEY=<tavily-key>
COMPOSE_ALLOWED_HOSTS=localhost,127.0.0.1     # optional; see production notes
COMPOSE_CORS_ORIGINS=http://localhost:8080    # optional; see production notes
FRONTEND_PORT=8080                            # optional; default 8080
```

`SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_DB_URL` are used by the live admin
scripts on the host. Compose deliberately sets both to empty strings inside the
backend container: request-serving code never needs the service-role key.

## Build and run

From the repository root:

```powershell
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

Open <http://localhost:8080>. The public health check is
<http://localhost:8080/api/health>; it should return:

```json
{"status":"ok","service":"Enterprise AI Intelligence Platform","version":"0.1.0","environment":"production"}
```

Useful commands:

```powershell
docker compose logs -f backend
docker compose logs -f frontend
docker compose exec backend id
docker compose exec frontend id
docker compose down
```

The Compose file forces `ENVIRONMENT=production`, disables debug mode, and
selects Supabase for auth, document storage, and file storage. `/api/docs` and
`/api/openapi.json` are therefore disabled. `/docs` is served by the SPA
fallback, not by FastAPI.

## Local Ollama override

If Ollama is running on the host with `llama3.1:8b` and `nomic-embed-text`:

```powershell
docker compose -f compose.yaml -f compose.ollama.yaml up -d --build
```

The override points the backend at
`http://host.docker.internal:11434/v1` and adds the Linux `host-gateway`
mapping. On Docker Desktop this is usually enough. If the container still gets a
connection error, Ollama is probably listening only on `127.0.0.1`. Bind it to
`0.0.0.0:11434` (for example, set `OLLAMA_HOST=0.0.0.0:11434` for the Ollama
service and restart it), or use a hosted provider.

The current local model is slow (roughly 5–6 tokens/second). The report path can
take two to three minutes, which is why nginx uses a 360-second proxy timeout.
Keep `LLM_TIMEOUT_SECONDS=300` unless the provider is faster.

## Production notes

The Compose file is already production-shaped, but a real deployment still needs:

1. **A production Supabase project.** Apply migrations, create the private
   `documents` bucket, and verify RLS with the live checks in
   `backend/scripts/` (`check_rls.py`, `check_storage.py`) from the host.
2. **A hosted LLM or a reachable inference server.** For a hosted
   OpenAI-compatible API set `LLM_PROVIDER=openai`, `OPENAI_API_KEY`,
   `OPENAI_MODEL`, and leave `OPENAI_BASE_URL` empty unless the provider uses a
   custom endpoint. Do not ship the host-Ollama override to production.
3. **The real hostname.** Set:

   ```dotenv
   COMPOSE_ALLOWED_HOSTS=app.example.com
   COMPOSE_CORS_ORIGINS=https://app.example.com
   ```

   `COMPOSE_ALLOWED_HOSTS` feeds `TrustedHostMiddleware`; a forged `Host` header
   is rejected before routing. The frontend and API share an origin, so CORS is
   mostly defence in depth, not the primary access path.
4. **TLS at the edge.** Put Caddy, nginx, HAProxy, or a cloud load balancer in
   front of port 8080 and terminate HTTPS there. Forward the original host and
   `X-Forwarded-Proto`; add HSTS at the TLS terminator. Only the frontend port
   needs to be reachable; the backend is not published to the host.
5. **Secret storage.** On a real host, inject `.env` from a secret manager or a
   root-owned file rather than committing it. The anon key is public by design
   and is embedded in the frontend build. The service-role key, Tavily key, and
   LLM key must never appear in a `VITE_*` variable or a frontend build arg.
6. **Image provenance.** Build from a reviewed commit, tag images with the commit
   SHA, and keep the previous tag for rollback. The Dockerfiles run the backend
   as UID 10001 and nginx as UID 101, and Compose enables
   `no-new-privileges`.

The backend intentionally runs with **one worker**. Rate limits, audit batching,
and some caches are process-local. Add Redis (or another shared store) before
running multiple workers or replicas. This stack does not include Kubernetes or
an autoscaling layer.

## Smoke test checklist

After `docker compose up -d`:

```powershell
# 1. Both services are healthy
docker compose ps

# 2. API health and security headers through nginx
Invoke-RestMethod http://localhost:8080/api/health
Invoke-WebRequest http://localhost:8080/api/health -UseBasicParsing |
  Select-Object -ExpandProperty Headers

# 3. SPA fallback works for a client-side route
Invoke-WebRequest http://localhost:8080/reports -UseBasicParsing |
  Select-Object StatusCode, @{n='Type';e={$_.Headers['Content-Type']}}

# 4. Static assets are cacheable, the shell is not
Invoke-WebRequest http://localhost:8080/ -UseBasicParsing |
  Select-Object -ExpandProperty Headers
```

Then use the UI: sign in through Supabase, upload a small PDF, ask a grounded
question, and run a short web-research report. A successful health check does not
prove the LLM or Tavily paths work; the UI smoke test does.

For a direct provider check from inside the backend container (Ollama example):

```powershell
docker compose exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=5).status)"
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `502 Bad Gateway` from nginx | Backend container is unhealthy or still starting | `docker compose ps`, then `docker compose logs backend` |
| `504` on a report request | nginx or the LLM timed out | Keep `LLM_TIMEOUT_SECONDS=300`; nginx is set to 360s. A slower model needs a corresponding increase in `frontend/nginx.conf` |
| `413 Request Entity Too Large` | Upload exceeds nginx's 25 MB limit or `MAX_UPLOAD_MB` | The default both are 25 MB. If you raise the app limit, also raise `client_max_body_size` in `frontend/nginx.conf` and rebuild |
| `not_authenticated` or `invalid_token` | Production requires Supabase Auth; the frontend was built with the wrong URL/anon key | Check `SUPABASE_URL`/`SUPABASE_ANON_KEY`, then rebuild the frontend image |
| LLM connection failure in a container | `OPENAI_BASE_URL` points at `127.0.0.1`, which is the container itself | Use the Ollama override or a hosted provider URL; for host services use `host.docker.internal` |
| `web_search_not_configured` | `TAVILY_API_KEY` is absent inside the backend | Set it in root `.env` and recreate the backend |
| Frontend loads but API calls fail | `ALLOWED_HOSTS` does not include the hostname | Set `COMPOSE_ALLOWED_HOSTS` and recreate the backend |
| Port 8080 is already in use | Another local process owns it | Set `FRONTEND_PORT=8081` in `.env` and recreate |

## What this stack does not include

- Redis, a task queue, or horizontal worker scaling.
- A self-hosted Supabase instance.
- Ollama in the default Compose stack (the override is local-only).
- A TLS terminator. Run one in front of the frontend for any non-local
  deployment.
- Automated backups. Supabase's backup policy is the database backup; the
  `documents` bucket needs its own retention/backup decision.
