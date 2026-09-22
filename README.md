<div align="center">

# SpeakQL

**A natural-language analytics copilot for a company's own warehouse.**

Ask a question in plain English. Get back a number, a chart, an explanation,
and the validated read-only SQL that produced it.

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
- [Team](#team)

---

## What it is

Enterprise decisions run on data held in relational warehouses, but querying
that data requires SQL — so business teams depend on a small pool of analysts
for routine reporting. Each ad-hoc request costs an analyst 2–4 hours and waits
1–2 days in a queue.

SpeakQL removes that queue for routine questions. A person types what they want
to know; the system finds the tables the question is about, writes the SQL,
refuses anything that is not a single read-only `SELECT`, runs it under a
database account that can only read, and answers with the number, a chart, a
plain-English explanation naming the columns it came from, and the SQL itself.

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
 routing ────────────── confidence ≥ 0.55 keep · else escalate and validate again
    │
    ▼
 execution ──────────── read-only role · read-only transaction · 10 s timeout
    │
    ▼
 answer · chart · explanation · SQL        and one query_log row, on every path
```

Two models are fine-tuned in-house:

| | Model | Job | Metric |
|---|---|---|---|
| **A** | MiniLM bi-encoder, 22M parameters | Find the tables a question is about | recall@10 |
| **B** | CodeT5-small, 60M parameters | Write the SQL | execution accuracy |

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

Each layer lives in its own branch and its own folder. The root of every branch
carries this README; each folder carries its own.

```
SpeakQL/
├── backend/            ← Backend branch    · API, database, safety layers, models
│   └── README.md
├── frontend/           ← Frontend branch   · the interface
│   └── README.md
├── .gitattributes
├── .gitignore
└── README.md           ← this file — the project overview for every branch
```

---

## Branches

| Branch | Contents | Its README |
|---|---|---|
| [`main`](https://github.com/garvbhargava/SpeakQL/tree/main) | This overview | — |
| [`Backend`](https://github.com/garvbhargava/SpeakQL/tree/Backend) | API, database layer, the four safety layers, model interfaces | [`backend/README.md`](https://github.com/garvbhargava/SpeakQL/blob/Backend/backend/README.md) |
| [`Frontend`](https://github.com/garvbhargava/SpeakQL/tree/Frontend) | The interface — every screen, both roles, all five response modes | `frontend/README.md` |
| [`Full-Stack`](https://github.com/garvbhargava/SpeakQL/tree/Full-Stack) | Backend and frontend wired together, one `docker compose up` | root and both folders |
| `documentation` | The four specification PDFs | — |

Each branch is **self-contained**: check out `Backend` and you have a complete,
runnable API with its own tests; check out `Frontend` and you have the complete
interface. `Full-Stack` is where they meet.

---

## Quick start

The backend runs today:

```bash
git clone -b Backend https://github.com/garvbhargava/SpeakQL.git
cd SpeakQL/backend
cp .env.example .env         # then set SECRET_KEY
make up                      # postgres + api
make bootstrap               # databases, five roles, two seeded warehouses
make test-privileges         # prove the read path cannot write
```

Then `http://localhost:8000/docs` for the API, or `http://localhost:8000/health`
for per-role status. Full instructions are in
[`backend/README.md`](https://github.com/garvbhargava/SpeakQL/blob/Backend/backend/README.md).

---

## Status

The build follows a ten-week plan in eight phases. Weeks 1–7 are the mid-term
presentation; weeks 8–10 are the end-term.

| Branch | State |
|---|---|
| `Backend` | ✅ **Weeks 1–7 complete** — twelve-step pipeline, four safety layers, 25 routes, 100 tests passing |
| `Frontend` | 🔨 Next — the interactive mockup, in dark by default |
| `Full-Stack` | ⏳ After the frontend is approved |
| `documentation` | ⏳ Added when the project is finished |

**Deferred to the end-term by design:** SSE streaming, voice input, export, the
evaluation harness and the ablation table — and CodeT5's training, which is on
the critical path and starts with synthetic pair generation.

---

## Documentation

Four specification documents, added to the `documentation` branch at the end:

| Document | What it covers |
|---|---|
| **Backend Plan** · Revision 5 | Architecture, database, safety layers, API contract, build order |
| **Complete Logic** | How every part of the system decides what it decides, with worked traces |
| **Frontend Specification** | The design system, every screen, and a 132-case test matrix |
| **Build Plan** | Weeks 1–7 serialised for one builder, with the critical path and cut order |

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
