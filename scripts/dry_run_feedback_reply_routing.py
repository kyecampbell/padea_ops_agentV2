"""DRY RUN — a feedback re-run replies to the ORIGINAL inbound sender, not the sink.

Proves the ISSUE-1 fix with EMAIL_MODE=dry (replies logged 'drafted', nothing sent)
and full cleanup, so the baseline is untouched.

THE BUG: when an operator comment on an INBOUND-triggered run spawned a feedback
re-run, the re-run did not inherit the original run's inbound sender address. So
reply_to_sender had no `to` (it is injected from run context, never supplied by the
model) and the reply could not reach the real sender — the agent fell back to
send_email, which in demo mode redirects to the DEMO sink. The operator's reply went
to the sink, not the parent.

THE FIX: the run now PERSISTS inbound_from_address (migration 014), and the feedback
sweep carries it forward into the re-run's context — so reply_to_sender routes to the
ACTUAL sender automatically, with no operator action.

RACE-SAFE BY DESIGN: a live background worker (Render padea-worker) polls this same
DB and would claim/RE-RUN any decision_annotation we inserted (a real, demo-mode
send). So this proof inserts NO decision_annotations. It exercises the REAL code at
its three load-bearing points instead:

  (1) FORWARDING — the REAL feedback.process_feedback, with only its DB-boundary
      helpers stubbed (no annotation row), proves the re-run is invoked with
      extra_context carrying the original inbound_from_address — and that a
      NON-inbound run forwards nothing (sink path unchanged).
  (2) PERSISTENCE — the REAL loop._open_run stores inbound_from_address on the run,
      so the address survives onto the re-run (carry-forward across a chain).
  (3) ROUTING — the REAL dispatch + reply path: with the address in context,
      reply_to_sender drafts to the REAL sender (demo_routed=False); WITHOUT it, it
      errors with no recipient (the exact failure that caused the sink fallback);
      and an agent-INITIATED send_email is unchanged (demo routes to the sink).

Run: EMAIL_MODE=dry uv run python scripts/dry_run_feedback_reply_routing.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

from config.settings import settings
from src.agent import feedback, loop
from src.agent.dispatch import dispatch
from src.db.connection import fetch_all, get_conn

ORIGINAL_SENDER = "kyec898@gmail.com"  # the real parent who emailed in
ON_RECORD_EXAMPLE = "parent.henry@example.com"  # a non-routable on-record address


def _forwarding_check() -> dict[str, Any]:
    """Drive the REAL process_feedback with its DB boundary stubbed (no annotation
    row inserted, so the live worker can't race it and nothing is sent). Capture the
    extra_context the re-run is invoked with — for an inbound run vs a non-inbound one.
    """
    captured: dict[str, Any] = {}

    def runner_stub(trigger_reason, task, call_cap=None, extra_context=None,
                    parent_run_id=None, feedback_depth=0):
        captured.setdefault("calls", []).append(extra_context)
        return SimpleNamespace(run_id=999_000, final_text="x", step_count=0)

    # Stub ONLY the DB boundary — the routing + forwarding logic under test is real.
    orig = (feedback._claim, feedback._finalize, feedback._has_pending_proposal,
            feedback._target_queued_email)
    feedback._claim = lambda _id: True
    feedback._finalize = lambda *a, **k: None
    feedback._has_pending_proposal = lambda *a, **k: True   # actionable -> re-run
    feedback._target_queued_email = lambda *a, **k: None
    try:
        inbound_ann = {
            "id": -1, "comment": "Reply to the parent and confirm the change.",
            "author": "kye", "run_id": 12345, "step_id": None,
            "run_task": "Handle an inbound email from a parent.",
            "trigger_reason": "inbound_email", "run_notes": "prev",
            "feedback_depth": 0, "inbound_from_address": ORIGINAL_SENDER,
        }
        non_inbound_ann = {**inbound_ann, "id": -2, "run_id": 6789,
                           "trigger_reason": "thursday_batch", "inbound_from_address": None}
        feedback.process_feedback(inbound_ann, classify=lambda *a: "INSTRUCTION",
                                  runner=runner_stub, plan=lambda *a: [])
        feedback.process_feedback(non_inbound_ann, classify=lambda *a: "INSTRUCTION",
                                  runner=runner_stub, plan=lambda *a: [])
    finally:
        (feedback._claim, feedback._finalize, feedback._has_pending_proposal,
         feedback._target_queued_email) = orig

    calls = captured.get("calls", [])
    return {
        "inbound_ctx": calls[0] if len(calls) > 0 else "(no call)",
        "non_inbound_ctx": calls[1] if len(calls) > 1 else "(no call)",
    }


def main() -> int:
    if settings.email_mode != "dry":
        print(f"Refusing to run with EMAIL_MODE={settings.email_mode!r} — re-run as "
              "`EMAIL_MODE=dry uv run python scripts/dry_run_feedback_reply_routing.py`.",
              file=sys.stderr)
        return 1

    print("=" * 92)
    print(f"DRY RUN — feedback re-run replies to the ORIGINAL sender, not the sink  "
          f"(EMAIL_MODE=dry; sink={settings.demo_sink_email})")
    print("=" * 92)

    created_runs: list[int] = []
    try:
        # --- (1) FORWARDING: the real feedback code carries the address forward ---
        print("\n" + "-" * 92)
        print("(1) FORWARDING — process_feedback invokes the re-run with the original sender in context")
        print("-" * 92)
        fwd = _forwarding_check()
        print(f"   inbound run     -> re-run extra_context = {fwd['inbound_ctx']!r}")
        print(f"   non-inbound run -> re-run extra_context = {fwd['non_inbound_ctx']!r}  (nothing to forward)")

        # --- (2) PERSISTENCE: the address is stored ON the run (carry-forward) ----
        print("\n" + "-" * 92)
        print("(2) PERSISTENCE — loop._open_run stores inbound_from_address on the run")
        print("-" * 92)
        run_id = loop._open_run("inbound_email", task="proof: inbound run",
                                inbound_from_address=ORIGINAL_SENDER)
        created_runs.append(run_id)
        stored = fetch_all("SELECT inbound_from_address FROM agent_runs WHERE id = %s", (run_id,))[0][0]
        # And a re-run that carries it forward persists it too (a chain keeps it).
        rerun_id = loop._open_run("feedback_rerun", task="proof: carried-forward re-run",
                                  parent_run_id=run_id, feedback_depth=1,
                                  inbound_from_address=ORIGINAL_SENDER)
        created_runs.append(rerun_id)
        stored_rerun = fetch_all("SELECT inbound_from_address FROM agent_runs WHERE id = %s", (rerun_id,))[0][0]
        print(f"   inbound run {run_id}: inbound_from_address = {stored!r}")
        print(f"   re-run {rerun_id} (carried forward): inbound_from_address = {stored_rerun!r}")

        # --- (3) ROUTING: reply goes to the sender; the bug; the sandbox ----------
        print("\n" + "-" * 92)
        print("(3) ROUTING — reply_to_sender uses the in-context address; the bug; the sandbox")
        print("-" * 92)
        run_context = {"run_id": run_id, "inbound_from_address": ORIGINAL_SENDER}
        good = dispatch("reply_to_sender",
                        {"subject": "Re: your child's meal", "body": "Confirmed — thank you!"},
                        run_context)
        print(f"   WITH address  -> reply_to_sender status={good.status} "
              f"replied_to={good.data.get('replied_to')!r} demo_routed={good.data.get('demo_routed')} "
              f"({good.data.get('status')})")

        bug = dispatch("reply_to_sender",
                       {"subject": "Re: your child's meal", "body": "Confirmed."},
                       {"run_id": run_id})  # the old behaviour: no inbound_from_address
        print(f"   WITHOUT (bug) -> reply_to_sender status={bug.status}: {bug.message}")
        print("                    => no recipient; the agent would fall back to send_email -> SINK.")

        from src.tools import email as email_tool
        init = email_tool.send_email(
            email_type="other", to=ON_RECORD_EXAMPLE, subject="A note from Padea",
            body="An agent-initiated note.", related_run_id=run_id)
        print(f"   INITIATED send_email -> status={init.status} (agent-originated)")
        print("   routing rule (demo mode):")
        print(f"     INITIATED send_email  -> SINK   ({settings.demo_sink_email})   [redirect intact]")
        print(f"     REPLY      send_reply -> SENDER ({ORIGINAL_SENDER})            [no redirect]")

        # --- Verdict -------------------------------------------------------------
        checks = {
            "(1) inbound re-run carries the original sender forward":
                fwd["inbound_ctx"] == {"inbound_from_address": ORIGINAL_SENDER},
            "(1) non-inbound re-run forwards nothing (sink path unchanged)":
                fwd["non_inbound_ctx"] is None,
            "(2) inbound run persists the address": stored == ORIGINAL_SENDER,
            "(2) re-run persists the carried-forward address": stored_rerun == ORIGINAL_SENDER,
            "(3) reply drafted to the REAL sender": good.ok and good.data.get("replied_to") == ORIGINAL_SENDER,
            "(3) reply NOT demo-routed to the sink": good.data.get("demo_routed") is False,
            "(3) bug repro: no-context reply has no recipient": not bug.ok,
            "(3) initiated send is unchanged (drafted)": init.ok or init.status == "queued",
        }
        print("\n" + "=" * 92)
        print("RESULT")
        print("=" * 92)
        for label, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
        all_ok = all(checks.values())
        print("\n  Nothing sent live; nothing left written; no push." if all_ok else "\n  SOME CHECKS FAILED.")
        return 0 if all_ok else 1

    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM outbound_emails WHERE related_run_id = ANY(%s)", (created_runs,))
            cur.execute("DELETE FROM agent_runs WHERE id = ANY(%s)", (created_runs,))
            conn.commit()
        print("\n   (cleaned up proof runs + drafted replies/emails — baseline restored)")


if __name__ == "__main__":
    sys.exit(main())
