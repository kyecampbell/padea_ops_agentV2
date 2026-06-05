"""Tool registry and dispatch.

Responsibility: own the agent's tool belt. Register every tool (currently the
read tools from `src/tools/query.py`), expose their JSON schemas to the
Anthropic API, and route each tool call to its implementation.

Critical contract: tools never throw raw exceptions at the agent. Every dispatch
is wrapped so the result is always a typed outcome (see `src/tools/results.py`):
found / empty / ambiguous / conflict / unavailable / error. The orchestrator
reasons over these typed results, not over exceptions.

The full belt is registered: read tools (`query`), write tools (`writes`), the
dietary-safety recompute (`eligible_pool`), the email tool (`email`),
escalate-to-human (`escalate`), and the four deterministic Thursday-batch tools
(`compose_week`, `apply_flexible_resolution`, `send_prefs_requests`,
`send_caterer_orders`) the agent supervises in order. Dispatch is gate-agnostic —
it just routes calls and returns typed results. The autonomous-vs-approval
ENFORCEMENT lives in `src/agent/loop.py`, which consults `gates.py` before each
dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

from src.tools import (
    caterer_quality_summary,
    eligible_pool,
    email,
    escalate,
    order_email,
    orders_batch,
    parent_prefs,
    query,
    student_choice,
    writes,
)
from src.tools.results import ToolResult, error, found

# --- Tool definition ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tool:
    """One entry in the tool belt: its name, what it does, the function that
    implements it, and the typed parameters the LLM may pass.

    `parameters` maps each argument name to a JSON-Schema property fragment
    (at minimum a ``type`` and a ``description``). `required` lists the
    arguments that must be present. Both feed the Anthropic tool schema and the
    argument coercion in `dispatch`.
    """

    name: str
    description: str
    func: Callable[..., ToolResult]
    parameters: dict[str, dict[str, Any]]
    required: tuple[str, ...] = ()
    # Ambient run-context to inject into the call, as (func_kwarg, context_key)
    # pairs. These kwargs are NOT exposed to the model (not in `parameters`); the
    # orchestrator supplies them so tool output (escalations, emails, batch
    # composition) links back to the current agent_runs row. Skipped when the
    # caller passes no context (e.g. the enforcement test), so the default of no
    # injection keeps every tool callable standalone.
    context_args: tuple[tuple[str, str], ...] = ()

    @property
    def schema(self) -> dict[str, Any]:
        """The Anthropic API tool schema for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": list(self.required),
            },
        }


def _id_param(entity: str) -> dict[str, dict[str, Any]]:
    """A single integer id parameter named ``<entity>_id``."""
    key = f"{entity}_id"
    return {key: {"type": "integer", "description": f"The {entity} id (integer)."}}


def _compose_week_tool(
    week_of: str, only_caterer_id: int | None = None, run_id: int | None = None
) -> ToolResult:
    """Tool adapter for the deterministic Thursday-batch calculator.

    Parses the ISO ``week_of`` the model passes (the calculator takes a ``date``)
    and runs ``orders_batch.compose_week`` for it. ``run_id`` is injected from the
    incident context so the per-student / caterer-wide escalations the calculator
    raises link back to this run; the model never supplies it. The result is the
    full plan: per-caterer summaries with the meal-by-meal breakdown, defaulted
    lines (with parent contact), and per-student stragglers.
    """
    try:
        wk = date.fromisoformat(week_of)
    except (TypeError, ValueError):
        return error(f"week_of must be an ISO date (YYYY-MM-DD); got {week_of!r}.")
    return orders_batch.compose_week(wk, run_id=run_id, only_caterer_id=only_caterer_id)


def _parse_week(week_of: str) -> date | ToolResult:
    """Parse the ISO ``week_of`` the model passes, or a typed error."""
    try:
        return date.fromisoformat(week_of)
    except (TypeError, ValueError):
        return error(f"week_of must be an ISO date (YYYY-MM-DD); got {week_of!r}.")


