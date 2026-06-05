"""Weekly student CHOOSE-AND-RATE — the kid picks their own meal and rates the
last one. The centerpiece of consumer-defined quality.

Each week a student is emailed (deterministic template, like the caterer order
email):
  - "How was last week's <meal>?" — a 1-5 rating + optional comment (omitted
    gracefully when they had no meal last week);
  - "Pick your meal for <next session date>:" — a NUMBERED list of options that is
    the MOQ-bounded offered set for their caterer-week (``orders_batch.
    plan_caterer_week``) INTERSECTED with their dietary-safe eligible pool. Every
    option is therefore SAFE and within V_max, so a free choice can never breach
    dietary safety or the MOQ ceiling.

The OPTIONS come from the exact same plan ``compose_week`` uses, so the set a kid
is shown is the set their pick is later honoured against (orders_batch is the
single source of truth for the offered set — see ``plan_caterer_week``).

It is email-based and reuses the inbound agent for replies: when a student
replies, the agent parses (pick + rating + comment), then calls the tools here:
  - ``record_meal_choice`` writes the PICK as a ``meal_requests`` row for the
    upcoming session — validated to be in the offered set AND dietary-eligible
    (never assigns an ineligible meal; an invalid pick is rejected so the agent
    falls back / asks). ``compose_week`` then prefers this pick over the fallback.
  - ``record_meal_rating`` writes the RATING + comment as a ``feedback`` row
    (source='student'), feeding the same caterer quality signal.

Demo / safety:
  - A student with a BLANK (NULL/empty) ``student_email`` is never emailed and
    simply falls back to the normal compose-time assignment.
  - Sends go through the email tool, so EMAIL_MODE governs them (demo-routed to
    the sink, or 'dry' = logged-not-sent). ``send_choice_requests`` is idempotent
    per (student, week) — a re-run sends 0.

Conventions: integer cents; parameterised SQL; timezone-aware DB timestamps;
never raises at the caller (DB failures come back as typed ``ToolResult``s).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from config.settings import settings
from src.db.connection import get_conn
from src.tools import email as email_tool
from src.tools import orders_batch
from src.tools.results import ToolResult, conflict, error, found, unavailable

logger = logging.getLogger(__name__)

CHOICE_EMAIL_TYPE = "student_meal_choice"
_STUDENT_SOURCE = "student"        # feedback_source code for a student rating.
_REQUEST_SOURCE = "request"        # order_line_source code for a student pick.
# A prior choice email in any of these states means "already asked this week".
_ACTIVE_EMAIL_STATES = ("sent", "queued_for_approval", "approved", "drafted")

_WEEKDAY = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# REPLY ATTRIBUTION (the demo's main risk): all demo students share one inbox
# (the sink), so a reply's From is never an identity signal. Instead every choice
# email carries a stable per-(student, week) reference token in BOTH the subject
# and the body — ``PADEA-CHOICE-<enrolment_id>-<YYYY-MM-DD>``. Email replies keep
# the subject ("Re: …"), and most clients quote the body, so the token round-trips
# and resolves a reply to EXACTLY one student + their session — never "the first
# open request", never a sender guess. ``parse_reply_reference`` extracts it; the
# distinctive ``PADEA-CHOICE-`` prefix can't collide with the prose ("dinner
# choice for …") elsewhere in the subject.
_REF_RE = re.compile(r"PADEA-CHOICE-(\d+)-(\d{4}-\d{2}-\d{2})", re.I)


def reply_reference(enrolment_id: int, week_of: date) -> str:
    """The stable reference token for one (student, week): ``PADEA-CHOICE-<id>-<wk>``."""
    return f"PADEA-CHOICE-{enrolment_id}-{orders_batch.monday_of_week(week_of).isoformat()}"


def parse_reply_reference(subject: str | None, body: str | None) -> tuple[int, date] | None:
    """Extract ``(enrolment_id, week_of)`` from a reply's reference token — PURE.

    Checks the SUBJECT first (most reliable — replies preserve it as "Re: …"), then
    the BODY (a quoted original). Returns ``None`` when no token is present (the
    agent then falls back to body-based identification per the handbook). Attribution
    is keyed solely on the token, so it can never be misattributed to another student
    who happens to share the inbox.
    """
    for text in (subject or "", body or ""):
        m = _REF_RE.search(text)
        if m:
            try:
                return int(m.group(1)), date.fromisoformat(m.group(2))
            except ValueError:
                continue
    return None


# --- Value objects -----------------------------------------------------------


@dataclass
class ChoiceOption:
    """One numbered, dietary-safe, MOQ-bounded option the student may pick."""

    number: int
    menu_item_id: int
    name: str
    price_cents: int


@dataclass
class LastMeal:
    """The meal a student received last week — the thing they're asked to rate."""

    item: str
    menu_item_id: int
    order_line_id: int
    order_id: int
    session_date: str


