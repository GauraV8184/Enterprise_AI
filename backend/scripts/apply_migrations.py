"""Apply the SQL files in ``supabase/migrations/`` to the Supabase database.

Why this exists
---------------
The application talks to Supabase through PostgREST, which is exactly right for
user traffic: it means Row Level Security is enforced on every query. But
PostgREST cannot run DDL, and neither the anon nor the service-role API key can
create a table. Schema changes therefore need a real Postgres connection.

This script opens one, applies each migration once, and records what it did in
``public.schema_migrations``, so re-running is safe and the applied set is
inspectable rather than a matter of memory.

Usage
-----
    # from the repository root, after adding SUPABASE_DB_URL to .env
    cd backend
    .\\.venv\\Scripts\\python.exe scripts\\apply_migrations.py

    # re-run everything, even files already applied
    .\\.venv\\Scripts\\python.exe scripts\\apply_migrations.py --force

    # show what has been applied, connect to nothing, change nothing
    .\\.venv\\Scripts\\python.exe scripts\\apply_migrations.py --status

An applied file that no longer matches its recorded checksum is treated as a
failure (exit 1), not a warning: the database and the repository disagree, and
silently ignoring that makes every later diagnosis unreliable. ``--force``
re-runs it deliberately and updates the checksum.

Exit codes: 0 = success, 1 = failure.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

ADVISORY_LOCK_KEY = 8_274_119_003_771

_MIGRATIONS_TABLE = """
create table if not exists public.schema_migrations (
    filename    text primary key,
    applied_at  timestamptz not null default now(),
    checksum    text not null
);
revoke all on public.schema_migrations from anon, authenticated;
"""


def _checksum(sql: str) -> str:
    """Stable fingerprint of a migration's contents.

    Newlines are normalised first: a file that differs only by line endings has
    not actually changed, and flagging it would be noise.
    """
    normalised = sql.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def _discover() -> list[Path]:
    """Every migration, in filename order (hence the 0001-, 0002- prefixes)."""
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"No migrations directory at {MIGRATIONS_DIR}")
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit(f"No .sql files in {MIGRATIONS_DIR}")
    return files


def _connect(settings: Settings):
    try:
        import psycopg
    except ImportError:  # pragma: no cover - dependency guidance, not logic
        raise SystemExit(
            "psycopg is not installed. It is a development dependency:\n"
            "    .\\.venv\\Scripts\\python.exe -m pip install -r requirements-dev.txt"
        ) from None

    if not settings.supabase_db_url:
        raise SystemExit(
            "SUPABASE_DB_URL is not set.\n\n"
            "Copy it from the Supabase dashboard:\n"
            "    Project -> Connect -> Session pooler -> Connection string (URI)\n"
            "It looks like:\n"
            "    postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres\n"
            "Then add it to the repository-root .env file. Never commit it."
        )

    dsn = settings.supabase_db_url.get_secret_value()
    return psycopg.connect(dsn, sslmode="require", autocommit=True)


def _print_status(connection) -> int:
    rows = connection.execute(
        "select filename, applied_at, checksum from public.schema_migrations order by filename"
    ).fetchall()
    if not rows:
        print("  (nothing applied yet)")
        return 0
    for filename, applied_at, checksum in rows:
        print(f"  {filename:<40} {applied_at:%Y-%m-%d %H:%M}  {checksum}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Supabase migrations.")
    parser.add_argument("--force", action="store_true", help="Re-apply every migration.")
    parser.add_argument("--status", action="store_true", help="List applied migrations and exit.")
    args = parser.parse_args(argv)

    settings = Settings()
    files = _discover()
    print(f"\nMigrations directory: {MIGRATIONS_DIR}")
    print(f"Found {len(files)} migration file(s).\n")

    connection = _connect(settings)
    with connection:
        connection.execute("select pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))

        connection.execute(_MIGRATIONS_TABLE)
        applied = dict(
            connection.execute("select filename, checksum from public.schema_migrations").fetchall()
        )

        if args.status:
            print("Applied migrations:")
            return _print_status(connection)

        drift: list[str] = []
        for path in files:
            sql = path.read_text(encoding="utf-8")
            checksum = _checksum(sql)
            name = path.name
            previous = applied.get(name)

            if previous == checksum and not args.force:
                print(f"  skip   {name}  (already applied)")
                continue
            if previous is not None and previous != checksum and not args.force:
                print(f"  WARN   {name}  was applied as {previous} but is now {checksum}.")
                print("         The file changed after it was applied. Re-running it with")
                print("         --force is safe for these idempotent migrations.")
                drift.append(name)
                continue

            verb = "re-run" if previous else "apply"
            print(f"  {verb} {name} ...", end=" ", flush=True)
            try:
                connection.execute(sql)
            except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
                print("FAILED")
                print(f"\n  {type(exc).__name__}: {exc}\n")
                return 1

            connection.execute(
                """
                insert into public.schema_migrations (filename, checksum)
                values (%s, %s)
                on conflict (filename) do update
                    set checksum = excluded.checksum,
                        applied_at = now()
                """,
                (name, checksum),
            )
            print("ok")

        print("\nApplied migrations:")
        _print_status(connection)

        if drift:
            print(f"\nRESULT: {len(drift)} applied migration(s) changed on disk:")
            for name in drift:
                print(f"  - {name}")
            print(
                "  The database no longer matches this repository. Restore the original "
                "file(s), or re-run with --force if the change is intentional."
            )
            return 1

    print("\nRESULT: migrations applied. Verify with:")
    print("  .\\.venv\\Scripts\\python.exe scripts\\check_supabase.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
