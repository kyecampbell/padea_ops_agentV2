-- 010: add the 'student' feedback_source.
--
-- Kids now rate the meal they just had as part of the weekly choose-and-rate
-- email. Their rating + free-text comment is stored as a feedback row with
-- source='student' (alongside the existing 'tutor' and 'manager' sources), so it
-- feeds the same caterer quality signal (get_caterer_feedback) — a
-- consumer-defined quality measure. The schema's feedback_source COMMENT already
-- anticipated this ("parent/student can be added later").
--
-- Reference/lookup value only — data, not structural DDL; reset_demo leaves lookup
-- tables intact.

INSERT INTO public.feedback_source (code, label, description, sort_order) VALUES
    ('student', 'Student', 'Per-meal rating + comment submitted by the student in the weekly choose-and-rate email', 30)
ON CONFLICT (code) DO NOTHING;
