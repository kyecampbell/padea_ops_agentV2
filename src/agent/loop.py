"""Orchestrator loop — the agent's heartbeat.

Responsibility: drive one incident from trigger to resolution. The single
orchestrator (Claude, model ``claude-sonnet-4-6``) is woken by one of three
triggers — an inbound email (polled), the weekly Thursday batch, or a
tool-surfaced gap — and then runs the cycle:

    trigger -> reason -> act -> (repeat until done)

Each incident:
  - opens an `agent_runs` row (the trigger reason),
  - asks the LLM to reason and choose tool calls (schemas from `dispatch.py`),
  - ENFORCES the hard-rules gate before each call (see ``_enforce_and_dispatch``):
    autonomous calls execute; requires-approval calls are recorded as a pending
    proposal and NOT executed,
  - records one `agent_steps` row per tool call — step_index, tool_name,
    tool_input, tool_output_full, the model's reasoning, urgency (default
    ``none``), and the action_class the gate assigned,
  - feeds the typed result back to the model and loops, and
  - closes the run with its final text answer.

Gate enforcement (the safety spine):
  - The verdict is computed per call: order-sensitive writes
    (update_term_meal_preference, record_dietary_update) consult
    ``order_state.has_order_been_sent``; ``send_email`` uses
    ``gates.classify_email``; everything else uses ``gates.gate``.
  - ``autonomous`` -> the tool runs.
  - ``requires_approval`` -> the tool does NOT run. ``send_email`` is the one
    exception we still dispatch, because it self-records the mail as
    ``queued_for_approval`` (and never sends it); every other gated write is
    intercepted here and returned as a "queued — NOT yet applied" result, with
    the intended call logged to ``agent_steps`` (action_class requires_approval)
    as the proposal. Applying an approved write (approve -> execute) is wired
    later with the decision UI.

Guardrail: a per-incident call cap (from runtime_config.yaml) bounds how many
LLM reasoning calls a single incident may spend. On the final allowed call the
tools are withheld so the model must conclude in text rather than run away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import anthropic
from psycopg.types.json import Jsonb

from config.settings import settings
from src.agent.context import (
    assemble_context,
    parse_lesson_citations,
    parse_policy_citations,
)
from src.agent.dispatch import dispatch, tool_schemas
from src.agent.gates import ActionClass, classify_email, gate, is_order_sensitive
from src.db.connection import get_conn
from src.tools.order_state import has_order_been_sent
from src.tools.results import ToolResult, queued

MODEL = "claude-sonnet-4-6"
# Headroom for a reasoning turn that emits several tool calls at once — e.g. a
# Thursday-batch turn sending a caterer order email with a full meal-by-meal
# breakdown plus parent emails — so a turn isn't truncated mid tool_use.
_MAX_TOKENS = 4096

_BASE_SYSTEM = """\
You are the operations orchestrator for Padea, a tutoring company that caters
dinner for students across several schools (one caterer per school; students
have dietary needs and meal preferences). You replace the human operator.

You reason over TYPED tool results. Every tool returns a JSON object with a
`status` field — one of: found, empty, ambiguous, conflict, queued,
unavailable, error — plus a human-readable `message` and, on `found`, a `data`
payload. Treat a non-`found` status as information, not a crash: e.g. `empty`
means the read succeeded but matched nothing; `queued` means the action requires
operator approval and was recorded as a pending proposal, NOT applied;
`unavailable` means a dependency is temporarily down. Never assume data you did
not retrieve.

You have read tools (query the operational data), write tools (update a meal
preference, record a dietary note, update a menu item's description, add an
enrolment, resolve an escalation), a dietary-safety recompute tool, an email
tool, and an escalate-to-human tool. Gather what you need, then act.

