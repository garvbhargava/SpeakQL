"""The seeded warehouses (§11.4, the Seed row of §14.1).

Bootstrap stops at the first guarantee that fails (`scripts/seed_warehouse.py
check`). This file asks the same of the running warehouses, as the read role,
one guarantee per test, on both. Then it asks the headline numbers again in
plain SQL -- the SQL a question would run -- so a passing check does not rest
on the generator's own arithmetic.

The last tests need no database. The same run date must give the same data,
and a later run date must keep every guarantee, the best months included:
bootstrap runs again before the end-term review.

These read the live warehouses as bootstrap left them. A correction made
during a demo (84120 filled in, say) fails a guarantee here until
`make bootstrap` reseeds.

    make test
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts import seed_warehouse as seed

needs_postgres = pytest.mark.skipif(
    not os.environ.get("META_DSN", "").startswith("postgresql"),
    reason="needs the bootstrapped Postgres; run `make test`",
)

# Added to every failure on live data: the usual cause is a correction made
# during a demo, not a broken seed.
RESEED = "If a demo corrected data, run `make bootstrap` first."

DATABASES = {profile.database: company for company, profile in seed.PROFILES.items()}
GUARANTEES = [(database, name) for database, company in DATABASES.items()
              for name in seed.guarantees_for(company)]

WEEK_12_SQL = """
    SELECT SUM(units), COUNT(*) FILTER (WHERE units IS NULL), COUNT(*)
    FROM public.shipments
    WHERE EXTRACT(ISOYEAR FROM shipped_on) = 2025
      AND EXTRACT(WEEK FROM shipped_on) = 12
