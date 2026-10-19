"""Live check of the Supabase project and the Phase 4 schema.

Answers the questions that decide how the integration should be built:

* are the credentials valid, and is each sub-service reachable?
* has the Phase 4 migration been applied yet?
* which JWT signing scheme is in use (this decides how tokens are verified)?
* does the storage bucket exist?

It never prints a key. Run it from the ``backend`` directory:

    .\\.venv\\Scripts\\python.exe scripts\\check_supabase.py

Exit codes: 0 = ready, 1 = something to fix.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import Settings  # noqa: E402

TIMEOUT = 20.0


def _line(label: str, value: object) -> None:
    print(f"  {label:<30} {value}")


def _mask(value: str | None) -> str:
    """Show a key's role and length without ever revealing it."""
    if not value:
        return "(not set)"
    return f"set ({len(value)} chars)"


def _rest_status(response: httpx.Response) -> str:
    """Compact description of a PostgREST response."""
    try:
        body: Any = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"

    if isinstance(body, dict):
        code = body.get("code")
        message = body.get("message")
        if code:
            return f"HTTP {response.status_code} {code}: {str(message)[:70]}"
        if not body:
            return f"HTTP {response.status_code} (empty)"
    if isinstance(body, list):
        return f"HTTP {response.status_code} (list, {len(body)} rows)"
    return f"HTTP {response.status_code}"


