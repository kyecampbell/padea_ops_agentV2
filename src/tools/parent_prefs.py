"""Parent meal-preference requests + flexible resolution for non-responders.

A student is "defaulted" when they are dietary-SAFE but have no usable meal
preference — compose_week gives them a tentative safe default line. This module
owns the deterministic (no-LLM) follow-up:

  - Send the parent a ONE-TIME preferences request (``parent_prefs_request``).
    Tracked by (related_enrolment_id, email_type), so it is sent exactly once per
    student, EVER — never re-sent week after week.
  - Flexible resolution for non-responders: once a student has been asked in a
    PRIOR run and still has no preference, and their dietary is KNOWN/confirmed
    ('No requirements' or real tags — NOT unknown), set their term preference to
    ALL their eligible meals. The optimizer can then give them any convenient
    eligible meal (rotation still varies it), and they stop being defaulted /
    re-noted. UNKNOWN-dietary students NEVER go flexible — they stay escalated
    until a human confirms their dietary needs.

Classification (``plan_prefs``) is pure (no writes, no sends), so the dry run and
the test can assert the intended behaviour with zero risk. ``apply_flexible``
writes a preference (a DATA change, not a send); ``send_prefs_request`` is the
only function that sends, and it is idempotent.

Conventions: integer cents; parameterised SQL; never raises at the caller (DB
failures come back as typed ``ToolResult``s).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools import email as email_tool
from src.tools import writes
from src.tools.results import ToolResult, error, found, unavailable

logger = logging.getLogger(__name__)

PREFS_REQUEST_TYPE = "parent_prefs_request"
_DEFAULT_SOURCE = "defaulted_pending_confirmation"
# A prior prefs-request in any of these states counts as "already asked".
_ACTIVE_EMAIL_STATES = ("sent", "queued_for_approval", "approved")

# Action codes from ``plan_prefs``.
ACTION_FIRST_ASK = "first_ask"   # never asked -> send the one-time prefs request
ACTION_FLEXIBLE = "flexible"     # asked before, dietary known -> go flexible
ACTION_SKIP = "skip"             # dietary unknown (shouldn't be defaulted) -> leave alone


# --- Value objects -----------------------------------------------------------


@dataclass
class DefaultedStudent:
    """A defaulted student this week, with the facts the follow-up keys on."""

    enrolment_id: int
    student_name: str
    parent_name: str | None
    parent_email: str | None
    school_name: str | None
    caterer_id: int | None
    item: str
    dietary_known: bool
    already_requested: bool


@dataclass
class PrefsAction:
    """The classified follow-up for one defaulted student (no side effects)."""

    student: DefaultedStudent
    action: str


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


def _week_bounds(week: date) -> tuple[date, date]:
    return week, week + timedelta(days=7)


# --- Defaulted-student discovery ---------------------------------------------


def defaulted_students(week: date) -> list[DefaultedStudent] | ToolResult:
    """Every defaulted student for the week (deduped), with dietary-known and
    already-requested flags resolved in one query.

    ``dietary_known`` = a non-blank dietary note OR at least one dietary tag.
    ``already_requested`` = a non-failed ``parent_prefs_request`` already logged
    for this enrolment (ever).
    """
    start, end = _week_bounds(week)
    rows = _read(
        f"loading defaulted students for week {week}",
        """
        SELECT DISTINCT
            e.id AS enrolment_id, e.student_name, e.parent_name, e.parent_email,
            s.name AS school_name, s.current_caterer_id AS caterer_id, mi.name AS item,
            (e.dietary_raw IS NOT NULL AND btrim(e.dietary_raw) <> ''
             OR EXISTS (SELECT 1 FROM enrolment_dietary_tags edt
                        WHERE edt.enrolment_id = e.id)) AS dietary_known,
            EXISTS (
                SELECT 1 FROM outbound_emails oe
                WHERE oe.related_enrolment_id = e.id
                  AND oe.email_type = %s
                  AND oe.status = ANY(%s)
            ) AS already_requested
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN enrolments e ON e.id = ol.enrolment_id
        JOIN schools s ON s.id = e.school_id
        JOIN menu_items mi ON mi.id = ol.menu_item_id
        WHERE o.caterer_id = s.current_caterer_id
          AND o.session_date >= %s AND o.session_date < %s
          AND ol.source = %s
        ORDER BY e.student_name
        """,
        (PREFS_REQUEST_TYPE, list(_ACTIVE_EMAIL_STATES), start, end, _DEFAULT_SOURCE),
    )
    if _failed(rows):
        return rows
    return [
        DefaultedStudent(
            enrolment_id=r["enrolment_id"], student_name=r["student_name"],
            parent_name=r["parent_name"], parent_email=r["parent_email"],
            school_name=r["school_name"], caterer_id=r["caterer_id"], item=r["item"],
            dietary_known=r["dietary_known"], already_requested=r["already_requested"],
        )
        for r in rows
    ]


def _eligible_item_names(enrolment_id: int) -> list[str] | ToolResult:
    """The names of the student's dietary-eligible menu items (their safe choices
    on their caterer's menu), name-ordered, for the prefs-request menu list."""
    rows = _read(
        f"loading eligible menu names for enrolment {enrolment_id}",
        """
        SELECT mi.name
        FROM student_eligible_meals sem
        JOIN menu_items mi ON mi.id = sem.menu_item_id
        WHERE sem.enrolment_id = %s AND sem.eligible = TRUE AND mi.active = TRUE
        ORDER BY mi.name
        """,
        (enrolment_id,),
    )
    if _failed(rows):
        return rows
    return [r["name"] for r in rows]


