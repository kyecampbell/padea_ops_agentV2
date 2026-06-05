"""Deterministic caterer order emails — the bulk Thursday-batch send, OFF the LLM path.

The weekly caterer order is a mechanical readout of the composed batch, not a
judgment call, so it must NOT be left to the agent to "decide to send N" (that path
over-sent and double-sent). This module renders + sends those emails deterministically:

  - ``render_order_email(caterer_id, week)`` builds a consistent, well-structured
    order-email body from the PERSISTED composed order: an easy-read weekly SUMMARY
    (meal-by-meal totals + a meals/delivery/GST cost breakdown) followed by one
    PER-SESSION student manifest in date order — a Student->Meal table so the
    caterer can plate and label each meal, with dietary requirements flagged inline
    and defaulted-pending-confirmation meals marked tentative. Holding meals are
    included in the totals, never added on top. Pure template — no LLM, no randomness.
  - ``send_caterer_orders(week)`` sends EXACTLY ONE ``session_order`` email per
    caterer with a sendable order. IDEMPOTENT: it skips any caterer that already has
    a non-failed ``session_order`` logged for that week (matched on the deterministic
    subject), so a re-run never double-sends. One email per caterer, never per session.

The judgment emails (parent notes for defaults, surfacing escalations) stay with the
agent. This module only handles the deterministic bulk order.

Conventions: money is integer cents; GST-incl totals come straight from the
``caterer_week_orders`` row compose_week persisted; never raises at the caller (DB
failures come back as typed ``ToolResult``s); all SQL parameterised.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import psycopg
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools import email as email_tool
from src.tools.results import ToolResult, error, found, unavailable

logger = logging.getLogger(__name__)

_SESSION_ORDER = "session_order"
_DEFAULT_SOURCE = "defaulted_pending_confirmation"
# An existing email in any of these states means "already handled" — don't resend.
# Only a 'failed' (or absent) prior send is eligible for a (re)send.
_ACTIVE_EMAIL_STATES = ("sent", "queued_for_approval", "approved")

# Weekday names indexed by date.weekday() (0 = Monday), for the session headers.
_WEEKDAY = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --- Result shapes -----------------------------------------------------------


@dataclass
class DefaultedLine:
    """A defaulted-pending-confirmation line: the safe default a parent must confirm."""

    enrolment_id: int
    student_name: str
    parent_name: str | None
    parent_email: str | None
    school_name: str | None
    item: str


@dataclass
class ManifestLine:
    """One student's meal on a session manifest — what the caterer plates + labels.

    ``dietary_labels`` are the student's on-record requirement labels (e.g.
    ``["Gluten free"]``), shown so the caterer prepares/labels safely. ``tentative``
    marks a defaulted-pending-confirmation meal (included in totals; prepare unless
    told otherwise within 48h).
    """

    student_name: str
    item: str
    dietary_labels: list[str] = field(default_factory=list)
    tentative: bool = False


@dataclass
class SessionManifest:
    """One session's block: a header, its meal counts, and the student->meal table."""

    session_date: str            # ISO date
    day_name: str
    school_name: str
    total_meals: int
    delivery_by: str             # 'HH:MM' = session dinner_time minus 10 min
    meal_counts: list[dict]      # {item, quantity} for THIS session, qty desc
    lines: list[ManifestLine]    # one per student, student-name order


@dataclass
class CostBreakdown:
    """The week's cost split so it reads as ``meals + delivery + GST = total``.

    Components are GST-exclusive; ``gst_cents`` is the remainder to the persisted
    GST-inclusive ``total_cents``, so the three always sum to the total exactly
    (for caterers whose menu prices already include GST, the GST is backed out).
    """

    meal_subtotal_cents: int
    delivery_cents: int
    gst_cents: int
    total_cents: int


@dataclass
class OrderDraft:
    """A rendered caterer order email plus the figures behind it.

    ``sendable`` is False when the caterer has no composed order for the week
    (escalated caterer-wide, or simply nothing composed) — there is nothing to send.
    """

    caterer_id: int
    caterer_name: str
    to: str | None
    subject: str
    body: str
    total_items: int
    total_cost_cents: int
    breakdown: list[dict] = field(default_factory=list)        # {item, quantity}
    defaulted: list[DefaultedLine] = field(default_factory=list)
    sessions: list[SessionManifest] = field(default_factory=list)
    cost: CostBreakdown | None = None
    schools: list[str] = field(default_factory=list)
    sendable: bool = True
    note: str = ""


