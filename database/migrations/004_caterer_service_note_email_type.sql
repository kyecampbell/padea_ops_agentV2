-- 004: add the 'caterer_service_note' outbound email kind.
--
-- A polite, low-stakes note to a caterer about a MINOR, fixable issue (a cold or
-- late delivery, a one-off mix-up). Factual / operational -> AUTONOMOUS (see
-- src/agent/gates.py classify_email). This is deliberately distinct from the
-- 'warning' kind, which embodies a fresh commercial judgment, may precede an RFP,
-- and stays requires_approval. Accumulating evidence of a real decline is drafted
-- to the operator + escalated to a human, never auto-warned (see the quality
-- policy in config/handbook.md).
--
-- Reference/lookup value only — data, not structural DDL; reset_demo leaves lookup
-- tables intact.

INSERT INTO public.email_type (code, label, description, sort_order) VALUES
    ('caterer_service_note', 'Caterer service note',
     'Polite note to a caterer about a minor, fixable issue (cold/late/one-off mix-up); autonomous', 35)
ON CONFLICT (code) DO NOTHING;
