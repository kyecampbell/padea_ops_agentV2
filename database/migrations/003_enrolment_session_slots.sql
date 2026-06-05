-- 003: per-student session rostering.
--
-- A student no longer implicitly attends every session at their school. This
-- junction records which session_slot(s) at their school a student is rostered
-- to. The Thursday batch composes a student's meals from their ROSTERED sessions
-- only, and each session's cohort is the set of students rostered to it (so a
-- multi-session school's caterer order now differs per session). PK on
-- (enrolment_id, session_slot_id); a student may be rostered to >1 session.
-- Data is generated deterministically (keyed on enrolment id) by
-- scripts/build_session_rosters.py and captured into the demo seed.

CREATE TABLE IF NOT EXISTS public.enrolment_session_slots (
    enrolment_id    bigint  NOT NULL REFERENCES public.enrolments(id),
    session_slot_id bigint  NOT NULL REFERENCES public.session_slots(id),
    PRIMARY KEY (enrolment_id, session_slot_id)
);
COMMENT ON TABLE public.enrolment_session_slots IS 'Per-student session roster: the session_slot(s) at their school a student attends. A student''s meals are composed from these sessions only; each session''s cohort is the students rostered to it.';

CREATE INDEX IF NOT EXISTS idx_ess_session_slot ON public.enrolment_session_slots (session_slot_id);
