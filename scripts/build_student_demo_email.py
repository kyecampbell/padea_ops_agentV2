"""Demo student-email generator — wire up the weekly choose-and-rate for a demo.

The weekly student CHOOSE-AND-RATE email goes to ``enrolments.student_email``. For
a clean, low-noise demo we want EXACTLY ONE active student per active session to
receive it (so exactly one choice email per session lands at the demo sink), and
everyone else BLANK — a blank-email student is never emailed and simply falls back
to the normal compose-time assignment.

So this generator, idempotently and deterministically:
  - BLANKS every enrolment's student_email, then
  - for each ACTIVE session_slot, sets the student_email of ONE active student
    rostered to it — the lowest enrolment id who has a non-empty dietary-safe pool
    (so they actually have options to pick) — to DEMO_SINK_EMAIL.

In EMAIL_MODE=demo every send is redirected to the sink anyway; setting the stored
address to the sink makes the "[DEMO — Intended for: <sink>]" banner read sensibly
and lets the demo operator reply from that mailbox. DATA-only; sends nothing.

Usage:
  uv run python scripts/build_student_demo_email.py          # apply to the live DB
  uv run python scripts/build_student_demo_email.py --dry    # show, write nothing

After applying, re-capture the demo seed so resets keep it:
  uv run python scripts/reset_demo.py --capture
"""

from __future__ import annotations

import argparse
import sys

from config.settings import settings
from src.db.connection import get_conn

# The student contact the demo routes choice emails to. Falls back to the literal
# demo sink if DEMO_SINK_EMAIL is unset, so the generator works out of the box.
_SINK = settings.demo_sink_email or "padea.demo@outlook.com"


def _chosen_enrolment_ids(cur) -> list[int]:
    """One active student id per active session_slot — the lowest enrolment id
    rostered to it that has a non-empty dietary-safe pool (so they have options).
    Deduped + sorted (a student lowest in two slots is chosen once)."""
    cur.execute("SELECT id FROM session_slots WHERE active = TRUE ORDER BY id")
    slot_ids = [r[0] for r in cur.fetchall()]
    chosen: set[int] = set()
    for slot_id in slot_ids:
        cur.execute(
            """
            SELECT e.id
            FROM enrolments e
            JOIN enrolment_session_slots ess
                 ON ess.enrolment_id = e.id AND ess.session_slot_id = %s
            WHERE e.opted_out_of_catering = FALSE
              AND e.current_period_start_date <= CURRENT_DATE
              AND (e.current_period_end_date IS NULL OR e.current_period_end_date >= CURRENT_DATE)
              AND EXISTS (
                  SELECT 1 FROM student_eligible_meals sem
                  WHERE sem.enrolment_id = e.id AND sem.eligible = TRUE)
            ORDER BY e.id
            LIMIT 1
            """,
            (slot_id,),
        )
        row = cur.fetchone()
        if row:
            chosen.add(row[0])
    return sorted(chosen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set the demo student choose-and-rate addresses.")
    parser.add_argument("--dry", action="store_true", help="Show the plan; write nothing.")
    args = parser.parse_args(argv)

    with get_conn() as conn, conn.cursor() as cur:
        chosen = _chosen_enrolment_ids(cur)
        cur.execute("SELECT count(*) FROM enrolments")
        total = cur.fetchone()[0]

        print(f"Demo sink: {_SINK}")
        print(f"Active session_slots covered -> {len(chosen)} student(s) emailed; "
              f"{total - len(chosen)} blank.")
        cur.execute(
            "SELECT id, student_name FROM enrolments WHERE id = ANY(%s) ORDER BY id",
            (chosen,),
        )
        for rid, name in cur.fetchall():
            print(f"    #{rid}: {name}  ->  {_SINK}")

        if args.dry:
            conn.rollback()
            print("\nDRY RUN — nothing written.")
            return 0

        cur.execute("UPDATE enrolments SET student_email = NULL")
        if chosen:
            cur.execute(
                "UPDATE enrolments SET student_email = %s WHERE id = ANY(%s)",
                (_SINK, chosen),
            )
        conn.commit()
        print(f"\nApplied: {len(chosen)} student(s) set to the demo sink, the rest blank.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
