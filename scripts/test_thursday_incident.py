"""Safe, repeatable test of the DETERMINISTIC Thursday flow — NO live sends.

Exercises the whole batch logic through the PLAN / dry-run + DB-write paths only
(it never calls ``send_caterer_orders`` or ``send_prefs_requests``, so no email is
ever sent). It resets to the golden seed, then asserts:

  PHASE 1 — caterer orders: exactly ONE order email per caterer would send, and an
            already-sent caterer is idempotently skipped (synthetic sent row).

  PHASE 2 — prefs request + flexible resolution (Henry Hill, the seed's lone
            defaulted, dietary 'No requirements' = KNOWN):
              (a) first time -> plan_prefs = first_ask;
              (b) after a prior prefs-request exists -> plan_prefs = flexible;
              (c) apply_flexible + re-compose -> Henry is no longer defaulted (his
                  line becomes a normal rotation pick), and a prefs request would
                  never be re-sent (idempotent).

  PHASE 3 — an UNKNOWN-dietary student is escalated with no line, is NEVER defaulted,
            and so is NEVER classified flexible/first_ask — they wait for a human.

Synthetic ``outbound_emails`` rows are inserted directly (no Gmail) to simulate
prior sends; injected rows are cleaned up. Run repeatedly without risk.

Run: uv run python scripts/test_thursday_incident.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

# reset_demo is a sibling script; ensure scripts/ is importable however launched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reset_demo  # noqa: E402

from src.db.connection import fetch_all, get_conn  # noqa: E402
from src.tools import order_email, orders_batch, parent_prefs  # noqa: E402

_HENRY_ENROLMENT_ID = 1          # seed's lone defaulted student (dietary 'No requirements')
_HENRY_CATERER_ID = 1
_TARGET_SCHOOL_ID = 4            # Indooroopilly / Kenko Sushi (caterer 3), clean
_UNKNOWN_NAME = "Uri Unknown (TEST)"

_passes = 0
_fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _passes, _fails
    if ok:
        _passes += 1
    else:
        _fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


# --- helpers -----------------------------------------------------------------


def _open_run() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs (trigger_reason) VALUES ('test_thursday') RETURNING id")
        rid = cur.fetchone()[0]
        conn.commit()
    return int(rid)


def _compose(week: date, run_id: int) -> None:
    res = orders_batch.compose_week(week, run_id=run_id)
    assert res.ok, f"compose failed: {res.message}"


def _order_line(enrolment_id: int, week: date) -> dict | None:
    dates = [week + timedelta(days=d) for d in range(7)]
    rows = fetch_all(
        """
        SELECT mi.name AS item, ol.source
        FROM order_lines ol JOIN orders o ON o.id = ol.order_id
        JOIN menu_items mi ON mi.id = ol.menu_item_id
        WHERE ol.enrolment_id = %s AND o.session_date = ANY(%s) LIMIT 1
        """,
        (enrolment_id, dates),
    )
    return {"item": rows[0][0], "source": rows[0][1]} if rows else None


def _insert_synthetic_email(email_type: str, subject: str, *, caterer_id=None, enrolment_id=None, to="x@test.example") -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outbound_emails
                (email_type, status, intended_to_address, subject, rendered_body,
                 sent_at, related_caterer_id, related_enrolment_id)
            VALUES (%s, 'sent', %s, %s, '(synthetic prior send)', now(), %s, %s)
            RETURNING id
            """,
            (email_type, to, subject, caterer_id, enrolment_id),
        )
        eid = cur.fetchone()[0]
        conn.commit()
    return int(eid)


def _delete_emails(ids: list[int]) -> None:
    if ids:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM outbound_emails WHERE id = ANY(%s)", (ids,))
            conn.commit()


def _inject_unknown_student() -> int:
    """An active student with BLANK dietary and NO eligible pool -> escalated."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO enrolments
                (school_id, student_name, student_year_level, parent_name, parent_email,
                 original_start_date, current_period_start_date, opted_out_of_catering, dietary_raw)
            VALUES (%s, %s, 9, %s, %s, %s, %s, FALSE, NULL)
            RETURNING id
            """,
            (_TARGET_SCHOOL_ID, _UNKNOWN_NAME, f"Parent of {_UNKNOWN_NAME}",
             "parent.unknown@test.example", date(2026, 1, 1), date(2026, 1, 1)),
        )
        eid = cur.fetchone()[0]
        conn.commit()
    return int(eid)


