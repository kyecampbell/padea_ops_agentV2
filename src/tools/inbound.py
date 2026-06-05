"""Inbound email polling — the event trigger.

Responsibility: read the REAL inbox (via the Gmail client) and, for each NEW
message, wake the orchestrator with an ``inbound_email`` trigger. The agent —
not this module — decides what each email is and what to do about it; there is
NO per-case branching here. We only:

  1. list recent/unread messages,
  2. dedup each against ``inbound_email_records`` (PK ``gmail_message_id``) and
     skip anything already processed,
  3. hand the full email to ``loop.run_incident`` and let the agent reason and
     act with its tool belt (the approval gate still applies),
  4. record the message in ``inbound_email_records`` (with the classification a
     cheap model derived from the email + the agent's conclusion) and mark it
     read.

Idempotency: a message is recorded only AFTER its incident finishes, and we skip
anything already in ``inbound_email_records``, so re-polling never reprocesses a
message. (Processing that crashes mid-incident leaves no record, so the message
is retried on the next poll — at-least-once.)

Demo mode note: reading the inbox is NEVER redirected. Demo mode only rewrites
outbound sends (see ``src/tools/email.py``); the poll always reads the real inbox.

Conventions:
  - Timestamps are timezone-aware (Gmail ``internalDate`` -> UTC).
  - All SQL is parameterised; the orchestrator model is sonnet, and a cheap model
    is used ONLY for the lightweight record-keeping classification.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

import anthropic

from config.settings import settings
from src.agent.loop import run_incident
from src.db.connection import fetch_all, get_conn
from src.gmail.client import GmailClient

logger = logging.getLogger(__name__)

# A cheap model used ONLY to label the processed email for the dedup record — it
# does not decide what to do (the orchestrator already did). Any failure here
# falls back to 'unclassified' so polling never breaks on the label step.
_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
_CLASSIFIER_MAX_TOKENS = 256
_FALLBACK_CODE = "unclassified"

# Loop guard: the subject banner that ``src/tools/email.py`` stamps on every
# demo-redirected send (keep in lockstep with ``email._demo_decorate``). A message
# whose subject starts with this — or whose From is our own sending account — is
# OUR OWN outbound mail and must never be treated as a new inbound message. This is
# the structural defence against a send -> poll -> send feedback loop, and it holds
# regardless of whether the demo sink and the polled inbox happen to coincide.
_DEMO_SUBJECT_PREFIX = "[DEMO — Intended for:"


def _is_own_outbound(email: "InboundEmail", own_address: str) -> bool:
    """True if this message is our own sent / demo-redirected mail, not real inbound.

    Two signals: the demo subject banner, or a From that is our own account (the
    address we authenticate + send as). Either one means skip — we never wake an
    incident on a message we ourselves produced.
    """
    subject = (email.subject or "").lstrip()
    if subject.startswith(_DEMO_SUBJECT_PREFIX):
        return True
    if own_address:
        return own_address.lower() in (email.from_address or "").lower()
    return False


# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InboundEmail:
    """A parsed inbound message — exactly what we hand the orchestrator."""

    gmail_message_id: str
    from_address: str
    to_address: str
    subject: str
    body: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ProcessedEmail:
    """The outcome of handling one new inbound message.

    On success ``error`` is None and the record/mark-read have happened. On a
    per-message failure ``error`` is set and NOTHING was recorded (so the next
    poll retries it).
    """

    email: InboundEmail
    run_id: int | None
    classified_as: str | None
    final_text: str
    step_count: int
    related_enrolment_id: int | None
    related_order_id: int | None
    error: str | None = None


# --- Gmail message parsing ----------------------------------------------------


def _header(headers: list[dict], name: str) -> str:
    """Case-insensitive lookup of a header value (empty string if absent)."""
    lname = name.lower()
    for h in headers:
        if h.get("name", "").lower() == lname:
            return h.get("value", "") or ""
    return ""


def _decode(data: str) -> str:
    """Decode a Gmail base64url body part to text (lossy on bad bytes)."""
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _find_part(payload: dict, mime: str) -> str:
    """Depth-first search for the first body of the given mime type."""
    if payload.get("mimeType") == mime:
        data = payload.get("body", {}).get("data")
        if data:
            return _decode(data)
    for part in payload.get("parts", []) or []:
        text = _find_part(part, mime)
        if text:
            return text
    return ""


def _strip_html(html: str) -> str:
    """Crude tag strip — a readable fallback when there is no text/plain part."""
    out, depth = [], 0
    for ch in html:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _extract_body(payload: dict) -> str:
    """Best-effort readable body: prefer text/plain, then stripped text/html."""
    plain = _find_part(payload, "text/plain")
    if plain.strip():
        return plain
    html = _find_part(payload, "text/html")
    return _strip_html(html) if html.strip() else ""


def _parse_message(full: dict) -> InboundEmail:
    """Turn a Gmail ``format=full`` message into an ``InboundEmail``."""
    payload = full.get("payload", {})
    headers = payload.get("headers", [])
    body = _extract_body(payload) or full.get("snippet", "") or ""
    received_at = datetime.fromtimestamp(int(full["internalDate"]) / 1000, tz=timezone.utc)
    return InboundEmail(
        gmail_message_id=full["id"],
        from_address=_header(headers, "From"),
        to_address=_header(headers, "To"),
        subject=_header(headers, "Subject"),
        body=body.strip(),
        received_at=received_at,
    )


# --- Orchestrator hand-off ----------------------------------------------------


def sender_address(email: InboundEmail) -> str:
    """The bare reply-to address of the inbound message (display name stripped)."""
    return parseaddr(email.from_address or "")[1].strip()


def _format_task(email: InboundEmail) -> str:
    """Lay the email out clearly for the orchestrator. No instructions about a
    specific case — the agent reads the policy and decides.

    The sender's reply-to address IS shown, but ONLY for two purposes: addressing a
    reply (the reply tool delivers to the actual sender) and reconciling their
    contact email. It is explicitly NOT an identity signal — identity still comes
    from the body, per the handbook.
    """
    sender = sender_address(email) or "(unknown)"
    return (
        "A new inbound email has arrived in the Padea operations inbox. Work out "
        "what it is and who it concerns, then handle it per the handbook's "
        "inbound-handling policy. Confirm identities and facts with your query "
        "tools before acting; the approval gate still applies to every action.\n\n"
        "--- INBOUND EMAIL ---\n"
        f"Gmail message id: {email.gmail_message_id}\n"
        f"Received (UTC): {email.received_at.isoformat()}\n"
        f"Sender reply-to address: {sender}\n"
        "  (Use this ONLY to (a) address your reply — reply_to_sender delivers to "
        "this address — and (b) reconcile their contact email on file. It is NOT "
        "proof of who they are; identify the person from the body.)\n"
        f"To: {email.to_address}\n"
        f"Subject: {email.subject}\n\n"
        f"{email.body or '(no readable body)'}\n"
        "--- END EMAIL ---\n\n"
        "Decide and act: act/queue the change, reply asking for confirmation, or "
        "escalate to a human — whichever the policy calls for. If you reply, use "
        "reply_to_sender (it goes to the actual sender). Once you've identified the "
        "person, compare their on-record email to the sender reply-to address above; "
        "if they differ, your reply should note the on-record address and OFFER to "
        "update it to this one — and only call update_contact_email if they have "
        "EXPLICITLY confirmed the change in this email. If you cannot confidently "
        "identify the person, do NOT claim a mismatch — ask who they are or escalate. "
        "Conclude with a short summary of what you determined and what you did."
    )


# --- Lightweight classification (record-keeping only) -------------------------


def _active_codes() -> list[dict]:
    """The current inbound-classification vocabulary (code/label/description)."""
    rows = fetch_all(
        """
        SELECT code, label, description
        FROM inbound_classification
        WHERE active = TRUE
        ORDER BY sort_order, code
        """
    )
    return [{"code": c, "label": l, "description": d} for (c, l, d) in rows]


def _classify(email: InboundEmail, final_text: str, codes: list[dict]) -> str:
    """Label the processed email with one existing classification code.

    Reads the email AND the agent's conclusion, so the label reflects what the
    agent determined. Uses the cheap model with a forced single-code choice;
    any failure (or an unknown code) falls back to ``unclassified``.
    """
    valid = {c["code"] for c in codes}
    fallback = _FALLBACK_CODE if _FALLBACK_CODE in valid else (codes[0]["code"] if codes else _FALLBACK_CODE)
    if not codes:
        return fallback

    catalogue = "\n".join(f"- {c['code']}: {c['label']} — {c['description']}" for c in codes)
    tool = {
        "name": "record_classification",
        "description": "Record the single best classification code for this email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "enum": sorted(valid),
                    "description": "The single best-fitting classification code.",
                }
            },
            "required": ["code"],
        },
    }
    user = (
        "Classify this inbound operations email as exactly ONE of the codes below, "
        "using the email content and what the agent concluded. If none fits well, "
        f"use '{fallback}'.\n\n"
        f"Codes:\n{catalogue}\n\n"
        f"From: {email.from_address}\n"
        f"Subject: {email.subject}\n"
        f"Body:\n{email.body[:2000]}\n\n"
        f"What the agent concluded:\n{final_text[:2000]}"
    )
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=_CLASSIFIER_MAX_TOKENS,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_classification"},
            messages=[{"role": "user", "content": user}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_classification":
                code = block.input.get("code")
                if code in valid:
                    return code
    except Exception as exc:  # the label is bookkeeping; never break the poll on it.
        logger.warning("inbound classify failed for %s: %s", email.gmail_message_id, exc)
    return fallback


# --- Dedup + record -----------------------------------------------------------


def _already_seen(gmail_message_id: str) -> bool:
    """True if this message is already in ``inbound_email_records``."""
    return bool(
        fetch_all(
            "SELECT 1 FROM inbound_email_records WHERE gmail_message_id = %s",
            (gmail_message_id,),
        )
    )


def _related_ids(run_id: int) -> tuple[int | None, int | None]:
    """The enrolment / order the incident actually touched, if unambiguous.

    Read off the run's logged tool calls (``agent_steps.tool_input``). We record
    an id only when exactly one distinct value was referenced — anything else
    stays NULL rather than guess.
    """
    enrolments: set[int] = set()
    orders: set[int] = set()
    for (tool_input,) in fetch_all(
        "SELECT tool_input FROM agent_steps WHERE run_id = %s", (run_id,)
    ):
        if not isinstance(tool_input, dict):
            continue
        for key in ("enrolment_id", "related_enrolment_id"):
            value = tool_input.get(key)
            if value is not None:
                try:
                    enrolments.add(int(value))
                except (TypeError, ValueError):
                    pass
        for key in ("order_id", "related_order_id"):
            value = tool_input.get(key)
            if value is not None:
                try:
                    orders.add(int(value))
                except (TypeError, ValueError):
                    pass
    enrolment_id = enrolments.pop() if len(enrolments) == 1 else None
    order_id = orders.pop() if len(orders) == 1 else None
    return enrolment_id, order_id


def _record_inbound(
    email: InboundEmail,
    classified_as: str,
    related_enrolment_id: int | None,
    related_order_id: int | None,
) -> None:
    """Insert the dedup record (idempotent on the gmail_message_id PK)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO inbound_email_records
                (gmail_message_id, received_at, from_address, subject,
                 classified_as, related_enrolment_id, related_order_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (gmail_message_id) DO NOTHING
            """,
            (
                email.gmail_message_id,
                email.received_at,
                email.from_address,
                email.subject,
                classified_as,
                related_enrolment_id,
                related_order_id,
            ),
        )
        conn.commit()


# --- Public entry point -------------------------------------------------------


def poll_topology() -> str:
    """A one-line description of the inbox topology for poller startup logs:
    ``reads=<polled inbox> sink=<demo sink>``. Makes it obvious, every time a
    poller starts, which inbox is read and where outbound demo mail is routed — so
    a misconfiguration that points the sink at the polled inbox is visible at a
    glance. Best-effort: an unresolvable inbox shows as ``unknown``.
    """
    try:
        inbox = GmailClient().address() or "unknown"
    except Exception:
        inbox = "unknown"
    if settings.email_mode == "demo":
        sink = settings.demo_sink_email or "(unset)"
    else:
        sink = "(live mode — no redirect)"
    return f"reads={inbox} sink={sink}"


def poll_inbox(max_results: int = 25, unread_only: bool = True) -> list[ProcessedEmail]:
    """Run one poll cycle: process every NEW message, return what was handled.

    Lists unread (or recent) inbox messages, skips any already recorded, and for
    each new one drives ``run_incident`` then records + marks-read. Already-seen
    messages are skipped silently (idempotent). A per-message failure is captured
    on the returned ``ProcessedEmail`` and leaves no record, so it retries next poll.
    """
    client = GmailClient()
    stubs = client.list_unread(max_results) if unread_only else client.list_recent(max_results)
    codes = _active_codes()
    try:
        own_address = client.address()
    except Exception:  # identity lookup is best-effort; fall back to subject-only guard.
        own_address = ""

    processed: list[ProcessedEmail] = []
    for stub in stubs:
        gmail_message_id = stub.get("id")
        if not gmail_message_id or _already_seen(gmail_message_id):
            continue

        email: InboundEmail | None = None
        try:
            email = _parse_message(client.get_message(gmail_message_id))

            # Loop guard: never wake an incident on our own sent / demo mail. Mark
            # it read so it leaves the unread queue, and move on without recording
            # it as inbound or sending anything.
            if _is_own_outbound(email, own_address):
                logger.info(
                    "inbound skip: own outbound/demo mail msg=%s from=%r subject=%r",
                    gmail_message_id, email.from_address, email.subject,
                )
                try:
                    client.mark_read(gmail_message_id)
                except Exception:  # best-effort; the guard re-skips it next poll anyway.
                    logger.warning("inbound: could not mark own mail read msg=%s", gmail_message_id)
                continue

            run = run_incident(
                trigger_reason="inbound_email", task=_format_task(email),
                extra_context={"inbound_from_address": sender_address(email)},
            )
            classified_as = _classify(email, run.final_text, codes)
            related_enrolment_id, related_order_id = _related_ids(run.run_id)

            _record_inbound(email, classified_as, related_enrolment_id, related_order_id)
            client.mark_read(gmail_message_id)

            processed.append(
                ProcessedEmail(
                    email=email,
                    run_id=run.run_id,
                    classified_as=classified_as,
                    final_text=run.final_text,
                    step_count=run.step_count,
                    related_enrolment_id=related_enrolment_id,
                    related_order_id=related_order_id,
                )
            )
            logger.info(
                "inbound processed msg=%s run=%s classified=%s",
                gmail_message_id, run.run_id, classified_as,
            )
        except Exception as exc:  # one bad message must not sink the whole poll.
            logger.exception("inbound failed for msg=%s", gmail_message_id)
            placeholder = email or InboundEmail(
                gmail_message_id=gmail_message_id,
                from_address="",
                to_address="",
                subject="",
                body="",
                received_at=datetime.now(timezone.utc),
            )
            processed.append(
                ProcessedEmail(
                    email=placeholder,
                    run_id=None,
                    classified_as=None,
                    final_text="",
                    step_count=0,
                    related_enrolment_id=None,
                    related_order_id=None,
                    error=str(exc),
                )
            )
    return processed
