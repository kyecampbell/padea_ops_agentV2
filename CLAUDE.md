# CLAUDE.md — Padea Operations Agent

Guidance for future Claude sessions working in this repo.

## What this is

An AI agent that runs end-to-end catering operations for a tutoring company
(6 schools, one caterer per school; students have dietary needs + meal
preferences). It replaces the human operator. See `README.md` for the full
overview.

## Architecture (keep the skeleton aligned with this)

- **One orchestrator agent** (Claude, `claude-sonnet-4-6`) woken by three
  triggers: inbound email (polled), the weekly Thursday batch (orders +
  finances), and tool-surfaced gaps.
- **Tool belt**: query, write, send-email, escalate-to-human, run-script,
  recall-past-cases. The agent reasons over **typed tool results**
  (`found / empty / ambiguous / conflict / unavailable / error`). Tools NEVER
  throw raw exceptions at the agent — `src/agent/dispatch.py` wraps every result
  as a typed outcome (`src/tools/results.py`).
- **Hard-rules gate** (`src/agent/gates.py`): autonomous vs. requires-approval.
  Always requires human approval: commercial emails, money changes, meal changes
  after an order is sent, adding a student, anything irreversible.
- **Casebook** (`src/tools/casebook.py`): operator feedback stored as
  retrievable cases (keyword + recency, **no vector DB**). The UI's comments /
  edits / approvals train it.
- **Decision-feed UI** (`ui/server.py`, Flask): operator sees decisions and
  comments/approves/edits.
- The **always-on handbook** is `config/handbook.md`; its core is loaded into
  context for every task.

## Conventions (non-negotiable)

- Python **3.12**. Package manager **uv**; venv at `.venv/`
  (`uv venv --python 3.12`).
- **Absolute imports** from project root: `from src.tools.query import ...`.
- **Money is integer cents, never floats.**
- **Timestamps are timezone-aware.**
- Postgres (Supabase) via **psycopg v3** (`psycopg[binary]`).
- LLM via the **Anthropic SDK**. Orchestrator: `claude-sonnet-4-6`. A cheap model
  only for lightweight classification.
- Email **demo mode** (`EMAIL_MODE=demo`, default): real sends are redirected to
  `DEMO_SINK_EMAIL` with a `[DEMO — Intended for: <real recipient>]` prefix.
  Redirection lives in `src/tools/email.py`. The inbound poll reads the REAL
  inbox and is never redirected.

## Layout

```
config/    settings.py (pydantic-settings), runtime_config.yaml, handbook.md
database/  schema/  seed/  migrations/   (SQL added later; MOQ becomes its own table)
src/agent/ loop.py  dispatch.py  gates.py  context.py
src/tools/ results.py  query.py  writes.py  email.py  escalate.py  run_script.py  casebook.py
src/db/    connection.py (psycopg v3)
src/gmail/ client.py
src/memory/cases/
ui/        server.py  templates/
scripts/   poll_inbox.py  run_thursday_batch.py
tests/
```

## Working style

- Build **spine-first**: thin end-to-end paths before breadth.
- Every module currently is a stub (module docstring + TODO). Implement logic
  only when the corresponding prompt asks for it.
- Writes are DATA only — no DDL in tools; schema changes go in
  `database/migrations/`.