@dataclass
class ChoiceOptions:
    """Everything the choose-and-rate email (and the reply tools) needs for one
    student-week. ``options`` is empty when the student has no safe, offered meal
    (e.g. unknown dietary, or escalated this week) — they are not emailed."""

    enrolment_id: int
    student_name: str
    student_email: str | None
    school_name: str | None
    caterer_id: int | None
    week_of: str                       # ISO Monday of the week being chosen FOR
    upcoming_slot_id: int | None
    upcoming_session_date: str | None  # ISO date of the session being picked for
    options: list[ChoiceOption] = field(default_factory=list)
    last_meal: LastMeal | None = None
    reason: str = ""                   # why options is empty, when it is

    @property
    def has_options(self) -> bool:
        return bool(self.options)

    def option_for(self, menu_item_id: int) -> ChoiceOption | None:
        return next((o for o in self.options if o.menu_item_id == menu_item_id), None)

    def as_dict(self) -> dict:
        return asdict(self)


# --- DB helpers --------------------------------------------------------------


def _read(describe: str, sql: str, params=None) -> list[dict] | ToolResult:
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


def upcoming_week(today: date | None = None) -> date:
    """The Monday of the week the choose-and-rate covers (same as the Thursday
    batch's target week)."""
    return orders_batch.upcoming_monday(today or date.today())


def _latest_choice_week(enrolment_id: int) -> date | None:
    """The target week of the student's most recent non-failed choice email, parsed
    from its reference token. ``None`` if they have no choice email on record."""
    rows = _read(
        f"finding latest choice email for enrolment {enrolment_id}",
        """
        SELECT subject FROM outbound_emails
        WHERE related_enrolment_id = %s AND email_type = %s AND status = ANY(%s)
        ORDER BY composed_at DESC, id DESC LIMIT 1
        """,
        (enrolment_id, CHOICE_EMAIL_TYPE, list(_ACTIVE_EMAIL_STATES)),
    )
    if _failed(rows) or not rows:
        return None
    ref = parse_reply_reference(rows[0]["subject"], "")
    return ref[1] if ref else None


def default_reply_week(enrolment_id: int) -> date:
    """The week a student's REPLY concerns: the target week of the choice email we
    most recently sent them (so a reply always answers the right week, even off the
    normal cadence), falling back to the upcoming week if they have none on record."""
    return _latest_choice_week(enrolment_id) or upcoming_week()


def _enrolment_row(enrolment_id: int) -> dict | None | ToolResult:
    rows = _read(
        f"loading enrolment {enrolment_id} for choice",
        """
        SELECT e.id, e.student_name, e.student_email, e.school_id,
               s.name AS school_name, s.current_caterer_id AS caterer_id
        FROM enrolments e JOIN schools s ON s.id = e.school_id
        WHERE e.id = %s
        """,
        (enrolment_id,),
    )
    if _failed(rows):
        return rows
    return rows[0] if rows else None


def _last_week_meal(enrolment_id: int, week_of: date) -> LastMeal | None | ToolResult:
    """The meal the student received in the PRIOR week ([week-7, week)) — the most
    recent one if they had several. ``None`` when they had no meal last week."""
    rows = _read(
        f"loading last week's meal for enrolment {enrolment_id}",
        """
        SELECT ol.id AS order_line_id, ol.order_id, ol.menu_item_id,
               mi.name AS item, o.session_date
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN menu_items mi ON mi.id = ol.menu_item_id
        WHERE ol.enrolment_id = %s
          AND o.session_date >= %s AND o.session_date < %s
        ORDER BY o.session_date DESC, ol.id DESC
        LIMIT 1
        """,
        (enrolment_id, week_of - timedelta(days=7), week_of),
    )
    if _failed(rows):
        return rows
    if not rows:
        return None
    r = rows[0]
    return LastMeal(
        item=r["item"], menu_item_id=r["menu_item_id"],
        order_line_id=r["order_line_id"], order_id=r["order_id"],
        session_date=r["session_date"].isoformat(),
    )


# --- Build the options (single-sourced from orders_batch.plan_caterer_week) --


