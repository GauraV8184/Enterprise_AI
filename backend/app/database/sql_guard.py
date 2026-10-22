"""Pre-flight validation for read-only SQL produced by the model.

**What this is.** A conservative, string-level guard that refuses anything which
is not obviously a single read-only statement, and refuses a blocklist of DML,
DDL, transaction and session keywords. It runs in Python before a query is sent
to either store.

**What this is not.** It is not a SQL parser and it is not the security boundary.
The real enforcement is the database's own privileges: the analytics route runs
with the caller's token, so Row Level Security and GRANTs decide which rows a
query can reach - a query that somehow slipped past this guard still cannot read
another tenant's documents, write anything, or drop a table. Resource abuse is
bounded by the row cap and, in Postgres, by the RPC's statement timeout.

Phase 7 (security and audit logging) tightens this further. Keeping the rules
here means the store, the tools and the tests all share one definition of
"read-only enough to run".

Rules, in order:

1. not blank, within the length budget,
2. no ``;`` anywhere (one statement only; this includes semicolons in comments,
   which the guard does not try to strip),
3. must start with ``SELECT`` or ``WITH`` (a CTE is fine),
4. must not contain a blocked keyword as a whole word.
"""

from __future__ import annotations

import re

from app.database.errors import AnalyticsQueryRejectedError

MAX_SQL_CHARS = 4000

MAX_ROWS_HARD_LIMIT = 200

ALLOWED_TABLES = frozenset({"demo_products", "demo_sales_orders"})
"""The only relations the analytics tool may read.

The SQLite store enforces this with a connection authorizer and Postgres with a
privilege set plus a plan check; keeping the names in one place means the two
backends cannot disagree about what the demo dataset is. Checked against
:mod:`app.database.demo_data` in the tests.
"""

_READ_ONLY_START = re.compile(r"^(?:select|with)\b", re.IGNORECASE)

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(?:"
    r"insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"call|merge|execute|do|vacuum|reindex|refresh|reassign|discard|"
    r"comment|security|begin|commit|rollback|savepoint|set|reset|"
    r"lock|listen|notify|prepare"
    r")\b",
    re.IGNORECASE,
)


def validate_readonly_sql(sql: str, *, max_chars: int = MAX_SQL_CHARS) -> str:
    """Return the statement if it may be run read-only, else raise.

    The return value is the stripped statement; callers should execute exactly
    what this function returned rather than re-reading the original string.

    Raises:
        AnalyticsQueryRejectedError: a client-safe 400 explaining which rule was
            broken. The message is also what the analyst agent feeds back to the
            model when it repairs a rejected query.
    """
    statement = (sql or "").strip()

    if not statement:
        raise AnalyticsQueryRejectedError("The generated query was empty.")

    if len(statement) > max_chars:
        raise AnalyticsQueryRejectedError(
            f"The generated query exceeds the {max_chars}-character limit."
        )

    if ";" in statement:
        raise AnalyticsQueryRejectedError(
            "Only a single statement is allowed; remove the semicolon."
        )

    if not _READ_ONLY_START.match(statement):
        raise AnalyticsQueryRejectedError(
            "Only SELECT statements are allowed (WITH ... SELECT is accepted)."
        )

    match = _FORBIDDEN_KEYWORDS.search(statement)
    if match:
        raise AnalyticsQueryRejectedError(
            f"The query contains the disallowed keyword {match.group(0).upper()!r}."
        )

    return statement
