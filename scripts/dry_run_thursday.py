"""DRY RUN of the Thursday batch — composes + renders everything, SENDS NOTHING.

Proves the deterministic order-email path before any live send:

  1. composes the week (persists orders; sends no email),
  2. for EACH caterer, prints the order email it WOULD send — recipient, subject,
     full deterministic body — and whether it would send or be skipped (idempotency),
  3. prints the parent default-note emails the agent WOULD send (one per defaulted
     student), and the students that compose_week escalated (no meal, no email),
  4. prints the headline counts so you can confirm "exactly N caterer emails, 0
     duplicates" with zero risk.

No email is ever sent here: it only calls ``order_email.plan_caterer_orders`` /
``build_order_email`` (pure render + idempotency check), never ``send_*``.

Run: uv run python scripts/dry_run_thursday.py [YYYY-MM-DD]
"""

from __future__ import annotations

import sys
from datetime import date

from src.db.connection import get_conn
from src.tools import order_email, orders_batch, parent_prefs


def _open_run(week_of: date) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (trigger_reason, notes) VALUES (%s, %s) RETURNING id",
            ("thursday_dry_run", f"dry run compose for week of {week_of.isoformat()}"),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return int(run_id)


def _close_run(run_id: int, notes: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runs SET completed_at = now(), notes = %s WHERE id = %s",
            (notes, run_id),
        )
        conn.commit()


