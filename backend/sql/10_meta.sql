-- SpeakQL · metadata schema, database speakql_meta (Backend Plan §5.2)
--
-- Owned by the API. This is the application's own data -- never a customer's
-- warehouse. Run by scripts/bootstrap.sh after 00_roles.sql.

\set ON_ERROR_STOP on

-- ------------------------------------------------------- organisations ----
-- kind is 'company' or 'personal'. domain is NULL for personal, and that null
-- is the whole tenant-isolation guarantee: domain_resolver asks
--   WHERE domain = ? AND kind = 'company'
-- and a NULL never equals anything, so a personal workspace can never be
-- reached by domain. Two people signing up with gmail.com get two rows here,
-- not one shared row. (§7.2)

CREATE TABLE IF NOT EXISTS organisations (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    kind         TEXT        NOT NULL CHECK (kind IN ('company', 'personal')),
    domain       TEXT        NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- a company must have a domain; a personal workspace must not have one
    CONSTRAINT org_domain_matches_kind CHECK (
        (kind = 'company'  AND domain IS NOT NULL) OR
        (kind = 'personal' AND domain IS NULL)
    )
);

-- Only one company may claim a domain. Personal rows are excluded because
-- their domain is NULL, and a partial index keeps them out entirely.
CREATE UNIQUE INDEX IF NOT EXISTS organisations_company_domain_key
    ON organisations (lower(domain)) WHERE kind = 'company';

