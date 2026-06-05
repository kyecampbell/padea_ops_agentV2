-- 009: add enrolments.student_email — the student's contact for the weekly
-- CHOOSE-AND-RATE email.
--
-- Additive and nullable: the deployed Thursday/inbound code never reads this
-- column, so adding it is safe while the old code runs. A student with a blank
-- (NULL/empty) student_email is simply not emailed a weekly choice and falls back
-- to the normal compose-time assignment (rotation / preference / safe default).
-- In EMAIL_MODE=demo, the per-student demo address is set by
-- scripts/build_student_demo_email.py (one active student per session -> the demo
-- sink, everyone else blank) and captured into the demo seed.
--
-- Structural DDL: idempotent (IF NOT EXISTS) so re-running is harmless. reset_demo
-- restores the column's data from seed.sql; this DDL is run once at migrate time.

ALTER TABLE public.enrolments
    ADD COLUMN IF NOT EXISTS student_email text;

COMMENT ON COLUMN public.enrolments.student_email IS
    'The student''s own contact for the weekly choose-and-rate email. NULL/blank = not emailed; the student falls back to compose-time assignment.';
