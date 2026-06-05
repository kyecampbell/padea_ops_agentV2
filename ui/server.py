"""Flask companion server — the operator's decision cockpit (LOCAL).

Responsibility: serve the lightweight local web UI for the operator. It is the
operator's window AND half of the learning loop:

  * a read-only FEED of recent agent runs (newest first) — each run's steps
    (``tool [action_class] -> status``), its final decision, and badges for items
    needing attention: open escalations, queued-for-approval emails, and
    requires-approval writes that were proposed but NOT applied;
  * a small set of explicit ACTION endpoints (the only things that mutate state):
      - approve & send a queued email   (POST /email/<id>/approve),
      - approve & apply a queued write   (POST /write/<step_id>/apply) — re-dispatch
        the proposed call from its logged agent_step,
      - resolve an escalation            (POST /escalation/<id>/resolve), offering a
        dietary recompute afterwards,
      - recompute a student's safe pool  (POST /enrolment/<id>/recompute),
      - comment on any run/step          (POST /comment) — writes a
        decision_annotation AND stores a case, so the comment trains the case-book.

Everything else is READ-ONLY. Server-rendered HTML (Jinja templates in
``ui/templates/``), no CDN or front-end framework. Binds to 127.0.0.1 only;
public deployment is a separate, later step.

Run: uv run python ui/server.py   (then open http://127.0.0.1:5000)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Sequence

import psycopg
from flask import Flask, flash, redirect, render_template, request, url_for
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.agent.dispatch import dispatch
from src.db.connection import get_conn
from src.tools import casebook
from src.tools import email as email_tool
from src.tools import policybook
from src.tools import writes
from src.tools.casebook import store_case
from src.tools.eligible_pool import recompute_eligible_meals

app = Flask(__name__)
# Local-only dev secret — flash messaging only; never deployed publicly as-is.
app.secret_key = "padea-cockpit-local-dev"

# How many recent runs the feed shows.
_FEED_LIMIT = 25

# How many recent weeks the Spending tab spans.
_SPENDING_WEEKS = 10


@app.template_filter("cents")
def _cents(value: Any) -> str:
    """Format integer cents as ``$1,234.56`` (money is always integer cents)."""
    if value is None:
        return "—"
    return f"${int(value) / 100:,.2f}"

# Write tools whose proposals the operator may approve-and-apply from the feed.
# (Email has its own approve path; escalate has its own resolve path; autonomous
# writes never queue. The rest are re-dispatchable proposals.)
_APPLYABLE_WRITE_TOOLS = (
    "update_term_meal_preference",
    "record_dietary_update",
    "add_enrolment",
    "update_menu_item_description",
    "resolve_escalation",
)

# Words that mark an escalation as dietary, so we can offer a recompute on resolve.
_DIETARY_HINTS = (
    "dietary", "diet", "allerg", "eligible", "safe meal", "gluten",
    "vegan", "vegetarian", "nut", "halal", "kosher", "dairy",
)


# --- DB helpers --------------------------------------------------------------


def _fetch(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """Run a read query and return dict rows. Reads never mutate; the action
    endpoints are the only writers."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    rows = _fetch(sql, params)
    return rows[0] if rows else None


# --- Feed data ---------------------------------------------------------------


def _recent_runs() -> list[dict]:
    return _fetch(
        """
        SELECT id, trigger_reason, started_at, completed_at, notes
        FROM agent_runs
        ORDER BY started_at DESC
        LIMIT %s
        """,
        (_FEED_LIMIT,),
    )


def _steps_for_runs(run_ids: Sequence[int]) -> dict[int, list[dict]]:
    """All steps for the given runs, grouped by run id (in step order)."""
    if not run_ids:
        return {}
    rows = _fetch(
        """
        SELECT id, run_id, step_index, tool_name, action_class,
               tool_output_full->>'status'              AS status,
               tool_output_full->'data'->>'applied'     AS applied,
               reasoning
        FROM agent_steps
        WHERE run_id = ANY(%s)
        ORDER BY run_id, step_index
        """,
        (list(run_ids),),
    )
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["run_id"], []).append(row)
    return grouped