-- --------------------------------------------------------------- people ----
CREATE TABLE IF NOT EXISTS people (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT        NOT NULL,
    org_id        BIGINT      NOT NULL REFERENCES organisations (id) ON DELETE CASCADE,
    product_role  TEXT        NOT NULL CHECK (product_role IN ('owner', 'member')),
    state         TEXT        NOT NULL DEFAULT 'pending'
                              CHECK (state IN ('pending', 'active', 'suspended')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS people_email_key ON people (lower(email));
CREATE INDEX IF NOT EXISTS people_org_idx ON people (org_id);

-- ---------------------------------------------------------- connections ----
-- A registered database, or the home of an uploaded dataset. Credentials are
-- encrypted at rest and are never returned by any endpoint, including the one
-- that lists connections. (§9.3)
CREATE TABLE IF NOT EXISTS connections (
    id             BIGSERIAL PRIMARY KEY,
    org_id         BIGINT      NOT NULL REFERENCES organisations (id) ON DELETE CASCADE,
    name           TEXT        NOT NULL,
    kind           TEXT        NOT NULL CHECK (kind IN ('external', 'uploaded')),
    host           TEXT        NULL,
    port           INTEGER     NULL,
    database_name  TEXT        NOT NULL,
    secret_cipher  BYTEA       NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

-- --------------------------------------------------------- access_grants ----
-- person x connection, carrying the DATABASE role. This is the second axis of
-- permission and it lives on the grant, never on the person -- which is what
-- lets one person be an analyst on Sales and a viewer on Support. (§6.4)
CREATE TABLE IF NOT EXISTS access_grants (
    id             BIGSERIAL PRIMARY KEY,
    person_id      BIGINT      NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    connection_id  BIGINT      NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    db_role        TEXT        NOT NULL CHECK (db_role IN ('analyst', 'viewer')),
    granted_by     BIGINT      NULL REFERENCES people (id),
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at     TIMESTAMPTZ NULL,
    UNIQUE (person_id, connection_id)
);

CREATE INDEX IF NOT EXISTS access_grants_person_idx ON access_grants (person_id)
    WHERE revoked_at IS NULL;

-- ---------------------------------------------------------- invitations ----
CREATE TABLE IF NOT EXISTS invitations (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT      NOT NULL REFERENCES organisations (id) ON DELETE CASCADE,
    email        TEXT        NOT NULL,
    token_hash   TEXT        NOT NULL UNIQUE,
    product_role TEXT        NOT NULL CHECK (product_role IN ('owner', 'member')),
    grants_json  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    expires_at   TIMESTAMPTZ NOT NULL,
    redeemed_at  TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------- schema_registry ----
-- One row per column. is_public decides what a viewer may read.
CREATE TABLE IF NOT EXISTS schema_registry (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT  NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    schema_name    TEXT    NOT NULL,
    table_name     TEXT    NOT NULL,
    column_name    TEXT    NOT NULL,
    data_type      TEXT    NOT NULL,
    description    TEXT    NULL,
    is_public      BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (connection_id, schema_name, table_name, column_name)
);

-- -------------------------------------------------------- editable_tables ----
-- Default: every table with a primary key. A change that cannot be pinned to
-- one row cannot be checked, so a table without one is never editable.
CREATE TABLE IF NOT EXISTS editable_tables (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    schema_name    TEXT   NOT NULL,
    table_name     TEXT   NOT NULL,
    pk_column      TEXT   NOT NULL,
    UNIQUE (connection_id, schema_name, table_name)
);

-- --------------------------------------------------------- member_edits ----
CREATE TABLE IF NOT EXISTS member_edits (
    id             BIGSERIAL PRIMARY KEY,
    person_id      BIGINT      NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    connection_id  BIGINT      NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    schema_name    TEXT        NOT NULL,
    table_name     TEXT        NOT NULL,
    pk_column      TEXT        NOT NULL,
    pk_value       TEXT        NOT NULL,
    column_name    TEXT        NOT NULL,
    before_value   TEXT        NULL,
    after_value    TEXT        NOT NULL,
    note           TEXT        NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -------------------------------------------------------- merge_requests ----
-- stale is a state, not an error: the row moved after the request was raised,
-- so applying it would destroy a newer verified value. (§10.5)
CREATE TABLE IF NOT EXISTS merge_requests (
    id             BIGSERIAL PRIMARY KEY,
    member_edit_id BIGINT      NOT NULL REFERENCES member_edits (id) ON DELETE CASCADE,
    decided_by     BIGINT      NULL REFERENCES people (id),
    state          TEXT        NOT NULL DEFAULT 'open'
                               CHECK (state IN ('open', 'merged', 'rejected', 'stale')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS merge_requests_open_idx ON merge_requests (state)
    WHERE state = 'open';

-- ------------------------------------------------------ uploaded_datasets ----
CREATE TABLE IF NOT EXISTS uploaded_datasets (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT      NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    original_name  TEXT        NOT NULL,
    schema_name    TEXT        NOT NULL,
    table_name     TEXT        NOT NULL,
    row_count      BIGINT      NOT NULL,
    uploaded_by    BIGINT      NOT NULL REFERENCES people (id),
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------- threads, messages, asking ----
CREATE TABLE IF NOT EXISTS threads (
    id          BIGSERIAL PRIMARY KEY,
    person_id   BIGINT      NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    title       TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   BIGINT      NOT NULL REFERENCES threads (id) ON DELETE CASCADE,
    person_id   BIGINT      NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    question    TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clarifications (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT      NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    options     JSONB       NOT NULL,
    chosen      TEXT        NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT      NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    person_id   BIGINT      NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    rating      SMALLINT    NOT NULL CHECK (rating IN (-1, 1)),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, person_id)
);

CREATE TABLE IF NOT EXISTS otp_attempts (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT        NOT NULL,
    failures      INTEGER     NOT NULL DEFAULT 0,
    locked_until  TIMESTAMPTZ NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (email)
);

-- ----------------------------------------------------------- the 3 logs ----
-- Each has exactly one writer. query_log gets a row on EVERY path -- answered,
-- refused, blocked and failed alike -- which is what makes the measurement
-- honest. (§20)

CREATE TABLE IF NOT EXISTS query_log (
    id              BIGSERIAL PRIMARY KEY,
    person_id       BIGINT      NOT NULL REFERENCES people (id),
    org_id          BIGINT      NOT NULL REFERENCES organisations (id),
    connection_id   BIGINT      NULL REFERENCES connections (id),
    question        TEXT        NOT NULL,
    outcome         TEXT        NOT NULL CHECK (outcome IN
                        ('answered', 'clarified', 'blocked', 'refused', 'failed', 'rate_limited')),
    route           TEXT        NULL,
    generated_sql   TEXT        NULL,
    confidence      NUMERIC(4, 3) NULL,
    latency_ms      INTEGER     NULL,
    row_count       INTEGER     NULL,
    refused_by      TEXT        NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS query_log_org_idx ON query_log (org_id, created_at DESC);

CREATE TABLE IF NOT EXISTS edit_log (
    id             BIGSERIAL PRIMARY KEY,
    person_id      BIGINT      NOT NULL REFERENCES people (id),
    connection_id  BIGINT      NOT NULL REFERENCES connections (id),
    schema_name    TEXT        NOT NULL,
    table_name     TEXT        NOT NULL,
    pk_column      TEXT        NOT NULL,
    pk_value       TEXT        NOT NULL,
    column_name    TEXT        NOT NULL,
    before_value   TEXT        NULL,
    after_value    TEXT        NULL,
    note           TEXT        NULL,
    via_merge_id   BIGINT      NULL REFERENCES merge_requests (id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    person_id   BIGINT      NULL REFERENCES people (id),
    org_id      BIGINT      NULL REFERENCES organisations (id),
    action      TEXT        NOT NULL,
    target      TEXT        NULL,
    detail      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_org_idx ON audit_log (org_id, created_at DESC);