def _dietary_labels(enrolment_id: int) -> list[str] | ToolResult:
    """The student's on-record dietary requirement labels (empty when none),
    label-ordered, for the prefs-request dietary-confirmation line."""
    rows = _read(
        f"loading dietary labels for enrolment {enrolment_id}",
        """
        SELECT dt.label
        FROM enrolment_dietary_tags edt
        JOIN dietary_tags dt ON dt.id = edt.dietary_tag_id
        WHERE edt.enrolment_id = %s
        ORDER BY dt.label
        """,
        (enrolment_id,),
    )
    if _failed(rows):
        return rows
    return [r["label"] for r in rows]


def plan_prefs(week: date) -> list[PrefsAction] | ToolResult:
    """Classify each defaulted student's follow-up — PURE (no writes, no sends).

    - dietary unknown -> ``skip`` (should never be defaulted; never goes flexible);
    - already asked in a prior run + dietary known -> ``flexible``;
    - not yet asked -> ``first_ask``.
    """
    students = defaulted_students(week)
    if _failed(students):
        return students
    actions: list[PrefsAction] = []
    for st in students:
        if not st.dietary_known:
            action = ACTION_SKIP
        elif st.already_requested:
            action = ACTION_FLEXIBLE
        else:
            action = ACTION_FIRST_ASK
        actions.append(PrefsAction(student=st, action=action))
    return actions


# --- Flexible resolution (DATA write — not a send) ---------------------------


def _eligible_item_ids(enrolment_id: int) -> list[int] | ToolResult:
    """The student's dietary-eligible menu item ids (their safe pool), id-ordered."""
    rows = _read(
        f"loading eligible pool for enrolment {enrolment_id}",
        """
        SELECT menu_item_id FROM student_eligible_meals
        WHERE enrolment_id = %s AND eligible = TRUE
        ORDER BY menu_item_id
        """,
        (enrolment_id,),
    )
    if _failed(rows):
        return rows
    return [r["menu_item_id"] for r in rows]


def apply_flexible(enrolment_id: int) -> ToolResult:
    """Set a student's term preference to ALL their eligible meals (flexible).

    A DATA write (reuses ``writes.update_term_meal_preference``, which validates
    every item is on the caterer's menu and dietary-eligible), so the optimizer can
    rotate them through any convenient safe meal instead of defaulting them. Does
    NOT send anything.
    """
    eligible = _eligible_item_ids(enrolment_id)
    if _failed(eligible):
        return eligible
    if not eligible:
        return error(
            f"enrolment {enrolment_id} has no eligible meals; cannot set a flexible "
            "preference (this student should be escalated, not defaulted)."
        )
    result = writes.update_term_meal_preference(enrolment_id, eligible)
    if result.ok:
        logger.info(
            "apply_flexible: enrolment=%s set flexible preference over %d eligible meals.",
            enrolment_id, len(eligible),
        )
    return result


def resolve_non_responders(week: date) -> ToolResult:
    """Apply flexible resolution to every eligible non-responder for the week.

    A non-responder is a defaulted student who is dietary-known AND was already
    sent a prefs request in a prior run (``plan_prefs`` -> ``flexible``). Returns
    the enrolment ids resolved; the caller should RE-COMPOSE when any were, so the
    resolved students get a proper rotated pick instead of a default this week.
    """
    actions = plan_prefs(week)
    if _failed(actions):
        return actions
    resolved: list[int] = []
    for a in actions:
        if a.action != ACTION_FLEXIBLE:
            continue
        res = apply_flexible(a.student.enrolment_id)
        if not res.ok:
            return res
        resolved.append(a.student.enrolment_id)
    return found(
        {"week_of": week.isoformat(), "resolved": resolved},
        f"Flexible resolution applied to {len(resolved)} non-responder(s).",
    )


# --- One-time prefs request (the only sending function) ----------------------


def _menu_block(choices: list[str]) -> str:
    """The 'meals they can choose from' section (empty string when none known)."""
    if not choices:
        return ""
    bullets = "\n".join(f"  - {name}" for name in choices)
    return (
        f"Here are the meals {{name}} can choose from (all suitable for them):\n"
        f"{bullets}\n\n"
    )


