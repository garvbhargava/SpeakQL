"""Synthetic question/SQL pairs over this warehouse (Backend Plan §23, weeks 3-4).

Spider teaches a model what SQL is. It cannot teach it this warehouse, and the
questions a business actually asks -- "which region sold the most", "what is
missing" -- are a narrow, repetitive slice that a 60M-parameter model can learn
properly. So the second training stage is in-domain, and these are the pairs.

Every pair is **executed before it is kept**. A generated pair that does not
run is not training data, it is a lie the model learns; pair verification is a
step in the plan (week 4) rather than an afterthought, and this is it.

Honesty about what the test split measures. Three kinds of pair exist here:

    seen shape, seen phrasing      train
    seen shape, NEW phrasing       test -- the last paraphrase of each template
                                          is never trained on
    NEW shape                      test -- whole templates held out, so the
                                          model has never seen the question
                                          type at all

Reporting one number over a test set drawn from the same templates as training
would be reporting memorisation. `generator_eval.py` reports the two slices
separately for that reason.

    python -m ml.synth
"""

from __future__ import annotations

import hashlib
import itertools
import os
import random
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from ml.paths import BACKEND, SYNTH, write_jsonl

SEED = 20260923
MAX_PER_TEMPLATE = 120

# ------------------------------------------------------------------ slots ----

REGIONS = ["West", "North", "South", "East", "Central"]
CARRIERS = ["Redline", "Northbound", "Coastal"]
CUSTOMERS = ["Aster Retail", "Brightline Foods", "Cobalt Industrial",
             "Dunmore Supply", "Eastgate Traders", "Fairhaven Group",
             "Granby Wholesale", "Halloway Partners"]
PRODUCTS = ["Barrier film, 50 micron", "Barrier film, 80 micron",
            "Carton, 12 litre", "Carton, 20 litre", "Label roll, thermal",
            "Pallet wrap, heavy"]
SKUS = ["BR-2214", "BR-2218", "CT-1180", "CT-1182", "LB-4400", "PL-7710"]
NS = [3, 5]
AMOUNTS = [50000, 70000, 80000]

# The seeded warehouse holds orders across Q4 2025 and shipments in March 2025.
PERIODS = [
    {"label": "the fourth quarter of 2025", "start": "2025-10-01", "end": "2025-12-31"},
    {"label": "Q4 2025", "start": "2025-10-01", "end": "2025-12-31"},
    {"label": "October 2025", "start": "2025-10-01", "end": "2025-10-31"},
    {"label": "November 2025", "start": "2025-11-01", "end": "2025-11-30"},
    {"label": "December 2025", "start": "2025-12-01", "end": "2025-12-31"},
    {"label": "2025", "start": "2025-01-01", "end": "2025-12-31"},
]
SHIPPING_PERIODS = [
    {"label": "March 2025", "start": "2025-03-01", "end": "2025-03-31"},
    {"label": "the week of 16 March 2025", "start": "2025-03-16", "end": "2025-03-22"},
]

SLOTS: dict[str, list] = {
    "region": REGIONS,
    "carrier": CARRIERS,
    "customer": CUSTOMERS,
    "product": PRODUCTS,
    "sku": SKUS,
    "n": NS,
    "amount": AMOUNTS,
    "period": PERIODS,
    "shipping_period": SHIPPING_PERIODS,
}

# The joins, written once. A template that needs sales by region uses this.
SALES_BY_REGION = (
    "FROM orders o "
    "JOIN customers c ON c.customer_id = o.customer_id "
    "JOIN regions r ON r.region_id = c.region_id"
)
ITEMS_TO_PRODUCTS = (
    "FROM order_items oi JOIN products p ON p.product_id = oi.product_id"
)


@dataclass(frozen=True)
class Template:
    name: str
    questions: tuple[str, ...]
    sql: str
    slots: tuple[str, ...] = ()
    holdout: bool = False      # the whole shape is kept out of training
    allow_empty: bool = False  # an empty result is the right answer here


T = Template

