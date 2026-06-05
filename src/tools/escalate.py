"""Escalate-to-human tool.

Responsibility: hand an incident to a human operator when the agent is unsure or
when a rule (handbook / hard-rules gate) requires human judgment. Records the
question, the relevant context, and any related ids as an OPEN ``escalations``
row so the operator can decide quickly, and so it surfaces in the decision feed.

Returns a typed result from `results.py` and NEVER raises at the agent.

Conventions:
  - Timestamps are timezone-aware (DB ``now()`` default on ``created_at``).
  - All SQL is parameterised — never string-built.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.db.connection import get_conn
from src.tools.results import ToolResult, error, found, unavailable

logger = logging.getLogger(__name__)


def _transaction(describe: str, work: Callable[[psycopg.Cursor], Any]) -> Any | ToolResult:
    """Run ``work(cur)`` in one committed transaction; translate failures to a
    typed ``unavailable`` / ``error`` result (rolled back on exit)."""
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            result = work(cur)
            conn.commit()
            return result
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


# Default look-back for caterer-level escalation de-duplication. A caterer concern
# that is still OPEN from a recent run is the same standing thread — append the new
# evidence rather than piling up duplicate rows the operator has to triage.
_CATERER_DEDUPE_WINDOW_DAYS = 14


def escalate_to_human(
    question: str,
    context: dict | None = None,
    related_run_id: int | None = None,
    related_caterer_id: int | None = None,
    related_enrolment_id: int | None = None,
    related_order_id: int | None = None,
    related_step_id: int | None = None,
    dedupe_window_days: int = _CATERER_DEDUPE_WINDOW_DAYS,
) -> ToolResult:
    """Raise an OPEN escalation for the operator and return its id.

    ``question`` is what the agent needs decided; ``context`` is any structured
    detail to help the human decide (stored as jsonb). The escalation starts with
    status ``open``; resolution is a separate ``requires_approval`` action
    (``writes.resolve_escalation``).

    De-duplication (keyed on the caterer): whenever a ``related_caterer_id`` is given
    and a recent OPEN *caterer-level* escalation already exists for that caterer (a
    caterer-wide row — one with NO ``related_enrolment_id``) within the last
    ``dedupe_window_days`` days, we do NOT create a duplicate. Instead we APPEND this
    question + context to that escalation's ``context.follow_ups`` and return its id
    (``appended: True``). So a second pass on the same caterer — even one triggered by
    a specific student's complaint — folds into the one open thread.

    What is NOT collapsed: a genuine per-student escalation when that caterer has NO
    open caterer-level thread (it stands alone), and the batch's own directly-inserted
    escalations (a different code path). Pass ``dedupe_window_days=0`` to force a fresh
    row.
    """
    if not (question or "").strip():
        return error("question is required.")

    def work(cur: psycopg.Cursor) -> dict:
        # --- De-dup on the caterer: append to a recent OPEN caterer-level thread. ---
        # Triggered for ANY escalation that names a caterer (even one citing a specific
        # student), but only ever appends to a caterer-WIDE open row, so standalone
        # per-student escalations are never merged into each other.
        if related_caterer_id is not None and dedupe_window_days > 0:
            cur.execute(
                """
                SELECT id, context
                FROM escalations
                WHERE status = 'open'
                  AND related_caterer_id = %s
                  AND related_enrolment_id IS NULL
                  AND created_at >= now() - make_interval(days => %s)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (related_caterer_id, dedupe_window_days),
            )
            existing = cur.fetchone()
            if existing is not None:
                merged = dict(existing["context"] or {})
                follow_ups = list(merged.get("follow_ups") or [])
                follow_ups.append(
                    {"question": question.strip(), "context": context, "run_id": related_run_id}
                )
                merged["follow_ups"] = follow_ups
                cur.execute(
                    "UPDATE escalations SET context = %s WHERE id = %s "
                    "RETURNING id, status, created_at",
                    (Jsonb(merged), existing["id"]),
                )
                row = cur.fetchone()
                row["_appended"] = True
                return row

        cur.execute(
            """
            INSERT INTO escalations
                (run_id, question, context, status,
                 related_caterer_id, related_enrolment_id, related_order_id, related_step_id)
            VALUES (%s, %s, %s, 'open', %s, %s, %s, %s)
            RETURNING id, status, created_at
            """,
            (
                related_run_id,
                question.strip(),
                Jsonb(context) if context is not None else None,
                related_caterer_id,
                related_enrolment_id,
                related_order_id,
                related_step_id,
            ),
        )
        row = cur.fetchone()
        row["_appended"] = False
        return row

    row = _transaction("creating escalation", work)
    if isinstance(row, ToolResult):
        return row

    if row["_appended"]:
        logger.info(
            "escalate_to_human: appended to existing OPEN escalation %s (caterer %s) — %r",
            row["id"], related_caterer_id, question,
        )
        return found(
            {
                "escalation_id": row["id"],
                "status": row["status"],
                "appended": True,
                "duplicate_avoided": True,
            },
            f"An open escalation ({row['id']}) already exists for this caterer; appended "
            "your evidence to it instead of creating a duplicate. Treat it as the live thread.",
        )

    logger.info("escalate_to_human: opened escalation %s — %r", row["id"], question)
    return found(
        {
            "escalation_id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "appended": False,
        },
        f"Opened escalation {row['id']} for the operator.",
    )
