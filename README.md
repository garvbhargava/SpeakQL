<div align="center">

# SpeakQL — Backend

**Ask your warehouse a question in plain English. Get a validated, read-only answer.**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![sqlglot](https://img.shields.io/badge/validator-sqlglot-8A2BE2)](https://github.com/tobymao/sqlglot)
[![Build phase](https://img.shields.io/badge/build%20phase-1%20of%208-E3A55C)](#roadmap)

`Backend` · [`Frontend`](../../tree/Frontend) · [`Full-Stack`](../../tree/Full-Stack) · [`main`](../../tree/main)

</div>

---

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Usage](#usage)
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
can only read, and returns the number, a chart, an explanation, and the SQL
that produced it.

This branch holds the **backend only** — the API, the database layer, the
safety layers and the models. The interface is on `Frontend`.

**The problem.** Querying a warehouse requires SQL, so business teams depend on
a small pool of analysts for routine reporting. Each ad-hoc request costs an
analyst 2–4 hours and waits 1–2 days in a queue.

**The approach.** Two models fine-tuned in-house — a schema-retrieval
bi-encoder and a text-to-SQL generator — with a local LLM as the fallback
generator and the explainer. A deterministic AST parser, never a model, decides
whether a query is allowed to run.

---

## Features

| | |
|---|---|
| **Four safety layers** | Intent gatekeeper → AST validator → database role → edit validator |
| **Read-only by construction** | Enforced at the grant, at the connection, and before execution |
| **Multi-tenant isolation** | Company domains and personal workspaces, with a null-domain guarantee |
| **Two-axis permissions** | Product role (owner/member) × database role (analyst/viewer) on each grant |
| **Correction workflow** | Owners write the real table; members propose through an overlay and a merge queue |
| **Honest measurement** | `query_log` gets a row on every path — answered, refused, blocked and failed alike |

---

## Architecture

```
question
   │
   ▼
┌──────────────────┐   retrieval    ┌─────────────────────────────┐
│  schema_retriever│ ─────────────► │  MiniLM bi-encoder + FAISS  │
└──────────────────┘                └─────────────────────────────┘
   │  only the relevant tables
   ▼
┌──────────────────┐   generation   ┌─────────────────────────────┐
│  sql_generator   │ ─────────────► │  CodeT5  ·  Gemma fallback  │
└──────────────────┘                └─────────────────────────────┘
   │  candidate SQL + confidence
   ▼
┌──────────────────┐
│  validator       │  sqlglot AST · single read-only SELECT · permitted tables only
└──────────────────┘
   │  accepted
   ▼
┌──────────────────┐
│  executor        │  runs under speakql_ro · 10 s timeout · row cap
└──────────────────┘
   │
   ▼
answer + chart + explanation + the SQL
```

**Gemma generates and explains. sqlglot decides.** The safety decision is
identical whether the SQL came from CodeT5, from Gemma, or from a person typing
it by hand — because a parser can be read and tested, and a model can only be
sampled.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + Uvicorn | Async, typed, generates the OpenAPI contract |
| Database | PostgreSQL 15 | Role-level privilege separation is the safety model |
| ORM / SQL | SQLAlchemy 2 + psycopg 3 | One engine per database role |
| Validation | **sqlglot** | Parses to an AST; never pattern-matches SQL text |
| Retrieval | sentence-transformers + FAISS | 22M-parameter bi-encoder, cosine over columns |
| Generation | CodeT5-small, fine-tuned | 60M parameters, trained in-house |
| Fallback LLM | Gemma `gemma3:4b` via Ollama | Local, open-weight, never used for safety |
| Auth | PyJWT + Argon2 | Passwordless: six-digit codes, no password anywhere |

---

## Getting started

### Prerequisites

- Docker and Docker Compose
- 8 GB RAM (16 GB to run Gemma locally)

### Installation

```bash
git clone -b Backend https://github.com/garv503/SpeakQL.git
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
  "version": "0.1.0",
  "databases": { "meta": "ok", "read": "ok", "write": "ok", "edits": "ok", "uploads": "ok" },
  "llm": { "mode": "local", "model": "gemma3:4b" }
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

---

## Configuration

All configuration is environment variables, validated at startup. `config.py`
exits with a plain message if something required is missing — a backend that
boots half-configured fails later, inside a request, where the cause is much
harder to find.

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | — | **required.** JWT signing |
| `META_DSN` | — | **required.** Application's own data |
| `RO_DSN` | — | **required.** Every read |
| `WRITE_DSN` | — | **required.** The edit path |
| `EDITS_DSN` | — | **required.** Member overlays |
| `UPLOADS_DSN` | — | **required.** File ingestion |
| `LLM_MODE` | `local` | `local` or `hosted` |
| `LLM_MODEL` | `gemma3:4b` | The only place a model is named |
| `CONFIDENCE_THRESHOLD` | `0.55` | Below this, escalate to Gemma |
| `FREE_EMAIL_MODE` | `personal_workspace` | `personal_workspace`, `invite_only`, `blocked` |
| `STATEMENT_TIMEOUT_MS` | `10000` | Hard stop on any query |
| `MAX_ROWS` | `5000` | Row cap |

> **`SPEAKQL_OWNER_DSN` is deliberately absent from `Settings`.**
> `speakql_owner` can create databases and roles; the API must never hold it.
> An accidental `settings.owner_dsn` fails at import rather than at runtime.
> `bootstrap.sh` reads it straight from the environment.

See [`.env.example`](backend/.env.example) for the complete list.

---

## Usage

```bash
make help              # list every target
make up                # postgres + api
make up-local          # postgres + api + Gemma
make bootstrap         # databases, roles, warehouses, seed
make test              # whole suite
make test-privileges   # the six assertions
make logs              # follow the api
make down              # stop, keep data
make clean             # stop and DELETE the volume
```

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
2. **At the connection** — the read engine sets
   `default_transaction_read_only`, so the server refuses a write even if a
   grant were wrong.
3. **Before execution** — an sqlglot AST parse rejects anything that is not a
   single read-only `SELECT` over permitted tables.

### Tenant isolation

A company is identified by its email domain. A free-mail signup gets a
**personal workspace**: an organisation row with `kind = 'personal'` and
`domain` **NULL**. The resolver asks `WHERE domain = ? AND kind = 'company'`,
and a null never equals anything — so a personal workspace is unreachable by
domain, permanently. Two people on `gmail.com` get two separate tenants.

---

## Testing

```bash
make test-privileges
```

These run against a live database and ask Postgres itself, which cannot be
argued with.

| # | Assertion |
|---|---|
| 1 | The read role can actually read the warehouse |
| 2 | The read role holds no `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE` on **any** table |
| 3 | Read transactions are read-only at the server, not merely by convention |
| 4 | The write role cannot `DELETE` or `TRUNCATE` — a correction never removes |
| 5 | The overlay role holds **nothing** on `public` |
| 6 | No runtime role can create a database or a role |

A seventh checks the same idea one layer up: `Settings` has no field for the
owner DSN, so the API cannot construct a privileged connection by accident.

**Two tenants, on purpose.** `bootstrap.sh` creates `northwind_dw` *and*
`trellis_dw`. Isolation cannot be tested against one database. The seed is also
deliberately imperfect — three rows in `shipments` have no `units`, because the
*incomplete* answer mode and the correction workflow need real missing values;
`21_seed.sql` raises if those gaps go missing.

---

## Project structure

```
SpeakQL/
├── backend/
│   ├── app/          main.py · config.py · deps · rbac · object_access · ratelimit
│   ├── api/          route modules; schemas.py is the single source of the contract
│   ├── auth/         OTP, JWT, domain resolution, invitations
│   ├── core/         retrieval → generation → validation → routing → execution
│   ├── db/           engines.py (one per role) · entities · introspection
│   ├── logs/         query_log · edit_log · audit_log · notifications
│   ├── sql/          roles · metadata schema · warehouse DDL · seed
│   ├── scripts/      bootstrap.sh
│   ├── tests/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── Makefile
├── .gitignore
└── README.md
```

**Two rules about this tree**

- `validator.py` and `edit_validator.py` are the **only** places safety rules
  live. A check anywhere else is a bug, not a second layer.
- `executor.py` receives an **engine, never a DSN**, and cannot construct one.

> **Naming note.** Backend Plan §15 calls the log package `logging/`. That name
> shadows Python's standard library and breaks every `import logging` in the
> process, so it is **`logs/`** here. The document should be corrected to match.

---

## Roadmap

| Phase | Deliverable | Status |
|:---:|---|:---:|
| 1 | Foundation — compose, config, engines, warehouse, roles, privilege assertions | ✅ |
| 2 | Synthetic pair generation — *the critical path* | ⏳ |
| 3 | Model A — MiniLM retriever + FAISS index | ⬜ |
| 4 | Auth, grants, object access; freeze the API contract | ⬜ |
| 5 | Model B — CodeT5 generator | ⬜ |
| 6 | Validator, router, executor, `/api/ask` | ⬜ |
| 7 | Corrections — owner path, member overlay, merge requests | ⬜ |
| 8 | File ingestion and external connections | ⬜ |

Order and rationale: `SpeakQL Build Plan.pdf` on the `documentation` branch.

---

## Branches

| Branch | Contents |
|---|---|
| [`main`](../../tree/main) | Combined README for the whole project |
| [`Backend`](../../tree/Backend) | **This branch** — API, database, models |
| [`Frontend`](../../tree/Frontend) | The interface |
| [`Full-Stack`](../../tree/Full-Stack) | Both, wired together |
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