TEMPLATES: list[Template] = [
    # ---------------------------------------------------------- by region ----
    T("sales_by_region",
      ("What is the total order amount for each region?",
       "Total sales by region",
       "Show me sales per region",
       "Break down order amounts by region"),
      f"SELECT r.region_name, SUM(o.amount) AS total_amount {SALES_BY_REGION} "
      "GROUP BY r.region_name ORDER BY total_amount DESC"),

    T("top_region",
      ("Which region had the highest total order amount?",
       "Which region sold the most?",
       "Which region leads on sales?",
       "What was our best region by sales?"),
      f"SELECT r.region_name, SUM(o.amount) AS total_amount {SALES_BY_REGION} "
      "GROUP BY r.region_name ORDER BY total_amount DESC LIMIT 1"),

    T("top_region_in_period",
      ("Which region had the highest sales in {period_label}?",
       "Best region by sales in {period_label}?",
       "Which region sold the most in {period_label}?",
       "Top region for {period_label}"),
      f"SELECT r.region_name, SUM(o.amount) AS total_amount {SALES_BY_REGION} "
      "WHERE o.order_date BETWEEN '{period_start}' AND '{period_end}' "
      "GROUP BY r.region_name ORDER BY total_amount DESC LIMIT 1",
      slots=("period",)),

    T("sales_in_region",
      ("What were total sales in the {region} region?",
       "How much did {region} sell?",
       "Total order amount for {region}",
       "Give me {region}'s sales total"),
      f"SELECT SUM(o.amount) AS total_amount {SALES_BY_REGION} "
      "WHERE r.region_name = '{region}'",
      slots=("region",)),

    T("customers_in_region",
      ("Which customers are in the {region} region?",
       "List the customers in {region}",
       "Who are our {region} customers?",
       "Show customers from {region}"),
      "SELECT c.name FROM customers c JOIN regions r ON r.region_id = c.region_id "
      "WHERE r.region_name = '{region}' ORDER BY c.name",
      slots=("region",)),

    T("customer_count_by_region",
      ("How many customers are in each region?",
       "Customer count per region",
       "Number of customers by region",
       "How many customers does each region have?"),
      "SELECT r.region_name, COUNT(*) AS customers "
      "FROM customers c JOIN regions r ON r.region_id = c.region_id "
      "GROUP BY r.region_name ORDER BY customers DESC"),

    # ------------------------------------------------------------- orders ----
    T("total_sales_in_period",
      ("What were total sales in {period_label}?",
       "Total order amount for {period_label}",
       "How much did we sell in {period_label}?",
       "Sum of orders in {period_label}"),
      "SELECT SUM(amount) AS total_amount FROM orders "
      "WHERE order_date BETWEEN '{period_start}' AND '{period_end}'",
      slots=("period",)),

    T("order_count_in_period",
      ("How many orders were placed in {period_label}?",
       "Number of orders in {period_label}",
       "Count the orders from {period_label}",
       "How many orders did we take in {period_label}?"),
      "SELECT COUNT(*) AS orders FROM orders "
      "WHERE order_date BETWEEN '{period_start}' AND '{period_end}'",
      slots=("period",)),

    T("best_month",
      ("Which month had the highest sales?",
       "Which was our best month?",
       "What month did we sell the most in?",
       "Our strongest month by order amount?"),
      "SELECT DATE_TRUNC('month', order_date) AS month, SUM(amount) AS total_amount "
      "FROM orders GROUP BY month ORDER BY total_amount DESC LIMIT 1"),

    T("sales_by_month",
      ("What were sales by month?",
       "Show monthly order totals",
       "Monthly sales",
       "Break sales down by month"),
      "SELECT DATE_TRUNC('month', order_date) AS month, SUM(amount) AS total_amount "
      "FROM orders GROUP BY month ORDER BY month"),

    T("average_order",
      ("What is the average order amount?",
       "Average order value",
       "How much is a typical order?",
       "Mean order amount"),
      "SELECT AVG(amount) AS average_amount FROM orders"),

    T("average_order_by_region",
      ("What is the average order amount for each region?",
       "Average order value by region",
       "Mean order amount per region",
       "How large is a typical order in each region?"),
      f"SELECT r.region_name, AVG(o.amount) AS average_amount {SALES_BY_REGION} "
      "GROUP BY r.region_name ORDER BY average_amount DESC"),

    T("orders_over_amount",
      ("How many orders were larger than {amount}?",
       "Count orders above {amount}",
       "How many orders came in over {amount}?",
       "Number of orders bigger than {amount}"),
      "SELECT COUNT(*) AS orders FROM orders WHERE amount > {amount}",
      slots=("amount",)),

    T("largest_orders",
      ("What are the {n} largest orders?",
       "Show the top {n} orders by amount",
       "List our {n} biggest orders",
       "The {n} highest order amounts"),
      "SELECT order_id, order_date, amount FROM orders "
      "ORDER BY amount DESC LIMIT {n}",
      slots=("n",)),

    T("latest_order",
      ("When was the most recent order?",
       "What is the date of the latest order?",
       "When did we last take an order?",
       "Date of the newest order"),
      "SELECT MAX(order_date) AS latest FROM orders"),

    # ---------------------------------------------------------- customers ----
    T("top_customers",
      ("Who are our top {n} customers by sales?",
       "The {n} biggest customers by order amount",
       "List the top {n} customers",
       "Which {n} customers spend the most?"),
      "SELECT c.name, SUM(o.amount) AS total_amount FROM orders o "
      "JOIN customers c ON c.customer_id = o.customer_id "
      "GROUP BY c.name ORDER BY total_amount DESC LIMIT {n}",
      slots=("n",)),

    T("customer_with_most_orders",
      ("Which customer placed the most orders?",
       "Who orders from us most often?",
       "Which customer has the highest number of orders?",
       "Our most frequent customer?"),
      "SELECT c.name, COUNT(*) AS orders FROM orders o "
      "JOIN customers c ON c.customer_id = o.customer_id "
      "GROUP BY c.name ORDER BY orders DESC LIMIT 1"),

    T("customer_total",
      ("How much has {customer} spent?",
       "Total order amount for {customer}",
       "What are {customer}'s total sales?",
       "How much business has {customer} given us?"),
      "SELECT SUM(o.amount) AS total_amount FROM orders o "
      "JOIN customers c ON c.customer_id = o.customer_id "
      "WHERE c.name = '{customer}'",
      slots=("customer",)),

    T("orders_per_customer",
      ("How many orders has each customer placed?",
       "Order count by customer",
       "Number of orders per customer",
       "Show how many times each customer ordered"),
      "SELECT c.name, COUNT(*) AS orders FROM orders o "
      "JOIN customers c ON c.customer_id = o.customer_id "
      "GROUP BY c.name ORDER BY orders DESC"),

    T("customer_region",
      ("Which region is {customer} in?",
       "What region does {customer} belong to?",
       "Where is {customer} based?",
       "{customer} is in which region?"),
      "SELECT r.region_name FROM customers c "
      "JOIN regions r ON r.region_id = c.region_id WHERE c.name = '{customer}'",
      slots=("customer",)),

    # ---------------------------------------------------------- shipments ----
    T("shipments_missing_units",
      ("How many shipments have no units recorded?",
       "Count shipments with a missing unit count",
       "How many shipments are missing their units?",
       "Number of shipments where units were not recorded"),
      "SELECT COUNT(*) AS shipments FROM shipments WHERE units IS NULL"),

    T("which_shipments_missing_units",
      ("Which shipments have no units recorded?",
       "List the shipments missing their unit count",
       "Show shipments where units is not recorded",
       "Which shipments are missing units?"),
      "SELECT shipment_id, shipped_on, carrier FROM shipments "
      "WHERE units IS NULL ORDER BY shipment_id"),

    T("shipments_by_carrier",
      ("How many shipments did each carrier make?",
       "Shipment count by carrier",
       "Number of shipments per carrier",
       "Break shipments down by carrier"),
      "SELECT carrier, COUNT(*) AS shipments FROM shipments "
      "GROUP BY carrier ORDER BY shipments DESC"),

    T("units_by_carrier",
      ("Which carrier shipped the most units?",
       "Who shipped the most units?",
       "Top carrier by units shipped",
       "Which carrier moved the largest number of units?"),
      "SELECT carrier, SUM(units) AS units FROM shipments "
      "GROUP BY carrier ORDER BY units DESC LIMIT 1"),

    T("units_for_carrier",
      ("How many units did {carrier} ship?",
       "Total units shipped by {carrier}",
       "{carrier} shipped how many units?",
       "Sum of units for {carrier}"),
      "SELECT SUM(units) AS units FROM shipments WHERE carrier = '{carrier}'",
      slots=("carrier",)),

    T("shipments_in_period",
      ("How many shipments went out in {shipping_period_label}?",
       "Shipment count for {shipping_period_label}",
       "How many shipments were sent in {shipping_period_label}?",
       "Number of shipments during {shipping_period_label}"),
      "SELECT COUNT(*) AS shipments FROM shipments "
      "WHERE shipped_on BETWEEN '{shipping_period_start}' AND '{shipping_period_end}'",
      slots=("shipping_period",)),

    # ----------------------------------------------------------- products ----
    T("product_revenue",
      ("Which product generated the most revenue?",
       "Our highest earning product?",
       "Which product brought in the most money?",
       "Top product by revenue"),
      f"SELECT p.name, SUM(oi.qty * oi.unit_price) AS revenue {ITEMS_TO_PRODUCTS} "
      "GROUP BY p.name ORDER BY revenue DESC LIMIT 1"),

    T("revenue_by_product",
      ("What is the revenue for each product?",
       "Revenue by product",
       "Show product revenue",
       "Break revenue down by product"),
      f"SELECT p.name, SUM(oi.qty * oi.unit_price) AS revenue {ITEMS_TO_PRODUCTS} "
      "GROUP BY p.name ORDER BY revenue DESC"),

    T("units_of_product",
      ("How many units of {product} were ordered?",
       "Quantity ordered of {product}",
       "How much {product} did customers order?",
       "Total quantity for {product}"),
      f"SELECT SUM(oi.qty) AS quantity {ITEMS_TO_PRODUCTS} "
      "WHERE p.name = '{product}'",
      slots=("product",)),

    T("most_expensive_products",
      ("What are the {n} most expensive products by unit cost?",
       "The {n} priciest products",
       "List the {n} highest unit costs",
       "Which {n} products cost the most per unit?"),
      "SELECT name, unit_cost FROM products ORDER BY unit_cost DESC LIMIT {n}",
      slots=("n",)),

    T("product_by_sku",
      ("What is the unit cost of {sku}?",
       "How much does {sku} cost per unit?",
       "Unit cost for SKU {sku}",
       "Price of {sku}"),
      "SELECT name, unit_cost FROM products WHERE sku = '{sku}'",
      slots=("sku",)),

    # -------------------------------------------------- held-out shapes ------
    # Never trained on: these measure whether the model generalises to a
    # question type it has not seen, rather than to a new wording of one.
    T("average_unit_price_by_product",
      ("What is the average unit price for each product?",
       "Average price per product",
       "Mean unit price by product",
       "Show the average selling price of each product"),
      f"SELECT p.name, AVG(oi.unit_price) AS average_price {ITEMS_TO_PRODUCTS} "
      "GROUP BY p.name ORDER BY average_price DESC",
      holdout=True),

    T("carrier_with_most_missing",
      ("Which carrier has the most shipments with no units recorded?",
       "Carrier with the most missing unit counts",
       "Which carrier is missing the most unit numbers?",
       "Who has the most shipments missing units?"),
      "SELECT carrier, COUNT(*) AS shipments FROM shipments "
      "WHERE units IS NULL GROUP BY carrier ORDER BY shipments DESC LIMIT 1",
      holdout=True),

    T("distinct_carriers",
      ("How many different carriers do we use?",
       "Count our carriers",
       "How many carriers are there?",
       "Number of distinct carriers"),
      "SELECT COUNT(DISTINCT carrier) AS carriers FROM shipments",
      holdout=True),

    T("average_order_by_month",
      ("What is the average order amount by month?",
       "Average order value per month",
       "Monthly average order amount",
       "Show the mean order size for each month"),
      "SELECT DATE_TRUNC('month', order_date) AS month, AVG(amount) AS average_amount "
      "FROM orders GROUP BY month ORDER BY month",
      holdout=True),

    T("region_share",
      ("How many orders came from each region?",
       "Order count by region",
       "Number of orders per region",
       "Break the order count down by region"),
      f"SELECT r.region_name, COUNT(*) AS orders {SALES_BY_REGION} "
      "GROUP BY r.region_name ORDER BY orders DESC",
      holdout=True),
]

