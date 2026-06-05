"""Build the per-student session roster (enrolment_session_slots).

Assigns every ACTIVE (non-opted-out) student to session(s) at THEIR school:

  - a school with ONE session  -> that session;
  - a school with MULTIPLE sessions -> ~80% of students to exactly ONE session
    (spread evenly across that school's sessions), ~20% to TWO sessions.

The assignment is a PURE function of the enrolment id and the school's (sorted)
session-slot ids — see ``rostered_slots`` — so it is deterministic and
reproducible: re-running the generator, or replaying the captured ``seed.sql``,
yields the identical roster. Keyed on enrolment id, the ~20% two-session split is
``enrolment_id % 5 == 0`` (exactly one in five) and the single-session pick is
``enrolment_id % n`` (even across the school's n sessions).

Idempotent: clears enrolment_session_slots, then re-inserts the full roster.

Run: uv run python scripts/build_session_rosters.py
"""

from __future__ import annotations

import sys
from collections import defaultdict

from src.db.connection import get_conn

# One in TWO_SESSION_EVERY students (keyed on enrolment id) at a multi-session
# school is rostered to TWO sessions; the rest to one. 5 -> ~20%.
TWO_SESSION_EVERY = 5


def rostered_slots(enrolment_id: int, slot_ids: list[int]) -> list[int]:
    """The session slot(s) a student is rostered to at their school.

    ``slot_ids`` MUST be the school's active session-slot ids in ascending order.
    Pure and deterministic in ``(enrolment_id, slot_ids)``:

      - 1 session  -> [that session];
      - n sessions -> a single session ``slot_ids[enrolment_id % n]`` for ~80% of
        students; for the ~20% with ``enrolment_id % TWO_SESSION_EVERY == 0`` a
        second, distinct session is added at an id-derived offset, so the pairs
        are spread rather than always adjacent.

    Returns the slots ascending.
    """
    n = len(slot_ids)
    if n == 0:
        return []
    if n == 1:
        return [slot_ids[0]]

    primary = enrolment_id % n
    chosen = {slot_ids[primary]}
    if enrolment_id % TWO_SESSION_EVERY == 0:
        # offset in [1, n-1] => always distinct from primary, varied across ids.
        offset = 1 + (enrolment_id // n) % (n - 1)
        chosen.add(slot_ids[(primary + offset) % n])
    return sorted(chosen)


def _school_slots() -> dict[int, list[int]]:
    """{school_id: [active session_slot_id ascending]}."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT school_id, id FROM session_slots WHERE active = TRUE "
            "ORDER BY school_id, id"
        )
        slots: dict[int, list[int]] = defaultdict(list)
        for school_id, slot_id in cur.fetchall():
            slots[school_id].append(slot_id)
    return slots


def _active_enrolments() -> list[tuple[int, int]]:
    """[(enrolment_id, school_id)] for every non-opted-out student, id order."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, school_id FROM enrolments "
            "WHERE opted_out_of_catering = FALSE ORDER BY id"
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def main() -> int:
    school_slots = _school_slots()
    enrolments = _active_enrolments()

    rows: list[tuple[int, int]] = []
    two_session = 0
    per_session: dict[int, int] = defaultdict(int)
    for eid, school_id in enrolments:
        slots = rostered_slots(eid, school_slots.get(school_id, []))
        if len(slots) > 1:
            two_session += 1
        for slot in slots:
            rows.append((eid, slot))
            per_session[slot] += 1

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM enrolment_session_slots")
        cur.executemany(
            "INSERT INTO enrolment_session_slots (enrolment_id, session_slot_id) "
            "VALUES (%s, %s)",
            rows,
        )
        conn.commit()

    single = len(enrolments) - two_session
    print("=" * 60)
    print("SESSION ROSTER BUILD")
    print("=" * 60)
    print(f"Active students ........... {len(enrolments)}")
    print(f"  one session ............. {single}")
    print(f"  two sessions ............ {two_session}  "
          f"({two_session / len(enrolments):.0%})" if enrolments else "")
    print(f"Roster rows written ....... {len(rows)}")
    print("\nStudents per session:")
    for slot in sorted(per_session):
        print(f"  session {slot:>2} ............ {per_session[slot]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