"""


def _engine(database: str, role: str = "RO_DSN"):
    return create_engine(make_url(os.environ[role]).set(database=database),
                         future=True)


def _rows(database: str, sql: str) -> list[tuple]:
    engine = _engine(database)
    try:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text(sql))]
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def live() -> dict[str, seed.Snapshot]:
    """Each warehouse as it is now -- seed record, catalogue, rows -- read once
    as speakql_ro."""
    snapshots = {}
    for database in DATABASES:
        with seed.connect(database, os.environ["RO_DSN"]) as conn:
            snapshots[database] = seed.snapshot(conn, database)
    return snapshots


# ============================================== the guarantees, on both ====

@needs_postgres
@pytest.mark.parametrize(("database", "guarantee"), GUARANTEES,
                         ids=[f"{d}:{g}" for d, g in GUARANTEES])
def test_every_guarantee_holds_on_both_warehouses(live, database, guarantee):
    assert live[database].company == DATABASES[database], RESEED
    try:
        checked = seed.check(live[database], only=guarantee)
    except seed.GuaranteeFailed as failure:
        pytest.fail(f"{failure}\n{RESEED}")
    assert checked == [guarantee]


@needs_postgres
@pytest.mark.parametrize("database", list(DATABASES))
def test_each_warehouse_is_exactly_what_its_seed_generates(live, database):
    """Bootstrap's rows are the generator's rows for the recorded run date,
    value for value: what the in-memory tests below prove of generated data
    is true of the loaded data."""
    found = live[database]
    wanted = seed.generate(found.company, found.run_date)
    for table in seed.TABLES:
        rows, expected = found.data[table], wanted[table]
        assert len(rows) == len(expected), (
            f"{database}.{table}: {len(rows)} rows, the seed makes "
            f"{len(expected)}. {RESEED}")
        for row, want in zip(rows, expected):
            assert row == want, f"{database}.{table}: {row} != {want}. {RESEED}"


# ===================================== the headline numbers, in plain SQL ====

@needs_postgres
def test_q4_2025_west_leads_and_north_follows():
    ranked = _rows("northwind_dw", """
        SELECT r.region_name, SUM(o.amount) AS total
        FROM public.orders o
        JOIN public.customers c ON c.customer_id = o.customer_id
        JOIN public.regions r ON r.region_id = c.region_id
        WHERE o.order_date BETWEEN DATE '2025-10-01' AND DATE '2025-12-31'
        GROUP BY r.region_name
        ORDER BY total DESC""")
    assert ranked[:2] == [("West", Decimal("482140.00")),
                          ("North", Decimal("355200.00"))], RESEED
    assert _rows("northwind_dw", "SELECT count(*) FROM public.orders "
                 "WHERE order_date BETWEEN DATE '2025-10-01' AND DATE '2025-12-31'"
                 ) == [(21,)], RESEED


@needs_postgres
def test_week_12_of_2025_is_1083_with_three_cells_to_fill():
    assert _rows("northwind_dw", WEEK_12_SQL) == [(1083, 3, 9)], RESEED


@needs_postgres
def test_the_week_of_16_march_finds_the_same_nine():
    """The wording the training pairs used before v6.1."""
    shipments = _rows("northwind_dw", """
        SELECT shipment_id FROM public.shipments
        WHERE shipped_on BETWEEN DATE '2025-03-16' AND DATE '2025-03-22'
        ORDER BY shipment_id""")
    assert [s for (s,) in shipments] == [84112, 84114, 84116, 84117, 84119,
                                         84120, 84121, 84122, 84123], RESEED


@needs_postgres
def test_filling_84120_with_203_makes_it_1286():
    """The demo's correction, made for real by the write role and rolled
    back: the total moves to 1,286 and two cells are left to fill."""
    engine = _engine("northwind_dw", "WRITE_DSN")
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(text(
                    "UPDATE public.shipments SET units = 203 WHERE shipment_id = 84120"))
                after = tuple(conn.execute(text(WEEK_12_SQL)).one())
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    assert after == (1286, 2, 9), RESEED
    assert _rows("northwind_dw", WEEK_12_SQL) == [(1083, 3, 9)], \
        f"84120 is not empty after the rollback. {RESEED}"


@needs_postgres
def test_the_three_best_months_in_sql():
    """Revenue, growth (both ways) and margin, each led by its own month."""
    (best_revenue,), = _rows("northwind_dw", """
        SELECT date_trunc('month', order_date)::date
        FROM public.orders GROUP BY 1 ORDER BY SUM(amount) DESC LIMIT 1""")
    assert best_revenue == date(2025, 6, 1), RESEED

    by_change, by_ratio = _rows("northwind_dw", """
        WITH monthly AS (
            SELECT date_trunc('month', order_date)::date AS month,
                   SUM(amount) AS revenue
            FROM public.orders GROUP BY 1),
        growth AS (
            SELECT month,
                   revenue - LAG(revenue) OVER (ORDER BY month) AS change,
                   revenue / LAG(revenue) OVER (ORDER BY month) AS ratio
            FROM monthly)
        SELECT (SELECT month FROM growth WHERE change IS NOT NULL
                ORDER BY change DESC LIMIT 1),
               (SELECT month FROM growth WHERE ratio IS NOT NULL
                ORDER BY ratio DESC LIMIT 1)""")[0]
    assert (by_change, by_ratio) == (date(2025, 4, 1), date(2025, 4, 1)), RESEED

    (best_margin,), = _rows("northwind_dw", """
        SELECT date_trunc('month', o.order_date)::date
        FROM public.orders o
        JOIN public.order_items oi ON oi.order_id = o.order_id
        JOIN public.products p ON p.product_id = oi.product_id
        GROUP BY 1
        ORDER BY (SUM(oi.qty * oi.unit_price) - SUM(oi.qty * p.unit_cost))
                 / SUM(oi.qty * oi.unit_price) DESC
        LIMIT 1""")
    assert best_margin == date(2025, 3, 1), RESEED


@needs_postgres
def test_harbor_is_unmistakable():
    assert _rows("harbor_dw", """
        SELECT (SELECT count(*) FROM public.regions
                WHERE region_name NOT LIKE 'Harbor%'),
               (SELECT count(*) FROM public.customers WHERE name NOT LIKE 'Harbor%'),
               (SELECT count(*) FROM public.shipments WHERE units IS NULL)"""
                 ) == [(0, 0, 3)], RESEED
    assert _rows("northwind_dw", """
        SELECT (SELECT count(*) FROM public.regions WHERE region_name ILIKE '%harbor%'),
               (SELECT count(*) FROM public.customers WHERE name ILIKE '%harbor%')"""
                 ) == [(0, 0)], RESEED


# ========================================================= no database ====

@pytest.mark.parametrize("company", list(seed.PROFILES))
def test_the_same_run_date_gives_the_same_data(company):
    run_date = date(2026, 11, 18)
    assert seed.generate(company, run_date) == seed.generate(company, run_date)


@pytest.mark.parametrize("run_date", [seed.EARLIEST_RUN_DATE, date(2027, 3, 1),
                                      date(2027, 6, 30), date(2028, 12, 31)],
                         ids=str)
@pytest.mark.parametrize("company", list(seed.PROFILES))
def test_a_later_run_date_keeps_every_guarantee(company, run_date):
    """Bootstrap runs again near the end-term; every month up to the day
    before must still give the same three best months, and the rest."""
    checked = seed.check(seed.generated(company, run_date))
    assert checked == [g for g in seed.guarantees_for(company) if g != "schema"]


def test_a_run_date_that_would_reach_into_q4_2025_is_refused():
    with pytest.raises(ValueError):
        seed.generate("northwind", date(2025, 12, 31))