def _queued_emails() -> list[dict]:
    return _fetch(
        """
        SELECT id, related_run_id, email_type, intended_to_address, subject, status
        FROM outbound_emails
        WHERE status = 'queued_for_approval'
        ORDER BY id DESC
        """
    )


def _open_escalations() -> list[dict]:
    return _fetch(
        """
        SELECT id, run_id, question, context,
               related_enrolment_id, related_caterer_id, related_order_id, created_at
        FROM escalations
        WHERE status = 'open'
        ORDER BY id DESC
        """
    )


def _pending_writes() -> list[dict]:
    """Requires-approval write proposals that were logged but not yet applied."""
    return _fetch(
        """
        SELECT id, run_id, step_index, tool_name, tool_input
        FROM agent_steps
        WHERE action_class = 'requires_approval'
          AND tool_name = ANY(%s)
          AND COALESCE(tool_output_full->'data'->>'applied', 'false') = 'false'
        ORDER BY id DESC
        """,
        (list(_APPLYABLE_WRITE_TOOLS),),
    )


def _citations_for_runs(run_ids: Sequence[int]) -> dict[int, list[dict]]:
    """The lessons each run cited as used, grouped by run id (lowest id first).

    Joins the recorded citations to the cases so the feed's info icon can show
    "Lesson #N (why…)" and link to that lesson in Manage Lessons. ``active`` lets
    the UI flag a since-disabled lesson; ``step_index`` is the decision it was
    cited at (NULL = the run's final answer)."""
    if not run_ids:
        return {}
    rows = _fetch(
        """
        SELECT slc.run_id, slc.case_id, slc.reason, slc.step_id, slc.created_at,
               s.step_index, c.situation, c.active
        FROM step_lesson_citations slc
        JOIN cases c ON c.id = slc.case_id
        LEFT JOIN agent_steps s ON s.id = slc.step_id
        WHERE slc.run_id = ANY(%s)
        ORDER BY slc.run_id, slc.case_id
        """,
        (list(run_ids),),
    )
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["run_id"], []).append(row)
    return grouped


def _policy_citations_for_runs(run_ids: Sequence[int]) -> dict[int, list[dict]]:
    """The policies each run cited as applied, grouped by run id (lowest id first).

    Parallel to ``_citations_for_runs`` but for the authoritative-policy layer:
    joins the recorded citations to the policies so the feed's info icon can show
    "Policy #N (why…)" and link to that policy in Manage Policies. ``active`` flags
    a since-disabled policy; ``step_index`` is the decision it was cited at
    (NULL = the run's final answer)."""
    if not run_ids:
        return {}
    rows = _fetch(
        """
        SELECT spc.run_id, spc.policy_id, spc.reason, spc.step_id, spc.created_at,
               s.step_index, p.text, p.active
        FROM step_policy_citations spc
        JOIN policies p ON p.id = spc.policy_id
        LEFT JOIN agent_steps s ON s.id = spc.step_id
        WHERE spc.run_id = ANY(%s)
        ORDER BY spc.run_id, spc.policy_id
        """,
        (list(run_ids),),
    )
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["run_id"], []).append(row)
    return grouped


def _counts_by_run(items: list[dict], key: str) -> dict[int, int]:
    """Tally pending items per run id (skipping rows with no run link)."""
    counts: dict[int, int] = {}
    for item in items:
        run_id = item.get(key)
        if run_id is not None:
            counts[run_id] = counts.get(run_id, 0) + 1
    return counts


# --- Feed route --------------------------------------------------------------