# Two slots at once. These are where the model learns to put a value in a
# WHERE clause rather than to recite a template: the same question shape with
# a different region and a different period has to produce different SQL.
TEMPLATES += [
    T("sales_in_region_in_period",
      ("What were sales in the {region} region in {period_label}?",
       "How much did {region} sell in {period_label}?",
       "Total order amount for {region} in {period_label}",
       "{region} sales for {period_label}",
       "Give me {region}'s total for {period_label}"),
      f"SELECT SUM(o.amount) AS total_amount {SALES_BY_REGION} "
      "WHERE r.region_name = '{region}' "
      "AND o.order_date BETWEEN '{period_start}' AND '{period_end}'",
      slots=("region", "period")),

    T("orders_for_customer_in_period",
      ("How many orders did {customer} place in {period_label}?",
       "Order count for {customer} in {period_label}",
       "How often did {customer} order in {period_label}?",
       "Number of {customer} orders in {period_label}",
       "{customer}'s orders during {period_label}"),
      "SELECT COUNT(*) AS orders FROM orders o "
      "JOIN customers c ON c.customer_id = o.customer_id "
      "WHERE c.name = '{customer}' "
      "AND o.order_date BETWEEN '{period_start}' AND '{period_end}'",
      slots=("customer", "period")),

    T("spend_for_customer_in_period",
      ("How much did {customer} spend in {period_label}?",
       "Total sales to {customer} in {period_label}",
       "{customer} spent how much in {period_label}?",
       "Order amount for {customer} during {period_label}",
       "What did {customer} buy in {period_label}, in money?"),
      "SELECT SUM(o.amount) AS total_amount FROM orders o "
      "JOIN customers c ON c.customer_id = o.customer_id "
      "WHERE c.name = '{customer}' "
      "AND o.order_date BETWEEN '{period_start}' AND '{period_end}'",
      slots=("customer", "period")),

    T("top_customers_in_region",
      ("Who are the top {n} customers in {region}?",
       "The {n} biggest customers in the {region} region",
       "List the top {n} {region} customers by sales",
       "Which {n} customers in {region} spend the most?",
       "Top {n} by sales in {region}"),
      f"SELECT c.name, SUM(o.amount) AS total_amount {SALES_BY_REGION} "
      "WHERE r.region_name = '{region}' "
      "GROUP BY c.name ORDER BY total_amount DESC LIMIT {n}",
      slots=("n", "region")),

    T("orders_over_amount_in_region",
      ("How many orders over {amount} came from {region}?",
       "Count {region} orders above {amount}",
       "Number of orders bigger than {amount} in the {region} region",
       "{region} orders over {amount}",
       "How many {region} orders were larger than {amount}?"),
      f"SELECT COUNT(*) AS orders {SALES_BY_REGION} "
      "WHERE r.region_name = '{region}' AND o.amount > {amount}",
      slots=("amount", "region")),

    T("product_revenue_in_period",
      ("What was the revenue for {product} in {period_label}?",
       "How much did {product} earn in {period_label}?",
       "Revenue from {product} during {period_label}",
       "{product} revenue for {period_label}",
       "Money taken for {product} in {period_label}"),
      "SELECT SUM(oi.qty * oi.unit_price) AS revenue FROM order_items oi "
      "JOIN products p ON p.product_id = oi.product_id "
      "JOIN orders o ON o.order_id = oi.order_id "
      "WHERE p.name = '{product}' "
      "AND o.order_date BETWEEN '{period_start}' AND '{period_end}'",
      slots=("product", "period")),

    T("units_for_carrier_in_period",
      ("How many units did {carrier} ship in {shipping_period_label}?",
       "Units shipped by {carrier} during {shipping_period_label}",
       "{carrier}'s units for {shipping_period_label}",
       "Total units {carrier} moved in {shipping_period_label}",
       "How much did {carrier} ship in {shipping_period_label}?"),
      "SELECT SUM(units) AS units FROM shipments WHERE carrier = '{carrier}' "
      "AND shipped_on BETWEEN '{shipping_period_start}' AND '{shipping_period_end}'",
      slots=("carrier", "shipping_period")),

    T("shipments_for_carrier_in_period",
      ("How many shipments did {carrier} make in {shipping_period_label}?",
       "Shipment count for {carrier} in {shipping_period_label}",
       "How many times did {carrier} ship during {shipping_period_label}?",
       "{carrier} shipments in {shipping_period_label}",
       "Number of {carrier} shipments for {shipping_period_label}"),
      "SELECT COUNT(*) AS shipments FROM shipments WHERE carrier = '{carrier}' "
      "AND shipped_on BETWEEN '{shipping_period_start}' AND '{shipping_period_end}'",
      slots=("carrier", "shipping_period")),

    # The other direction of the sort. A model trained only on "highest"
    # answers "lowest" with DESC, and the answer looks right.
    T("worst_region",
      ("Which region had the lowest total order amount?",
       "Which region sold the least?",
       "Our weakest region by sales?",
       "Which region is at the bottom on sales?",
       "Lowest selling region"),
      f"SELECT r.region_name, SUM(o.amount) AS total_amount {SALES_BY_REGION} "
      "GROUP BY r.region_name ORDER BY total_amount ASC LIMIT 1"),

    T("worst_month",
      ("Which month had the lowest sales?",
       "Our worst month?",
       "Which month did we sell the least in?",
       "Weakest month by order amount",
       "The month with the smallest total"),
      "SELECT DATE_TRUNC('month', order_date) AS month, SUM(amount) AS total_amount "
      "FROM orders GROUP BY month ORDER BY total_amount ASC LIMIT 1"),

    T("smallest_orders",
      ("What are the {n} smallest orders?",
       "Show the {n} lowest order amounts",
       "List our {n} smallest orders",
       "The {n} cheapest orders",
       "Bottom {n} orders by amount"),
      "SELECT order_id, order_date, amount FROM orders "
      "ORDER BY amount ASC LIMIT {n}",
      slots=("n",)),

    T("cheapest_products",
      ("What are the {n} cheapest products by unit cost?",
       "The {n} lowest unit costs",
       "List the {n} least expensive products",
       "Which {n} products cost the least per unit?",
       "Bottom {n} products by cost"),
      "SELECT name, unit_cost FROM products ORDER BY unit_cost ASC LIMIT {n}",
      slots=("n",)),
]


