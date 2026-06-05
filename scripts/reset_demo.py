"""Demo data reset — reload the clean seed, keep the training lessons.

Restores the operational dataset to a known-clean state so a demo can be re-run
from scratch, WITHOUT discarding what the operator has taught the agent. The
operator-authored content survives every reset:

  - cases               — the case-book of situation/decision/rationale lessons.
  - decision_annotations — operator comments on agent steps (the UI "train" path).
  - policies            — authoritative operator policies. Left wholly untouched
                          (not in any list below), so they persist across resets.

Three groups are managed:

  SEED      — business inputs + the derived dietary pool + historical feedback +
              the captured operational state (orders, order_lines, the weekly
              caterer summaries, the agent_runs that produced them, their
              agent_steps + citation rows — i.e. the full decision-feed / audit
              trail — and their outbound emails). Truncated and re-inserted
              verbatim from database/seed/seed.sql, so a reset RESTORES that state
              rather than clearing it. Re-capture with --capture to re-baseline.
  EPHEMERAL — the remaining agent-run artifacts (escalations, inbound records).
              Truncated to empty; the agent regenerates them.
  PRESERVE  — cases + decision_annotations. NEVER truncated. Their FK links to
              ephemeral/seed rows (run_id, step_id, related_*) are nulled first
              so the truncate is legal and no lesson is left pointing at a row
              that no longer exists; the lesson TEXT is untouched.

Reference/lookup tables (the enum replacements) are seeded by the schema DDL and
are left alone.

Usage:
  uv run python scripts/reset_demo.py --capture   # snapshot current DB -> seed.sql
  uv run python scripts/reset_demo.py --yes        # reset to the captured seed

``--capture`` writes database/seed/seed.sql from the CURRENT seed-table contents
(run it once against a clean DB to mint the golden seed). The default action is a
reset; it is destructive, so it requires ``--yes`` (or an interactive y/N).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Json

from config.settings import PROJECT_ROOT
from src.db.connection import get_conn

SEED_PATH = PROJECT_ROOT / "database" / "seed" / "seed.sql"

# Restored verbatim from seed.sql. ORDER MATTERS: parents before children so the
# inserts satisfy foreign keys without deferral.
SEED_TABLES: tuple[str, ...] = (
    "caterers",
    "schools",
    "caterer_moq_tier",
    "menu_items",
    "dietary_tags",
    "menu_item_dietary_tags",
    "tutors",
    "enrolments",
    "enrolment_dietary_tags",
    "session_slots",
    "session_tutor_assignments",
    "enrolment_session_slots",
    "exclusions",
    "absences",
    "term_meal_preferences",
    "term_meal_preference_items",
    "meal_requests",
    # Captured operational state — moved here from EPHEMERAL so a reset RESTORES
    # it. ORDER MATTERS (FK parents first): orders before order_lines (and before
    # feedback, which may reference them); agent_runs before outbound_emails
    # before caterer_week_orders (run_id / summary_email_id chain).
    "orders",
    "order_lines",
    "checklist_item",
    "feedback",
    "feedback_checklist_response",
    "student_eligible_meals",
    "opt_back_in_requests",
    "agent_runs",
    "agent_steps",
    "outbound_emails",
    "caterer_week_orders",
    # Citation link rows complete the baseline run's decision-feed / audit trail.
    # They reference agent_runs + agent_steps (both seeded just above). NOTE the
    # cross-FK to the PRESERVE tables: step_lesson_citations.case_id -> cases is
    # only FK-safe while EMPTY, because cases are truncated and re-inserted AFTER
    # seed.sql runs (see _restore_preserved) — a seeded lesson citation would
    # point at a case that does not yet exist at seed-load time. policies persist
    # untouched, so step_policy_citations is safe whenever its policies exist.
    "step_lesson_citations",
    "step_policy_citations",
)

# Agent-run artifacts that stay transient: truncated to empty, never seeded — the
# agent recreates them. The single multi-table TRUNCATE is order-independent, but
# every referencer of a truncated table must be in the TRUNCATE set (no CASCADE);
# the combined `targets` set below guarantees that. Nothing in SEED points at
# these with a non-null FK (the decision_annotations FK links into agent_steps /
# agent_runs are nulled by PRESERVE), so leaving them empty is FK-safe.
EPHEMERAL_TABLES: tuple[str, ...] = (
    "escalations",
    "inbound_email_records",
)

# Content preserved across a reset (the training lessons). Postgres won't let us
# TRUNCATE a table these still reference, so we snapshot their rows, truncate them
# alongside everything else, then re-insert — with the FK columns below nulled,
# since the runs/steps/orders they pointed at are regenerated (the lesson TEXT,
# which is the actual training value, is kept verbatim).
PRESERVE_TABLES: tuple[str, ...] = ("cases", "decision_annotations")
PRESERVE_NULL_FKS: dict[str, tuple[str, ...]] = {
    "cases": ("related_run_id", "related_caterer_id", "related_enrolment_id"),
    "decision_annotations": ("step_id", "run_id"),
}

_BATCH = 100  # rows per multi-row INSERT in the captured seed file.


# --- Capture (snapshot current DB -> seed.sql) -------------------------------


def _columns(cur: psycopg.Cursor, table: str) -> list[str]:
    """Ordinal-ordered column names for a public table."""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _lit(value):
    """Make a fetched value safe for sql.Literal.

    psycopg returns jsonb columns as Python dict/list, which sql.Literal cannot
    adapt directly. Wrap those in Json so they serialise to a quoted ``::jsonb``
    literal; every other value passes through unchanged.
    """
    return Json(value) if isinstance(value, (dict, list)) else value


def capture() -> int:
    """Write database/seed/seed.sql from the current seed-table contents.

    Uses a FRESH short-lived connection per table rather than holding one across the
    whole snapshot — the Supabase pooler can drop a long-idle connection mid-loop,
    which would hang the capture. Per-table connections keep each read short.
    """
    SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    with SEED_PATH.open("w", encoding="utf-8") as fh:
        fh.write("-- Padea demo seed — data only, generated by scripts/reset_demo.py --capture.\n")
        fh.write("-- Re-inserted verbatim on reset. Do not edit by hand.\n\n")
        for table in SEED_TABLES:
            with get_conn() as conn, conn.cursor() as cur:
                cols = _columns(cur, table)
                collist = ", ".join(cols)
                order = ", ".join(str(i + 1) for i in range(len(cols)))
                cur.execute(
                    sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                        sql.SQL(collist), sql.Identifier(table), sql.SQL(order)
                    )
                )
                rows = cur.fetchall()
                blocks: list[str] = [f"-- {table}: {len(rows)} row(s)\n"]
                for start in range(0, len(rows), _BATCH):
                    chunk = rows[start:start + _BATCH]
                    values = ",\n".join(
                        "(" + ", ".join(sql.Literal(_lit(v)).as_string(conn) for v in row) + ")"
                        for row in chunk
                    )
                    blocks.append(f"INSERT INTO {table} ({collist}) VALUES\n{values};\n")
            fh.write("".join(blocks))
            fh.write("\n")
            total_rows += len(rows)
    print(f"Captured {total_rows} row(s) across {len(SEED_TABLES)} table(s) -> {SEED_PATH}")
    return 0


# --- Restore (truncate + replay seed.sql) ------------------------------------


def _fix_sequences(cur: psycopg.Cursor) -> None:
    """Realign every serial sequence so the next id follows the seeded max.

    TRUNCATE ... RESTART IDENTITY rewinds sequences to 1, but the seed re-inserts
    explicit ids, so without this the next autogenerated id would collide. Empty
    (ephemeral) tables are rewound to start at 1.
    """
    cur.execute(
        """
        SELECT c.relname, a.attname, pg_get_serial_sequence(c.relname, a.attname)
        FROM pg_class c
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE c.relkind = 'r'
          AND c.relnamespace = 'public'::regnamespace
          AND a.attnum > 0 AND NOT a.attisdropped
          AND pg_get_serial_sequence(c.relname, a.attname) IS NOT NULL
        """
    )
    for table, column, seq in cur.fetchall():
        cur.execute(
            sql.SQL(
                "SELECT setval(%s, COALESCE(max({col}), 1), max({col}) IS NOT NULL) FROM {tbl}"
            ).format(col=sql.Identifier(column), tbl=sql.Identifier(table)),
            (seq,),
        )


def _snapshot_preserved(cur: psycopg.Cursor) -> dict[str, tuple[list[str], list[tuple]]]:
    """Read the preserve tables into memory, nulling their volatile FK columns.

    Returns ``{table: (columns, rows)}`` ready to re-insert after the truncate.
    """
    snapshot: dict[str, tuple[list[str], list[tuple]]] = {}
    for table in PRESERVE_TABLES:
        cols = _columns(cur, table)
        null_idx = {cols.index(c) for c in PRESERVE_NULL_FKS.get(table, ()) if c in cols}
        cur.execute(
            sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in cols), sql.Identifier(table)
            )
        )
        rows = [
            tuple(None if i in null_idx else v for i, v in enumerate(row))
            for row in cur.fetchall()
        ]
        snapshot[table] = (cols, rows)
    return snapshot


def _restore_preserved(cur: psycopg.Cursor, snapshot: dict[str, tuple[list[str], list[tuple]]]) -> None:
    """Re-insert the snapshotted preserve rows (ids and text intact)."""
    for table, (cols, rows) in snapshot.items():
        if not rows:
            continue
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(cols))
        cur.executemany(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                placeholders,
            ),
            rows,
        )


def restore() -> int:
    """Truncate everything and replay seed.sql, re-inserting preserved lessons."""
    if not SEED_PATH.exists():
        print(
            f"No seed file at {SEED_PATH}. Run `--capture` against a clean DB first.",
            file=sys.stderr,
        )
        return 1
    seed_sql = SEED_PATH.read_text(encoding="utf-8")

    # Every table touched, so the single TRUNCATE has no outside referencer.
    targets = list(EPHEMERAL_TABLES) + list(SEED_TABLES) + list(PRESERVE_TABLES)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            # 1. Snapshot the lessons (text kept, volatile FK links nulled).
            snapshot = _snapshot_preserved(cur)

            # 2. Wipe everything in one statement (FK-safe: all tables included).
            cur.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in targets)
                )
            )

            # 3. Reload the clean seed; ephemeral tables stay empty.
            cur.execute(seed_sql)

            # 4. Put the preserved lessons back.
            _restore_preserved(cur, snapshot)

            # 5. Realign sequences so new inserts don't collide with seeded ids.
            _fix_sequences(cur)
            conn.commit()
    except psycopg.Error as exc:
        print(f"Reset failed: {exc}", file=sys.stderr)
        return 1

    _report()
    return 0


def _report() -> None:
    """Print a short before/after-style confirmation of the reset state."""
    checks = [
        ("enrolments", "SELECT count(*) FROM enrolments"),
        ("student_eligible_meals", "SELECT count(*) FROM student_eligible_meals"),
        ("feedback", "SELECT count(*) FROM feedback"),
        ("orders (restored)", "SELECT count(*) FROM orders"),
        ("order_lines (restored)", "SELECT count(*) FROM order_lines"),
        ("caterer_week_orders (restored)", "SELECT count(*) FROM caterer_week_orders"),
        ("agent_runs (restored)", "SELECT count(*) FROM agent_runs"),
        ("outbound_emails (restored)", "SELECT count(*) FROM outbound_emails"),
        ("agent_steps (restored)", "SELECT count(*) FROM agent_steps"),
        ("step_lesson_citations (restored)", "SELECT count(*) FROM step_lesson_citations"),
        ("step_policy_citations (restored)", "SELECT count(*) FROM step_policy_citations"),
        ("cases (preserved)", "SELECT count(*) FROM cases"),
        ("decision_annotations (preserved)", "SELECT count(*) FROM decision_annotations"),
        ("policies (preserved)", "SELECT count(*) FROM policies"),
    ]
    print("Demo reset complete. Seed reloaded (orders restored); training lessons preserved.\n")
    with get_conn() as conn, conn.cursor() as cur:
        width = max(len(label) for label, _ in checks)
        for label, q in checks:
            cur.execute(q)
            print(f"  {label:<{width}}  {cur.fetchone()[0]:>5}")


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset demo data to the clean seed.")
    parser.add_argument(
        "--capture", action="store_true",
        help="Snapshot the current seed tables into database/seed/seed.sql and exit.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt (required when stdin is not a TTY).",
    )
    args = parser.parse_args(argv)

    if args.capture:
        return capture()

    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to reset without --yes (stdin is not a TTY).", file=sys.stderr)
            return 1
        reply = input("This truncates operational data (cases + annotations kept). Proceed? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    return restore()


if __name__ == "__main__":
    sys.exit(main())
