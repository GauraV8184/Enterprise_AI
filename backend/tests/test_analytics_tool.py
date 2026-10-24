"""The SQL analytics tool against the real SQLite store.

The store, the demo seed, the guard and the executor are all real; nothing here
is mocked. That is the point: the values below are computed from the same
``app.database.demo_data`` generator that seeds the database, so a seed that
drifts from the definition fails the test instead of quietly changing answers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database.demo_data import DEMO_ORDER_COUNT, DEMO_PRODUCTS, demo_sales_rows
from app.database.errors import AnalyticsQueryFailedError, AnalyticsQueryRejectedError
from app.database.repositories import DocumentRepository
from app.database.sql_guard import ALLOWED_TABLES, MAX_ROWS_HARD_LIMIT
from app.llm.prompts import build_sql_generation_prompt
from app.tools.analytics import (
    DEMO_SCHEMA_DESCRIPTION,
    render_rows,
    run_analytics_query,
    strip_unrequested_limit,
)


@pytest.fixture
def store(tmp_path: Path) -> DocumentRepository:
    return DocumentRepository(tmp_path / "analytics" / "app.db")


def test_demo_dataset_is_seeded_deterministically(store: DocumentRepository) -> None:
    products = run_analytics_query(
        store, sql="select count(*) as n from demo_products", max_rows=10
    )
    orders = run_analytics_query(
        store, sql="select count(*) as n from demo_sales_orders", max_rows=10
    )

    assert products.rows == [{"n": len(DEMO_PRODUCTS)}]
    assert orders.rows == [{"n": DEMO_ORDER_COUNT}]

    expected_revenue = round(sum(row[5] for row in demo_sales_rows()), 2)
    totals = run_analytics_query(
        store, sql="select round(sum(revenue), 2) as total from demo_sales_orders", max_rows=10
    )
    assert totals.rows == [{"total": expected_revenue}]


def test_seeding_runs_once_even_when_the_store_is_reopened(tmp_path: Path) -> None:
    path = tmp_path / "analytics" / "app.db"
    DocumentRepository(path)
    reopened = DocumentRepository(path)

    orders = run_analytics_query(
        reopened, sql="select count(*) as n from demo_sales_orders", max_rows=10
    )
    assert orders.rows == [{"n": DEMO_ORDER_COUNT}]


def test_aggregates_answer_business_questions(store: DocumentRepository) -> None:
    by_region = run_analytics_query(
        store,
        sql=(
            "select region, round(sum(revenue), 2) as revenue "
            "from demo_sales_orders group by region order by revenue desc"
        ),
        max_rows=10,
    )

    assert len(by_region.rows) == 4
    assert all(set(row) == {"region", "revenue"} for row in by_region.rows)
    revenues = [row["revenue"] for row in by_region.rows]
    assert revenues == sorted(revenues, reverse=True)
    assert sum(revenues) == pytest.approx(round(sum(row[5] for row in demo_sales_rows()), 2))

    by_year = run_analytics_query(
        store,
        sql=(
            "select substr(order_date, 1, 4) as year, count(*) as orders "
            "from demo_sales_orders group by year order by year"
        ),
        max_rows=10,
    )
    assert [row["year"] for row in by_year.rows] == ["2024", "2025"]
    assert [row["orders"] for row in by_year.rows] == [DEMO_ORDER_COUNT // 2] * 2


def test_row_cap_is_enforced_by_the_outer_limit(store: DocumentRepository) -> None:
    capped = run_analytics_query(store, sql="select * from demo_sales_orders", max_rows=10)

    assert capped.row_count == 10
    assert capped.truncated is True

    oversized = run_analytics_query(
        store, sql="select * from demo_sales_orders", max_rows=MAX_ROWS_HARD_LIMIT + 100
    )
    assert oversized.row_count == MAX_ROWS_HARD_LIMIT
    assert oversized.truncated is True

    complete = run_analytics_query(store, sql="select * from demo_sales_orders", max_rows=400)
    assert complete.row_count == MAX_ROWS_HARD_LIMIT


def test_inner_limit_cannot_defeat_the_cap(store: DocumentRepository) -> None:
    capped = run_analytics_query(
        store, sql="select * from demo_sales_orders limit 1000", max_rows=5
    )
    assert capped.row_count == 5


def test_guard_rejection_never_reaches_the_database(store: DocumentRepository) -> None:
    with pytest.raises(AnalyticsQueryRejectedError):
        run_analytics_query(store, sql="drop table demo_sales_orders", max_rows=10)


def test_only_the_demo_tables_can_be_read(store: DocumentRepository) -> None:
    """A read is the dangerous case the keyword guard cannot catch.

    ``select * from documents`` is valid SQL, starts with SELECT, and contains no
    forbidden word - and on the caller's own connection it would return their
    document text. The authorizer refuses it before a row is read.
    """
    cases = [
        ("select * from documents", "documents"),
        ("select count(*) as n from chunks", "chunks"),
        ("select name from sqlite_master", "sqlite_master"),
    ]

    for sql, table in cases:
        with pytest.raises(AnalyticsQueryRejectedError) as caught:
            run_analytics_query(store, sql=sql, max_rows=10)

        assert table in caught.value.message
        assert "demo_products" in caught.value.message


def test_allowlist_cannot_be_evaded_with_aliases_or_ctes(store: DocumentRepository) -> None:
    cases = [
        "select d.filename from documents as d",
        "with hidden as (select * from chunks) select count(*) as n from hidden",
        "select * from (select * from documents) as sub",
    ]

    for sql in cases:
        with pytest.raises(AnalyticsQueryRejectedError):
            run_analytics_query(store, sql=sql, max_rows=10)


def test_allowed_tables_still_work_through_aliases(store: DocumentRepository) -> None:
    result = run_analytics_query(
        store,
        sql="select p.name from demo_products as p order by p.name limit 3",
        max_rows=10,
    )

    assert len(result.rows) == 3


def test_allowed_tables_still_work_through_a_cte(store: DocumentRepository) -> None:
    """The authorizer must see the real table, not the CTE's name."""

    result = run_analytics_query(
        store,
        sql=(
            "with products as (select name from demo_products) "
            "select name from products order by name limit 3"
        ),
        max_rows=10,
    )

    assert len(result.rows) == 3


