"""Feedback-driven re-execution — the second work trigger (close the loop).

Today an operator comment is stored as a ``decision_annotation`` and then PHASES
OUT: a rejection-with-explanation never gets acted on. This module makes the
work-check surface not just NEW inbound (``src.tools.inbound.poll_inbox``) but any
UN-ACTIONED operator feedback, and process each comment exactly once:

  1. CLASSIFY the comment's intent with the cheap model —
     INSTRUCTION (act on this now) / LESSON (general, for the future) / BOTH /
     UNCLEAR (escalate to ask). A comment on a still-pending proposal (a
     rejection-with-explanation) is actionable and defaults to INSTRUCTION.
  2. RE-RUN on an instruction / rejection: re-invoke the ORIGINAL task with the
     operator's feedback appended as binding context, back THROUGH THE EXISTING
     GATES (a corrected commercial action re-queues; nothing irreversible
     auto-fires). The superseded queued email is marked ``rejected`` first, so the
     original can never also send — no double-send. BOUNDED: the redo chain is
     capped at ``_REDO_CAP`` attempts (``agent_runs.feedback_depth``); the next
     rejection escalates "tried twice, stuck" instead of looping.
  3. LESSON / BOTH still writes the operator's guidance to the case-book (capture
     unchanged); UNCLEAR escalates to ask what they meant.

Idempotency: ``handled_at`` is set under a conditional UPDATE that CLAIMS the row
(``WHERE handled_at IS NULL``); a second sweep finds it already handled and skips
it, so nothing requiring action is dropped or done twice.

Gates are unchanged — the re-run flows through ``run_incident`` exactly like any
other incident. The classifier uses the cheap model (bookkeeping only); the re-run
itself uses the orchestrator (sonnet) via ``run_incident``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import anthropic

from config.settings import settings
from src.agent.loop import run_incident
from src.db.connection import fetch_all, get_conn
from src.tools.casebook import recall_cases, store_case, update_case
from src.tools.escalate import escalate_to_human

logger = logging.getLogger(__name__)

# The cheap model classifies the comment's intent — bookkeeping, not the decision.
_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
_CLASSIFIER_MAX_TOKENS = 256

_INTENTS = ("INSTRUCTION", "LESSON", "BOTH", "UNCLEAR")

# The cheap model also DECOMPOSES a lesson comment into its distinct atomic
# lessons and decides, per lesson, whether it restates/extends an existing one
# (merge) or is genuinely new (create) — bookkeeping, not the decision.
_LESSON_MODEL = "claude-haiku-4-5-20251001"
_LESSON_MAX_TOKENS = 1024
# How many existing operator-feedback lessons to surface as merge candidates.
# Bounded so the decompose prompt stays small; recall ranks them by relevance.
_DEDUPE_CANDIDATES = 8

# How many redo attempts a single line of feedback may spawn before we stop and
# escalate "tried twice, stuck" instead of looping. The original run is depth 0;
# each re-run is parent depth + 1. depth >= _REDO_CAP means the cap is reached.
_REDO_CAP = 2


class _Runner(Protocol):
    """The re-run entry point — ``run_incident`` in production, a stub in proofs."""

    def __call__(
        self,
        trigger_reason: str,
        task: str,
        call_cap: int | None = ...,
        extra_context: dict[str, Any] | None = ...,
        parent_run_id: int | None = ...,
        feedback_depth: int = ...,
    ) -> Any: ...


# --- Pure routing (no LLM, no DB) — unit-testable -----------------------------


@dataclass(frozen=True, slots=True)
class Route:
    """What to do with one comment, given its intent + redo depth.

    ``rerun`` re-invokes the original task; ``lesson`` stores a case; ``escalate``
    is ``None`` / ``"unclear"`` (ask what they meant) / ``"stuck"`` (cap hit)."""

    rerun: bool
    lesson: bool
    escalate: str | None


def decide_route(
    intent: str, depth: int, was_rejection: bool, cap: int = _REDO_CAP
) -> Route:
    """Map (intent, redo depth, is-this-a-rejection) -> the action to take.

    A rejection-with-explanation is always actionable (defaults to INSTRUCTION),
    even if the free-text intent came back UNCLEAR. An actionable comment re-runs —
    UNLESS the redo chain has hit the cap, in which case it escalates "stuck". A
    pure LESSON only writes a case; an UNCLEAR non-rejection escalates to ask.
    """
    intent = (intent or "").strip().upper()
    actionable = was_rejection or intent in ("INSTRUCTION", "BOTH")
    want_lesson = intent in ("LESSON", "BOTH")

    if actionable:
        if depth >= cap:
            # Re-ran the cap's worth of attempts and it's still being rejected.
            return Route(rerun=False, lesson=want_lesson, escalate="stuck")
        return Route(rerun=True, lesson=want_lesson, escalate=None)
    if want_lesson:
        return Route(rerun=False, lesson=True, escalate=None)
    return Route(rerun=False, lesson=False, escalate="unclear")


# --- Intent classification (cheap model) --------------------------------------


def classify_intent(comment: str, situation: str, was_rejection: bool) -> str:
    """Label the operator comment as exactly one of ``_INTENTS`` (cheap model).

    Reads the comment and the decision it was made against. A rejection-with-
    explanation defaults to INSTRUCTION (and is usually BOTH). On any failure we
    fail SAFE: a rejection still defaults to INSTRUCTION (so it gets acted on); a
    plain comment defaults to UNCLEAR (so it escalates to ask rather than guess).
    """
    fallback = "INSTRUCTION" if was_rejection else "UNCLEAR"
    tool = {
        "name": "record_intent",
        "description": "Record the single best intent for this operator comment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": list(_INTENTS),
                    "description": (
                        "INSTRUCTION = act on this specific decision now. "
                        "LESSON = general guidance for the future, no redo. "
                        "BOTH = act now AND a lasting lesson. "
                        "UNCLEAR = ambiguous; a human should clarify."
                    ),
                }
            },
            "required": ["intent"],
        },
    }
    rejection_note = (
        "This comment was made on a still-pending proposal, so it is a "
        "REJECTION-with-explanation: default to INSTRUCTION (usually BOTH).\n"
        if was_rejection
        else ""
    )
    user = (
        "Classify the operator's comment on an agent decision as exactly ONE intent.\n"
        f"{rejection_note}\n"
        f"What the agent was doing:\n{situation}\n\n"
        f"Operator comment:\n{comment}"
    )
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=_CLASSIFIER_MAX_TOKENS,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_intent"},
            messages=[{"role": "user", "content": user}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_intent":
                intent = block.input.get("intent")
                if intent in _INTENTS:
                    return intent
    except Exception as exc:  # the classifier is bookkeeping; never break the sweep.
        logger.warning("feedback classify failed: %s", exc)
    return fallback


# --- Lesson decomposition + dedupe/merge (cheap model) ------------------------


@dataclass(frozen=True, slots=True)
class LessonOp:
    """One atomic lesson the planner extracted from a comment, with its dedupe
    verdict. ``merge_into`` is an existing case id to UPDATE/merge into, or None to
    create a new case. ``lesson`` is the (consolidated, when merging) guidance."""

    lesson: str
    situation: str | None
    tags: tuple[str, ...]
    merge_into: int | None


def _lesson_candidates(comment: str) -> list[dict]:
    """Existing operator-feedback lessons most relevant to this comment — the pool
    the planner may merge into. Keyword-ranked and bounded; empty on a miss/error
    (so dedupe simply degrades to always-create rather than breaking the sweep)."""
    res = recall_cases(query=comment, tags=["operator-feedback"], limit=_DEDUPE_CANDIDATES)
    if not res.ok:
        return []
    return [
        {"id": c["id"], "situation": c.get("situation"), "decision": c.get("decision")}
        for c in res.data
    ]


def plan_lessons(comment: str, situation: str, candidates: list[dict]) -> list[LessonOp]:
    """Decompose one operator comment into its DISTINCT atomic lessons, and decide
    per lesson whether it restates/extends an existing one (merge) or is new (create).

    Uses the cheap model with a forced tool call. The model is asked to split a
    genuinely multi-point comment into separate lessons (e.g. "check the DB before
    trusting a parent's claim" AND "reply to the real sender" = two), WITHOUT
    fragmenting one coherent thought into near-duplicates and WITHOUT over-splitting.
    For each lesson it sets ``merge_into_case_id`` to a candidate id when the lesson
    restates/extends it (supplying the CONSOLIDATED wording), else null to create.

    On any failure / empty result we fall back to ONE create op carrying the whole
    comment — the prior single-lesson behaviour, so capture is never lost.
    """
    if not (comment or "").strip():
        return []
    fallback = [LessonOp(lesson=comment.strip(), situation=situation, tags=(), merge_into=None)]
    cand_ids = {int(c["id"]) for c in candidates}

    tool = {
        "name": "record_lessons",
        "description": "Record the distinct atomic lessons contained in this operator comment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lessons": {
                    "type": "array",
                    "description": (
                        "One entry per DISTINCT atomic lesson. Usually exactly 1 — "
                        "only emit more when the comment genuinely carries separate, "
                        "independent lessons. Never fragment one coherent thought."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "lesson": {
                                "type": "string",
                                "description": (
                                    "The atomic, self-contained guidance/postulate. "
                                    "When merging, give the CONSOLIDATED wording that "
                                    "should REPLACE the existing lesson."
                                ),
                            },
                            "situation": {
                                "type": "string",
                                "description": "Short 'when this applies' context for the lesson.",
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "A few keyword tags to aid future recall.",
                            },
                            "merge_into_case_id": {
                                "type": ["integer", "null"],
                                "description": (
                                    "The id of an existing candidate lesson this "
                                    "restates or extends (merge into it), or null to "
                                    "create a genuinely new lesson."
                                ),
                            },
                        },
                        "required": ["lesson"],
                    },
                }
            },
            "required": ["lessons"],
        },
    }
    if candidates:
        cand_text = "\n".join(
            f"- #{c['id']}: {(c.get('decision') or c.get('situation') or '').strip()[:200]}"
            for c in candidates
        )
        cand_note = (
            "EXISTING lessons (merge into one of these by id if a lesson restates or "
            f"extends it; otherwise create new):\n{cand_text}\n\n"
        )
    else:
        cand_note = "There are no existing lessons yet — every lesson is new.\n\n"
    user = (
        "Decompose the operator's comment into its DISTINCT atomic lessons for the "
        "agent's case-book. Split only genuinely separate lessons; do NOT fragment a "
        "single coherent thought, and do NOT over-split. For each lesson, decide "
        "whether it restates/extends an existing lesson (merge) or is new (create).\n\n"
        f"{cand_note}"
        f"What the agent was doing:\n{situation}\n\n"
        f"Operator comment:\n{comment}"
    )
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=_LESSON_MODEL,
            max_tokens=_LESSON_MAX_TOKENS,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_lessons"},
            messages=[{"role": "user", "content": user}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_lessons":
                ops: list[LessonOp] = []
                for item in block.input.get("lessons") or []:
                    text = str(item.get("lesson") or "").strip()
                    if not text:
                        continue
                    raw_merge = item.get("merge_into_case_id")
                    # Only honour a merge into a candidate we actually surfaced —
                    # a hallucinated / stale id falls back to create.
                    merge_into = (
                        int(raw_merge)
                        if isinstance(raw_merge, int) and int(raw_merge) in cand_ids
                        else None
                    )
                    tags = tuple(
                        str(t).strip() for t in (item.get("tags") or []) if str(t).strip()
                    )
                    sit = str(item.get("situation") or "").strip() or None
                    ops.append(
                        LessonOp(lesson=text, situation=sit, tags=tags, merge_into=merge_into)
                    )
                return ops or fallback
    except Exception as exc:  # decomposition is bookkeeping; never break the sweep.
        logger.warning("feedback lesson planning failed: %s", exc)
    return fallback


# --- DB reads / claim / writes ------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcessedFeedback:
    """The outcome of handling one un-actioned comment."""

    annotation_id: int
    run_id: int | None
    intent: str | None
    outcome: str
    redo_run_id: int | None = None
    # The primary lesson (first) for back-compat; ``lesson_case_ids`` carries every
    # case touched (one comment may yield several atomic lessons), and
    # ``lesson_actions`` records whether each was created or merged.
    lesson_case_id: int | None = None
    lesson_case_ids: tuple[int, ...] = ()
    lesson_actions: tuple[str, ...] = ()
    escalation_id: int | None = None
    error: str | None = None


def _unactioned() -> list[dict]:
    """Un-actioned comments, oldest first, with their target run's replay context.

    ``handled_at IS NULL`` is the work-list. The LEFT JOIN pulls the original
    prompt / trigger / notes / redo depth so a re-run can replay the task and so
    the cap can be evaluated."""
    rows = fetch_all(
        """
        SELECT a.id, a.comment, a.author, a.run_id, a.step_id,
               r.task        AS run_task,
               r.trigger_reason,
               r.notes       AS run_notes,
               COALESCE(r.feedback_depth, 0) AS feedback_depth,
               r.inbound_from_address
        FROM decision_annotations a
        LEFT JOIN agent_runs r ON r.id = a.run_id
        WHERE a.handled_at IS NULL
        ORDER BY a.id
        """
    )
    cols = (
        "id", "comment", "author", "run_id", "step_id",
        "run_task", "trigger_reason", "run_notes", "feedback_depth",
        "inbound_from_address",
    )
    return [dict(zip(cols, row)) for row in rows]


def _claim(annotation_id: int) -> bool:
    """Atomically claim a comment for processing (exactly-once).

    Sets ``handled_at`` only while it is still NULL; a concurrent sweep that lost
    the race gets 0 rows back and skips the comment."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE decision_annotations SET handled_at = now() "
            "WHERE id = %s AND handled_at IS NULL RETURNING id",
            (annotation_id,),
        )
        claimed = cur.fetchone() is not None
        conn.commit()
    return claimed