def _apply_flexible_resolution_tool(week_of: str) -> ToolResult:
    """Tool adapter: flexible resolution for non-responders (one bounded call).

    For every defaulted, dietary-known student already asked for preferences in a
    PRIOR run, set their term preference to "any eligible meal" so the next
    composition rotates them a real pick instead of defaulting them again. A DATA
    change only — sends nothing. Returns the resolved enrolment ids; a non-empty
    list means the agent should re-run ``compose_week`` before ordering.
    """
    wk = _parse_week(week_of)
    if isinstance(wk, ToolResult):
        return wk
    return parent_prefs.resolve_non_responders(wk)


def _send_prefs_requests_tool(week_of: str, run_id: int | None = None) -> ToolResult:
    """Tool adapter: send the one-time parent prefs requests for the week (one
    bounded, idempotent call — never per-email looping). Sends exactly one
    ``parent_prefs_request`` per first-time defaulted student, skipping anyone
    already asked. Returns the students it sent to and those it skipped.
    """
    wk = _parse_week(week_of)
    if isinstance(wk, ToolResult):
        return wk
    return parent_prefs.send_prefs_requests(wk, run_id=run_id)


def _send_caterer_orders_tool(week_of: str, run_id: int | None = None) -> ToolResult:
    """Tool adapter: send the caterer order emails for the week (one bounded,
    idempotent call). Sends EXACTLY ONE ``session_order`` email per caterer with a
    sendable composed order — the full per-session student manifests — skipping any
    caterer already sent for the week. Never per-session, never per-email looping.
    Returns the caterers sent, skipped (with reasons), and any send failures.
    """
    wk = _parse_week(week_of)
    if isinstance(wk, ToolResult):
        return wk
    return order_email.send_caterer_orders(wk, run_id=run_id)


def _options_result(opts) -> ToolResult:
    """Wrap a ``ChoiceOptions`` in a typed ``found`` (the agent reads ``data``)."""
    if not opts.has_options:
        return found(
            opts.as_dict(),
            f"{opts.student_name} has no safe offered options this week ({opts.reason}); "
            "do not assign a meal — confirm or let them fall back.",
        )
    return found(
        opts.as_dict(),
        f"{opts.student_name} (enrolment {opts.enrolment_id}): {len(opts.options)} safe "
        f"option(s) for {opts.upcoming_session_date}"
        + (f"; last week's meal was {opts.last_meal.item}." if opts.last_meal else "; no meal last week."),
    )


def _identify_choice_reply_tool(subject: str = "", body: str = "") -> ToolResult:
    """Tool adapter: deterministically resolve a choose-and-rate reply to the student
    (via the reference token) and return their numbered options. A ``conflict`` (no
    token) tells the agent to fall back to identifying the student from the body.
    """
    opts = student_choice.identify_reply(subject, body)
    if isinstance(opts, ToolResult):
        return opts
    return _options_result(opts)


def _get_caterer_weekly_summary_tool(caterer_id: int) -> ToolResult:
    """Tool adapter: one caterer's Monday quality scorecard DATA + the rendered draft
    (warm partner scorecard). Read-only — the agent reviews per-school student
    satisfaction, the noise-filtered recurring themes, reliability signals, and the
    strong-performer / concern flags before the deterministic send."""
    week = orders_batch.upcoming_monday(date.today()) - timedelta(days=7)
    data = caterer_quality_summary.summary_data(caterer_id, week)
    if isinstance(data, ToolResult):
        return data
    rendered = caterer_quality_summary.render_caterer_weekly_summary(caterer_id, week)
    if isinstance(rendered, ToolResult):
        return rendered
    subject, body = rendered
    payload = data.as_dict()
    payload["draft_subject"] = subject
    payload["draft_body"] = body
    return found(
        payload,
        f"{data.caterer_name}: overall {data.overall_avg}/5 across {data.overall_count} "
        f"student ratings; {len(data.themes)} recurring theme(s), "
        f"{len(data.dropped_noise)} one-off(s) filtered; "
        f"{'strong performer' if data.strong_performer else 'not a capacity-ask candidate'}.",
    )


