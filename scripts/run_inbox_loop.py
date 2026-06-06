"""Continuously run the work-check until Ctrl-C (local watch / lightweight daemon).

Each cycle (every N seconds) runs both triggers: ``poll_inbox()`` for new mail and
``sweep_feedback()`` for un-actioned operator feedback (reusing the per-item reports
from ``run_inbox_once``). New mail and new operator comments that arrive while this
runs are handled automatically, cycle by cycle. Stop with Ctrl-C.

The interval N comes from runtime_config (``poll_interval_seconds``), defaulting
to 30 if it is unset/invalid; an optional CLI argument overrides it:

    uv run python scripts/run_inbox_loop.py        # interval from config (or 30)
    uv run python scripts/run_inbox_loop.py 10     # override: every 10s
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from config.settings import settings
from scripts.run_inbox_once import report_feedback, report_processed
from src.agent.feedback import sweep_feedback
from src.tools.inbound import poll_inbox, poll_topology

_DEFAULT_INTERVAL_SECONDS = 30


def _interval_seconds(argv: list[str]) -> int:
    """Resolve the poll interval: CLI arg > runtime_config > default 30."""
    if len(argv) > 1:
        try:
            n = int(argv[1])
            if n > 0:
                return n
        except ValueError:
            pass
        print(f"Ignoring invalid interval {argv[1]!r}; falling back to config/default.")

    configured = getattr(settings, "poll_interval_seconds", None)
    if isinstance(configured, int) and configured > 0:
        return configured
    return _DEFAULT_INTERVAL_SECONDS


def main() -> int:
    interval = _interval_seconds(sys.argv)
    print(f"Inbox watch started — polling every {interval}s. Press Ctrl-C to stop.")
    print(f"  topology: {poll_topology()}\n")

    cycle = 0
    try:
        while True:
            cycle += 1
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            processed = poll_inbox()
            handled_fb = sweep_feedback()
            if processed or handled_fb:
                print(f"--- cycle {cycle} @ {stamp}: "
                      f"{len(processed)} new message(s), {len(handled_fb)} feedback item(s) ---")
                if processed:
                    report_processed(processed)
                if handled_fb:
                    report_feedback(handled_fb)
            else:
                print(f"--- cycle {cycle} @ {stamp}: nothing new ---")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\nStopped after {cycle} cycle(s).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