def _finalize(
    annotation_id: int,
    intent: str | None,
    outcome: str,
    redo_run_id: int | None,
    redo_attempts: int,
) -> None:
    """Record how the comment was handled (``handled_at`` was set by ``_claim``)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE decision_annotations "
            "SET intent = %s, outcome = %s, redo_run_id = %s, redo_attempts = %s "
            "WHERE id = %s",
            (intent, outcome, redo_run_id, redo_attempts, annotation_id),
        )
        conn.commit()


def _target_queued_email(run_id: int | None, step_id: int | None) -> int | None:
    """A queued-for-approval email this comment is rejecting, if any.

    Prefer one tied to the commented step; else the run's queued email. We only
    supersede an unambiguous single match — anything else stays untouched."""
    if step_id is not None:
        rows = fetch_all(
            "SELECT id FROM outbound_emails "
            "WHERE related_step_id = %s AND status = 'queued_for_approval'",
            (step_id,),
        )
        if len(rows) == 1:
            return int(rows[0][0])
    if run_id is not None:
        rows = fetch_all(
            "SELECT id FROM outbound_emails "
            "WHERE related_run_id = %s AND status = 'queued_for_approval'",
            (run_id,),
        )
        if len(rows) == 1:
            return int(rows[0][0])
    return None


def _has_pending_proposal(run_id: int | None, step_id: int | None) -> bool:
    """True if the run/step has a still-pending action — i.e. the comment is a
    rejection-with-explanation. A pending action is a queued-for-approval email or
    a requires-approval write proposal that was logged but not applied."""
    if _target_queued_email(run_id, step_id) is not None:
        return True
    if step_id is not None:
        rows = fetch_all(
            "SELECT 1 FROM agent_steps "
            "WHERE id = %s AND action_class = 'requires_approval' "
            "AND COALESCE(tool_output_full->'data'->>'applied', 'false') = 'false'",
            (step_id,),
        )
        if rows:
            return True
    if run_id is not None:
        rows = fetch_all(
            "SELECT 1 FROM agent_steps "
            "WHERE run_id = %s AND action_class = 'requires_approval' "
            "AND COALESCE(tool_output_full->'data'->>'applied', 'false') = 'false' "
            "LIMIT 1",
            (run_id,),
        )
        if rows:
            return True
    return False


def _supersede_email(email_id: int) -> bool:
    """Mark a rejected queued email TERMINAL (``rejected``), so the approve path
    can never also send it. Only flips a row still ``queued_for_approval``."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE outbound_emails "
            "SET status = 'rejected', failed_at = now(), "
            "    failure_reason = 'Superseded by operator rejection; a corrected draft was re-queued by the feedback re-run.' "
            "WHERE id = %s AND status = 'queued_for_approval' RETURNING id",
            (email_id,),
        )
        ok = cur.fetchone() is not None
        conn.commit()
    return ok