def main() -> int:
    settings = Settings()
    blockers: list[str] = []

    print("\nConfiguration")
    print("-" * 62)
    _line("SUPABASE_URL", settings.supabase_url or "(not set)")
    _line(
        "anon key",
        _mask(
            settings.supabase_anon_key.get_secret_value() if settings.supabase_anon_key else None
        ),
    )
    _line(
        "service-role key",
        _mask(
            settings.supabase_service_role_key.get_secret_value()
            if settings.supabase_service_role_key
            else None
        ),
    )
    _line("document store in use", "supabase" if settings.use_supabase_store else "sqlite (local)")
    _line("auth backend in use", "supabase" if settings.use_supabase_auth else "dev stub")
    _line("documents bucket", settings.supabase_documents_bucket)

    if not settings.supabase_configured:
        print("\nRESULT: not configured.")
        print("  Set SUPABASE_URL and SUPABASE_ANON_KEY in the repository-root .env file.")
        return 1

    anon = settings.supabase_anon_key.get_secret_value() if settings.supabase_anon_key else ""
    service = (
        settings.supabase_service_role_key.get_secret_value()
        if settings.supabase_service_role_key
        else ""
    )
    anon_headers = {"apikey": anon, "Authorization": f"Bearer {anon}"}

    with httpx.Client(timeout=TIMEOUT) as client:
        # -- 1. Project reachable, and schema state ------------------------
        print("\n1) REST API (PostgREST)")
        print("-" * 62)
        try:
            # The PostgREST root document requires elevated privileges, so the auth
            # service is used as the liveness probe instead - it answers with the
            # project's GoTrue version and is safe to call with the public key.
            alive = client.get(
                f"{settings.supabase_service_url('auth')}/health", headers=anon_headers
            )
            _line("GET /auth/v1/health", f"HTTP {alive.status_code}")
            if alive.status_code == 200:
                _line("auth server", alive.json().get("name", "?"))
        except httpx.HTTPError as exc:
            print(f"  FAILED to reach the project: {type(exc).__name__}")
            return 1
        if alive.status_code != 200:
            blockers.append("The project did not answer on /auth/v1/health. Is it paused?")
            print("\nRESULT: FAILED - the project is not answering.")
            return 1

        # -- 1b. Service-role key -----------------------------------------
        # Checked first because the schema probes below prefer it: it always has
        # access, so a missing table reports as "missing" rather than as a
        # permission error.
        print("\n1b) Service-role key")
        print("-" * 62)
        admin_headers: dict[str, str] | None = None
        if not settings.supabase_admin_configured:
            _line("check", "skipped (not set)")
        else:
            admin_headers = {"apikey": service, "Authorization": f"Bearer {service}"}
            try:
                root = client.get(
                    f"{settings.supabase_service_url('rest')}/", headers=admin_headers
                )
                _line("GET /rest/v1/ with admin key", f"HTTP {root.status_code}")
                if root.status_code == 200:
                    print("  This key bypasses Row Level Security. It must never be shipped to")
                    print("  a browser, and is used only by admin scripts.")
                else:
                    blockers.append(
                        "SUPABASE_SERVICE_ROLE_KEY was rejected. Admin scripts need a valid one."
                    )
                    admin_headers = None
            except httpx.HTTPError as exc:
                _line("GET /rest/v1/ with admin key", f"FAILED {type(exc).__name__}")
                admin_headers = None

        # -- 1c. Tables ----------------------------------------------------
        print("\n1c) Tables")
        print("-" * 62)
        schema_probe_headers = admin_headers or anon_headers
        schema_probe_label = "service-role" if admin_headers else "anon"

        for table in ("documents", "chunks"):
            probe = client.get(
                f"{settings.supabase_service_url('rest')}/{table}?limit=1",
                headers=schema_probe_headers,
            )
            _line(f"table '{table}' ({schema_probe_label} key)", _rest_status(probe))
            if probe.status_code == 401:
                blockers.append(f"Reading '{table}' was rejected. Check SUPABASE_URL and the keys.")
            elif probe.status_code == 404:
                blockers.append(f"The '{table}' table does not exist yet - apply the migration.")
            elif probe.status_code == 200:
                _line("  rows", len(probe.json()))

        # -- 1d. Row Level Security ---------------------------------------
        # The very same query, this time as the public `anon` role. It must see
        # nothing: either refused outright (anon holds no table grants, which is
        # what migration 0001 arranges) or an empty result set.
        print("\n1d) Row Level Security (probing as the public anon role)")
        print("-" * 62)
        for table in ("documents", "chunks"):
            probe = client.get(
                f"{settings.supabase_service_url('rest')}/{table}?limit=1",
                headers=anon_headers,
            )
            body = probe.text.lower()
            if probe.status_code in (401, 403) or "permission denied" in body:
                _line(f"anon reading '{table}'", "denied (correct)")
            elif probe.status_code == 404:
                _line(f"anon reading '{table}'", "table missing")
            elif probe.status_code == 200:
                rows = probe.json()
                if rows:
                    _line(f"anon reading '{table}'", f"LEAK - {len(rows)} row(s) visible")
                    blockers.append(
                        f"The public anon role can read rows from '{table}'. Check the "
                        "policies and grants from migration 0001."
                    )
                else:
                    _line(f"anon reading '{table}'", "0 rows visible (correct)")

        # -- 2. Vector search function ------------------------------------
        # Existence is checked with an administrative key, because the anon key
        # is (correctly) not granted EXECUTE. That the function *respects RLS* is
        # a different claim, tested against a real signed-in user later - an
        # empty table would make a leak look identical to correct behaviour.
        print("\n2) Vector search RPCs")
        print("-" * 62)
        rpc_headers = {**(admin_headers or anon_headers), "Content-Type": "application/json"}

        probe = client.post(
            f"{settings.supabase_service_url('rest')}/rpc/match_document_chunks",
            headers=rpc_headers,
            json={"query_embedding": [0.0] * 768, "match_count": 1},
        )
        _line("match_document_chunks", _rest_status(probe))
        if probe.status_code == 404:
            blockers.append(
                "The match_document_chunks() function does not exist - apply the migration."
            )
        elif probe.status_code == 200:
            _line("  rows returned", len(probe.json()))

        dims = client.post(
            f"{settings.supabase_service_url('rest')}/rpc/embedding_dimensions",
            headers=rpc_headers,
            json={},
        )
        if dims.status_code == 200:
            _line("embedding_dimensions()", dims.json())
        else:
            _line("embedding_dimensions()", _rest_status(dims))
            if dims.status_code == 404:
                blockers.append(
                    "The embedding_dimensions() function does not exist - apply the migration."
                )

        # -- 3. Auth service and signing scheme ---------------------------
        print("\n3) Auth service")
        print("-" * 62)
        try:
            auth = client.get(
                f"{settings.supabase_service_url('auth')}/settings", headers=anon_headers
            )
            _line("GET /auth/v1/settings", f"HTTP {auth.status_code}")
            if auth.status_code == 200:
                data = auth.json()
                _line("email signup enabled", data.get("external", {}).get("email", "?"))
                providers = [name for name, on in (data.get("external") or {}).items() if on]
                _line("enabled providers", ", ".join(providers) or "(none)")
        except httpx.HTTPError as exc:
            _line("GET /auth/v1/settings", f"FAILED {type(exc).__name__}")

        jwks_url = f"{settings.supabase_service_url('auth')}/.well-known/jwks.json"
        try:
            jwks = client.get(jwks_url, headers=anon_headers)
            if jwks.status_code == 200:
                keys = jwks.json().get("keys") or []
                _line("JWKS keys published", len(keys))
                if keys:
                    _line("signature algorithm", keys[0].get("alg") or "(unspecified)")
                    print("\n  Asymmetric signing is enabled, so access tokens can be verified")
                    print("  locally against the published public keys - no shared secret needed.")
                else:
                    print("\n  No public keys published: this project uses a shared HS256 secret.")
                    print("  Local verification then needs SUPABASE_JWT_SECRET, or a fallback")
                    print("  round-trip to the auth server per request.")
            else:
                _line("JWKS endpoint", f"HTTP {jwks.status_code}")
        except httpx.HTTPError as exc:
            _line("JWKS endpoint", f"FAILED {type(exc).__name__}")

        # -- 4. Storage ---------------------------------------------------
        print("\n4) Storage")
        print("-" * 62)
        if not settings.supabase_admin_configured:
            _line("buckets", "skipped (no service-role key)")
        else:
            admin_headers = {"apikey": service, "Authorization": f"Bearer {service}"}
            try:
                buckets = client.get(
                    f"{settings.supabase_service_url('storage')}/bucket", headers=admin_headers
                )
                if buckets.status_code == 200:
                    names = [item.get("name") for item in buckets.json()]
                    _line("existing buckets", ", ".join(names) or "(none)")
                    present = settings.supabase_documents_bucket in names
                    _line(
                        f"'{settings.supabase_documents_bucket}' bucket",
                        "present" if present else "missing",
                    )
                    if not present:
                        blockers.append(
                            f"The '{settings.supabase_documents_bucket}' storage bucket does not "
                            "exist yet - apply the storage migration."
                        )
                else:
                    _line("GET /storage/v1/bucket", f"HTTP {buckets.status_code}")
            except httpx.HTTPError as exc:
                _line("GET /storage/v1/bucket", f"FAILED {type(exc).__name__}")

    print("\n" + "=" * 62)
    if blockers:
        print(f"RESULT: credentials work, {len(blockers)} thing(s) still to do:")
        for blocker in blockers:
            print(f"  - {blocker}")
        print("\n  Apply the SQL in supabase/migrations/ - see supabase/README.md.")
        return 1

    print("RESULT: OK - credentials are valid and the Phase 4 schema is in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