def build_choice_options(
    enrolment_id: int, week_of: date, cache: dict | None = None
) -> ChoiceOptions | ToolResult:
    """Assemble one student's choose-and-rate options for ``week_of`` — PURE.

    Options = the caterer-week's MOQ-bounded offered set INTERSECTED with this
    student's dietary-safe eligible pool, numbered deterministically (most-popular
    first, then by name). Also resolves their upcoming session and last week's
    meal. A student who is escalated / has no safe offered meal this week gets an
    empty ``options`` with a ``reason`` (they are not emailed).

    ``cache`` is an optional caller-owned plan memo (see
    ``orders_batch.plan_caterer_week_by_id``) so resolving many students of one
    caterer-week gathers it once.
    """
    week_of = orders_batch.monday_of_week(week_of)
    enr = _enrolment_row(enrolment_id)
    if _failed(enr):
        return enr
    if enr is None:
        return error(f"No enrolment with id {enrolment_id}.")

    last_meal = _last_week_meal(enrolment_id, week_of)
    if _failed(last_meal):
        return last_meal

    base = ChoiceOptions(
        enrolment_id=enrolment_id, student_name=enr["student_name"],
        student_email=(enr["student_email"] or None), school_name=enr["school_name"],
        caterer_id=enr["caterer_id"], week_of=week_of.isoformat(),
        upcoming_slot_id=None, upcoming_session_date=None,
        last_meal=last_meal if isinstance(last_meal, LastMeal) else None,
    )

    if enr["caterer_id"] is None:
        base.reason = "the student's school has no caterer assigned"
        return base

    plan = orders_batch.plan_caterer_week_by_id(enr["caterer_id"], week_of, cache=cache)
    if _failed(plan):
        return plan

    if enrolment_id not in plan.safe_students:
        # Not a safe, orderable member of this caterer-week (escalated, opted out,
        # absent, or not rostered) — no options to offer; falls back at order time.
        base.reason = "no safe, offered meal this week (escalated / not rostered)"
        return base

    student = plan.safe_students[enrolment_id]
    order = sorted(
        plan.student_sessions[enrolment_id],
        key=lambda slot: (plan.session_meta[slot][0], slot),
    )
    if order:
        slot = order[0]
        base.upcoming_slot_id = slot
        base.upcoming_session_date = plan.session_meta[slot][0].isoformat()

    # Offered set ∩ this student's safe pool, numbered most-popular-first then name.
    eligible_offered = [i for i in plan.offered if i in student["pool"]]
    eligible_offered.sort(key=lambda i: (-plan.popularity.get(i, 0.0), plan.menu[i]["name"]))
    base.options = [
        ChoiceOption(
            number=n, menu_item_id=i,
            name=plan.menu[i]["name"], price_cents=plan.menu[i]["price_cents"],
        )
        for n, i in enumerate(eligible_offered, start=1)
    ]
    if not base.options:
        base.reason = "no offered meal is dietary-safe for this student this week"
    return base


def identify_reply(subject: str | None, body: str | None) -> ChoiceOptions | ToolResult:
    """Resolve a student's choose-and-rate REPLY to the exact student + their options
    — deterministically, via the reference token in the subject/body — PURE.

    This is how a reply is attributed when every student shares one inbox: the
    token (``PADEA-CHOICE-<id>-<week>``) names the enrolment + the week, so the
    match is to ONE student and ONE session, never the sender and never "the first
    open request". Returns the student's ``ChoiceOptions`` (so the agent both
    identifies them AND gets the numbered options to map the pick), or a
    ``conflict`` when no token is present (fall back to body identification).
    """
    ref = parse_reply_reference(subject, body)
    if ref is None:
        return conflict(
            "No Padea-Choice reference token found in the reply subject/body; cannot "
            "attribute it by token. Identify the student from the body instead, then "
            "use get_student_choice_options.",
        )
    enrolment_id, week_of = ref
    return build_choice_options(enrolment_id, week_of)


# --- Deterministic render ----------------------------------------------------


def _first_name(student_name: str) -> str:
    return (student_name or "").split()[0] if student_name else "there"


def render_choice_email(
    enrolment_id: int, week_of: date, cache: dict | None = None
) -> tuple[str, str] | ToolResult:
    """The deterministic choose-and-rate (subject, body) for one student — no LLM.

    Mirrors the order-email style: a consistent layout, warm greeting, an optional
    "rate last week" block, the NUMBERED safe options for the next session, clear
    reply instructions, and a no-pressure reassurance. Returns a typed failure on a
    read error, or a ``conflict`` when the student has no options to offer (they
    should not be emailed).
    """
    opts = build_choice_options(enrolment_id, week_of, cache=cache)
    if _failed(opts):
        return opts
    if not opts.has_options:
        return conflict(
            f"{opts.student_name} has no safe offered meal for the week of "
            f"{opts.week_of} ({opts.reason}); no choice email to send."
        )
    return choice_subject(week_of, enrolment_id, opts.upcoming_session_date), _render(opts)