@app.route("/")
def index() -> str:
    runs = _recent_runs()
    run_ids = [r["id"] for r in runs]
    steps = _steps_for_runs(run_ids)
    citations = _citations_for_runs(run_ids)
    policy_citations = _policy_citations_for_runs(run_ids)

    queued_emails = _queued_emails()
    open_escalations = _open_escalations()
    pending_writes = _pending_writes()

    # Per-run badge counts (newest runs in the feed link to their attention items).
    badges = {
        "emails": _counts_by_run(queued_emails, "related_run_id"),
        "escalations": _counts_by_run(open_escalations, "run_id"),
        "writes": _counts_by_run(pending_writes, "run_id"),
    }

    offer_recompute = request.args.get("offer_recompute", type=int)

    return render_template(
        "index.html",
        active_tab="feed",
        runs=runs,
        steps=steps,
        citations=citations,
        policy_citations=policy_citations,
        queued_emails=queued_emails,
        open_escalations=open_escalations,
        pending_writes=pending_writes,
        badges=badges,
        offer_recompute=offer_recompute,
    )


# --- Weekly Orders tab -------------------------------------------------------


def _order_weeks() -> list[date]:
    """Distinct batch weeks that have a composed order, newest first."""
    rows = _fetch(
        "SELECT DISTINCT week_of FROM caterer_week_orders ORDER BY week_of DESC"
    )
    return [r["week_of"] for r in rows]


def _weekly_orders(week_of: date) -> list[dict]:
    """Per-caterer order summary for ``week_of``, each with its meal breakdown and
    per-session manifests (read straight from caterer_week_orders + orders/lines).

    ``week_of`` is the batch Monday; a session belongs to the week when its date
    falls in ``[week_of, week_of + 7)``.
    """
    week_end = week_of + timedelta(days=7)

    summaries = _fetch(
        """
        SELECT cwo.id, cwo.caterer_id, c.name AS caterer_name,
               cwo.total_items, cwo.variety_count, cwo.moq_min_total,
               cwo.moq_floor_applied, cwo.moq_variance_cents,
               cwo.total_cost_cents, cwo.gst_rate_percent
        FROM caterer_week_orders cwo
        JOIN caterers c ON c.id = cwo.caterer_id
        WHERE cwo.week_of = %s
        ORDER BY c.name
        """,
        (week_of,),
    )
    if not summaries:
        return []

    breakdown_rows = _fetch(
        """
        SELECT o.caterer_id, mi.name AS item_name, mi.price_cents,
               count(*) AS quantity
        FROM orders o
        JOIN order_lines ol ON ol.order_id = o.id
        JOIN menu_items mi ON mi.id = ol.menu_item_id
        WHERE o.session_date >= %s AND o.session_date < %s
        GROUP BY o.caterer_id, mi.id, mi.name, mi.price_cents
        ORDER BY o.caterer_id, quantity DESC, mi.name
        """,
        (week_of, week_end),
    )

    session_rows = _fetch(
        """
        SELECT o.id AS order_id, o.caterer_id, o.session_date,
               o.total_items, o.total_cost_cents, s.name AS school_name
        FROM orders o
        JOIN session_slots ss ON ss.id = o.session_slot_id
        JOIN schools s ON s.id = ss.school_id
        WHERE o.session_date >= %s AND o.session_date < %s
        ORDER BY o.caterer_id, o.session_date, s.name
        """,
        (week_of, week_end),
    )

    order_ids = [r["order_id"] for r in session_rows]
    line_rows = (
        _fetch(
            """
            SELECT ol.order_id, e.student_name, mi.name AS item_name, ol.source
            FROM order_lines ol
            JOIN enrolments e ON e.id = ol.enrolment_id
            JOIN menu_items mi ON mi.id = ol.menu_item_id
            WHERE ol.order_id = ANY(%s)
            ORDER BY ol.order_id, e.student_name
            """,
            (order_ids,),
        )
        if order_ids
        else []
    )

    lines_by_order: dict[int, list[dict]] = {}
    for line in line_rows:
        lines_by_order.setdefault(line["order_id"], []).append(line)

    breakdown_by_caterer: dict[int, list[dict]] = {}
    for row in breakdown_rows:
        item = dict(row)
        item["line_total_cents"] = item["price_cents"] * item["quantity"]
        breakdown_by_caterer.setdefault(row["caterer_id"], []).append(item)

    sessions_by_caterer: dict[int, list[dict]] = {}
    for row in session_rows:
        sess = dict(row)
        sess["lines"] = lines_by_order.get(row["order_id"], [])
        sessions_by_caterer.setdefault(row["caterer_id"], []).append(sess)

    caterers: list[dict] = []
    for summary in summaries:
        cid = summary["caterer_id"]
        breakdown = breakdown_by_caterer.get(cid, [])
        caterers.append(
            {
                **summary,
                "meal_breakdown": breakdown,
                "meal_base_cents": sum(b["line_total_cents"] for b in breakdown),
                "sessions": sessions_by_caterer.get(cid, []),
            }
        )
    return caterers


