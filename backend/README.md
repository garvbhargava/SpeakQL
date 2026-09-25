<div align="center">

# SpeakQL — Backend

**Ask your warehouse a question in plain English. Get a validated, read-only answer.**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![sqlglot](https://img.shields.io/badge/validator-sqlglot-8A2BE2)](https://github.com/tobymao/sqlglot)
[![Gemma](https://img.shields.io/badge/LLM-gemma3%3A4b-4A9EFF)](https://ollama.com/library/gemma3)
[![Tests](https://img.shields.io/badge/tests-218%20passing-3FD68B)](#testing)

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
| **Two models, trained in-house** | MiniLM finds the tables, CodeT5 writes the SQL; Gemma answers below the confidence threshold |
| **Four safety layers** | Intent gatekeeper → AST validator → database role → edit validator |
| **Read-only by construction** | Enforced at the grant, at the connection, and before execution |
| **Multi-tenant isolation** | Company domains and personal workspaces, with a null-domain guarantee |
| **Per-connection engines** | A question runs on the warehouse it was about — checked again with `current_database()` before it runs |
| **Two-axis permissions** | Product role (owner/member) × database role (analyst/viewer) per grant |
| **Object-level authorisation** | Every id resolved to its owner; a guessed id is indistinguishable from a missing one |
| **Correction workflow** | Owners write the real table; members propose through an overlay and a merge queue |
| **Staleness refusal** | A merge whose row moved is refused, not applied — this was a real data-loss bug |
| **Prompt-injection defence** | Warehouse values are fenced as data; explainer output cannot act |
| **SSRF defence** | External hosts are *resolved* then judged, and the connection is pinned to the address that passed |
| **Sign-in abuse controls** | Five-attempt lockout, 60 s resend cooldown, hourly caps per address and per network |
| **Join paths, not guesses** | Foreign keys come from the catalogue, so the generator is never left to invent one |
| **Honest measurement** | `query_log` gets a row on every path: answered, refused, blocked, failed — with the generator and its confidence |

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
│ schema_retriever │ ─────────────► │  Model A · MiniLM + FAISS   │
└──────────────────┘                └─────────────────────────────┘
   │  the relevant tables — never the whole schema
   ▼  + the tables a join must pass through, from the foreign keys
┌──────────────────┐   generation   ┌─────────────────────────────┐
│  sql_generator   │ ─────────────► │  Model B · CodeT5-small     │
└──────────────────┘                │  Gemma, below the threshold │
   │                                └─────────────────────────────┘
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

## The two models

Both are trained in-house, on this machine, on a CPU. **Both are optional at
runtime**: with no checkpoint the pipeline retrieves lexically and generates
with Gemma — which is not a degraded mode so much as the off-the-shelf
baseline the comparative study measures against. `/health` says which is
loaded.

| | Model A — retrieval | Model B — generation |
|---|---|---|
| Base | `all-MiniLM-L6-v2`, 22M | `Salesforce/codet5-small`, 60M |
| Job | which tables is this question about | question + those tables → SQL |
| Trained on | Spider gold tables + this warehouse's pairs, contrastive with one hard negative per example | Spider (7,000 questions, 140 databases), then this warehouse's verified pairs |
| Confidence | cosine, used to rank | the length-normalised beam score — the number the router routes on |
| Absent | lexical overlap (the baseline) | Gemma writes every query |

### The data

| Set | Size | What it is |
|---|---|---|
| Spider 1.0 | 7,000 train / 1,034 dev | 200 databases, dev databases never seen in training. `python -m ml.spider_prep` |
| Synthetic pairs | 1,329 | Generated over *this* warehouse from 48 question templates and **executed before being kept** — a pair that does not run is not training data, it is a lie the model learns |

The synthetic test split is held out two ways, and they are never averaged
into one number:

- **new phrasing** — a wording the model never saw, of a question shape it did
- **new shape** — whole question types held out of training entirely

Reporting the first alone would be reporting memorisation with extra steps.

### Reproducing

```bash
make data              # Spider, then the synthetic pairs (verified against the warehouse)
make train-generator   # Model B: stage 1 then stage 2
make train-retriever   # Model A
make evaluate          # the recall table and the accuracy table
```

Or unattended, in a container that survives the terminal closing:

```bash
docker compose --profile train up -d trainer
docker compose logs -f trainer
```

Every stage checkpoints as it goes and resumes from its own checkpoint, so an
interruption costs minutes rather than the run. The checkpoints land in
`ml/checkpoints/`, which the `api` container mounts read-only — retraining is
a restart, not a rebuild.

### How these are measured

Four decisions about the numbers, made before they were produced:

- **Recall is strict**: *every* gold table inside the top k, not "at least
  one". The generator is handed the top k and nothing else, so a query
  needing three tables of which two were retrieved is a query that cannot be
  written. Counting that as two-thirds right would flatter the number.
- **Retrieval is also measured as the pipeline uses it** — with join-path
  completion — because retrieval scores each table against the question on
  its own and therefore cannot score a table the question never mentions.
- **Execution accuracy is the number that matters**: the generated statement
  and the gold statement are both *run* on the seeded warehouse and their rows
  compared. Exact match is reported beside it and is unfair by construction —
  there are many correct ways to write the same query and it calls them wrong.
- **Gemma is sampled, not run over the whole set.** On a CPU it takes tens of
  seconds a question; the sample size is printed beside the number rather than
  the number being quoted as more than it is.

The warehouse test set is synthetic and in-domain: it says how well the model
answers questions of the kind it was trained on, about this warehouse. It says
nothing about a warehouse it has never seen — that is what the Spider dev
numbers are for, and they are reported separately.

### Model A — measured

Strict recall: **every** gold table inside the top k. `ml/results/retriever-recall.json`.

**Spider dev — 1,034 questions over 20 databases that appear nowhere in training**

| Retriever | recall@1 | recall@3 | recall@5 |
|---|---:|---:|---:|
| lexical overlap (baseline) | 49.9% | 93.3% | 98.5% |
| MiniLM, off the shelf | **54.0%** | 95.7% | 99.4% |
| MiniLM, fine-tuned | 53.6% | 97.7% | 99.6% |
| MiniLM, fine-tuned + join paths | 53.6% | **98.1%** | **99.6%** |

**This warehouse — 299 held-out questions**

| Retriever | recall@1 | recall@3 | recall@5 |
|---|---:|---:|---:|
| lexical overlap (baseline) | 16.1% | 42.5% | 93.0% |
| MiniLM, off the shelf | **20.4%** | 38.5% | 80.3% |
| MiniLM, fine-tuned | 19.1% | **99.7%** | **100.0%** |

Three things in those tables are worth saying out loud:

- **Fine-tuning is what makes retrieval usable here.** Off the shelf, MiniLM
  covers every needed table for 38.5% of warehouse questions; fine-tuned, for
  99.7%. The off-the-shelf model ranks tables by how much their *names* sound
  like the question, and "which region sold the most" does not sound like
  `orders`.
- **recall@1 is low everywhere on the warehouse, and that is arithmetic, not
  failure.** Most of these questions need two or three tables — sales by
  region is `orders`, `customers` and `regions` — so one table can almost
  never be all of them. It is reported because leaving it out would be
  choosing the flattering columns.
- **The fine-tuned model is marginally worse at recall@1 on Spider** (53.6%
  against 54.0%) while being clearly better at 3 and 5. Training pushed it
  towards covering the whole set of tables a query needs rather than ranking
  one of them first, which is the behaviour the pipeline actually uses.

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
make bootstrap                # databases, roles, warehouses, demo organisations
make test-privileges          # prove the safety claims
```

No `make` (plain Windows)? The targets are one line each:

```bash
docker compose up -d --build db api
docker compose run --rm bootstrap
docker compose exec -T api pytest tests/test_privileges.py -v
```

`bootstrap` is a **one-off container** and the only place the owner's
credentials exist at runtime; the long-running `api` container is never given
them. It is safe to run again.

### Demo organisations

Bootstrap seeds two tenants, each attached to **its own** warehouse:

| Organisation | Sign in as | Warehouse |
|---|---|---|
| Northwind Group | `garv@northwind.co` (owner) | `northwind_dw` |
| Harbor Supply | `owner@harborsupply.co` (owner) | `harbor_dw` |

Harbor's data is deliberately different from Northwind's (`sql/22_second_tenant.sql`),
so an answer read from the wrong warehouse would be visibly wrong.

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

24 routes, plus `/health`. Full OpenAPI at `http://localhost:8000/docs`.

| Method | Route | What |
|---|---|---|
| `POST` | `/api/auth/start` | Send a code, and say what signing in will do; accepts an `invite_token` |
| `POST` | `/api/auth/verify` | Check the code; sign in, create, join, or redeem the invitation |
| `POST` | `/api/auth/refresh` | Exchange a refresh token |
| `GET` | `/api/auth/me` | Session identity |
| `POST` | **`/api/ask`** | The twelve steps, end to end |
| `GET` | `/api/org` · `/api/org/people` | Organisation and its people |
| `POST` | `/api/org/invite` | Single-use, expiring, role-fixing invitation |
| `POST` | `/api/org/people/{id}/approve` | The owner decides who is in |
| `POST` `DELETE` | `/api/org/people/{id}/grants` | Grant and revoke per database |
| `GET` `POST` | `/api/connections` | List databases; register an external one (four checks, reported one by one) |
| `POST` | `/api/connections/{id}/reindex` | Re-read a database's schema into the registry |
| `POST` | `/api/datasets/plan` · `/load` | Upload: say what would happen, then do it |
| `POST` | `/api/rows/edit` | Correct a value (owner writes; member proposes) |
| `GET` `POST` | `/api/merges` | The merge queue, with the staleness check |
| `GET` | `/api/schema/{id}` · `/api/threads` | Schema and conversation history |
| `POST` | `/api/feedback/{message_id}` | Rate an answer |

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
| `SECRET_KEY` | — | **required.** JWT signing, OTP hashing, and (through HKDF) the credential key |
| `META_DSN` | — | **required.** Application's own data, as `speakql_app` |
| `RO_DSN` | — | **required.** Credentials for every read; the database is chosen per connection |
| `WRITE_DSN` | — | **required.** The edit path |
| `EDITS_DSN` | — | **required.** Member overlays |
| `UPLOADS_DSN` | — | **required.** File ingestion |
| `LLM_MODE` | `local` | `local` or `hosted` |
| `LLM_MODEL` | `gemma3:4b` | The only place a model is named |
| `CONFIDENCE_THRESHOLD` | `0.55` | Below this, escalate to the fallback |
| `FREE_EMAIL_MODE` | `personal_workspace` | `personal_workspace`, `invite_only`, `blocked` |
| `STATEMENT_TIMEOUT_MS` | `10000` | Hard stop on any query |
| `MAX_ROWS` | `5000` | Row cap |
| `INVITE_TTL_HOURS` | `168` | How long an invitation link lives |
| `RATE_CODES_PER_ADDRESS_HOUR` | `10` | Sign-in codes one address may be sent per hour |
| `RATE_CODES_PER_IP_HOUR` | `30` | Sign-in codes one network may request per hour |
| `COMPOSE_LLM_ENDPOINT` | `http://llm:11434` | Where the `api` container finds Gemma |

> **`SPEAKQL_OWNER_DSN` is deliberately absent from `Settings`.**
> `speakql_owner` can create databases and roles; the API must never hold it.
> An accidental `settings.owner_dsn` fails at *import*, not at runtime — and
> `config.py` **refuses to start** if any of the five runtime DSNs names
> `speakql_owner` or `postgres`.

---

## Security model

### Six database roles — five at runtime, none of them privileged

| Role | Holds | Used by |
|---|---|---|
| `speakql_owner` | everything (superuser) | the one-off `bootstrap` container only — **never** the API |
| `speakql_app` | owns `speakql_meta`; **nothing** on any warehouse | the API's own data |
| `speakql_ro` | `SELECT` on warehouse and upload schemas, and on overlay rows | every read |
| `speakql_write` | `SELECT`/`INSERT`/`UPDATE` on the warehouse; never `DELETE` or `TRUNCATE` | the edit path |
| `speakql_edits_rw` | its own overlay tables in `member_edits`; **nothing** on `public` | pending member corrections |
| `speakql_upload_ddl` | `CREATE` on the uploads database, for per-organisation schemas | file ingestion |

No runtime role can create a database or a role, bypass row security, or log
in to `speakql_meta` except `speakql_app` — and Postgres itself is asked to
confirm each of those in `tests/test_privileges.py`.

### A question runs on the warehouse it was about

Every warehouse engine is **built from the connection row** the caller was
authorised to hold (`db/tenant_engine.py`) — the role is fixed, only the
database changes. There is no shared read engine that could point anywhere
else. Then, immediately before the query runs, the executor asks the server
`SELECT current_database()` and refuses a mismatch. Two checks, because one
check is one point of failure.

### Member corrections are an overlay, resolved at execution

A member's pending value lives in `member_edits."p{person}_{table}"`. After the
question's SQL is validated, the executor swaps each corrected table for a
derived table that `COALESCE`s the member's pending cells over the real ones —
so their own answers include their pending value, and nobody else's change.
Naming `member_edits` in a question is refused by the validator; the
substitution is the executor's, never a statement anybody supplied.

### External databases

The URL is **built from its parts** (`URL.create`), never pasted together, and
the connection goes to **the exact address `check_host` judged** (libpq
`hostaddr`), re-checked every time an engine is built. Credentials are sealed
with Fernet under a key derived from `SECRET_KEY` by HKDF, decrypted in one
place, and returned by no endpoint. An account that can write is refused at
registration.

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

### Sign-in

No passwords. A six-digit code, hashed with HMAC-SHA256 and salted with the
address, compared in constant time. Five wrong codes lock the address for
fifteen minutes; a new code cannot be requested within sixty seconds of the
last; and each address and each network has an hourly cap. Invitations are
single-use (a conditional `UPDATE`, so two racing redemptions cannot both
win), expire, are bound to the address they were sent to, and still need the
code from that inbox — a leaked link is not enough on its own.

### Found in review, and fixed

Each of these was a real defect in an earlier version of this branch. Each now
has a test that fails if it comes back.

| Defect | Consequence | Fix |
|---|---|---|
| One shared read engine | A question could run on another company's warehouse | Per-connection engines + `current_database()` check |
| API connected as `speakql_owner` | The API held superuser | `speakql_app`; config refuses a superuser DSN |
| DSN pasted from user input | A password like `x@127.0.0.1:5432/db?` moved the connection past the SSRF check | `URL.create` from parts |
| Host resolved twice | DNS rebinding could swap in a private address | Connect via the judged address (`hostaddr`) |
| Failure count rolled back with the refusal | The five-attempt lockout never fired | Count committed before refusing |
| PKs read from `information_schema` | Invisible to a SELECT-only role: zero editable tables | Read from `pg_catalog` |
| No `.dockerignore` | `COPY . .` baked `.env` secrets into the image | `.dockerignore` |
| `email-validator` missing | The container could not start | Pinned in `requirements.txt` |

---

## Testing

```bash
make test               # the whole suite, inside the api container
make test-privileges    # the Postgres assertions, against the live database
```

**218 tests.** 183 of them run anywhere, without a database:

| Suite | Covers |
|---|---|
| `test_validator.py` | 56 cases — every statement above, plus viewer restrictions, nesting, limits, and that it never raises on hostile input |
| `test_security.py` | SSRF, prompt-injection fencing, and the write path |
| `test_isolation.py` | Per-connection routing, the `current_database()` refusal, host pinning, the password-moves-the-host bug, credential sealing, the superuser refusal, the overlay rewrite |
| `test_pipeline.py` | Intent screening, asking back once, follow-up resolution, chart choice, the incomplete-data warning, and the explanation written without a model |
| `test_models.py` | The seams around the two models: the input format both training and serving must agree on, ranking and cutoff, the confidence the router routes on, and that a missing checkpoint degrades instead of failing |
| `test_api.py` | The real app end to end: sign-in, the lockout, cooldowns and caps, invitations (single-use, bound, expiring, racing), tenant isolation, token-kind confusion, layer 1 |

And **35 that only Postgres can answer**, run in the container — the privilege
assertions below, plus three suites that SQLite cannot host at all (the
overlay lives in a *schema*, uploads create schemas, and neither exists in
SQLite):

| Suite | Covers |
|---|---|
| `test_corrections.py` | An owner's correction lands in the real table; a member's does not; the same query returns the member's pending value to them and the real one to everyone else; approving writes it; a merge whose row moved is refused as stale |
| `test_ingestion.py` | Plan creates nothing; load creates, loads and reindexes in one step; commas and dd/mm/yyyy are coerced; another organisation sees neither the table nor its rows |
| `test_permissions.py` | The two-axis matrix: what a viewer may read, that a viewer and Readout never receive the SQL, that the persona header grants nothing, and that only an owner administers |

### The privilege assertions

| # | Assertion |
|---|---|
| 1 | The read role can actually read the warehouse |
| 2 | The read role holds no `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE` on **any** table |
| 3 | Read transactions are read-only at the server, not merely by convention |
| 4 | The write role cannot `DELETE` or `TRUNCATE` — a correction never removes |
| 5 | The overlay role holds **nothing** on `public` |
| 6 | No runtime role — `speakql_app` included — can create a database or a role, or bypass row security |
| + | The two tenants' warehouses give different answers to the same query |
| + | No warehouse role can log in to `speakql_meta` |
| + | `speakql_app` cannot connect to or read a warehouse |

**Two tenants, on purpose.** `bootstrap.sh` creates `northwind_dw` *and*
`harbor_dw` — isolation cannot be tested against one database, and Harbor's
names and amounts are changed so a cross-tenant read would be visible. The
seed is deliberately imperfect too: three rows in `shipments` have no `units`,
because the *incomplete* answer mode and the correction workflow need real
missing values. `21_seed.sql` raises if those gaps go missing.

---

## Project structure

Everything backend lives in this folder. The repository root holds only the
combined README and the git configuration.

```
backend/
├── app/        main · config · deps · rbac · object_access · ratelimit · mailer
├── api/        routes_ask · routes_org · routes_connections · routes_datasets
│               routes_merges · routes_misc · schemas.py (the API contract)
├── auth/       routes · otp · jwt_handler · invitations · domain_resolver · public_domains
├── core/       question_handler · context_resolver · schema_retriever
│               sql_generator · validator · edit_validator · router · executor
│               merge · visualiser · explainer · llm_client · file_ingest
├── db/         engines · tenant_engine · crypto · entities · session
│               introspect · edits_engine · host_guard
├── ml/         spider_prep · synth · serialize · warehouse
│               retriever_train · retriever_eval          ← Model A
│               generator_train · generator_eval          ← Model B
│               data/synth/ (committed) · checkpoints/ (not) · results/
├── logs/       query_log · edit_log · audit_log
├── sql/        00_roles · 10_meta · 20_warehouse · 21_seed · 22_second_tenant
├── scripts/    bootstrap.sh · seed_demo.py · train_all.sh · demo.py
├── tests/      test_validator · test_security · test_isolation · test_pipeline
│               test_models · test_api · test_corrections · test_ingestion
│               test_permissions · test_privileges
├── .dockerignore
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
  Every warehouse engine comes from `db/tenant_engine.py`, built from a
  connection row — there is no `engines.read`.
- Each of the three logs has **exactly one writer**.

> **Naming note.** Backend Plan §15 calls the log package `logging/`. That name
> shadows Python's standard library and breaks every `import logging` in the
> process, so it is **`logs/`** here. The document should be corrected to match.

---

## Roadmap

Build weeks 1–7 are the mid-term presentation; weeks 8–10 are the end-term.

| Phase | Deliverable | Status |
|:---:|---|:---:|
| 1 | Foundation — compose, config, per-connection engines, roles, privilege assertions (verified on Postgres 15) | ✅ |
| 2 | Synthetic pair generation — *the critical path* | ⏳ |
| 3 | Model A — MiniLM retriever + FAISS (lexical baseline in place) | ⏳ |
| 4 | Auth, invitations, grants, object access, API contract | ✅ |
| 5 | Model B — CodeT5 generator (Gemma answers until it lands) | ⏳ |
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
