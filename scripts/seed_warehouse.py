"""Generate the two seeded warehouses, and check every guarantee (§11.4).

    python scripts/seed_warehouse.py seed     fill northwind_dw and harbor_dw
    python scripts/seed_warehouse.py check    stop at the first guarantee that fails

Run by bootstrap.sh as speakql_owner: `seed` once sql/20_warehouse.sql has
made the 18 tables, and `check` as the very last step, so nothing after it can
undo what it checked. tests/test_seed.py runs the same checks as speakql_ro.

Deterministic. Each company has a fixed seed of its own, and every month is
drawn from a random stream of its own, generated in full and then cut at the
run date. So the same run date always gives the same data, and a later run
date gives the same history plus the days in between. The run date is the
database's CURRENT_DATE -- the day the warehouse answers "this month" against
-- and it is recorded in a comment on each warehouse, so a test can check "up
to the day before bootstrap" on any later day.

Both companies are fictional and every row is generated. Fixed by hand is only
what an answer depends on: Northwind's 21 orders of Q4 2025, the nine
shipments of week 12, and the regions, customers and products seeded before
v6.1, which keep their ids and names. The best months are made true by the
monthly revenue plan and by the prices of March 2025, not by editing totals.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

Dataset = dict[str, list[dict]]

# ---------------------------------------------------------- the 18 tables ----
# §11.4, column for column. The first column of each is its primary key, and
# the order is the load order: every foreign key points at a table above it.

TABLES: dict[str, tuple[str, ...]] = {
    "regions":         ("region_id", "region_name"),
    "categories":      ("category_id", "name"),
    "suppliers":       ("supplier_id", "name", "country"),
    "customers":       ("customer_id", "name", "region_id", "created_at"),
    "employees":       ("employee_id", "name", "title", "region_id", "hired_on"),
    "products":        ("product_id", "sku", "name", "unit_cost",
                        "category_id", "supplier_id"),
    "warehouses":      ("warehouse_id", "name", "city", "region_id"),
    "promotions":      ("promotion_id", "name", "starts_on", "ends_on",
                        "discount_pct"),
    "sales_targets":   ("target_id", "region_id", "year", "quarter",
                        "target_amount"),
    "orders":          ("order_id", "customer_id", "order_date", "amount",
                        "employee_id"),
    "order_items":     ("order_item_id", "order_id", "product_id", "qty",
                        "unit_price"),
    "shipments":       ("shipment_id", "order_id", "shipped_on", "carrier",
                        "units", "warehouse_id"),
    "returns":         ("return_id", "order_id", "product_id", "returned_on",
                        "qty", "reason"),
    "invoices":        ("invoice_id", "order_id", "issued_on", "due_on",
                        "amount", "status"),
    "payments":        ("payment_id", "invoice_id", "paid_on", "amount",
                        "method"),
    "inventory":       ("inventory_id", "warehouse_id", "product_id",
                        "on_hand", "reorder_level", "counted_on"),
    "purchase_orders": ("po_id", "supplier_id", "ordered_on", "amount",
                        "status"),
    "support_tickets": ("ticket_id", "customer_id", "opened_on", "closed_on",
                        "category", "priority"),
}

# (table, column, referenced table, referenced column): every join path.
FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("customers", "region_id", "regions", "region_id"),
    ("employees", "region_id", "regions", "region_id"),
    ("products", "category_id", "categories", "category_id"),
    ("products", "supplier_id", "suppliers", "supplier_id"),
    ("warehouses", "region_id", "regions", "region_id"),
    ("sales_targets", "region_id", "regions", "region_id"),
    ("orders", "customer_id", "customers", "customer_id"),
    ("orders", "employee_id", "employees", "employee_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("shipments", "order_id", "orders", "order_id"),
    ("shipments", "warehouse_id", "warehouses", "warehouse_id"),
    ("returns", "order_id", "orders", "order_id"),
    ("returns", "product_id", "products", "product_id"),
    ("invoices", "order_id", "orders", "order_id"),
    ("payments", "invoice_id", "invoices", "invoice_id"),
    ("inventory", "warehouse_id", "warehouses", "warehouse_id"),
    ("inventory", "product_id", "products", "product_id"),
    ("purchase_orders", "supplier_id", "suppliers", "supplier_id"),
    ("support_tickets", "customer_id", "customers", "customer_id"),
)

# The columns v6.1 adds to the six original tables: nullable, so every query
# written against the six still runs.
ADDED_COLUMNS = (("orders", "employee_id"), ("products", "category_id"),
                 ("products", "supplier_id"), ("shipments", "warehouse_id"))

# ------------------------------------------------------------ fixed facts ----

FIRST_DAY = date(2025, 1, 1)

# The span ends the day before the run date, with an order on that day. That
# day must fall after Q4 2025, or the order would land in a quarter that has
# to stay exactly as seeded.
EARLIEST_RUN_DATE = date(2026, 1, 2)

# Seeded before v6.1, and kept: ids 1-5, 1-8 and 1-6, names unchanged.
NORTHWIND_REGIONS = ("West", "North", "South", "East", "Central")
NORTHWIND_CUSTOMERS = (
    ("Aster Retail",      1, date(2024, 3, 11)),
    ("Brightline Foods",  1, date(2024, 6, 2)),
    ("Cobalt Industrial", 2, date(2024, 1, 19)),
    ("Dunmore Supply",    2, date(2024, 8, 27)),
    ("Eastgate Traders",  3, date(2025, 2, 14)),
    ("Fairhaven Group",   3, date(2024, 11, 5)),
    ("Granby Wholesale",  4, date(2025, 1, 30)),
    ("Halloway Partners", 5, date(2024, 9, 16)),
)
# (sku, name, unit_cost, category_id, supplier_id)
NORTHWIND_PRODUCTS = (
    ("BR-2214", "Barrier film, 50 micron",    "142.00", 1, 1),
    ("BR-2218", "Barrier film, 80 micron",    "188.50", 1, 1),
    ("CT-1180", "Carton, 12 litre",            "38.25", 2, 2),
    ("CT-1182", "Carton, 20 litre",            "51.75", 2, 2),
    ("LB-4400", "Label roll, thermal",         "12.40", 3, 3),
    ("PL-7710", "Pallet wrap, heavy",          "96.00", 4, 4),
    # added in v6.1
    ("BR-2230", "Barrier film, 120 micron",   "236.00", 1, 1),
    ("CT-1186", "Carton, 40 litre",            "72.40", 2, 6),
    ("CT-1190", "Carton, double wall",         "88.10", 2, 6),
    ("LB-4410", "Label roll, direct print",     "9.85", 3, 3),
    ("LB-4420", "Label sheet, A4 matte",        "6.30", 3, 3),
    ("PL-7720", "Pallet wrap, light",          "64.50", 4, 4),
    ("PL-7730", "Pallet wrap, pre-stretched", "112.00", 4, 4),
    ("TP-3100", "Packing tape, clear 48 mm",    "4.20", 5, 5),
    ("TP-3110", "Packing tape, printed",        "6.90", 5, 5),
    ("TP-3120", "Strapping band, polyester",   "58.00", 5, 5),
    ("VF-5200", "Air pillows, roll",           "31.50", 6, 4),
    ("VF-5210", "Paper void fill, bale",       "44.75", 6, 2),
)

# Q4 2025, exactly as seeded before v6.1: (customer_id, order_date, amount).
# West (customers 1 and 2) leads with 482,140.00; North (3 and 4) follows
# with 355,200.00. No other order is placed in the quarter.
Q4_2025 = (date(2025, 10, 1), date(2025, 12, 31))
Q4_2025_ORDERS = (
    (1, date(2025, 10, 4), "61200.00"), (1, date(2025, 11, 12), "88400.00"),
    (1, date(2025, 12, 2), "74300.00"), (2, date(2025, 10, 21), "92800.00"),
    (2, date(2025, 11, 29), "77140.00"), (2, date(2025, 12, 18), "88300.00"),
    (3, date(2025, 10, 9), "74100.00"), (3, date(2025, 11, 3), "96200.00"),
    (3, date(2025, 12, 11), "81900.00"), (4, date(2025, 10, 27), "45800.00"),
    (4, date(2025, 12, 5), "57200.00"), (5, date(2025, 10, 15), "71300.00"),
    (5, date(2025, 11, 20), "84600.00"), (6, date(2025, 12, 22), "69500.00"),
    (6, date(2025, 11, 8), "73000.00"), (7, date(2025, 10, 30), "92400.00"),
    (7, date(2025, 12, 14), "66100.00"), (7, date(2025, 11, 17), "83400.00"),
    (8, date(2025, 10, 12), "58900.00"), (8, date(2025, 11, 25), "64200.00"),
    (8, date(2025, 12, 9), "64200.00"),
)
WEST_Q4_2025 = Decimal("482140.00")
NORTH_Q4_2025 = Decimal("355200.00")

# "Which was our best month?" has three right answers, one per reading.
BEST_REVENUE_MONTH = (2025, 6)
BEST_GROWTH_MONTH = (2025, 4)      # over the month before, absolute and relative
BEST_MARGIN_MONTH = (2025, 3)      # (revenue - qty x unit_cost) / revenue

# Week 12 of 2025 is the ISO week -- how PostgreSQL's EXTRACT(WEEK ...)
# counts -- Monday 17 to Sunday 23 March. It holds exactly the nine shipments
# seeded before v6.1, with their dates, carriers and units, except that 84112
# moves from Sunday 16 March (ISO week 11) to Monday 17 March. Three have no
# units: the cells the correction workflow fills.
WEEK_12 = (2025, 12)
WEEK_12_SHIPMENTS = (      # (shipment_id, shipped_on, carrier, units)
    (84112, date(2025, 3, 17), "Redline",    198),
    (84114, date(2025, 3, 17), "Redline",    214),
    (84116, date(2025, 3, 18), "Northbound", 187),
    (84117, date(2025, 3, 19), "Northbound", None),
    (84119, date(2025, 3, 20), "Redline",    241),
    (84120, date(2025, 3, 20), "Coastal",    None),
    (84121, date(2025, 3, 21), "Coastal",    132),
    (84122, date(2025, 3, 21), "Northbound", None),
    (84123, date(2025, 3, 22), "Coastal",    111),
)
WEEK_12_UNITS = 1083
# The correction the demo makes: 84120 was 203 units -- its order was for 203,
# so the value has a source -- and the week's total becomes 1,286.
FILLED_SHIPMENT, FILLED_UNITS, FILLED_TOTAL = 84120, 203, 1286
# Nothing else ships on these days, so "the week of 16 March" -- the wording
# of the training pairs before v6.1 -- finds the same nine.
QUIET_DAYS = (date(2025, 3, 16), date(2025, 3, 23))

# Nothing records a cancellation or a customer's status, so a churn question
# is refused with the data it would need.
CHURN_WORDS = ("churn", "cancel")

# ------------------------------------------------------- the shape of trade --

MARKUP = (14_000, 16_600)          # unit_price / unit_cost, in basis points
BEST_MARGIN_MARKUP = (18_000, 19_200)   # March 2025: wholly above MARKUP
QTY_MIN, QTY_MAX, LAST_QTY_MAX = 5, 300, 400
ITEM_COUNTS = (1, 2, 2, 3, 3, 4)
LEAD_DAYS = (1, 1, 2, 2, 2, 3, 3, 4, 5, 6)
RETURN_RATE = 0.06
RETURN_REASONS = ("damaged in transit", "wrong item sent", "quality issue",
                  "over-delivered")
PAYMENT_TERMS = 30
UNPAID_RATE, INSTALMENT_RATE = 0.03, 0.10
PAYMENT_METHODS = ("bank transfer", "bank transfer", "card", "cheque")
TICKET_CATEGORIES = ("delivery", "billing", "product quality",
                     "damaged goods", "order change", "returns")
PRIORITIES, PRIORITY_WEIGHTS = ("low", "normal", "high", "urgent"), (3, 5, 2, 1)
RESOLUTION_DAYS = (0, 1, 1, 2, 3, 5, 8, 13)
DISCOUNTS = ("5.00", "7.50", "10.00", "12.50", "15.00", "20.00")
CUSTOMERS_FROM, CUSTOMERS_UNTIL = date(2023, 1, 1), date(2025, 9, 30)
GAPS_BEFORE = date(2025, 7, 1)     # Harbor's own gaps fall in its first half-year
CLOSING = 1_000_000                # sorts the span's closing order last in its day

# Northwind's revenue plan, in whole currency units per month. Q4 2025 is the
# fixed orders (496,500.00, 566,940.00 and 501,500.00). The margins are wide on
# purpose, so the best months hold for every run date: June 2025 leads every
# other month by more than 80,000; April's rise over March (about +290,000,
# +97%) is more than three times any other month's, either way it is measured;
# and every month from 2026 on stays far below June 2025.
NORTHWIND_2025 = {1: 430_000, 2: 415_000, 3: 300_000, 4: 590_000,
                  5: 610_000, 6: 690_000, 7: 560_000, 8: 540_000,
                  9: 470_000}
NORTHWIND_LATER = {1: 455_000, 2: 445_000, 3: 470_000, 4: 500_000,
                   5: 515_000, 6: 530_000, 7: 505_000, 8: 495_000,
                   9: 480_000, 10: 510_000, 11: 520_000, 12: 490_000}

NORTHWIND_SEED = "speakql-northwind-dw-v6.1"
HARBOR_SEED = "speakql-harbor-dw-v6.1"


def _northwind_target(year: int, month: int) -> int:
    plan = NORTHWIND_2025 if year == 2025 else NORTHWIND_LATER
    return plan[month] * 100


def _harbor_target(year: int, month: int) -> int:
    rng = random.Random(f"{HARBOR_SEED}:{year}-{month:02d}:target")
    return rng.randint(140_000, 230_000) * 100


@dataclass(frozen=True)
class Profile:
    """One company: its fixed seed, its names and the shape of its trade."""

    key: str
    database: str
    seed: str
    regions: tuple[str, ...]
    region_weights: tuple[int, ...]
    categories: tuple[str, ...]
    suppliers: tuple[tuple[str, str], ...]          # name, country
    products: tuple[tuple[str, str, str, int, int], ...]
    customers: tuple[tuple[str, int, date], ...]    # kept with their ids
    more_customers: tuple[str, ...]                 # region and date drawn
    employees: tuple[tuple[str, str, int, date], ...]
    warehouses: tuple[tuple[str, str], ...]         # one per region, in order
    carriers: tuple[str, ...]
    promotion_themes: tuple[str, ...]
    target: Callable[[int, int], int]               # a month's revenue, cents
    order_goal: tuple[int, int]                     # one order's value, cents
    tickets_per_month: tuple[int, int]
    purchase_orders_per_month: tuple[int, int]
    purchase_order_value: tuple[int, int]           # whole currency units
    fixed_orders: tuple = ()
    best_margin_month: tuple[int, int] | None = None
    week_12: bool = False
    first_shipment_id: int = 1
    gaps: int = 0                                   # shipments left without units


NORTHWIND = Profile(
    key="northwind", database="northwind_dw", seed=NORTHWIND_SEED,
    regions=NORTHWIND_REGIONS, region_weights=(5, 4, 3, 3, 2),
    categories=("Barrier films", "Cartons", "Labels", "Pallet wrap",
                "Tapes and strapping", "Void fill"),
    suppliers=(("Kestrel Polymers", "Germany"), ("Mondego Papers", "Portugal"),
               ("Tallis Label Co", "United Kingdom"),
               ("Varuna Plastics", "India"), ("Lindqvist Tapes", "Sweden"),
               ("Coromandel Fibre", "India")),
    products=NORTHWIND_PRODUCTS,
    customers=NORTHWIND_CUSTOMERS,
    more_customers=(
        "Ironbridge Foods", "Juniper Logistics", "Kingsway Pharma",
        "Larkfield Dairy", "Meridian Home", "Netherby Brewing",
        "Oakhurst Bakeries", "Pemberton Cosmetics", "Quinton Tools",
        "Redfern Apparel", "Silverbirch Organics", "Thornbury Chemicals",
        "Upland Garden Centres", "Vale Street Pets", "Whitmore Hardware",
        "Yardley Print", "Zephyr Sports", "Alder Creek Coffee",
        "Bramble and Co", "Castleford Ceramics", "Dovetail Furniture",
        "Elmstead Farms", "Foxglove Florists", "Greystone Labs",
        "Hawthorne Kitchens", "Ivywood Toys", "Lowther Wines",
        "Marlow Stationery", "Nettlebed Soap", "Orchard Lane Juices",
        "Pinecroft Candles", "Rosedale Health",
    ),
    employees=(
        ("Priya Raman", "Regional sales lead", 1, date(2019, 4, 1)),
        ("Daniel Okoro", "Regional sales lead", 2, date(2018, 9, 17)),
        ("Sofia Marquez", "Regional sales lead", 3, date(2020, 1, 13)),
        ("Liam Chen", "Regional sales lead", 4, date(2019, 11, 4)),
        ("Aisha Khan", "Regional sales lead", 5, date(2021, 3, 8)),
        ("Hannah Becker", "Senior account manager", 1, date(2020, 8, 3)),
        ("Tomas Silva", "Account manager", 1, date(2022, 6, 20)),
        ("Nina Petrova", "Senior account manager", 1, date(2021, 5, 31)),
        ("Arjun Mehta", "Account manager", 2, date(2023, 2, 27)),
        ("Grace Mwangi", "Account manager", 2, date(2025, 5, 12)),
        ("Oliver Grant", "Senior account manager", 3, date(2021, 10, 18)),
        ("Mei Tanaka", "Account manager", 3, date(2025, 8, 4)),
        ("Rahul Verma", "Account manager", 4, date(2022, 1, 10)),
        ("Chloe Dubois", "Account manager", 4, date(2024, 4, 15)),
        ("Samuel Adeyemi", "Account manager", 5, date(2023, 9, 25)),
    ),
    warehouses=(("West distribution centre", "Westbury"),
                ("North distribution centre", "Northgate"),
                ("South distribution centre", "Southmere"),
                ("East distribution centre", "Eastwick"),
                ("Central distribution centre", "Midfield")),
    carriers=("Redline", "Northbound", "Coastal"),
    promotion_themes=("Carton bundle offer", "Film volume discount",
                      "Label clearance", "Wrap and strap deal",
                      "Tape multipack offer", "Void fill trial price",
                      "New account welcome", "Year-end stock-up"),
    target=_northwind_target,
    order_goal=(800_000, 8_000_000),
    tickets_per_month=(6, 14),
    purchase_orders_per_month=(3, 6),
    purchase_order_value=(4_000, 60_000),
    fixed_orders=Q4_2025_ORDERS,
    best_margin_month=BEST_MARGIN_MONTH,
    week_12=True,
)

HARBOR = Profile(
    key="harbor", database="harbor_dw", seed=HARBOR_SEED,
    regions=("Harbor Coast", "Harbor Valley", "Harbor Uplands",
             "Harbor Islands"),
    region_weights=(4, 3, 2, 2),
    categories=("Rope and line", "Fenders", "Paints and coatings", "Fittings",
                "Safety", "Cleaning"),
    suppliers=(("Brightwater Cordage", "United Kingdom"),
               ("Nordkyst Marine", "Norway"),
               ("Tasman Coatings", "New Zealand"),
               ("Ostrava Fittings", "Czechia"),
               ("Cape Rescue Gear", "South Africa")),
    products=(
        ("RP-100", "Mooring rope, 12 mm",          "18.40", 1, 1),
        ("RP-140", "Mooring rope, 16 mm",          "27.90", 1, 1),
        ("FD-210", "Boat fender, medium",          "22.50", 2, 2),
        ("FD-260", "Boat fender, large",           "39.00", 2, 2),
        ("AP-300", "Antifouling paint, 2.5 litre", "64.00", 3, 3),
        ("AP-320", "Deck varnish, 1 litre",        "21.75", 3, 3),
        ("FS-410", "Stainless shackle, 8 mm",       "3.60", 4, 4),
        ("FS-450", "Deck cleat, 150 mm",           "11.20", 4, 4),
        ("SF-500", "Life jacket, adult",           "34.80", 5, 5),
        ("SF-520", "Flare kit, coastal",           "48.50", 5, 5),
        ("CL-610", "Bilge cleaner, 5 litre",       "15.30", 6, 3),
        ("CL-650", "Hull wax, 1 kg",               "19.90", 6, 3),
    ),
    customers=(),
    more_customers=(
        "Harbor Marine Stores", "Harbor Fresh Market", "Harbor Boatworks",
        "Harbor Chandlery", "Harbor Fisheries", "Harbor Cold Storage",
        "Harbor Building Supplies", "Harbor Kitchen Co", "Harbor Print House",
        "Harbor Ferry Services", "Harbor Garden Centre", "Harbor Auto Parts",
        "Harbor Medical Supply", "Harbor Craft Brewery",
        "Harbor Office Direct", "Harbor Pet Care", "Harbor Textiles",
        "Harbor Yacht Club", "Harbor Timber", "Harbor Bakehouse",
        "Harbor Electrical", "Harbor Sailmakers",
    ),
    employees=(
        ("Marta Lindgren", "Regional sales lead", 1, date(2018, 3, 5)),
        ("Kofi Mensah", "Regional sales lead", 2, date(2019, 7, 22)),
        ("Elena Rossi", "Regional sales lead", 3, date(2020, 2, 10)),
        ("Jack Moreau", "Regional sales lead", 4, date(2021, 6, 14)),
        ("Clara Novak", "Senior account manager", 1, date(2020, 11, 30)),
        ("Ines Duarte", "Account manager", 1, date(2022, 9, 12)),
        ("Yusuf Demir", "Account manager", 2, date(2023, 5, 8)),
        ("Ravi Pillai", "Account manager", 2, date(2025, 7, 7)),
        ("Ben Whitaker", "Account manager", 3, date(2025, 3, 3)),
        ("Lucia Ferreira", "Account manager", 4, date(2024, 1, 22)),
    ),
    warehouses=(("Coast depot", "Saltmarsh"), ("Valley depot", "Millbrook"),
                ("Uplands depot", "Highcross"), ("Islands depot", "Gullrock")),
    carriers=("Tidewater Freight", "Keel Express", "Bayline Couriers"),
    promotion_themes=("Spring fit-out sale", "Rope and line week",
                      "Safety gear check", "Antifouling season",
                      "Winter lay-up offer", "Trade account welcome",
                      "Fender bundle deal", "Clean hull offer"),
    target=_harbor_target,
    order_goal=(300_000, 3_000_000),
    tickets_per_month=(3, 9),
    purchase_orders_per_month=(2, 5),
    purchase_order_value=(2_000, 25_000),
    first_shipment_id=30001,
    gaps=3,
)

PROFILES: dict[str, Profile] = {NORTHWIND.key: NORTHWIND, HARBOR.key: HARBOR}


# =============================================================== generate ====

def _money(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


def _price(cost: int, markup: int) -> int:
    """A unit price in cents: cost times a markup in basis points, rounded."""
    return (cost * markup + 5_000) // 10_000


def _months(first: date, last: date) -> Iterator[tuple[int, int]]:
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _quarters(first: date, last: date) -> Iterator[tuple[int, int]]:
    seen = []
    for year, month in _months(first, last):
        quarter = (year, (month - 1) // 3 + 1)
        if quarter not in seen:
            seen.append(quarter)
            yield quarter


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


@dataclass
class _Order:
    key: tuple                          # (order_date, place in its month)
    customer_id: int
    order_date: date
    employee_id: int
    items: list[tuple[int, int, int]]   # product_id, qty, unit price in cents
    shipment: dict = field(default_factory=dict)
    returned: dict | None = None
    invoice: dict = field(default_factory=dict)
    payments: list[dict] = field(default_factory=list)
    order_id: int = 0

    @property
    def amount(self) -> int:
        return sum(qty * price for _, qty, price in self.items)

    @property
    def units(self) -> int:
        return sum(qty for _, qty, _ in self.items)


class _World:
    """One company: the rows that do not move with time, and the streams that
    generate the ones that do -- one random stream per month and per kind of
    row, so no month's data depends on how many days came after it."""

    def __init__(self, profile: Profile) -> None:
        self.p = profile
        rng = random.Random(f"{profile.seed}:world")

        self.regions = [{"region_id": i, "region_name": name}
                        for i, name in enumerate(profile.regions, 1)]
        self.categories = [{"category_id": i, "name": name}
                           for i, name in enumerate(profile.categories, 1)]
        self.suppliers = [{"supplier_id": i, "name": name, "country": country}
                          for i, (name, country) in enumerate(profile.suppliers, 1)]
        self.products = [
            {"product_id": i, "sku": sku, "name": name,
             "unit_cost": Decimal(cost), "category_id": category,
             "supplier_id": supplier}
            for i, (sku, name, cost, category, supplier)
            in enumerate(profile.products, 1)
        ]
        self.cost = {p["product_id"]: int(p["unit_cost"] * 100)
                     for p in self.products}

        customers = list(profile.customers)
        region_ids = list(range(1, len(profile.regions) + 1))
        span = (CUSTOMERS_UNTIL - CUSTOMERS_FROM).days
        for name in profile.more_customers:
            region_id = rng.choices(region_ids, weights=profile.region_weights)[0]
            created_at = CUSTOMERS_FROM + timedelta(days=rng.randint(0, span))
            customers.append((name, region_id, created_at))
        self.customers = [
            {"customer_id": i, "name": name, "region_id": region_id,
             "created_at": created_at}
            for i, (name, region_id, created_at) in enumerate(customers, 1)
        ]
        self.customer = {c["customer_id"]: c for c in self.customers}
        self.customer_weight = {c["customer_id"]: rng.randint(1, 5)
                                for c in self.customers}

        self.employees = [
            {"employee_id": i, "name": name, "title": title,
             "region_id": region_id, "hired_on": hired_on}
            for i, (name, title, region_id, hired_on)
            in enumerate(profile.employees, 1)
        ]
        self.warehouses = [
            {"warehouse_id": i, "name": name, "city": city, "region_id": i}
            for i, (name, city) in enumerate(profile.warehouses, 1)
        ]
        self.warehouse_of_region = {w["region_id"]: w["warehouse_id"]
                                    for w in self.warehouses}
        self.stock_level = {(w["warehouse_id"], p["product_id"]): rng.randint(80, 900)
                            for w in self.warehouses for p in self.products}

    # ------------------------------------------------------------ orders ----

    def band(self, year: int, month: int) -> tuple[int, int]:
        if (year, month) == self.p.best_margin_month:
            return BEST_MARGIN_MARKUP
        return MARKUP

    def plan(self, year: int, month: int) -> int:
        """A month's planned revenue in cents: its fixed orders, if it has any."""
        fixed = [_cents(a) for _, d, a in self.p.fixed_orders
                 if (d.year, d.month) == (year, month)]
        return sum(fixed) if fixed else self.p.target(year, month)

    def orders_of_month(self, year: int, month: int) -> list[_Order]:
        rng = random.Random(f"{self.p.seed}:{year}-{month:02d}:orders")
        band = self.band(year, month)

        # A month with fixed orders holds those and nothing else (Q4 2025).
        fixed = sorted((o for o in self.p.fixed_orders
                        if (o[1].year, o[1].month) == (year, month)),
                       key=lambda o: o[1])
        if fixed:
            return [
                self.order(rng, (day, seq), day,
                           self.items_for_amount(rng, _cents(amount), band),
                           customer_id=customer_id)
                for seq, (customer_id, day, amount) in enumerate(fixed)
            ]

        orders: list[_Order] = []
        if self.p.week_12 and (year, month) == (2025, 3):
            orders += self.week_12_orders(rng, band)

        target = self.p.target(year, month)
        total = sum(o.amount for o in orders)
        low, high = self.p.order_goal
        last = calendar.monthrange(year, month)[1]
        while total < target:
            remaining = target - total
            goal = rng.randint(low, high)
            if goal > remaining:           # the month's last order fills it
                goal = max(remaining, low // 4)
            # The span starts on 1 January 2025 with an order on that day.
            day = 1 if (year, month) == (2025, 1) and not orders \
                else rng.randint(1, last)
            on = date(year, month, day)
            order = self.order(rng, (on, len(orders)), on,
                               self.items_for_goal(rng, goal, band))
            orders.append(order)
            total += order.amount
        return orders

    def week_12_orders(self, rng: random.Random, band) -> list[_Order]:
        """The nine orders behind week 12's shipments, placed 3-14 March 2025.

        Each order's quantities add up to its shipment's units, so a unit
        count has a source. The three shipments without units have orders too:
        84120's was for 203 units, the value the demo's correction fills in.
        """
        days = sorted(rng.randint(3, 14) for _ in WEEK_12_SHIPMENTS)
        orders = []
        for seq, (day, (shipment_id, shipped_on, carrier, units)) in enumerate(
                zip(days, WEEK_12_SHIPMENTS)):
            ordered = units
            if ordered is None:
                ordered = FILLED_UNITS if shipment_id == FILLED_SHIPMENT \
                    else rng.randint(150, 230)
            on = date(2025, 3, day)
            shipment = {"shipment_id": shipment_id, "shipped_on": shipped_on,
                        "carrier": carrier, "units": units}
            orders.append(self.order(rng, (on, seq), on,
                                     self.items_for_units(rng, ordered, band),
                                     shipment=shipment))
        return orders

    def closing_order(self, day: date) -> _Order:
        """An order on the span's last day, when the month drew none there."""
        rng = random.Random(f"{self.p.seed}:closing:{day.isoformat()}")
        low = self.p.order_goal[0]
        items = self.items_for_goal(rng, rng.randint(low, 2 * low),
                                    self.band(day.year, day.month))
        return self.order(rng, (day, CLOSING), day, items)

    def order(self, rng, key, on: date, items, *, customer_id: int | None = None,
              shipment: dict | None = None) -> _Order:
        customer = (self.customer[customer_id] if customer_id
                    else self.pick_customer(rng, on))
        staff = [e["employee_id"] for e in self.employees
                 if e["region_id"] == customer["region_id"] and e["hired_on"] <= on]
        order = _Order(key=key, customer_id=customer["customer_id"],
                       order_date=on, employee_id=rng.choice(staff), items=items)
        self.follow(rng, order, customer["region_id"], shipment)
        return order

    def follow(self, rng, order: _Order, region_id: int,
               shipment: dict | None) -> None:
        """What happens to an order: shipped, sometimes returned, invoiced,
        paid. Drawn in full whatever the run date; the run date only cuts."""
        lead = rng.choice(LEAD_DAYS)
        carrier = rng.choice(self.p.carriers)
        if shipment is None:
            shipped_on = order.order_date + timedelta(days=lead)
            if self.p.week_12 and QUIET_DAYS[0] <= shipped_on <= QUIET_DAYS[1]:
                shipped_on = QUIET_DAYS[1] + timedelta(days=1)
            shipment = {"shipment_id": None, "shipped_on": shipped_on,
                        "carrier": carrier, "units": order.units}
        shipment["warehouse_id"] = self.warehouse_of_region[region_id]
        order.shipment = shipment

        if rng.random() < RETURN_RATE:
            product_id, qty, _ = rng.choice(order.items)
            order.returned = {
                "product_id": product_id,
                "qty": rng.randint(1, max(1, qty // 8)),
                "returned_on": shipment["shipped_on"]
                + timedelta(days=rng.randint(3, 25)),
                "reason": rng.choice(RETURN_REASONS),
            }

        issued_on = order.order_date + timedelta(days=rng.choice((0, 0, 1, 2)))
        order.invoice = {"issued_on": issued_on,
                         "due_on": issued_on + timedelta(days=PAYMENT_TERMS)}
        plan = rng.random()
        if plan < UNPAID_RATE:
            parts: list[tuple[int, int]] = []
        elif plan < UNPAID_RATE + INSTALMENT_RATE:
            first = order.amount * rng.randint(40, 70) // 100
            parts = [(rng.randint(5, 25), first),
                     (rng.randint(26, 60), order.amount - first)]
        else:
            parts = [(rng.randint(5, 45), order.amount)]
        order.payments = [
            {"paid_on": issued_on + timedelta(days=days), "amount": cents,
             "method": rng.choice(PAYMENT_METHODS)}
            for days, cents in parts
        ]

    def pick_customer(self, rng, on: date) -> dict:
        eligible = [c for c in self.customers if c["created_at"] <= on]
        weights = [self.customer_weight[c["customer_id"]] for c in eligible]
        return rng.choices(eligible, weights=weights)[0]

    # ------------------------------------------------------------- items ----

    def items_for_goal(self, rng, goal: int, band) -> list[tuple[int, int, int]]:
        """Items worth roughly `goal` cents."""
        chosen = rng.sample(self.products, rng.choice(ITEM_COUNTS))
        weights = [rng.randint(1, 4) for _ in chosen]
        items = []
        for product, weight in zip(chosen, weights):
            price = _price(self.cost[product["product_id"]], rng.randint(*band))
            share = goal * weight // sum(weights)
            qty = min(QTY_MAX, max(QTY_MIN, (share + price // 2) // price))
            items.append((product["product_id"], qty, price))
        return items

    def items_for_units(self, rng, units: int, band) -> list[tuple[int, int, int]]:
        """Items whose quantities add up to `units`."""
        chosen = rng.sample(self.products, rng.choice((1, 2, 2, 3)))
        cuts = sorted(rng.sample(range(1, units), len(chosen) - 1))
        quantities = [b - a for a, b in zip([0, *cuts], [*cuts, units])]
        return [(p["product_id"], qty,
                 _price(self.cost[p["product_id"]], rng.randint(*band)))
                for p, qty in zip(chosen, quantities)]

    def items_for_amount(self, rng, amount: int, band) -> list[tuple[int, int, int]]:
        """Items whose qty x unit_price add up to `amount` cents exactly.

        The last item's quantity is chosen to divide what is left to the cent,
        at a price inside the same markup band as every other item, so a fixed
        order is priced like any other.
        """
        low, high = band
        for _ in range(1_000):
            chosen = rng.sample(self.products, rng.choice((2, 3, 3, 4)))
            *first, last = chosen
            weights = [rng.randint(1, 4) for _ in chosen]
            items, spent = [], 0
            for product, weight in zip(first, weights):
                price = _price(self.cost[product["product_id"]], rng.randint(low, high))
                qty = min(QTY_MAX, max(QTY_MIN, amount * weight // sum(weights) // price))
                items.append((product["product_id"], qty, price))
                spent += qty * price
            rest = amount - spent
            if rest <= 0:
                continue
            cost = self.cost[last["product_id"]]
            cheapest, dearest = -(-cost * low // 10_000), cost * high // 10_000
            fits = [qty for qty in range(QTY_MIN, LAST_QTY_MAX + 1)
                    if rest % qty == 0 and cheapest <= rest // qty <= dearest]
            if fits:
                qty = rng.choice(fits)
                return [*items, (last["product_id"], qty, rest // qty)]
        raise RuntimeError(f"no items add up to {_money(amount)}")

    # ------------------------------------------------------- other rows ----

    def leave_gaps(self, orders: list[_Order]) -> None:
        """Harbor's own missing unit counts, for the correction tests."""
        if not self.p.gaps:
            return
        rng = random.Random(f"{self.p.seed}:gaps")
        early = sorted((o for o in orders if o.shipment["shipped_on"] < GAPS_BEFORE),
                       key=lambda o: o.key)
        for order in rng.sample(early, self.p.gaps):
            order.shipment["units"] = None

    def tickets_of_month(self, year: int, month: int) -> list[dict]:
        rng = random.Random(f"{self.p.seed}:{year}-{month:02d}:tickets")
        last = calendar.monthrange(year, month)[1]
        tickets = []
        for seq in range(rng.randint(*self.p.tickets_per_month)):
            opened = date(year, month, rng.randint(1, last))
            tickets.append({
                "key": (opened, seq),
                "customer_id": self.pick_customer(rng, opened)["customer_id"],
                "opened_on": opened,
                "closed_on": opened + timedelta(days=rng.choice(RESOLUTION_DAYS)),
                "category": rng.choice(TICKET_CATEGORIES),
                "priority": rng.choices(PRIORITIES, weights=PRIORITY_WEIGHTS)[0],
            })
        return tickets

    def purchase_orders_of_month(self, year: int, month: int) -> list[dict]:
        rng = random.Random(f"{self.p.seed}:{year}-{month:02d}:purchase-orders")
        last = calendar.monthrange(year, month)[1]
        orders = []
        for seq in range(rng.randint(*self.p.purchase_orders_per_month)):
            ordered = date(year, month, rng.randint(1, last))
            orders.append({
                "key": (ordered, seq),
                "supplier_id": rng.randint(1, len(self.suppliers)),
                "ordered_on": ordered,
                "amount": rng.randint(*self.p.purchase_order_value) * 100,
                "arrives": ordered + timedelta(days=rng.randint(7, 21)),
            })
        return orders

    def counts_of_month(self, year: int, month: int) -> list[dict]:
        """A stock count of every product in every warehouse, at month end."""
        rng = random.Random(f"{self.p.seed}:{year}-{month:02d}:inventory")
        counted = _month_end(year, month)
        return [
            {"warehouse_id": w["warehouse_id"], "product_id": p["product_id"],
             "on_hand": level * rng.randint(35, 165) // 100,
             "reorder_level": level * 4 // 100 * 10, "counted_on": counted}
            for w in self.warehouses for p in self.products
            for level in (self.stock_level[(w["warehouse_id"], p["product_id"])],)
        ]

    def promotions_of_quarter(self, year: int, quarter: int) -> list[dict]:
        rng = random.Random(f"{self.p.seed}:{year}-Q{quarter}:promotions")
        start = date(year, 3 * quarter - 2, 1)
        promotions = []
        for seq, theme in enumerate(rng.sample(self.p.promotion_themes, 2)):
            starts = start + timedelta(days=rng.randint(0, 70))
            promotions.append({
                "key": (starts, seq), "name": f"{theme}, Q{quarter} {year}",
                "starts_on": starts,
                "ends_on": starts + timedelta(days=rng.randint(7, 28)),
                "discount_pct": Decimal(rng.choice(DISCOUNTS)),
            })
        return promotions

    def targets_of_quarter(self, year: int, quarter: int) -> list[dict]:
        rng = random.Random(f"{self.p.seed}:{year}-Q{quarter}:targets")
        plan = sum(self.plan(year, m) for m in range(3 * quarter - 2, 3 * quarter + 1))
        weights = self.p.region_weights
        targets = []
        for region_id, weight in enumerate(weights, 1):
            cents = plan * weight // sum(weights) * rng.randint(92, 110) // 100
            targets.append({"region_id": region_id, "year": year,
                            "quarter": quarter,
                            "target_amount": _money((cents + 50_000) // 100_000 * 100_000)})
        return targets


def _cents(amount: str) -> int:
    return int(Decimal(amount) * 100)


def _numbered(rows: list[dict], key: str) -> list[dict]:
    for number, row in enumerate(rows, 1):
        row[key] = number
    return rows


def generate(company: str, run_date: date) -> Dataset:
    """Every row of one company's warehouse, as of `run_date`.

    Pure: no database, no clock. The same company and run date always give
    the same rows, in primary-key order.
    """
    if run_date < EARLIEST_RUN_DATE:
        raise ValueError(
            f"the run date must be {EARLIEST_RUN_DATE} or later: the span ends "
            "the day before it, and that day must fall after Q4 2025")
    world = _World(PROFILES[company])
    last_day = run_date - timedelta(days=1)
    months = list(_months(FIRST_DAY, last_day))
    quarters = list(_quarters(FIRST_DAY, last_day))

    orders = [o for year, month in months for o in world.orders_of_month(year, month)
              if o.order_date <= last_day]
    if all(o.order_date != last_day for o in orders):
        orders.append(world.closing_order(last_day))
    world.leave_gaps(orders)
    orders.sort(key=lambda o: o.key)
    for order_id, order in enumerate(orders, 1):
        order.order_id = order_id

    data: Dataset = {
        "regions": world.regions,
        "categories": world.categories,
        "suppliers": world.suppliers,
        "customers": world.customers,
        "employees": world.employees,
        "products": world.products,
        "warehouses": world.warehouses,
    }

    promotions = sorted((p for y, q in quarters for p in world.promotions_of_quarter(y, q)
                         if p["starts_on"] <= last_day), key=lambda p: p["key"])
    data["promotions"] = _numbered(
        [{k: p[k] for k in TABLES["promotions"][1:]} for p in promotions],
        "promotion_id")
    data["sales_targets"] = _numbered(
        [t for y, q in quarters for t in world.targets_of_quarter(y, q)], "target_id")

    data["orders"] = [
        {"order_id": o.order_id, "customer_id": o.customer_id,
         "order_date": o.order_date, "amount": _money(o.amount),
         "employee_id": o.employee_id}
        for o in orders
    ]
    data["order_items"] = _numbered(
        [{"order_id": o.order_id, "product_id": product_id, "qty": qty,
          "unit_price": _money(price)}
         for o in orders for product_id, qty, price in o.items],
        "order_item_id")

    shipped = [o for o in orders if o.shipment["shipped_on"] <= last_day]
    _number_shipments(world.p, shipped)
    data["shipments"] = sorted(
        ({"shipment_id": o.shipment["shipment_id"], "order_id": o.order_id,
          "shipped_on": o.shipment["shipped_on"], "carrier": o.shipment["carrier"],
          "units": o.shipment["units"], "warehouse_id": o.shipment["warehouse_id"]}
         for o in shipped),
        key=lambda s: s["shipment_id"])

    returned = sorted((o for o in orders
                       if o.returned and o.returned["returned_on"] <= last_day),
                      key=lambda o: (o.returned["returned_on"], o.order_id))
    data["returns"] = _numbered(
        [{"order_id": o.order_id, **o.returned} for o in returned], "return_id")

    invoiced = sorted((o for o in orders if o.invoice["issued_on"] <= last_day),
                      key=lambda o: (o.invoice["issued_on"], o.order_id))
    invoices, paid = [], []
    for invoice_id, o in enumerate(invoiced, 1):
        made = [p for p in o.payments if p["paid_on"] <= last_day]
        if sum(p["amount"] for p in made) == o.amount:
            status = "paid"
        elif o.invoice["due_on"] <= last_day:
            status = "overdue"
        else:
            status = "open"
        invoices.append({"invoice_id": invoice_id, "order_id": o.order_id,
                         "issued_on": o.invoice["issued_on"],
                         "due_on": o.invoice["due_on"],
                         "amount": _money(o.amount), "status": status})
        paid += [(p["paid_on"], invoice_id, part, p) for part, p in enumerate(made)]
    data["invoices"] = invoices
    data["payments"] = _numbered(
        [{"invoice_id": invoice_id, "paid_on": p["paid_on"],
          "amount": _money(p["amount"]), "method": p["method"]}
         for _, invoice_id, _, p in sorted(paid, key=lambda t: t[:3])],
        "payment_id")

    data["inventory"] = _numbered(
        [c for y, m in months if _month_end(y, m) <= last_day
         for c in world.counts_of_month(y, m)], "inventory_id")

    purchases = sorted((p for y, m in months for p in world.purchase_orders_of_month(y, m)
                        if p["ordered_on"] <= last_day), key=lambda p: p["key"])
    data["purchase_orders"] = _numbered(
        [{"supplier_id": p["supplier_id"], "ordered_on": p["ordered_on"],
          "amount": _money(p["amount"]),
          "status": "received" if p["arrives"] <= last_day else "ordered"}
         for p in purchases], "po_id")

    tickets = sorted((t for y, m in months for t in world.tickets_of_month(y, m)
                      if t["opened_on"] <= last_day), key=lambda t: t["key"])
    data["support_tickets"] = _numbered(
        [{"customer_id": t["customer_id"], "opened_on": t["opened_on"],
          "closed_on": t["closed_on"] if t["closed_on"] <= last_day else None,
          "category": t["category"], "priority": t["priority"]}
         for t in tickets], "ticket_id")

    # Every row in §11.4 column order, primary key first.
    return {table: [{c: row[c] for c in columns} for row in data[table]]
            for table, columns in TABLES.items()}


def _number_shipments(profile: Profile, shipped: list[_Order]) -> None:
    """Shipment ids run in date order. Northwind's go around the nine fixed
    ids of week 12: those before the quiet days end at 84111, those after
    start at 84124, and 84113, 84115 and 84118 never exist."""
    rest = sorted((o for o in shipped if o.shipment["shipment_id"] is None),
                  key=lambda o: (o.shipment["shipped_on"], o.order_id))
    if not profile.week_12:
        for number, order in enumerate(rest, profile.first_shipment_id):
            order.shipment["shipment_id"] = number
        return
    before = [o for o in rest if o.shipment["shipped_on"] < QUIET_DAYS[0]]
    after = [o for o in rest if o.shipment["shipped_on"] > QUIET_DAYS[1]]
    if len(before) + len(after) != len(rest):
        raise RuntimeError("a shipment was generated inside 16-23 March 2025")
    first = WEEK_12_SHIPMENTS[0][0] - len(before)
    for number, order in enumerate(before, first):
        order.shipment["shipment_id"] = number
    for number, order in enumerate(after, WEEK_12_SHIPMENTS[-1][0] + 1):
        order.shipment["shipment_id"] = number


# ============================================================= guarantees ====

class GuaranteeFailed(Exception):
    """A §11.4 guarantee that does not hold. Bootstrap stops on the first."""

    def __init__(self, database: str, guarantee: str, detail: str) -> None:
        super().__init__(f"seed guarantee failed on {database} [{guarantee}]: {detail}")
        self.database, self.guarantee, self.detail = database, guarantee, detail


class _Broken(Exception):
    pass


def _fail(detail: str) -> None:
    raise _Broken(detail)


@dataclass
class Snapshot:
    """One warehouse's rows, and what is needed to judge them."""

    database: str
    company: str
    run_date: date
    data: Dataset
    catalog: dict | None = None      # None for generated data, never loaded


@dataclass(frozen=True)
class _Rule:
    name: str
    check: Callable[[Snapshot], None]
    northwind_only: bool
    needs_catalog: bool


_RULES: list[_Rule] = []


def _guarantee(name: str, *, northwind_only: bool = False,
               needs_catalog: bool = False):
    def register(check: Callable[[Snapshot], None]):
        _RULES.append(_Rule(name, check, northwind_only, needs_catalog))
        return check
    return register


def guarantees_for(company: str) -> list[str]:
    """The guarantee names that apply to a company, in the order checked."""
    return [r.name for r in _RULES if not r.northwind_only or company == NORTHWIND.key]


def check(snapshot: Snapshot, only: str | None = None) -> list[str]:
    """Check every guarantee for this warehouse, in order. Raises
    GuaranteeFailed at the first that does not hold; returns the names that
    were checked. The schema guarantee needs the database's catalogue and is
    skipped for generated data."""
    passed = []
    for rule in _RULES:
        if rule.northwind_only and snapshot.company != NORTHWIND.key:
            continue
        if only is not None and rule.name != only:
            continue
        if rule.needs_catalog and snapshot.catalog is None:
            continue
        try:
            rule.check(snapshot)
        except _Broken as broken:
            raise GuaranteeFailed(snapshot.database, rule.name, str(broken)) from None
        passed.append(rule.name)
    return passed


def generated(company: str, run_date: date) -> Snapshot:
    """What `seed` would load for this run date, ready to check."""
    return Snapshot(PROFILES[company].database, company, run_date,
                    generate(company, run_date))


def _label(month: tuple[int, int]) -> str:
    return f"{calendar.month_name[month[1]]} {month[0]}"


@_guarantee("schema", needs_catalog=True)
def _schema(s: Snapshot) -> None:
    columns = s.catalog["columns"]
    missing = [t for t in TABLES if t not in columns]
    if missing:
        _fail(f"missing tables: {', '.join(missing)}")
    for table, expected in TABLES.items():
        if sorted(columns[table]) != sorted(expected):
            _fail(f"{table} has columns {list(columns[table])}; "
                  f"§11.4 lists {list(expected)}")
        keys = s.catalog["primary_keys"].get(table, [])
        if keys != [[expected[0]]]:
            _fail(f"{table} needs a single-column primary key on {expected[0]}; "
                  f"it has {keys or 'none'}")
    found = Counter(fk for fk in s.catalog["foreign_keys"] if fk[0] in TABLES)
    wanted = Counter(FOREIGN_KEYS)
    if found != wanted:
        _fail(f"foreign keys missing: {sorted(wanted - found) or 'none'}; "
              f"unexpected or repeated: {sorted(found - wanted) or 'none'}")
    for table, column in ADDED_COLUMNS:
        if not columns[table][column]:
            _fail(f"{table}.{column} must be nullable")


@_guarantee("keys")
def _keys(s: Snapshot) -> None:
    ids: dict[str, set] = {}
    for table, columns in TABLES.items():
        values = [row[columns[0]] for row in s.data[table]]
        if not values:
            _fail(f"{table} is empty")
        if None in values or len(set(values)) != len(values):
            _fail(f"{table}.{columns[0]} is not a unique key")
        ids[table] = set(values)
    for table, column, target, _ in FOREIGN_KEYS:
        for row in s.data[table]:
            if row[column] is not None and row[column] not in ids[target]:
                _fail(f"{table} {row[TABLES[table][0]]}: {column} "
                      f"{row[column]} is not in {target}")


@_guarantee("span")
def _span(s: Snapshot) -> None:
    dates = [o["order_date"] for o in s.data["orders"]]
    last = s.run_date - timedelta(days=1)
    if min(dates) != FIRST_DAY or max(dates) != last:
        _fail(f"orders run from {min(dates)} to {max(dates)}; they must run "
              f"from {FIRST_DAY} to {last}, the day before the run date")


@_guarantee("amounts add up")
def _amounts(s: Snapshot) -> None:
    totals: dict[int, Decimal] = defaultdict(Decimal)
    for item in s.data["order_items"]:
        totals[item["order_id"]] += item["qty"] * item["unit_price"]
    for order in s.data["orders"]:
        if totals.get(order["order_id"]) != order["amount"]:
            _fail(f"order {order['order_id']} is for {order['amount']}, but its "
                  f"items add up to {totals.get(order['order_id'], 0)}")


@_guarantee("dates in order")
def _dates(s: Snapshot) -> None:
    placed = {o["order_id"]: o["order_date"] for o in s.data["orders"]}
    for table, column in (("shipments", "shipped_on"), ("returns", "returned_on"),
                          ("invoices", "issued_on")):
        for row in s.data[table]:
            if row["order_id"] is not None and row[column] < placed[row["order_id"]]:
                _fail(f"{table} {row[TABLES[table][0]]} is dated {row[column]}, "
                      f"before its order {row['order_id']} was placed")
    order_of = {i["invoice_id"]: i["order_id"] for i in s.data["invoices"]}
    for payment in s.data["payments"]:
        if payment["paid_on"] < placed[order_of[payment["invoice_id"]]]:
            _fail(f"payment {payment['payment_id']} is dated {payment['paid_on']}, "
                  "before its order was placed")


@_guarantee("no churn")
def _no_churn(s: Snapshot) -> None:
    for table, columns in TABLES.items():
        for name in (table, *columns):
            if any(word in name for word in CHURN_WORDS):
                _fail(f"{name} names churn or a cancellation")
    if "status" in TABLES["customers"]:
        _fail("customers records a status")
    for table, rows in s.data.items():
        for row in rows:
            for value in row.values():
                if isinstance(value, str) and any(w in value.lower() for w in CHURN_WORDS):
                    _fail(f"{table} records {value!r}")


@_guarantee("names")
def _names(s: Snapshot) -> None:
    if s.company == HARBOR.key:
        for table, column in (("regions", "region_name"), ("customers", "name")):
            for row in s.data[table]:
                if not row[column].startswith("Harbor"):
                    _fail(f"Harbor's {table} include {row[column]!r}; every "
                          "name must start with Harbor")
        return
    for table, rows in s.data.items():
        for row in rows:
            for value in row.values():
                if isinstance(value, str) and "harbor" in value.lower():
                    _fail(f"{table} holds {value!r}; no Northwind name may "
                          "contain Harbor")


@_guarantee("missing units")
def _missing_units(s: Snapshot) -> None:
    missing = sorted(r["shipment_id"] for r in s.data["shipments"] if r["units"] is None)
    if s.company == NORTHWIND.key:
        wanted = sorted(i for i, _, _, units in WEEK_12_SHIPMENTS if units is None)
        if missing != wanted:
            _fail(f"shipments {missing} have no units; exactly {wanted} "
                  "must be the ones without")
    elif len(missing) != HARBOR.gaps:
        _fail(f"{len(missing)} shipments have no units; Harbor keeps exactly "
              f"{HARBOR.gaps} for the correction tests")


@_guarantee("seeded rows kept", northwind_only=True)
def _seeded_rows(s: Snapshot) -> None:
    regions = {r["region_id"]: r["region_name"] for r in s.data["regions"]}
    for region_id, name in enumerate(NORTHWIND_REGIONS, 1):
        if regions.get(region_id) != name:
            _fail(f"region {region_id} is {regions.get(region_id)!r}, not {name!r}")
    customers = {c["customer_id"]: c for c in s.data["customers"]}
    for customer_id, (name, region_id, _) in enumerate(NORTHWIND_CUSTOMERS, 1):
        row = customers.get(customer_id)
        if row is None or (row["name"], row["region_id"]) != (name, region_id):
            _fail(f"customer {customer_id} is not {name!r} in region {region_id}")
    products = {p["product_id"]: p for p in s.data["products"]}
    for product_id, (sku, name, *_) in enumerate(NORTHWIND_PRODUCTS[:6], 1):
        row = products.get(product_id)
        if row is None or (row["sku"], row["name"]) != (sku, name):
            _fail(f"product {product_id} is not {sku} {name!r}")


@_guarantee("Q4 2025", northwind_only=True)
def _q4_2025(s: Snapshot) -> None:
    found = Counter((o["customer_id"], o["order_date"], o["amount"])
                    for o in s.data["orders"] if Q4_2025[0] <= o["order_date"] <= Q4_2025[1])
    wanted = Counter((c, d, Decimal(a)) for c, d, a in Q4_2025_ORDERS)
    if found != wanted:
        _fail(f"Q4 2025 holds {sum(found.values())} orders; it must hold the 21 "
              f"seeded ones unchanged and nothing else (missing "
              f"{sorted(wanted - found)}, extra {sorted(found - wanted)})")
    region_of = {c["customer_id"]: c["region_id"] for c in s.data["customers"]}
    names = {r["region_id"]: r["region_name"] for r in s.data["regions"]}
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for (customer_id, _, amount), count in found.items():
        totals[names[region_of[customer_id]]] += amount * count
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    if ranked[:2] != [("West", WEST_Q4_2025), ("North", NORTH_Q4_2025)]:
        _fail(f"Q4 2025 by region is {ranked[:2]}; West must lead with "
              f"{WEST_Q4_2025} and North follow with {NORTH_Q4_2025}")


def _strict_best(values: dict, wanted: tuple[int, int], reading: str) -> None:
    ranked = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    if best[0] != wanted or runner_up[1] == best[1]:
        _fail(f"by {reading}, {_label(best[0])} is the best month "
              f"({float(best[1]):,.4f}); it must be {_label(wanted)}, alone")


@_guarantee("best months", northwind_only=True)
def _best_months(s: Snapshot) -> None:
    month_of = {o["order_id"]: (o["order_date"].year, o["order_date"].month)
                for o in s.data["orders"]}
    revenue: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for order in s.data["orders"]:
        revenue[month_of[order["order_id"]]] += order["amount"]
    unit_cost = {p["product_id"]: p["unit_cost"] for p in s.data["products"]}
    sales: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    cost: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for item in s.data["order_items"]:
        month = month_of[item["order_id"]]
        sales[month] += item["qty"] * item["unit_price"]
        cost[month] += item["qty"] * unit_cost[item["product_id"]]

    months = list(_months(FIRST_DAY, s.run_date - timedelta(days=1)))
    empty = [_label(m) for m in months if not revenue.get(m)]
    if empty:
        _fail(f"no revenue in {', '.join(empty)}, so growth into the month "
              "after is undefined")

    _strict_best(dict(revenue), BEST_REVENUE_MONTH, "revenue")
    pairs = list(zip(months, months[1:]))
    _strict_best({m: revenue[m] - revenue[p] for p, m in pairs},
                 BEST_GROWTH_MONTH, "growth over the month before")
    _strict_best({m: Fraction(revenue[m]) / Fraction(revenue[p]) for p, m in pairs},
                 BEST_GROWTH_MONTH, "growth over the month before, as a ratio")
    _strict_best({m: Fraction(sales[m] - cost[m]) / Fraction(sales[m]) for m in months},
                 BEST_MARGIN_MONTH, "margin")


@_guarantee("week 12", northwind_only=True)
def _week_12(s: Snapshot) -> None:
    shipments = s.data["shipments"]
    in_week = {r["shipment_id"]: r for r in shipments
               if tuple(r["shipped_on"].isocalendar())[:2] == WEEK_12}
    wanted = [i for i, *_ in WEEK_12_SHIPMENTS]
    if sorted(in_week) != wanted:
        _fail(f"ISO week 12 of 2025 holds shipments {sorted(in_week)}; it must "
              f"hold exactly {wanted}")
    for shipment_id, shipped_on, carrier, units in WEEK_12_SHIPMENTS:
        row = in_week[shipment_id]
        if (row["shipped_on"], row["carrier"], row["units"]) != (shipped_on, carrier, units):
            _fail(f"shipment {shipment_id} is ({row['shipped_on']}, {row['carrier']}, "
                  f"{row['units']}); it must be ({shipped_on}, {carrier}, {units})")
    quiet = sorted(r["shipment_id"] for r in shipments
                   if QUIET_DAYS[0] <= r["shipped_on"] <= QUIET_DAYS[1])
    if quiet != wanted:
        _fail(f"shipments {sorted(set(quiet) - set(wanted))} ship between 16 and "
              "23 March 2025; only the nine of week 12 may")
    counted = sum(r["units"] for r in in_week.values() if r["units"] is not None)
    gaps = sum(1 for r in in_week.values() if r["units"] is None)
    if (counted, gaps) != (WEEK_12_UNITS, 3):
        _fail(f"week 12 counts {counted} units with {gaps} missing; it must count "
              f"{WEEK_12_UNITS} with 3 missing")
    if counted + FILLED_UNITS != FILLED_TOTAL:
        _fail(f"filling {FILLED_SHIPMENT} with {FILLED_UNITS} gives "
              f"{counted + FILLED_UNITS}, not {FILLED_TOTAL}")
    placed = {o["order_id"]: o["order_date"] for o in s.data["orders"]}
    for shipment_id, row in in_week.items():
        on = placed.get(row["order_id"])
        if on is None or not (date(2025, 3, 1) <= on < row["shipped_on"]):
            _fail(f"shipment {shipment_id} points at an order placed {on}; it must "
                  "point at an order placed earlier in March 2025")


# ============================================================== database ====

def connect(database: str, dsn: str | None = None):
    """psycopg, on one warehouse. Takes the owner's DSN by default; a test
    passes a runtime role's (an SQLAlchemy-style `postgresql+psycopg://` URL
    is accepted)."""
    import psycopg  # noqa: PLC0415
    from psycopg.conninfo import make_conninfo  # noqa: PLC0415

    dsn = dsn if dsn is not None else os.environ.get("SPEAKQL_OWNER_DSN", "")
    if not dsn:
        raise SystemExit("SPEAKQL_OWNER_DSN is required (bootstrap.sh passes it)")
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(make_conninfo(dsn, dbname=database))


def load(conn, database: str, company: str, run_date: date, data: Dataset) -> None:
    """Replace a warehouse's rows with `data`, in one transaction, and record
    the company and run date on the database."""
    from psycopg import sql  # noqa: PLC0415

    with conn.transaction():
        conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
            sql.SQL(", ").join(sql.Identifier("public", t) for t in TABLES)))
        for table, columns in TABLES.items():
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier("public", table),
                sql.SQL(", ").join(map(sql.Identifier, columns)))
            with conn.cursor() as cursor, cursor.copy(statement) as copy:
                for row in data[table]:
                    copy.write_row([row[c] for c in columns])
            # The ids were given explicitly; the sequence must carry on after them.
            conn.execute("SELECT setval(pg_get_serial_sequence(%s, %s), %s)",
                         (f"public.{table}", columns[0],
                          max(row[columns[0]] for row in data[table])))
        record = json.dumps({"company": company, "run_date": run_date.isoformat(),
                             "seed": PROFILES[company].seed})
        conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(database), sql.Literal(record)))


def read_catalog(conn) -> dict:
    """Tables, columns, primary and foreign keys of `public`, from pg_catalog
    -- readable by a SELECT-only role, unlike information_schema's
    constraint views."""
    columns: dict[str, dict[str, bool]] = defaultdict(dict)
    for table, column, nullable in conn.execute(
            "SELECT c.relname, a.attname, NOT a.attnotnull "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid "
            "     AND a.attnum > 0 AND NOT a.attisdropped "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "ORDER BY c.relname, a.attnum"):
        columns[table][column] = nullable
    primary_keys: dict[str, list[list[str]]] = defaultdict(list)
    for table, key in conn.execute(
            "SELECT c.relname, array_agg(a.attname ORDER BY a.attnum) "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (con.conkey) "
            "WHERE con.contype = 'p' AND n.nspname = 'public' "
            "GROUP BY c.relname, con.oid"):
        primary_keys[table].append(list(key))
    foreign_keys = [tuple(row) for row in conn.execute(
        "SELECT c.relname, a.attname, fc.relname, fa.attname "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = con.conkey[1] "
        "JOIN pg_class fc ON fc.oid = con.confrelid "
        "JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = con.confkey[1] "
        "WHERE con.contype = 'f' AND n.nspname = 'public' "
        "  AND cardinality(con.conkey) = 1")]
    return {"columns": dict(columns), "primary_keys": dict(primary_keys),
            "foreign_keys": foreign_keys}


def snapshot(conn, database: str) -> Snapshot:
    """A warehouse as it is: its seed record, its catalogue and every row."""
    (record,) = conn.execute(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
        "WHERE datname = current_database()").fetchone()
    try:
        seeded = json.loads(record)
        company, run_date = seeded["company"], date.fromisoformat(seeded["run_date"])
    except (TypeError, ValueError, KeyError):
        raise GuaranteeFailed(database, "seed record",
                              "no seed record on the database; run the seed first") from None
    catalog = read_catalog(conn)
    data: Dataset = {}
    for table, columns in TABLES.items():
        if not set(columns) <= set(catalog["columns"].get(table, {})):
            data[table] = []       # the schema guarantee names what is missing
            continue
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM public.{table} "  # noqa: S608 - fixed names
            f"ORDER BY {columns[0]}").fetchall()
        data[table] = [dict(zip(columns, row)) for row in rows]
    return Snapshot(database, company, run_date, data, catalog)


# ================================================================== main ====

def seed_all(run_date: date | None = None) -> None:
    """Both warehouses, with one run date: the database's day, read once."""
    with connect(NORTHWIND.database) as conn:
        run_date = run_date or conn.execute("SELECT CURRENT_DATE").fetchone()[0]
    for company, profile in PROFILES.items():
        data = generate(company, run_date)
        with connect(profile.database) as conn:
            load(conn, profile.database, company, run_date, data)
        rows = sum(len(r) for r in data.values())
        print(f"  {profile.database:<13} run date {run_date}: {rows:,} rows "
              f"({len(data['orders'])} orders, {len(data['order_items'])} items, "
              f"{len(data['shipments'])} shipments)")


def check_all() -> int:
    for company, profile in PROFILES.items():
        try:
            with connect(profile.database) as conn:
                found = snapshot(conn, profile.database)
            if found.company != company:
                raise GuaranteeFailed(profile.database, "seed record",
                                      f"seeded as {found.company!r}, not {company!r}")
            passed = check(found)
        except GuaranteeFailed as failure:
            print(str(failure), file=sys.stderr)
            return 1
        print(f"  {profile.database:<13} all {len(passed)} guarantees hold "
              f"(run date {found.run_date})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("seed", "check"))
    parser.add_argument("--run-date", type=date.fromisoformat, default=None,
                        help="seed as if bootstrap ran on this day "
                             "(default: the database's CURRENT_DATE)")
    args = parser.parse_args()
    if args.action == "seed":
        seed_all(args.run_date)
        return 0
    return check_all()


if __name__ == "__main__":
    sys.exit(main())