@app.route("/orders")
def orders() -> str:
    weeks = _order_weeks()
    selected = request.args.get("week")
    week_of: date | None = None
    if selected:
        try:
            week_of = date.fromisoformat(selected)
        except ValueError:
            week_of = None
    if week_of is None and weeks:
        week_of = weeks[0]

    caterers = _weekly_orders(week_of) if week_of else []
    return render_template(
        "orders.html",
        active_tab="orders",
        weeks=weeks,
        week_of=week_of,
        caterers=caterers,
    )


# --- Spending tab ------------------------------------------------------------


@app.route("/spending")
def spending() -> str:
    weeks = _order_weeks()[:_SPENDING_WEEKS]
    weeks = sorted(weeks)  # oldest -> newest for left-to-right reading

    rows = (
        _fetch(
            """
            SELECT cwo.caterer_id, c.name AS caterer_name, cwo.week_of,
                   cwo.total_cost_cents, cwo.total_items
            FROM caterer_week_orders cwo
            JOIN caterers c ON c.id = cwo.caterer_id
            WHERE cwo.week_of = ANY(%s)
            ORDER BY c.name, cwo.week_of
            """,
            (weeks,),
        )
        if weeks
        else []
    )

    # Pivot into a caterer x week matrix of weekly totals + a running term total.
    caterers: dict[int, dict] = {}
    for row in rows:
        cat = caterers.setdefault(
            row["caterer_id"],
            {"caterer_name": row["caterer_name"], "by_week": {}, "term_total_cents": 0},
        )
        cat["by_week"][row["week_of"]] = {
            "cost_cents": row["total_cost_cents"],
            "items": row["total_items"],
        }
        cat["term_total_cents"] += row["total_cost_cents"]

    caterer_rows = sorted(caterers.values(), key=lambda c: c["caterer_name"])
    week_totals = {
        w: sum(c["by_week"].get(w, {}).get("cost_cents", 0) for c in caterer_rows)
        for w in weeks
    }
    grand_total = sum(week_totals.values())
    # Largest single weekly cell — scales the inline bars (0 guards an empty term).
    max_cell = max(
        (cell["cost_cents"] for c in caterer_rows for cell in c["by_week"].values()),
        default=0,
    )

    return render_template(
        "spending.html",
        active_tab="spending",
        weeks=weeks,
        caterers=caterer_rows,
        week_totals=week_totals,
        grand_total=grand_total,
        max_cell=max_cell,
    )


# --- Manage Lessons tab ------------------------------------------------------


@app.route("/lessons")
def lessons() -> str:
    result = casebook.list_cases()
    cases = result.data if result.ok else []
    return render_template("lessons.html", active_tab="lessons", cases=cases)


@app.route("/lesson/<int:case_id>/edit", methods=["POST"])
def edit_lesson(case_id: int):
    situation = (request.form.get("situation") or "").strip()
    decision = (request.form.get("decision") or "").strip()
    rationale = (request.form.get("rationale") or "").strip()
    tags = [t.strip() for t in (request.form.get("tags") or "").split(",") if t.strip()]
    if not situation:
        flash("A lesson needs a situation.", "error")
        return redirect(url_for("lessons"))

    result = casebook.update_case(
        case_id, situation=situation, decision=decision or None,
        rationale=rationale or None, tags=tags or None,
    )
    flash(f"Lesson {case_id}: {result.message}", "success" if result.ok else "error")
    return redirect(url_for("lessons"))


