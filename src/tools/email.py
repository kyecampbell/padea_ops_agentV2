"""Email tools.

Responsibility: the agent's outbound email surface. ``send_email`` is the single
entry point. The hard-rules gate (``gates.classify_email``) decides each kind:
  - autonomous (factual / operational) — send now via the Gmail client, log 'sent'
    and return a ``found`` result.
  - requires_approval (commercial / money / binding) — DO NOT send; record the
    composed mail with status 'queued_for_approval' for a human to release, and
    return a ``queued`` result (the shared "pending approval" signal).

Demo-mode safety: when EMAIL_MODE=demo, real sends are redirected to
DEMO_SINK_EMAIL with a subject/body prefix `[DEMO — Intended for: <real
recipient>]`. The REAL recipient is always stored in
``outbound_emails.intended_to_address`` regardless of the redirect, so the audit
trail reflects intent. The inbound poll reads the real inbox and is never
redirected (see src/gmail/client.py).

All functions return typed results from `results.py` and NEVER raise at the agent.

Conventions:
  - Timestamps are timezone-aware (DB ``now()`` / ``timestamptz``).
  - All SQL is parameterised — never string-built.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config.settings import settings
from src.agent.gates import email_requires_approval
from src.db.connection import get_conn
from src.gmail.client import GmailClient
from src.tools.results import ToolResult, conflict, error, found, queued, unavailable

logger = logging.getLogger(__name__)


# --- DB helper ---------------------------------------------------------------


def _transaction(describe: str, work: Callable[[psycopg.Cursor], Any]) -> Any | ToolResult:
    """Run ``work(cur)`` in one committed transaction; translate failures.

    Returns whatever ``work`` returns on success, or an ``unavailable`` / ``error``
    ToolResult on a DB failure (the transaction is rolled back on exit). Keeps the
    "never raise at the caller" contract.
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            result = work(cur)
            conn.commit()
            return result
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


def _failed(value: Any) -> bool:
    """True when a helper returned a failure ToolResult rather than data."""
    return isinstance(value, ToolResult)


# --- Outbound signature -------------------------------------------------------
# Every agent email — originated (send_email) or a reply (send_reply) — closes
# with the same two-line TEXT signature. No image: WebP isn't reliable in email
# clients and we never add weight to the send path. Applied centrally so EVERY
# outbound body is signed consistently, and idempotent so a body that already
# carries it (the templated builders, or a reply the model signed itself) is left
# untouched rather than double-signed.

_SIGNATURE_TAGLINE = "Structure and Support Study"
_SIGNATURE = f"Padea Operations\n{_SIGNATURE_TAGLINE}"


def _ensure_signature(body: str) -> str:
    """Return ``body`` closed with the standard Padea sign-off, exactly once.

    - Tagline already present anywhere → returned unchanged (idempotent).
    - Body already ends on a bare "Padea Operations" line → append the tagline
      directly beneath it (upgrades an older one-line sign-off).
    - Otherwise → append the full two-line signature after a blank line.
    """
    text = (body or "").rstrip()
    if _SIGNATURE_TAGLINE in text:
        return text
    if text.endswith("Padea Operations"):
        return f"{text}\n{_SIGNATURE_TAGLINE}"
    return f"{text}\n\n{_SIGNATURE}" if text else _SIGNATURE


# --- Demo-mode redirection ----------------------------------------------------


def _demo_decorate(real_to: str, subject: str, body: str) -> tuple[str, str]:
    """Prefix the subject and body with the `[DEMO …]` banner for the sink send."""
    banner = f"[DEMO — Intended for: {real_to}]"
    return f"{banner} {subject}", f"{banner}\n\n{body}"


def _cc_addresses(cc: str | None) -> list[str] | None:
    """Split a comma-separated cc string into a list (None when no cc)."""
    if not cc:
        return None
    addrs = [a.strip() for a in cc.split(",") if a.strip()]
    return addrs or None


# --- Cross-link sanitisation --------------------------------------------------

