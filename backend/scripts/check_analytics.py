"""Prove, against the live project, that the demo analytics path behaves.

Unit tests can prove the guard's policy and the SQLite executor. Only the live
project can prove that the migration's SQL seed produces the same rows as
``app/database/demo_data.py``, that the database itself refuses writes when the
application's guard is bypassed, and that a runaway query is actually cancelled
by the function's ``statement_timeout``.

What it checks, end to end:

  1. two throwaway users can be created and signed in
  2. the seeded demo dataset matches the Python seed exactly (products, row
     count, total revenue, per-region revenue - computed on both sides)
  3. every authenticated caller reads the same demo rows (the read-all policy)
  4. the row cap holds however the query is written, and is clamped to 200
  5. the Python guard refuses writes without touching the network
  6. the database refuses writes, a second statement, and disallowed keywords
     even when the application is bypassed and the RPC is called directly
  7. an unknown column fails with a repair hint meant for the model, not a guess
  8. a query that overruns 5 seconds is cancelled by ``statement_timeout``
  9. generated SQL cannot read anything except the two demo tables - not the
     caller's documents, not another schema, not a system catalog - and the
     function really runs as the restricted ``demo_reader`` role
 10. the anon role cannot call the function at all

Every user it creates is deleted at the end, including on failure.

Run it from ``backend`` (migrations 0005 and 0006 must already be applied):

    .\\.venv\\Scripts\\python.exe scripts\\check_analytics.py

Exit codes: 0 = every check passed, 1 = something failed or is not configured.
"""

from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.demo_data import (  # noqa: E402
    DEMO_ORDER_COUNT,
    DEMO_PRODUCTS,
    DEMO_REGIONS,
    demo_sales_rows,
)
from app.database.errors import (  # noqa: E402
    AnalyticsQueryFailedError,
    AnalyticsQueryRejectedError,
)
from app.database.postgrest import PostgrestClient  # noqa: E402
from app.database.supabase_repository import SupabaseDocumentRepository  # noqa: E402
from app.tools.analytics import run_analytics_query  # noqa: E402

TIMEOUT = 30.0


@dataclass
class Report:
    passed: int = 0
    failed: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.passed += 1
        print(f"  [pass] {message}")

    def fail(self, message: str, detail: str = "") -> None:
        self.failed.append(message)
        print(f"  [FAIL] {message}")
        if detail:
            print(f"         {detail[:220]}")

    def check(self, condition: bool, message: str, detail: str = "") -> bool:
        if condition:
            self.ok(message)
        else:
            self.fail(message, detail)
        return condition


class Client:
    """The admin, auth and REST calls this check needs, as the app makes them.

    The key in ``apikey`` and the token in ``Authorization`` are kept separate
    on purpose: the same code path the application uses identifies the caller by
    the token while presenting the public key, so every check below runs with
    the privileges a real request would have.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._anon = settings.supabase_anon_key.get_secret_value()
        self._service = (
            settings.supabase_service_role_key.get_secret_value()
            if settings.supabase_service_role_key
            else ""
        )
        self._http = httpx.Client(timeout=TIMEOUT)

    def close(self) -> None:
        self._http.close()

    def _url(self, service: str, path: str) -> str:
        return f"{self._settings.supabase_service_url(service)}{path}"  # type: ignore[arg-type]

    def _headers(self, token: str | None) -> dict[str, str]:
        return {
            "apikey": self._anon,
            "Authorization": f"Bearer {token or self._anon}",
            "Content-Type": "application/json",
        }

    def create_user(self, email: str, password: str) -> str:
        response = self._http.post(
            self._url("auth", "/admin/users"),
            headers={"apikey": self._service, "Authorization": f"Bearer {self._service}"},
            json={"email": email, "password": password, "email_confirm": True},
        )
        response.raise_for_status()
        return response.json()["id"]

    def sign_in(self, email: str, password: str) -> str:
        response = self._http.post(
            self._url("auth", "/token?grant_type=password"),
            headers=self._headers(None),
            json={"email": email, "password": password},
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def delete_user(self, user_id: str) -> None:
        response = self._http.delete(
            self._url("auth", f"/admin/users/{user_id}"),
            headers={"apikey": self._service, "Authorization": f"Bearer {self._service}"},
        )
        response.raise_for_status()

    def rpc(self, name: str, token: str | None, body: dict[str, Any]) -> httpx.Response:
        return self._http.post(
            self._url("rest", f"/rpc/{name}"),
            headers=self._headers(token),
            json=body,
        )

    def repository(self, token: str) -> SupabaseDocumentRepository:
        """The application's own repository class, scoped to one caller."""
        return SupabaseDocumentRepository(
            PostgrestClient(
                base_url=self._settings.supabase_service_url("rest"),
                api_key=self._anon,
                access_token=token,
                client=self._http,
            )
        )


