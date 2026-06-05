"""Run ONE inbound poll cycle (manual / cron entrypoint).

Polls the real inbox once via ``src.tools.inbound.poll_inbox``, letting the
orchestrator reason about and handle each NEW message. For every message
processed it prints the email, the agent's classification + related ids, the
final decision text, and the tool steps the incident logged (so you can watch it
identify, act/queue, reply-for-confirmation, or escalate). Already-seen messages
are skipped silently (idempotent).

Exits non-zero if any message failed mid-processing.

Run: uv run python scripts/run_inbox_once.py
"""

from __future__ import annotations

import sys

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


def main() -> int:
    print("Polling inbox (one cycle)...")
    print(f"  topology: {poll_topology()}\n")
    processed = poll_inbox()

    if not processed:
        print("No new messages to process (everything already seen or inbox empty).")
        return 0

    failures = report_processed(processed)
    handled = len(processed) - failures
    print(f"Done. {handled} processed, {failures} failed.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