# Each optional related id is a foreign key on outbound_emails. The agent
# occasionally supplies one that does not resolve (e.g. confusing the weekly
# caterer_week_orders id for a per-session orders id). A dangling FK would only
# blow up at INSERT time — i.e. AFTER the Gmail send for an autonomous mail has
# already happened — making a sent email look failed and inviting a retry that
# double-sends. So we drop any non-existent link up front: the audit cross-link
# is best-effort, the email content is unaffected, and the send stays exactly-once.
_RELATED_FK_SQL: dict[str, str] = {
    "related_caterer_id": "SELECT 1 FROM caterers WHERE id = %s",
    "related_enrolment_id": "SELECT 1 FROM enrolments WHERE id = %s",
    "related_order_id": "SELECT 1 FROM orders WHERE id = %s",
}


def _sanitise_related_ids(ids: dict[str, int | None]) -> dict[str, int | None]:
    """Null out any related_* id that doesn't resolve to a real row.

    Best-effort: if the existence check itself can't run (DB down), the ids pass
    through unchanged and the normal logging path surfaces the failure as
    ``unavailable``. Logs a warning whenever it drops a dangling link.
    """
    provided = {k: v for k, v in ids.items() if v is not None}
    if not provided:
        return ids
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for key, value in provided.items():
                cur.execute(_RELATED_FK_SQL[key], (value,))
                if cur.fetchone() is None:
                    logger.warning("send_email: dropping dangling %s=%s (no such row).", key, value)
                    ids[key] = None
    except psycopg.Error as exc:  # best-effort; let the real send/log report DB trouble.
        logger.warning("send_email: could not validate related ids (%s); leaving as-is.", exc)
    return ids


# --- outbound_emails logging --------------------------------------------------


def _log_outbound(
    *,
    email_type: str,
    status: str,
    intended_to: str,
    cc_addresses: list[str] | None,
    subject: str,
    body: str,
    gmail_message_id: str | None,
    failure_reason: str | None,
    related_run_id: int | None,
    related_caterer_id: int | None,
    related_enrolment_id: int | None,
    related_order_id: int | None,
) -> dict | ToolResult:
    """Insert one ``outbound_emails`` row, stamping the status-specific timestamp.

    Stores the REAL recipient/subject/body (the demo banner is a delivery detail,
    not part of the logged intent). Returns the new row, or a failure ToolResult.
    """
    ts_col = {
        "sent": "sent_at",
        "queued_for_approval": "queued_for_approval_at",
        "failed": "failed_at",
        # dry-run drafts carry no lifecycle timestamp of their own; stamp composed_at
        # (which already defaults to now()) so the INSERT shape stays uniform.
        "drafted": "composed_at",
    }[status]

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            f"""
            INSERT INTO outbound_emails
                (email_type, status, intended_to_address, intended_cc_addresses,
                 subject, rendered_body, gmail_message_id, failure_reason, {ts_col},
                 related_run_id, related_caterer_id, related_enrolment_id, related_order_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s)
            RETURNING id, status, {ts_col}
            """,
            (
                email_type,
                status,
                intended_to,
                Jsonb(cc_addresses) if cc_addresses is not None else None,
                subject,
                body,
                gmail_message_id,
                failure_reason,
                related_run_id,
                related_caterer_id,
                related_enrolment_id,
                related_order_id,
            ),
        )
        return cur.fetchone()

    return _transaction(f"logging outbound email ({email_type}/{status})", work)


# --- Public tool --------------------------------------------------------------