def _send_caterer_weekly_summaries_tool(week_of: str, run_id: int | None = None) -> ToolResult:
    """Tool adapter: send ONE warm quality scorecard per caterer with student ratings
    for the week (idempotent, autonomous). Never per-caterer looping by hand — this
    is the single send path. Returns caterers sent / skipped / failed."""
    wk = _parse_week(week_of)
    if isinstance(wk, ToolResult):
        return wk
    return caterer_quality_summary.send_caterer_weekly_summaries(wk, run_id=run_id)


def _get_student_choice_options_tool(enrolment_id: int) -> ToolResult:
    """Tool adapter: the student's numbered choose-and-rate options for the upcoming
    week — their dietary-safe, MOQ-bounded menu for the next session, the session
    being chosen for, and the meal they had last week (the one to rate). Read-only;
    the agent uses the numbers to map a reply's pick to a menu_item_id.
    """
    opts = student_choice.build_choice_options(
        enrolment_id, student_choice.default_reply_week(enrolment_id)
    )
    if isinstance(opts, ToolResult):
        return opts
    # A clean, typed signal rather than an error when there are no options: this
    # student has nothing safe to offer this week (escalated / blank dietary / no
    # caterer) — the agent should fall back or confirm, never invent a meal.
    return _options_result(opts)


# --- Registry (the full tool belt) -------------------------------------------

