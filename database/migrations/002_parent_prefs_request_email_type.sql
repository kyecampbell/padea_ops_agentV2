-- 002: add the 'parent_prefs_request' outbound email kind.
--
-- The Thursday batch sends a parent a ONE-TIME request for their child's meal
-- preferences when the student is defaulted (dietary-safe but no usable
-- preference). It is tracked by (related_enrolment_id, email_type) so it is sent
-- exactly once per student, never weekly. Factual / operational -> autonomous
-- (see src/agent/gates.py). Reference/lookup value only — data, not structural
-- DDL; reset_demo leaves lookup tables intact.

INSERT INTO public.email_type (code, label, description, sort_order) VALUES
    ('parent_prefs_request', 'Parent preferences request',
     'One-time request to a parent for the student''s meal preferences', 85)
ON CONFLICT (code) DO NOTHING;