def error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def run(settings: Settings) -> int:
    report = Report()
    client = Client(settings)
    suffix = uuid.uuid4().hex[:10]
    users: dict[str, str] = {}
    password = f"analytics-check-{suffix}"

    expected_total = round(sum(row[5] for row in demo_sales_rows()), 2)
    expected_by_region: dict[str, float] = dict.fromkeys(DEMO_REGIONS, 0.0)
    for row in demo_sales_rows():
        expected_by_region[row[2]] = round(expected_by_region[row[2]] + row[5], 2)

    print("\nAnalytics configuration")
    print("-" * 62)
    print(f"  {'project':<22} {settings.supabase_url}")
    print(f"  {'analytics row cap':<22} {settings.analytics_max_rows}")
    print(f"  {'expected demo orders':<22} {DEMO_ORDER_COUNT}")
    print(f"  {'expected total revenue':<22} {expected_total:.2f}")

    try:
        print("\n1) Create two throwaway users")
        print("-" * 62)
        for label in ("a", "b"):
            email = f"analytics-check-{label}-{suffix}@example.com"
            users[label] = client.create_user(email, password)
            report.ok(f"created user {label} ({users[label][:8]}...)")

        tokens = {
            label: client.sign_in(f"analytics-check-{label}-{suffix}@example.com", password)
            for label in users
        }
        for label in tokens:
            report.check(bool(tokens[label]), f"user {label} signed in and received a token")

        alice_token = tokens["a"]
        alice_repo = client.repository(alice_token)

        print("\n2) The SQL seed matches app/database/demo_data.py exactly")
        print("-" * 62)
        counts = run_analytics_query(
            alice_repo,
            sql=(
                "select (select count(*) from demo_products) as products, "
                "(select count(*) from demo_sales_orders) as orders"
            ),
            max_rows=10,
        )
        report.check(
            counts.rows == [{"products": len(DEMO_PRODUCTS), "orders": DEMO_ORDER_COUNT}],
            f"table sizes are {len(DEMO_PRODUCTS)} products and {DEMO_ORDER_COUNT} orders",
            str(counts.rows),
        )

        products = run_analytics_query(
            alice_repo,
            sql="select id, name, category, unit_price from demo_products order by id",
            max_rows=10,
        )
        expected_products = [
            {
                "id": product.id,
                "name": product.name,
                "category": product.category,
                "unit_price": product.unit_price,
            }
            for product in DEMO_PRODUCTS
        ]
        same_products = len(products.rows) == len(expected_products) and all(
            row["id"] == expected["id"]
            and row["name"] == expected["name"]
            and row["category"] == expected["category"]
            and abs(float(row["unit_price"]) - expected["unit_price"]) < 0.001
            for row, expected in zip(products.rows, expected_products, strict=False)
        )
        report.check(
            same_products,
            "the product table matches the Python constants",
            str(products.rows),
        )

        totals = run_analytics_query(
            alice_repo,
            sql="select round(sum(revenue), 2) as total from demo_sales_orders",
            max_rows=10,
        )
        actual_total = float(totals.rows[0]["total"]) if totals.rows else -1.0
        report.check(
            abs(actual_total - expected_total) < 0.01,
            f"total revenue matches the Python seed ({expected_total:.2f})",
            f"database returned {actual_total}",
        )

        by_region = run_analytics_query(
            alice_repo,
            sql=(
                "select region, round(sum(revenue), 2) as revenue "
                "from demo_sales_orders group by region order by region"
            ),
            max_rows=10,
        )
        actual_by_region = {
            str(row["region"]): round(float(row["revenue"]), 2) for row in by_region.rows
        }
        report.check(
            actual_by_region == expected_by_region,
            "per-region revenue matches the Python seed",
            f"database {actual_by_region} vs python {expected_by_region}",
        )

        print("\n3) Every authenticated caller reads the same demo rows")
        print("-" * 62)
        bob_count = run_analytics_query(
            client.repository(tokens["b"]),
            sql="select count(*) as orders from demo_sales_orders",
            max_rows=10,
        )
        report.check(
            bob_count.rows == [{"orders": DEMO_ORDER_COUNT}],
            f"a second user reads all {DEMO_ORDER_COUNT} demo orders",
            str(bob_count.rows),
        )

        print("\n4) The row cap holds however the query is written")
        print("-" * 62)
        capped = run_analytics_query(alice_repo, sql="select * from demo_sales_orders", max_rows=5)
        report.check(
            capped.row_count == 5 and capped.truncated,
            "the cap returns 5 rows and reports truncation",
            f"rows={capped.row_count} truncated={capped.truncated}",
        )
        sneaky = run_analytics_query(
            alice_repo, sql="select * from demo_sales_orders limit 1000", max_rows=5
        )
        report.check(
            sneaky.row_count == 5,
            "an inner LIMIT cannot defeat the outer cap",
            f"rows={sneaky.row_count}",
        )
        oversized = run_analytics_query(
            alice_repo, sql="select * from demo_sales_orders", max_rows=500
        )
        report.check(
            oversized.row_count == 200 and oversized.truncated,
            "a cap above the hard limit is clamped to 200",
            f"rows={oversized.row_count} truncated={oversized.truncated}",
        )

        print("\n5) The Python guard refuses writes before any request is made")
        print("-" * 62)
        for statements in (
            "drop table demo_products",
            "select 1; delete from demo_sales_orders",
            "insert into demo_products values (9, 'x', 'y', 1)",
        ):
            try:
                run_analytics_query(alice_repo, sql=statements, max_rows=5)
                report.fail(f"the guard allowed {statements!r}")
            except AnalyticsQueryRejectedError as exc:
                report.ok(f"refused locally: {exc.message}")

        print("\n6) The database refuses what the guard refuses, even when bypassed")
        print("-" * 62)
        for label, statements in (
            ("an INSERT", "insert into demo_products values (9, 'x', 'y', 1)"),
            ("a second statement", "select 1; select 2"),
            ("a DROP", "drop table demo_products"),
            ("a keyword inside a literal", "select 1 where 'delete' = 'delete'"),
        ):
            response = client.rpc("run_demo_query", alice_token, {"sql": statements, "max_rows": 5})
            body = error_body(response)
            report.check(
                response.status_code >= 400 and str(body.get("code")) == "P0001",
                f"{label} is refused by the SQL function",
                f"HTTP {response.status_code} {body.get('message', response.text[:160])}",
            )

        print("\n7) A broken column fails with a repair hint, not a guess")
        print("-" * 62)
        try:
            run_analytics_query(alice_repo, sql="select revenu from demo_sales_orders", max_rows=5)
            report.fail("a query with an unknown column succeeded")
        except AnalyticsQueryFailedError as exc:
            report.check(
                "revenu" in exc.repair_hint,
                "the failure carries the column name for the repair attempt",
                exc.repair_hint,
            )

        print("\n8) A runaway query is cancelled by the function timeout")
        print("-" * 62)
        started = time.perf_counter()
        try:
            run_analytics_query(alice_repo, sql="select pg_sleep(15)", max_rows=5)
            elapsed = time.perf_counter() - started
            report.fail(f"pg_sleep(15) returned instead of being cancelled ({elapsed:.1f}s)")
        except AnalyticsQueryFailedError as exc:
            elapsed = time.perf_counter() - started
            report.check(
                elapsed < 12,
                f"pg_sleep(15) was cancelled after {elapsed:.1f}s by statement_timeout",
                exc.repair_hint,
            )

        print("\n9) Generated SQL can read only the demo tables")
        print("-" * 62)
        for label, statements in (
            ("the caller's documents", "select * from public.documents"),
            ("the caller's chunks", "select count(*) as n from public.chunks"),
            (
                "a CTE over documents",
                "with leaked as (select * from public.documents) select count(*) as n from leaked",
            ),
            ("a system catalog", "select rolname from pg_catalog.pg_roles"),
        ):
            try:
                run_analytics_query(alice_repo, sql=statements, max_rows=5)
                report.fail(f"{label} was readable through the analytics tool")
            except AnalyticsQueryRejectedError as exc:
                # The database may refuse during EXPLAIN (permission denied on
                # documents/chunks) or after walking the plan (a catalog). Both
                # are the same client-safe allowlist rejection, and the wording
                # differs, so assert the guarantee rather than one exact string.
                expected = "demo" in exc.message.lower()
                report.check(
                    expected,
                    f"{label} is refused by the database",
                    exc.message,
                )
            except AnalyticsQueryFailedError as exc:
                # A permission error is still a refusal; surface it rather than
                # pretending the allowlist produced the friendly message.
                report.fail(f"{label} failed instead of being rejected", exc.repair_hint)

        direct = client.rpc(
            "run_demo_query", alice_token, {"sql": "select * from public.documents", "max_rows": 5}
        )
        direct_body = error_body(direct)
        report.check(
            direct.status_code >= 400 and str(direct_body.get("code")) == "P0001",
            "the allowlist refuses documents even when the RPC is called directly",
            f"HTTP {direct.status_code} {direct_body.get('message', direct.text[:160])}",
        )

        role = run_analytics_query(alice_repo, sql="select current_user as role_name", max_rows=5)
        report.check(
            role.rows == [{"role_name": "demo_reader"}],
            "the function executes as demo_reader, not as the caller or postgres",
            str(role.rows),
        )

        allowed = run_analytics_query(
            alice_repo,
            sql="select p.name from public.demo_products as p order by p.name limit 3",
            max_rows=5,
        )
        report.check(
            len(allowed.rows) == 3,
            "the two demo tables remain readable through aliases and schema-qualified names",
            str(allowed.rows),
        )

        print("\n10) The anon role cannot call the function")
        print("-" * 62)
        anon_response = client.rpc(
            "run_demo_query", None, {"sql": "select count(*) from demo_products", "max_rows": 5}
        )
        anon_body = error_body(anon_response)
        report.check(
            anon_response.status_code >= 400,
            f"anon is refused (HTTP {anon_response.status_code})",
            str(anon_body.get("message", anon_response.text[:160])),
        )
    finally:
        print("\n11) Cleanup")
        print("-" * 62)
        for label, user_id in users.items():
            try:
                client.delete_user(user_id)
                print(f"  deleted user {label} ({user_id[:8]}...)")
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                report.fail(f"could not delete user {label} during cleanup", repr(exc))
        client.close()

    return _finish(report)


def _finish(report: Report) -> int:
    print("\n" + "=" * 62)
    if report.failed:
        print(f"RESULT: FAILED - {report.passed} passed, {len(report.failed)} failed")
        for failure in report.failed:
            print(f"  - {failure}")
        return 1
    print(f"RESULT: OK - {report.passed} checks passed. The demo analytics path holds.")
    return 0


def main() -> int:
    settings = Settings()
    if not settings.supabase_configured or not settings.supabase_admin_configured:
        print("This check needs SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY.")
        return 1
    if settings.supabase_service_role_key is None:
        return 1
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
