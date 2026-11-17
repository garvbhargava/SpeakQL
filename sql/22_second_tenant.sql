-- SpeakQL · make the second tenant's data unmistakably its own
--
-- Run against harbor_dw after 21_seed.sql. Both warehouses are loaded from the
-- same seed, which would make the most important test in the project
-- worthless: if tenant B's question were wrongly executed on tenant A's
-- warehouse, identical data would return identical answers, and nothing could
-- tell the cross-tenant read from a correct one.
--
-- So every region and customer name here is different, and every amount is.
-- A question asked on Harbor's connection that comes back mentioning plain
-- "West" or $482,140 has read Northwind's warehouse -- and
-- tests/test_privileges.py asks both databases directly and fails if their
-- answers agree.
--
-- No extra marker table: it would appear in Harbor's schema, in retrieval and
-- in the demo, and the data below already tells the two apart.
--
-- Idempotent only because 21_seed.sql truncates and reloads first, which is
-- the order bootstrap.sh runs them in.

\set ON_ERROR_STOP on

UPDATE regions   SET region_name = 'Harbor ' || region_name;
UPDATE customers SET name = 'Harbor ' || name;
UPDATE orders    SET amount = round(amount * 0.37, 2);
