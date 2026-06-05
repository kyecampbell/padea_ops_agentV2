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
from datetime import date
from typing import Any, Callable

from src.tools import (
    eligible_pool,
    email,
    escalate,
    order_email,
    orders_batch,
    parent_prefs,
    query,
    writes,
)
from src.tools.results import ToolResult, error

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
