-- 006: lesson-citation traceability — record which recalled lessons a decision used.
--
-- The context assembler surfaces recalled cases to the model as "[Lesson #<id>]",
-- and the handbook asks the model to note "(applying Lesson #<id>: <why>)" in its
-- reasoning whenever a lesson actually shaped a decision. The orchestrator parses
-- those citations out of the step reasoning / final answer and records ONLY the
-- lessons it cited as used (not everything that was recalled) here, so each
-- decision in the cockpit can show the precedent it leaned on.
--
-- Grain: one row per (decision, lesson) the run cited. step_id is the decision the
-- citation was attached to; NULL means the run's final answer (no single step).
-- Reversible: DROP TABLE public.step_lesson_citations.

CREATE TABLE IF NOT EXISTS public.step_lesson_citations (
    id          bigserial    PRIMARY KEY,
    run_id      bigint       NOT NULL REFERENCES public.agent_runs(id),
    step_id     bigint       REFERENCES public.agent_steps(id),
    case_id     bigint       NOT NULL REFERENCES public.cases(id),
    reason      text,
    created_at  timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.step_lesson_citations IS 'Lesson-citation traceability: the recalled cases a run actually CITED as used (parsed from "(applying Lesson #<id>: <why>)" in the reasoning), with the why. step_id = the cited decision/step; NULL = the run''s final answer. Only cited-as-used, not everything recalled.';

-- A lesson is cited at most once per run (first decision that cites it wins); this
-- partial unique index dedupes run-level (final-answer) citations where step_id IS NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_slc_run_case
    ON public.step_lesson_citations (run_id, case_id);

CREATE INDEX IF NOT EXISTS idx_slc_step ON public.step_lesson_citations (step_id);
CREATE INDEX IF NOT EXISTS idx_slc_case ON public.step_lesson_citations (case_id);
