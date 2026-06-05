"""Order-state tool — the "money line".

Responsibility: answer one question the gate keys on — has a *binding* order that
covers a given student's session already been sent? A meal or dietary change is
cheap to make right up until the order goes to the caterer; after that it costs
money (and possibly a wasted meal), so the gate flips such changes from
``autonomous`` to ``requires_approval``. This module computes that fact; it never
decides policy.

An order is considered SENT (covering the student's session) when either:
  - a per-session ``orders`` row with ``sent_at`` IS NOT NULL has an
    ``order_lines`` row for this enrolment (optionally narrowed to one
    ``session_date``); or
  - a ``caterer_week_orders`` row exists for the student's current caterer for
    the ISO week of the given session (the Thursday batch's consolidated grain).
    This branch needs a ``session_date`` to know which week to look at, so it is
    skipped when ``session_date`` is None — ``orders`` is the per-student
    authority in that case.

Conventions: read-only (SELECT only); parameterised SQL; timezone-aware
timestamps; never raises at the caller — DB failures come back as typed
``ToolResult``s (``unavailable`` / ``error``).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.results import ToolResult, error, found, unavailable


def _read(describe: str, sql: str, params: Sequence[Any] | None = None) -> list[dict] | ToolResult:
    """Run a SELECT, translating any psycopg failure into a typed result.

    Returns the rows on success, or an ``unavailable`` / ``error`` ToolResult the
    caller should return as-is. Mirrors the translation in ``src.tools.query``.
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


def _failed(rows: list[dict] | ToolResult) -> bool:
    """True when ``_read`` returned a failure result rather than rows."""
    return isinstance(rows, ToolResult)


def has_order_been_sent(enrolment_id: int, session_date: date | None = None) -> ToolResult:
    """Has a binding order covering this student's session already been sent?

    Returns ``found`` with a payload::

        {"order_sent": bool,
         "via": "orders" | "caterer_week_orders" | None,
         "order_ids": [...],            # sent per-session orders that include them
         "caterer_week_order_ids": [...]}  # weekly batch rows for their caterer/week

    ``order_sent`` is True if EITHER source covers the session. On a DB failure
    the read's ``unavailable`` / ``error`` result is returned unchanged — the
    gate treats an undetermined order state as fail-safe (requires approval).
    """
    # --- 1. Sent per-session orders that include this student. -----------------
    order_sql = """
        SELECT o.id
        FROM orders o
        JOIN order_lines ol ON ol.order_id = o.id
        WHERE ol.enrolment_id = %s
          AND o.sent_at IS NOT NULL
    """
    order_params: list[Any] = [enrolment_id]
    if session_date is not None:
        order_sql += " AND o.session_date = %s"
        order_params.append(session_date)
    order_sql += " ORDER BY o.id"

    order_rows = _read(
        f"checking sent orders for enrolment {enrolment_id}", order_sql, order_params
    )
    if _failed(order_rows):
        return order_rows
    order_ids = [r["id"] for r in order_rows]

    # --- 2. Weekly consolidated batch for the student's caterer & week. --------
    # Only meaningful with a session_date (it tells us which week to match).
    week_order_ids: list[int] = []
    if session_date is not None:
        week_rows = _read(
            f"checking weekly batch for enrolment {enrolment_id}",
            """
            SELECT cwo.id
            FROM caterer_week_orders cwo
            JOIN enrolments e ON e.id = %s
            JOIN schools s ON s.id = e.school_id
            WHERE cwo.caterer_id = s.current_caterer_id
              AND date_trunc('week', cwo.week_of) = date_trunc('week', %s::date)
            ORDER BY cwo.id
            """,
            (enrolment_id, session_date),
        )
        if _failed(week_rows):
            return week_rows
        week_order_ids = [r["id"] for r in week_rows]

    order_sent = bool(order_ids) or bool(week_order_ids)
    via = "orders" if order_ids else ("caterer_week_orders" if week_order_ids else None)

    scope = f" for session {session_date}" if session_date is not None else ""
    if order_sent:
        message = f"A sent order covers enrolment {enrolment_id}{scope} (via {via})."
    else:
        message = f"No sent order covers enrolment {enrolment_id}{scope}."

    return found(
        {
            "order_sent": order_sent,
            "via": via,
            "order_ids": order_ids,
            "caterer_week_order_ids": week_order_ids,
        },
        message,
    )
