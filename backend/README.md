<div align="center">

# SpeakQL — Backend

**Ask your warehouse a question in plain English. Get a validated, read-only answer.**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![sqlglot](https://img.shields.io/badge/validator-sqlglot-8A2BE2)](https://github.com/tobymao/sqlglot)
[![Gemma](https://img.shields.io/badge/LLM-gemma3%3A4b-4A9EFF)](https://ollama.com/library/gemma3)
[![Tests](https://img.shields.io/badge/tests-100%20passing-3FD68B)](#testing)

`Backend` · [`Frontend`](https://github.com/garvbhargava/SpeakQL/tree/Frontend) · [`Full-Stack`](https://github.com/garvbhargava/SpeakQL/tree/Full-Stack) · [`main`](https://github.com/garvbhargava/SpeakQL/tree/main)

*This file is the Backend branch's own README. The project overview for every
branch is [the README at the root](../README.md).*

</div>

---

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [API](#api)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Testing](#testing)
- [Project structure](#project-structure)
- [Roadmap](#roadmap)
- [Branches](#branches)
- [Team](#team)

---

## Overview

SpeakQL converts a plain-English business question into SQL, refuses anything
that is not a single read-only `SELECT`, runs it under a database account that
can only read, and returns the number, a chart, an explanation and the SQL that
produced it.

This branch holds the **backend only** — the API, the database layer, the four
safety layers and the model interfaces. The interface lives on `Frontend`.

**The problem.** Querying a warehouse requires SQL, so business teams depend on
a small pool of analysts for routine reporting. Each ad-hoc request costs an
analyst 2–4 hours and waits 1–2 days in a queue.

**The approach.** Two models fine-tuned in-house — a schema-retrieval
bi-encoder and a text-to-SQL generator — with Gemma as the fallback generator
and the explainer. A deterministic AST parser, never a model, decides whether a
query may run.

> **Gemma generates and explains. sqlglot decides.**
> The safety decision is identical whether the SQL came from CodeT5, from
> Gemma, or from a person typing it by hand — because a parser can be read and
> tested, and a model can only be sampled.

---

## Features

| | |
|---|---|
| **Four safety layers** | Intent gatekeeper → AST validator → database role → edit validator |
| **Read-only by construction** | Enforced at the grant, at the connection, and before execution |
| **Multi-tenant isolation** | Company domains and personal workspaces, with a null-domain guarantee |
| **Two-axis permissions** | Product role (owner/member) × database role (analyst/viewer) per grant |
| **Object-level authorisation** | Every id resolved to its owner; a guessed id is indistinguishable from a missing one |
| **Correction workflow** | Owners write the real table; members propose through an overlay and a merge queue |
| **Staleness refusal** | A merge whose row moved is refused, not applied — this was a real data-loss bug |
| **Prompt-injection defence** | Warehouse values are fenced as data; explainer output cannot act |
| **SSRF defence** | External hosts are *resolved* then judged — the address, never the string |
| **Honest measurement** | `query_log` gets a row on every path: answered, refused, blocked, failed |

---

## Architecture

```
question
   │
   ▼  layer 1 · intent gatekeeper — runs BEFORE generation
┌──────────────────┐
│ question_handler │  credentials? destruction? injection? → refuse, no SQL exists
└──────────────────┘
   │
   ▼
┌──────────────────┐   retrieval    ┌─────────────────────────────┐
│ schema_retriever │ ─────────────► │  MiniLM bi-encoder + FAISS  │
└──────────────────┘                └─────────────────────────────┘
   │  only the relevant tables — never the whole schema
   ▼
┌──────────────────┐   generation   ┌─────────────────────────────┐
│  sql_generator   │ ─────────────► │  CodeT5  ·  Gemma fallback  │
└──────────────────┘                └─────────────────────────────┘
   │  candidate SQL + confidence
   ▼  layer 2 · AST validator — the model is never consulted
┌──────────────────┐
│    validator     │  single read-only SELECT · permitted tables only · sqlglot
└──────────────────┘
   │
   ▼  router — confidence chooses the GENERATOR, never whether it may run
┌──────────────────┐
│     router       │  ≥ 0.55 keep it  ·  below, escalate and validate again
└──────────────────┘
   │
   ▼  layer 3 · the database role itself
┌──────────────────┐
│    executor      │  speakql_ro · read-only transaction · 10 s timeout · row cap
└──────────────────┘
   │
   ▼
answer + chart + explanation + the SQL   →   query_log
```

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + Uvicorn | Async, typed, generates the OpenAPI contract |
| Database | PostgreSQL 15 | Role-level privilege separation *is* the safety model |
| ORM | SQLAlchemy 2 + psycopg 3 | One engine per database role |
| Validation | **sqlglot** | Parses to an AST; never pattern-matches SQL text |
| Retrieval | sentence-transformers + FAISS | 22M-parameter bi-encoder, cosine over columns |
| Generation | CodeT5-small, fine-tuned | 60M parameters, trained in-house |
| Fallback LLM | Gemma `gemma3:4b` via Ollama | Local, open-weight, **never used for safety** |
| Auth | PyJWT + HMAC-SHA256 OTP | Passwordless: six-digit codes, no password anywhere |

---

## Getting started

### Prerequisites

- Docker and Docker Compose
- 8 GB RAM (16 GB to run Gemma locally)

### Installation

```bash
git clone -b Backend https://github.com/garvbhargava/SpeakQL.git
cd SpeakQL/backend

cp .env.example .env          # then set SECRET_KEY
make up                       # postgres + api
make bootstrap                # databases, roles, warehouses, seed
make test-privileges          # prove the safety claims
```

Verify:

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "version": "0.2.0",
  "databases": { "meta": "ok", "read": "ok", "write": "ok", "edits": "ok", "uploads": "ok" },
  "llm": { "mode": "local", "model": "gemma3:4b", "reachable": "yes" }
}
```

Health reports **per-role** status rather than one boolean, because *"the API
is up"* and *"the read path can reach the warehouse"* are different facts, and
the second is the one that breaks.

### Running Gemma locally

```bash
make up-local     # adds the llm container
make pull-model   # ~3 GB, once
```

Without it the API still starts, still answers `/health`, and the explainer
falls back to a deterministic sentence — a backend that refused to boot without
a 3 GB download would make the whole demo hostage to it.

### Signing in during development

There is no password. `make logs` prints the six-digit code:

```
speakql.mail: SIGN-IN CODE for you@company.com is 418902
```

---

## API

25 routes. Full OpenAPI at `http://localhost:8000/docs`.

| Method | Route | What |
|---|---|---|
| `POST` | `/api/auth/start` | Send a code, and say what signing in will do |
| `POST` | `/api/auth/verify` | Check the code; create or join |
| `POST` | `/api/auth/refresh` | Exchange a refresh token |
| `GET` | `/api/auth/me` | Session identity |
| `POST` | **`/api/ask`** | The twelve steps, end to end |
| `GET` | `/api/org` · `/api/org/people` | Organisation and its people |
| `POST` | `/api/org/invite` | Single-use, expiring, role-fixing invitation |
| `POST` | `/api/org/people/{id}/approve` | The owner decides who is in |
| `POST` `DELETE` | `/api/org/people/{id}/grants` | Grant and revoke per database |
| `GET` `POST` | `/api/connections` | List and register external databases |
| `POST` | `/api/datasets/plan` · `/load` | Upload: say what would happen, then do it |
| `POST` | `/api/rows/edit` | Correct a value (owner writes; member proposes) |
| `GET` `POST` | `/api/merges` | The merge queue, with the staleness check |
| `GET` | `/api/schema/{id}` · `/api/threads` | Schema and conversation history |

`/api/ask` returns one of **five shapes**, so the interface renders each
differently: `answer`, `clarify`, `blocked`, `refusal`, `failure`.

---

## Configuration

All configuration is environment variables, validated at startup. `config.py`
exits with a plain message if something required is missing — a backend that
boots half-configured fails later, inside a request, where the cause is much
harder to find.

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | — | **required.** JWT signing and OTP hashing |
| `META_DSN` | — | **required.** Application's own data |
| `RO_DSN` | — | **required.** Every read |
| `WRITE_DSN` | — | **required.** The edit path |
| `EDITS_DSN` | — | **required.** Member overlays |
| `UPLOADS_DSN` | — | **required.** File ingestion |
| `LLM_MODE` | `local` | `local` or `hosted` |
| `LLM_MODEL` | `gemma3:4b` | The only place a model is named |
| `CONFIDENCE_THRESHOLD` | `0.55` | Below this, escalate to the fallback |
| `FREE_EMAIL_MODE` | `personal_workspace` | `personal_workspace`, `invite_only`, `blocked` |
| `STATEMENT_TIMEOUT_MS` | `10000` | Hard stop on any query |
| `MAX_ROWS` | `5000` | Row cap |

> **`SPEAKQL_OWNER_DSN` is deliberately absent from `Settings`.**
> `speakql_owner` can create databases and roles; the API must never hold it.
> An accidental `settings.owner_dsn` fails at *import*, not at runtime.
> `bootstrap.sh` reads it straight from the environment, and a test asserts
> that no field on `Settings` could ever hold it.

---

## Security model

### Five database roles

| Role | Holds | Used by |
|---|---|---|
| `speakql_owner` | everything | `bootstrap.sh` only — **never** the API |
| `speakql_ro` | `SELECT` on warehouse and upload schemas; `SELECT` on overlay **views** only | every read |
| `speakql_write` | `UPDATE`/`INSERT`, only on editable tables, only scoped by primary key | the edit path |
| `speakql_edits_rw` | its own overlay tables; **nothing** on `public` | pending member corrections |
| `speakql_upload_ddl` | `CREATE`/`INSERT` in its own organisation's upload schema | file ingestion |

### Read-only, enforced three times

1. **At the grant** — `speakql_ro` is granted `SELECT` and nothing else.
2. **At the connection** — the read engine sets `default_transaction_read_only`,
   so the server refuses a write even if a grant were wrong.
3. **Before execution** — an sqlglot AST parse rejects anything that is not a
   single read-only `SELECT` over permitted tables.

### What the validator catches that a word-list does not

```sql
SELECT 1 FROM orders; DROP TABLE orders          -- two statements
WITH gone AS (DELETE FROM orders RETURNING *)    -- a write inside a CTE,
SELECT * FROM gone                               --   wrapped in a SELECT
SELECT * INTO copied FROM orders                 -- a SELECT that creates a table
SELECT * FROM orders FOR UPDATE                  -- a SELECT that takes locks
/* harmless */ DROP /* c */ TABLE orders         -- comments between keywords
SELECT * FROM pg_catalog.pg_user                 -- the system catalogue
SELECT pg_read_file('/etc/passwd')               -- a function with side effects
SELECT * FROM other_tenant.payroll               -- another organisation
```

Every one of those is in [`tests/test_validator.py`](tests/test_validator.py).

### Tenant isolation

A company is identified by its email domain. A free-mail signup gets a
**personal workspace**: an organisation row with `kind = 'personal'` and
`domain` **NULL**. The resolver asks `WHERE domain = ? AND kind = 'company'`,
and a null never equals anything — so a personal workspace is unreachable by
domain, permanently. Two people on `gmail.com` get two separate tenants, and a
test asserts exactly that.

### Object-level authorisation

Grants answer *may this person use this database*. They do not answer *is this
message theirs*. Every identifier is resolved to its owner, and **a guessed id
returns the same response as one that does not exist** — same status, same
message. A 404/403 split would be an oracle for mapping the system.

---

## Testing

```bash
make test               # the whole suite
make test-privileges    # the six §17 assertions, against a live database
```

**100 tests run without a database**, so the safety surface can be checked
anywhere:

| Suite | Covers |
|---|---|
| `test_validator.py` | 56 cases — every statement above, plus viewer restrictions, nesting, limits, and that it never raises on hostile input |
| `test_security.py` | SSRF, prompt-injection fencing, and the write path |
| `test_api.py` | The real app end to end: auth, tenant isolation, token-kind confusion, layer 1 |

And **six that only Postgres can answer**, which need the container:

| # | Assertion |
|---|---|
| 1 | The read role can actually read the warehouse |
| 2 | The read role holds no `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE` on **any** table |
| 3 | Read transactions are read-only at the server, not merely by convention |
| 4 | The write role cannot `DELETE` or `TRUNCATE` — a correction never removes |
| 5 | The overlay role holds **nothing** on `public` |
| 6 | No runtime role can create a database or a role |

**Two tenants, on purpose.** `bootstrap.sh` creates `northwind_dw` *and*
`trellis_dw` — isolation cannot be tested against one database. The seed is
deliberately imperfect too: three rows in `shipments` have no `units`, because
the *incomplete* answer mode and the correction workflow need real missing
values. `21_seed.sql` raises if those gaps go missing.

---

## Project structure

Everything backend lives in this folder. The repository root holds only the
combined README and the git configuration.

```
backend/
├── app/        main · config · deps · rbac · object_access · ratelimit · mailer
├── api/        routes_ask · routes_org · routes_connections · routes_datasets
│               routes_merges · routes_misc · schemas.py (the API contract)
├── auth/       routes · otp · jwt_handler · domain_resolver · public_domains
├── core/       question_handler · context_resolver · schema_retriever
│               sql_generator · validator · edit_validator · router · executor
│               merge · visualiser · explainer · llm_client · file_ingest
├── db/         engines · entities · session · introspect · edits_engine · host_guard
├── logs/       query_log · edit_log · audit_log
├── sql/        00_roles · 10_meta · 20_warehouse · 21_seed
├── scripts/    bootstrap.sh
├── tests/      test_validator · test_security · test_api · test_privileges
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── Makefile
├── pytest.ini
├── requirements.txt
└── README.md   ← you are here
```

**Three rules about this tree**

- `validator.py` and `edit_validator.py` are the **only** places safety rules
  live. A check anywhere else is a bug, not a second layer.
- `executor.py` receives an **engine, never a DSN**, and cannot construct one.
- Each of the three logs has **exactly one writer**.

> **Naming note.** Backend Plan §15 calls the log package `logging/`. That name
> shadows Python's standard library and breaks every `import logging` in the
> process, so it is **`logs/`** here. The document should be corrected to match.

---

## Roadmap

Build weeks 1–7 are the mid-term presentation; weeks 8–10 are the end-term.

| Phase | Deliverable | Status |
|:---:|---|:---:|
| 1 | Foundation — compose, config, engines, roles, privilege assertions | ✅ |
| 2 | Synthetic pair generation — *the critical path* | ⏳ |
| 3 | Model A — MiniLM retriever + FAISS (lexical baseline in place) | ⏳ |
| 4 | Auth, grants, object access, API contract | ✅ |
| 5 | Model B — CodeT5 generator | ⬜ |
| 6 | Validator, router, executor, `/api/ask` | ✅ |
| 7 | Corrections — owner path, member overlay, merge + staleness | ✅ |
| 8 | File ingestion and external connections | ✅ |

**Deferred to weeks 8–10 by design:** SSE streaming, voice input, CSV and
report export, the evaluation harness and the ablation table.

Order and rationale: `SpeakQL Build Plan.pdf` on the `documentation` branch.

---

## Branches

| Branch | Contents |
|---|---|
| [`main`](https://github.com/garvbhargava/SpeakQL/tree/main) | Combined README for the whole project |
| [`Backend`](https://github.com/garvbhargava/SpeakQL/tree/Backend) | **This branch** — API, database, models |
| [`Frontend`](https://github.com/garvbhargava/SpeakQL/tree/Frontend) | The interface |
| [`Full-Stack`](https://github.com/garvbhargava/SpeakQL/tree/Full-Stack) | Both, wired together |
| `documentation` | Backend Plan · Complete Logic · Frontend Specification · Build Plan |

---

## Team

**Minor Project · MCA (Artificial Intelligence and Machine Learning)**
School of Computer Science, UPES Dehradun

| Name | Roll |
|---|---|
| Garv Bhargava | 590020664 |
| Dhruv Chhatrawal | 590028180 |
| Kritant Poudnel | — |

**Mentor:** Mr. Pankaj Dadure

---

<div align="center">
<sub>Built for the MCA minor project, 2026–27.</sub>
</div>
