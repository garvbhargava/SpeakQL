"""Adversarial tests for the AST validator (Backend Plan §21).

Every statement in the REFUSED block below is something a text-matching
validator lets through. None of them reach the database here, because nothing
in validator.py looks at the words -- it looks at what the statement *is*.

These need no database. Run them anywhere:

    cd backend && pytest tests/test_validator.py -v
"""

from __future__ import annotations

import pytest

from core.validator import Permitted, Refusal, enforce_limit, validate

PERMITTED = Permitted(
    tables=frozenset({
        "public.orders", "public.customers", "public.regions",
        "public.products", "public.order_items", "public.shipments",
    }),
    public_columns=frozenset({"region_name", "order_date", "amount"}),
)

VIEWER = Permitted(
    tables=PERMITTED.tables,
    public_columns=PERMITTED.public_columns,
    public_only=True,
)


# ============================================================ ACCEPTED ======

ACCEPTED = [
    "SELECT * FROM orders",
    "SELECT r.region_name, SUM(o.amount) AS total FROM orders o "
    "JOIN customers c ON c.customer_id = o.customer_id "
    "JOIN regions r ON r.region_id = c.region_id GROUP BY r.region_name",
    "SELECT count(*) FROM shipments WHERE units IS NULL",
    "WITH monthly AS (SELECT date_trunc('month', order_date) m, SUM(amount) a "
    "FROM orders GROUP BY 1) SELECT * FROM monthly ORDER BY a DESC",
    "SELECT * FROM orders WHERE amount > (SELECT avg(amount) FROM orders)",
    "SELECT sku FROM products UNION SELECT name FROM customers",
    "SELECT o.order_id FROM public.orders o LIMIT 10",
]


@pytest.mark.parametrize("sql", ACCEPTED, ids=lambda s: s[:44])
def test_legitimate_queries_are_accepted(sql):
    verdict = validate(sql, PERMITTED)
    assert verdict.allowed, f"wrongly refused: {verdict.reason}"
    assert verdict.tables, "accepted but named no tables"


# ============================================================= REFUSED ======

REFUSED = [
    # --- the classic: a second statement smuggled after a valid one --------
    ("SELECT 1 FROM orders; DROP TABLE orders", Refusal.NOT_SINGLE),
    ("SELECT * FROM orders; DELETE FROM orders", Refusal.NOT_SINGLE),
    ("SELECT * FROM orders;;DROP TABLE customers;", Refusal.NOT_SINGLE),

    # --- plain writes and DDL ----------------------------------------------
    ("DELETE FROM orders WHERE order_date < '2019-01-01'", Refusal.WRITE_OPERATION),
    ("UPDATE orders SET amount = 0", Refusal.WRITE_OPERATION),
    ("INSERT INTO orders (amount) VALUES (1)", Refusal.WRITE_OPERATION),
    ("DROP TABLE orders", Refusal.DDL),
    ("TRUNCATE orders", Refusal.DDL),
    ("ALTER TABLE orders ADD COLUMN x int", Refusal.DDL),
    ("CREATE TABLE evil (x int)", Refusal.DDL),

    # --- a write hidden inside a CTE, wrapped in a SELECT ------------------
    # This is the one that defeats "does it start with SELECT?"
    ("WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
     Refusal.WRITE_OPERATION),
    ("WITH bumped AS (UPDATE orders SET amount = amount * 2 RETURNING *) "
     "SELECT count(*) FROM bumped", Refusal.WRITE_OPERATION),

    # --- SELECT that is really a write -------------------------------------
    ("SELECT * INTO copied FROM orders", Refusal.DDL),
    ("SELECT * FROM orders FOR UPDATE", Refusal.WRITE_OPERATION),

    # --- system catalogues: schema discovery, and a route to credentials ---
    ("SELECT * FROM pg_catalog.pg_user", Refusal.SYSTEM_CATALOGUE),
    ("SELECT * FROM information_schema.tables", Refusal.SYSTEM_CATALOGUE),
    ("SELECT * FROM pg_shadow", Refusal.SYSTEM_CATALOGUE),
    ("SELECT rolname FROM pg_roles", Refusal.SYSTEM_CATALOGUE),

    # --- another tenant's data ---------------------------------------------
    # Identical refusal to a table that does not exist, so existence cannot be
    # discovered by iterating.
    ("SELECT * FROM trellis.orders", Refusal.UNKNOWN_TABLE),
    ("SELECT * FROM secret_table", Refusal.UNKNOWN_TABLE),
    ("SELECT o.* FROM orders o JOIN other_co.payroll p ON p.id = o.order_id",
     Refusal.UNKNOWN_TABLE),

    # --- functions with side effects or privilege reach --------------------
    ("SELECT pg_read_file('/etc/passwd')", Refusal.FUNCTION_NOT_ALLOWED),
    ("SELECT pg_sleep(60) FROM orders", Refusal.FUNCTION_NOT_ALLOWED),
    ("SELECT current_setting('is_superuser') FROM orders",
     Refusal.FUNCTION_NOT_ALLOWED),
    ("SELECT lo_import('/etc/shadow') FROM orders", Refusal.FUNCTION_NOT_ALLOWED),

    # --- nonsense -----------------------------------------------------------
    ("", Refusal.UNPARSEABLE),
    ("this is not sql at all", Refusal.UNPARSEABLE),
    ("SELECT 1", Refusal.NO_TABLES),
]