_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_enrolment",
        description=(
            "Fetch one enrolment (student-at-school) by id, including its raw "
            "dietary string and the dietary tag names the student requires."
        ),
        func=query.get_enrolment,
        parameters=_id_param("enrolment"),
        required=("enrolment_id",),
    ),
    Tool(
        name="list_active_enrolments",
        description=(
            "List the active enrolments at a school (not opted out, within the "
            "current enrolment period). Each row carries its dietary tag names."
        ),
        func=query.list_active_enrolments,
        parameters=_id_param("school"),
        required=("school_id",),
    ),
    Tool(
        name="get_caterer",
        description="Fetch one caterer by id (contact details, delivery, GST).",
        func=query.get_caterer,
        parameters=_id_param("caterer"),
        required=("caterer_id",),
    ),
    Tool(
        name="get_caterer_for_school",
        description="Fetch the caterer currently assigned to a school.",
        func=query.get_caterer_for_school,
        parameters=_id_param("school"),
        required=("school_id",),
    ),
    Tool(
        name="get_caterer_feedback",
        description=(
            "Fetch a caterer's recent feedback over the last N weeks, shaped to "
            "judge the TREND. Returns the overall mean rating, a per-week breakdown "
            "(count / mean / min rating, oldest to newest — a decline shows as a "
            "falling weekly mean), every rated row that carried a manager comment "
            "(late / cold / wrong / dietary, newest first), and how many times each "
            "manager quality check was answered 'no' in the window. Use this for the "
            "weekly quality review and for inbound complaints before judging a caterer."
        ),
        func=query.get_caterer_feedback,
        parameters={
            "caterer_id": {"type": "integer", "description": "The caterer id (integer)."},
            "weeks": {
                "type": "integer",
                "description": "How many weeks back to look (default 4). Use a wider window to see a trend.",
            },
        },
        required=("caterer_id",),
    ),
    Tool(
        name="get_caterer_moq_tiers",
        description=(
            "Fetch a caterer's minimum-order-quantity tiers "
            "(variety_count -> min_total_items)."
        ),
        func=query.get_caterer_moq_tiers,
        parameters=_id_param("caterer"),
        required=("caterer_id",),
    ),
    Tool(
        name="get_menu_items",
        description=(
            "Fetch a caterer's active menu items. Each item has its name, "
            "contents_text, tweaks_text, price_cents (integer cents), and the "
            "dietary_tag_names it is certified to satisfy."
        ),
        func=query.get_menu_items,
        parameters=_id_param("caterer"),
        required=("caterer_id",),
    ),
    Tool(
        name="get_all_dietary_tags",
        description=(
            "List the dietary vocabulary (all active dietary tags: name, label, "
            "description). Use to learn the exact tag name for a dietary need."
        ),
        func=query.get_all_dietary_tags,
        parameters={},
        required=(),
    ),
    # --- Write tools ---------------------------------------------------------
    Tool(
        name="update_term_meal_preference",
        description=(
            "Replace a student's ranked term meal-preference items "
            "(highest preference first). Every item must be on the student's "
            "current caterer's menu AND dietary-eligible for them, or the whole "
            "change is rejected (conflict). Changing a preference after the "
            "session's order has been sent requires operator approval."
        ),
        func=writes.update_term_meal_preference,
        parameters={
            "enrolment_id": {"type": "integer", "description": "The enrolment id (integer)."},
            "ranked_menu_item_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Menu item ids, highest preference first (rank 1 = first).",
            },
        },
        required=("enrolment_id", "ranked_menu_item_ids"),
    ),
    Tool(
        name="record_dietary_update",
        description=(
            "Update a student's raw dietary note (enrolments.dietary_raw). Records "
            "the verbatim note only; follow with recompute_eligible_meals so the "
            "derived dietary tags and eligible pool reflect the change. Changing "
            "this after the session's order has been sent requires operator approval."
        ),
        func=writes.record_dietary_update,
        parameters={
            "enrolment_id": {"type": "integer", "description": "The enrolment id (integer)."},
            "new_dietary_raw": {
                "type": "string",
                "description": "The new verbatim dietary note (use '' to clear it).",
            },
        },
        required=("enrolment_id", "new_dietary_raw"),
    ),
    Tool(
        name="add_enrolment",
        description=(
            "Add a new enrolment (student-at-school). Adding a student is a "
            "billing + identity action and ALWAYS requires operator approval."
        ),
        func=writes.add_enrolment,
        parameters={
            "school_id": {"type": "integer", "description": "The school id (integer)."},
            "student_name": {"type": "string", "description": "The student's full name."},
            "parent_name": {"type": "string", "description": "The parent's full name."},
            "parent_email": {"type": "string", "description": "The parent's email address."},
            "year_level": {"type": "integer", "description": "The student's year level (optional)."},
            "dietary_raw": {"type": "string", "description": "Raw dietary note, if any (optional)."},
        },
        required=("school_id", "student_name", "parent_name", "parent_email"),
    ),
    Tool(
        name="update_menu_item_description",
        description=(
            "Record a caterer's authoritative clarification of a menu item's text "
            "(contents_text and/or tweaks_text). Only the field(s) supplied are "
            "changed. Autonomous (records a fact); follow with recompute_eligible_meals."
        ),
        func=writes.update_menu_item_description,
        parameters={
            "menu_item_id": {"type": "integer", "description": "The menu item id (integer)."},
            "contents_text": {"type": "string", "description": "New contents text (optional)."},
            "tweaks_text": {"type": "string", "description": "New tweaks text (optional)."},
        },
        required=("menu_item_id",),
    ),
    Tool(
        name="resolve_escalation",
        description=(
            "Mark an escalation resolved, with a resolution note and the resolver. "
            "Closes a human-facing question, so it ALWAYS requires operator approval."
        ),
        func=writes.resolve_escalation,
        parameters={
            "escalation_id": {"type": "integer", "description": "The escalation id (integer)."},
            "resolution": {"type": "string", "description": "The resolution note."},
            "resolved_by": {"type": "string", "description": "Who resolved it."},
        },
        required=("escalation_id", "resolution", "resolved_by"),
    ),
    # --- Dietary-safety recompute -------------------------------------------
    Tool(
        name="recompute_eligible_meals",
        description=(
            "Recompute and persist one student's dietary-safe meal pool "
            "(student_eligible_meals) from their current dietary note and their "
            "caterer's menu, escalating any item too ambiguous to call safe. "
            "Idempotent. Run after a dietary note or menu description change."
        ),
        func=eligible_pool.recompute_eligible_meals,
        parameters=_id_param("enrolment"),
        required=("enrolment_id",),
    ),
    # --- Weekly batch composition (read/compute; autonomous) -----------------
    Tool(
        name="compose_week",
        description=(
            "Compose every caterer's consolidated order for a week (the Thursday "
            "batch's deterministic calculator). Read/compute only — it sizes each "
            "caterer's MOQ-safe order, rotates each student's meal, writes the "
            "orders, and raises the per-student / caterer-wide escalations itself. "
            "Returns the plan: per caterer, a status (composed | escalated), the "
            "meal-by-meal breakdown (item -> quantity) + week total, the caterer's "
            "contact email, the defaulted-pending-confirmation lines (each with the "
            "student and parent contact), and the escalated stragglers (unknown "
            "dietary / no safe meal — no line ordered). Act on this result: send "
            "each composed caterer its order email, email each defaulted student's "
            "parent, and surface the escalations."
        ),
        func=_compose_week_tool,
        parameters={
            "week_of": {
                "type": "string",
                "description": "The Monday of the target week as an ISO date (YYYY-MM-DD).",
            },
            "only_caterer_id": {
                "type": "integer",
                "description": "Restrict composition to one caterer (optional; omit for all caterers).",
            },
        },
        required=("week_of",),
        context_args=(("run_id", "run_id"),),
    ),
    Tool(
        name="apply_flexible_resolution",
        description=(
            "Flexible resolution for non-responders (the Thursday batch's step 2). "
            "One bounded, deterministic, idempotent call — NOT per student. For every "
            "defaulted, dietary-KNOWN student already sent a prefs request in a PRIOR "
            "run, it sets their term preference to 'any eligible meal' so a re-compose "
            "rotates them a real pick instead of defaulting them again. A DATA change "
            "only — sends nothing. Returns the resolved enrolment ids; if any were "
            "resolved, call compose_week again before ordering so the change takes effect."
        ),
        func=_apply_flexible_resolution_tool,
        parameters={
            "week_of": {
                "type": "string",
                "description": "The Monday of the target week as an ISO date (YYYY-MM-DD).",
            },
        },
        required=("week_of",),
    ),
    Tool(
        name="send_prefs_requests",
        description=(
            "Send the one-time parent meal-preference requests for the week (the "
            "Thursday batch's step 3). One bounded, idempotent call — NOT per email. "
            "Sends exactly one parent_prefs_request to each FIRST-TIME defaulted "
            "student's parent (with their safe menu choices + a dietary-confirmation "
            "line), skipping anyone already asked. Returns the students sent to and "
            "those skipped. Re-running never re-sends."
        ),
        func=_send_prefs_requests_tool,
        parameters={
            "week_of": {
                "type": "string",
                "description": "The Monday of the target week as an ISO date (YYYY-MM-DD).",
            },
        },
        required=("week_of",),
        context_args=(("run_id", "run_id"),),
    ),
    Tool(
        name="send_caterer_orders",
        description=(
            "Send the caterer order emails for the week (the Thursday batch's step 4). "
            "One bounded, idempotent call — NOT per caterer, NOT per session. Sends "
            "EXACTLY ONE session_order email per caterer with a sendable composed order "
            "— the full per-session student manifests — and skips any caterer already "
            "sent for the week, so a re-run never double-sends. Do NOT hand-send these "
            "yourself; this tool is the only way to send them. Returns the caterers it "
            "sent to, those it skipped (with reasons), and any send failures — assess "
            "the result and HOLD + escalate if anything failed."
        ),
        func=_send_caterer_orders_tool,
        parameters={
            "week_of": {
                "type": "string",
                "description": "The Monday of the target week as an ISO date (YYYY-MM-DD).",
            },
        },
        required=("week_of",),
        context_args=(("run_id", "run_id"),),
    ),
    # --- Weekly student choose-and-rate (inbound reply handling) -------------
    Tool(
        name="identify_choice_reply",
        description=(
            "Attribute a student's choose-and-rate REPLY to the correct student + "
            "session, deterministically, from the reference token (PADEA-CHOICE-<id>-"
            "<week>) carried in the email subject/body. Returns that student's numbered "
            "safe options + last week's meal — exactly like get_student_choice_options, "
            "but resolved from the reply itself. ALWAYS use this FIRST on a "
            "choose-and-rate reply: every student shares one demo inbox, so the From "
            "address is NOT an identity signal and you must match on the token, never "
            "guess by sender or pick 'the first open request'. If it returns a conflict "
            "(no token found), fall back to identifying the student from the body."
        ),
        func=_identify_choice_reply_tool,
        parameters={
            "subject": {"type": "string", "description": "The reply email's full subject line (incl. any 'Re:')."},
            "body": {"type": "string", "description": "The reply email's full body (incl. any quoted original)."},
        },
        required=("subject", "body"),
    ),
    Tool(
        name="get_student_choice_options",
        description=(
            "Fetch a student's weekly CHOOSE-AND-RATE options for the upcoming week: "
            "the NUMBERED list of dietary-safe, MOQ-bounded meals they may pick from "
            "for their next session (offered set ∩ their safe pool — every option is "
            "safe and within the variety ceiling), the session date being chosen for, "
            "and the meal they had LAST week (the one they're rating). Use this when a "
            "student replies to a choose-and-rate email: map their stated number/meal "
            "to a menu_item_id here BEFORE recording the pick. If it returns no "
            "options, do NOT assign a meal — let them fall back or confirm."
        ),
        func=_get_student_choice_options_tool,
        parameters=_id_param("enrolment"),
        required=("enrolment_id",),
    ),
    Tool(
        name="record_student_meal_choice",
        description=(
            "Record a student's weekly PICK (from their choose-and-rate reply) as a "
            "one-off meal request for their upcoming session. The menu_item_id MUST be "
            "one of their safe options from get_student_choice_options — an ineligible "
            "or off-menu pick is rejected (conflict), so you never assign an unsafe "
            "meal (fall back or ask instead). The Thursday batch then prefers this pick "
            "over the usual rotation/default. Changing the pick after that session's "
            "order has been sent requires operator approval."
        ),
        func=student_choice.record_meal_choice,
        parameters={
            "enrolment_id": {"type": "integer", "description": "The enrolment id (integer)."},
            "menu_item_id": {
                "type": "integer",
                "description": "The picked meal's menu_item_id (must be one of the student's safe options).",
            },
        },
        required=("enrolment_id", "menu_item_id"),
    ),
    Tool(
        name="record_student_meal_rating",
        description=(
            "Record a student's RATING (1-5) and optional free-text comment of last "
            "week's meal, from their choose-and-rate reply, as student feedback. Feeds "
            "the same caterer quality signal as tutor/manager feedback. Autonomous "
            "(recording a fact). Links last week's meal automatically when there is one."
        ),
        func=student_choice.record_meal_rating,
        parameters={
            "enrolment_id": {"type": "integer", "description": "The enrolment id (integer)."},
            "rating": {"type": "integer", "description": "The student's rating, 1 (poor) to 5 (great)."},
            "comment": {"type": "string", "description": "The student's free-text comment (optional)."},
        },
        required=("enrolment_id", "rating"),
    ),
    # --- Weekly per-caterer quality scorecard --------------------------------
    Tool(
        name="get_caterer_weekly_summary",
        description=(
            "Fetch one caterer's Monday QUALITY SCORECARD data + a rendered draft: "
            "meals served, STUDENT satisfaction per school (with standout + soft "
            "spot) and overall, the RECURRING student themes (one-off noise already "
            "filtered out), the four manager reliability signals (on-time / counts / "
            "dietary / temperature, with failed counts), and whether they're a clean "
            "strong performer. Read-only. Use it to review a caterer's week before "
            "the scorecards go out."
        ),
        func=_get_caterer_weekly_summary_tool,
        parameters=_id_param("caterer"),
        required=("caterer_id",),
    ),
    Tool(
        name="send_caterer_weekly_summaries",
        description=(
            "Send the warm weekly quality SCORECARD to every caterer with student "
            "ratings for the week — ONE per caterer, idempotent (a re-run sends 0). "
            "Each is a partner scorecard: genuine specific praise first, per-school "
            "student satisfaction, the recurring themes behind it, a gentle service "
            "note from manager reliability, and a capacity ask only for a clean "
            "strong performer. Autonomous (a factual appraisal tied to real numbers); "
            "a formal warning / RFP / cancellation is NOT this — those stay operator- "
            "gated. Returns caterers sent / skipped (with reasons) / failed; assess "
            "the result and HOLD + escalate if anything failed."
        ),
        func=_send_caterer_weekly_summaries_tool,
        parameters={
            "week_of": {
                "type": "string",
                "description": "The Monday of the week to summarise as an ISO date (YYYY-MM-DD).",
            },
        },
        required=("week_of",),
        context_args=(("run_id", "run_id"),),
    ),
    # --- Inbound reply + identity reconciliation -----------------------------
    Tool(
        name="reply_to_sender",
        description=(
            "Reply to the person who sent the inbound email you're handling. Unlike "
            "send_email (agent-INITIATED mail, demo-routed to the sink), this delivers "
            "to the ACTUAL sender's address even in demo mode — because they are a real "
            "correspondent who wrote in. You do NOT supply the address; it is the "
            "inbound sender's address, injected automatically. Use this for every reply "
            "to an inbound email (clarifications, confirmations, the contact-email update "
            "offer). Factual/operational — autonomous — but the commercial-intent "
            "backstop still applies, so a reply that reads commercial is queued for "
            "approval. Only valid while handling an inbound email."
        ),
        func=email.send_reply,
        parameters={
            "subject": {"type": "string", "description": "The reply subject line."},
            "body": {"type": "string", "description": "The plain-text reply body."},
            "related_enrolment_id": {"type": "integer", "description": "The enrolment this concerns (optional, for the audit trail)."},
        },
        required=("subject", "body"),
        context_args=(("to", "inbound_from_address"), ("related_run_id", "run_id")),
    ),
    Tool(
        name="update_contact_email",
        description=(
            "Update a student's on-record contact email (field 'parent_email' or "
            "'student_email') during inbound identity reconciliation. Call this ONLY "
            "after the person has EXPLICITLY confirmed, in their email, that they want "
            "the on-file address changed to the one they're writing from — never "
            "silently and never on a guess. A reversible data change (autonomous), but "
            "the explicit confirmation is required by policy. If you're unsure who they "
            "are, do not call this — ask or escalate."
        ),
        func=writes.update_contact_email,
        parameters={
            "enrolment_id": {"type": "integer", "description": "The enrolment id (integer)."},
            "new_email": {"type": "string", "description": "The confirmed new email (normally the sender's reply-to address)."},
            "field": {"type": "string", "description": "'parent_email' or 'student_email' — which contact to update."},
        },
        required=("enrolment_id", "new_email", "field"),
    ),
    # --- Communication -------------------------------------------------------
    Tool(
        name="send_email",
        description=(
            "Send an outbound email (or, for the approval-gated commercial kinds, "
            "queue it for operator approval — it is NOT sent until approved). "
            "Factual / operational kinds send autonomously: parent_enrolment, "
            "parent_reminder, opt_back_in_request_to_parent, operator_notification, "
            "caterer_service_note (a polite note to a caterer about a minor, fixable "
            "issue), other, AND the safety-vetted Thursday-batch output session_order "
            "(the caterer's order) and weekly_consolidated_summary. Commercial / "
            "relational kinds that embody a fresh judgment are queued for approval: "
            "warning, rfp, cancellation, rfp_loser_courtesy. The email is logged "
            "either way."
        ),
        func=email.send_email,
        parameters={
            "email_type": {
                "type": "string",
                "description": "The kind of email; drives the approval gate (see the tool description).",
            },
            "to": {"type": "string", "description": "The real recipient address."},
            "subject": {"type": "string", "description": "The subject line."},
            "body": {"type": "string", "description": "The plain-text body."},
            "cc": {"type": "string", "description": "Comma-separated cc addresses (optional)."},
            "related_caterer_id": {"type": "integer", "description": "Related caterer id (optional)."},
            "related_enrolment_id": {"type": "integer", "description": "Related enrolment id (optional)."},
            "related_order_id": {"type": "integer", "description": "Related order id (optional)."},
        },
        required=("email_type", "to", "subject", "body"),
        context_args=(("related_run_id", "run_id"),),
    ),
    Tool(
        name="escalate_to_human",
        description=(
            "Raise an OPEN escalation for the operator when you are unsure or a "
            "rule requires human judgment. Records the question and any structured "
            "context, and surfaces it in the decision feed. Returns the escalation id. "
            "Caterer escalations are de-duplicated on the caterer: if you escalate a "
            "caterer (related_caterer_id) that already has a recent OPEN caterer-wide "
            "escalation, your evidence is APPENDED to that one thread instead of "
            "creating a duplicate (the result will say appended: true) — even if a "
            "specific student's complaint triggered it."
        ),
        func=escalate.escalate_to_human,
        parameters={
            "question": {"type": "string", "description": "What you need the operator to decide."},
            "context": {
                "type": "object",
                "description": "Structured detail to help the operator decide (optional).",
            },
            "related_caterer_id": {"type": "integer", "description": "Related caterer id (optional)."},
            "related_enrolment_id": {"type": "integer", "description": "Related enrolment id (optional)."},
            "related_order_id": {"type": "integer", "description": "Related order id (optional)."},
            "related_step_id": {"type": "integer", "description": "Related agent step id (optional)."},
        },
        required=("question",),
        context_args=(("related_run_id", "run_id"),),
    ),
)

