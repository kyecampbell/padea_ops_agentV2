-- 005: add a reversible `active` flag to the case-book.
--
-- The cockpit's Manage Lessons tab lets the operator DISABLE a lesson (case)
-- without losing it: an inactive case is skipped by recall_cases (so it no longer
-- influences the agent) but stays in the table and can be re-enabled. This is the
-- reversible alternative to DELETE.
--
-- Backfills existing rows to TRUE (DEFAULT true), so prior cases keep recalling.
-- Reversible: DROP COLUMN public.cases.active restores the old behaviour.

ALTER TABLE public.cases
    ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
COMMENT ON COLUMN public.cases.active IS 'Whether the case is live in recall. Operator may DISABLE (set false) a lesson from the cockpit; recall_cases skips inactive cases. Reversible.';

-- recall_cases only ever scans recent cases; an index keeps the active filter cheap.
CREATE INDEX IF NOT EXISTS idx_cases_active_created ON public.cases (active, created_at DESC);
