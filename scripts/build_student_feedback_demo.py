"""Demo student-ratings generator — realistic per-school student feedback.

The weekly per-caterer QUALITY SUMMARY headlines STUDENT satisfaction per school,
so the demo needs believable student ratings (feedback source='student'). This
generator creates them deterministically and idempotently against the seeded order
week's order_lines (which carry the school via the enrolment), with:

  - VARIED per-school averages (a clear standout and a soft spot per multi-school
    caterer, and a strong vs a weaker caterer overall);
  - a MIX of free-text: a recurring, legitimate theme per school (repeated by
    several students — the signal) plus unique JUNK one-offs ("soft drinks pls" —
    the noise the summary's filter must drop), and many no-comment ratings.

DATA-only; sends nothing. Idempotent: clears existing source='student' feedback
first, then re-inserts. After applying, re-capture the seed so resets keep it:
  uv run python scripts/build_student_feedback_demo.py        # apply to live DB
  uv run python scripts/build_student_feedback_demo.py --dry  # show, write nothing
  uv run python scripts/reset_demo.py --capture               # persist into seed.sql
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from src.db.connection import get_conn

# Per-school demo profile: (target average student rating, recurring legit theme).
# Schools 2&3 -> caterer 2 (Terrific Noodles); 5&6 -> caterer 4 (Guzman y Gomez).
# Caterer 4 is the STRONG performer (4.8 / 4.5); caterer 2 the weaker (4.0 / 3.9).
_SCHOOL_PROFILE: dict[int, tuple[float, str]] = {
    1: (4.2, "A bit more sauce would be perfect"),   # Lakehouse @ Moreton Bay
    2: (4.0, "Portions felt a little small"),         # Terrific Noodles @ John Paul
    3: (3.9, "The curry was a bit mild"),             # Terrific Noodles @ MacGregor (soft spot)
    4: (4.6, "The sushi rolls were excellent"),       # Kenko Sushi @ Indooroopilly
    5: (4.8, "The burritos are amazing"),             # GYG @ Loreto (standout)
    6: (4.5, "Best guac around"),                     # GYG @ Cannon Hill
}

# Unique JUNK one-offs — each used at most once, so the recurring-theme filter drops
# them as noise (never surfaced in the summary).
_JUNK = [
    "soft drinks pls", "can we get fanta", "more chips please", "🔥🔥",
    "idk", "where's the dessert", "extra cheese??", "can i get a bigger fork",
    "play music next time", "fortnite", "more more more", "meh",
]

_MAX_PER_SCHOOL = 22          # cap ratings per school (realistic sample)
_RECURRING_N = 4             # how many students repeat the legit theme (>= filter threshold)
_JUNK_N = 3                  # how many unique junk one-offs per school
_WINDOW_START = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)  # within the review window


def _ratings_for(target: float, n: int) -> list[int]:
    """A deterministic length-``n`` rating list whose mean ≈ ``target`` (1–5).

    Splits between floor(target) and floor(target)+1 to hit the average, then nudges
    one rating down for a touch of realistic spread (kept away from the mean)."""
    base = int(target)
    hi = min(5, base + 1)
    n_hi = round((target - base) * n)
    ratings = [hi] * n_hi + [base] * (n - n_hi)
    return ratings[:n]


def _comments_for(school_id: int, n: int) -> list[str | None]:
    """Comment per rating: the school's recurring legit theme on the first few, a
    few unique junk one-offs next, the rest blank."""
    theme = _SCHOOL_PROFILE[school_id][1]
    out: list[str | None] = [None] * n
    for i in range(min(_RECURRING_N, n)):
        out[i] = theme
    for j in range(_JUNK_N):
        idx = _RECURRING_N + j
        if idx < n:
            # Deterministic, unique junk (vary by school so they never coincide).
            out[idx] = _JUNK[(school_id * 3 + j) % len(_JUNK)]
    return out


def _school_lines(cur) -> dict[int, list[tuple[int, int, int]]]:
    """{school_id: [(order_line_id, order_id, caterer_id) ...]} for the order week,
    ordered for deterministic sampling."""
    cur.execute(
        """
        SELECT e.school_id, ol.id, ol.order_id, o.caterer_id
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN enrolments e ON e.id = ol.enrolment_id
        ORDER BY e.school_id, ol.id
        """
    )
    by_school: dict[int, list[tuple[int, int, int]]] = {}
    for school_id, line_id, order_id, caterer_id in cur.fetchall():
        by_school.setdefault(school_id, []).append((line_id, order_id, caterer_id))
    return by_school


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate demo student ratings.")
    parser.add_argument("--dry", action="store_true", help="Show the plan; write nothing.")
    args = parser.parse_args(argv)

    rows_to_insert: list[tuple] = []
    summary: list[str] = []
    with get_conn() as conn, conn.cursor() as cur:
        by_school = _school_lines(cur)
        for school_id, (target, theme) in _SCHOOL_PROFILE.items():
            lines = by_school.get(school_id, [])
            n = min(_MAX_PER_SCHOOL, len(lines))
            if n == 0:
                continue
            ratings = _ratings_for(target, n)
            comments = _comments_for(school_id, n)
            actual_mean = sum(ratings) / n
            for i in range(n):
                line_id, order_id, caterer_id = lines[i]
                ts = _WINDOW_START + timedelta(days=(i % 16), hours=(i % 5))
                rows_to_insert.append(
                    ("student", line_id, order_id, caterer_id, ratings[i], comments[i], ts)
                )
            summary.append(
                f"  school {school_id} (caterer {lines[0][2]}): {n} ratings, "
                f"mean {actual_mean:.2f} (target {target}), recurring theme {theme!r} x{min(_RECURRING_N,n)}"
            )

        print(f"Student ratings to generate: {len(rows_to_insert)} across {len(summary)} school(s)")
        print("\n".join(summary))

        if args.dry:
            conn.rollback()
            print("\nDRY RUN — nothing written.")
            return 0

        cur.execute("DELETE FROM feedback WHERE source = 'student'")
        cur.executemany(
            """
            INSERT INTO feedback (source, order_line_id, order_id, caterer_id, rating, comment, submitted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            rows_to_insert,
        )
        conn.commit()
        print(f"\nApplied {len(rows_to_insert)} student rating(s) to the live DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