@dataclass
class OrderPlan:
    """A draft plus the idempotency decision for one caterer (no send performed)."""

    draft: OrderDraft
    already_sent: bool
    would_send: bool
    reason: str


# --- Helpers -----------------------------------------------------------------


def _money(cents: int | None) -> str:
    """Integer cents -> '$1,234.56'."""
    return f"${(cents or 0) / 100:,.2f}"


# Lead time the caterer must deliver before a session's dinner sit-down.
_DELIVERY_LEAD = timedelta(minutes=10)


def _delivery_by(dinner_time: time) -> str:
    """The deliver-by clock time = dinner_time minus the delivery lead, 'HH:MM'."""
    return (datetime.combine(date.min, dinner_time) - _DELIVERY_LEAD).strftime("%H:%M")


def _week_bounds(week: date) -> tuple[date, date]:
    """[Monday, next Monday) — the half-open date range of the order week."""
    return week, week + timedelta(days=7)


def week_subject(caterer_name: str, week: date) -> str:
    """The DETERMINISTIC order-email subject. Same (caterer, week) -> same string,
    which is exactly what the idempotency check matches on."""
    return f"Padea Dinner Order — {caterer_name} — Week of {week.isoformat()}"


def _read(describe: str, sql: str, params=None) -> list[dict] | ToolResult:
    """Run a SELECT returning dict rows; translate any psycopg failure to typed."""
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


def _failed(value) -> bool:
    return isinstance(value, ToolResult)


# --- Loaders (read the PERSISTED composed order) -----------------------------


def sendable_caterer_ids(week: date) -> list[int] | ToolResult:
    """Caterers with a composed (sendable) order for the week, in id order.

    A ``caterer_week_orders`` row exists only for a caterer whose order composed;
    caterer-wide escalations leave none, so they are correctly excluded.
    """
    rows = _read(
        "listing caterers with a composed order",
        "SELECT caterer_id FROM caterer_week_orders WHERE week_of = %s ORDER BY caterer_id",
        (week,),
    )
    if _failed(rows):
        return rows
    return [r["caterer_id"] for r in rows]


def _caterer(caterer_id: int) -> dict | None | ToolResult:
    rows = _read(
        f"loading caterer {caterer_id}",
        """
        SELECT id, name, contact_email, price_includes_gst, gst_rate_percent,
               delivery_fee_cents
        FROM caterers WHERE id = %s
        """,
        (caterer_id,),
    )
    if _failed(rows):
        return rows
    return rows[0] if rows else None


def _week_order(caterer_id: int, week: date) -> dict | None | ToolResult:
    rows = _read(
        f"loading week order for caterer {caterer_id}",
        """
        SELECT total_items, variety_count, total_cost_cents, moq_min_total
        FROM caterer_week_orders
        WHERE caterer_id = %s AND week_of = %s
        """,
        (caterer_id, week),
    )
    if _failed(rows):
        return rows
    return rows[0] if rows else None


def _defaults(caterer_id: int, week: date) -> list[DefaultedLine] | ToolResult:
    """One DefaultedLine per defaulted student for the week (deduped across sessions)."""
    start, end = _week_bounds(week)
    rows = _read(
        f"loading defaulted lines for caterer {caterer_id}",
        """
        SELECT DISTINCT e.id AS enrolment_id, e.student_name, e.parent_name,
               e.parent_email, s.name AS school_name, mi.name AS item
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN enrolments e ON e.id = ol.enrolment_id
        JOIN schools s ON s.id = e.school_id
        JOIN menu_items mi ON mi.id = ol.menu_item_id
        WHERE o.caterer_id = %s AND o.session_date >= %s AND o.session_date < %s
          AND ol.source = %s
        ORDER BY e.student_name
        """,
        (caterer_id, start, end, _DEFAULT_SOURCE),
    )
    if _failed(rows):
        return rows
    return [
        DefaultedLine(
            enrolment_id=r["enrolment_id"], student_name=r["student_name"],
            parent_name=r["parent_name"], parent_email=r["parent_email"],
            school_name=r["school_name"], item=r["item"],
        )
        for r in rows
    ]


