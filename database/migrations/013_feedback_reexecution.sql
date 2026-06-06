-- 013: feedback-driven re-execution (close the loop).
--
-- Today an operator comment is stored as a decision_annotation (and a lesson) and
-- then PHASES OUT — nothing acts on a rejection-with-explanation. This migration
-- adds the state the feedback sweep needs to surface un-actioned operator feedback,
-- process each comment exactly once, and re-run the original task with the feedback
-- as context (back through the existing approval gate — gates are unchanged).
--
-- See src/agent/feedback.py (the sweep) and config/handbook.md (the policy).
-- Additive only: ALTER ... ADD COLUMN IF NOT EXISTS + one lookup value + a backfill.

-- agent_runs: persist the incident prompt so a re-run can faithfully replay it,
-- plus the re-run lineage and a bounded redo depth (the ~2-attempt cap lives here:
-- a re-run is parent depth + 1; the 3rd rejection escalates instead of looping).
ALTER TABLE public.agent_runs
    ADD COLUMN IF NOT EXISTS task           text,
    ADD COLUMN IF NOT EXISTS parent_run_id  bigint REFERENCES public.agent_runs(id),
    ADD COLUMN IF NOT EXISTS feedback_depth integer NOT NULL DEFAULT 0;
COMMENT ON COLUMN public.agent_runs.task IS 'The incident prompt, stored so feedback-driven re-runs can replay it with operator feedback appended (NULL for runs that predate this column).';
COMMENT ON COLUMN public.agent_runs.parent_run_id IS 'For a feedback_rerun: the run this corrects.';
COMMENT ON COLUMN public.agent_runs.feedback_depth IS 'Redo-chain depth: 0 = original; a re-run is parent depth + 1. Bounds redo attempts (see src/agent/feedback.py _REDO_CAP).';

-- decision_annotations: the handling-state for the feedback sweep. handled_at NULL
-- = un-actioned (the work-check surfaces it); the sweep claims each row exactly once
-- via a conditional UPDATE on handled_at, so nothing requiring action is dropped or
-- processed twice.
ALTER TABLE public.decision_annotations
    ADD COLUMN IF NOT EXISTS intent        text,
    ADD COLUMN IF NOT EXISTS handled_at    timestamptz,
    ADD COLUMN IF NOT EXISTS outcome       text,
    ADD COLUMN IF NOT EXISTS redo_run_id   bigint REFERENCES public.agent_runs(id),
    ADD COLUMN IF NOT EXISTS redo_attempts integer NOT NULL DEFAULT 0;
COMMENT ON COLUMN public.decision_annotations.intent IS 'LLM-classified operator-comment intent: INSTRUCTION (act now) / LESSON (general, future) / BOTH / UNCLEAR (escalate to ask).';
COMMENT ON COLUMN public.decision_annotations.handled_at IS 'When the feedback sweep processed this comment (NULL = un-actioned). Set under a conditional UPDATE so each comment is handled exactly once.';
COMMENT ON COLUMN public.decision_annotations.outcome IS 'How the sweep handled it: re_ran / lesson_only / escalated_unclear / escalated_stuck / noop / legacy.';
COMMENT ON COLUMN public.decision_annotations.redo_run_id IS 'The feedback_rerun incident this comment triggered, if any.';
COMMENT ON COLUMN public.decision_annotations.redo_attempts IS 'How many redo attempts this comment has caused (bounded by src/agent/feedback.py _REDO_CAP).';

-- A queued email the operator rejected becomes TERMINAL, so it can never also be
-- approved + sent — the no-double-send guarantee. The corrected draft is a fresh
-- queued_for_approval row produced by the re-run.
INSERT INTO public.email_status (code, label, description, sort_order) VALUES
    ('rejected', 'Rejected', 'Operator rejected the queued draft; superseded by a feedback re-run', 70)
ON CONFLICT (code) DO NOTHING;

-- Backfill: existing annotations predate the feedback sweep — mark them handled so
-- the first sweep does not reprocess historical comments as if newly un-actioned.
UPDATE public.decision_annotations
   SET handled_at = COALESCE(handled_at, created_at),
       outcome    = COALESCE(outcome, 'legacy')
 WHERE handled_at IS NULL;
