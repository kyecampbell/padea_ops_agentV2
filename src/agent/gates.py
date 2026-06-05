"""Hard-rules gate — autonomous vs. requires-approval.

Responsibility: classify a proposed tool action as either ``autonomous`` (the
agent may execute it directly) or ``requires_approval`` (it must be queued for a
human). This is a deterministic policy gate, not an LLM judgment call — it is the
safety backstop behind the handbook. The two values match the `action_class`
reference codes in the schema, so each step's gate decision is auditable.

Actions that ALWAYS require human approval (enforced once the corresponding
tools land in the registry):
  - commercial emails (e.g. performance/quality emails to caterers),
  - money changes,
  - meal changes after an order has been sent,
  - adding a student,
  - anything irreversible.

Confidence is NEVER a gate. A high-confidence money change still requires
approval; a low-confidence read is still autonomous. The gate keys only on the
nature of the action (the tool), never on how sure the agent is.

Currently only read tools exist, and all reads are autonomous. The mapping is
scaffolded so write / email / escalate tools can be marked ``requires_approval``
as they are added, and any tool not explicitly classified defaults to
``requires_approval`` (fail safe).
"""

from __future__ import annotations

import re
from typing import Literal

ActionClass = Literal["autonomous", "requires_approval"]

# Explicit per-tool classification. Read tools are autonomous; write / email /
# escalate tools that are ALWAYS gated (regardless of state) are
# requires_approval. Tools whose verdict depends on the order-sent "money line"
# are handled separately by ``_ORDER_SENSITIVE`` below, not listed here.
_TOOL_ACTION_CLASS: dict[str, ActionClass] = {
    # --- Read tools (all autonomous) ---
    "get_enrolment": "autonomous",
    "list_active_enrolments": "autonomous",
    "get_caterer": "autonomous",
    "get_caterer_for_school": "autonomous",
    "get_caterer_moq_tiers": "autonomous",
    "get_caterer_feedback": "autonomous",
    "get_menu_items": "autonomous",
    "get_all_dietary_tags": "autonomous",
    # --- Write tools that are autonomous (recording a fact, reversible) ---
    "update_menu_item_description": "autonomous",  # records a caterer's clarification.
    "recompute_eligible_meals": "autonomous",      # idempotent dietary-safety recompute.
    # The Thursday-batch calculator: read/compute that composes the safe order and
    # raises its own escalations. Idempotent per (caterer, week); a re-run replaces
    # rather than duplicates. The binding step is the order EMAIL (gated per kind),
    # not this composition, so composing is autonomous.
    "compose_week": "autonomous",
    # The rest of the Thursday batch's deterministic, idempotent steps. Each is one
    # bounded call that only ever emits the safety-vetted batch output, so — like
    # compose_week and the session_order email — they are autonomous:
    #   - apply_flexible_resolution: a DATA change (sets a dietary-known non-responder's
    #     preference to their eligible pool); reversible, sends nothing.
    #   - send_prefs_requests: sends only the autonomous parent_prefs_request kind,
    #     idempotent per student.
    #   - send_caterer_orders: sends only the autonomous session_order kind off a
    #     vetted composed order, idempotent per (caterer, week).
    "apply_flexible_resolution": "autonomous",
    "send_prefs_requests": "autonomous",
    "send_caterer_orders": "autonomous",
    # Raising an escalation IS the agent's safe way to ASK for a human — gating it
    # behind approval is circular (the loop would intercept it and the escalations
    # row would never be created). Opening an escalation is benign + reversible.
    "escalate_to_human": "autonomous",
    # --- Write tools that ALWAYS require approval ---
    "add_enrolment": "requires_approval",      # billing + identity: a new student.
    "resolve_escalation": "requires_approval",  # closes a human-facing question.
}

# Tools whose classification keys on the order-state "money line": a meal /
# dietary change is autonomous while it is still cheap to make, but once a
# binding order has been sent for the affected session the change is no longer
# free — it must be approved by a human. The caller decides whether an order has
# been sent (via src.tools.order_state.has_order_been_sent) and passes the result
# in as ``order_sent``; the gate itself stays a pure, deterministic policy.
_ORDER_SENSITIVE: frozenset[str] = frozenset(
    {
        "update_term_meal_preference",
        "record_dietary_update",
    }
)

# Fail-safe default for any tool not explicitly classified above. A new tool is
# assumed to need approval until someone deliberately marks it autonomous.
_DEFAULT: ActionClass = "requires_approval"