def send_email(
    email_type: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    related_run_id: int | None = None,
    related_caterer_id: int | None = None,
    related_enrolment_id: int | None = None,
    related_order_id: int | None = None,
) -> ToolResult:
    """Send (or queue for approval) one outbound email, logging it every time.

    The gate keys on ``email_type`` (see ``gates.classify_email``):
      - ``requires_approval`` — the mail is NOT sent; it is recorded with status
        ``queued_for_approval`` for a human to release (``queued`` result).
      - ``autonomous`` — the mail is sent now (this includes ``caterer_service_note``,
        a polite note to a caterer about a minor, fixable issue). In demo mode it is
        redirected to ``DEMO_SINK_EMAIL`` with a ``[DEMO …]`` banner, but the real
        recipient is still stored. On success the row is logged ``sent``
        (+ gmail_message_id); if the Gmail send fails the row is logged ``failed``
        and an ``unavailable`` result is returned. In ``dry`` mode the mail is logged
        ``drafted`` and NOT sent (a ``found`` result with ``dry_run: True``).
    """
    if not (to or "").strip():
        return error("recipient 'to' is required.")
    if not (subject or "").strip():
        return error("subject is required.")
    if body is None:
        return error("body is required (use '' for an empty body).")

    # These are PLAIN-TEXT emails, but the model occasionally HTML-escapes its
    # output (e.g. "&amp;" for "&" in a subject). Unescape once up front so the
    # stored draft and the actual send both read as plain text. Idempotent on text
    # that has no entities.
    subject = html.unescape(subject)
    body = html.unescape(body)
    body = _ensure_signature(body)

    # Drop any dangling cross-link BEFORE we send, so a bad related id can never
    # turn a successful send into a logging failure (which would invite a retry
    # and double-send).
    sanitised = _sanitise_related_ids(
        {
            "related_caterer_id": related_caterer_id,
            "related_enrolment_id": related_enrolment_id,
            "related_order_id": related_order_id,
        }
    )
    related_caterer_id = sanitised["related_caterer_id"]
    related_enrolment_id = sanitised["related_enrolment_id"]
    related_order_id = sanitised["related_order_id"]

    cc_addresses = _cc_addresses(cc)

    # --- Commercial / money / binding: record only, never send. ---
    # Two independent triggers, EITHER of which forces approval (see
    # gates.email_requires_approval): (1) the declared email_type is a commercial
    # kind, or (2) the content backstop — a deterministic scan of the actual
    # subject/body — finds commercial-consequence language even though the model
    # tagged the mail as a benign kind. (2) closes the mislabel seam: email_type is
    # model-supplied, so a warning/RFP/cancellation/price nudge disguised as a
    # benign type can never auto-send.
    needs_approval, intent_signals = email_requires_approval(email_type, subject, body)
    if needs_approval:
        row = _log_outbound(
            email_type=email_type,
            status="queued_for_approval",
            intended_to=to,
            cc_addresses=cc_addresses,
            subject=subject,
            body=body,
            gmail_message_id=None,
            failure_reason=None,
            related_run_id=related_run_id,
            related_caterer_id=related_caterer_id,
            related_enrolment_id=related_enrolment_id,
            related_order_id=related_order_id,
        )
        if _failed(row):
            return row
        if intent_signals:
            # Mislabel caught by the content backstop: a "benign" type whose body
            # reads as commercial. Loud log so the override is auditable.
            logger.warning(
                "send_email: QUEUED (commercial-intent backstop) email=%s declared_type=%s "
                "to=%r signals=%s",
                row["id"], email_type, to, intent_signals,
            )
            reason = (
                f"Email declared as '{email_type}' but its content reads as commercial "
                f"({', '.join(intent_signals)}); queued for operator approval, not sent."
            )
        else:
            logger.info(
                "send_email: QUEUED FOR APPROVAL email=%s type=%s to=%r",
                row["id"], email_type, to,
            )
            reason = f"Commercial email ({email_type}) to {to} queued for operator approval; not sent."
        return queued(
            reason,
            data={
                "email_id": row["id"],
                "status": "queued_for_approval",
                "sent": False,
                "commercial_intent_signals": intent_signals,
            },
        )

    # --- Dry run: record the draft, send NOTHING (no Gmail call). ---
    # Used to preview a run end-to-end; the agent proceeds as if sent, but the mail
    # is only logged 'drafted' so a human can inspect exactly what would have gone out.
    if settings.email_mode == "dry":
        row = _log_outbound(
            email_type=email_type,
            status="drafted",
            intended_to=to,
            cc_addresses=cc_addresses,
            subject=subject,
            body=body,
            gmail_message_id=None,
            failure_reason=None,
            related_run_id=related_run_id,
            related_caterer_id=related_caterer_id,
            related_enrolment_id=related_enrolment_id,
            related_order_id=related_order_id,
        )
        if _failed(row):
            return row
        logger.info("send_email: DRY RUN drafted email=%s type=%s to=%r (NOT sent)", row["id"], email_type, to)
        return found(
            {
                "email_id": row["id"],
                "status": "drafted",
                "sent": False,
                "dry_run": True,
            },
            f"DRY RUN: {email_type} email to {to} drafted and logged; NOT sent.",
        )

    # --- Autonomous: actually send (demo-redirected if configured). ---
    if settings.email_mode == "demo":
        if not settings.demo_sink_email:
            return error("EMAIL_MODE=demo but DEMO_SINK_EMAIL is not configured.")
        send_to = settings.demo_sink_email
        send_cc = None  # never leak real cc recipients in demo mode.
        send_subject, send_body = _demo_decorate(to, subject, body)
    else:
        send_to, send_cc = to, cc
        send_subject, send_body = subject, body

    try:
        response = GmailClient().send_message(send_to, send_subject, send_body, cc=send_cc)
        gmail_message_id = response.get("id")
    except Exception as exc:  # Gmail is an external dependency; capture, never raise.
        row = _log_outbound(
            email_type=email_type,
            status="failed",
            intended_to=to,
            cc_addresses=cc_addresses,
            subject=subject,
            body=body,
            gmail_message_id=None,
            failure_reason=str(exc),
            related_run_id=related_run_id,
            related_caterer_id=related_caterer_id,
            related_enrolment_id=related_enrolment_id,
            related_order_id=related_order_id,
        )
        if _failed(row):
            return row
        logger.warning("send_email: FAILED email=%s type=%s to=%r: %s", row["id"], email_type, to, exc)
        return unavailable(f"Failed to send {email_type} email to {to}: {exc}")

    row = _log_outbound(
        email_type=email_type,
        status="sent",
        intended_to=to,
        cc_addresses=cc_addresses,
        subject=subject,
        body=body,
        gmail_message_id=gmail_message_id,
        failure_reason=None,
        related_run_id=related_run_id,
        related_caterer_id=related_caterer_id,
        related_enrolment_id=related_enrolment_id,
        related_order_id=related_order_id,
    )
    if _failed(row):
        return row
    logger.info(
        "send_email: SENT email=%s type=%s to=%r gmail_id=%s%s",
        row["id"], email_type, to, gmail_message_id,
        f" (demo-routed to {settings.demo_sink_email})" if settings.email_mode == "demo" else "",
    )
    return found(
        {
            "email_id": row["id"],
            "status": "sent",
            "sent": True,
            "gmail_message_id": gmail_message_id,
            "demo_routed": settings.email_mode == "demo",
        },
        f"Sent {email_type} email to {to}.",
    )


