"""Database smoke test — proves the app can read the seeded Supabase DB.

Connects via src.db.connection, runs a fixed set of row counts against the
business seed, prints them as a table, and asserts each matches the expected
value. Exits non-zero on any mismatch.

Run: uv run python scripts/db_smoke_test.py
"""

from __future__ import annotations

import sys

from src.db.connection import fetch_all

# (table, expected count) — the known shape of the loaded business seed.
EXPECTED: list[tuple[str, int]] = [
    ("enrolments", 320),
    ("feedback", 378),
    ("caterer_moq_tier", 36),
    ("menu_items", 40),
    ("term_meal_preference_items", 1536),
    ("dietary_tags", 11),
    ("enrolment_dietary_tags", 61),
]


def main() -> int:
    rows: list[tuple[str, int, int, bool]] = []
    all_ok = True

    for table, expected in EXPECTED:
        (actual,) = fetch_all(f"SELECT count(*) FROM {table}")[0]
        ok = actual == expected
        all_ok = all_ok and ok
        rows.append((table, expected, actual, ok))

    name_w = max(len(t) for t, *_ in rows)
    print(f"{'table':<{name_w}}  {'expected':>8}  {'actual':>8}  result")
    print(f"{'-' * name_w}  {'-' * 8}  {'-' * 8}  ------")
    for table, expected, actual, ok in rows:
        print(f"{table:<{name_w}}  {expected:>8}  {actual:>8}  {'OK' if ok else 'MISMATCH'}")

    print()
    if all_ok:
        print("All counts matched. ✅")
        return 0
    print("One or more counts did not match. ❌", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
