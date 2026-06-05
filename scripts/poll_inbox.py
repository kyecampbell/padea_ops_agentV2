"""Inbound trigger runner — inbox polling.

Responsibility: periodically read the real inbox (via the Gmail client) and, for
each new relevant message, wake the orchestrator loop with an inbound-email
trigger. Runs on the interval from runtime_config.yaml (poll_interval_seconds).

The inbound read is never redirected by demo mode — it always reads the real
inbox. (Outbound demo redirection is handled in the email tools.)

Run as a standalone process / cron entrypoint.

TODO: implement the poll loop and dispatch to the orchestrator.
"""

# TODO: implement inbound poll runner.

if __name__ == "__main__":
    # TODO: start the poll loop.
    raise SystemExit("poll_inbox: not yet implemented")