def _render(opts: ChoiceOptions) -> str:
    """The body string for a built ``ChoiceOptions`` (pure)."""
    name = _first_name(opts.student_name)
    when = opts.upcoming_session_date or opts.week_of
    try:
        nice_when = _WEEKDAY[date.fromisoformat(when).weekday()] + " " + when
    except ValueError:
        nice_when = when

    lines: list[str] = [
        f"Hi {name},",
        "",
        "It's time to choose your Padea dinner for next session — and tell us how "
        "the last one went.",
        "",
    ]

    if opts.last_meal is not None:
        lines += [
            "════════════════════════  RATE LAST WEEK  ════════════════════════",
            "",
            f"How was last week's {opts.last_meal.item}?",
            "  Reply with a rating out of 5 (1 = poor, 5 = great) and, if you like, "
            "a quick comment.",
            "",
        ]

    lines += [
        f"════════════════════════  PICK YOUR MEAL  ════════════════════════",
        "",
        f"Pick your meal for {nice_when}:",
        "",
    ]
    for o in opts.options:
        lines.append(f"  {o.number}. {o.name}")
    lines += [
        "",
        "Every option above is suitable for you and ready to go.",
        "",
        "To reply: just send back your meal NUMBER, a RATING out of 5 for last "
        "week, and any comments — for example: \"2, rated 4/5, a bit cold but tasty\".",
        "",
        "No rush — if we don't hear from you, we'll give you something you've "
        "enjoyed before. You'll never be left without a meal.",
        "",
        "Thanks,",
        "Padea",
        "",
        f"(Ref: {reply_reference(opts.enrolment_id, date.fromisoformat(opts.week_of))} — "
        "please keep this in your reply so we match it to you.)",
    ]
    return "\n".join(lines)


def choice_subject(week_of: date, enrolment_id: int, session_date: str | date | None = None) -> str:
    """The DETERMINISTIC per-(student, week) choose-and-rate subject.

    Human-readable for the student ("Your Padea meal for next Tuesday 🍽"), with the
    attribution token tucked in brackets at the END (``[PADEA-CHOICE-<id>-<week>]``)
    so a reply ("Re: …") is still unambiguously matched to this student by token —
    never by sender. Stable per (student, week) — the same inputs always render the
    same string — so the idempotency check matches on it. ``session_date`` (the
    student's upcoming session) drives the "next <Weekday>" phrasing; without it,
    falls back to "the week of <Monday>"."""
    token = reply_reference(enrolment_id, week_of)
    when = f"the week of {orders_batch.monday_of_week(week_of).isoformat()}"
    if session_date is not None:
        d = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        when = f"next {_WEEKDAY[d.weekday()]}"
    return f"Your Padea meal for {when} 🍽 [{token}]"


# --- Send (idempotent, demo-routed) ------------------------------------------


def _already_sent(enrolment_id: int, subject: str) -> bool | ToolResult:
    """True if a non-failed choice email with this exact subject already exists for
    the student (the per-(student, week) idempotency guard)."""
    rows = _read(
        f"checking prior choice email for enrolment {enrolment_id}",
        """
        SELECT 1 FROM outbound_emails
        WHERE related_enrolment_id = %s AND email_type = %s
          AND status = ANY(%s) AND subject = %s
        LIMIT 1
        """,
        (enrolment_id, CHOICE_EMAIL_TYPE, list(_ACTIVE_EMAIL_STATES), subject),
    )
    if _failed(rows):
        return rows
    return bool(rows)


def _active_emailable_enrolments(week_of: date) -> list[int] | ToolResult:
    """Active students WITH a non-blank student_email — the candidates for a weekly
    choice email. Blank-email students are excluded here (they fall back silently);
    students with no safe options are filtered later by ``build_choice_options``."""
    rows = _read(
        "listing emailable students for choice",
        """
        SELECT DISTINCT e.id
        FROM enrolments e
        JOIN enrolment_session_slots ess ON ess.enrolment_id = e.id
        WHERE e.student_email IS NOT NULL AND btrim(e.student_email) <> ''
          AND e.opted_out_of_catering = FALSE
          AND e.current_period_start_date <= %s
          AND (e.current_period_end_date IS NULL OR e.current_period_end_date >= %s)
        ORDER BY e.id
        """,
        (week_of, week_of),
    )
    if _failed(rows):
        return rows
    return [r["id"] for r in rows]