A hard-rules approval gate sits in front of every action — you do not decide it,
it is enforced for you:
  - Factual / operational actions run autonomously. This includes the Thursday
    batch's output: a session_order email to a caterer and the weekly_consolidated
    _summary are autonomous, because the deterministic compose_week calculator only
    ever composes an order it has proven safe (every dietary student covered, no MOQ
    breach, anyone unsafe escalated with no line) — the email just reports that
    vetted plan.
    A caterer_service_note (a polite note to a caterer about a minor, fixable issue
    like a cold or late delivery) is also autonomous.
  - Commercial / relational emails that embody a fresh judgment REQUIRE operator
    approval: warning, rfp, cancellation, rfp_loser_courtesy. So do adding a
    student, changing a meal/dietary preference after that session's order has
    already been sent, and anything irreversible. These are NOT executed — they are
    recorded as a pending proposal for a human. A formal quality WARNING to a caterer
    is never sent off one bad night: accumulating evidence of a real decline is
    drafted to the operator and escalated, not auto-warned.
When a tool result comes back with status `queued` (a commercial email logged
queued_for_approval, or a write recorded as a pending proposal), the action was
NOT applied — treat it as still pending: do NOT assume it happened and do NOT
retry it hoping it will go through. Note it and continue. Money is in integer
cents.

Dietary safety rule: a menu item is safe for a student only if the item's
dietary tags include every tag the student requires (set containment)."""


def _system_prompt(task: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """Base instructions plus the per-task context (handbook core + active
    operator policies + the most relevant recalled cases), assembled by
    ``src.agent.context``.

    Returns ``(system_text, recalled_case_ids, active_policy_ids)`` — the ids let
    the loop validate the model's lesson / policy citations against what was
    actually surfaced.
    """
    context = assemble_context(task)
    if not context.system_text:
        return _BASE_SYSTEM, context.recalled_case_ids, context.active_policy_ids
    return (
        f"{_BASE_SYSTEM}\n\n{context.system_text}",
        context.recalled_case_ids,
        context.active_policy_ids,
    )


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of one incident: the run id, the final text answer, and how
    many tool steps were logged."""

    run_id: int
    final_text: str
    step_count: int


# --- DB logging --------------------------------------------------------------


def _jsonb(value: Any) -> Jsonb:
    """Wrap a value for a jsonb column, stringifying anything json can't handle
    natively (Decimal from numeric columns, date/datetime, etc.)."""
    return Jsonb(value, dumps=lambda obj: json.dumps(obj, default=str))


def _result_payload(result: ToolResult) -> dict[str, Any]:
    """The full typed outcome, as a json-able dict, for tool_output_full."""
    return {"status": result.status, "message": result.message, "data": result.data}


def _open_run(trigger_reason: str) -> int:
    """Insert an `agent_runs` row and return its id."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (trigger_reason) VALUES (%s) RETURNING id",
            (trigger_reason,),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return int(run_id)


def _log_step(
    run_id: int,
    step_index: int,
    tool_name: str,
    tool_input: dict[str, Any],
    result: ToolResult,
    reasoning: str,
    action_class: str,
) -> int:
    """Insert one `agent_steps` row for a tool call; return its id."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_steps
                (run_id, step_index, tool_name, tool_input, tool_output_full,
                 reasoning, urgency, action_class)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                run_id,
                step_index,
                tool_name,
                _jsonb(dict(tool_input)),
                _jsonb(_result_payload(result)),
                reasoning or None,
                "none",
                action_class,
            ),
        )
        step_id = cur.fetchone()[0]
        conn.commit()
    return int(step_id)


