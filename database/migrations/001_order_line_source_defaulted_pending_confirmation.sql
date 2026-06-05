-- 001: add the 'defaulted_pending_confirmation' order-line provenance.
--
-- The Thursday batch composes the safe majority + per-student defaults (the
-- "Sally Jane" model): a student who is dietary-safe but has no usable
-- preference gets the most-popular item from their eligible pool as a tentative
-- SAFE default, flagged with this source so the downstream gap flow can email
-- the parent ("assume this unless told otherwise in 48h"). Reference/lookup
-- value only — data, not structural DDL; reset_demo leaves lookup tables intact.

INSERT INTO public.order_line_source (code, label, description, sort_order) VALUES
    ('defaulted_pending_confirmation', 'Defaulted (pending confirmation)',
     'Safe default for a student with no usable preference; parent to confirm within 48h', 30)
ON CONFLICT (code) DO NOTHING;