@pytest.mark.parametrize("sql,expected", REFUSED, ids=lambda v: str(v)[:44])
def test_dangerous_statements_are_refused(sql, expected):
    verdict = validate(sql, PERMITTED)
    assert not verdict.allowed, f"ACCEPTED something dangerous: {sql!r}"
    assert verdict.refusal is expected, (
        f"refused for the wrong reason: got {verdict.refusal}, expected {expected}"
    )


def test_refusal_names_the_rule_that_fired():
    """The interface says which rule refused, so the reason has to be usable
    prose and not an error code."""
    verdict = validate("DELETE FROM orders", PERMITTED)
    assert "writes" in verdict.reason
    assert "DELETE" in verdict.reason


# ========================================== case, comments, whitespace ======
# A word-matching validator is defeated by all three. A parser does not care.

@pytest.mark.parametrize("sql", [
    "dRoP tAbLe orders",
    "/* harmless */ DROP /* comment */ TABLE orders",
    "DROP\n\tTABLE\n\torders",
    "SELECT * FROM orders -- ; DROP TABLE orders",
])
def test_obfuscation_does_not_help(sql):
    verdict = validate(sql, PERMITTED)
    if "SELECT" in sql.upper() and "--" in sql:
        # the trailing comment is not a second statement; this one is fine
        assert verdict.allowed
    else:
        assert not verdict.allowed


# ================================================== viewer restrictions ======

def test_viewer_cannot_select_star():
    verdict = validate("SELECT * FROM orders", VIEWER)
    assert not verdict.allowed
    assert verdict.refusal is Refusal.FORBIDDEN_COLUMN


def test_viewer_may_read_public_columns():
    verdict = validate("SELECT region_name FROM regions", VIEWER)
    assert verdict.allowed, verdict.reason


def test_viewer_refused_a_non_public_column():
    verdict = validate("SELECT unit_cost FROM products", VIEWER)
    assert not verdict.allowed
    assert verdict.refusal is Refusal.FORBIDDEN_COLUMN
    assert "unit_cost" in verdict.detail


# ============================================================== shape ======

def test_deep_nesting_is_refused():
    sql = "SELECT * FROM orders"
    for _ in range(8):
        sql = f"SELECT * FROM ({sql}) t"
    verdict = validate(sql, PERMITTED)
    assert not verdict.allowed
    assert verdict.refusal is Refusal.SUBQUERY_DEPTH


def test_cartesian_join_is_flagged_not_refused():
    """A deliberate cross join is legitimate. It carries a warning because it
    is usually a generation bug, but refusing it would be the validator making
    an editorial judgement rather than a safety one."""
    verdict = validate("SELECT * FROM orders, customers", PERMITTED)
    assert verdict.allowed
    assert any("without a condition" in n for n in verdict.notes)


# ============================================================== limit ======

def test_limit_is_added_when_absent():
    out = enforce_limit("SELECT * FROM orders", 5000)
    assert "LIMIT 5000" in out.upper()


def test_a_tighter_limit_is_left_alone():
    out = enforce_limit("SELECT * FROM orders LIMIT 10", 5000)
    assert "LIMIT 10" in out.upper()
    assert "5000" not in out


def test_a_looser_limit_is_tightened():
    out = enforce_limit("SELECT * FROM orders LIMIT 100000", 5000)
    assert "LIMIT 5000" in out.upper()


# ======================================================= never explodes ======

@pytest.mark.parametrize("sql", [
    None, "", "   ", ";", "SELECT", "((((", "\x00", "SELECT * FROM " + "a" * 5000,
])
def test_validator_never_raises(sql):
    """It is asked to judge model output and untrusted input. A crash here is
    a 500 on a path whose entire job is refusing safely."""
    verdict = validate(sql, PERMITTED)  # type: ignore[arg-type]
    assert verdict.allowed in (True, False)
