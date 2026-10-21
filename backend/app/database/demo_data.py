"""The demo sales dataset behind the SQL analytics tool.

**These are demonstration records, not business data.** The SQL tool needs a
real table to query before it can be called honest, so migration
``0005_demo_analytics.sql`` creates two clearly-named tables (``demo_products``,
``demo_sales_orders``) and seeds them; the SQLite development store seeds the
same rows from this module. Swapping in real tables means rewriting the
migration, the schema description in :mod:`app.tools.analytics`, and the RLS
policy that currently lets every authenticated caller read the demo rows.

**The two seeds must agree.** The Python table and the SQL ``INSERT ... SELECT``
in the migration produce the same 384 rows (4 products x 4 regions x 24 months)
with the same ids, so a query behaves identically against SQLite and Postgres
and one expected value in the tests holds for both. ``order_date`` is stored as
an ISO ``YYYY-MM-DD`` string on purpose: grouping by month or year becomes
``substr(order_date, 1, 7)``, which is valid in both dialects, so the generated
SQL does not need dialect-specific date functions.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

DEMO_START_YEAR = 2024

DEMO_MONTHS = 24


@dataclass(frozen=True)
class DemoProduct:
    id: int
    name: str
    category: str
    unit_price: float


DEMO_PRODUCTS: tuple[DemoProduct, ...] = (
    DemoProduct(id=1, name="Aurora Laptop", category="Hardware", unit_price=1299.00),
    DemoProduct(id=2, name="Pulse Headset", category="Hardware", unit_price=189.00),
    DemoProduct(id=3, name="Insight Suite", category="Software", unit_price=349.00),
    DemoProduct(id=4, name="Care Plan", category="Services", unit_price=99.00),
)

DEMO_REGIONS: tuple[str, ...] = ("North", "South", "East", "West")

DEMO_ORDER_COUNT = len(DEMO_PRODUCTS) * len(DEMO_REGIONS) * DEMO_MONTHS


def demo_sales_rows() -> Iterator[tuple[int, str, str, int, int, float]]:
    """Yield ``(id, order_date, region, product_id, quantity, revenue)`` rows.

    Deterministic: the sequence, the ids and the quantities are fixed by the
    formula ``1 + (region_index * 7 + product_index * 3 + month) % 20``, which
    the migration's seed repeats in SQL. Nothing here is random, so both stores
    always agree.
    """
    order_id = 0
    for month in range(DEMO_MONTHS):
        year = DEMO_START_YEAR + (month // 12)
        month_number = (month % 12) + 1
        order_date = f"{year:04d}-{month_number:02d}-01"

        for region_index, region in enumerate(DEMO_REGIONS):
            for product_index, product in enumerate(DEMO_PRODUCTS):
                order_id += 1
                quantity = 1 + (region_index * 7 + product_index * 3 + month) % 20
                revenue = round(quantity * product.unit_price, 2)
                yield order_id, order_date, region, product.id, quantity, revenue
