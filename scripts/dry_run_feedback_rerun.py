"""DRY RUN — operator feedback drives re-execution (close the loop). NOTHING live.

Proves the new feedback sweep with EMAIL_MODE=dry and full cleanup, so the baseline
is untouched. The work-check surfaces UN-ACTIONED operator comments and handles each
exactly once:

  (a) REJECTION-with-explanation on a queued commercial email -> the agent RE-RUNS,
      a corrected version is RE-QUEUED for approval, and the original is marked
      'rejected' so it can never also send (no double-send).
  (b) A "from now on ..." LESSON comment -> stored to the case-book, NO re-run.
  (c) An AMBIGUOUS comment -> escalates to ask the operator, NO re-run.
  (d) A REPEATED rejection that has hit the redo cap -> escalates "tried twice,
      stuck", does NOT loop (no new re-run).

The intent classifier and the orchestrator re-run are INJECTED here as deterministic
stubs, so the proof exercises the REAL DB claim/idempotency, the REAL approval gate
verdict (gates.classify_email), the REAL supersede/no-double-send path, and the REAL
escalation path — without spending LLM calls or sending anything. In production the
sweep uses the cheap-model classifier and run_incident (the real gate) unchanged.

Run: EMAIL_MODE=dry uv run python scripts/dry_run_feedback_rerun.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

from config.settings import settings
from src.agent import feedback
from src.agent.gates import classify_email
from src.db.connection import fetch_all, get_conn
from src.tools import email as email_tool

SINK = "ops-feedback-dryrun@example.com"  # an .example address; demo would route to sink


# --- Scoped fixtures (all torn down in finally) -------------------------------


def _new_run(task: str, depth: int = 0) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (trigger_reason, task, notes, feedback_depth) "
            "VALUES ('feedback_dryrun', %s, %s, %s) RETURNING id",
            (task, "previous (rejected) conclusion", depth),
        )
        rid = cur.fetchone()[0]
        conn.commit()
    return int(rid)


def _queued_warning(run_id: int, body: str) -> int:
    """The ORIGINAL commercial action: a caterer warning. classify_email('warning')
    is requires_approval, so send_email parks it at queued_for_approval (even in
    dry mode) — the pending proposal a rejection comment then supersedes."""
    res = email_tool.send_email(
        email_type="warning", to=SINK, subject="Formal warning — repeated late delivery",
        body=body, related_run_id=run_id,
    )
    return int(res.data["email_id"])


def _comment(run_id: int, text: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO decision_annotations (run_id, comment, author) "
            "VALUES (%s, %s, 'kye') RETURNING id",
            (run_id, text),
        )
        aid = cur.fetchone()[0]
        conn.commit()
    return int(aid)


# --- Injected stubs (deterministic; no LLM) -----------------------------------


def _classify_stub(comment: str, situation: str, was_rejection: bool) -> str:
    """Deterministic stand-in for the cheap-model classifier, keyed on the comment.
    Mirrors what the real classifier would return for each scenario."""
    low = comment.lower()
    if "soften" in low or "still too harsh" in low:
        return "INSTRUCTION"          # (a) and (d): rejection-with-explanation
    if "from now on" in low:
        return "LESSON"               # (b)
    return "UNCLEAR"                  # (c)


def _plan_stub(comment: str, situation: str, candidates: list) -> list:
    """Deterministic stand-in for the lesson decomposer: one create op carrying the
    whole comment (no LLM). The decompose/dedupe/merge judgment itself is proved in
    scripts/dry_run_lesson_consolidation.py; here we only need a lesson stored."""
    return [feedback.LessonOp(lesson=comment.strip(), situation=situation, tags=(), merge_into=None)]


def _runner_stub(
    trigger_reason: str, task: str, call_cap: int | None = None,
    extra_context: dict[str, Any] | None = None,
    parent_run_id: int | None = None, feedback_depth: int = 0,
) -> SimpleNamespace:
    """Stand-in for run_incident: opens a real feedback_rerun row and re-issues the
    corrected commercial action through the SAME path the gate uses — send_email on
    a 'warning' self-queues at queued_for_approval, i.e. re-queued for approval, not
    sent. Returns a RunResult-shaped object."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (trigger_reason, task, parent_run_id, feedback_depth, notes) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (trigger_reason, task, parent_run_id, feedback_depth, "corrected: softened warning, re-queued"),
        )
        run_id = int(cur.fetchone()[0])
        conn.commit()
    # The corrected action — back through the gate (warning is requires_approval).
    email_tool.send_email(
        email_type="warning", to=SINK,
        subject="A note about recent deliveries", related_run_id=run_id,
        body="Hi team — a gentle heads-up about a couple of late deliveries; "
             "let's find a fix together. (corrected, softened per operator)",
    )
    return SimpleNamespace(run_id=run_id, final_text="corrected decision", step_count=1)


