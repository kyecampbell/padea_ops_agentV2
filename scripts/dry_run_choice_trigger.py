"""DRY RUN — per-session DINNER-TIME TRIGGER for the student choose-and-rate.

The real trigger is a data-driven sweep (worker.py ticks
``student_choice.run_due_session_choice_sends(now)``): at each session's dinner
time it sends THAT session's choice emails. This proves it, with EMAIL_MODE=dry
(logs 'drafted', sends nothing live) and full cleanup:

  1. Simulate "just after Tuesday's dinner" -> due_session_slots returns only
     Tuesday's slots (data-driven; day-of-week + dinner-time + misfire grace).
  2. Run the sweep -> only Tuesday-session students are drafted; Monday-only,
     Wednesday, and Thursday students are NOT.
  3. An immediate SECOND sweep sends 0 (idempotent per student-week).
  4. Blank-email students rostered to a due session are skipped (never emailed).
  5. Subject polish: the human subject still carries the attribution token, and
     parse_reply_reference still resolves it.

Writes only 'drafted' rows (EMAIL_MODE=dry), then deletes them — baseline verified
pristine at the end. Nothing sent live.

Run: EMAIL_MODE=dry uv run python scripts/dry_run_choice_trigger.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import settings
from src.db.connection import fetch_all, get_conn
from src.tools import student_choice

# "Just after Tuesday's dinner" — a fixed, deterministic instant in the demo week
# (2026-06-09 is the Tuesday of the seeded order week). Grace is widened to capture
# the whole Tuesday dinner span (17:00–18:00) in one snapshot; production uses the
# configured misfire_grace_seconds and a frequent tick, firing each slot near its
# own dinner time.
_NOW = datetime(2026, 6, 9, 18, 30, tzinfo=ZoneInfo(settings.scheduler_timezone))
_GRACE = 7200

# Students we expect to be EXCLUDED (not Tuesday-session): Mon-only #266,
# Wed #19/#267, Thu #82/#92. (#90/#210 are Tuesday too, so they ARE included.)
_NON_TUESDAY_DEMO = {266, 19, 267, 82, 92}


def _counts() -> dict:
    return {
        "outbound": fetch_all("SELECT count(*) FROM outbound_emails")[0][0],
        "agent_runs": fetch_all("SELECT count(*) FROM agent_runs")[0][0],
        "meal_requests": fetch_all("SELECT count(*) FROM meal_requests")[0][0],
        "feedback": fetch_all("SELECT count(*) FROM feedback")[0][0],
    }


def main() -> int:
    if settings.email_mode != "dry":
        print(f"Refusing to run with EMAIL_MODE={settings.email_mode!r} — re-run as "
              "`EMAIL_MODE=dry uv run python scripts/dry_run_choice_trigger.py` "
              "(logs 'drafted', sends nothing live).", file=sys.stderr)
        return 1

    print("=" * 84)
    print(f"DRY RUN — DINNER-TIME TRIGGER  (now = {_NOW.isoformat()}, EMAIL_MODE=dry)")
    print("=" * 84)
    baseline = _counts()
    print(f"\nbaseline: {baseline}")

    # 1. Due sessions — only Tuesday's.
    due = student_choice.due_session_slots(_NOW, grace_seconds=_GRACE)
    print("\n" + "-" * 84)
    print("1. DUE SESSIONS at 'just after Tuesday dinner' (data-driven)")
    print("-" * 84)
    for s in due:
        print(f"   slot {s['id']:>2} school {s['school_id']} day_of_week={s['day_of_week']} "
              f"dinner {s['dinner_time']}")
    all_tuesday = all(s["day_of_week"] == 2 for s in due)
    print(f"   -> {len(due)} due; all day_of_week==2 (Tuesday)? {all_tuesday}")

    # 2. Run the sweep (drafts in dry mode), open a scoped run for clean teardown.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs (trigger_reason, notes) VALUES "
                    "('choice_trigger_dryrun','dinner-time trigger proof') RETURNING id")
        run_id = cur.fetchone()[0]
        conn.commit()
    try:
        r1 = student_choice.run_due_session_choice_sends(_NOW, run_id=run_id, grace_seconds=_GRACE)
        drafted = sorted({d["enrolment_id"] for slot in r1.data["per_slot"] for d in slot["sent"]})
        print("\n" + "-" * 84)
        print("2. SWEEP — who got drafted (only Tuesday-session students)")
        print("-" * 84)
        names = {r[0]: r[1] for r in fetch_all(
            "SELECT id, student_name FROM enrolments WHERE id = ANY(%s)", (drafted,))}
        for eid in drafted:
            print(f"   drafted #{eid:>3} {names.get(eid,'?')}")
        leaked = sorted(set(drafted) & _NON_TUESDAY_DEMO)
        print(f"   -> {len(drafted)} drafted. Non-Tuesday demo students leaked in? "
              f"{leaked if leaked else 'NONE'}")
        # confirm every drafted student is rostered to a due (Tuesday) slot
        due_ids = [s["id"] for s in due]
        rostered_ok = all(
            fetch_all("SELECT 1 FROM enrolment_session_slots WHERE enrolment_id=%s AND session_slot_id = ANY(%s) LIMIT 1",
                      (eid, due_ids))
            for eid in drafted
        )
        print(f"   every drafted student is rostered to a due Tuesday slot? {rostered_ok}")

        # 3. Second sweep -> 0.
        r2 = student_choice.run_due_session_choice_sends(_NOW, run_id=run_id, grace_seconds=_GRACE)
        print("\n" + "-" * 84)
        print("3. IMMEDIATE SECOND SWEEP (idempotent)")
        print("-" * 84)
        print(f"   first sweep: {r1.data['sent']} sent   second sweep: {r2.data['sent']} sent, "
              f"{r2.data['skipped']} skipped")
        idem_ok = r1.data["sent"] > 0 and r2.data["sent"] == 0

        # 4. Blank-email skip — Blake Brown #2 shares slot 1 (Tuesday) with Henry #1.
        print("\n" + "-" * 84)
        print("4. BLANK-EMAIL STUDENT on a due session is skipped")
        print("-" * 84)
        blank_eid = 2
        blank_drafted = blank_eid in drafted
        rostered_tue = bool(fetch_all(
            "SELECT 1 FROM enrolment_session_slots ess JOIN session_slots ss ON ss.id=ess.session_slot_id "
            "WHERE ess.enrolment_id=%s AND ss.day_of_week=2 LIMIT 1", (blank_eid,)))
        print(f"   #2 Blake Brown rostered to a Tuesday slot? {rostered_tue}; "
              f"student_email blank; drafted? {blank_drafted} (expect False)")
        blank_ok = rostered_tue and not blank_drafted

        # 5. Subject polish + token still parses.
        target_week = student_choice.orders_batch.monday_of_week(_NOW.date()) + student_choice.timedelta(days=7)
        subj, _body = student_choice.render_choice_email(1, target_week)
        parsed = student_choice.parse_reply_reference(f"Re: {subj}", "")
        print("\n" + "-" * 84)
        print("5. SUBJECT POLISH (human + token)")
        print("-" * 84)
        print(f"   {subj}")
        print(f"   parse_reply_reference(reply) -> {parsed}  (token still attributes to #1)")
        subj_ok = parsed is not None and parsed[0] == 1
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM outbound_emails WHERE related_run_id = %s", (run_id,))
            cur.execute("DELETE FROM agent_runs WHERE id = %s", (run_id,))
            conn.commit()
        print("\n   (cleaned up drafted rows + the proof run)")

    after = _counts()
    pristine = after == baseline
    print("\n" + "=" * 84)
    print("RESULT")
    print("=" * 84)
    print(f"  Only Tuesday sessions due ....................... {all_tuesday}")
    print(f"  Only Tuesday-session students drafted ........... {not leaked and rostered_ok}")
    print(f"  Second sweep sends 0 (idempotent) ............... {idem_ok}")
    print(f"  Blank-email student skipped ..................... {blank_ok}")
    print(f"  Polished subject still carries/parses token ..... {subj_ok}")
    print(f"  Baseline restored (nothing persisted) ........... {pristine}")
    if not pristine:
        print(f"    baseline={baseline}\n    after   ={after}")
    print("\n  Nothing sent live. No files pushed.")
    return 0 if (all_tuesday and not leaked and rostered_ok and idem_ok and blank_ok and subj_ok and pristine) else 1


if __name__ == "__main__":
    sys.exit(main())
