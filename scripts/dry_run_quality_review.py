"""DRY RUN of the quality channels — runs the real agent, SENDS NOTHING.

Forces EMAIL_MODE=dry (every autonomous email is logged 'drafted', Gmail is never
called) and then drives the two quality channels through the REAL orchestrator so
you can see exactly what it WOULD do:

  1. the weekly_quality_review incident — the agent reviews each caterer's recent
     feedback and decides per the handbook quality policy (expected: flag the
     declining caterer with a draft-to-Dylan + escalation, handle a minor issue
     with a caterer_service_note, leave the healthy caterers alone);
  2. one simulated INBOUND COMPLAINT email — a parent reporting a bad meal — handled
     exactly as the live inbound poll would (acknowledge the parent + take action).

Nothing is sent: drafts land in outbound_emails with status 'drafted', and the
escalations / agent_steps are the audit trail. Re-runnable; it only adds rows.

Run: uv run python scripts/dry_run_quality_review.py [weeks]
"""

from __future__ import annotations

# Force the dry email mode BEFORE any src import loads the settings singleton.
# An actual env var takes precedence over the .env file in pydantic-settings, so
# this guarantees the run cannot send live mail regardless of .env.
import os

os.environ["EMAIL_MODE"] = "dry"

import sys
from datetime import datetime, timezone

from config.settings import settings
from src.agent.loop import run_incident
from src.db.connection import fetch_all
from src.tools.inbound import InboundEmail, _format_task

# Sibling module in scripts/ (this dir is sys.path[0] when run directly).
import run_quality_review as qr


def _guard_dry() -> None:
    """Hard stop unless we are genuinely in dry mode — never send live from here."""
    if settings.email_mode != "dry":
        print(
            f"REFUSING TO RUN: EMAIL_MODE is {settings.email_mode!r}, not 'dry'. "
            "This preview must not send live mail.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _complaint_email() -> InboundEmail:
    """A synthetic parent complaint about a bad meal (Terrific Noodles, John Paul
    College). Identifiable from the body — the agent confirms identity with its
    query tools, never from the From address."""
    body = (
        "Hi,\n\n"
        "I wanted to flag that my son Lucas's dinner at the John Paul College "
        "session last night was cold when it arrived, and it turned up quite late "
        "so the kids were waiting around. Lucas didn't eat much of it. It's the "
        "first time we've had a problem and he usually loves the food — just wanted "
        "to let you know so it can be sorted.\n\n"
        "Thanks,\nHarper Anderson (Lucas Anderson's mum)"
    )
    return InboundEmail(
        gmail_message_id="DRYRUN-complaint-lucas-anderson",
        from_address="demo-relay@example.com",  # demo relay — NOT an identity signal
        to_address="ops@padea.example",
        subject="Cold dinner last night at John Paul College",
        body=body,
        received_at=datetime.now(timezone.utc),
    )


def _feedback_snapshot(weeks: int) -> None:
    """Print the per-caterer weekly trend the agent will reason over, so the dry
    run is self-contained (you see the evidence AND the decision)."""
    print("-" * 80)
    print(f"FEEDBACK TREND SNAPSHOT (weekly mean rating, last {weeks} weeks)")
    print("-" * 80)
    rows = fetch_all(
        """
        SELECT f.caterer_id, c.name,
               date_trunc('week', f.submitted_at)::date AS wk,
               count(*) AS n, round(avg(f.rating)::numeric, 2)::float AS avg,
               min(f.rating) AS mn
        FROM feedback f JOIN caterers c ON c.id = f.caterer_id
        WHERE f.rating IS NOT NULL
          AND f.submitted_at >= now() - make_interval(weeks => %s)
        GROUP BY 1, 2, 3 ORDER BY 1, 3
        """,
        (weeks,),
    )
    current = None
    for cid, name, wk, n, avg, mn in rows:
        if cid != current:
            print(f"\n  caterer {cid} — {name}")
            current = cid
        print(f"      {wk}  n={n:2}  mean={avg}  min={mn}")
    print()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    weeks = qr._DEFAULT_WEEKS
    if argv:
        try:
            weeks = max(1, int(argv[0]))
        except ValueError:
            print(f"Bad weeks {argv[0]!r}; expected an integer.", file=sys.stderr)
            return 1

    _guard_dry()

    print("=" * 80)
    print(f"DRY RUN — quality channels (EMAIL_MODE={settings.email_mode}; NOTHING WILL BE SENT)")
    print("=" * 80)

    _feedback_snapshot(weeks)

    # --- 1. Weekly quality review incident. ---
    print("#" * 80)
    print(f"# 1. WEEKLY QUALITY REVIEW  (reviewing each caterer, {weeks}-week window)")
    print("#" * 80)
    review = run_incident(qr.TRIGGER_REASON, qr.build_task(weeks), call_cap=qr._CALL_CAP)
    print(f"\nagent_runs.id = {review.run_id}  ({review.step_count} tool step(s))\n")
    qr.print_run(review.run_id)
    print("\n--- final answer ---")
    print(review.final_text or "(no final text)")

    # --- 2. Simulated inbound complaint. ---
    print("\n" + "#" * 80)
    print("# 2. SIMULATED INBOUND COMPLAINT  (one parent, bad meal)")
    print("#" * 80)
    email = _complaint_email()
    print(f"\n  From (relay): {email.from_address}")
    print(f"  Subject:      {email.subject}")
    print("  Body:")
    for line in email.body.splitlines():
        print(f"    | {line}")
    print()
    complaint = run_incident("inbound_email", _format_task(email), call_cap=10)
    print(f"\nagent_runs.id = {complaint.run_id}  ({complaint.step_count} tool step(s))\n")
    qr.print_run(complaint.run_id)
    print("\n--- final answer ---")
    print(complaint.final_text or "(no final text)")

    print("\n" + "=" * 80)
    print("DRY RUN COMPLETE — every email above is 'drafted' only. NOTHING was sent.")
    print("Review the drafts/escalations, then run scripts/run_quality_review.py to go live.")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
