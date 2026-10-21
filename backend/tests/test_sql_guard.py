"""The read-only SQL guard: what it accepts, what it refuses, and why.

The guard is a string-level pre-flight check, not the security boundary - the
database enforces read-only independently (Postgres refuses writes inside the
``stable`` query function; RLS and grants apply to whatever it runs). These
tests pin its *policy*: conservative, explicit about which rule was broken, and
never string-matching so loosely that a valid aggregate query is refused.
"""

from __future__ import annotations

import pytest

from app.database.errors import AnalyticsQueryRejectedError
from app.database.sql_guard import MAX_SQL_CHARS, validate_readonly_sql


def test_accepts_a_plain_select_and_strips_whitespace() -> None:
    assert validate_readonly_sql("  select count(*) from demo_products  ") == (
        "select count(*) from demo_products"
    )


def test_accepts_a_cte_and_mixed_case_keywords() -> None:
    statement = "WITH totals AS (SELECT region, sum(revenue) AS r FROM demo_sales_orders GROUP BY region) SELECT * FROM totals ORDER BY r DESC"
    assert validate_readonly_sql(statement) == statement
    assert validate_readonly_sql("SELECT 1") == "SELECT 1"


def test_accepts_column_names_that_merely_contain_blocked_words() -> None:
    statement = "select sum(revenue) as total_revenue from demo_sales_orders"
    assert validate_readonly_sql(statement) == statement


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "select count(*) from demo_products;",
        "select count(*) from demo_products; drop table demo_products",
        "delete from demo_sales_orders",
        "update demo_products set unit_price = 0",
        "insert into demo_products values (5, 'x', 'y', 1)",
        "drop table demo_products",
        "create table t (id int)",
        "grant select on demo_products to anon",
        "with x as (delete from demo_sales_orders returning id) select * from x",
        "select 1 -- delete",
        "truncate demo_sales_orders",
        "copy demo_products to stdout",
        "call run_demo_query('select 1')",
        "vacuum",
        "begin",
        "select pg_terminate_backend(1) from demo_products where 'lock' = 'lock'",
    ],
)
def test_rejects_everything_that_is_not_one_read_only_select(sql: str) -> None:
    with pytest.raises(AnalyticsQueryRejectedError):
        validate_readonly_sql(sql)


def test_rejection_messages_name_the_rule_that_was_broken() -> None:
    with pytest.raises(AnalyticsQueryRejectedError, match="semicolon"):
        validate_readonly_sql("select 1;")
    with pytest.raises(AnalyticsQueryRejectedError, match="SELECT"):
        validate_readonly_sql("explain select 1")
    with pytest.raises(AnalyticsQueryRejectedError, match="DROP"):
        validate_readonly_sql("select 1 from demo_products where drop = 1")
    with pytest.raises(AnalyticsQueryRejectedError, match="empty"):
        validate_readonly_sql("  ")


def test_rejects_a_statement_over_the_length_budget() -> None:
    too_long = "select " + "x" * (MAX_SQL_CHARS - len("select ") + 10)
    with pytest.raises(AnalyticsQueryRejectedError, match="character"):
        validate_readonly_sql(too_long)


def test_the_boundary_length_is_accepted() -> None:
    statement = "select " + "x" * (MAX_SQL_CHARS - len("select "))
    assert len(statement) == MAX_SQL_CHARS
    assert validate_readonly_sql(statement) == statement
