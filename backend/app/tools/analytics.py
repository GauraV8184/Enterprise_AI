"""The SQL analytics tool: demo schema description, execution, row rendering.

The store does the executing (SQLite in development, ``run_demo_query`` in
Postgres); this module owns the parts that must be identical on both backends:

* the schema description handed to the model,
* the pre-flight validation (delegated to :mod:`app.database.sql_guard`),
* the result shape, and
* how rows are rendered into a prompt within a character budget.

The demo dataset itself is defined in :mod:`app.database.demo_data`, and the
description below is derived from those same constants so the prompt can never
describe tables the seed does not create.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.database.demo_data import (
    DEMO_MONTHS,
    DEMO_ORDER_COUNT,
    DEMO_PRODUCTS,
    DEMO_REGIONS,
    DEMO_START_YEAR,
)
from app.database.repositories import DocumentStore
from app.database.sql_guard import MAX_ROWS_HARD_LIMIT, validate_readonly_sql

MAX_ROWS_CHARS = 8000

_TRAILING_LIMIT = re.compile(r"\s+limit\s+\d+\s*$", re.IGNORECASE)

# "top 3", "3 highest", "first ten", "limit to 5". A question that names a
# count asked for a bounded result; anything else did not, and a model-invented
# LIMIT 1 there hides the other groups instead of shortening the answer.
_REQUESTED_TOP_N = re.compile(
    r"\b(?:top|first|last|best|worst|highest|lowest|largest|smallest|latest|"
    r"earliest|bottom|leading)\s+(?:\d+|one|two|three|four|five|six|seven|"
    r"eight|nine|ten)\b"
    r"|\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:top|best|worst|highest|lowest|largest|smallest|latest|earliest|leading)\b"
    r"|\blimit(?:ed)?\s+(?:to\s+)?(?:\d+|one|two|three|four|five|six|seven|"
    r"eight|nine|ten)\b",
    re.IGNORECASE,
)

_LAST_YEAR = DEMO_START_YEAR + (DEMO_MONTHS - 1) // 12
_LAST_MONTH = (DEMO_MONTHS - 1) % 12 + 1

_CATEGORIES = ", ".join(sorted({product.category for product in DEMO_PRODUCTS}))
_REGIONS = ", ".join(DEMO_REGIONS)


def _build_schema_description() -> str:
    return (
        "Read-only demo sales dataset, reachable as the tables below. It is "
        "clearly-labelled demonstration data, not real company records.\n"
        "\n"
        "Table demo_products\n"
        "    id          integer  primary key\n"
        "    name        text     product name\n"
        f"    category    text     one of: {_CATEGORIES}\n"
        "    unit_price  numeric  list price per unit, in USD\n"
        "\n"
        "Table demo_sales_orders\n"
        "    id          integer  primary key\n"
        "    order_date  text     ISO date string, YYYY-MM-DD (for example 2024-03-01)\n"
        f"    region      text     one of: {_REGIONS}\n"
        "    product_id  integer  references demo_products.id\n"
        "    quantity    integer  units sold\n"
        "    revenue     numeric  quantity * unit_price, in USD\n"
        "\n"
        "A column exists only in the table under which it is listed. "
        "demo_sales_orders does not contain category, name, or unit_price. "
        "To group or filter by those, join demo_products:\n"
        "    demo_sales_orders AS o JOIN demo_products AS p ON p.id = o.product_id\n"
        "\n"
        "Example - revenue by category across the whole dataset:\n"
        "    SELECT p.category, SUM(o.revenue) AS revenue_usd\n"
        "    FROM demo_sales_orders AS o\n"
        "    JOIN demo_products AS p ON p.id = o.product_id\n"
        "    GROUP BY p.category\n"
        "    ORDER BY revenue_usd DESC\n"
        "\n"
        f"Coverage: {DEMO_ORDER_COUNT} orders, {DEMO_MONTHS} consecutive months from "
        f"{DEMO_START_YEAR}-01 to {_LAST_YEAR}-{_LAST_MONTH:02d}.\n"
        "\n"
        "Write SQL that runs on both SQLite and PostgreSQL. Use common functions "
        "only: count, sum, avg, min, max, round, substr, coalesce, group by, "
        "order by, limit, and common table expressions. Avoid to_char, date_trunc, "
        "strftime, casts to date types, and any other dialect-specific syntax. "
        "Only when the question asks for a period, filter the year with "
        "substr(order_date, 1, 4) = '2024' or the month with "
        "substr(order_date, 1, 7) = '2024-03'. Never add a date filter the "
        "question did not ask for."
    )


DEMO_SCHEMA_DESCRIPTION = _build_schema_description()


@dataclass(frozen=True)
class AnalyticsResult:
    """One executed query and the rows it returned."""

    sql: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    """True when the row cap was hit, so more rows may exist than were returned."""

    @property
    def row_count(self) -> int:
        return len(self.rows)


def strip_unrequested_limit(sql: str, question: str) -> str:
    """Remove a trailing LIMIT that the question did not ask for.

    Small models sometimes append ``LIMIT 1`` to a question that asks for a
    breakdown ("revenue by category"), which returns one group and makes the
    comparison impossible. The row cap is enforced by the store, not by the
    generated LIMIT, so dropping an unrequested one widens evidence without
    weakening a resource bound. A count named in the question ("top 3", "first
    ten") is left alone.

    Only a trailing top-level LIMIT is considered. A LIMIT inside a subquery or
    followed by OFFSET is part of the query's own logic and is not touched.
    """
    if not _TRAILING_LIMIT.search(sql):
        return sql
    if _REQUESTED_TOP_N.search(question):
        return sql
    return _TRAILING_LIMIT.sub("", sql)


def run_analytics_query(
    store: DocumentStore,
    *,
    sql: str,
    max_rows: int,
) -> AnalyticsResult:
    """Validate and execute one read-only query against the demo dataset.

    Raises:
        AnalyticsQueryRejectedError: the guard refused the statement; the
            message is safe to show and to feed back to the model.
        AnalyticsQueryFailedError: the database refused or failed the query.
    """
    statement = validate_readonly_sql(sql)
    rows = store.run_analytics_query(sql=statement, max_rows=max_rows)
    effective_cap = max(1, min(int(max_rows), MAX_ROWS_HARD_LIMIT))
    return AnalyticsResult(
        sql=statement,
        rows=rows,
        truncated=len(rows) >= effective_cap,
    )


def render_rows(
    rows: list[dict[str, Any]],
    *,
    max_chars: int = MAX_ROWS_CHARS,
) -> tuple[str, int]:
    """Serialise rows as JSON for a prompt, staying within a character budget.

    Rows are rendered one JSON object per line and dropped from the end once the
    budget is reached, because a truncated prompt is worse than fewer rows - the
    model cannot tell that its evidence was cut off mid-object. Returns the text
    and how many rows actually made it in, so a report can state the difference.
    """
    if not rows:
        return "[]", 0

    lines: list[str] = []
    used = 3
    for row in rows:
        line = json.dumps(row, ensure_ascii=False, default=str)
        if lines and used + len(line) + 2 > max_chars:
            break
        lines.append(line)
        used += len(line) + 2

    return "[\n" + ",\n".join(lines) + "\n]", len(lines)