_REGISTRY: dict[str, Tool] = {t.name: t for t in _TOOLS}


# --- Schemas + dispatch ------------------------------------------------------

# Maps a JSON-Schema ``type`` to the Python callable that coerces a raw value
# (LLM tool args sometimes arrive as strings) into that type.
_COERCERS: dict[str, Callable[[Any], Any]] = {
    "integer": int,
    "number": float,
    "string": str,
    "boolean": bool,
}


def tool_schemas() -> list[dict[str, Any]]:
    """The full tool-schema list to hand to the Anthropic API."""
    return [tool.schema for tool in _REGISTRY.values()]


def registered_tool_names() -> list[str]:
    """The names of all registered tools (used by tests / introspection)."""
    return list(_REGISTRY.keys())


def _coerce_args(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """Coerce and validate `args` against `tool`'s declared parameters.

    Unknown keys are dropped (the model occasionally hallucinates extras);
    declared keys are coerced to their schema type. Raises ValueError on a
    missing required argument or a value that won't coerce — the caller turns
    that into an `error` ToolResult.
    """
    coerced: dict[str, Any] = {}
    for name, spec in tool.parameters.items():
        if name not in args or args[name] is None:
            continue
        coercer = _COERCERS.get(spec.get("type", "string"), lambda v: v)
        try:
            coerced[name] = coercer(args[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"argument {name!r}={args[name]!r} is not a valid {spec.get('type')}") from exc

    missing = [name for name in tool.required if name not in coerced]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")
    return coerced


def dispatch(
    name: str, args: dict[str, Any] | None, context: dict[str, Any] | None = None
) -> ToolResult:
    """Route a tool call to its implementation, always returning a ToolResult.

    Unknown tool -> `error`. Bad / missing arguments -> `error`. Any unexpected
    exception from the tool itself is caught and returned as `error` so the
    orchestrator never sees a raw traceback (defence in depth — the read tools
    already trap their own DB failures).

    ``context`` carries ambient run state (currently just ``run_id``). A tool's
    declared ``context_args`` pull values out of it and pass them as extra kwargs,
    so escalations / emails / batch composition link back to the run. The model
    never sees or controls these, and a missing context simply skips injection.
    """
    tool = _REGISTRY.get(name)
    if tool is None:
        return error(f"Unknown tool {name!r}. Known tools: {', '.join(_REGISTRY)}.")

    try:
        coerced = _coerce_args(tool, args or {})
    except ValueError as exc:
        return error(f"Invalid arguments for {name}: {exc}")

    for kwarg, context_key in tool.context_args:
        value = (context or {}).get(context_key)
        if value is not None:
            coerced[kwarg] = value

    try:
        return tool.func(**coerced)
    except Exception as exc:  # pragma: no cover - tools shouldn't raise; backstop
        return error(f"Tool {name} raised unexpectedly: {exc!r}")