def gate(tool_name: str, *, order_sent: bool | None = None) -> ActionClass:
    """Classify a tool call as ``autonomous`` or ``requires_approval``.

    For order-sensitive tools (meal-preference / dietary changes), ``order_sent``
    is the money line: ``False`` -> ``autonomous`` (no binding order has gone out
    yet), ``True`` -> ``requires_approval`` (changing it costs money). If
    ``order_sent`` is left ``None`` for such a tool — i.e. the caller could not
    determine the order state — the gate fails safe to ``requires_approval``.

    Unknown / unclassified tools also fail safe to ``requires_approval``.
    """
    if tool_name in _ORDER_SENSITIVE:
        # Autonomous ONLY when we positively know no order has been sent.
        return "autonomous" if order_sent is False else "requires_approval"
    return _TOOL_ACTION_CLASS.get(tool_name, _DEFAULT)


def requires_approval(tool_name: str, *, order_sent: bool | None = None) -> bool:
    """Predicate form of `gate`: True when the action must be approved."""
    return gate(tool_name, order_sent=order_sent) == "requires_approval"


def is_order_sensitive(tool_name: str) -> bool:
    """True for write tools whose verdict depends on the order-sent "money line".

    The orchestrator uses this to decide whether it must consult
    ``order_state.has_order_been_sent`` before gating the call.
    """
    return tool_name in _ORDER_SENSITIVE


# --- Email classification ----------------------------------------------------
# An email's gate keys only on its *kind*, not its content. Commercial / relational
# mail that embodies a fresh judgment (a quality warning, an RFP, a cancellation)
# always goes through a human. Factual / operational mail the agent may send itself:
# parent / operator notices AND the Thursday-batch output (the session order to a
# caterer and the week's consolidated summary) — those report a plan the
# deterministic batch already vetted for safety, so they are not a fresh commercial
# decision (see _EMAIL_ACTION_CLASS). The codes match email_type.code in the schema.
# Anything unrecognised fails safe to requires_approval.
_EMAIL_ACTION_CLASS: dict[str, ActionClass] = {
    # --- Commercial / money / binding — ALWAYS requires approval. ---
    "warning": "requires_approval",                      # quality/performance to a caterer.
    "rfp": "requires_approval",                          # request for proposal to candidates.
    "cancellation": "requires_approval",                 # cancelling an incumbent caterer.
    "rfp_loser_courtesy": "requires_approval",           # thanks-but-no-thanks to candidates.
    # --- Thursday-batch output — autonomous BECAUSE the batch is the safety gate. ---
    # The deterministic calculator (orders_batch.compose_week) only EVER composes a
    # caterer's order when it is provably safe: every dietary student is covered, no
    # MOQ floor is breached, and any student it can't place safely is escalated with
    # NO line rather than ordered. An unsafe order is never produced, so the order
    # email (and the week's consolidated summary) that reports a composed batch is a
    # factual readout of a vetted plan, not a fresh commercial judgment — hence
    # autonomous. (Per-student escalations still go to a human; meal changes AFTER an
    # order is sent are still order-sensitive and gated.)
    "session_order": "autonomous",                       # binding order off a vetted batch.
    "weekly_consolidated_summary": "autonomous",         # consolidated finance summary.
    # --- Factual / operational — autonomous. ---
    # A polite, low-stakes service note to a caterer about a minor, fixable issue
    # (a cold or late delivery, a one-off mix-up). It is NOT a commercial/relational
    # judgment — a formal quality WARNING (which embodies that judgment and may
    # precede an RFP) stays requires_approval above. Accumulating evidence of a real
    # decline is escalated to a human, never auto-warned (see the handbook).
    "caterer_service_note": "autonomous",                # minor, fixable issue note to a caterer.
    "parent_enrolment": "autonomous",                    # term-start enrolment email.
    "parent_reminder": "autonomous",                     # chase reminder to a parent.
    "parent_prefs_request": "autonomous",                # one-time meal-preferences request.
    "opt_back_in_request_to_parent": "autonomous",       # tutor-triggered opt-back-in.
    "operator_notification": "autonomous",               # system-to-operator notice.
    "other": "autonomous",                               # uncategorised factual mail.
}


