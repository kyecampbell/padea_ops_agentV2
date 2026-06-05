-- 012: add the 'caterer_weekly_summary' outbound email kind.
--
-- The Monday per-caterer QUALITY SUMMARY — a warm "scorecard from a partner":
-- genuine specific praise, student satisfaction per school, the recurring (noise-
-- filtered) themes behind it, a gentle manager-reliability service note, and a
-- capacity ask only for a strong performer. It is a factual/operational appraisal
-- tied to real numbers (NOT a commercial judgment), so classify_email -> autonomous
-- (see src/agent/gates.py). The commercial-intent backstop still scans its content,
-- so a summary that ever drifted into warning/termination/price language would be
-- re-gated to operator approval — a formal warning / RFP / caterer swap always
-- stays operator-gated.
--
-- Reference/lookup value only — data, not structural DDL; reset_demo leaves lookup
-- tables intact.

INSERT INTO public.email_type (code, label, description, sort_order) VALUES
    ('caterer_weekly_summary', 'Caterer weekly summary',
     'Warm Monday per-caterer quality scorecard: praise + per-school student satisfaction + recurring themes + gentle service note + capacity ask; autonomous', 25)
ON CONFLICT (code) DO NOTHING;