def _session_rows(caterer_id: int, week: date) -> list[dict] | ToolResult:
    """Every order line for the caterer's week, one row per student-session, with
    the session it belongs to, the student, the meal + its price, and the line
    source. Ordered for stable rendering: by date, then school, then student."""
    start, end = _week_bounds(week)
    return _read(
        f"loading per-session manifest for caterer {caterer_id}",
        """
        SELECT o.session_date, o.session_slot_id, ss.dinner_time, sch.id AS school_id,
               sch.name AS school_name, e.id AS enrolment_id, e.student_name,
               mi.name AS item, mi.price_cents, ol.source
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN session_slots ss ON ss.id = o.session_slot_id
        JOIN schools sch ON sch.id = ss.school_id
        JOIN enrolments e ON e.id = ol.enrolment_id
        JOIN menu_items mi ON mi.id = ol.menu_item_id
        WHERE o.caterer_id = %s AND o.session_date >= %s AND o.session_date < %s
        ORDER BY o.session_date, sch.name, e.student_name, e.id
        """,
        (caterer_id, start, end),
    )


def _dietary_labels(caterer_id: int, week: date) -> dict[int, list[str]] | ToolResult:
    """{enrolment_id: [dietary requirement label …]} for every student in the
    caterer's week who has at least one tag on record (label-ordered)."""
    start, end = _week_bounds(week)
    rows = _read(
        f"loading dietary labels for caterer {caterer_id}",
        """
        SELECT DISTINCT e.id AS enrolment_id, dt.label
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN enrolments e ON e.id = ol.enrolment_id
        JOIN enrolment_dietary_tags edt ON edt.enrolment_id = e.id
        JOIN dietary_tags dt ON dt.id = edt.dietary_tag_id
        WHERE o.caterer_id = %s AND o.session_date >= %s AND o.session_date < %s
        ORDER BY e.id, dt.label
        """,
        (caterer_id, start, end),
    )
    if _failed(rows):
        return rows
    labels: dict[int, list[str]] = {}
    for r in rows:
        labels.setdefault(r["enrolment_id"], []).append(r["label"])
    return labels


def _already_sent(caterer_id: int, subject: str) -> bool | ToolResult:
    """True if a non-failed session_order with this exact subject already exists."""
    rows = _read(
        f"checking prior order email for caterer {caterer_id}",
        f"""
        SELECT 1 FROM outbound_emails
        WHERE related_caterer_id = %s AND email_type = %s
          AND status = ANY(%s) AND subject = %s
        LIMIT 1
        """,
        (caterer_id, _SESSION_ORDER, list(_ACTIVE_EMAIL_STATES), subject),
    )
    if _failed(rows):
        return rows
    return bool(rows)


# --- Assembly (deterministic, no LLM) ----------------------------------------


def _cost_breakdown(
    meal_base: int, delivery_base: int, total_cents: int,
    *, includes_gst: bool, gst_rate_percent: float,
) -> CostBreakdown:
    """Split the persisted GST-inclusive total into meals + delivery + GST.

    For a caterer whose menu prices already include GST, the GST is backed out of
    the meal + delivery bases so the three components are GST-exclusive; the GST
    line is the remainder to the persisted total, so the parts always sum exactly.
    """
    if includes_gst:
        rate = 1.0 + float(gst_rate_percent) / 100.0
        net_meal = round(meal_base / rate)
        net_delivery = round(delivery_base / rate)
    else:
        net_meal = meal_base
        net_delivery = delivery_base
    gst = total_cents - net_meal - net_delivery
    return CostBreakdown(
        meal_subtotal_cents=net_meal, delivery_cents=net_delivery,
        gst_cents=gst, total_cents=total_cents,
    )


