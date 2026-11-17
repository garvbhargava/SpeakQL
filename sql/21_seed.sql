-- SpeakQL · warehouse seed
--
-- Small, deterministic and deliberately imperfect. The gaps in shipments.units
-- are not an oversight: the "incomplete" answer mode and the whole correction
-- workflow need real missing values to act on.

\set ON_ERROR_STOP on

TRUNCATE order_items, shipments, orders, customers, products, regions RESTART IDENTITY CASCADE;

INSERT INTO regions (region_name) VALUES
    ('West'), ('North'), ('South'), ('East'), ('Central');

INSERT INTO customers (name, region_id, created_at) VALUES
    ('Aster Retail',      1, DATE '2024-03-11'),
    ('Brightline Foods',  1, DATE '2024-06-02'),
    ('Cobalt Industrial', 2, DATE '2024-01-19'),
    ('Dunmore Supply',    2, DATE '2024-08-27'),
    ('Eastgate Traders',  3, DATE '2025-02-14'),
    ('Fairhaven Group',   3, DATE '2024-11-05'),
    ('Granby Wholesale',  4, DATE '2025-01-30'),
    ('Halloway Partners', 5, DATE '2024-09-16');

INSERT INTO products (sku, name, unit_cost) VALUES
    ('BR-2214', 'Barrier film, 50 micron', 142.00),
    ('BR-2218', 'Barrier film, 80 micron', 188.50),
    ('CT-1180', 'Carton, 12 litre',         38.25),
    ('CT-1182', 'Carton, 20 litre',         51.75),
    ('LB-4400', 'Label roll, thermal',      12.40),
    ('PL-7710', 'Pallet wrap, heavy',       96.00);

-- Orders across Q4 2025, weighted so West leads the quarter. The documents
-- quote $482,140 for West; these rows are what produce it.
INSERT INTO orders (customer_id, order_date, amount) VALUES
    (1, DATE '2025-10-04', 61200.00), (1, DATE '2025-11-12', 88400.00),
    (1, DATE '2025-12-02', 74300.00), (2, DATE '2025-10-21', 92800.00),
    (2, DATE '2025-11-29', 77140.00), (2, DATE '2025-12-18', 88300.00),
    (3, DATE '2025-10-09', 74100.00), (3, DATE '2025-11-03', 96200.00),
    (3, DATE '2025-12-11', 81900.00), (4, DATE '2025-10-27', 45800.00),
    (4, DATE '2025-12-05', 57200.00), (5, DATE '2025-10-15', 71300.00),
    (5, DATE '2025-11-20', 84600.00), (6, DATE '2025-12-22', 69500.00),
    (6, DATE '2025-11-08', 73000.00), (7, DATE '2025-10-30', 92400.00),
    (7, DATE '2025-12-14', 66100.00), (7, DATE '2025-11-17', 83400.00),
    (8, DATE '2025-10-12', 58900.00), (8, DATE '2025-11-25', 64200.00),
    (8, DATE '2025-12-09', 64200.00);

INSERT INTO order_items (order_id, product_id, qty, unit_price) VALUES
    (1, 1, 180, 214.00), (1, 3,  90,  61.00),
    (2, 2, 260, 279.00), (2, 5, 400,  19.90),
    (3, 1, 210, 214.00), (3, 6, 120, 148.00),
    (4, 4, 310,  79.50), (4, 3, 140,  61.00),
    (5, 2, 190, 279.00), (5, 5, 520,  19.90),
    (6, 1, 240, 214.00), (6, 6, 150, 148.00),
    (7, 3, 300,  61.00), (7, 4, 180,  79.50),
    (8, 2, 280, 279.00), (8, 1, 130, 214.00),
    (9, 6, 200, 148.00), (9, 5, 610,  19.90);

-- Week 12 of 2025, 16-22 March. Three rows have no unit count, on purpose:
-- 84117, 84120 and 84122. The documents use exactly these three.
INSERT INTO shipments (shipment_id, order_id, shipped_on, carrier, units) OVERRIDING SYSTEM VALUE VALUES
    (84112, 1, DATE '2025-03-16', 'Redline',    198),
    (84114, 2, DATE '2025-03-17', 'Redline',    214),
    (84116, 3, DATE '2025-03-18', 'Northbound', 187),
    (84117, 4, DATE '2025-03-19', 'Northbound', NULL),
    (84119, 5, DATE '2025-03-20', 'Redline',    241),
    (84120, 6, DATE '2025-03-20', 'Coastal',    NULL),
    (84121, 7, DATE '2025-03-21', 'Coastal',    132),
    (84122, 8, DATE '2025-03-21', 'Northbound', NULL),
    (84123, 9, DATE '2025-03-22', 'Coastal',    111);

SELECT setval('shipments_shipment_id_seq', (SELECT MAX(shipment_id) FROM shipments));

-- Sanity: the three gaps must exist, or the correction workflow has nothing to
-- correct and half the test matrix is untestable.
DO $$
DECLARE gaps INTEGER;
BEGIN
    SELECT count(*) INTO gaps FROM shipments WHERE units IS NULL;
    IF gaps <> 3 THEN
        RAISE EXCEPTION 'seed is wrong: expected 3 rows with no units, found %', gaps;
    END IF;
END
$$;
