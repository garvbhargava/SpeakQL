<div align="center">

# SpeakQL

**A natural-language analytics copilot for a company's own warehouse.**

Ask a question in plain English. Get back a number, a chart, and an
explanation naming the columns it came from. The validated read-only SQL
behind it is logged for measurement, not shown.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![CodeT5](https://img.shields.io/badge/generator-CodeT5-E3A55C)](https://huggingface.co/Salesforce/codet5-small)
[![Gemma](https://img.shields.io/badge/LLM-gemma3%3A4b-4A9EFF)](https://ollama.com/library/gemma3)
[![Validator](https://img.shields.io/badge/validator-sqlglot-8A2BE2)](https://github.com/tobymao/sqlglot)

</div>

---

## Table of contents

- [What it is](#what-it-is)
- [How it works](#how-it-works)
- [What makes it safe](#what-makes-it-safe)
- [Repository layout](#repository-layout)
- [Branches](#branches)
- [Quick start](#quick-start)
- [Status](#status)
- [Documentation](#documentation)

---

## What it is

Enterprise decisions run on data held in relational warehouses, but querying
that data requires SQL — so business teams depend on a small pool of analysts
for routine reporting. Each ad-hoc request costs an analyst 2–4 hours and waits
1–2 days in a queue.

SpeakQL removes that queue for routine questions. A person types what they want
to know; the system finds the tables the question is about, writes the SQL,
refuses anything that is not a single read-only `SELECT`, runs it under a
database account that can only read, and answers with the number, a chart and
a plain-English explanation naming the columns it came from. The SQL is kept in
the query log, where accuracy is measured, and is not shown to the person
asking.

When it cannot answer, it says so. When a question is ambiguous, it asks back
once. When the data is short, it marks the missing cells and lets the right
person fill them where they stand.

---

## How it works

```
 question
    │
    ▼
 intent gatekeeper ──── credentials? destruction? injection? ──► refused, no SQL exists
    │
    ▼
 schema retrieval ───── MiniLM bi-encoder + FAISS ───────────► only the relevant tables
    │
    ▼
 SQL generation ─────── CodeT5 (fine-tuned) · Gemma fallback ► candidate + confidence
    │
    ▼
 AST validation ─────── sqlglot, never a model ──────────────► single read-only SELECT?
    │
    ▼
 routing ────────────── ≥ 0.63 keep · 0.47–0.63 ask back once · below: Gemma, validated again
    │
    ▼
 execution ──────────── read-only role · read-only transaction · 10 s timeout
    │
    ▼
 answer · chart · explanation              and one query_log row, on every path
```

Two models are fine-tuned in-house, on a CPU, and **measured rather than
described**:

| | Model | Job | Measured |
|---|---|---|---|
| **A** | MiniLM bi-encoder, 22M parameters | Find the tables a question is about | recall@3 **99.7%** on this warehouse, up from 38.5% off the shelf; 97.7% on Spider databases it has never seen |
| **B** | CodeT5-small, 60M parameters | Write the SQL | **55.5%** execution accuracy, 100% of its output accepted by the validator, 1.4 s a question |

Both are optional at runtime: with no checkpoint the pipeline retrieves
lexically and generates with Gemma, which is the off-the-shelf baseline the
comparison is measured against.

**Gemma** (`gemma3:4b`, open-weight, running locally under Ollama) is the
fallback generator below the confidence threshold, and writes the explanation.

> **Gemma generates and explains. sqlglot decides.**
> Whether a statement may run is decided by a deterministic parser, identically
> whether the SQL came from CodeT5, from Gemma, or from a person typing it by
> hand. A parser can be read and tested; a model can only be sampled.

---

## What makes it safe

**Four independent safety layers.** Any one of them refusing is enough.

| Layer | Where | Catches |
|:---:|---|---|
| 1 | Intent gatekeeper, **before** generation | Questions asking for credentials or destruction |
| 2 | AST validator, after generation | Anything that is not one read-only `SELECT` over permitted tables |
| 3 | The database role itself | Any write, even if both layers above were wrong |
| 4 | Edit validator, on the write path | A correction not scoped to one row by primary key |

**Tenant isolation is structural.** A company is identified by its email
domain. A free-mail signup gets a *personal workspace* whose domain is stored as
`NULL` — and a `NULL` never equals anything, so it can never be matched by
domain resolution. Two people on `gmail.com` get two separate tenants.

**Two axes of permission.** A *product role* (owner or member) says who you are
in your company. A *database role* (analyst or viewer) sits on each grant and
says what you may do with one database. The same person can be an analyst on
Sales and a viewer on Support.

**Corrections without contamination.** An owner's correction writes the real
table immediately. A member's goes into their own overlay: their answers use it
at once, nobody else's change, and the owner merges it. A merge whose row moved
in the meantime is refused rather than applied — that was a real data-loss bug,
caught and fixed.

---

## Repository layout

Each part lives in its own branch. `Backend` and `Frontend` keep their code at
the top of the branch, each with its own README. `Full-Stack` holds both, side
by side:

```
SpeakQL/                (the Full-Stack branch)
├── backend/            ← the Backend branch    · API, database, safety layers, models
│   ├── app · api · auth · core · db · logs · sql · ml · scripts · tests
│   └── README.md
├── frontend/           ← the Frontend branch   · the interface
│   ├── mockup.html         one file, no build, no server
│   ├── explorations/       the two passes before it
│   └── README.md
├── .gitattributes
├── .gitignore
└── README.md
```

---

## Branches

| Branch | Contents | Its README |
|---|---|---|
| [`main`](https://github.com/garvbhargava/SpeakQL/tree/main) | This overview | — |
| [`Backend`](https://github.com/garvbhargava/SpeakQL/tree/Backend) | API, database layer, the four safety layers, model interfaces | [`README.md`](https://github.com/garvbhargava/SpeakQL/blob/Backend/README.md) |
| [`Frontend`](https://github.com/garvbhargava/SpeakQL/tree/Frontend) | The interface — every screen, both roles, all five response modes | [`README.md`](https://github.com/garvbhargava/SpeakQL/blob/Frontend/README.md) |
| [`Full-Stack`](https://github.com/garvbhargava/SpeakQL/tree/Full-Stack) | Backend and frontend wired together, one `docker compose up` | root and both folders |
| [`Documents`](https://github.com/garvbhargava/SpeakQL/tree/Documents) | The specification PDFs; Backend Specification v6.1 is the one to build from | [Documentation](#documentation) |

Each branch is **self-contained**: check out `Backend` and you have a complete,
runnable API with its own tests; check out `Frontend` and you have the complete
interface. `Full-Stack` is where they meet.

---

## Quick start

**The interface needs nothing at all** — clone the Frontend branch and open one
file:

```bash
git clone -b Frontend https://github.com/garvbhargava/SpeakQL.git
start SpeakQL/mockup.html      # or `open` on macOS
```

**The backend runs today:**

```bash
git clone -b Backend https://github.com/garvbhargava/SpeakQL.git
cd SpeakQL
cp .env.example .env         # then set SECRET_KEY
make up                      # postgres + api
make bootstrap               # databases, roles, two tenants on two warehouses
make test-privileges         # prove the read path cannot write
make demo                    # ask it the demo questions, end to end
```

Then `http://localhost:8000/docs` for the API, or `http://localhost:8000/health`
for per-role status and which models are loaded. Full instructions are in
[the Backend README](https://github.com/garvbhargava/SpeakQL/blob/Backend/README.md).

Training the two models is one command and runs unattended in a container:

```bash
docker compose --profile train up -d trainer
```

---

## Status

The build follows a ten-week plan in eight phases. Weeks 1–7 are the mid-term
presentation; weeks 8–10 are the end-term.

| Branch | State |
|---|---|
| `Backend` | ✅ **Build weeks 1–7 complete, verified on Postgres 15** (built to Revision 5; what moves it to v6.1 is listed in §15.2 of the Backend Specification) — twelve-step pipeline, four safety layers, per-connection tenant engines, 24 routes, 218 tests, and both models trained in-house on CPU. Measured numbers in [the Backend README](https://github.com/garvbhargava/SpeakQL/blob/Backend/README.md#the-two-models) |
| `Frontend` | ✅ **The approved design, in one file** — eight screens, both roles, all five response modes, a 34-step walkthrough, dark by default. ⏳ The real interface, in React, Vite and Tailwind and connected to the API, is being built from it |
| `Full-Stack` | ⏳ Once the React interface is connected: the backend and the interface together, one `docker compose up` |
| `Documents` | ✅ **Backend Specification v6.1** (17 November 2026) and the documents aligned to it |

**Deferred to the end-term by design:** SSE streaming, voice input, export, the
evaluation harness, the ablation table and the confidence-threshold sweep.

### What the demo answers today

```
Which region had the highest total order amount?
  West · 482,140.00      codet5-small · confidence 1.0 · 1 ms
How many shipments have no units recorded?
  3                      codet5-small · confidence 0.998
What is the database password?
  refused at Layer 1 — the intent gatekeeper, before anything is generated
```

---

## Documentation

The `Documents` branch holds the specifications. **Build from Backend
Specification v6.1**; where any other document disagrees with it, v6.1 is
right.

| Document | What it covers | Status |
|---|---|---|
| **Backend Specification v6.1** | Architecture, roles, the 12-step pipeline, safety layers, corrections, the API contract (§10), the seeded warehouses (§11.4), tests and the demo script (§14), build order and the work list from the Backend branch (§15) | Source of truth, 17 November 2026. Its appendix records why each change from v6 was made and how it was tested |
| **Complete Logic** | How every part of the system decides what it decides, with six worked traces and the viva answers | Rewritten for v6.1 |
| **Frontend Specification** | The design system, every screen, and the test matrix | Opens with a v6.1 change list; every page it touches is marked |
| **Backend Plan** · Revision 5 | The design the Backend branch was built to | Superseded by v6.1, kept as a record |

Reading order: Complete Logic first, then the Backend Specification for
backend work or the Frontend Specification (change list first) for interface
work.

---

<div align="center">
<sub>Built for the MCA minor project, 2026–27.</sub>
</div>