# --- Helpers ------------------------------------------------------------------


def _email_status(email_id: int) -> str:
    return fetch_all("SELECT status FROM outbound_emails WHERE id = %s", (email_id,))[0][0]


def _queued_emails_for(run_id: int) -> list[int]:
    return [r[0] for r in fetch_all(
        "SELECT id FROM outbound_emails WHERE related_run_id = %s AND status = 'queued_for_approval'",
        (run_id,))]


def main() -> int:
    if settings.email_mode != "dry":
        print(f"Refusing to run with EMAIL_MODE={settings.email_mode!r} — re-run as "
              "`EMAIL_MODE=dry uv run python scripts/dry_run_feedback_rerun.py`.", file=sys.stderr)
        return 1

    print("=" * 90)
    print(f"DRY RUN — operator feedback drives re-execution   (EMAIL_MODE=dry, redo cap={feedback._REDO_CAP})")
    print("=" * 90)
    print(f"\nGate check: classify_email('warning') -> {classify_email('warning')!r}  "
          "(commercial => re-runs re-queue, never auto-send)")

    created_runs: list[int] = []
    created_annotations: list[int] = []
    results: dict[int, feedback.ProcessedFeedback] = {}

    try:
        # --- Build the four un-actioned comments -----------------------------
        # (a) rejection of a queued warning
        run_a = _new_run("Draft a formal warning to Coastal Catering for repeated late delivery.")
        created_runs.append(run_a)
        email_a = _queued_warning(run_a, "This is unacceptable. Formal warning: repeated breach.")
        ann_a = _comment(run_a, "Reject — this is far too harsh for a first issue. SOFTEN it to a friendly heads-up.")
        created_annotations.append(ann_a)

        # (b) general lesson, no pending proposal
        run_b = _new_run("Reply to a parent confirming a meal change.")
        created_runs.append(run_b)
        ann_b = _comment(run_b, "From now on, always thank the parent in the first line of any reply.")
        created_annotations.append(ann_b)

        # (c) ambiguous comment, no pending proposal
        run_c = _new_run("Send the weekly parent reminder.")
        created_runs.append(run_c)
        ann_c = _comment(run_c, "Hmm, not sure about this one.")
        created_annotations.append(ann_c)

        # (d) repeated rejection that has already hit the redo cap (depth == cap)
        run_d = _new_run("Re-drafted warning (2nd correction).", depth=feedback._REDO_CAP)
        created_runs.append(run_d)
        email_d = _queued_warning(run_d, "Softened warning v2.")
        ann_d = _comment(run_d, "Still too harsh. SOFTEN further.")
        created_annotations.append(ann_d)

        unactioned_before = fetch_all(
            "SELECT count(*) FROM decision_annotations WHERE handled_at IS NULL")[0][0]
        print(f"\nWork-check sees {unactioned_before} un-actioned comment(s). Sweeping...\n")

        # --- One sweep handles all four, exactly once ------------------------
        handled = feedback.sweep_feedback(classify=_classify_stub, runner=_runner_stub, plan=_plan_stub)
        results = {h.annotation_id: h for h in handled}

        # --- Idempotency: a second sweep finds nothing to do -----------------
        second = feedback.sweep_feedback(classify=_classify_stub, runner=_runner_stub, plan=_plan_stub)

        # --- Gather state for assertions -------------------------------------
        ra, rb, rc, rd = results[ann_a], results[ann_b], results[ann_c], results[ann_d]
        # (a) corrected re-queued email under the re-run; original superseded.
        corrected_a = _queued_emails_for(ra.redo_run_id) if ra.redo_run_id else []
        if ra.redo_run_id:
            created_runs.append(ra.redo_run_id)
        original_a_status = _email_status(email_a)
        # no-double-send: approving the rejected original must be refused.
        resend = email_tool.send_queued_email(email_a, approved_by="kye")
        # (d) cap -> escalated, NO new run spawned.
        d_reruns = fetch_all("SELECT count(*) FROM agent_runs WHERE parent_run_id = %s", (run_d,))[0][0]

        # --- Report ----------------------------------------------------------
        print("-" * 90)
        print("(a) REJECTION -> RE-RUN, corrected re-queued, original superseded (no double-send)")
        print("-" * 90)
        print(f"   intent={ra.intent}  outcome={ra.outcome}  re-run id={ra.redo_run_id}")
        print(f"   original warning email {email_a}: status now '{original_a_status}'  (was queued_for_approval)")
        print(f"   corrected email re-queued under re-run: {corrected_a}  (status queued_for_approval)")
        print(f"   approve-original attempt -> status={resend.status}: {resend.message[:70]}...")

        print("\n" + "-" * 90)
        print("(b) LESSON ('from now on ...') -> case-book, NO re-run")
        print("-" * 90)
        print(f"   intent={rb.intent}  outcome={rb.outcome}  lesson case id={rb.lesson_case_id}  re-run id={rb.redo_run_id}")

        print("\n" + "-" * 90)
        print("(c) AMBIGUOUS -> escalate to ask, NO re-run")
        print("-" * 90)
        print(f"   intent={rc.intent}  outcome={rc.outcome}  escalation id={rc.escalation_id}  re-run id={rc.redo_run_id}")

        print("\n" + "-" * 90)
        print(f"(d) REPEATED rejection at cap (depth={feedback._REDO_CAP}) -> escalate 'stuck', does NOT loop")
        print("-" * 90)
        print(f"   intent={rd.intent}  outcome={rd.outcome}  escalation id={rd.escalation_id}  re-runs spawned={d_reruns}")

        print(f"\n   idempotency: 2nd sweep handled {len(second)} comment(s) (expected 0)")

        # --- Verdict ---------------------------------------------------------
        checks = {
            "(a) re-ran": ra.outcome == "re_ran" and ra.redo_run_id is not None,
            "(a) corrected re-queued": len(corrected_a) == 1,
            "(a) original superseded -> 'rejected'": original_a_status == "rejected",
            "(a) no double-send (approve refused)": resend.status == "conflict",
            "(b) lesson only, no re-run": rb.outcome == "lesson_only" and rb.lesson_case_id and rb.redo_run_id is None,
            "(c) escalated unclear, no re-run": rc.outcome == "escalated_unclear" and rc.escalation_id and rc.redo_run_id is None,
            "(d) escalated stuck, no loop": rd.outcome == "escalated_stuck" and d_reruns == 0,
            "idempotent 2nd sweep (0 handled)": len(second) == 0,
        }
        print("\n" + "=" * 90)
        print("RESULT")
        print("=" * 90)
        for label, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
        all_ok = all(checks.values())
        print("\n  Nothing sent live; nothing left written; no push." if all_ok else "\n  SOME CHECKS FAILED.")
        return 0 if all_ok else 1

    finally:
        # Teardown — FK-safe order. redo runs were appended to created_runs above.
        rerun_ids = [r[0] for r in fetch_all(
            "SELECT id FROM agent_runs WHERE parent_run_id = ANY(%s)", (created_runs,))]
        all_runs = list({*created_runs, *rerun_ids})
        lesson_ids = [cid for h in results.values() for cid in h.lesson_case_ids]
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM outbound_emails WHERE related_run_id = ANY(%s)", (all_runs,))
            cur.execute("DELETE FROM escalations WHERE run_id = ANY(%s)", (all_runs,))
            cur.execute("DELETE FROM decision_annotations WHERE id = ANY(%s)", (created_annotations,))
            if lesson_ids:
                cur.execute("DELETE FROM cases WHERE id = ANY(%s)", (lesson_ids,))
            cur.execute("DELETE FROM agent_runs WHERE id = ANY(%s)", (all_runs,))
            conn.commit()
        print("\n   (cleaned up proof runs, emails, annotations, lessons, escalations — baseline restored)")


if __name__ == "__main__":
    sys.exit(main())