# ------------------------------------------------------------ generation ----

def _combinations(template: Template, rng: random.Random) -> list[dict]:
    """Every slot combination, capped, so one template cannot dominate."""
    if not template.slots:
        return [{}]
    pools = [SLOTS[name] for name in template.slots]
    combos = [dict(zip(template.slots, values))
              for values in itertools.product(*pools)]
    rng.shuffle(combos)
    return combos[:MAX_PER_TEMPLATE]


def _fill(text: str, values: dict) -> str:
    flat: dict[str, object] = {}
    for name, value in values.items():
        if isinstance(value, dict):
            for key, inner in value.items():
                flat[f"{name}_{key}"] = inner
        else:
            flat[name] = value
    return text.format(**flat)


def canonical(sql: str) -> str:
    """One shape for every target, exactly as ml/spider_prep.py does it."""
    tree = sqlglot.parse_one(sql, read="postgres")
    return normalize_identifiers(tree, dialect="postgres").sql(dialect="postgres")


def tables_in(sql: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read="postgres")
    return sorted({t.name.lower() for t in tree.find_all(exp.Table) if t.name})


@dataclass
class Pair:
    question: str
    sql: str
    tables: list[str]
    template: str
    split: str
    kind: str                     # trained | new phrasing | new shape
    rows: int = field(default=-1)