# --- REPLY to an inbound sender (the ONE non-redirected outbound path) --------
# This is the deliberate branch from the agent-INITIATED ``send_email`` path
# above. ``send_email`` is for mail the agent ORIGINATES (orders, choice, scorecard,
# proactive parent/caterer notes): in demo mode it is REDIRECTED to DEMO_SINK_EMAIL
# (the on-record .example addresses are non-routable) — that sandbox is untouched.
# ``send_reply`` is ONLY for answering a real person who emailed in: it delivers to
# their ACTUAL address even in demo mode (they are a real correspondent, not a demo
# placeholder). Same approval/backstop gate applies, so a reply can never carry a
# commercial decision and auto-send. In ``dry`` mode it is logged 'drafted', not sent.

REPLY_EMAIL_TYPE = "other"  # a reply is factual/operational (autonomous unless the
                            # commercial-intent backstop trips on its content).


def send_reply(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    related_run_id: int | None = None,
    related_caterer_id: int | None = None,
    related_enrolment_id: int | None = None,
    related_order_id: int | None = None,
) -> ToolResult:
    """Reply to the ACTUAL sender of an inbound email — delivered to ``to`` even in
    demo mode (NO sink redirect, NO ``[DEMO …]`` banner), because ``to`` is a real
    person who wrote in. This is the ONLY outbound path that is not demo-redirected;
    the agent-initiated ``send_email`` is unchanged. The commercial-intent gate
    still applies (a reply that reads commercial is queued for approval, not sent).
    In ``dry`` mode the reply is logged 'drafted' and NOT sent.
    """
    if not (to or "").strip():
        return error("reply recipient 'to' is required.")
    if not (subject or "").strip():
        return error("subject is required.")
    if body is None:
        return error("body is required (use '' for an empty body).")

    subject = html.unescape(subject)
    body = html.unescape(body)
    body = _ensure_signature(body)
    sanitised = _sanitise_related_ids(
        {
            "related_caterer_id": related_caterer_id,
            "related_enrolment_id": related_enrolment_id,
            "related_order_id": related_order_id,
        }
    )
    related_caterer_id = sanitised["related_caterer_id"]
    related_enrolment_id = sanitised["related_enrolment_id"]
    related_order_id = sanitised["related_order_id"]
    cc_addresses = _cc_addresses(cc)

    # Gate: a reply must never carry commercial intent and auto-send. Same backstop
    # as send_email — if the content reads commercial, queue for approval.
    needs_approval, intent_signals = email_requires_approval(REPLY_EMAIL_TYPE, subject, body)
    if needs_approval:
        row = _log_outbound(
            email_type=REPLY_EMAIL_TYPE, status="queued_for_approval", intended_to=to,
            cc_addresses=cc_addresses, subject=subject, body=body, gmail_message_id=None,
            failure_reason=None, related_run_id=related_run_id,
            related_caterer_id=related_caterer_id, related_enrolment_id=related_enrolment_id,
            related_order_id=related_order_id,
        )
        if _failed(row):
            return row
        logger.warning("send_reply: QUEUED (commercial-intent backstop) email=%s to=%r signals=%s",
                       row["id"], to, intent_signals)
        return queued(
            f"Reply to {to} reads as commercial ({', '.join(intent_signals)}); queued for "
            "operator approval, not sent.",
            data={"email_id": row["id"], "status": "queued_for_approval", "sent": False,
                  "commercial_intent_signals": intent_signals},
        )

    # Dry run: record the draft, send nothing.
    if settings.email_mode == "dry":
        row = _log_outbound(
            email_type=REPLY_EMAIL_TYPE, status="drafted", intended_to=to,
            cc_addresses=cc_addresses, subject=subject, body=body, gmail_message_id=None,
            failure_reason=None, related_run_id=related_run_id,
            related_caterer_id=related_caterer_id, related_enrolment_id=related_enrolment_id,
            related_order_id=related_order_id,
        )
        if _failed(row):
            return row
        logger.info("send_reply: DRY RUN drafted reply=%s to=%r (NOT sent)", row["id"], to)
        return found(
            {"email_id": row["id"], "status": "drafted", "sent": False, "dry_run": True,
             "replied_to": to, "demo_routed": False},
            f"DRY RUN: reply to {to} drafted and logged; NOT sent.",
        )

    # demo OR live: deliver to the ACTUAL sender — NO redirect, NO banner.
    try:
        response = GmailClient().send_message(to, subject, body, cc=cc)
        gmail_message_id = response.get("id")
    except Exception as exc:  # Gmail is external; capture, never raise.
        row = _log_outbound(
            email_type=REPLY_EMAIL_TYPE, status="failed", intended_to=to,
            cc_addresses=cc_addresses, subject=subject, body=body, gmail_message_id=None,
            failure_reason=str(exc), related_run_id=related_run_id,
            related_caterer_id=related_caterer_id, related_enrolment_id=related_enrolment_id,
            related_order_id=related_order_id,
        )
        if _failed(row):
            return row
        logger.warning("send_reply: FAILED reply=%s to=%r: %s", row["id"], to, exc)
        return unavailable(f"Failed to send reply to {to}: {exc}")

    row = _log_outbound(
        email_type=REPLY_EMAIL_TYPE, status="sent", intended_to=to,
        cc_addresses=cc_addresses, subject=subject, body=body,
        gmail_message_id=gmail_message_id, failure_reason=None, related_run_id=related_run_id,
        related_caterer_id=related_caterer_id, related_enrolment_id=related_enrolment_id,
        related_order_id=related_order_id,
    )
    if _failed(row):
        return row
    logger.info("send_reply: SENT reply=%s to=%r gmail_id=%s (direct to sender, not sink)",
                row["id"], to, gmail_message_id)
    return found(
        {"email_id": row["id"], "status": "sent", "sent": True,
         "gmail_message_id": gmail_message_id, "replied_to": to, "demo_routed": False},
        f"Replied directly to {to}.",
    )


