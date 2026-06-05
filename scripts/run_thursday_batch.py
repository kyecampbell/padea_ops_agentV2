"""Weekly Thursday batch runner — CORE order composition (no emails yet).

Opens an ``agent_runs`` row, composes the upcoming week's consolidated order for
every caterer via ``src.tools.orders_batch.compose_week``, and prints, per
caterer: the MOQ ceiling (V_max) and the offered set chosen against it, whether
the week auto-composed or escalated, a couple of students' last-few-weeks meals
as rotation evidence, and total meals + cost.

This step computes + persists orders, or — when a caterer-week can't be ordered
safely within V_max — composes nothing sendable and raises an escalation.

Run: uv run python scripts/run_thursday_batch.py
"""

from __future__ import annotations

import sys
from datetime import date

from src.db.connection import get_conn
from src.tools import orders_batch


def _open_run(week_of: date) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (trigger_reason, notes) VALUES (%s, %s) RETURNING id",
            ("thursday_batch", f"compose orders for week of {week_of.isoformat()}"),
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


def _cents(value: int) -> str:
    """Integer cents -> '$1,234.56'."""
    return f"${value / 100:,.2f}"


def _print_caterer(c: dict) -> None:
    print("=" * 78)
    verdict = "AUTO-COMPOSE ✅" if c["status"] == "composed" else "ESCALATE ⚠"
    print(f"{c['caterer_name']}  (caterer {c['caterer_id']})   [{verdict}]")
    print(f"  schools ............ {', '.join(c['schools'])}")
    print(
        f"  sessions ........... {c['num_sessions']} this week "
        f"({', '.join(s['session_date'] for s in c['sessions']) or 'none'})"
    )

    # --- V_max ceiling derivation (sized against the safe order). ---
    print(
        f"  V_max (ceiling) .... {c['v_max']}  "
        f"(budget {c['vmax_budget']} = safe order {c['safe_candidates']} "
        f"- {c['typical_absences']} typical absences - {c['safety_margin']} safety margin)"
    )
    print(
        f"  cohort ............. {c['cohort_candidates']} student-sessions "
        f"({c['no_line_count']} escalated individually, no line)"
    )

    # --- Caterer-wide escalation path: nothing sendable. ---
    if c["status"] == "escalated":
        print(f"  escalation ......... [{c['escalation_reason']}] esc #{c['escalation_id']}")
        print(f"                       {c['escalation_detail']}")
        print("  order .............. nothing composed (not sendable)")
        _print_student_escalations(c)
        return

    # --- Composed path: offered set + MOQ status. ---
    floor = c["moq_min_total"]
    moq_status = (
        "no MOQ tiers defined" if floor is None
        else f"clears floor {floor} — never breaches V_max"
    )
    print(
        f"  offered set ........ {c['offered_count']} of <= {c['v_max']}: "
        f"{', '.join(c['offered_items'])}"
    )
    print(
        f"  distinct ordered ... {c['variety_count']} ({c['total_items']} meals, "
        f"{c['defaulted_count']} defaulted) | MOQ: {moq_status}"
    )

    # --- Meal-by-meal breakdown (what the order email tells the caterer). ---
    if c.get("meal_breakdown"):
        print("  order (per meal) ... week totals the caterer email lists:")
        for m in c["meal_breakdown"]:
            print(f"      × {m['quantity']:>3}  {m['item']}")

    # --- Rotation evidence: a couple of students' last-few-weeks meals. ---
    if c["rotation_sample"]:
        print("  rotation ........... per-student meal sequence (one per session):")
        for r in c["rotation_sample"]:
            history = " -> ".join(r["recent_items"]) if r["recent_items"] else "(no prior weeks)"
            this_week = " | ".join(r["assigned_items"]) if r["assigned_items"] else "(none)"
            print(
                f"      • {r['student_name']}: last weeks [{history}] "
                f"=> this week [{this_week}]"
            )

    # --- Defaulted lines (safe, pending parent confirmation). ---
    defaulted = c.get("defaulted_lines") or []
    if defaulted:
        shown = defaulted[:5]
        more = len(defaulted) - len(shown)
        suffix = f" (+{more} more)" if more > 0 else ""
        print(f"  defaulted .......... safe default, awaiting parent confirmation{suffix}:")
        for d in shown:
            print(f"      ~ {d['student_name']} -> '{d['item']}'  (parent {d['parent_email']})")

    # --- Per-student escalations (composed the rest; these get no line). ---
    _print_student_escalations(c)

    # --- Totals. ---
    gst = "GST-incl. price" if c["price_includes_gst"] else f"+{c['gst_rate_percent']:.0f}% GST added"
    print(f"  total meals ........ {c['total_items']}")
    print(f"  total cost ......... {_cents(c['total_cost_cents'])}  ({gst})")

    # --- Per-session breakdown. ---
    for s in c["sessions"]:
        print(
            f"      - {s['session_date']}  {s['school_name']:<34} "
            f"{s['total_items']:>3} meals  {_cents(s['total_cost_cents'])}"
        )


_STUDENT_ESC_LABELS = {
    "dietary_unconfirmed": "confirm dietary (unknown)",
    "no_safe_meal": "no safe meal",
}


def _print_student_escalations(c: dict) -> None:
    """Per-student escalations — stragglers who get NO line and need a human."""
    students = c.get("escalated_students") or []
    if not students:
        return
    print(f"  per-student esc .... {len(students)} student(s) need a human (no line):")
    for st in students:
        label = _STUDENT_ESC_LABELS.get(st["reason"], st["reason"])
        esc = f"esc #{st['escalation_id']}" if st.get("escalation_id") else "queued"
        print(f"      ! [{label}] {st['student_name']} ({esc}) — {st['detail']}")


def main() -> int:
    week_of = orders_batch.upcoming_monday(date.today())
    run_id = _open_run(week_of)
    print(f"Run {run_id}: composing Thursday batch for week of {week_of.isoformat()}\n", flush=True)

    result = orders_batch.compose_week(week_of, run_id=run_id)
    if not result.ok:
        print(f"Batch failed: {result.status} — {result.message}", file=sys.stderr)
        _close_run(run_id, f"failed: {result.status} — {result.message}")
        return 1

    caterers = result.data["caterers"]
    total_meals = sum(c["total_items"] for c in caterers)
    total_cost = sum(c["total_cost_cents"] for c in caterers)
    composed = sum(1 for c in caterers if c["status"] == "composed")
    escalated = sum(1 for c in caterers if c["status"] == "escalated")

    for c in caterers:
        _print_caterer(c)

    print("=" * 78)
    print(
        f"BATCH TOTAL: {len(caterers)} caterers ({composed} auto-composed, "
        f"{escalated} escalated) | {total_meals} meals | {_cents(total_cost)}"
    )
    _close_run(
        run_id,
        f"week_of={week_of.isoformat()} caterers={len(caterers)} "
        f"composed={composed} escalated={escalated} "
        f"meals={total_meals} cost_cents={total_cost}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