def build() -> list[Pair]:
    rng = random.Random(SEED)
    pairs: dict[tuple[str, str], Pair] = {}

    for template in TEMPLATES:
        for values in _combinations(template, rng):
            for index, question_template in enumerate(template.questions):
                question = _fill(question_template, values)
                sql = canonical(_fill(template.sql, values))

                last = index == len(template.questions) - 1
                if template.holdout:
                    split, kind = "test", "new shape"
                elif last:
                    split, kind = "test", "new phrasing"
                else:
                    # A tenth of the rest is development data, chosen by hash
                    # so the split is the same on every run.
                    digest = hashlib.sha1(f"{question}|{sql}".encode()).hexdigest()
                    split = "dev" if int(digest[:4], 16) % 10 == 0 else "train"
                    kind = "trained"

                pairs.setdefault((question, sql), Pair(
                    question=question, sql=sql, tables=tables_in(sql),
                    template=template.name, split=split, kind=kind,
                ))

    return list(pairs.values())


# ---------------------------------------------------------- verification ----

def warehouse_dsn() -> str:
    """The read-only role, against the seeded warehouse.

    Verification runs as speakql_ro like everything else on the read path: a
    pair that only works with more privilege than the product has is not a
    pair the product can answer.
    """
    dsn = os.environ.get("SYNTH_DSN") or os.environ.get("RO_DSN", "")
    if not dsn:
        env = BACKEND / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("RO_DSN="):
                    dsn = line.split("=", 1)[1].strip()
    if not dsn:
        raise SystemExit("set RO_DSN (or SYNTH_DSN) to the seeded warehouse")
    return dsn


