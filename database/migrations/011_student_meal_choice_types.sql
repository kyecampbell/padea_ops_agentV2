-- 011: add the 'student_meal_choice' email + inbound classification kinds.
--
-- email_type 'student_meal_choice' — the weekly choose-and-rate email sent to a
-- student: "how was last week's meal?" (rating) + "pick your meal for <date>"
-- (the MOQ-bounded, dietary-safe options). It is a factual/operational message
-- (no commercial judgment), so classify_email -> autonomous (see
-- src/agent/gates.py). It is demo-routed like every other autonomous send.
--
-- inbound_classification 'student_meal_choice' — the record-keeping label for the
-- student's REPLY (their pick + rating), so the inbound dedup record reflects what
-- the reply was. The agent (not this row) decides what to do with the reply.
--
-- Reference/lookup values only — data, not structural DDL; reset_demo leaves
-- lookup tables intact.

INSERT INTO public.email_type (code, label, description, sort_order) VALUES
    ('student_meal_choice', 'Student meal choice',
     'Weekly choose-and-rate email to a student: rate last week + pick this week from MOQ-bounded, dietary-safe options; autonomous', 87)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.inbound_classification (code, label, description, sort_order) VALUES
    ('student_meal_choice', 'Student meal choice',
     'Student replying to the weekly choose-and-rate email with their meal pick + rating', 45)
ON CONFLICT (code) DO NOTHING;
