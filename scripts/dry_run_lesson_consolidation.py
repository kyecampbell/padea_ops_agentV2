"""DRY RUN — one comment, multiple lessons: consolidate, don't fragment.

Proves the ISSUE-2 fix and writes only scoped rows that are cleaned up, so the
baseline is untouched.

THE BEHAVIOUR: when an operator comment is a LESSON/BOTH, the feedback sweep now
DECOMPOSES it into its distinct atomic lessons (model judgment), and for EACH one
dedupes against existing lessons — MERGING a restatement/extension into the existing
case (update in place) and CREATING only what is genuinely new. It does not fragment
a single coherent thought, and does not over-split.

Unlike the routing/escalation proofs, the decompose + dedupe/merge decision IS the
thing under test, so this proof runs the REAL planner + case-book writes
(feedback._apply_lessons -> plan_lessons + store_case/update_case).

RACE-SAFE BY DESIGN: a live background worker (Render padea-worker) polls this same
DB for un-actioned decision_annotations. So this proof inserts NONE — it calls
_apply_lessons DIRECTLY with in-memory comment records, which only ever writes to the
`cases` table (which the worker reads but never claims/mutates). No re-run is ever
triggered; nothing is sent.

  (A) MULTI-POINT comment ("verify the allergy in the DB before trusting the parent"
      AND "reply to the real sender, not the sink") -> TWO distinct atomic lessons:
      the reply point MERGES into the pre-seeded reply lesson; the DB point is NEW.
  (B) RESTATEMENT of an existing lesson -> MERGES into it; NO new/duplicate case.

Run: EMAIL_MODE=dry uv run python scripts/dry_run_lesson_consolidation.py
"""

from __future__ import annotations

import sys

from config.settings import settings
from src.agent import feedback
from src.db.connection import fetch_all, get_conn
from src.tools.casebook import store_case

# A pre-existing operator lesson the restatements should MERGE into (not duplicate).
SEED_LESSON = {
    "situation": "Replying to a person who emailed the operations inbox",
    "decision": "Reply to the actual person who wrote in, never the shared demo sink inbox.",
    "tags": ["operator-feedback", "inbound_email", "reply-routing"],
}

COMMENT_A = (
    "Two things going forward: when a parent's email mentions it's their child's "
    "birthday that week, add a complimentary dessert to that child's order for the "
    "birthday day; and make sure your reply actually goes to the person who wrote "
    "in, not our internal sink address."
)
COMMENT_B = "Reminder for next time: replies must go to the real sender, not the sink."


def _active_feedback_cases() -> int:
    return fetch_all(
        "SELECT count(*) FROM cases WHERE active = TRUE AND 'operator-feedback' = ANY(tags)"
    )[0][0]


def _case_text(case_id: int) -> str:
    row = fetch_all("SELECT decision FROM cases WHERE id = %s", (case_id,))
    return (row[0][0] if row else "") or ""


def main() -> int:
    if settings.email_mode != "dry":
        print(f"Refusing to run with EMAIL_MODE={settings.email_mode!r} — re-run as "
              "`EMAIL_MODE=dry uv run python scripts/dry_run_lesson_consolidation.py`.",
              file=sys.stderr)
        return 1

    print("=" * 92)
    print("DRY RUN — one comment -> many atomic lessons; restatements merge, no duplicates")
    print("=" * 92)

    created_cases: list[int] = []
    try:
        seed = store_case(created_by="operator (proof seed)", **SEED_LESSON)
        seed_id = int(seed.data["case_id"])
        created_cases.append(seed_id)
        print(f"\nPre-seeded existing lesson #{seed_id}: {SEED_LESSON['decision']!r}")
        base = _active_feedback_cases()
        print(f"Active operator-feedback lessons before: {base}")

        situation = "Run handling an inbound parent email (lesson-consolidation proof)."

        # --- (A) multi-point comment ----------------------------------------
        print("\n" + "-" * 92)
        print("(A) MULTI-POINT comment -> distinct atomic lessons (real model decomposes + dedupes)")
        print("-" * 92)
        print(f"   comment: {COMMENT_A}")
        # Call the REAL _apply_lessons directly (no decision_annotation inserted, so
        # the live worker can't race it); writes only to `cases`.
        ann_a = {"comment": COMMENT_A, "author": "kye (proof)", "run_id": None,
                 "trigger_reason": "inbound_email"}
        applied_a = feedback._apply_lessons(ann_a, situation)
        for rec in applied_a:
            if rec["action"] == "created":
                created_cases.append(rec["case_id"])
            print(f"   - {rec['action']:7} case #{rec['case_id']}: {_case_text(rec['case_id'])!r}")
        after_a = _active_feedback_cases()
        print(f"   active operator-feedback lessons now: {after_a}  (created {after_a - base} new)")
        print(f"   existing reply lesson #{seed_id} after merge: {_case_text(seed_id)!r}")

        # --- (B) restatement of an existing lesson --------------------------
        print("\n" + "-" * 92)
        print("(B) RESTATEMENT of an existing lesson -> merges, NO duplicate")
        print("-" * 92)
        print(f"   comment: {COMMENT_B}")
        before_b = _active_feedback_cases()
        ann_b = {"comment": COMMENT_B, "author": "kye (proof)", "run_id": None,
                 "trigger_reason": "inbound_email"}
        applied_b = feedback._apply_lessons(ann_b, situation)
        for rec in applied_b:
            if rec["action"] == "created":
                created_cases.append(rec["case_id"])
            print(f"   - {rec['action']:7} case #{rec['case_id']}: {_case_text(rec['case_id'])!r}")
        after_b = _active_feedback_cases()
        print(f"   active operator-feedback lessons: {before_b} -> {after_b}  (created {after_b - before_b} new)")

        actions_a = [r["action"] for r in applied_a]
        created_in_b = sum(1 for r in applied_b if r["action"] == "created")
        # --- Verdict --------------------------------------------------------
        checks = {
            "(A) decomposed into >=2 distinct lessons": len(applied_a) >= 2,
            "(A) not over-split (<=4 lessons)": len(applied_a) <= 4,
            "(A) at least one restatement merged into an existing lesson": "merged" in actions_a,
            "(A) at least one genuinely new lesson created": "created" in actions_a,
            "(B) restatement created NO new lesson (deduped)": created_in_b == 0,
            "(B) no duplicate proliferation (count unchanged)": after_b == before_b,
        }
        print("\n" + "=" * 92)
        print("RESULT")
        print("=" * 92)
        for label, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
        all_ok = all(checks.values())
        print("\n  Nothing sent live; nothing left written; no push." if all_ok else "\n  SOME CHECKS FAILED.")
        return 0 if all_ok else 1

    finally:
        all_cases = list(set(created_cases))
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM cases WHERE id = ANY(%s)", (all_cases,))
            conn.commit()
        print("\n   (cleaned up the proof lessons — baseline restored)")


if __name__ == "__main__":
    sys.exit(main())
