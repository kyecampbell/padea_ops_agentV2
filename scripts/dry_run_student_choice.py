"""DRY RUN of the weekly student CHOOSE-AND-RATE — renders + previews, WRITES/SENDS NOTHING.

Proves the centerpiece end-to-end with zero risk (no DB writes, no emails):

  1. plan_choice_requests — who WOULD be emailed this week (one per session at the
     sink), and who is skipped (blank email / no safe options / already sent);
  2. (a) the full rendered choose-and-rate email for one demo student;
  3. (b) a simulated student reply, parsed into the would-be meal_request (PICK) +
     student feedback (RATING) rows — via the PURE planners (nothing written), plus
     an ineligible pick being correctly rejected;
  4. (c) compose_week's assignment for that student WITH the pick vs the fallback,
     showing the pick (source 'request') wins — computed by the same plan/assign
     code compose_week uses (pure preview, no persist);
  5. (d) a BLANK-email student: skipped by the send, still assigned a clean
     fallback meal at compose time.

Everything here calls only PURE functions (plan_*/build_*/render_*/assign_student);
no order_lines, meal_requests, feedback, or emails are created. Run the live flow
(send_choice_requests in EMAIL_MODE=dry first) only after confirming this.

Run: uv run python scripts/dry_run_student_choice.py [YYYY-MM-DD]
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from src.db.connection import fetch_all
from src.tools import orders_batch, student_choice


def _indent(text: str, prefix: str = "      | ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _target_week(argv: list[str]) -> date:
    """The week to choose FOR. Default: the week AFTER the latest composed order
    week, so 'last week' (the meal being rated) is the seeded order week."""
    if argv:
        return orders_batch.monday_of_week(date.fromisoformat(argv[0]))
    rows = fetch_all("SELECT max(session_date) AS d FROM orders")
    if rows and rows[0][0]:
        return orders_batch.monday_of_week(rows[0][0]) + timedelta(days=7)
    return orders_batch.upcoming_monday(date.today())


def _meals(assigned, menu) -> str:
    """Render an assign_student result as 'Meal [source]' per session."""
    return "; ".join(f"{menu[i]['name']} [{src}]" for _, i, src in assigned)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        week = _target_week(argv)
    except ValueError:
        print(f"Bad date {argv[0]!r}; use YYYY-MM-DD.", file=sys.stderr)
        return 1

    print("=" * 82)
    print(f"DRY RUN — student CHOOSE-AND-RATE for week of {week.isoformat()}  (NOTHING WRITTEN OR SENT)")
    print("=" * 82)

    # --- 1. Who would be emailed (one per session at the sink). ---
    plan = student_choice.plan_choice_requests(week)
    if not isinstance(plan, dict):
        print(f"plan_choice_requests failed: {plan.status} — {plan.message}", file=sys.stderr)
        return 1
    would, skipped = plan["would_send"], plan["skipped"]
    print("\n" + "-" * 82)
    print("1. CHOICE EMAILS THAT WOULD SEND (one per session; idempotent; blanks excluded)")
    print("-" * 82)
    print(f"  would send: {len(would)}    skipped: {len(skipped)}")
    for w in would:
        rate = f"rate '{w['rate_last']}'" if w["rate_last"] else "no meal to rate"
        print(f"  → #{w['enrolment_id']:>3} {w['student_name']:<22} session {w['session_date']} "
              f"| {len(w['options'])} options | {rate}")
    skip_blank = sum(1 for s in skipped if "no options" in s["reason"] or "no offered" in s["reason"]
                     or "no safe" in s["reason"] or "escalated" in s["reason"])
    print(f"  (skipped reasons incl. blank-email/no-options: {len(skipped)} total)")

    if not would:
        print("\nNo emailable students with options this week — nothing to demo. "
              "Run scripts/build_student_demo_email.py first.", file=sys.stderr)
        return 1

    # Choose a demo student: prefer one that also has a meal to rate (full email).
    demo = next((w for w in would if w["rate_last"]), would[0])
    eid = demo["enrolment_id"]

    # --- (a) The rendered choose-and-rate email. ---
    rendered = student_choice.render_choice_email(eid, week)
    if not isinstance(rendered, tuple):
        print(f"render failed: {rendered.status} — {rendered.message}", file=sys.stderr)
        return 1
    subject, body = rendered
    opts = student_choice.build_choice_options(eid, week)
    print("\n" + "-" * 82)
    print(f"2(a). RENDERED CHOICE EMAIL — #{eid} {opts.student_name}")
    print("-" * 82)
    print(f"  To:      {opts.student_email}   (EMAIL_MODE governs delivery; demo-routed to the sink)")
    print(f"  Subject: {subject}")
    print("  Body:")
    print(_indent(body))

    # --- (b) Simulated reply parsed into a meal_request + a feedback row. ---
    # Pick an option that DIFFERS from the fallback, to make the override visible.
    cplan = orders_batch.plan_caterer_week_by_id(opts.caterer_id, week)
    fallback = orders_batch.assign_student(cplan, eid, None)
    fallback_item = fallback[0][1] if fallback else None
    pick = next((o for o in opts.options if o.menu_item_id != fallback_item), opts.options[0])
    sample_reply = f'"{pick.number}, rated 4/5 — {pick.name.lower()} was great, maybe warmer next time"'

    print("\n" + "-" * 82)
    print("2(b). SIMULATED REPLY, PARSED (pure planners — nothing written)")
    print("-" * 82)
    print(f"  Student reply (free text): {sample_reply}")
    print(f"  Agent parses -> pick #{pick.number} ({pick.name}); rating 4/5; comment.")

    mr = student_choice.plan_meal_choice(eid, pick.menu_item_id, week)
    if not isinstance(mr, dict):
        print(f"  ! pick rejected: {mr.status} — {mr.message}")
    else:
        print("\n  would-be meal_requests row (the PICK):")
        print(f"      enrolment_id={mr['enrolment_id']} session_slot_id={mr['session_slot_id']} "
              f"session_date={mr['session_date']}")
        print(f"      menu_item_id={mr['menu_item_id']} ({mr['item']})   -> order_line source 'request'")

    fb = student_choice.plan_meal_rating(eid, 4, "was great, maybe warmer next time", week)
    if not isinstance(fb, dict):
        print(f"  ! rating rejected: {fb.status} — {fb.message}")
    else:
        print("\n  would-be feedback row (the RATING):")
        print(f"      source='{fb['source']}' caterer_id={fb['caterer_id']} rating={fb['rating']} "
              f"order_line_id={fb['order_line_id']} (rated '{fb['rated_meal']}')")
        print(f"      comment={fb['comment']!r}")

    # An ineligible pick is rejected — never assigns an unsafe/off-menu meal.
    menu_ids = [r[0] for r in fetch_all(
        "SELECT id FROM menu_items WHERE caterer_id=%s AND active=TRUE", (opts.caterer_id,))]
    option_ids = {o.menu_item_id for o in opts.options}
    bogus = next((i for i in menu_ids if i not in option_ids), 999999)
    bad = student_choice.plan_meal_choice(eid, bogus, week)
    print(f"\n  ineligible pick (menu_item {bogus}, not in options) -> "
          f"{bad.status.upper()}: {bad.message if not isinstance(bad, dict) else 'UNEXPECTEDLY ACCEPTED'}")

    # --- (c) compose_week assignment: pick OVER fallback (pure preview). ---
    withpick = orders_batch.assign_student(cplan, eid, pick.menu_item_id)
    print("\n" + "-" * 82)
    print("2(c). compose_week ASSIGNMENT — PICK over FALLBACK (pure; not persisted)")
    print("-" * 82)
    print(f"  fallback (no pick): {_meals(fallback, cplan.menu)}")
    print(f"  with the pick:      {_meals(withpick, cplan.menu)}")
    picked_ok = any(src == 'request' and i == pick.menu_item_id for _, i, src in withpick)
    print(f"  -> the student's pick {'WINS (source request)' if picked_ok else 'was already the fallback'}; "
          f"variety stays within V_max (offered set has {len(cplan.offered)} items, ceiling {cplan.ceiling}).")

    # --- (d) Blank-email student falls back cleanly. ---
    print("\n" + "-" * 82)
    print("2(d). BLANK-EMAIL STUDENT — not emailed, falls back cleanly at compose time")
    print("-" * 82)
    would_ids = {w["enrolment_id"] for w in would}
    blank_eid = next(
        (e for e in cplan.safe_students if e not in would_ids and _blank_email(e)),
        None,
    )
    if blank_eid is None:
        print("  (no blank-email safe student in this caterer-week to illustrate)")
    else:
        bname = cplan.safe_students[blank_eid]["name"]
        in_send = blank_eid in would_ids
        bassign = orders_batch.assign_student(cplan, blank_eid, None)
        print(f"  #{blank_eid} {bname}: student_email blank -> in would_send? {in_send} (skipped)")
        print(f"     compose assigns (fallback): {_meals(bassign, cplan.menu)}")
        print("     -> safe meal, no email, no breakage.")

    print("\n" + "=" * 82)
    print("DRY-RUN SUMMARY — NOTHING WRITTEN, NOTHING SENT")
    print("=" * 82)
    print(f"  Week of ........................ {week.isoformat()}")
    print(f"  Choice emails would send ....... {len(would)}  (one per session, idempotent)")
    print(f"  Students skipped (blank/etc.) .. {len(skipped)}")
    print(f"  Demo student ................... #{eid} {opts.student_name}")
    print(f"  Pick over fallback ............. {'demonstrated (source request)' if picked_ok else 'pick == fallback'}")
    print("\n  Review the rendered email + parsed rows above. To preview the real send")
    print("  without delivering, run with EMAIL_MODE=dry; go live only after that.")
    return 0


_BLANK_CACHE: dict[int, bool] = {}


def _blank_email(enrolment_id: int) -> bool:
    """True if the enrolment's student_email is NULL/blank (cached single reads)."""
    if enrolment_id not in _BLANK_CACHE:
        rows = fetch_all(
            "SELECT student_email FROM enrolments WHERE id = %s", (enrolment_id,)
        )
        val = rows[0][0] if rows else None
        _BLANK_CACHE[enrolment_id] = not (val and val.strip())
    return _BLANK_CACHE[enrolment_id]


if __name__ == "__main__":
    sys.exit(main())