def _indent(text: str, prefix: str = "      | ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        try:
            week_of = orders_batch.monday_of_week(date.fromisoformat(argv[0]))
        except ValueError:
            print(f"Bad date {argv[0]!r}; use YYYY-MM-DD.", file=sys.stderr)
            return 1
    else:
        week_of = orders_batch.upcoming_monday(date.today())

    print("=" * 80)
    print(f"DRY RUN — Thursday batch for week of {week_of.isoformat()}  (NOTHING WILL BE SENT)")
    print("=" * 80)

    # --- 1. Compose (persists orders; sends no email). ---
    run_id = _open_run(week_of)
    composed = orders_batch.compose_week(week_of, run_id=run_id)
    if not composed.ok:
        print(f"compose_week failed: {composed.status} — {composed.message}", file=sys.stderr)
        _close_run(run_id, f"failed: {composed.message}")
        return 1
    caterers = composed.data["caterers"]
    escalated_by_caterer = {
        c["caterer_id"]: c.get("escalated_students", []) for c in caterers
    }
    print(f"\ncompose_week: {composed.message}\n")

    # --- 2. Caterer order emails we WOULD send (deterministic + idempotent). ---
    plans = order_email.plan_caterer_orders(week_of)
    if not isinstance(plans, list):
        print(f"plan_caterer_orders failed: {plans.status} — {plans.message}", file=sys.stderr)
        _close_run(run_id, f"failed: {plans.message}")
        return 1

    print("-" * 80)
    print("CATERER ORDER EMAILS (one per caterer; deterministic template)")
    print("-" * 80)
    would_send_count = 0
    for plan in plans:
        d = plan.draft
        verdict = "WOULD SEND ✅" if plan.would_send else f"SKIP ⤬ ({plan.reason})"
        if plan.would_send:
            would_send_count += 1
        print(f"\n● Caterer {d.caterer_id} — {d.caterer_name}   [{verdict}]")
        print(f"    To:      {d.to}")
        print(f"    Subject: {d.subject}")
        print(f"    Meals:   {d.total_items}   Cost (incl GST): {order_email._money(d.total_cost_cents)}")
        if d.sendable:
            print("    Body:")
            print(_indent(d.body))

    # --- 3. Parent prefs requests / flexible resolution (deterministic, PURE plan). ---
    prefs_actions = parent_prefs.plan_prefs(week_of)
    if not isinstance(prefs_actions, list):
        print(f"plan_prefs failed: {prefs_actions.status} — {prefs_actions.message}", file=sys.stderr)
        _close_run(run_id, f"failed: {prefs_actions.message}")
        return 1
    first_ask = [a for a in prefs_actions if a.action == parent_prefs.ACTION_FIRST_ASK]
    flexible = [a for a in prefs_actions if a.action == parent_prefs.ACTION_FLEXIBLE]

    print("\n" + "-" * 80)
    print("PARENT PREFS REQUESTS (one-time, per defaulted student)")
    print("-" * 80)
    if not first_ask:
        print("  (none would send — no first-time defaulted students)")
    for a in first_ask:
        s = a.student
        built = parent_prefs.build_prefs_request(s)
        if not isinstance(built, tuple):
            print(f"  ! could not render prefs request for {s.student_name}: {built.message}")
            continue
        subject, body = built
        print(f"\n  → to {s.parent_email}  | {s.student_name} ({s.school_name}); "
              f"assumed '{s.item}' this week.")
        print(f"    Subject: {subject}")
        print("    Body:")
        print(_indent(body))

    print("\n" + "-" * 80)
    print("FLEXIBLE RESOLUTION (non-responders already asked in a prior run)")
    print("-" * 80)
    if not flexible:
        print("  (none — no defaulted student has a prior prefs request outstanding)")
    for a in flexible:
        s = a.student
        print(f"  ↻ {s.student_name} ({s.school_name}) — would set ALL eligible meals as "
              f"flexible preference; no further note. (dietary known)")

    # --- 4. Escalated students (no meal, no email — surfaced only). ---
    print("\n" + "-" * 80)
    print("ESCALATED STUDENTS (no meal ordered; surfaced, NOT emailed a meal)")
    print("-" * 80)
    esc_total = 0
    for caterer_id, students in escalated_by_caterer.items():
        for st in students:
            esc_total += 1
            print(f"  ⚠ {st['student_name']} — {st['reason']} (caterer {caterer_id})")
    if esc_total == 0:
        print("  (none)")

    # --- Agent-supervised tool sequence (what run_thursday_incident.py drives). ---
    # Deterministic preview of the ordered tool calls the thursday_batch incident
    # makes — the agent ASSESSES each result and CONFIRMS success before the next,
    # HOLDING + escalating on any empty/error/unavailable. Nothing is sent here.
    print("\n" + "-" * 80)
    print("AGENT-SUPERVISED TOOL SEQUENCE (thursday_batch incident; NOTHING SENT)")
    print("-" * 80)
    wk = week_of.isoformat()
    recompose = bool(flexible)
    step = 1
    print(f"  {step}. compose_week(week_of='{wk}')")
    print(f"        → assess: {composed.message}")
    print("        → confirm composed; on unavailable/error HOLD + escalate_to_human")
    step += 1
    print(f"  {step}. apply_flexible_resolution(week_of='{wk}')")
    print(f"        → would resolve {len(flexible)} non-responder(s)"
          + (" → re-compose required" if recompose else " → no re-compose needed"))
    if recompose:
        step += 1
        print(f"  {step}. compose_week(week_of='{wk}')   [re-compose after flexible resolution]")
    step += 1
    print(f"  {step}. send_prefs_requests(week_of='{wk}')")
    print(f"        → would send {len(first_ask)} one-time prefs request(s); assess + confirm")
    step += 1
    print(f"  {step}. send_caterer_orders(week_of='{wk}')")
    print(f"        → would send {would_send_count} order email(s) (one per caterer, "
          "full per-session manifests); assess 'failed' → escalate")
    print("  (agent never hand-sends caterer orders; send_caterer_orders is the only path)")

    # --- Headline counts. ---
    print("\n" + "=" * 80)
    print("DRY-RUN SUMMARY")
    print("=" * 80)
    print(f"  Caterers with a composed order ... {len(plans)}")
    print(f"  Caterer order emails WOULD send .. {would_send_count}  (≤ 1 per caterer, 0 duplicates)")
    print(f"  Already-sent (idempotent skips) .. {sum(1 for p in plans if p.already_sent)}")
    print(f"  Prefs requests WOULD send ........ {len(first_ask)}  (one-time per student)")
    print(f"  Flexible resolutions WOULD apply . {len(flexible)}  (non-responders)")
    print(f"  Students escalated (no meal) ..... {esc_total}")
    print("\n  NOTHING WAS SENT. Re-run with the live flow only after confirming the count.")

    _close_run(
        run_id,
        f"dry run week={week_of.isoformat()} caterer_emails={would_send_count} "
        f"prefs_requests={len(first_ask)} flexible={len(flexible)} escalated={esc_total}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