def _cleanup_enrolment(enrolment_id: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM outbound_emails WHERE related_enrolment_id = %s", (enrolment_id,))
        cur.execute("DELETE FROM order_lines WHERE enrolment_id = %s", (enrolment_id,))
        cur.execute("DELETE FROM escalations WHERE related_enrolment_id = %s", (enrolment_id,))
        cur.execute("DELETE FROM student_eligible_meals WHERE enrolment_id = %s", (enrolment_id,))
        cur.execute(
            "DELETE FROM term_meal_preference_items WHERE preference_id IN "
            "(SELECT id FROM term_meal_preferences WHERE enrolment_id = %s)", (enrolment_id,))
        cur.execute("DELETE FROM term_meal_preferences WHERE enrolment_id = %s", (enrolment_id,))
        cur.execute("DELETE FROM enrolments WHERE id = %s", (enrolment_id,))
        conn.commit()


def _action_for(actions, enrolment_id: int):
    for a in actions:
        if a.student.enrolment_id == enrolment_id:
            return a.action
    return None


# --- phases ------------------------------------------------------------------


def phase_caterer_orders(week: date) -> None:
    print("\n=== PHASE 1 — caterer orders: one per caterer + idempotent skip ===")
    plans = order_email.plan_caterer_orders(week)
    assert isinstance(plans, list), plans
    would = [p for p in plans if p.would_send]
    check("one order email per composed caterer (4 caterers, 4 would-send)",
          len(plans) == 4 and len(would) == 4,
          f"{len(plans)} caterers, {len(would)} would-send")
    check("every caterer email is distinct (≤1 per caterer)",
          len({p.draft.caterer_id for p in would}) == len(would))

    # Idempotency: a caterer already sent for the week is skipped.
    subj = order_email.week_subject("Lakehouse Victoria Point", week)
    fake = _insert_synthetic_email("session_order", subj, caterer_id=_HENRY_CATERER_ID)
    plans2 = order_email.plan_caterer_orders(week)
    would2 = [p for p in plans2 if p.would_send]
    c1 = next(p for p in plans2 if p.draft.caterer_id == _HENRY_CATERER_ID)
    check("already-sent caterer is idempotently skipped", c1.already_sent and not c1.would_send,
          f"already_sent={c1.already_sent} would_send={c1.would_send}")
    check("the other 3 caterers still would-send (no duplicates)", len(would2) == 3,
          f"{len(would2)} would-send after synthetic prior send")
    _delete_emails([fake])


def phase_prefs_and_flexible(week: date, run_id: int) -> None:
    print("\n=== PHASE 2 — prefs request + flexible resolution (Henry Hill) ===")
    # (a) first ask.
    actions = parent_prefs.plan_prefs(week)
    assert isinstance(actions, list), actions
    check("(a) Henry is a first-ask defaulted student (dietary known, not yet asked)",
          _action_for(actions, _HENRY_ENROLMENT_ID) == parent_prefs.ACTION_FIRST_ASK,
          f"action={_action_for(actions, _HENRY_ENROLMENT_ID)}")

    # (b) simulate a prior prefs-request -> non-responder -> flexible.
    subj, _ = parent_prefs.render_prefs_request("Henry Hill", "Moreton Bay Boys' College", "x")
    fake = _insert_synthetic_email("parent_prefs_request", subj, enrolment_id=_HENRY_ENROLMENT_ID)
    actions_b = parent_prefs.plan_prefs(week)
    check("(b) with a prior prefs-request, Henry classifies as FLEXIBLE",
          _action_for(actions_b, _HENRY_ENROLMENT_ID) == parent_prefs.ACTION_FLEXIBLE,
          f"action={_action_for(actions_b, _HENRY_ENROLMENT_ID)}")

    # (c) apply flexible + re-compose -> Henry no longer defaulted.
    res = parent_prefs.apply_flexible(_HENRY_ENROLMENT_ID)
    check("(c) apply_flexible set a preference over all eligible meals", res.ok, res.message)
    _compose(week, run_id)
    line = _order_line(_HENRY_ENROLMENT_ID, week)
    check("(c) Henry now has a normal line (not defaulted)",
          line is not None and line["source"] == "rotation",
          f"line={line}")
    actions_c = parent_prefs.plan_prefs(week)
    check("(c) Henry is no longer in the defaulted/prefs set",
          _action_for(actions_c, _HENRY_ENROLMENT_ID) is None,
          f"action={_action_for(actions_c, _HENRY_ENROLMENT_ID)}")
    check("(c) prefs request would never re-send (idempotent)",
          parent_prefs.prefs_request_exists(_HENRY_ENROLMENT_ID) is True)
    _delete_emails([fake])


def phase_unknown_dietary(week: date, run_id: int) -> None:
    print("\n=== PHASE 3 — unknown-dietary student: escalated, NEVER flexible ===")
    eid = _inject_unknown_student()
    try:
        _compose(week, run_id)
        line = _order_line(eid, week)
        check("unknown-dietary student got NO line", line is None, f"line={line}")
        esc = fetch_all(
            "SELECT context->>'reason' FROM escalations WHERE related_enrolment_id=%s AND status='open' ORDER BY id DESC LIMIT 1",
            (eid,),
        )
        check("unknown-dietary student escalated (dietary_unconfirmed)",
              bool(esc) and esc[0][0] == "dietary_unconfirmed", f"esc={esc}")
        actions = parent_prefs.plan_prefs(week)
        check("unknown-dietary student NEVER classified flexible/first_ask",
              _action_for(actions, eid) is None, f"action={_action_for(actions, eid)}")
    finally:
        _cleanup_enrolment(eid)


def main() -> int:
    print("Resetting to the golden seed (safe, repeatable; NO emails are sent)...")
    reset_demo.restore()
    week = orders_batch.upcoming_monday(date.today())
    run_id = _open_run()
    _compose(week, run_id)

    phase_caterer_orders(week)
    phase_prefs_and_flexible(week, run_id)
    phase_unknown_dietary(week, run_id)

    print(f"\n{_passes} passed, {_fails} failed.  (no emails were sent)")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