def plan_choice_requests(week_of: date, cache: dict | None = None) -> dict | ToolResult:
    """Decide, per emailable student, whether a choice email WOULD send — without
    sending. Drives both the dry run and the real send so they agree exactly.

    Returns ``{"week_of", "would_send": [...], "skipped": [...]}``. A student is
    ``would_send`` only with a non-blank email, ≥1 safe option, and no non-failed
    choice email already logged for the week (idempotency). ``cache`` (optional)
    memoises the per-caterer-week plan so all students of a caterer cost one gather."""
    week_of = orders_batch.monday_of_week(week_of)
    if cache is None:
        cache = {}
    ids = _active_emailable_enrolments(week_of)
    if _failed(ids):
        return ids

    would: list[dict] = []
    skipped: list[dict] = []
    for eid in ids:
        opts = build_choice_options(eid, week_of, cache=cache)
        if _failed(opts):
            return opts
        subject = choice_subject(week_of, eid, opts.upcoming_session_date)
        if not opts.has_options:
            skipped.append({"enrolment_id": eid, "student_name": opts.student_name,
                            "reason": opts.reason or "no options"})
            continue
        already = _already_sent(eid, subject)
        if _failed(already):
            return already
        if already:
            skipped.append({"enrolment_id": eid, "student_name": opts.student_name,
                            "reason": "already sent this week (idempotent skip)"})
            continue
        would.append({
            "enrolment_id": eid, "student_name": opts.student_name,
            "student_email": opts.student_email, "subject": subject,
            "session_date": opts.upcoming_session_date,
            "options": [o.name for o in opts.options],
            "rate_last": opts.last_meal.item if opts.last_meal else None,
        })
    return {"week_of": week_of.isoformat(), "would_send": would, "skipped": skipped}


def send_choice_requests(
    week_of: date, run_id: int | None = None, cache: dict | None = None
) -> ToolResult:
    """Send EXACTLY ONE ``student_meal_choice`` email per emailable student with
    options for the week. Idempotent per (student, week): a student already sent
    (non-failed) is skipped, so a re-run sends 0. Blank-email students are never
    emailed. Returns the students sent to, those skipped (with reasons), and any
    send failures. ``cache`` (optional) memoises the per-caterer-week plan; the
    idempotency check still queries ``outbound_emails`` fresh each call."""
    if cache is None:
        cache = {}
    plan = plan_choice_requests(week_of, cache=cache)
    if _failed(plan):
        return plan
    week_of = orders_batch.monday_of_week(week_of)

    sent: list[dict] = []
    failed: list[dict] = []
    for w in plan["would_send"]:
        eid = w["enrolment_id"]
        rendered = render_choice_email(eid, week_of, cache=cache)
        if _failed(rendered):
            failed.append({"enrolment_id": eid, "student_name": w["student_name"],
                           "status": rendered.status, "message": rendered.message})
            continue
        subject, body = rendered
        result = email_tool.send_email(
            email_type=CHOICE_EMAIL_TYPE, to=w["student_email"],
            subject=subject, body=body,
            related_enrolment_id=eid, related_run_id=run_id,
        )
        if result.ok:
            sent.append({"enrolment_id": eid, "student_name": w["student_name"],
                         "email_id": (result.data or {}).get("email_id")})
            logger.info("send_choice_requests: choice email to enrolment %s.", eid)
        else:
            failed.append({"enrolment_id": eid, "student_name": w["student_name"],
                           "status": result.status, "message": result.message})
            logger.warning("send_choice_requests: FAILED for enrolment %s: %s", eid, result.message)

    return found(
        {"week_of": week_of.isoformat(), "sent": sent,
         "skipped": plan["skipped"], "failed": failed},
        f"Choice requests for week of {week_of.isoformat()}: {len(sent)} sent, "
        f"{len(plan['skipped'])} skipped, {len(failed)} failed.",
    )


# --- Per-session dinner-time TRIGGER (data-driven sweep) ---------------------
# The real trigger: send each session's choice emails AT that session's dinner
# time. A data-driven APScheduler sweep (worker.py) ticks frequently and calls
# ``run_due_session_choice_sends(now)``; that reads session_slots, finds the ones
# whose dinner_time just passed (today, within the misfire grace window), and sends
# each due session's choice emails. At a session occurrence on date D, the email
# rates the meal eaten at D and asks the student to PICK for next week's session
# (target week = the week after D). Idempotent per (student, target-week) via the
# subject token, so repeated sweeps — or a worker restart inside the grace window —
# send 0 the second time. Blank-email students are never emailed.


