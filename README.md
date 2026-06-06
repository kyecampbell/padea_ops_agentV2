# Padea Operations Agent

An AI agent that runs end-to-end catering operations for a tutoring company —
6 schools, one caterer per school, students with dietary needs and meal
preferences. It replaces the human who does this job.

## Live deployment

- **Main site:** https://padeacatering.com
- **Operator cockpit:** https://padea-cockpit.onrender.com (the decision feed —
  approve / reject / comment; login required)

Email runs in **demo mode** in production: outbound mail is redirected to a demo
sink (`[DEMO — Intended for: <real recipient>]`), so nothing reaches real
recipients.

## Architecture

- **One orchestrator agent** (Claude, model `claude-sonnet-4-6`) woken by three
  triggers:
  1. an **inbound email** (polled),
  2. a weekly **Thursday batch** (orders + finances), and
  3. **tool-surfaced gaps** discovered while working.
- A **tool belt**: query (read), write (data), send-email, escalate-to-human,
  run-script, recall-past-cases. The agent reasons over **typed tool results**
  (`found / empty / ambiguous / conflict / unavailable / error`) — tools never
  throw raw exceptions at the agent. It escalates when unsure or when a rule
  requires a human.
- A **hard-rules gate** decides which actions are autonomous vs. need human
  approval. Always requires approval: commercial emails, money changes, meal
  changes after an order is sent, adding a student, anything irreversible.
- A **casebook memory**: operator feedback is stored as retrievable "cases"
  (keyword + recency, no vector DB) so the agent learns over time.
- A **lightweight local web UI** (Flask) shows a feed of the agent's decisions;
  the operator comments, approves, and edits — which trains the casebook.

## Conventions

- Python 3.12. Package manager: **uv**. Virtualenv at `.venv/`.
- Absolute imports from the project root (e.g. `from src.tools.query import ...`).
- **Money is always integer cents**, never floats.
- **Timestamps are timezone-aware.**
- Postgres (Supabase) via **psycopg v3** (`psycopg[binary]`).
- LLM via the **Anthropic SDK**: orchestrator `claude-sonnet-4-6`; a cheap model
  only for lightweight classification.
- Email sending defaults to **demo mode**: real sends are routed to a demo sink
  inbox with a `[DEMO — Intended for: <real recipient>]` prefix. The inbound
  poll reads the real inbox and is never redirected.

## Directory map

```
config/      Settings loader, tunable runtime config, and the policy handbook.
database/    SQL schema, seed data, and migrations (added later).
src/         Application source.
  agent/     Orchestrator: loop, tool dispatch, hard-rules gate, context.
  tools/     Tool belt: typed results, query, writes, email, escalate, scripts, casebook.
  db/        psycopg v3 connection helper.
  gmail/     Thin Gmail API client (send + inbox read).
  memory/    Stored resolution cases.
ui/          Flask companion server + templates (decision feed).
scripts/     Trigger runners: inbox poll, Thursday batch.
tests/       Test suite.
```

## Setup

```sh
uv venv --python 3.12          # creates .venv/
uv pip install -r requirements.txt
cp .env.example .env           # then fill in secrets
```

## Status

Scaffolding only — modules are stubs (docstring + TODO). Logic is built
spine-first in later iterations.
