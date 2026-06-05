"""Demo address generator — derive realistic, role-based .example addresses.

Every stored email in the demo dataset is a placeholder (parent + tutor rows all
share one relay inbox; caterers use ad-hoc slugs). In EMAIL_MODE=demo every
outbound send is redirected to DEMO_SINK_EMAIL with a banner that names the REAL
intended recipient — so the stored address is what the demo audience sees. This
generator rewrites those stored addresses into believable role-based ones so the
"[DEMO — Intended for: <recipient>]" banner reads realistically.

Derivation (lowercase; non-alphanumerics stripped):
  - enrolments.parent_email -> "<student_first>.<student_last>.parent@example.com"
    (Henry Hill            -> henry.hill.parent@example.com)
  - caterers.contact_email  -> "<caterer_slug>.caterer@example.com"
    (Terrific Noodles       -> terrificnoodles.caterer@example.com)
  - tutors.email            -> "<tutor_name_slug>.tutor@example.com"
    (Jessie                 -> jessie.tutor@example.com)

parent_email is keyed off the STUDENT name (parent_email represents the family;
there is no separate student-email field). The derivation is pure and idempotent:
it is computed from the name each run, so re-running — or replaying after a
seed re-capture — yields the same addresses. DATA-only; sends nothing.

Usage:
  uv run python scripts/build_demo_addresses.py          # apply to the live DB
  uv run python scripts/build_demo_addresses.py --dry    # show changes, write none
"""

from __future__ import annotations

import argparse
import re
import sys

from src.db.connection import get_conn

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(token: str) -> str:
    """Lowercase a token and drop every non-alphanumeric character."""
    return _NON_ALNUM.sub("", token.lower())


def parent_email(student_name: str) -> str:
    """"<first>.<last>.parent@example.com" from a student's full name."""
    parts = [_slug(p) for p in student_name.split() if _slug(p)]
    local = ".".join([parts[0], parts[-1]]) if len(parts) >= 2 else parts[0]
    return f"{local}.parent@example.com"


def caterer_email(name: str) -> str:
    """"<slug>.caterer@example.com" — the whole name slugged (spaces removed)."""
    return f"{_slug(name)}.caterer@example.com"


def tutor_email(name: str) -> str:
    """"<first>.<last>.tutor@example.com"; single-token names slug to one part."""
    parts = [_slug(p) for p in name.split() if _slug(p)]
    local = ".".join([parts[0], parts[-1]]) if len(parts) >= 2 else parts[0]
    return f"{local}.tutor@example.com"


# (table, name-source column, email column, derive fn)
_TARGETS = (
    ("enrolments", "student_name", "parent_email", parent_email),
    ("caterers", "name", "contact_email", caterer_email),
    ("tutors", "name", "email", tutor_email),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate role-based demo addresses.")
    parser.add_argument("--dry", action="store_true", help="Show changes; write nothing.")
    args = parser.parse_args(argv)

    total = 0
    with get_conn() as conn, conn.cursor() as cur:
        for table, name_col, email_col, derive in _TARGETS:
            cur.execute(f"SELECT id, {name_col}, {email_col} FROM {table} ORDER BY id")
            rows = cur.fetchall()
            updates = [(rid, derive(name), old) for rid, name, old in rows]
            changed = [(rid, new) for rid, new, old in updates if new != old]

            print(f"{table}.{email_col}: {len(changed)}/{len(rows)} row(s) to update")
            for rid, new, old in updates[:3]:
                print(f"    #{rid}: {old}  ->  {new}")

            if not args.dry:
                cur.executemany(
                    f"UPDATE {table} SET {email_col} = %s WHERE id = %s",
                    [(new, rid) for rid, new in changed],
                )
            total += len(changed)

        if args.dry:
            conn.rollback()
            print(f"\nDRY RUN — {total} change(s) computed, nothing written.")
        else:
            conn.commit()
            print(f"\nApplied {total} address change(s) to the live DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