def due_session_slots(now: datetime, grace_seconds: int | None = None) -> list[dict] | ToolResult:
    """Active session_slots whose dinner_time has JUST passed at ``now`` — PURE.

    "Due" = the slot's ``day_of_week`` matches ``now``'s weekday AND ``now`` is in
    ``[dinner_time, dinner_time + grace]`` today. ``grace_seconds`` defaults to the
    configured misfire grace, so a slot whose dinner passed while the worker was
    briefly down still fires when it returns within the window (misfire-safe).
    ``now`` must be timezone-aware (the scheduler's tz). Returns the due slot rows
    (id, school_id, day_of_week, dinner_time), earliest dinner first."""
    grace = settings.misfire_grace_seconds if grace_seconds is None else grace_seconds
    rows = _read(
        "listing active session slots for the dinner-time sweep",
        "SELECT id, school_id, day_of_week, dinner_time FROM session_slots "
        "WHERE active = TRUE ORDER BY dinner_time, id",
    )
    if _failed(rows):
        return rows
    due: list[dict] = []
    for r in rows:
        if r["day_of_week"] != now.isoweekday():
            continue
        dinner_dt = datetime.combine(now.date(), r["dinner_time"], tzinfo=now.tzinfo)
        delta = (now - dinner_dt).total_seconds()
        if 0 <= delta <= grace:
            due.append(r)
    return due


def _session_emailable(slot_id: int, target_session_date: date) -> list[int] | ToolResult:
    """Enrolment ids rostered to ``slot_id`` with a non-blank student_email, active
    for the TARGET session date (next week's occurrence). Blank-email students are
    excluded here, so they are never emailed and fall back at order time."""
    rows = _read(
        f"listing emailable students rostered to slot {slot_id}",
        """
        SELECT DISTINCT e.id
        FROM enrolments e
        JOIN enrolment_session_slots ess ON ess.enrolment_id = e.id
        WHERE ess.session_slot_id = %s
          AND e.student_email IS NOT NULL AND btrim(e.student_email) <> ''
          AND e.opted_out_of_catering = FALSE
          AND e.current_period_start_date <= %s
          AND (e.current_period_end_date IS NULL OR e.current_period_end_date >= %s)
        ORDER BY e.id
        """,
        (slot_id, target_session_date, target_session_date),
    )
    if _failed(rows):
        return rows
    return [r["id"] for r in rows]


def _attempt_choice_send(
    enrolment_id: int, week_of: date, run_id: int | None, cache: dict | None
) -> tuple[str, dict]:
    """Render + idempotency-check + send one student's choice email. Returns
    ``(outcome, detail)`` where outcome is sent | skipped | failed. Shared by the
    per-session sender; the idempotency guard queries ``outbound_emails`` fresh."""
    opts = build_choice_options(enrolment_id, week_of, cache=cache)
    if _failed(opts):
        return "failed", {"enrolment_id": enrolment_id, "status": opts.status, "message": opts.message}
    if not opts.has_options:
        return "skipped", {"enrolment_id": enrolment_id, "student_name": opts.student_name,
                           "reason": opts.reason or "no options"}
    subject = choice_subject(week_of, enrolment_id, opts.upcoming_session_date)
    already = _already_sent(enrolment_id, subject)
    if _failed(already):
        return "failed", {"enrolment_id": enrolment_id, "status": already.status, "message": already.message}
    if already:
        return "skipped", {"enrolment_id": enrolment_id, "student_name": opts.student_name,
                           "reason": "already sent this week (idempotent skip)"}
    result = email_tool.send_email(
        email_type=CHOICE_EMAIL_TYPE, to=opts.student_email, subject=subject,
        body=_render(opts), related_enrolment_id=enrolment_id, related_run_id=run_id,
    )
    if result.ok:
        return "sent", {"enrolment_id": enrolment_id, "student_name": opts.student_name,
                        "email_id": (result.data or {}).get("email_id")}
    return "failed", {"enrolment_id": enrolment_id, "student_name": opts.student_name,
                      "status": result.status, "message": result.message}


def send_session_choice_requests(
    slot_id: int, occurrence_date: date, run_id: int | None = None, cache: dict | None = None
) -> ToolResult:
    """Send choice emails for ONE session occurrence (slot on ``occurrence_date``).

    Each rostered, emailable student is asked to PICK for next week's occurrence of
    this session (target week = the week after ``occurrence_date``) and RATE the
    meal at this occurrence. Idempotent per (student, target-week): a re-run sends
    0. Returns sent / skipped / failed for this session."""
    if cache is None:
        cache = {}
    target_week = orders_batch.monday_of_week(occurrence_date) + timedelta(days=7)
    target_session_date = occurrence_date + timedelta(days=7)
    ids = _session_emailable(slot_id, target_session_date)
    if _failed(ids):
        return ids

    sent: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for eid in ids:
        outcome, detail = _attempt_choice_send(eid, target_week, run_id, cache)
        {"sent": sent, "skipped": skipped, "failed": failed}[outcome].append(detail)
    return found(
        {"slot_id": slot_id, "occurrence_date": occurrence_date.isoformat(),
         "target_week": target_week.isoformat(), "sent": sent, "skipped": skipped, "failed": failed},
        f"Session {slot_id} on {occurrence_date.isoformat()}: {len(sent)} sent, "
        f"{len(skipped)} skipped, {len(failed)} failed.",
    )


