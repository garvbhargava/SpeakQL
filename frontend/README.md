<div align="center">

# SpeakQL — Frontend

**The interface: one console, two roles, and a walkthrough that proves the whole product without a server.**

[![HTML](https://img.shields.io/badge/HTML-single%20file-E34F26?logo=html5&logoColor=white)](mockup.html)
[![Theme](https://img.shields.io/badge/theme-dark%20first-0B0E14)](#the-design-system)
[![Tour](https://img.shields.io/badge/walkthrough-34%20steps-3FD68B)](#the-walkthrough)
[![Tokens](https://img.shields.io/badge/colours-tokens%20only-8A2BE2)](#the-design-system)

[`Backend`](https://github.com/garvbhargava/SpeakQL/tree/Backend) · `Frontend` · [`Full-Stack`](https://github.com/garvbhargava/SpeakQL/tree/Full-Stack) · [`main`](https://github.com/garvbhargava/SpeakQL/tree/main)

*This file is the Frontend branch's own README. The project overview for every
branch is [the README at the root](../README.md).*

</div>

---

## Table of contents

- [Overview](#overview)
- [Opening it](#opening-it)
- [What is in here](#what-is-in-here)
- [The screens](#the-screens)
- [The walkthrough](#the-walkthrough)
- [The design system](#the-design-system)
- [The sign-in screen](#the-sign-in-screen)
- [What is real and what is mocked](#what-is-real-and-what-is-mocked)
- [Roadmap](#roadmap)
- [Branches](#branches)
- [Team](#team)

---

## Overview

`mockup.html` is the interface, whole, in one file that opens by
double-clicking it. No build, no server, no dependencies — which is the point:
the interface had to be reviewable and demonstrable **before** the backend
existed, and it has to keep demonstrating when the laptop has no network.

It is not a slide deck of screenshots. Every screen is live: the tabs work,
the tour drives the real controls, the theme toggle re-themes the whole
console, and the refusals refuse.

> **The interface is where the product's promises are kept or broken.**
> A viewer must not be shown SQL. A member's pending correction must look
> different from a fact. A refusal must say which layer refused and why. Those
> are interface decisions as much as backend ones, and this file is where they
> were decided.

---

## Opening it

```bash
# Windows
start frontend\mockup.html

# macOS / Linux
open frontend/mockup.html
```

Then press **Play** on the walkthrough bar, or pick any section from it.

---

## What is in here

```
frontend/
├── mockup.html          the interface — one file, no dependencies
├── explorations/        earlier work, kept because it shows the reasoning
│   ├── design/          the first pass: five separate screens, split CSS and JS
│   │   ├── index.html · 01-signin · 02-otp · 03-workspaces
│   │   ├── 04-ask-empty · 05-ask-answer
│   │   ├── css/  tokens · base · components · ambient
│   │   └── js/   ui · mock · icons
│   └── designing.html   the second pass: one page, before the dark rebuild
└── README.md            ← you are here
```

`explorations/` is kept deliberately rather than deleted. The first pass split
the console across five files and a four-file stylesheet; the second pulled it
into one page; the third — `mockup.html` — rebuilt it dark-first on a single
token set. Keeping all three makes the design's reasoning checkable instead of
asserted.

---

## The screens

Eight, reachable from the console's own navigation:

| Screen | What it is for |
|---|---|
| **Ask** | The console: question, the twelve pipeline steps, answer, chart, explanation, the SQL |
| **Databases** | Registered databases and uploaded files, with the four registration checks reported one at a time |
| **People** | The two-axis grid — who holds which database role on which database |
| **Organisation** | The company, its domain, and the one question asked once at signup |
| **Merges** | The owner's queue of members' proposed corrections, including a stale one |
| **Fill** | The member's side: correcting a value that is missing or wrong |
| **Activity** | `query_log` made visible — answered, refused, blocked, failed |
| **Reach** | What this person can see, stated plainly rather than implied |

---

## The walkthrough

34 steps, in five sections, each one driving the real controls rather than
swapping a picture:

| Section | Steps | What it shows |
|---|---:|---|
| Signing up | 9 | The one question asked once; a work domain, a free-mail address, an invitation |
| Setting up | 7 | Registering a database, uploading a file, inviting people, granting roles |
| Asking | 7 | A question end to end, a clarification, and every refusal mode |
| Correcting | 6 | An owner writing the real table; a member proposing; the merge queue; a stale merge |
| Everything else | 5 | Activity, reach, the theme, and what the product refuses to do |

---

## The design system

**One token set, two grounds.** `:root` carries the dark theme;
`[data-theme="light"]` re-values *the same token names* for the porcelain one.
Nothing else in the stylesheet knows which ground it is on.

Five **derived tokens** exist for the values that must know:

| Token | Why it cannot be one value |
|---|---|
| `solid` / `on-solid` | A solid fill inverts differently on each ground |
| `ring` / `focus-edge` | Focus has to stay visible against both |
| `tint` / `tint2` / `tint-edge` | Tints wash out on porcelain and glow on dark |
| `bar-*` | The walkthrough bar stops inverting on dark and becomes a raised surface |
| `lift` / `glow` / `sweep` | Shadow reads as depth on light, as light on dark |

**The invariant: no rule names a colour.** The two token blocks are the only
place a hex appears. It is grep-checkable, and the check is part of the
Frontend Specification's test matrix (§22.9.1):

```bash
python - <<'PY'
import re
css = re.search(r'<style>(.*?)</style>',
                open('frontend/mockup.html', encoding='utf-8').read(), re.S).group(1)
blocks = [m.span() for m in re.finditer(r'(:root|\[data-theme="light"\])\s*\{.*?\}', css, re.S)]
stray = [m.group(0) for m in re.finditer(r'#[0-9a-fA-F]{3,8}\b', css)
         if not any(a <= m.start() < b for a, b in blocks)]
print(f"{len(stray)} colours outside the token blocks")   # must print 0
PY
```

It printed `0 colours outside the token blocks` when this README was written,
over 55 hex values. It caught about twenty rules the first time it was run.

Contrast and colour-vision figures for the palette were **measured, not
asserted** — WCAG 2.1 ratios, CIEDE2000 distances, and Machado 2009 at
severity 1.0 for the three common deficiencies. They live in §3.1 of the
Frontend Specification. Re-measure if the palette moves; do not retype them.

---

## The sign-in screen

Two columns. The left is the one field the product asks for. The right runs
the pipeline on a loop, by itself:

- the question types itself
- retrieval bars fill, with a cut line where the rest of the schema stops
- the SQL streams in token by token
- the confidence meter reads against the 0.55 threshold
- four validation layers tick in turn

Three runs cycle and **one of them is refused** — a `DELETE` caught at layer 2
at confidence 0.71. That pairing is the argument the whole project makes:
*confidence is not permission*. A person who watches the sign-in screen for
twenty seconds has seen what SpeakQL does and what it refuses to do, before
typing anything.

Below 1080px the panel is dropped rather than squashed, and reduced-motion
renders one finished run statically instead of animating.

---

## What is real and what is mocked

Honesty about a mockup matters more than polish, because the next branch wires
it to a server that will behave differently if this lies.

| | |
|---|---|
| **Real** | Every layout, state, transition, refusal, empty state and error state. The theme system. The twelve pipeline steps and their order. The response *shapes* — they were written against Backend Plan §12, which is the same contract the API implements |
| **Mocked** | The data. Answers, rows, charts, confidences and timings are fixtures chosen to match the seeded warehouse (West at $482,140; three shipments with no units) |
| **Absent** | Any network call. Opening this file makes none |

---

## Roadmap

| Phase | Deliverable | Status |
|:---:|---|:---:|
| 1 | Screen inventory, states, the one-question signup | ✅ |
| 2 | The console, both roles, all five answer modes | ✅ |
| 3 | Dark-first rebuild on one token set | ✅ |
| 4 | The animated sign-in and the 34-step walkthrough | ✅ |
| 5 | Wire it to the API — the `Full-Stack` branch | ⏳ next, after this is approved |

The wiring is deliberately a **different branch**: a mockup that fetches is no
longer a mockup, and this one has to keep working with no server for the
demonstration.

---

## Branches

| Branch | Contents |
|---|---|
| [`main`](https://github.com/garvbhargava/SpeakQL/tree/main) | Combined README for the whole project |
| [`Backend`](https://github.com/garvbhargava/SpeakQL/tree/Backend) | API, database, the four safety layers, both models |
| [`Frontend`](https://github.com/garvbhargava/SpeakQL/tree/Frontend) | **This branch** — the interface |
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