def _dietary_line(student_name: str, dietary_labels: list[str]) -> str:
    """The dietary-confirmation line: states what's on record and asks the parent
    to reply if it has changed, so we keep the student's meals safe."""
    if dietary_labels:
        record = f"has these dietary requirements on record: {', '.join(dietary_labels)}"
    else:
        record = "has no dietary requirements on record"
    return (
        f"Our records show {student_name} {record}. If that's changed, please reply "
        f"and let us know so we keep {student_name}'s meals safe.\n\n"
    )


def render_prefs_request(
    student_name: str,
    school_name: str | None,
    item: str,
    choices: list[str] | None = None,
    dietary_labels: list[str] | None = None,
) -> tuple[str, str]:
    """The deterministic prefs-request subject + body (no LLM).

    Warm, no-action-needed framing, now with the list of meals the student can
    choose from (their dietary-safe menu) and a dietary-confirmation line stating
    what's on record so the parent can correct it.
    """
    subject = f"Padea — meal preferences for {student_name}"
    menu = _menu_block(choices or []).format(name=student_name)
    dietary = _dietary_line(student_name, dietary_labels or [])
    body = (
        f"Hi,\n\n"
        f"We don't yet have meal preferences on file for {student_name}"
        f"{f' at {school_name}' if school_name else ''}. So they don't miss out, "
        f"we've gone ahead and arranged {item} for them this week.\n\n"
        f"{menu}"
        f"When you have a moment, please reply with the meals {student_name} would "
        f"like and we'll set those as their preferences. If we don't hear back, "
        f"we'll simply keep choosing a suitable meal for them each week — no action "
        f"needed on your part.\n\n"
        f"{dietary}"
        f"Thank you,\nPadea Operations"
    )
    return subject, body


def build_prefs_request(student: DefaultedStudent) -> tuple[str, str] | ToolResult:
    """The full prefs-request (subject, body) for one student — loads their safe
    menu choices + on-record dietary tags, then renders. PURE (no send), so the
    dry run and the real send share exactly the same rendered email."""
    choices = _eligible_item_names(student.enrolment_id)
    if _failed(choices):
        return choices
    dietary_labels = _dietary_labels(student.enrolment_id)
    if _failed(dietary_labels):
        return dietary_labels
    return render_prefs_request(
        student.student_name, student.school_name, student.item,
        choices=choices, dietary_labels=dietary_labels,
    )


def prefs_request_exists(enrolment_id: int) -> bool | ToolResult:
    """True if a non-failed prefs request was already logged for this student."""
    rows = _read(
        f"checking prior prefs request for enrolment {enrolment_id}",
        """
        SELECT 1 FROM outbound_emails
        WHERE related_enrolment_id = %s AND email_type = %s AND status = ANY(%s)
        LIMIT 1
        """,
        (enrolment_id, PREFS_REQUEST_TYPE, list(_ACTIVE_EMAIL_STATES)),
    )
    if _failed(rows):
        return rows
    return bool(rows)


def send_prefs_request(student: DefaultedStudent, run_id: int | None = None) -> ToolResult:
    """Send the ONE-TIME prefs request for one student (idempotent).

    Skips (without sending) if the student has no parent email or has already been
    asked. The only function in this module that actually sends mail.
    """
    if not student.parent_email:
        return found(
            {"sent": False, "reason": "no parent email"},
            f"No parent email for {student.student_name}; prefs request not sent.",
        )
    already = prefs_request_exists(student.enrolment_id)
    if _failed(already):
        return already
    if already:
        return found(
            {"sent": False, "reason": "already requested"},
            f"Prefs request already sent for {student.student_name}; not re-sending.",
        )

    built = build_prefs_request(student)
    if _failed(built):
        return built
    subject, body = built
    return email_tool.send_email(
        email_type=PREFS_REQUEST_TYPE, to=student.parent_email, subject=subject,
        body=body, related_enrolment_id=student.enrolment_id, related_run_id=run_id,
    )


def send_prefs_requests(week: date, run_id: int | None = None) -> ToolResult:
    """Send a one-time prefs request to each first-ask defaulted student this week.

    First-ask only: students flagged ``flexible`` are non-responders handled by
    ``resolve_non_responders`` (and are no longer defaulted after the re-compose),
    so they never reach here. Idempotent per student.
    """
    actions = plan_prefs(week)
    if _failed(actions):
        return actions
    sent: list[dict] = []
    skipped: list[dict] = []
    for a in actions:
        if a.action != ACTION_FIRST_ASK:
            skipped.append({"enrolment_id": a.student.enrolment_id,
                            "student_name": a.student.student_name, "reason": a.action})
            continue
        res = send_prefs_request(a.student, run_id=run_id)
        if res.ok and (res.data or {}).get("sent", True):
            sent.append({"enrolment_id": a.student.enrolment_id, "student_name": a.student.student_name})
        else:
            skipped.append({"enrolment_id": a.student.enrolment_id,
                            "student_name": a.student.student_name,
                            "reason": (res.data or {}).get("reason", res.status)})
    return found(
        {"week_of": week.isoformat(), "sent": sent, "skipped": skipped},
        f"Prefs requests for week of {week.isoformat()}: {len(sent)} sent, {len(skipped)} skipped.",
    )