def verify(pairs: list[Pair]) -> tuple[list[Pair], list[tuple[Pair, str]]]:
    """Run every pair. Keep what runs and returns something."""
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(warehouse_dsn(), future=True)
    kept: list[Pair] = []
    dropped: list[tuple[Pair, str]] = []
    allow_empty = {t.name for t in TEMPLATES if t.allow_empty}

    with engine.connect() as conn:
        for pair in pairs:
            try:
                rows = conn.execute(text(pair.sql)).fetchmany(5)
            except Exception as exc:  # noqa: BLE001 - the point is to catch these
                conn.rollback()
                dropped.append((pair, str(exc).splitlines()[0][:120]))
                continue
            pair.rows = len(rows)
            # An aggregate over nothing returns one row of NULL, not no rows --
            # so "how much did East sell in November" looks verified while
            # answering nothing. As a test pair it is worthless: a wrong query
            # returns NULL just as readily.
            empty = not rows or all(
                all(value is None for value in row) for row in rows
            )
            if empty and pair.template not in allow_empty:
                dropped.append((pair, "no rows" if not rows else "all NULL"))
                continue
            kept.append(pair)

    engine.dispose()
    return kept, dropped


def main() -> None:
    pairs = build()
    print(f"{len(pairs)} pairs generated from {len(TEMPLATES)} templates")

    kept, dropped = verify(pairs)
    print(f"{len(kept)} verified, {len(dropped)} dropped")
    for pair, reason in dropped[:5]:
        print(f"  dropped: {pair.question[:60]!r}: {reason}")

    for split in ("train", "dev", "test"):
        rows = [p for p in kept if p.split == split]
        count = write_jsonl(SYNTH / f"{split}.jsonl", [
            {"question": p.question, "sql": p.sql, "tables": p.tables,
             "template": p.template, "kind": p.kind, "rows": p.rows}
            for p in rows
        ])
        kinds = {k: sum(1 for p in rows if p.kind == k) for k in
                 {p.kind for p in rows}}
        print(f"{split}: {count} pairs {kinds}")


if __name__ == "__main__":
    main()
