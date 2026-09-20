-- SpeakQL · a customer warehouse (Backend Plan §5)
--
-- Run once per tenant database by scripts/bootstrap.sh, against northwind_dw
-- and again against trellis_dw. Two tenants, deliberately: isolation cannot be
-- tested against one.
--
-- The schema is the 18-table warehouse the documents describe, reduced here to
-- the tables the pipeline actually reaches. More arrive with file ingestion.

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

CREATE INDEX IF NOT EXISTS orders_date_idx     ON orders (order_date);
CREATE INDEX IF NOT EXISTS shipments_date_idx  ON shipments (shipped_on);

-- ------------------------------------------------------------ privileges ----
-- This block is the point of the file. tests/test_privileges.py asserts every
-- line of it against the running database.

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

-- speakql_edits_rw: owns the overlay schema and holds NOTHING on public.
-- This is the assertion that protects the warehouse.
GRANT CONNECT ON DATABASE :"dbname" TO speakql_edits_rw;
GRANT USAGE, CREATE ON SCHEMA member_edits TO speakql_edits_rw;
REVOKE ALL ON SCHEMA public FROM speakql_edits_rw;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM speakql_edits_rw;

-- speakql_upload_ddl: nothing here. It works only inside speakql_uploads.
REVOKE ALL ON DATABASE :"dbname" FROM speakql_upload_ddl;

-- Nobody gets the public schema's default CREATE right.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
