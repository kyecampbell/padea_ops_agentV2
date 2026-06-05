-- 008: policy-citation traceability — record which active policies a decision applied.
--
-- Parallel to 006 (step_lesson_citations). The context assembler injects every
-- ACTIVE policy as "[Policy #<id>]", and the handbook asks the model to note
-- "(applying Policy #<id>: <why>)" in its reasoning whenever a policy actually
-- governed a decision. The orchestrator parses those citations out of the step
-- reasoning / final answer and records ONLY the policies it cited as applied
-- (validated against the policies that were genuinely in context), so each
-- decision in the cockpit can show the authoritative rule it followed.
--
-- Grain: one row per (decision, policy) the run cited. step_id is the decision the
-- citation was attached to; NULL means the run's final answer (no single step).
-- Reversible: DROP TABLE public.step_policy_citations.

CREATE TABLE IF NOT EXISTS public.step_policy_citations (
    id          bigserial    PRIMARY KEY,
    run_id      bigint       NOT NULL REFERENCES public.agent_runs(id),
    step_id     bigint       REFERENCES public.agent_steps(id),
    policy_id   bigint       NOT NULL REFERENCES public.policies(id),
    reason      text,
    created_at  timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.step_policy_citations IS 'Policy-citation traceability: the active policies a run actually CITED as applied (parsed from "(applying Policy #<id>: <why>)" in the reasoning), with the why. step_id = the cited decision/step; NULL = the run''s final answer. Only cited-as-applied, not every policy in context.';

-- A policy is cited at most once per run (first decision that cites it wins).
CREATE UNIQUE INDEX IF NOT EXISTS idx_spc_run_policy
    ON public.step_policy_citations (run_id, policy_id);

CREATE INDEX IF NOT EXISTS idx_spc_step   ON public.step_policy_citations (step_id);
CREATE INDEX IF NOT EXISTS idx_spc_policy ON public.step_policy_citations (policy_id);
