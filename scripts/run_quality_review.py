"""Weekly quality review as an AGENT-SUPERVISED incident (the orchestrator drives it).

The second weekly trigger, run ALONGSIDE the Thursday batch
(scripts/run_thursday_incident.py). Where the Thursday batch composes + sends the
week's orders, this incident reviews CATERING QUALITY: the agent looks at each
caterer's recent feedback (get_caterer_feedback) and, per the handbook's quality
policy, decides what — if anything — to do:

  - a minor, fixable issue (cold / late / one-off mix-up) -> a polite
    caterer_service_note to the caterer (autonomous);
  - accumulating evidence of a real DECLINE (a falling weekly trend, repeated
    comments, failed checks) -> draft the case to Dylan (operator_notification) and
    escalate_to_human — the last resort before a formal warning / RFP;
  - a caterer that is fine -> no action.

It NEVER auto-sends a formal warning/RFP/cancellation — those are commercial and
require operator approval. The run logs every decision to agent_runs / agent_steps,
and any escalation to the escalations table.

This performs LIVE sends (demo-routed unless EMAIL_MODE=live). Use
scripts/dry_run_quality_review.py to preview the decisions + drafted emails with
NOTHING sent (EMAIL_MODE=dry).

Run: uv run python scripts/run_quality_review.py [weeks]
"""

from __future__ import annotations

import json
import sys
from datetime import date

from config.settings import settings
from src.agent.loop import run_incident
from src.db.connection import fetch_all
from src.tools import orders_batch

# Reviewing four caterers — each a feedback read, a judgment, and possibly an
# action (note, or draft-to-Dylan + escalate) — needs more reasoning turns than a
# single inbound email.
_CALL_CAP = 18

# How far back the review looks. Wide enough that a decline reads as a TREND
# (good weeks -> bad weeks) rather than a single snapshot.
_DEFAULT_WEEKS = 8

TRIGGER_REASON = "weekly_quality_review"


def _caterer_roster() -> list[tuple[int, str]]:
    """The caterers to review (id, name), each with its assigned schools appended
    to the name for context."""
    return [
        (cid, name)
        for cid, name in fetch_all("SELECT id, name FROM caterers ORDER BY id")
    ]


def build_task(weeks: int = _DEFAULT_WEEKS) -> str:
    """The incident brief: review each caterer's recent feedback per the quality
    policy, decide proportionately, and act."""
    roster = _caterer_roster()
    lines = "\n".join(f"  - caterer {cid}: {name}" for cid, name in roster)
    return (
        "This is the WEEKLY QUALITY REVIEW, run alongside the Thursday batch. Review "
        "each caterer's recent service quality and act per the handbook's quality & "
        "satisfaction policy.\n\n"
        "Caterers to review:\n"
        f"{lines}\n\n"
        f"For EACH caterer, call get_caterer_feedback(caterer_id, weeks={weeks}) and "
        "read the TREND, not just the latest week: the weekly mean rating over time, "
        "the manager comments, and the failed quality checks. Then decide, "
        "proportionately:\n"
        "  - Caterer is fine (stable, good) -> no action; say so.\n"
        "  - A MINOR, fixable issue (cold / late / a one-off mix-up) -> send a polite "
        "caterer_service_note to that caterer (look up its contact email first). This "
        "is NOT a warning.\n"
        "  - Accumulating evidence of a real DECLINE (a falling weekly trend, repeated "
        "comments, recurring failed checks — especially a dietary-safety miss) -> "
        "assemble the accumulated evidence (the weekly trend + the specific comments + "
        "the failed checks) into an operator_notification email to Dylan, the "
        "operations owner (dylan.chern.operator@example.com), recommending action, AND raise an "
        "escalate_to_human. This is the last resort before a formal warning / RFP — "
        "which are Dylan's call, never yours. Do NOT send a warning/rfp/cancellation "
        "yourself.\n"
        "  - If recalled lessons conflict on a caterer, escalate rather than pick.\n\n"
        "Never knee-jerk a warning off one bad night.\n\n"
        "THEN, once you've reviewed everyone, send the warm weekly SCORECARDS in ONE "
        f"call: send_caterer_weekly_summaries(week_of='{orders_batch.upcoming_monday(date.today()).isoformat()}'). "
        "This sends one autonomous caterer_weekly_summary per caterer (praise-first, "
        "per-school student satisfaction, recurring themes, a gentle service note, and "
        "a capacity ask only for a clean strong performer) — idempotent, so a re-run "
        "sends none. It is SEPARATE from any decline escalation above: a scorecard is "
        "never a warning. Assess its result and HOLD + escalate if any send failed.\n\n"
        "Finish with a short per-caterer summary: the trend you saw, what you did "
        "(note / drafted-to-Dylan + escalated / no action), and that the scorecards went out."
    )


