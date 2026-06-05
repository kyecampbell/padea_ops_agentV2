"""DRY RUN — REPLY ATTRIBUTION + IDEMPOTENCY for the student choose-and-rate.

The demo's main risk: all demo students share ONE inbox (the sink), so replies all
arrive FROM the same address. This proves a reply is matched to the CORRECT student
+ session by the per-(student, week) reference token in the email, NOT by sender and
NOT by "the first open request":

  A. ATTRIBUTION — render TWO students' (A, B) choice emails, build a reply that
     quotes B's email, and show identify_reply resolves it to B (pick + rating
     attribute to B, not A). Cross-checked the other way (a reply quoting A -> A),
     and a token-less reply -> conflict (agent falls back to body identification).

  B. SEND IDEMPOTENCY — send_choice_requests(week) run twice sends N then 0
     (requires EMAIL_MODE=dry: logs 'drafted', sends nothing live). Cleaned up.

  C. RECORD IDEMPOTENCY — recording the SAME reply twice creates NO duplicate rows:
     the PICK upserts one meal_requests row; the RATING upserts one feedback row.
     Cleaned up.

Parts B and C write test rows and then DELETE them (scoped), leaving the baseline
pristine — verified by before/after counts at the end. Nothing is sent live.

Run: EMAIL_MODE=dry uv run python scripts/dry_run_choice_attribution.py [YYYY-MM-DD]
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from config.settings import settings
from src.db.connection import fetch_all, get_conn
from src.tools import orders_batch, student_choice


def _target_week(argv: list[str]) -> date:
    if argv:
        return orders_batch.monday_of_week(date.fromisoformat(argv[0]))
    rows = fetch_all("SELECT max(session_date) AS d FROM orders")
    if rows and rows[0][0]:
        return orders_batch.monday_of_week(rows[0][0]) + timedelta(days=7)
    return orders_batch.upcoming_monday(date.today())


def _counts() -> dict:
    return {
        "meal_requests": fetch_all("SELECT count(*) FROM meal_requests")[0][0],
        "feedback": fetch_all("SELECT count(*) FROM feedback")[0][0],
        "student_feedback": fetch_all("SELECT count(*) FROM feedback WHERE source='student'")[0][0],
        "outbound": fetch_all("SELECT count(*) FROM outbound_emails")[0][0],
        "agent_runs": fetch_all("SELECT count(*) FROM agent_runs")[0][0],
    }


def _quoted_reply(student_subject: str, student_body: str, typed: str) -> tuple[str, str]:
    """A realistic reply: the student's typed text on top, the ORIGINAL choice email
    quoted below (subject becomes 'Re: [DEMO …] <subject>', as the sink would relay)."""
    demo_subject = f"[DEMO — Intended for: {settings.demo_sink_email}] {student_subject}"
    reply_subject = f"Re: {demo_subject}"
    quoted = "\n".join("> " + ln for ln in student_body.splitlines())
    reply_body = f"{typed}\n\nOn ... Padea wrote:\n{quoted}"
    return reply_subject, reply_body


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        week = _target_week(argv)
    except ValueError:
        print(f"Bad date {argv[0]!r}; use YYYY-MM-DD.", file=sys.stderr)
        return 1

    print("=" * 84)
    print(f"DRY RUN — REPLY ATTRIBUTION + IDEMPOTENCY for week of {week.isoformat()}")
    print("=" * 84)

    baseline = _counts()
    print(f"\nbaseline counts: {baseline}")

    cache: dict = {}   # per-invocation plan memo (one gather per caterer-week)
    plan = student_choice.plan_choice_requests(week, cache=cache)
    if not isinstance(plan, dict):
        print(f"plan_choice_requests failed: {plan.status} — {plan.message}", file=sys.stderr)
        return 1
    rateable = [w for w in plan["would_send"] if w["rate_last"]]
    if len(rateable) < 2:
        print("Need >=2 demo students with a meal to rate; run build_student_demo_email.py.",
              file=sys.stderr)
        return 1
    A, B = rateable[0], rateable[1]
    a_eid, b_eid = A["enrolment_id"], B["enrolment_id"]

    # =====================================================================
    # A. ATTRIBUTION
    # =====================================================================
    print("\n" + "-" * 84)
    print("A. REPLY ATTRIBUTION (two students, one shared inbox)")
    print("-" * 84)
    a_subj, a_body = student_choice.render_choice_email(a_eid, week, cache=cache)
    b_subj, b_body = student_choice.render_choice_email(b_eid, week, cache=cache)
    print(f"  Student A = #{a_eid} {A['student_name']}   subject: {a_subj}")
    print(f"  Student B = #{b_eid} {B['student_name']}   subject: {b_subj}")
    print("  Both emails are delivered FROM/redirected to the same sink "
          f"({settings.demo_sink_email}); the From address is NOT an identity signal.")

    # A reply that quotes student B's email, with B's pick + rating typed on top.
    b_opts = student_choice.build_choice_options(b_eid, week, cache=cache)
    b_pick = b_opts.options[0]
    typed_b = f"{b_pick.number}, rated 5/5 — {b_pick.name.lower()} was great"
    reply_subject, reply_body = _quoted_reply(b_subj, b_body, typed_b)
    print(f"\n  Incoming reply (quotes B), typed: {typed_b!r}")
    print(f"    reply subject: {reply_subject[:96]}...")

    resolved = student_choice.identify_reply(reply_subject, reply_body)
    if not isinstance(resolved, student_choice.ChoiceOptions):
        print(f"  ! identify_reply failed: {resolved.status} — {resolved.message}", file=sys.stderr)
        return 1
    ok_ident = resolved.enrolment_id == b_eid
    print(f"\n  identify_reply -> enrolment {resolved.enrolment_id} ({resolved.student_name}), "
          f"session {resolved.upcoming_session_date}")
    print(f"    attributes to B (#{b_eid})? {ok_ident}    (NOT A #{a_eid}, NOT 'first open request')")

    # The pick + rating attribute to B specifically (pure planners).
    mr = student_choice.plan_meal_choice(resolved.enrolment_id, b_pick.menu_item_id, week)
    fb = student_choice.plan_meal_rating(resolved.enrolment_id, 5, "was great", week)
    mr_ok = isinstance(mr, dict) and mr["enrolment_id"] == b_eid
    fb_ok = isinstance(fb, dict) and fb["enrolment_id"] == b_eid
    print(f"    PICK would write meal_request for enrolment {mr['enrolment_id'] if mr_ok else mr}; "
          f"to B? {mr_ok}")
    print(f"    RATING would write feedback for enrolment {fb['enrolment_id'] if fb_ok else fb}; "
          f"to B? {fb_ok}")

    # Cross-check: a reply quoting A resolves to A; a token-less reply -> conflict.
    a_reply_subject, a_reply_body = _quoted_reply(a_subj, a_body, "1, 3/5")
    a_res = student_choice.identify_reply(a_reply_subject, a_reply_body)
    a_ok = isinstance(a_res, student_choice.ChoiceOptions) and a_res.enrolment_id == a_eid
    notoken = student_choice.identify_reply("Re: a reply with no token", "thanks!")
    print(f"\n  cross-check: reply quoting A -> enrolment "
          f"{a_res.enrolment_id if a_ok else a_res}; to A (#{a_eid})? {a_ok}")
    print(f"  token-less reply -> {notoken.status.upper()} "
          f"(agent then identifies from body): {notoken.message[:70] if hasattr(notoken,'message') else ''}...")

    attribution_ok = ok_ident and mr_ok and fb_ok and a_ok and notoken.status == "conflict"
    print(f"\n  ATTRIBUTION CORRECT: {attribution_ok}")

    # =====================================================================
    # B. SEND IDEMPOTENCY (EMAIL_MODE=dry: drafted, never live)
    # =====================================================================
    print("\n" + "-" * 84)
    print("B. SEND IDEMPOTENCY — send_choice_requests(week) twice")
    print("-" * 84)
    send_ok = None
    if settings.email_mode != "dry":
        print(f"  SKIPPED — EMAIL_MODE={settings.email_mode!r}, not 'dry'. Re-run as "
              "`EMAIL_MODE=dry uv run python scripts/dry_run_choice_attribution.py` to "
              "prove this safely (logs 'drafted', sends nothing live).")
    else:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO agent_runs (trigger_reason, notes) VALUES "
                        "('choice_idempotency_dryrun','attribution+idempotency proof') RETURNING id")
            run_id = cur.fetchone()[0]
            conn.commit()
        try:
            r1 = student_choice.send_choice_requests(week, run_id=run_id, cache=cache)
            r2 = student_choice.send_choice_requests(week, run_id=run_id, cache=cache)
            n1 = len((r1.data or {}).get("sent", []))
            n2 = len((r2.data or {}).get("sent", []))
            sk2 = len((r2.data or {}).get("skipped", []))
            print(f"  first run:  {n1} drafted (EMAIL_MODE=dry — nothing sent live)")
            print(f"  second run: {n2} sent, {sk2} skipped (idempotent)")
            send_ok = (n1 > 0 and n2 == 0)
            print(f"  SEND IDEMPOTENT (2nd run sends 0): {send_ok}")
        finally:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM outbound_emails WHERE related_run_id = %s", (run_id,))
                cur.execute("DELETE FROM agent_runs WHERE id = %s", (run_id,))
                conn.commit()
            print("  (cleaned up drafted rows + the proof run)")

    # =====================================================================
    # C. RECORD IDEMPOTENCY (record the same reply twice -> no dupes)
    # =====================================================================
    print("\n" + "-" * 84)
    print("C. RECORD IDEMPOTENCY — record the same reply twice")
    print("-" * 84)
    slot = b_opts.upcoming_slot_id
    sdate = b_opts.upcoming_session_date
    fb_line_id = b_opts.last_meal.order_line_id if b_opts.last_meal else None

    def mr_count():
        return fetch_all(
            "SELECT count(*) FROM meal_requests WHERE enrolment_id=%s AND session_slot_id=%s AND session_date=%s",
            (b_eid, slot, sdate))[0][0]

    def fb_count():
        if fb_line_id is None:
            return None
        return fetch_all(
            "SELECT count(*) FROM feedback WHERE order_line_id=%s AND source='student'",
            (fb_line_id,))[0][0]

    try:
        c1 = student_choice.record_meal_choice(b_eid, b_pick.menu_item_id, week)
        n_after_1 = mr_count()
        c2 = student_choice.record_meal_choice(b_eid, b_pick.menu_item_id, week)
        n_after_2 = mr_count()
        print(f"  PICK recorded twice -> meal_requests rows for (B, slot, date): "
              f"{n_after_1} then {n_after_2}  (no duplicate: {n_after_2 == 1})")

        student_choice.record_meal_rating(b_eid, 5, "was great", week)
        f_after_1 = fb_count()
        student_choice.record_meal_rating(b_eid, 4, "actually pretty good", week)
        f_after_2 = fb_count()
        print(f"  RATING recorded twice -> student feedback rows for last week's line: "
              f"{f_after_1} then {f_after_2}  (no duplicate: {f_after_2 == 1})")
        record_ok = (n_after_2 == 1 and (f_after_2 == 1))
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM meal_requests WHERE enrolment_id=%s AND session_slot_id=%s AND session_date=%s",
                (b_eid, slot, sdate))
            if fb_line_id is not None:
                cur.execute(
                    "DELETE FROM feedback WHERE order_line_id=%s AND source='student'", (fb_line_id,))
            conn.commit()
        print("  (cleaned up the test pick + rating)")

    # =====================================================================
    # Verify pristine baseline restored.
    # =====================================================================
    after = _counts()
    pristine = after == baseline
    print("\n" + "=" * 84)
    print("RESULT")
    print("=" * 84)
    print(f"  Attribution correct (B not A, token not sender) ... {attribution_ok}")
    print(f"  Send idempotent (2nd run = 0) .................... {send_ok}")
    print(f"  Record idempotent (no duplicate rows) ........... {record_ok}")
    print(f"  Baseline restored (nothing persisted) ........... {pristine}")
    if not pristine:
        print(f"    baseline={baseline}\n    after   ={after}")
    print("\n  Nothing was sent live. No files pushed.")
    return 0 if (attribution_ok and record_ok and pristine and send_ok is not False) else 1


if __name__ == "__main__":
    sys.exit(main())