# --- Operator-approved release ------------------------------------------------


def send_queued_email(email_id: int, approved_by: str = "operator") -> ToolResult:
    """Release a queued-for-approval email: actually send it now and mark it sent.

    This is the operator-approval path behind the decision UI — the human has
    approved a commercial mail that ``send_email`` deliberately parked at
    ``queued_for_approval``. We send the SAME stored draft (demo-redirected to
    ``DEMO_SINK_EMAIL`` like any other send, with the real recipient preserved),
    stamp ``approved_by`` / ``approved_at`` / ``sent_at`` and flip the row to
    ``sent``. A Gmail failure flips it to ``failed`` and returns ``unavailable``;
    a row that is missing or already sent returns a typed ``conflict``.
    """
    row = _read_email(email_id)
    if _failed(row):
        return row
    if row is None:
        return conflict(f"No outbound email with id {email_id}; nothing to send.")
    if row["status"] == "sent":
        return conflict(f"Email {email_id} was already sent; not re-sending.")
    if row["status"] not in ("queued_for_approval", "approved"):
        return conflict(
            f"Email {email_id} is '{row['status']}', not awaiting approval; not sending."
        )

    to = row["intended_to_address"]
    subject = row["subject"]
    body = row["rendered_body"]
    cc_addresses = row["intended_cc_addresses"]  # jsonb list or None

    # Same delivery rules as a fresh autonomous send: demo redirect, real cc only
    # outside demo mode.
    if settings.email_mode == "demo":
        if not settings.demo_sink_email:
            return error("EMAIL_MODE=demo but DEMO_SINK_EMAIL is not configured.")
        send_to = settings.demo_sink_email
        send_cc = None
        send_subject, send_body = _demo_decorate(to, subject, body)
    else:
        send_to = to
        send_cc = ", ".join(cc_addresses) if cc_addresses else None
        send_subject, send_body = subject, body

    try:
        response = GmailClient().send_message(send_to, send_subject, send_body, cc=send_cc)
        gmail_message_id = response.get("id")
    except Exception as exc:  # Gmail is external; capture, never raise.
        failed = _mark_email_failed(email_id, str(exc))
        if _failed(failed):
            return failed
        logger.warning("send_queued_email: FAILED email=%s: %s", email_id, exc)
        return unavailable(f"Failed to send approved email {email_id} to {to}: {exc}")

    sent = _mark_email_sent(email_id, gmail_message_id, approved_by)
    if _failed(sent):
        return sent

    logger.info(
        "send_queued_email: SENT email=%s to=%r approved_by=%r gmail_id=%s%s",
        email_id, to, approved_by, gmail_message_id,
        f" (demo-routed to {settings.demo_sink_email})" if settings.email_mode == "demo" else "",
    )
    return found(
        {
            "email_id": email_id,
            "status": "sent",
            "sent": True,
            "gmail_message_id": gmail_message_id,
            "approved_by": approved_by,
            "demo_routed": settings.email_mode == "demo",
        },
        f"Approved and sent email {email_id} to {to}.",
    )


