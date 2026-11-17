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
: "${APP_PASSWORD:=app_dev}"

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
for db in speakql_meta northwind_dw harbor_dw speakql_uploads; do
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
  -v app_password="$APP_PASSWORD" \
  -f "$sql_dir/00_roles.sql" >/dev/null

# ------------------------------------------------------------------ meta ----
# speakql_app owns the application's database, so the tables it creates are
# its own and the API never needs the owner role to reach them. (In Postgres
# 15 the public schema belongs to pg_database_owner, i.e. to whoever owns the
# database -- which is what lets speakql_app create tables there.)
say "metadata schema, owned by speakql_app"
psql "$SPEAKQL_OWNER_DSN" -c "ALTER DATABASE speakql_meta OWNER TO speakql_app" >/dev/null
# Postgres grants CONNECT on every new database to PUBLIC. The warehouse roles
# have no business in the application's own database, so only its owner may
# connect -- the read role holding a login here would be one grant away from
# reading every organisation's registry.
psql "$SPEAKQL_OWNER_DSN" -c "REVOKE ALL ON DATABASE speakql_meta FROM PUBLIC" >/dev/null
# The password travels in PGPASSWORD, not spliced into the URL: a password
# containing @, / or # would otherwise change what the URL means.
PGPASSWORD="$APP_PASSWORD" psql "postgresql://speakql_app@${base_dsn##*@}/speakql_meta" \
  -f "$sql_dir/10_meta.sql" >/dev/null

# ------------------------------------------------------------- warehouses ----
for db in northwind_dw harbor_dw; do
  say "warehouse $db: schema and privileges"
  psql "$base_dsn/$db" -v dbname="$db" -f "$sql_dir/20_warehouse.sql" >/dev/null
  say "warehouse $db: seed"
  psql "$base_dsn/$db" -f "$sql_dir/21_seed.sql" >/dev/null
done

# The second tenant's data must differ from the first, or a cross-tenant
# read would return the same answer as a correct one and no test could see it.
say "warehouse harbor_dw: make its data its own"
psql "$base_dsn/harbor_dw" -f "$sql_dir/22_second_tenant.sql" >/dev/null

# --------------------------------------------------------------- uploads ----
say "uploads database"
psql "$base_dsn/speakql_uploads" -c \
  "GRANT CONNECT ON DATABASE speakql_uploads TO speakql_upload_ddl, speakql_ro" >/dev/null
# CREATE on the database is what lets the upload role make each
# organisation's own schema (org_<id>). Without it every upload fails.
psql "$base_dsn/speakql_uploads" -c \
  "GRANT CREATE ON DATABASE speakql_uploads TO speakql_upload_ddl" >/dev/null
psql "$base_dsn/speakql_uploads" -c \
  "REVOKE ALL ON DATABASE speakql_uploads FROM PUBLIC" >/dev/null

# -------------------------------------------------------------- demo seed ----
# Two organisations, each attached to its own warehouse and introspected, so a
# question can be asked the moment the stack is up. Runs through the
# application's own modules rather than raw SQL, so the registry it writes is
# exactly the one the API would write.
say "demo organisations and their warehouses"
python "$here/seed_demo.py"

say "done. now run: make test-privileges"