@app.route("/lesson/<int:case_id>/toggle", methods=["POST"])
def toggle_lesson(case_id: int):
    # The button posts the desired next state; disabling is reversible.
    active = (request.form.get("active") or "").lower() == "true"
    result = casebook.set_case_active(case_id, active)
    flash(f"Lesson {case_id}: {result.message}", "success" if result.ok else "error")
    return redirect(url_for("lessons"))


@app.route("/lesson/<int:case_id>/delete", methods=["POST"])
def delete_lesson(case_id: int):
    result = casebook.delete_case(case_id)
    flash(f"Lesson {case_id}: {result.message}", "success" if result.ok else "error")
    return redirect(url_for("lessons"))


# --- Manage Policies tab -----------------------------------------------------
# The operator's authoritative business-rule layer (parallel to Manage Lessons).
# Edits are effective immediately — the next task's context reflects them.


def _form_sort_order() -> int | None:
    """The optional sort_order from a policy form (None when blank/non-numeric)."""
    raw = (request.form.get("sort_order") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@app.route("/policies")
def policies() -> str:
    result = policybook.list_policies()
    items = result.data if result.ok else []
    return render_template("policies.html", active_tab="policies", policies=items)


@app.route("/policies/add", methods=["POST"])
def add_policy_route():
    text = (request.form.get("text") or "").strip()
    if not text:
        flash("A policy needs text.", "error")
        return redirect(url_for("policies"))
    result = policybook.add_policy(text, sort_order=_form_sort_order())
    flash(f"Policy: {result.message}", "success" if result.ok else "error")
    return redirect(url_for("policies"))


@app.route("/policy/<int:policy_id>/edit", methods=["POST"])
def edit_policy(policy_id: int):
    text = (request.form.get("text") or "").strip()
    if not text:
        flash("A policy needs text.", "error")
        return redirect(url_for("policies"))
    result = policybook.update_policy(policy_id, text=text, sort_order=_form_sort_order())
    flash(f"Policy {policy_id}: {result.message}", "success" if result.ok else "error")
    return redirect(url_for("policies"))


@app.route("/policy/<int:policy_id>/toggle", methods=["POST"])
def toggle_policy(policy_id: int):
    # The button posts the desired next state; disabling is reversible.
    active = (request.form.get("active") or "").lower() == "true"
    result = policybook.set_policy_active(policy_id, active)
    flash(f"Policy {policy_id}: {result.message}", "success" if result.ok else "error")
    return redirect(url_for("policies"))


@app.route("/policy/<int:policy_id>/delete", methods=["POST"])
def delete_policy_route(policy_id: int):
    result = policybook.delete_policy(policy_id)
    flash(f"Policy {policy_id}: {result.message}", "success" if result.ok else "error")
    return redirect(url_for("policies"))


# --- Action: approve & send a queued email -----------------------------------


@app.route("/email/<int:email_id>/approve", methods=["POST"])
def approve_email(email_id: int):
    actor = (request.form.get("author") or "operator").strip() or "operator"
    result = email_tool.send_queued_email(email_id, approved_by=actor)
    category = "success" if result.ok else "error"
    flash(f"Email {email_id}: {result.message}", category)
    return redirect(url_for("index"))


# --- Action: approve & apply a queued write ----------------------------------


@app.route("/write/<int:step_id>/apply", methods=["POST"])
def apply_write(step_id: int):
    step = _fetch_one(
        """
        SELECT id, run_id, tool_name, tool_input, action_class, tool_output_full,
               COALESCE(tool_output_full->'data'->>'applied', 'false') AS applied
        FROM agent_steps
        WHERE id = %s
        """,
        (step_id,),
    )
    if step is None:
        flash(f"No agent step {step_id}.", "error")
        return redirect(url_for("index"))
    if step["action_class"] != "requires_approval" or step["tool_name"] not in _APPLYABLE_WRITE_TOOLS:
        flash(f"Step {step_id} is not an applyable write proposal.", "error")
        return redirect(url_for("index"))
    if step["applied"] == "true":
        flash(f"Step {step_id} was already applied.", "error")
        return redirect(url_for("index"))

    # Re-dispatch the proposed write from its logged tool_name + tool_input. This
    # is the approval path, so it deliberately bypasses the gate (the operator IS
    # the approval).
    actor = (request.form.get("author") or "operator").strip() or "operator"
    result = dispatch(step["tool_name"], step["tool_input"] or {})

    if result.status == "found":
        _mark_step_applied(step, result, actor)
        flash(f"Applied {step['tool_name']} (step {step_id}): {result.message}", "success")
    else:
        # Leave it pending so the operator can fix the cause and retry.
        flash(
            f"Could not apply {step['tool_name']} (step {step_id}) — "
            f"{result.status}: {result.message}",
            "error",
        )
    return redirect(url_for("index"))


def _mark_step_applied(step: dict, result: Any, actor: str) -> None:
    """Stamp the proposal step as applied, recording who applied it and the
    real outcome (so it drops out of the pending-writes badge and stays audited)."""
    output = dict(step["tool_output_full"] or {})
    data = dict(output.get("data") or {})
    data["applied"] = True
    data["applied_by"] = actor
    data["apply_result"] = {
        "status": result.status,
        "message": result.message,
        "data": result.data,
    }
    output["data"] = data

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_steps SET tool_output_full = %s WHERE id = %s",
            (Jsonb(output, dumps=_json_dumps), step["id"]),
        )
        conn.commit()


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)


