"""24/7 background worker — inbox poller + weekly scheduler in one process.

This is the cloud "worker" service (Render). It runs TWO things side by side in a
single long-lived process:

  (a) the inbound INBOX POLL on an interval (``poll_interval_seconds`` from config),
      reusing ``src.tools.inbound.poll_inbox``, and BESIDE it the operator-FEEDBACK
      sweep (``src.agent.feedback.sweep_feedback``) — the same two work-check
      triggers the manual ``scripts/run_inbox_once.py`` drives; and
  (b) an APScheduler firing the two WEEKLY incidents on cron triggers, in
      ``Australia/Brisbane``: the Thursday order run and the Monday quality review.
      Both go through ``loop.run_incident`` — identical to the manual
      ``scripts/run_thursday_incident.py`` / ``scripts/run_quality_review.py``.

Idempotency (why concurrent / restarting fires are safe):
  - Inbox poll: a message is recorded only AFTER its incident finishes and is
    deduped on ``inbound_email_records`` (gmail_message_id), so re-polling never
    reprocesses a message.
  - Thursday batch: ``compose_week`` is idempotent per (caterer, week) — a re-run
    REPLACES rather than duplicates — and ``send_caterer_orders`` is idempotent per
    (caterer, week). So a coalesced/late fire recomputes the same safe order.
  - Quality review: reads feedback and (at most) sends one service note / drafts to
    the operator + escalates; re-running re-evaluates the same window.
  The scheduler is further configured ``coalesce=True`` (a backlog collapses to one
  run), ``max_instances=1`` (a long run can't overlap its next fire), and a
  ``misfire_grace_time`` so a brief restart still lets a just-missed fire run.

The poll loop runs on the MAIN thread; APScheduler uses a BackgroundScheduler
(its own thread). The manual scripts remain the way to trigger any of this
on-demand for a demo.

Run (cloud / local):   python worker.py
Verify without side effects (no polling, no firing):   python worker.py --selftest

Demo safety: EMAIL_MODE is unchanged here — it stays whatever the environment sets
(``demo`` by default), so outbound mail is still redirected to DEMO_SINK_EMAIL.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
from scripts.run_inbox_once import report_feedback, report_processed
from scripts.run_quality_review import (
    TRIGGER_REASON as QUALITY_TRIGGER,
    _CALL_CAP as QUALITY_CALL_CAP,
    build_task as quality_task,
)
from scripts.run_thursday_incident import _CALL_CAP as THURSDAY_CALL_CAP, _task as thursday_task
from src.agent.feedback import sweep_feedback
from src.agent.loop import run_incident
from src.tools import orders_batch, student_choice
from src.tools.inbound import poll_inbox, poll_topology

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worker")

_DEFAULT_POLL_SECONDS = 300

# Set by the signal handler so both the poll loop and the scheduler shut down cleanly.
_shutdown = threading.Event()


# --- Scheduled incident jobs --------------------------------------------------


def _run_thursday_batch() -> None:
    """The Thursday order run, as the scheduler fires it (idempotent per week)."""
    week_of = orders_batch.upcoming_monday(date.today())
    logger.info("scheduler: Thursday batch firing for week of %s", week_of.isoformat())
    result = run_incident("thursday_batch", thursday_task(week_of), call_cap=THURSDAY_CALL_CAP)
    logger.info(
        "scheduler: Thursday batch done — run %s, %s step(s)",
        result.run_id, result.step_count,
    )


def _run_quality_review() -> None:
    """The weekly quality review, as the scheduler fires it (idempotent)."""
    logger.info("scheduler: quality review firing")
    result = run_incident(QUALITY_TRIGGER, quality_task(), call_cap=QUALITY_CALL_CAP)
    logger.info(
        "scheduler: quality review done — run %s, %s step(s)",
        result.run_id, result.step_count,
    )


def _run_session_choice_sweep() -> None:
    """The data-driven student choose-and-rate sweep, as the scheduler ticks it.

    Sends choice emails for any session whose dinner_time just passed (within the
    misfire grace window). Idempotent per (student, target-week), so frequent ticks
    and restarts never double-send. EMAIL_MODE governs delivery (demo-routed)."""
    now = datetime.now(ZoneInfo(settings.scheduler_timezone))
    try:
        result = student_choice.run_due_session_choice_sends(now)
    except Exception:  # a sweep hiccup must not kill the scheduler thread.
        logger.exception("scheduler: session choice sweep failed; will retry next tick")
        return
    logger.info("scheduler: session choice sweep — %s", result.message)


def build_scheduler() -> BackgroundScheduler:
    """Construct (but do not start) the weekly scheduler in the configured tz.

    Both jobs are coalesced, capped at one concurrent instance, and given a misfire
    grace window — so a backlog, a long run, or a worker restart can never
    double-fire or overlap them.
    """
    tz = ZoneInfo(settings.scheduler_timezone)
    scheduler = BackgroundScheduler(
        timezone=tz,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": settings.misfire_grace_seconds,
        },
    )
    scheduler.add_job(
        _run_thursday_batch,
        CronTrigger(
            day_of_week=settings.thursday_batch_day,
            hour=settings.thursday_batch_hour,
            minute=settings.thursday_batch_minute,
            timezone=tz,
        ),
        id="thursday_batch",
        name="Thursday order run",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_quality_review,
        CronTrigger(
            day_of_week=settings.quality_review_day,
            hour=settings.quality_review_hour,
            minute=settings.quality_review_minute,
            timezone=tz,
        ),
        id="weekly_quality_review",
        name="Weekly quality review",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_session_choice_sweep,
        IntervalTrigger(minutes=settings.session_choice_sweep_minutes, timezone=tz),
        id="session_choice_sweep",
        name="Student choose-and-rate dinner-time sweep",
        replace_existing=True,
    )
    return scheduler


# --- Inbox poll loop ----------------------------------------------------------


def _poll_interval_seconds() -> int:
    configured = getattr(settings, "poll_interval_seconds", None)
    if isinstance(configured, int) and configured > 0:
        return configured
    return _DEFAULT_POLL_SECONDS


def _poll_once() -> None:
    """One inbox poll cycle, with per-cycle logging (failures never kill the loop)."""
    try:
        processed = poll_inbox()
    except Exception:  # an inbox/LLM/DB hiccup must not stop the daemon.
        logger.exception("inbox poll cycle failed; will retry next interval")
        return
    if processed:
        logger.info("inbox poll: %d new message(s) processed", len(processed))
        report_processed(processed)
    else:
        logger.info("inbox poll: nothing new")


def _sweep_feedback_once() -> None:
    """One operator-feedback sweep, beside the inbox poll — the second work-check
    trigger. Surfaces any UN-ACTIONED operator comment and handles each exactly
    once (re-run on an instruction/rejection, store a lesson, or escalate if
    unclear). A failure is logged and retried next interval; it never kills the loop."""
    try:
        handled = sweep_feedback()
    except Exception:  # a sweep hiccup must not stop the daemon.
        logger.exception("feedback sweep failed; will retry next interval")
        return
    if handled:
        logger.info("feedback sweep: %d operator comment(s) handled", len(handled))
        report_feedback(handled)
    else:
        logger.info("feedback sweep: nothing un-actioned")


def _install_signal_handlers() -> None:
    def _handle(signum, _frame):
        logger.info("received signal %s — shutting down", signum)
        _shutdown.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


# --- Entry points -------------------------------------------------------------


def _log_jobs(scheduler: BackgroundScheduler) -> None:
    """Log each scheduled job and its next fire time (in the configured tz)."""
    for job in scheduler.get_jobs():
        nxt = job.next_run_time.isoformat() if job.next_run_time else "(paused)"
        logger.info("  job %-22s next: %s", job.id, nxt)


def selftest() -> int:
    """Wire everything up and report — WITHOUT polling or firing any job.

    Proves: settings load, Gmail creds resolve (the topology line), the scheduler
    builds, and both cron jobs compute their next Brisbane fire times. No inbox
    message is processed and no incident is run.
    """
    logger.info("worker selftest — EMAIL_MODE=%s", settings.email_mode)
    logger.info("inbox topology: %s", poll_topology())
    logger.info("work-check triggers: inbox poll + operator-feedback sweep")
    scheduler = build_scheduler()
    # Start paused so next_run_time is computed but no job actually fires.
    scheduler.start(paused=True)
    logger.info("scheduler timezone: %s; poll interval: %ss",
                settings.scheduler_timezone, _poll_interval_seconds())
    _log_jobs(scheduler)
    scheduler.shutdown(wait=False)
    logger.info("selftest OK — nothing was polled or fired")
    return 0


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()

    _install_signal_handlers()
    interval = _poll_interval_seconds()

    logger.info("worker starting — EMAIL_MODE=%s", settings.email_mode)
    logger.info("inbox topology: %s", poll_topology())

    scheduler = build_scheduler()
    scheduler.start()
    logger.info("scheduler started (tz=%s); weekly jobs:", settings.scheduler_timezone)
    _log_jobs(scheduler)
    logger.info("work-check loop starting — inbox poll + feedback sweep every %ss "
                "(Ctrl-C / SIGTERM to stop)", interval)

    try:
        while not _shutdown.is_set():
            _poll_once()
            _sweep_feedback_once()
            # Interruptible sleep: wake immediately on shutdown signal.
            _shutdown.wait(interval)
    finally:
        scheduler.shutdown(wait=False)
        logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