# --- Printing helpers (shared with the dry-run preview) ----------------------


def print_steps(run_id: int) -> None:
    """Print the tool sequence the agent actually drove (ordered)."""
    rows = fetch_all(
        """
        SELECT step_index, tool_name, tool_input,
               tool_output_full ->> 'status'  AS status,
               tool_output_full ->> 'message' AS message,
               action_class
        FROM agent_steps WHERE run_id = %s ORDER BY step_index
        """,
        (run_id,),
    )
    if not rows:
        print("  (no tool steps logged)")
        return
    for step_index, tool_name, tool_input, status, message, action_class in rows:
        print(f"  [{step_index}] {tool_name}({json.dumps(tool_input)})  [{action_class}]")
        print(f"        -> {status}: {message}")


def print_outbound(run_id: int) -> None:
    """Print the outbound emails this run logged, with full body for drafts/sends."""
    rows = fetch_all(
        """
        SELECT id, email_type, status, intended_to_address, subject, rendered_body,
               gmail_message_id IS NOT NULL AS actually_sent
        FROM outbound_emails WHERE related_run_id = %s ORDER BY id
        """,
        (run_id,),
    )
    if not rows:
        print("  (no outbound emails)")
        return
    for eid, etype, status, to, subject, body, sent in rows:
        if sent:
            disp = "SENT (demo-routed)" if settings.email_mode == "demo" else "SENT"
        elif status == "drafted":
            disp = "DRAFTED — NOT SENT (dry run)"
        elif status == "queued_for_approval":
            disp = "QUEUED FOR APPROVAL — NOT SENT"
        else:
            disp = status
        print(f"\n  ● #{eid} [{etype}] -> {to}   [{disp}]")
        print(f"      Subject: {subject}")
        print("      Body:")
        for line in (body or "").splitlines():
            print(f"        | {line}")


def print_escalations(run_id: int) -> None:
    """Print the escalations this run raised for a human."""
    rows = fetch_all(
        """
        SELECT id, status, question, related_caterer_id
        FROM escalations WHERE run_id = %s ORDER BY id
        """,
        (run_id,),
    )
    if not rows:
        print("  (no escalations raised)")
        return
    for eid, status, question, caterer_id in rows:
        tag = f" (caterer {caterer_id})" if caterer_id else ""
        print(f"  ! esc #{eid} [{status}]{tag} — {question}")


def print_run(run_id: int, header: str = "") -> None:
    """Print the full audit of one run: steps, outbound mail, escalations."""
    if header:
        print(header)
    print("=== agent tool sequence ===")
    print_steps(run_id)
    print("\n=== outbound emails (this run) ===")
    print_outbound(run_id)
    print("\n=== escalations (this run) ===")
    print_escalations(run_id)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    weeks = _DEFAULT_WEEKS
    if argv:
        try:
            weeks = max(1, int(argv[0]))
        except ValueError:
            print(f"Bad weeks {argv[0]!r}; expected an integer.", file=sys.stderr)
            return 1

    mode = settings.email_mode
    note = " (NOTHING SENT)" if mode == "dry" else (" (demo-routed)" if mode == "demo" else " (LIVE)")
    print(f"Weekly quality review (AGENT-SUPERVISED) — looking back {weeks} week(s); EMAIL_MODE={mode}{note}")

    result = run_incident(TRIGGER_REASON, build_task(weeks), call_cap=_CALL_CAP)

    print(f"\nagent_runs.id = {result.run_id}  ({result.step_count} tool step(s))\n")
    print_run(result.run_id)
    print("\n=== final answer ===")
    print(result.final_text or "(no final text)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