# --- Action: resolve an escalation (offer recompute if dietary) ---------------


@app.route("/escalation/<int:escalation_id>/resolve", methods=["POST"])
def resolve_escalation_route(escalation_id: int):
    resolution = (request.form.get("resolution") or "").strip()
    actor = (request.form.get("author") or "operator").strip() or "operator"
    if not resolution:
        flash("A resolution note is required to resolve an escalation.", "error")
        return redirect(url_for("index"))

    # Capture dietary context BEFORE resolving (to offer a recompute afterwards).
    escalation = _fetch_one(
        "SELECT id, question, context, related_enrolment_id FROM escalations WHERE id = %s",
        (escalation_id,),
    )

    result = writes.resolve_escalation(escalation_id, resolution, actor)
    if not result.ok:
        flash(f"Escalation {escalation_id}: {result.message}", "error")
        return redirect(url_for("index"))

    flash(f"Resolved escalation {escalation_id}.", "success")

    enrolment_id = _dietary_enrolment(escalation)
    if enrolment_id is not None:
        flash(
            f"This looked dietary — you can recompute the safe meal pool for "
            f"enrolment {enrolment_id} below.",
            "recompute",
        )
        return redirect(url_for("index", offer_recompute=enrolment_id))
    return redirect(url_for("index"))


def _dietary_enrolment(escalation: dict | None) -> int | None:
    """The enrolment id to offer a recompute for, if this escalation is dietary.

    Dietary = it concerns a student (has related_enrolment_id) and its text hints
    at diet/allergy/eligibility. Returns None when no recompute should be offered.
    """
    if not escalation or escalation.get("related_enrolment_id") is None:
        return None
    haystack = " ".join(
        str(escalation.get(k) or "") for k in ("question", "context")
    ).lower()
    if any(hint in haystack for hint in _DIETARY_HINTS):
        return int(escalation["related_enrolment_id"])
    return None


# --- Action: recompute a student's dietary-safe pool -------------------------


@app.route("/enrolment/<int:enrolment_id>/recompute", methods=["POST"])
def recompute_route(enrolment_id: int):
    result = recompute_eligible_meals(enrolment_id)
    category = "success" if result.ok else "error"
    flash(f"Recompute for enrolment {enrolment_id}: {result.message}", category)
    return redirect(url_for("index"))


# --- Action: comment on a run/step (also trains the case-book) ----------------


