"""Thursday batch as an AGENT-SUPERVISED incident (the orchestrator drives it).

Unlike a hard-coded pipeline, the weekly batch is handed to the orchestrator as
one incident. The agent calls the four deterministic Thursday tools IN ORDER, and
between each it ASSESSES the typed result, CONFIRMS success, and on any
empty / error / unavailable it HOLDS (stops) and escalates to a human instead of
charging ahead:

  1. compose_week ................ compose + persist every caterer's safe order;
  2. apply_flexible_resolution ... flip dietary-known non-responders to flexible,
                                   then compose_week again if any changed;
  3. send_prefs_requests ......... one-time prefs request to first-time defaults;
  4. send_caterer_orders ......... exactly one order email per caterer (the full
                                   per-session student manifests).

The tools stay deterministic + idempotent — one bounded call each, no per-email
looping — and the agent NEVER hand-sends a caterer order (send_caterer_orders is
the only path). A DB failure mid-batch surfaces as a typed `unavailable`, the
agent holds, and the run ends with an escalation rather than a half-sent batch.

This performs LIVE sends (demo-routed). Use scripts/dry_run_thursday.py to preview
the composed orders + emails first, and to see the tool sequence with NOTHING sent.

Run: uv run python scripts/run_thursday_incident.py [YYYY-MM-DD]
"""

from __future__ import annotations

import json
import sys
from datetime import date

from src.agent.loop import run_incident
from src.db.connection import fetch_all
from src.tools import orders_batch

# A multi-step batch (compose, maybe re-compose, prefs, orders, plus assessing
# each result) needs more reasoning turns than a single inbound email.
_CALL_CAP = 14


def _task(week_of: date) -> str:
    """The incident brief: run the four Thursday tools in order, assess each,
    confirm success, and HOLD + escalate on any failure."""
    return (
        f"Run the weekly Thursday batch for the week beginning Monday "
        f"{week_of.isoformat()} (pass week_of='{week_of.isoformat()}' to every tool).\n\n"
        "This is a deterministic, idempotent batch. Call each tool EXACTLY ONCE in "
        "this order — never loop per email, per caterer, or per student — and after "
        "each call ASSESS its typed result and CONFIRM it succeeded before moving on:\n\n"
        "1. compose_week — compose + persist every caterer's safe order. If the "
        "result is unavailable or error, HOLD: do not proceed; escalate_to_human "
        "with the failure and stop. Note any per-student or caterer-wide escalations "
        "it reports.\n"
        "2. apply_flexible_resolution — resolve dietary-known non-responders. If it "
        "resolved one or more enrolment ids, call compose_week ONCE more so the "
        "re-composed week reflects them. On unavailable/error, HOLD + escalate.\n"
        "3. send_prefs_requests — send the one-time prefs requests to first-time "
        "defaulted students. On unavailable/error, HOLD + escalate; otherwise note "
        "how many were sent vs skipped.\n"
        "4. send_caterer_orders — send exactly one order email per caterer. Do NOT "
        "hand-send caterer orders yourself with send_email; this tool is the only "
        "way. Inspect its result: if any caterer is in 'failed', escalate_to_human "
        "with the details. Report the caterers sent and skipped.\n\n"
        "Only proceed to the next step once the current one has succeeded. Finish "
        "with a short summary: composed caterers, flexible resolutions, prefs "
        "requests sent, caterer orders sent, and anything you held or escalated."
    )


def print_outbound(run_id: int) -> None:
    """Print the outbound emails this run logged (status + intended recipient)."""
    rows = fetch_all(
        """
        SELECT id, email_type, status, intended_to_address, subject,
               gmail_message_id IS NOT NULL AS actually_sent
        FROM outbound_emails WHERE related_run_id = %s ORDER BY id
        """,
        (run_id,),
    )
    if not rows:
        print("  (no outbound emails)")
        return
    for eid, etype, status, to, subject, sent in rows:
        print(f"  #{eid} [{etype}] -> {to}  [{'SENT (demo-routed)' if sent else status}]")
        print(f"       {subject}")


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

    print(f"Thursday batch (AGENT-SUPERVISED, LIVE) — week of {week_of.isoformat()}")
    result = run_incident("thursday_batch", _task(week_of), call_cap=_CALL_CAP)

    print(f"\nagent_runs.id = {result.run_id}  ({result.step_count} tool step(s))\n")
    print("=== agent tool sequence ===")
    print_steps(result.run_id)
    print("\n=== outbound emails (this run) ===")
    print_outbound(result.run_id)
    print("\n=== final answer ===")
    print(result.final_text or "(no final text)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