def run_due_session_choice_sends(
    now: datetime, run_id: int | None = None, grace_seconds: int | None = None
) -> ToolResult:
    """The data-driven dinner-time SWEEP: send choice emails for every session whose
    dinner_time just passed at ``now`` (within the misfire grace window).

    Reads session_slots each call (no per-slot cron registration — adapts to data
    changes), so it's the one job the scheduler ticks. Idempotent (per student,
    target-week) and misfire-safe (the grace window catches a just-missed dinner
    after a brief restart). Returns the due slots and aggregate sent/skipped/failed."""
    due = due_session_slots(now, grace_seconds)
    if _failed(due):
        return due
    cache: dict = {}
    sent = skipped = failed = 0
    per_slot: list[dict] = []
    for slot in due:
        res = send_session_choice_requests(slot["id"], now.date(), run_id=run_id, cache=cache)
        if not res.ok:   # send_* always returns a ToolResult; key on .ok, not isinstance
            return res
        d = res.data
        sent += len(d["sent"]); skipped += len(d["skipped"]); failed += len(d["failed"])
        per_slot.append(d)
    return found(
        {"now": now.isoformat(), "due_slot_ids": [s["id"] for s in due],
         "sent": sent, "skipped": skipped, "failed": failed, "per_slot": per_slot},
        f"Dinner-time sweep at {now.isoformat()}: {len(due)} session(s) due, "
        f"{sent} sent, {skipped} skipped, {failed} failed.",
    )


# --- Reply: PICK (meal_request) ----------------------------------------------


def plan_meal_choice(enrolment_id: int, menu_item_id: int, week_of: date) -> dict | ToolResult:
    """Validate a student's PICK and return the would-be ``meal_requests`` row —
    PURE (no write). The pick must be one of the student's NUMBERED options for the
    week (offered set ∩ dietary-safe pool); otherwise a ``conflict`` so the caller
    never assigns an ineligible meal. Used by the dry run and by ``record_meal_choice``."""
    opts = build_choice_options(enrolment_id, week_of)
    if _failed(opts):
        return opts
    if opts.upcoming_slot_id is None:
        return conflict(
            f"{opts.student_name} has no upcoming session in the week of {opts.week_of}; "
            "cannot record a pick."
        )
    chosen = opts.option_for(menu_item_id)
    if chosen is None:
        valid = ", ".join(f"{o.number}={o.name}" for o in opts.options) or "(none)"
        return conflict(
            f"Menu item {menu_item_id} is not one of {opts.student_name}'s safe options "
            f"for this week; not recorded. Offer the student one of: {valid}.",
            data={"options": [asdict(o) for o in opts.options]},
        )
    return {
        "enrolment_id": enrolment_id,
        "session_slot_id": opts.upcoming_slot_id,
        "session_date": opts.upcoming_session_date,
        "menu_item_id": menu_item_id,
        "item": chosen.name,
        "student_name": opts.student_name,
    }


def record_meal_choice(
    enrolment_id: int, menu_item_id: int, week_of: date | None = None
) -> ToolResult:
    """Record a student's weekly PICK as a ``meal_requests`` row for their upcoming
    session (the agent calls this after parsing the reply).

    Validates the pick is in the student's safe, offered options (rejects with a
    ``conflict`` otherwise — never assigns an ineligible meal). Idempotent per
    (enrolment, slot, date): a re-pick supersedes the prior one. ``compose_week``
    then prefers this pick over the fallback. A DATA change, not a send. When
    ``week_of`` is omitted it defaults to the week the student's latest choice email
    was for (so a reply answers the right week)."""
    week_of = orders_batch.monday_of_week(week_of or default_reply_week(enrolment_id))
    plan_row = plan_meal_choice(enrolment_id, menu_item_id, week_of)
    if _failed(plan_row):
        return plan_row

    def work(cur: psycopg.Cursor) -> dict:
        cur.execute(
            """
            INSERT INTO meal_requests (enrolment_id, session_slot_id, session_date, menu_item_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (enrolment_id, session_slot_id, session_date)
            DO UPDATE SET menu_item_id = EXCLUDED.menu_item_id, requested_at = now(),
                          consumed_at = NULL
            RETURNING id
            """,
            (enrolment_id, plan_row["session_slot_id"],
             plan_row["session_date"], menu_item_id),
        )
        return cur.fetchone()

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = work(cur)
            conn.commit()
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while recording pick: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while recording pick: {exc}")

    logger.info(
        "record_meal_choice: enrolment=%s slot=%s date=%s item=%s request=%s",
        enrolment_id, plan_row["session_slot_id"], plan_row["session_date"],
        menu_item_id, row["id"],
    )
    return found(
        {"meal_request_id": row["id"], "source": _REQUEST_SOURCE, **plan_row},
        f"Recorded {plan_row['student_name']}'s pick of {plan_row['item']} for "
        f"{plan_row['session_date']}.",
    )


