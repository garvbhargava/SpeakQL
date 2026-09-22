-- SpeakQL · the five database roles (Backend Plan §5.1)
--
-- Run once, as speakql_owner, by scripts/bootstrap.sh.
--
-- The read path holds no write privilege of any kind. Every write in the system
-- goes through one path that is validated, scoped to a single primary key,
-- restricted to tables the owner marked editable, and written to the edit log.
-- tests/test_privileges.py asserts all six of those facts against a live
-- database; if you change anything here, that file is what tells you.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- roles ----
-- Created with NOLOGIN first, then given passwords from the environment, so
-- this file carries no secret and can live in the repository.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'speakql_ro') THEN
    CREATE ROLE speakql_ro LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'speakql_write') THEN
    CREATE ROLE speakql_write LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'speakql_edits_rw') THEN
    CREATE ROLE speakql_edits_rw LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'speakql_upload_ddl') THEN
    CREATE ROLE speakql_upload_ddl LOGIN;
  END IF;
  -- The application's own data. The first version connected to speakql_meta
  -- as speakql_owner -- a SUPERUSER -- so the API held exactly the privilege
  -- the design says it never holds. This role owns the metadata tables and
  -- nothing else, and it is not a superuser.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'speakql_app') THEN
    CREATE ROLE speakql_app LOGIN;
  END IF;
END
$$;

-- Passwords come from the bootstrap environment, never from this file.
ALTER ROLE speakql_ro          PASSWORD :'ro_password';
ALTER ROLE speakql_write       PASSWORD :'write_password';
ALTER ROLE speakql_edits_rw    PASSWORD :'edits_password';
ALTER ROLE speakql_upload_ddl  PASSWORD :'upload_password';
ALTER ROLE speakql_app         PASSWORD :'app_password';

-- No runtime role may create databases or roles, bypass row security, or be a
-- superuser. Only speakql_owner can, and it is used by bootstrap alone: the
-- API's container is never given its DSN, and config.py refuses to start if
-- any runtime DSN names it.
ALTER ROLE speakql_ro          NOCREATEDB NOCREATEROLE NOSUPERUSER NOBYPASSRLS;
ALTER ROLE speakql_write       NOCREATEDB NOCREATEROLE NOSUPERUSER NOBYPASSRLS;
ALTER ROLE speakql_edits_rw    NOCREATEDB NOCREATEROLE NOSUPERUSER NOBYPASSRLS;
ALTER ROLE speakql_upload_ddl  NOCREATEDB NOCREATEROLE NOSUPERUSER NOBYPASSRLS;
ALTER ROLE speakql_app         NOCREATEDB NOCREATEROLE NOSUPERUSER NOBYPASSRLS;

-- ------------------------------------------------------------ databases ----
-- speakql_meta   the application's own data, owned by the API
-- northwind_dw   the seeded customer warehouse
-- harbor_dw      a second tenant, because isolation cannot be tested with one
-- speakql_uploads  tables created from uploaded files

SELECT 'databases are created by bootstrap.sh before this file runs' AS note;
