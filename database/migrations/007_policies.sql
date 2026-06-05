-- 007: operator-authored policies — the editable BUSINESS-rule layer.
--
-- Policies are authoritative rules the operator writes and maintains from the
-- cockpit, parallel to the case-book of lessons but PRESCRIPTIVE rather than
-- recalled-by-similarity: every ACTIVE policy is injected into the agent's
-- always-on context (like the handbook), tagged with a visible "[Policy #<id>]",
-- and the agent treats it as binding. They sit ON TOP of the handbook, which
-- keeps the hard safety/operational invariants (dietary safety, the approval
-- gate, the order-state money line, demo routing) in code/handbook — NOT here.
--
-- Starts EMPTY: the operator fills it in. `active` mirrors cases.active (disable
-- without losing); `sort_order` lets the operator order how policies read in
-- context (lowest first, id as the tiebreak). updated_at is maintained by the
-- shared OPT-01 trigger so edits are timestamped.
-- Reversible: DROP TABLE public.policies (and its trigger).

CREATE TABLE IF NOT EXISTS public.policies (
    id          bigserial    PRIMARY KEY,
    text        text         NOT NULL,
    active      boolean      NOT NULL DEFAULT true,
    sort_order  integer      NOT NULL DEFAULT 0,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at  timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.policies IS 'Operator-authored authoritative business rules, injected into the agent''s always-on context as "[Policy #<id>]" (active ones only). The editable layer ON TOP of the handbook''s hard invariants. Disable via active=false (reversible); sort_order then id orders how they read in context.';

-- Context injection only ever scans active policies in sort order; this index
-- keeps that read cheap.
CREATE INDEX IF NOT EXISTS idx_policies_active_sort
    ON public.policies (active, sort_order, id);

-- Maintain updated_at on edit via the shared OPT-01 trigger (same as the six
-- core tables), so a policy edit is timestamped without app-side bookkeeping.
DROP TRIGGER IF EXISTS trg_policies_updated_at ON public.policies;
CREATE TRIGGER trg_policies_updated_at BEFORE UPDATE ON public.policies
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