@app.route("/comment", methods=["POST"])
def comment():
    comment_text = (request.form.get("comment") or "").strip()
    actor = (request.form.get("author") or "operator").strip() or "operator"
    run_id = request.form.get("run_id", type=int)
    step_id = request.form.get("step_id", type=int)

    if not comment_text:
        flash("A comment is required.", "error")
        return redirect(url_for("index"))
    if run_id is None and step_id is None:
        flash("A comment must target a run or a step.", "error")
        return redirect(url_for("index"))

    # 1) Record the operator's annotation against the run/step.
    _insert_annotation(step_id, run_id, comment_text, actor)

    # 2) Train the case-book: turn the comment into a retrievable case.
    situation, tags, related = _case_from_target(run_id, step_id)
    case = store_case(
        situation=situation,
        decision=comment_text,
        rationale="Operator feedback captured via the decision UI.",
        tags=tags,
        related_run_id=related.get("run_id"),
        related_caterer_id=related.get("caterer_id"),
        related_enrolment_id=related.get("enrolment_id"),
        created_by=actor,
    )
    if case.ok:
        flash(f"Comment saved and stored as case {case.data['case_id']}.", "success")
    else:
        flash(f"Comment saved, but storing the case failed: {case.message}", "error")
    return redirect(url_for("index"))


def _insert_annotation(step_id: int | None, run_id: int | None, comment: str, author: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decision_annotations (step_id, run_id, comment, author)
            VALUES (%s, %s, %s, %s)
            """,
            (step_id, run_id, comment, author),
        )
        conn.commit()


def _case_from_target(run_id: int | None, step_id: int | None) -> tuple[str, list[str], dict]:
    """Build the case ``situation`` + tags + related ids from the commented item.

    The operator's comment is the case's *decision* (their guidance); this is the
    *situation* it applies to — what the agent was doing when commented on.
    """
    tags = ["operator-feedback"]
    related: dict[str, int] = {}

    if step_id is not None:
        step = _fetch_one(
            """
            SELECT s.id, s.run_id, s.tool_name, s.action_class,
                   s.tool_input,
                   s.tool_output_full->>'status' AS status,
                   r.trigger_reason
            FROM agent_steps s
            JOIN agent_runs r ON r.id = s.run_id
            WHERE s.id = %s
            """,
            (step_id,),
        )
        if step:
            related["run_id"] = step["run_id"]
            if step["tool_name"]:
                tags.append(step["tool_name"])
            _harvest_related_ids(step.get("tool_input"), related)
            situation = (
                f"Run {step['run_id']} (trigger: {step['trigger_reason']}), "
                f"step '{step['tool_name']}' [{step['action_class']}] -> {step['status']}."
            )
            return situation, tags, related

    if run_id is not None:
        run = _fetch_one(
            "SELECT id, trigger_reason, notes FROM agent_runs WHERE id = %s",
            (run_id,),
        )
        if run:
            related["run_id"] = run["id"]
            decision = (run["notes"] or "").strip()
            situation = f"Run {run['id']} (trigger: {run['trigger_reason']})."
            if decision:
                situation += f" Final decision: {decision[:300]}"
            return situation, tags, related

    return "Operator comment via the decision UI.", tags, related


def _harvest_related_ids(tool_input: Any, related: dict) -> None:
    """Pull enrolment/caterer ids out of a step's tool_input, if present."""
    if not isinstance(tool_input, dict):
        return
    if "enrolment_id" in tool_input and tool_input["enrolment_id"] is not None:
        try:
            related["enrolment_id"] = int(tool_input["enrolment_id"])
        except (TypeError, ValueError):
            pass
    if "caterer_id" in tool_input and tool_input["caterer_id"] is not None:
        try:
            related["caterer_id"] = int(tool_input["caterer_id"])
        except (TypeError, ValueError):
            pass


if __name__ == "__main__":
    # Local cockpit only — bind to loopback, never 0.0.0.0.
    app.run(host="127.0.0.1", port=5000, debug=True)