def _read_email(email_id: int) -> dict | None | ToolResult:
    """Load the columns needed to release a queued email (None if absent)."""
    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            """
            SELECT id, email_type, status, intended_to_address,
                   intended_cc_addresses, subject, rendered_body
            FROM outbound_emails
            WHERE id = %s
            """,
            (email_id,),
        )
        return cur.fetchone()

    return _transaction(f"loading outbound email {email_id}", work)


def _mark_email_sent(
    email_id: int, gmail_message_id: str | None, approved_by: str
) -> dict | ToolResult:
    """Flip a released email to ``sent``, stamping approval + send timestamps."""
    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            """
            UPDATE outbound_emails
            SET status = 'sent',
                approved_by = %s,
                approved_at = now(),
                sent_at = now(),
                gmail_message_id = %s
            WHERE id = %s
            RETURNING id, status
            """,
            (approved_by, gmail_message_id, email_id),
        )
        return cur.fetchone()

    return _transaction(f"marking email {email_id} sent", work)


def _mark_email_failed(email_id: int, reason: str) -> dict | ToolResult:
    """Flip a released email to ``failed`` with the failure reason."""
    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            """
            UPDATE outbound_emails
            SET status = 'failed',
                failed_at = now(),
                failure_reason = %s
            WHERE id = %s
            RETURNING id, status
            """,
            (reason, email_id),
        )
        return cur.fetchone()

    return _transaction(f"marking email {email_id} failed", work)