def classify_email(email_type: str) -> ActionClass:
    """Classify an outbound email kind as ``autonomous`` or ``requires_approval``.

    Commercial / relational kinds that embody a fresh judgment (warning, rfp,
    cancellation, rfp_loser_courtesy) require human approval; factual / operational
    kinds — parent / operator notices and the safety-vetted Thursday-batch output
    (session_order, weekly_consolidated_summary) — are autonomous. An unknown
    ``email_type`` fails safe to ``requires_approval`` — we never auto-send mail we
    cannot classify.
    """
    return _EMAIL_ACTION_CLASS.get(email_type, _DEFAULT)


# --- Commercial-intent content backstop --------------------------------------
# ``email_type`` is supplied by the model, so the kind-based gate above can be
# defeated by a MISLABEL: a commercial warning / RFP / cancellation / price or
# performance nudge tagged as a benign kind (caterer_service_note, parent_*,
# operator_notification, other) would otherwise auto-send. This is a deterministic
# second line of defence: it scans the actual subject + body for commercial-
# CONSEQUENCE language and, if found, forces approval REGARDLESS of the declared
# type. It targets the binding/commercial intent itself (warning, terminate, RFP,
# price change, withholding money) — not mere quality wording or dollar figures —
# so the legitimately-autonomous kinds that DO contain prices (session_order,
# weekly_consolidated_summary) and minor service notes (a cold/late delivery) are
# not swept up. A false positive only costs one human approval; a false negative
# auto-sends a commercial email. We err toward approval.
_COMMERCIAL_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("formal warning", re.compile(r"\b(formal|final|first|second|written|official)\s+warning\b", re.I)),
    ("warning notice", re.compile(r"\b(issue|issuing|serve|put\w*\s+you\s+on)\b[^.]{0,40}\bnotice\b", re.I)),
    ("breach / non-compliance", re.compile(r"\b(breach(ed|ing)?|non[\-\s]?compliance|in\s+violation)\b", re.I)),
    ("corrective / improvement action", re.compile(r"\b(corrective action|improvement plan|remediation|performance (review|management|improvement))\b", re.I)),
    ("rfp / tender", re.compile(r"\b(request for (proposal|tender|quote)s?|\brfps?\b|go(ing)? to (tender|market)|invitation to (bid|tender)|seeking (new |alternative |other )?(caterer|supplier|provider|vendor|quote|proposal)s?)\b", re.I)),
    ("cancellation / termination", re.compile(r"\b(terminat(e|ing|ion)|cancel(l?ing|l?ation)?|discontinu(e|ing)|end(ing)?|wind(ing)?\s+down|cease)\b[^.]{0,40}\b(contract|agreement|arrangement|partnership|engagement|service|relationship)\b", re.I)),
    ("price / rate change", re.compile(r"\b(price|pricing|rate|fee)s?\b[^.]{0,30}\b(increase|rise|change|adjustment|review|renegotiat)\w*|\brenegotiat\w+", re.I)),
    ("payment withholding / penalty", re.compile(r"\b(withhold(ing)?|deduct(ing|ion)?|withdraw(ing)?)\b[^.]{0,30}\b(payment|fee|invoice|amount)\b|\b(financial penalty|liquidated damages|charge\w*\s+back)\b", re.I)),
    ("refund / compensation / rebate", re.compile(r"\b(refund|compensat(e|ion)|rebate|credit note)\b", re.I)),
)


def commercial_intent_signals(subject: str, body: str) -> list[str]:
    """Labels of any commercial-consequence phrases found in the subject/body.

    Empty list means the content reads as factual/operational. A non-empty list is
    the deterministic reason an otherwise-autonomous email must be re-gated to
    ``requires_approval`` (see ``email_requires_approval``).
    """
    text = f"{subject or ''}\n{body or ''}"
    return [label for label, pattern in _COMMERCIAL_INTENT_PATTERNS if pattern.search(text)]


def email_requires_approval(email_type: str, subject: str, body: str) -> tuple[bool, list[str]]:
    """Final send-path verdict for an email: kind gate OR content backstop.

    Returns ``(requires_approval, signals)``. ``requires_approval`` is True when
    EITHER the declared ``email_type`` is a commercial kind (``classify_email``)
    OR the content trips the commercial-intent backstop — so a mislabelled
    commercial email cannot auto-send. ``signals`` lists the content phrases that
    forced approval (empty when only the kind gate applied), for the audit trail.
    """
    if classify_email(email_type) == "requires_approval":
        return True, []
    signals = commercial_intent_signals(subject, body)
    return (bool(signals), signals)