def _log_lesson_citations(
    run_id: int,
    step_id: int | None,
    reasoning: str,
    recalled_case_ids: set[int],
    already_cited: set[int],
) -> None:
    """Persist the recalled lessons this decision CITED as used (not everything
    recalled). Parses ``(applying Lesson #<id>: <why>)`` out of ``reasoning``,
    keeps only ids that were genuinely recalled (drops hallucinated numbers) and
    not already recorded for this run, and links each to ``step_id`` (None = the
    run's final answer) with the model's stated why. ``already_cited`` is mutated
    so a lesson is recorded once per run, against the first decision that cites it.
    """
    new = [
        (case_id, why)
        for case_id, why in parse_lesson_citations(reasoning)
        if case_id in recalled_case_ids and case_id not in already_cited
    ]
    if not new:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO step_lesson_citations (run_id, step_id, case_id, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, case_id) DO NOTHING
            """,
            [(run_id, step_id, case_id, why or None) for case_id, why in new],
        )
        conn.commit()
    already_cited.update(case_id for case_id, _ in new)


def _log_policy_citations(
    run_id: int,
    step_id: int | None,
    reasoning: str,
    active_policy_ids: set[int],
    already_cited: set[int],
) -> None:
    """Persist the active policies this decision CITED as applied (not every policy
    in context). Parses ``(applying Policy #<id>: <why>)`` out of ``reasoning``,
    keeps only ids that were genuinely in context (drops hallucinated numbers) and
    not already recorded for this run, and links each to ``step_id`` (None = the
    run's final answer) with the model's stated why. ``already_cited`` is mutated
    so a policy is recorded once per run, against the first decision that cites it.
    """
    new = [
        (policy_id, why)
        for policy_id, why in parse_policy_citations(reasoning)
        if policy_id in active_policy_ids and policy_id not in already_cited
    ]
    if not new:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO step_policy_citations (run_id, step_id, policy_id, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, policy_id) DO NOTHING
            """,
            [(run_id, step_id, policy_id, why or None) for policy_id, why in new],
        )
        conn.commit()
    already_cited.update(policy_id for policy_id, _ in new)


def _close_run(run_id: int, notes: str | None) -> None:
    """Stamp `completed_at` and store the final answer as the run's notes."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runs SET completed_at = now(), notes = %s WHERE id = %s",
            (notes, run_id),
        )
        conn.commit()


# --- Gate enforcement --------------------------------------------------------


def _gate_verdict(tool_name: str, tool_input: dict[str, Any]) -> ActionClass:
    """Compute the hard-rules verdict for one proposed tool call.

    ``send_email`` keys on its ``email_type`` (commercial mail is gated);
    order-sensitive writes consult ``has_order_been_sent`` for the affected
    enrolment (an undeterminable order state fails safe to requires_approval);
    every other tool uses the static per-tool gate.
    """
    if tool_name == "send_email":
        return classify_email(str(tool_input.get("email_type", "")))

    if is_order_sensitive(tool_name):
        order_sent: bool | None = None
        enrolment_id = tool_input.get("enrolment_id")
        if enrolment_id is not None:
            try:
                sent = has_order_been_sent(int(enrolment_id))
            except (TypeError, ValueError):
                sent = None
            if sent is not None and sent.ok:
                order_sent = sent.data["order_sent"]
        return gate(tool_name, order_sent=order_sent)

    return gate(tool_name)


def _queued_result(tool_name: str, tool_input: dict[str, Any]) -> ToolResult:
    """The typed result returned for a requires-approval write we did NOT run.

    A non-ok ``queued`` so the model cannot mistake it for success: it records
    the proposed call as pending and tells the model not to assume it happened.
    """
    return queued(
        f"'{tool_name}' requires operator approval and was NOT applied. "
        "It has been recorded as a pending proposal for a human to review. "
        "Do not assume it happened or retry it — treat it as still pending.",
        data={
            "action_class": "requires_approval",
            "applied": False,
            "status": "queued_for_approval",
            "proposed_tool": tool_name,
            "proposed_input": dict(tool_input),
        },
    )


def _enforce_and_dispatch(
    tool_name: str, tool_input: dict[str, Any], context: dict[str, Any] | None = None
) -> tuple[ToolResult, ActionClass]:
    """Gate, then (only if allowed) dispatch one tool call.

    Returns ``(result, action_class)``. Autonomous calls are dispatched normally.
    A requires-approval call is NOT executed — except ``send_email``, which we
    still dispatch because it self-records the mail as ``queued_for_approval``
    (and never sends it); all other gated writes get a ``_queued_result``.

    ``context`` is the ambient run context passed to ``dispatch`` (always carries
    ``run_id``; for an inbound incident it also carries ``inbound_from_address`` so
    the reply tool can route to the actual sender). Tool ``context_args`` pull from
    it; the model never sees or controls these.
    """
    verdict = _gate_verdict(tool_name, tool_input)
    if verdict == "requires_approval" and tool_name != "send_email":
        return _queued_result(tool_name, tool_input), verdict
    return dispatch(tool_name, tool_input, context or {}), verdict


# --- Orchestration -----------------------------------------------------------


def _text_of(content: list[Any]) -> str:
    """Join the text blocks of an assistant message into the step's reasoning."""
    parts = [block.text.strip() for block in content if block.type == "text"]
    return "\n".join(p for p in parts if p)


def run_incident(
    trigger_reason: str, task: str, call_cap: int | None = None,
    extra_context: dict[str, Any] | None = None,
) -> RunResult:
    """Drive one incident to a final text answer.

    Opens a run, runs the Anthropic tool-use loop (logging one agent_steps row
    per tool call), enforces the per-incident call cap, closes the run, and
    returns the final answer.

    ``call_cap`` overrides ``settings.per_incident_call_cap`` for this incident —
    a multi-caterer Thursday batch (compose, then send orders + parent emails +
    surface escalations) needs more reasoning turns than a single inbound email.
    ``extra_context`` adds ambient run context for tool ``context_args`` (e.g. the
    inbound sender's address, so the reply tool can route to the actual sender);
    it is merged with ``run_id`` and never exposed to the model.
    """
    run_id = _open_run(trigger_reason)
    run_context: dict[str, Any] = {"run_id": run_id, **(extra_context or {})}
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    schemas = tool_schemas()
    system, recalled_ids, policy_ids = _system_prompt(task)
    recalled_case_ids = set(recalled_ids)
    active_policy_ids = set(policy_ids)
    # Lessons / policies cited as used so far this run — keeps each recorded once,
    # against the first decision that cites it (see the _log_*_citations helpers).
    cited_case_ids: set[int] = set()
    cited_policy_ids: set[int] = set()
    cap = call_cap if call_cap is not None else settings.per_incident_call_cap

    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    step_index = 0
    model_calls = 0
    final_text = ""

    while True:
        # On the final allowed reasoning call, withhold tools so the model must
        # conclude in text rather than keep calling tools forever.
        force_final = model_calls + 1 >= cap
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if not force_final:
            kwargs["tools"] = schemas

        response = client.messages.create(**kwargs)
        model_calls += 1

        reasoning = _text_of(response.content)
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})

        if force_final or response.stop_reason != "tool_use" or not tool_uses:
            final_text = reasoning
            break

        tool_result_blocks: list[dict[str, Any]] = []
        for use in tool_uses:
            result, action_class = _enforce_and_dispatch(use.name, use.input, run_context)
            step_id = _log_step(
                run_id,
                step_index,
                use.name,
                use.input,
                result,
                reasoning,
                action_class,
            )
            # Record any lessons / policies this reasoning turn cited as used,
            # against the step.
            if recalled_case_ids:
                _log_lesson_citations(
                    run_id, step_id, reasoning, recalled_case_ids, cited_case_ids
                )
            if active_policy_ids:
                _log_policy_citations(
                    run_id, step_id, reasoning, active_policy_ids, cited_policy_ids
                )
            step_index += 1
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": json.dumps(_result_payload(result), default=str),
                }
            )
        messages.append({"role": "user", "content": tool_result_blocks})

    # A lesson / policy the model only cites in its closing answer (no tool step)
    # is recorded against the run itself (step_id = None).
    if recalled_case_ids:
        _log_lesson_citations(
            run_id, None, final_text, recalled_case_ids, cited_case_ids
        )
    if active_policy_ids:
        _log_policy_citations(
            run_id, None, final_text, active_policy_ids, cited_policy_ids
        )

    _close_run(run_id, final_text or None)
    return RunResult(run_id=run_id, final_text=final_text, step_count=step_index)
