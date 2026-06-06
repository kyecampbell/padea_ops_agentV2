"""Run ONE work-check cycle (manual / cron entrypoint).

The work-check has two triggers and runs them in order:
  1. INBOUND mail — ``src.tools.inbound.poll_inbox`` reasons about and handles each
     NEW message (identify, act/queue, reply-for-confirmation, or escalate).
  2. UN-ACTIONED operator feedback — ``src.agent.feedback.sweep_feedback`` surfaces
     any operator comment that hasn't been handled and processes each exactly once
     (re-run on an instruction/rejection, store a lesson, or escalate if unclear).

For each item it prints what was handled and the tool steps any incident logged.
Already-seen messages and already-handled comments are skipped silently (idempotent).

Exits non-zero if any item failed mid-processing.

Run: uv run python scripts/run_inbox_once.py
"""

from __future__ import annotations

import sys

from src.agent.feedback import sweep_feedback
from src.db.connection import fetch_all
from src.tools.inbound import poll_inbox, poll_topology


def _print_steps(run_id: int) -> None:
    """Print the tool calls the incident logged, in order."""
    rows = fetch_all(
        """
        SELECT step_index, tool_name, action_class,
               tool_output_full->>'status'  AS result_status,
               tool_input
        FROM agent_steps WHERE run_id = %s ORDER BY step_index
        """,
        (run_id,),
    )
    if not rows:
        print("    steps: (none — agent answered without tool calls)")
        return
    print("    steps:")
    for step_index, tool_name, action_class, result_status, tool_input in rows:
        print(
            f"      #{step_index} {tool_name} "
            f"[{action_class}] -> {result_status}  input={tool_input}"
        )


def report_processed(processed: list) -> int:
    """Print each processed email and its decision; return the failure count."""
    failures = 0
    for i, p in enumerate(processed, start=1):
        e = p.email
        print(f"=== [{i}/{len(processed)}] message {e.gmail_message_id} ===")
        print(f"  From:    {e.from_address}")
        print(f"  Subject: {e.subject}")
        print(f"  Received:{e.received_at.isoformat()}")

        if p.error:
            failures += 1
            print(f"  ERROR (not recorded, will retry next poll): {p.error}\n")
            continue

        print(f"  Classified as: {p.classified_as}")
        print(
            "  Related ids:   "
            f"enrolment={p.related_enrolment_id} order={p.related_order_id}"
        )
        print(f"  Run id:        {p.run_id}  ({p.step_count} step(s))")
        if p.run_id is not None:
            _print_steps(p.run_id)
        print(f"  Agent decision:\n    {p.final_text or '(no final text)'}\n")

    return failures


def report_feedback(handled: list) -> int:
    """Print each handled operator comment and its outcome; return the failure count."""
    failures = 0
    for i, h in enumerate(handled, start=1):
        print(f"=== feedback [{i}/{len(handled)}] annotation {h.annotation_id} "
              f"(run {h.run_id}) ===")
        print(f"  Intent:  {h.intent}")
        print(f"  Outcome: {h.outcome}")
        if h.redo_run_id is not None:
            print(f"  Re-ran:  run {h.redo_run_id}")
            _print_steps(h.redo_run_id)
        if h.lesson_case_id is not None:
            print(f"  Lesson:  case {h.lesson_case_id}")
        if h.escalation_id is not None:
            print(f"  Escalated: escalation {h.escalation_id}")
        if h.error:
            failures += 1
            print(f"  ERROR: {h.error}")
        print()
    return failures


def main() -> int:
    print("Work-check (one cycle).")
    print(f"  topology: {poll_topology()}\n")

    print("[1/2] Polling inbox...")
    processed = poll_inbox()
    if not processed:
        print("  No new messages (everything already seen or inbox empty).")
        inbox_failures = 0
    else:
        inbox_failures = report_processed(processed)
        print(f"  {len(processed) - inbox_failures} processed, {inbox_failures} failed.")

    print("\n[2/2] Sweeping un-actioned operator feedback...")
    handled_fb = sweep_feedback()
    if not handled_fb:
        print("  No un-actioned operator feedback.")
        fb_failures = 0
    else:
        fb_failures = report_feedback(handled_fb)
        print(f"  {len(handled_fb) - fb_failures} handled, {fb_failures} failed.")

    failures = inbox_failures + fb_failures
    print(f"\nDone. {failures} failure(s).")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
