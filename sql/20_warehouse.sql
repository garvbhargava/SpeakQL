-- SpeakQL · a customer warehouse (§11.4)
--
-- Run once per tenant database by scripts/bootstrap.sh, against northwind_dw
-- and again against harbor_dw. Two tenants, deliberately: isolation cannot be
-- tested against one.
--
-- The 18 tables of §11.4, column for column. The six the pipeline was first
-- built on keep their names and columns; twelve are added, and the four
-- columns added to the six are nullable, so every existing query still runs.
-- Every table has a single-column primary key, so every table is correctable.
-- The rows come from scripts/seed_warehouse.py, which bootstrap runs next.
--
-- Safe to run again on an existing volume: tables are created only if they
-- are absent, and the added columns and their foreign keys only if missing.

\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS public;
-- one overlay schema per member is created on demand by db/edits_engine.py;
-- this is just the parent it lives beside
CREATE SCHEMA IF NOT EXISTS member_edits;

-- ---------------------------------------------------------------- tables ----

CREATE TABLE IF NOT EXISTS regions (
    region_id    SERIAL PRIMARY KEY,
    region_name  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id  SERIAL PRIMARY KEY,
    name         TEXT    NOT NULL,
    region_id    INTEGER NOT NULL REFERENCES regions (region_id),
    created_at   DATE    NOT NULL DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS products (
    product_id   SERIAL PRIMARY KEY,
    sku          TEXT           NOT NULL UNIQUE,
    name         TEXT           NOT NULL,
    unit_cost    NUMERIC(12, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id     SERIAL PRIMARY KEY,
    customer_id  INTEGER        NOT NULL REFERENCES customers (customer_id),
    order_date   DATE           NOT NULL,
    amount       NUMERIC(12, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id      INTEGER        NOT NULL REFERENCES orders (order_id),
    product_id    INTEGER        NOT NULL REFERENCES products (product_id),
    qty           INTEGER        NOT NULL,
    unit_price    NUMERIC(12, 2) NOT NULL
);

-- units is deliberately nullable, and deliberately has gaps in the seed. Rows
-- with no value are EXCLUDED from a total rather than counted as zero, which
-- is what makes the "incomplete" answer mode real rather than staged.
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id  SERIAL PRIMARY KEY,
    order_id     INTEGER NULL REFERENCES orders (order_id),
    shipped_on   DATE    NOT NULL,
    carrier      TEXT    NOT NULL,
    units        INTEGER NULL
);

-- ------------------------------------------------- added in v6.1 (§11.4) ----

CREATE TABLE IF NOT EXISTS categories (
    category_id  SERIAL PRIMARY KEY,
    name         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id  SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    country      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    employee_id  SERIAL PRIMARY KEY,
    name         TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    region_id    INTEGER NOT NULL REFERENCES regions (region_id),
    hired_on     DATE    NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id SERIAL PRIMARY KEY,
    name         TEXT    NOT NULL,
    city         TEXT    NOT NULL,
    region_id    INTEGER NOT NULL REFERENCES regions (region_id)
);

CREATE TABLE IF NOT EXISTS sales_targets (
    target_id     SERIAL PRIMARY KEY,
    region_id     INTEGER        NOT NULL REFERENCES regions (region_id),
    year          INTEGER        NOT NULL,
    quarter       INTEGER        NOT NULL,
    target_amount NUMERIC(12, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS promotions (
    promotion_id SERIAL PRIMARY KEY,
    name         TEXT          NOT NULL,
    starts_on    DATE          NOT NULL,
    ends_on      DATE          NOT NULL,
    discount_pct NUMERIC(5, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    inventory_id  SERIAL PRIMARY KEY,
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses (warehouse_id),
    product_id    INTEGER NOT NULL REFERENCES products (product_id),
    on_hand       INTEGER NOT NULL,
    reorder_level INTEGER NOT NULL,
    counted_on    DATE    NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id        SERIAL PRIMARY KEY,
    supplier_id  INTEGER        NOT NULL REFERENCES suppliers (supplier_id),
    ordered_on   DATE           NOT NULL,
    amount       NUMERIC(12, 2) NOT NULL,
    status       TEXT           NOT NULL
);

CREATE TABLE IF NOT EXISTS returns (
    return_id    SERIAL PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES orders (order_id),
    product_id   INTEGER NOT NULL REFERENCES products (product_id),
    returned_on  DATE    NOT NULL,
    qty          INTEGER NOT NULL,
    reason       TEXT    NOT NULL
);

-- closed_on is empty while a ticket is open.
CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id    SERIAL PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers (customer_id),
    opened_on    DATE    NOT NULL,
    closed_on    DATE    NULL,
    category     TEXT    NOT NULL,
    priority     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   SERIAL PRIMARY KEY,
    order_id     INTEGER        NOT NULL REFERENCES orders (order_id),
    issued_on    DATE           NOT NULL,
    due_on       DATE           NOT NULL,
    amount       NUMERIC(12, 2) NOT NULL,
    status       TEXT           NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id   SERIAL PRIMARY KEY,
    invoice_id   INTEGER        NOT NULL REFERENCES invoices (invoice_id),
    paid_on      DATE           NOT NULL,
    amount       NUMERIC(12, 2) NOT NULL,
    method       TEXT           NOT NULL
);

-- The four columns added to the six original tables. Nullable, so every
-- existing query and row stays valid, and added here rather than in the
-- CREATE TABLEs above, so a fresh volume and one bootstrapped before v6.1
-- take the same path.
ALTER TABLE orders    ADD COLUMN IF NOT EXISTS employee_id  INTEGER NULL;
ALTER TABLE products  ADD COLUMN IF NOT EXISTS category_id  INTEGER NULL;
ALTER TABLE products  ADD COLUMN IF NOT EXISTS supplier_id  INTEGER NULL;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS warehouse_id INTEGER NULL;

-- Their foreign keys. PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS, and a
-- second run must not add a second copy, so each is added only when
-- pg_constraint shows no foreign key on that column yet.
DO $$
DECLARE
    fk RECORD;
BEGIN
    FOR fk IN
        SELECT * FROM (VALUES
            ('orders',    'employee_id',  'employees',  'employee_id'),
            ('products',  'category_id',  'categories', 'category_id'),
            ('products',  'supplier_id',  'suppliers',  'supplier_id'),
            ('shipments', 'warehouse_id', 'warehouses', 'warehouse_id')
        ) AS added (table_name, column_name, ref_table, ref_column)
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint con
            JOIN pg_attribute a
              ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey)
            WHERE con.contype = 'f'
              AND con.conrelid = format('public.%I', fk.table_name)::regclass
              AND a.attname = fk.column_name
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.%I ADD CONSTRAINT %I '
                'FOREIGN KEY (%I) REFERENCES public.%I (%I)',
                fk.table_name, fk.table_name || '_' || fk.column_name || '_fkey',
                fk.column_name, fk.ref_table, fk.ref_column);
        END IF;
    END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS orders_date_idx     ON orders (order_date);
CREATE INDEX IF NOT EXISTS shipments_date_idx  ON shipments (shipped_on);

-- ------------------------------------------------------------ privileges ----
-- This block is the point of the file. tests/test_privileges.py asserts every
-- line of it against the running database. Each grant names ALL TABLES, so it
-- reaches the tables added in v6.1 exactly as it reaches the first six; the
-- per-column grants of §9.2 replace this policy in a later step.

-- speakql_ro: read the warehouse, write nothing, anywhere.
GRANT CONNECT ON DATABASE :"dbname" TO speakql_ro;
GRANT USAGE   ON SCHEMA public TO speakql_ro;
GRANT SELECT  ON ALL TABLES IN SCHEMA public TO speakql_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO speakql_ro;

-- speakql_write: the edit path, and nothing else. UPDATE and INSERT only, and
-- only on tables the owner marked editable -- which the edit validator
-- enforces per statement. The grant is the floor, not the rule.
GRANT CONNECT ON DATABASE :"dbname" TO speakql_write;
GRANT USAGE   ON SCHEMA public TO speakql_write;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO speakql_write;
-- never DELETE, never TRUNCATE, never DDL
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM speakql_write;

-- speakql_edits_rw: owns the overlay tables and holds NOTHING on public.
-- This is the assertion that protects the warehouse.
GRANT CONNECT ON DATABASE :"dbname" TO speakql_edits_rw;
GRANT USAGE, CREATE ON SCHEMA member_edits TO speakql_edits_rw;
REVOKE ALL ON SCHEMA public FROM speakql_edits_rw;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM speakql_edits_rw;

-- The read role may look up names in member_edits. It gains SELECT on each
-- overlay table only when edits_rw -- that table's owner -- grants it, and the
-- executor applies only the caller's own overlay. A statement naming
-- member_edits directly is refused by the validator before it gets here.
GRANT USAGE ON SCHEMA member_edits TO speakql_ro;

-- speakql_upload_ddl: nothing here. It works only inside speakql_uploads.
REVOKE ALL ON DATABASE :"dbname" FROM speakql_upload_ddl;

-- Nobody gets the public schema's default CREATE right.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