# --- Reply: RATING (feedback) ------------------------------------------------


def plan_meal_rating(
    enrolment_id: int, rating: int, comment: str | None, week_of: date
) -> dict | ToolResult:
    """Validate a student's RATING and return the would-be ``feedback`` row — PURE
    (no write). Links last week's meal (order_line) when there is one. Used by the
    dry run and by ``record_meal_rating``."""
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return error("rating must be an integer 1-5.")
    if not 1 <= rating <= 5:
        return error(f"rating must be between 1 and 5; got {rating}.")

    enr = _enrolment_row(enrolment_id)
    if _failed(enr):
        return enr
    if enr is None:
        return error(f"No enrolment with id {enrolment_id}.")
    if enr["caterer_id"] is None:
        return conflict(f"Enrolment {enrolment_id}'s school has no caterer; cannot record a rating.")

    last_meal = _last_week_meal(enrolment_id, orders_batch.monday_of_week(week_of))
    if _failed(last_meal):
        return last_meal

    return {
        "enrolment_id": enrolment_id,
        "student_name": enr["student_name"],
        "source": _STUDENT_SOURCE,
        "caterer_id": enr["caterer_id"],
        "rating": rating,
        "comment": (comment or "").strip() or None,
        "order_line_id": last_meal.order_line_id if isinstance(last_meal, LastMeal) else None,
        "order_id": last_meal.order_id if isinstance(last_meal, LastMeal) else None,
        "rated_meal": last_meal.item if isinstance(last_meal, LastMeal) else None,
    }


def record_meal_rating(
    enrolment_id: int, rating: int, comment: str | None = None, week_of: date | None = None
) -> ToolResult:
    """Record a student's RATING + comment of last week's meal as a ``feedback`` row
    (source='student'), linked to the rated order_line when there is one.

    Feeds the same caterer quality signal as tutor/manager feedback
    (``get_caterer_feedback``). A benign, reversible fact — no send. The agent calls
    this after parsing the reply. When ``week_of`` is omitted it defaults to the
    week the student's latest choice email was for (so the rating lands on the meal
    they were asked to rate)."""
    week_of = orders_batch.monday_of_week(week_of or default_reply_week(enrolment_id))
    row = plan_meal_rating(enrolment_id, rating, comment, week_of)
    if _failed(row):
        return row

    def work(cur: psycopg.Cursor) -> dict:
        # Idempotent on the rated meal: a student rates a given order_line at most
        # once (re-submitting the same reply UPDATES that row rather than adding a
        # duplicate). feedback has no natural unique key, so we upsert in-app on
        # (order_line_id, source='student'). When there's no linked meal
        # (order_line_id NULL — they had no meal to rate) we just insert.
        if row["order_line_id"] is not None:
            cur.execute(
                "SELECT id FROM feedback WHERE order_line_id = %s AND source = %s LIMIT 1",
                (row["order_line_id"], row["source"]),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """
                    UPDATE feedback
                    SET rating = %s, comment = %s, caterer_id = %s,
                        order_id = %s, submitted_at = now()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (row["rating"], row["comment"], row["caterer_id"],
                     row["order_id"], existing["id"]),
                )
                return cur.fetchone()
        cur.execute(
            """
            INSERT INTO feedback (source, order_line_id, order_id, caterer_id, rating, comment)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (row["source"], row["order_line_id"], row["order_id"],
             row["caterer_id"], row["rating"], row["comment"]),
        )
        return cur.fetchone()

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            fb = work(cur)
            conn.commit()
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while recording rating: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while recording rating: {exc}")

    logger.info(
        "record_meal_rating: enrolment=%s caterer=%s rating=%s feedback=%s",
        enrolment_id, row["caterer_id"], row["rating"], fb["id"],
    )
    rated = f" of {row['rated_meal']}" if row["rated_meal"] else ""
    return found(
        {"feedback_id": fb["id"], **row},
        f"Recorded {row['student_name']}'s {row['rating']}/5 rating{rated} "
        f"(caterer {row['caterer_id']}).",
    )
