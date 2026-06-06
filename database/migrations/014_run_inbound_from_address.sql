-- 014: carry the inbound sender's address ON the run, so a feedback re-run can
-- reply to the ORIGINAL sender automatically.
--
-- An inbound incident receives the sender's reply-to address as ambient run
-- context (loop.run_incident extra_context -> reply_to_sender's `to`), but that
-- address was never persisted. So a feedback re-run — a NEW run that replays the
-- task with the operator's correction — had no inbound_from_address in context:
-- reply_to_sender could not route to the real sender, and the agent fell back to
-- send_email, which in demo mode redirects to the sink. Persisting the address on
-- agent_runs lets the feedback sweep read the original run's value and forward it,
-- so the corrected reply reaches the actual sender automatically.
--
-- See src/agent/loop.py (_open_run persists it) and src/agent/feedback.py (the
-- re-run carries it forward). Additive only: ADD COLUMN IF NOT EXISTS.

ALTER TABLE public.agent_runs
    ADD COLUMN IF NOT EXISTS inbound_from_address text;
COMMENT ON COLUMN public.agent_runs.inbound_from_address IS 'The inbound sender''s reply-to address for an inbound_email run (NULL otherwise). Persisted so a feedback re-run can carry it forward and reply_to_sender routes to the real sender automatically. Runs predating this column are NULL.';
