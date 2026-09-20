#!/usr/bin/env bash
# SpeakQL · create the databases, the five roles and the seeded warehouses.
#
# Runs as speakql_owner, which is the ONLY place that DSN is used. The API
# cannot load it: config.py defines no field for it.
#
#   ./scripts/bootstrap.sh          bootstrap against SPEAKQL_OWNER_DSN
#   make bootstrap                  the same, inside the compose network
#
# Safe to run twice. Databases and roles are created only if absent; the
# warehouse DDL is idempotent; the seed truncates and reloads.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sql_dir="$here/../sql"

# .env is optional -- the compose service passes these as real env vars.
if [[ -f "$here/../.env" ]]; then
  set -a; . "$here/../.env"; set +a
fi

: "${SPEAKQL_OWNER_DSN:?SPEAKQL_OWNER_DSN is required (see .env.example)}"
: "${RO_PASSWORD:=ro_dev}"
: "${WRITE_PASSWORD:=write_dev}"
: "${EDITS_PASSWORD:=edits_dev}"
: "${UPLOAD_PASSWORD:=upload_dev}"

# The owner DSN points at `postgres`; swap the database per step.
base_dsn="${SPEAKQL_OWNER_DSN%/*}"
say() { printf '\033[36m==>\033[0m %s\n' "$1"; }

say "waiting for postgres"
for _ in $(seq 1 30); do
  if psql "$SPEAKQL_OWNER_DSN" -c 'SELECT 1' >/dev/null 2>&1; then break; fi
  sleep 1
done
psql "$SPEAKQL_OWNER_DSN" -c 'SELECT 1' >/dev/null

# ------------------------------------------------------------- databases ----
# Two tenants deliberately: isolation cannot be tested against one.
for db in speakql_meta northwind_dw trellis_dw speakql_uploads; do
  if psql "$SPEAKQL_OWNER_DSN" -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    say "database $db already exists"
  else
    say "creating database $db"
    psql "$SPEAKQL_OWNER_DSN" -c "CREATE DATABASE $db"
  fi
done

# ----------------------------------------------------------------- roles ----
say "creating the five roles"
psql "$SPEAKQL_OWNER_DSN" \
  -v ro_password="$RO_PASSWORD" \
  -v write_password="$WRITE_PASSWORD" \
  -v edits_password="$EDITS_PASSWORD" \
  -v upload_password="$UPLOAD_PASSWORD" \
  -f "$sql_dir/00_roles.sql" >/dev/null

# ------------------------------------------------------------------ meta ----
say "metadata schema"
psql "$base_dsn/speakql_meta" -f "$sql_dir/10_meta.sql" >/dev/null

# ------------------------------------------------------------- warehouses ----
for db in northwind_dw trellis_dw; do
  say "warehouse $db: schema and privileges"
  psql "$base_dsn/$db" -v dbname="$db" -f "$sql_dir/20_warehouse.sql" >/dev/null
  say "warehouse $db: seed"
  psql "$base_dsn/$db" -f "$sql_dir/21_seed.sql" >/dev/null
done

# --------------------------------------------------------------- uploads ----
say "uploads database"
psql "$base_dsn/speakql_uploads" -c \
  "GRANT CONNECT ON DATABASE speakql_uploads TO speakql_upload_ddl, speakql_ro" >/dev/null
psql "$base_dsn/speakql_uploads" -c \
  "REVOKE ALL ON DATABASE speakql_uploads FROM PUBLIC" >/dev/null

say "done. now run: make test-privileges"