def _supersede_write_step(step_id: int) -> bool:
    """Mark a rejected requires-approval write proposal superseded, so the apply
    path can never also re-apply it (no double-action). Only flips a proposal that
    is still un-applied; the corrected write is re-proposed by the re-run."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_steps "
            "SET tool_output_full = jsonb_set("
            "      COALESCE(tool_output_full, '{}'::jsonb), '{data,superseded}', 'true'::jsonb, true) "
            "WHERE id = %s AND action_class = 'requires_approval' "
            "AND COALESCE(tool_output_full->'data'->>'applied', 'false') = 'false' "
            "RETURNING id",
            (step_id,),
        )
        ok = cur.fetchone() is not None
        conn.commit()
    return ok


def _situation(ann: dict) -> str:
    """A one-line description of what the agent was doing when commented on —
    used as the lesson's situation and the escalation's context."""
    rid = ann.get("run_id")
    if rid is None:
        return "Operator comment with no linked run (decision UI)."
    notes = (ann.get("run_notes") or "").strip()
    base = f"Run {rid} (trigger: {ann.get('trigger_reason')})."
    if notes:
        base += f" Previous conclusion: {notes[:300]}"
    return base


def _build_rerun_task(ann: dict) -> str:
    """The corrected-task prompt: the original prompt + the operator's feedback as
    a binding correction. Falls back to a reconstructed prompt for legacy runs that
    predate the stored ``task``."""
    original = (ann.get("run_task") or "").strip()
    if not original:
        original = (
            "(The original prompt was not recorded.) "
            f"Trigger: {ann.get('trigger_reason')}. "
            f"Your previous conclusion was: {(ann.get('run_notes') or '(none)')[:500]}"
        )
    author = ann.get("author") or "the operator"
    return (
        "You are RE-RUNNING a task because the operator rejected / corrected your "
        "previous decision. Redo it, treating the feedback below as binding.\n\n"
        "--- ORIGINAL TASK ---\n"
        f"{original}\n"
        "--- END ORIGINAL TASK ---\n\n"
        f"--- OPERATOR FEEDBACK ON YOUR PREVIOUS ATTEMPT (run #{ann.get('run_id')}) ---\n"
        f"{author} reviewed your previous decision and gave this correction:\n"
        f"\"{ann.get('comment')}\"\n"
        "--- END FEEDBACK ---\n\n"
        "Produce the CORRECTED decision. Your previous attempt's actions are NOT "
        "automatically re-applied; the approval gate still applies to every action "
        "(a corrected commercial action re-queues for approval; nothing irreversible "
        "auto-fires). Do NOT repeat the specific action the operator rejected. "
        "Conclude with a short summary of what you changed and why."
    )


def _apply_lessons(
    ann: dict,
    situation: str,
    plan: Callable[[str, str, list[dict]], list[LessonOp]] = plan_lessons,
) -> list[dict]:
    """Capture the operator's guidance as one OR MORE retrievable lessons.

    Decomposes the comment into its distinct atomic lessons (``plan``), then for
    each: MERGES into an existing lesson when the planner flagged one (so a
    restated/extended lesson updates in place — no duplicate), else stores a new
    case. Consolidate, don't fragment. Returns one record per applied lesson:
    ``{case_id, action ('created'|'merged'), lesson}``. ``plan`` is injectable so
    proofs can drive it deterministically.
    """
    comment = str(ann.get("comment") or "")
    candidates = _lesson_candidates(comment)
    cand_ids = {int(c["id"]) for c in candidates}

    try:
        ops = plan(comment, situation, candidates)
    except Exception:  # planning is bookkeeping; fall back to one whole-comment lesson.
        logger.exception("lesson planning raised; falling back to single lesson")
        ops = [LessonOp(lesson=comment.strip(), situation=situation, tags=(), merge_into=None)]

    base_tags = ["operator-feedback"]
    if ann.get("trigger_reason"):
        base_tags.append(str(ann["trigger_reason"]))

    applied: list[dict] = []
    for op in ops:
        tags = base_tags + [t for t in op.tags if t and t not in base_tags]
        sit = op.situation or situation
        if op.merge_into in cand_ids:
            merged = update_case(
                op.merge_into,
                situation=sit,
                decision=op.lesson,
                rationale="Operator feedback merged into an existing lesson (feedback sweep).",
                tags=tags,
            )
            if merged.ok:
                applied.append(
                    {"case_id": op.merge_into, "action": "merged", "lesson": op.lesson}
                )
                continue
            # The target vanished (e.g. deleted between recall and write) — create.
        stored = store_case(
            situation=sit,
            decision=op.lesson,
            rationale="Operator feedback captured via the decision UI (feedback sweep).",
            tags=tags,
            related_run_id=ann.get("run_id"),
            created_by=ann.get("author"),
        )
        if stored.ok:
            applied.append(
                {"case_id": stored.data["case_id"], "action": "created", "lesson": op.lesson}
            )
    return applied


# --- The sweep ----------------------------------------------------------------


def process_feedback(
    ann: dict,
    *,
    classify: Callable[[str, str, bool], str] = classify_intent,
    runner: _Runner = run_incident,
    plan: Callable[[str, str, list[dict]], list[LessonOp]] = plan_lessons,
) -> ProcessedFeedback | None:
    """Handle one un-actioned comment, exactly once.

    Claims the row; if the claim is lost (already handled), returns None. Otherwise
    classifies intent, routes it, performs the action(s), and records the outcome.
    ``classify`` / ``runner`` / ``plan`` are injectable so proofs can drive
    deterministic paths.
    """
    annotation_id = int(ann["id"])
    if not _claim(annotation_id):
        return None  # another sweep already handled it.

    run_id = ann.get("run_id")
    step_id = ann.get("step_id")
    depth = int(ann.get("feedback_depth") or 0)
    situation = _situation(ann)
    target_email = _target_queued_email(run_id, step_id)
    was_rejection = _has_pending_proposal(run_id, step_id)

    intent = classify(str(ann.get("comment") or ""), situation, was_rejection)
    route = decide_route(intent, depth, was_rejection)

    redo_run_id: int | None = None
    lessons: list[dict] = []
    escalation_id: int | None = None
    redo_attempts = 0
    outcome = "noop"

    try:
        # An instruction with nothing to re-run against can't be acted on — ask.
        if route.rerun and not ann.get("run_id"):
            esc = escalate_to_human(
                "Operator gave an instruction but it isn't tied to a run we can "
                f"re-execute. Please action it directly. Comment: {ann.get('comment')!r}",
                context={"annotation_id": annotation_id},
            )
            escalation_id = esc.data.get("escalation_id") if esc.ok else None
            outcome = "escalated_unclear"

        elif route.rerun:
            if target_email is not None:
                _supersede_email(target_email)  # no double-send of the original.
            if step_id is not None:
                _supersede_write_step(step_id)  # no double-apply of a rejected write.
            redo_attempts = depth + 1
            # Carry the ORIGINAL run's inbound sender forward, so a re-run of an
            # inbound incident replies to the REAL sender automatically (without
            # it, reply_to_sender has no address and the agent falls back to
            # send_email -> the demo sink). NULL for non-inbound runs (no change).
            rerun_context: dict[str, Any] | None = None
            inbound_from = ann.get("inbound_from_address")
            if inbound_from:
                rerun_context = {"inbound_from_address": inbound_from}
            result = runner(
                trigger_reason="feedback_rerun",
                task=_build_rerun_task(ann),
                extra_context=rerun_context,
                parent_run_id=int(ann["run_id"]),
                feedback_depth=redo_attempts,
            )
            redo_run_id = int(result.run_id)
            outcome = "re_ran"

        elif route.escalate == "stuck":
            esc = escalate_to_human(
                f"I re-ran this task {depth} times on operator feedback and the "
                "corrected version is still being rejected — I'm stuck. Please "
                f"handle it directly. Latest comment: {ann.get('comment')!r}",
                context={"annotation_id": annotation_id, "redo_depth": depth},
                related_run_id=ann.get("run_id"),
                related_step_id=step_id,
            )
            escalation_id = esc.data.get("escalation_id") if esc.ok else None
            outcome = "escalated_stuck"

        elif route.escalate == "unclear":
            esc = escalate_to_human(
                "Operator comment intent is unclear — please clarify what you'd "
                f"like done. Comment: {ann.get('comment')!r}",
                context={"annotation_id": annotation_id},
                related_run_id=ann.get("run_id"),
                related_step_id=step_id,
            )
            escalation_id = esc.data.get("escalation_id") if esc.ok else None
            outcome = "escalated_unclear"

        # LESSON / BOTH: decompose into distinct atomic lessons, merging restatements
        # into existing lessons (no duplicates) and creating only what's genuinely
        # new. For a lesson-only comment this is the whole outcome.
        if route.lesson:
            lessons = _apply_lessons(ann, situation, plan)
            if outcome == "noop":
                outcome = "lesson_only"
    except Exception as exc:  # one bad comment must not sink the sweep.
        logger.exception("feedback processing failed for annotation %s", annotation_id)
        outcome = "error"
        _finalize(annotation_id, intent, outcome, redo_run_id, redo_attempts)
        return ProcessedFeedback(
            annotation_id=annotation_id, run_id=run_id, intent=intent,
            outcome=outcome, redo_run_id=redo_run_id, error=str(exc),
        )

    lesson_case_ids = tuple(int(l["case_id"]) for l in lessons)
    lesson_actions = tuple(str(l["action"]) for l in lessons)
    _finalize(annotation_id, intent, outcome, redo_run_id, redo_attempts)
    logger.info(
        "feedback handled annotation=%s intent=%s outcome=%s redo_run=%s lessons=%s",
        annotation_id, intent, outcome, redo_run_id, list(zip(lesson_case_ids, lesson_actions)),
    )
    return ProcessedFeedback(
        annotation_id=annotation_id, run_id=run_id, intent=intent, outcome=outcome,
        redo_run_id=redo_run_id,
        lesson_case_id=(lesson_case_ids[0] if lesson_case_ids else None),
        lesson_case_ids=lesson_case_ids, lesson_actions=lesson_actions,
        escalation_id=escalation_id,
    )


def sweep_feedback(
    *,
    classify: Callable[[str, str, bool], str] = classify_intent,
    runner: _Runner = run_incident,
    plan: Callable[[str, str, list[dict]], list[LessonOp]] = plan_lessons,
) -> list[ProcessedFeedback]:
    """Run one feedback sweep: handle every un-actioned operator comment once.

    The parallel of ``poll_inbox`` for the operator-feedback trigger. Returns what
    was handled (skipped/already-claimed comments are omitted)."""
    handled: list[ProcessedFeedback] = []
    for ann in _unactioned():
        result = process_feedback(ann, classify=classify, runner=runner, plan=plan)
        if result is not None:
            handled.append(result)
    return handled