def _build_sessions(rows: list[dict], labels: dict[int, list[str]]) -> list[SessionManifest]:
    """Group the flat per-line rows into one manifest per session, in date then
    school order, each with its meal counts and student->meal table."""
    sessions: list[SessionManifest] = []
    grouped: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in rows:
        key = (r["session_date"], r["session_slot_id"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(r)

    for key in order:
        session_rows = grouped[key]
        s_date = session_rows[0]["session_date"]
        counts: dict[str, int] = defaultdict(int)
        manifest_lines: list[ManifestLine] = []
        for r in session_rows:
            counts[r["item"]] += 1
            manifest_lines.append(
                ManifestLine(
                    student_name=r["student_name"],
                    item=r["item"],
                    dietary_labels=labels.get(r["enrolment_id"], []),
                    tentative=r["source"] == _DEFAULT_SOURCE,
                )
            )
        meal_counts = [
            {"item": item, "quantity": qty}
            for item, qty in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        sessions.append(
            SessionManifest(
                session_date=s_date.isoformat(),
                day_name=_WEEKDAY[s_date.weekday()],
                school_name=session_rows[0]["school_name"],
                total_meals=len(session_rows),
                delivery_by=_delivery_by(session_rows[0]["dinner_time"]),
                meal_counts=meal_counts,
                lines=manifest_lines,
            )
        )
    return sessions


def _render_body(
    caterer_name: str, week: date, schools: list[str], total_items: int,
    weekly_breakdown: list[dict], cost: CostBreakdown, sessions: list[SessionManifest],
) -> str:
    """The order-email body: an easy-read weekly SUMMARY (meal totals + cost
    breakdown) followed by one per-session student manifest in date order. Stable,
    structured, no randomness."""
    has_tentative = any(ln.tentative for s in sessions for ln in s.lines)
    school_phrase = ", ".join(schools) if schools else "your sessions"

    lines: list[str] = [
        f"Hi {caterer_name} team,",
        "",
        f"Please find Padea's dinner order for the week beginning Monday "
        f"{week.isoformat()}. It covers {len(sessions)} "
        f"{'session' if len(sessions) == 1 else 'sessions'} across {school_phrase}.",
        "",
        "════════════════════════  WEEKLY SUMMARY  ════════════════════════",
        "",
        "Meals this week (all sessions combined):",
    ]
    if weekly_breakdown:
        for b in weekly_breakdown:
            lines.append(f"  {b['quantity']:>4} × {b['item']}")
    else:
        lines.append("  (no meals)")
    lines += [
        f"  {'':>4}   ── total {total_items} meals",
        "",
        "Cost breakdown:",
        f"  Meal subtotal (ex GST) ... {_money(cost.meal_subtotal_cents)}",
        f"  Delivery (ex GST) ........ {_money(cost.delivery_cents)}",
        f"  GST ...................... {_money(cost.gst_cents)}",
        f"  {'-' * 34}",
        f"  Total (incl. GST) ........ {_money(cost.total_cents)}",
        "",
        "────────────────────────  PER-SESSION MANIFESTS  ─────────────────",
        "",
        "Each block is one delivery. The table assigns every meal to a named "
        "student so each can be plated and labelled.",
    ]

    for s in sessions:
        lines.append("")
        lines.append(
            f"── {s.day_name} {s.session_date} — {s.school_name} "
            f"({s.total_meals} {'meal' if s.total_meals == 1 else 'meals'}) ──"
        )
        lines.append(f"  Deliver by {s.delivery_by} (10 min before dinner)")
        counts = ", ".join(f"{c['quantity']} × {c['item']}" for c in s.meal_counts)
        lines.append(f"  Counts: {counts}")
        lines.append("")
        name_w = max([len(ln.student_name) for ln in s.lines] + [len("Student")])
        lines.append(f"  {'Student'.ljust(name_w)}   Meal")
        lines.append(f"  {'-' * name_w}   {'-' * 40}")
        for ln in s.lines:
            meal = ln.item
            if ln.tentative:
                meal += " *"
            if ln.dietary_labels:
                meal += f"  [{', '.join(ln.dietary_labels)}]"
            lines.append(f"  {ln.student_name.ljust(name_w)}   {meal}")

    if has_tentative:
        lines += [
            "",
            "* tentative — included in the totals above; please prepare unless we "
            "tell you otherwise within 48h.",
        ]
    lines += [
        "",
        "Items in [brackets] flag a student's dietary requirement — please prepare "
        "and label that meal accordingly.",
        "",
        "Thank you,",
        "Padea Operations",
    ]
    return "\n".join(lines)


def build_order_email(caterer_id: int, week: date) -> OrderDraft | ToolResult:
    """Assemble the order draft for one caterer-week from the persisted order.

    Returns a non-``sendable`` draft when nothing is composed for that caterer-week
    (e.g. a caterer-wide escalation), or a typed failure on a DB error.
    """
    caterer = _caterer(caterer_id)
    if _failed(caterer):
        return caterer
    if caterer is None:
        return error(f"No caterer with id {caterer_id}.")

    wk = _week_order(caterer_id, week)
    if _failed(wk):
        return wk
    subject = week_subject(caterer["name"], week)
    if wk is None:
        return OrderDraft(
            caterer_id=caterer_id, caterer_name=caterer["name"],
            to=caterer["contact_email"], subject=subject, body="",
            total_items=0, total_cost_cents=0, sendable=False,
            note="no composed order for this week (escalated caterer-wide or not composed)",
        )

    rows = _session_rows(caterer_id, week)
    if _failed(rows):
        return rows
    labels = _dietary_labels(caterer_id, week)
    if _failed(labels):
        return labels
    defaults = _defaults(caterer_id, week)
    if _failed(defaults):
        return defaults

    # Weekly meal-by-meal totals (qty desc, then name) from the per-line rows.
    weekly_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        weekly_counts[r["item"]] += 1
    weekly_breakdown = [
        {"item": item, "quantity": qty}
        for item, qty in sorted(weekly_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    # Cost components: meal base = listed price of every line; delivery = fee per
    # session (matching compose_week's per-session delivery charge).
    sessions = _build_sessions(rows, labels)
    schools: list[str] = []
    for s in sessions:
        if s.school_name not in schools:
            schools.append(s.school_name)
    meal_base = sum(r["price_cents"] for r in rows)
    delivery_base = caterer["delivery_fee_cents"] * len(sessions)
    cost = _cost_breakdown(
        meal_base, delivery_base, wk["total_cost_cents"],
        includes_gst=caterer["price_includes_gst"],
        gst_rate_percent=float(caterer["gst_rate_percent"]),
    )

    body = _render_body(
        caterer["name"], week, schools, wk["total_items"],
        weekly_breakdown, cost, sessions,
    )
    return OrderDraft(
        caterer_id=caterer_id, caterer_name=caterer["name"],
        to=caterer["contact_email"], subject=subject, body=body,
        total_items=wk["total_items"], total_cost_cents=wk["total_cost_cents"],
        breakdown=weekly_breakdown, defaulted=defaults, sessions=sessions,
        cost=cost, schools=schools, sendable=True,
    )


def render_order_email(caterer_id: int, week: date) -> str:
    """The order-email BODY for one caterer-week (deterministic template).

    Returns the rendered body, or "" when nothing is composed / on a read failure
    (callers that need the structured figures or the typed outcome use
    ``build_order_email`` / ``plan_caterer_orders`` instead).
    """
    draft = build_order_email(caterer_id, week)
    if _failed(draft) or not draft.sendable:
        return ""
    return draft.body


# --- Planning + sending (idempotent) -----------------------------------------


def plan_caterer_orders(week: date) -> list[OrderPlan] | ToolResult:
    """Decide, per caterer, whether an order email WOULD be sent — without sending.

    Drives both the dry run and the real send, so they agree exactly. A caterer is
    ``would_send`` only when it has a composed, sendable order, a contact email, and
    no non-failed ``session_order`` already logged for the week (idempotency).
    """
    ids = sendable_caterer_ids(week)
    if _failed(ids):
        return ids

    plans: list[OrderPlan] = []
    for caterer_id in ids:
        draft = build_order_email(caterer_id, week)
        if _failed(draft):
            return draft

        already = _already_sent(draft.caterer_id, draft.subject)
        if _failed(already):
            return already

        if not draft.sendable:
            would, reason = False, draft.note
        elif not draft.to:
            would, reason = False, "no caterer contact email on file"
        elif already:
            would, reason = False, "already sent for this week (idempotent skip)"
        else:
            would, reason = True, "would send one session_order email"
        plans.append(OrderPlan(draft=draft, already_sent=bool(already), would_send=would, reason=reason))
    return plans


def send_caterer_orders(week: date, run_id: int | None = None) -> ToolResult:
    """Send EXACTLY ONE ``session_order`` email per caterer with a sendable order.

    Idempotent: a caterer already covered for the week (non-failed prior send) is
    skipped, so re-running never double-sends. Returns a ``found`` summary with the
    caterers it sent to, those it skipped (with reasons), and any send failures.
    """
    plans = plan_caterer_orders(week)
    if _failed(plans):
        return plans

    sent: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for plan in plans:
        d = plan.draft
        if not plan.would_send:
            skipped.append({"caterer_id": d.caterer_id, "caterer_name": d.caterer_name, "reason": plan.reason})
            continue
        result = email_tool.send_email(
            email_type=_SESSION_ORDER, to=d.to, subject=d.subject, body=d.body,
            related_caterer_id=d.caterer_id, related_run_id=run_id,
        )
        if result.ok:
            sent.append({"caterer_id": d.caterer_id, "caterer_name": d.caterer_name,
                         "email_id": (result.data or {}).get("email_id")})
            logger.info("send_caterer_orders: sent order email to caterer %s (%s).", d.caterer_id, d.caterer_name)
        else:
            failed.append({"caterer_id": d.caterer_id, "caterer_name": d.caterer_name,
                           "status": result.status, "message": result.message})
            logger.warning("send_caterer_orders: FAILED for caterer %s: %s", d.caterer_id, result.message)

    return found(
        {"week_of": week.isoformat(), "sent": sent, "skipped": skipped, "failed": failed},
        f"Caterer orders for week of {week.isoformat()}: "
        f"{len(sent)} sent, {len(skipped)} skipped, {len(failed)} failed.",
    )