def test_allowlist_matches_the_documented_demo_schema() -> None:
    assert ALLOWED_TABLES == {"demo_products", "demo_sales_orders"}
    for table in ALLOWED_TABLES:
        assert table in DEMO_SCHEMA_DESCRIPTION


def test_a_broken_query_fails_with_a_repair_hint(store: DocumentRepository) -> None:
    with pytest.raises(AnalyticsQueryFailedError) as caught:
        run_analytics_query(store, sql="select revenu from demo_sales_orders", max_rows=10)

    assert "revenu" in caught.value.repair_hint
    assert caught.value.message == caught.value.public_message


def test_schema_description_describes_the_seeded_tables() -> None:
    assert "demo_products" in DEMO_SCHEMA_DESCRIPTION
    assert "demo_sales_orders" in DEMO_SCHEMA_DESCRIPTION
    assert "substr(order_date, 1, 7)" in DEMO_SCHEMA_DESCRIPTION
    assert str(DEMO_ORDER_COUNT) in DEMO_SCHEMA_DESCRIPTION
    for product in DEMO_PRODUCTS:
        assert product.category in DEMO_SCHEMA_DESCRIPTION


def test_schema_description_names_the_join_for_product_columns() -> None:
    """A small model must be told which table owns category, not infer it."""
    assert "demo_sales_orders does not contain category" in DEMO_SCHEMA_DESCRIPTION
    assert "JOIN demo_products AS p ON p.id = o.product_id" in DEMO_SCHEMA_DESCRIPTION
    assert "Never add a date filter" in DEMO_SCHEMA_DESCRIPTION


def test_a_breakdown_question_loses_an_invented_limit() -> None:
    sql = (
        "select p.category, sum(o.revenue) as revenue_usd "
        "from demo_sales_orders as o join demo_products as p on p.id = o.product_id "
        "group by p.category order by revenue_usd desc limit 1"
    )

    assert strip_unrequested_limit(
        sql, "Analyse revenue by product category and recommend the next focus."
    ) == (
        "select p.category, sum(o.revenue) as revenue_usd "
        "from demo_sales_orders as o join demo_products as p on p.id = o.product_id "
        "group by p.category order by revenue_usd desc"
    )


def test_a_count_named_in_the_question_keeps_its_limit() -> None:
    sql = "select name, unit_price from demo_products order by unit_price desc limit 3"

    assert strip_unrequested_limit(sql, "What are the top 3 products by price?") == sql
    assert strip_unrequested_limit(sql, "Show the first ten products") == sql


def test_a_limit_inside_a_subquery_is_left_alone() -> None:
    sql = (
        "select count(*) as n from (select id from demo_products limit 1) as sample"
    )

    assert strip_unrequested_limit(sql, "Compare revenue by category") == sql


def test_repair_feedback_points_at_the_schema_and_the_join() -> None:
    prompt = build_sql_generation_prompt(
        question="revenue by category",
        schema=DEMO_SCHEMA_DESCRIPTION,
        max_rows=100,
        feedback='column "category" does not exist',
    )

    assert 'column "category" does not exist' in prompt
    assert "A column is available only in the table under which it is listed" in prompt
    assert "join demo_products" in prompt


def test_render_rows_stays_within_the_character_budget() -> None:
    rows = [{"region": "North", "revenue": index * 1000} for index in range(200)]

    text, included = render_rows(rows, max_chars=500)

    assert 0 < included < len(rows)
    assert json.loads(text) == rows[:included]


def test_render_rows_keeps_one_oversized_row_rather_than_nothing() -> None:
    rows = [{"blob": "x" * 5000}]

    text, included = render_rows(rows, max_chars=100)

    assert included == 1
    assert json.loads(text) == rows


def test_render_rows_handles_an_empty_result() -> None:
    assert render_rows([]) == ("[]", 0)
