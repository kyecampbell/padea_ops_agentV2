"""Thursday order batch — dietary-aware MOQ composition + per-student rotation.

Responsibility: for a given week, compose every caterer's consolidated order from
the active cohort's ranked term preferences and their dietary-safe pool. The
direction is unchanged — maximize variety UP TO the caterer's MOQ ceiling and
NEVER breach it — but the mechanism is now dietary-first and rotation-driven.
This module ONLY computes and persists orders (or escalates); it sends no
caterer/parent emails. Deterministic and idempotent per (caterer, week).

What "compose_week" does, per caterer:
  1. Gather the caterer's active sessions for the week (every active session_slot
     of every school the caterer serves) and, for each, the active cohort — the
     students ROSTERED to that session (``enrolment_session_slots``) who are not
     opted out, inside their enrolment period, and not excluded or absent for that
     session date. A student's meals come from their rostered session(s) ONLY, not
     every session at their school, so a multi-session school's cohort differs per
     session. cohort_candidates = these rostered student-session orders (the total
     MEAL quantity for the week, against which V_max is sized).
  2. V_max — the HARD variety ceiling. N = cohort_candidates - typical_absences;
     a further safety margin (settings.moq_safety_margin) is subtracted so an
     above-typical absence spike can't tip the order below a tier floor we
     committed to. V_max = the largest ``caterer_moq_tier.variety_count`` whose
     ``min_total_items`` floor is <= that budget. The order's DISTINCT-item count
     must never exceed V_max (priority 1 — never breach).
  3. Offered set (size <= V_max): chosen to (a) COVER every dietary student —
     each must have >= 1 eligible item in the set — then (b) fill the remaining
     slots with the cohort's most-preferred items to maximize satisfaction.
     Concentration is fine (30 of one meal is OK).
  4. Per-student ROTATION — a per-meal SEQUENCE, not one meal per week: each
     student is assigned a meal for EACH of their rostered sessions, drawn from
     their eligible preferences in the offered set. Within a week a student
     rostered to >1 session gets a DIFFERENT meal at each (distinct items where
     their eligible prefs allow). Across weeks the pick prefers a meal they did
     NOT receive in their recent meal instances (last
     ``settings.rotation_lookback_weeks`` weeks of order history) and avoids
     repeating the immediately-preceding meal, so a 2-session student never lands
     the same meal four sessions running. Occasional repeats by chance are fine.
     A safe student with no usable preference keeps a single tentative default
     across their sessions (see below).
  5. Since every ordered line comes from the offered set, distinct items ordered
     <= V_max, so the MOQ floor is always met — no breach, no variance.

Outcomes (per caterer-week):
  - Everyone safe AND distinct items <= V_max -> compose ``orders`` /
    ``order_lines`` / one ``caterer_week_orders`` summary (the sendable path).

Per-student handling within a composed caterer-week (the "Sally Jane" model —
compose the safe majority, handle stragglers individually):
  - Safe + has a usable (eligible) preference -> rotated assignment.
  - Safe but NO usable preference (dietary known/safe) -> the most-popular item
    in their eligible pool that is in the offered set, as a tentative DEFAULT
    line flagged ``defaulted_pending_confirmation`` (the gap flow confirms with
    the parent within 48h). They still get a SAFE line.
  - dietary UNKNOWN (blank/NULL dietary) -> NO line; escalate that student. We
    NEVER default an unknown-allergy child onto a meal.
  - no_safe_meal (dietary known, empty eligible pool here) -> NO line; escalate
    that student.

The caterer auto-composes the safe majority + defaults even when some students
are escalated individually. A caterer-WIDE escalation (compose NOTHING sendable)
happens ONLY when the safe order itself can't proceed:
  - the MOQ floor can't be met for the remaining safe order, OR
  - dietary coverage is infeasible within V_max (no <=V_max set gives every
    safe dietary student a meal).

Idempotent per (caterer, week): a re-run replaces that caterer's orders, weekly
summary, and BOTH its caterer-wide and per-student escalations for the week
rather than duplicating, and flips cleanly between the composed and escalated
states. Rotation reads only history STRICTLY before the target week, so a re-run
never reads its own output.

Conventions: absolute imports; money is integer cents (never floats);
timestamps are timezone-aware (DB ``now()``); never raises at the caller — DB
failures come back as typed ``ToolResult``s.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config.settings import settings
from src.db.connection import get_conn
from src.tools.results import ToolResult, error, found, unavailable

# order_line_source provenance codes. Most lines are rotation-sourced (the
# student's rotated standing preference); a safe student with no usable
# preference gets a tentative default line, flagged for parent confirmation; a
# student who made a weekly PICK gets a request-sourced line for that session.
_LINE_SOURCE = "rotation"
_DEFAULT_SOURCE = "defaulted_pending_confirmation"
_REQUEST_SOURCE = "request"  # the student's own weekly choose-and-rate pick.

# Caterer-WIDE escalation reasons (compose nothing sendable for the caterer).
ESC_COVERAGE_INFEASIBLE = "coverage_infeasible"  # no <=V_max set covers everyone
ESC_MOQ_BREACH = "moq_breach_unavoidable"     # safe order too small for any tier floor

# Per-STUDENT escalation reasons (the rest of the caterer still composes).
STUDENT_DIETARY_UNCONFIRMED = "dietary_unconfirmed"  # unknown dietary -> confirm first
STUDENT_NO_SAFE_MEAL = "no_safe_meal"                # known dietary, empty safe pool

# context->>'kind' tags — let a re-run find and replace its own prior escalations
# (caterer-wide and per-student) for the (caterer, week).
_ESC_KIND = "thursday_moq_compose"
_ESC_STUDENT_KIND = "thursday_student_dietary"


# --- Result shapes -----------------------------------------------------------


@dataclass
class SessionSummary:
    """Per-session order figures (one row of the caterer's week)."""

    session_slot_id: int
    school_name: str
    session_date: str          # ISO date
    total_items: int
    total_cost_cents: int      # GST-normalised


@dataclass
class RotationSample:
    """One student's rotation evidence: the meal sequence assigned this week (one
    per rostered session, in session order) and the meals they received over the
    lookback window (oldest -> newest)."""

    enrolment_id: int
    student_name: str
    assigned_items: list[str]
    recent_items: list[str]


@dataclass
class CatererWeekSummary:
    """Everything the report needs for one caterer's composed (or escalated) week."""

    caterer_id: int
    caterer_name: str
    caterer_contact_email: str | None  # where the order email should go
    schools: list[str]
    week_of: str                       # ISO date (Monday)
    num_sessions: int
    status: str                        # "composed" | "escalated"

    cohort_candidates: int             # ALL active student-session orders this week
    safe_candidates: int               # safe student-sessions (basis for V_max)
    no_line_count: int                 # students escalated individually (no line)
    typical_absences: int
    safety_margin: int                 # extra MOQ buffer (meals)
    expected_orders: int               # safe_candidates - typical_absences
    vmax_budget: int                   # expected_orders - safety_margin
    v_max: int                         # HARD variety ceiling

    offered_items: list[str]           # the chosen offered set (<= V_max)
    offered_count: int
    variety_count: int                 # distinct items ACTUALLY ordered
    total_items: int                   # order lines written
    defaulted_count: int               # of total_items, how many are defaults

    moq_min_total: int | None          # floor that applies at variety_count
    moq_floor_applied: bool            # always False on the composed path
    moq_variance_cents: int            # always 0 on the composed path
    total_cost_cents: int              # GST-normalised week total
    gst_rate_percent: float
    price_includes_gst: bool
    caterer_week_orders_id: int | None

    escalation_id: int | None = None
    escalation_reason: str | None = None
    escalation_detail: str | None = None
    request_count: int = 0  # of total_items, how many are student weekly picks

    # The week's order as a meal-by-meal breakdown for the caterer email:
    # one entry per distinct ordered item, {menu_item_id, item, quantity}.
    meal_breakdown: list[dict] = field(default_factory=list)
    sessions: list[SessionSummary] = field(default_factory=list)
    rotation_sample: list[RotationSample] = field(default_factory=list)
    # Every defaulted (pending-confirmation) line, with the parent contact the
    # agent needs to send the "we've assumed [meal]" email:
    # {enrolment_id, student_name, parent_name, parent_email, school_name, item}.
    defaulted_lines: list[dict] = field(default_factory=list)
    escalated_students: list[dict] = field(default_factory=list)  # {enrolment_id, student_name, parent_*, reason, detail}

    def as_dict(self) -> dict:
        d = asdict(self)
        d["gst_rate_percent"] = float(self.gst_rate_percent)
        return d


# --- Date helpers ------------------------------------------------------------


def monday_of_week(d: date) -> date:
    """The Monday on or before ``d`` (ISO weekday 1 = Monday)."""
    return d - timedelta(days=d.weekday())


def upcoming_monday(today: date) -> date:
    """The next Monday strictly after ``today`` (the week the batch will cover).

    Run on a Thursday, this returns the following Monday — the start of the week
    the batch composes orders for.
    """
    return monday_of_week(today) + timedelta(days=7)


def _session_date(week_of: date, day_of_week: int) -> date:
    """Concrete date of a (week, day-of-week) slot. day_of_week: 1=Mon..7=Sun."""
    return week_of + timedelta(days=day_of_week - 1)


# --- DB access (typed-result wrappers) ---------------------------------------


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


# --- Cohort gathering --------------------------------------------------------


def _caterers(only_caterer_id: int | None) -> list[dict] | ToolResult:
    """Caterers that currently serve at least one school, with GST settings."""
    where = "WHERE EXISTS (SELECT 1 FROM schools s WHERE s.current_caterer_id = c.id)"
    params: tuple = ()
    if only_caterer_id is not None:
        where += " AND c.id = %s"
        params = (only_caterer_id,)
    return _read(
        "listing caterers",
        f"""
        SELECT c.id, c.name, c.contact_email, c.price_includes_gst,
               c.gst_rate_percent, c.delivery_fee_cents
        FROM caterers c
        {where}
        ORDER BY c.id
        """,
        params,
    )


def _sessions_for_caterer(caterer_id: int) -> list[dict] | ToolResult:
    """Active sessions of every school this caterer serves."""
    return _read(
        f"listing sessions for caterer {caterer_id}",
        """
        SELECT ss.id AS session_slot_id, ss.school_id, ss.day_of_week,
               s.name AS school_name
        FROM session_slots ss
        JOIN schools s ON s.id = ss.school_id
        WHERE s.current_caterer_id = %s AND ss.active = TRUE
        ORDER BY ss.school_id, ss.day_of_week
        """,
        (caterer_id,),
    )


def _active_cohort(session_slot_id: int, on_date: date) -> list[dict] | ToolResult:
    """Active students ROSTERED to a session for a session date.

    The cohort is the students rostered to this session_slot
    (``enrolment_session_slots``) — NOT every student at the school — minus those
    opted out, outside their enrolment period, with a recorded absence for the
    date, or covered by an exclusion (student-, school-, or system-wide). Carries
    each student's dietary-requirement flag and a dietary-unknown flag (blank/NULL
    dietary_raw, no tag — never confirmed).
    """
    return _read(
        f"gathering cohort for session {session_slot_id} on {on_date}",
        """
        SELECT e.id, e.student_name, e.parent_name, e.parent_email,
               (e.dietary_raw IS NOT NULL AND btrim(e.dietary_raw) <> ''
                OR EXISTS (SELECT 1 FROM enrolment_dietary_tags edt
                           WHERE edt.enrolment_id = e.id)) AS has_dietary,
               (e.dietary_raw IS NULL OR btrim(e.dietary_raw) = '')
               AND NOT EXISTS (SELECT 1 FROM enrolment_dietary_tags edt
                               WHERE edt.enrolment_id = e.id) AS dietary_unknown
        FROM enrolments e
        JOIN enrolment_session_slots ess ON ess.enrolment_id = e.id
        WHERE ess.session_slot_id = %s
          AND e.opted_out_of_catering = FALSE
          AND e.current_period_start_date <= %s
          AND (e.current_period_end_date IS NULL
               OR e.current_period_end_date >= %s)
          AND NOT EXISTS (
              SELECT 1 FROM absences a
              WHERE a.enrolment_id = e.id AND a.absence_date = %s)
          AND NOT EXISTS (
              SELECT 1 FROM exclusions x
              WHERE %s BETWEEN x.start_date AND x.end_date
                AND (x.enrolment_id = e.id
                     OR (x.enrolment_id IS NULL
                         AND (x.school_id = e.school_id OR x.school_id IS NULL))))
        ORDER BY e.student_name
        """,
        (session_slot_id, on_date, on_date, on_date, on_date),
    )


def _menu(caterer_id: int) -> dict[int, dict] | ToolResult:
    """{menu_item_id: {name, price_cents}} for a caterer's active items."""
    rows = _read(
        f"loading menu for caterer {caterer_id}",
        "SELECT id, name, price_cents FROM menu_items WHERE caterer_id = %s AND active = TRUE",
        (caterer_id,),
    )
    if _failed(rows):
        return rows
    return {r["id"]: r for r in rows}


def _ranked_preferences(enrolment_ids: list[int], caterer_id: int) -> dict[int, list[int]] | ToolResult:
    """{enrolment_id: [menu_item_id ...]} ranked best-first, for this caterer's
    current (non-superseded) preference set. Unranked items sort last."""
    if not enrolment_ids:
        return {}
    rows = _read(
        f"loading preferences for caterer {caterer_id}",
        """
        SELECT tmp.enrolment_id, tmpi.menu_item_id, tmpi.rank
        FROM term_meal_preferences tmp
        JOIN term_meal_preference_items tmpi ON tmpi.preference_id = tmp.id
        WHERE tmp.caterer_id = %s
          AND tmp.superseded_at IS NULL
          AND tmp.enrolment_id = ANY(%s)
        ORDER BY tmp.enrolment_id, tmpi.rank NULLS LAST
        """,
        (caterer_id, enrolment_ids),
    )
    if _failed(rows):
        return rows
    prefs: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        prefs[r["enrolment_id"]].append(r["menu_item_id"])
    return prefs


def _eligible_pool(enrolment_ids: list[int]) -> dict[int, set[int]] | ToolResult:
    """{enrolment_id: {eligible menu_item_id ...}} from student_eligible_meals."""
    if not enrolment_ids:
        return {}
    rows = _read(
        "loading eligible pools",
        """
        SELECT enrolment_id, menu_item_id
        FROM student_eligible_meals
        WHERE eligible = TRUE AND enrolment_id = ANY(%s)
        """,
        (enrolment_ids,),
    )
    if _failed(rows):
        return rows
    pool: dict[int, set[int]] = defaultdict(set)
    for r in rows:
        pool[r["enrolment_id"]].add(r["menu_item_id"])
    return pool


def _recent_meals(
    enrolment_ids: list[int], week_of: date, lookback_weeks: int
) -> dict[int, dict[int, date]] | ToolResult:
    """{enrolment_id: {menu_item_id: most-recent session_date}} for the rotation
    window ``[week_of - lookback_weeks*7, week_of)``.

    The window is STRICTLY before the target week, so composing the same week
    twice never lets a re-run read its own freshly written order lines — keeping
    the rotation (and therefore the whole batch) deterministic and idempotent.
    """
    if not enrolment_ids or lookback_weeks <= 0:
        return {}
    window_start = week_of - timedelta(weeks=lookback_weeks)
    rows = _read(
        "loading rotation history",
        """
        SELECT ol.enrolment_id, ol.menu_item_id, max(o.session_date) AS last_date
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        WHERE ol.enrolment_id = ANY(%s)
          AND o.session_date >= %s
          AND o.session_date < %s
        GROUP BY ol.enrolment_id, ol.menu_item_id
        """,
        (enrolment_ids, window_start, week_of),
    )
    if _failed(rows):
        return rows
    out: dict[int, dict[int, date]] = defaultdict(dict)
    for r in rows:
        out[r["enrolment_id"]][r["menu_item_id"]] = r["last_date"]
    return out


def _student_picks(
    enrolment_ids: list[int], week_of: date
) -> dict[int, dict[int, int]] | ToolResult:
    """{enrolment_id: {session_slot_id: menu_item_id}} — the students' own weekly
    PICKS (``meal_requests``) for sessions in ``[week_of, week_of + 7)``.

    Read verbatim; the caller validates each pick is dietary-safe AND in the
    offered set before honouring it (an invalid/stale pick is simply not applied,
    and the student falls back to rotation / default). The most recent request per
    (enrolment, slot) wins, so a re-pick supersedes an earlier one.
    """
    if not enrolment_ids:
        return {}
    start, end = week_of, week_of + timedelta(days=7)
    rows = _read(
        "loading student meal picks",
        """
        SELECT DISTINCT ON (enrolment_id, session_slot_id)
               enrolment_id, session_slot_id, menu_item_id
        FROM meal_requests
        WHERE enrolment_id = ANY(%s)
          AND session_date >= %s AND session_date < %s
        ORDER BY enrolment_id, session_slot_id, requested_at DESC
        """,
        (enrolment_ids, start, end),
    )
    if _failed(rows):
        return rows
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for r in rows:
        out[r["enrolment_id"]][r["session_slot_id"]] = r["menu_item_id"]
    return out


# --- MOQ ceiling + variety scoring -------------------------------------------


def _vmax(tiers: list[dict], budget: int) -> int:
    """The HARD variety ceiling: the largest ``variety_count`` whose
    ``min_total_items`` floor the budget can fill. 0 if even the smallest tier's
    floor exceeds the budget (any order would breach -> escalate)."""
    affordable = [t["variety_count"] for t in tiers if t["min_total_items"] <= budget]
    return max(affordable) if affordable else 0


def _floor_for_variety(tiers: list[dict], variety: int) -> int | None:
    """The MOQ floor that applies once ``variety`` distinct items are ordered.

    Crossing a variety breakpoint commits you to that tier's floor, so the
    applicable tier is the one with the greatest ``variety_count`` <= ``variety``,
    clamped to the smallest tier when fewer varieties than any tier are ordered.
    """
    if not tiers:
        return None
    at_or_below = [t for t in tiers if t["variety_count"] <= variety]
    chosen = max(at_or_below, key=lambda t: t["variety_count"]) if at_or_below \
        else min(tiers, key=lambda t: t["variety_count"])
    return chosen["min_total_items"]


def _rank_weighted_popularity(students: dict[int, dict]) -> dict[int, float]:
    """Score each menu item by summed rank weight across the cohort's eligible
    preferences (each student counted once). A top (rank-1) pick contributes 1.0,
    the next 1/2, then 1/3 … — so the fill step favours items the cohort ranks
    highly and broadly."""
    score: dict[int, float] = defaultdict(float)
    for s in students.values():
        for position, item_id in enumerate(s["eligible_prefs"], start=1):
            score[item_id] += 1.0 / position
    return score


def _select_offered_set(
    students: dict[int, dict],
    dietary_ids: list[int],
    all_items: set[int],
    popularity: dict[int, float],
    v_max: int,
) -> tuple[set[int], list[int]]:
    """Choose the offered set (size <= ``v_max``).

    (a) COVER: greedily add the item that gives a safe meal to the most still-
        uncovered dietary students (ties broken by cohort preference popularity,
        then by id for determinism) until every dietary student is covered.
    (b) FILL: spend any leftover slots on the most-preferred remaining items, so
        more students can be served an actual preference.

    Returns ``(offered_set, uncovered)``; a non-empty ``uncovered`` means no
    <=V_max set can give every dietary student a safe meal (-> escalate).
    """
    by_popularity = sorted(all_items, key=lambda i: (-popularity.get(i, 0.0), i))
    chosen: set[int] = set()
    uncovered = set(dietary_ids)

    while uncovered and len(chosen) < v_max:
        best_item = None
        best_cover = 0
        for item in by_popularity:               # popularity order => deterministic ties
            if item in chosen:
                continue
            cover = sum(1 for s in uncovered if item in students[s]["pool"])
            if cover > best_cover:
                best_cover = cover
                best_item = item
        if best_item is None or best_cover == 0:
            break                                # nothing left can cover anyone
        chosen.add(best_item)
        uncovered = {s for s in uncovered if best_item not in students[s]["pool"]}

    # FILL remaining slots with the most-preferred items not yet offered.
    for item in by_popularity:
        if len(chosen) >= v_max:
            break
        chosen.add(item)

    return chosen, sorted(uncovered)


# --- Per-student rotation assignment -----------------------------------------


def _assign_sequence(
    session_order: list[tuple[int, date]],
    pref_candidates: list[int],
    recent: dict[int, date],
    rank: dict[int, int],
    forced: dict[int, int] | None = None,
) -> dict[int, int]:
    """Assign one meal per rostered session — a per-student meal SEQUENCE.

    ``session_order`` is the student's rostered (slot_id, session_date) pairs in
    chronological order. ``pref_candidates`` are their eligible preferences in the
    offered set, ``recent`` the most-recent date each item was received in the
    lookback window, and ``rank`` the item's preference position (lower = better).
    ``forced`` maps a slot to the meal the STUDENT PICKED for it (validated in the
    offered set + their safe pool by the caller) — that slot takes the pick
    verbatim instead of rotating.

    For each NON-forced session in turn the pick is the candidate that, by a total
    order:
      1. is NOT the immediately-preceding meal (within the week, or the most
         recent historical meal for the first session) — avoids back-to-back;
      2. has NOT already been used this week — distinct meals within the week;
      3. was least-recently received (never-received first) — cross-week rotation;
      4. is the higher preference, then the lowest id — deterministic ties.
    A forced (picked) slot is honoured verbatim. After each assignment the chosen
    item becomes "just used" so the next session avoids it (so a pick still pushes
    the rest of the sequence toward distinctness). With only one candidate the
    penalties simply tie and it repeats (an occasional, unavoidable repeat).
    Returns ``{slot_id: menu_item_id}``.
    """
    forced = forced or {}
    last_used = dict(recent)                       # item -> last date received
    used_this_week: set[int] = set()
    # Seed the back-to-back guard with the most recent historical meal, if any.
    prev_item = max(recent, key=lambda i: recent[i]) if recent else None

    assignment: dict[int, int] = {}
    for slot_id, s_date in session_order:
        if slot_id in forced:
            chosen = forced[slot_id]               # the student's own pick wins.
        else:
            def key(item: int) -> tuple:
                return (
                    item == prev_item,             # avoid the immediately-prior meal
                    item in used_this_week,        # prefer distinct within the week
                    last_used.get(item, date.min), # least-recently received first
                    rank.get(item, 10**9),         # then best preference
                    item,                          # then lowest id
                )

            chosen = min(pref_candidates, key=key)
        assignment[slot_id] = chosen
        used_this_week.add(chosen)
        last_used[chosen] = s_date
        prev_item = chosen
    return assignment


def _default_pick(pool_in_offered: set[int], popularity: dict[int, float]) -> int:
    """Tentative default for a safe student with NO usable preference: the most-
    popular item in their eligible pool that is in the offered set (ties by id).
    A SAFE meal — only their *preference* is unknown, not their dietary needs."""
    return min(pool_in_offered, key=lambda i: (-popularity.get(i, 0.0), i))


def _rotation_sample(
    students: dict[int, dict],
    student_items: dict[int, list[int]],
    recent: dict[int, dict[int, date]],
    menu: dict[int, dict],
    limit: int = 3,
) -> list[RotationSample]:
    """A few students' rotation evidence — multi-session students with the richest
    history first, so the report shows both within-week distinctness and real
    week-over-week movement."""
    ranked = sorted(
        students,
        key=lambda eid: (
            -len(student_items.get(eid, [])),       # multi-session first
            -len(recent.get(eid, {})),              # then richest history
            students[eid]["name"],
        ),
    )
    samples: list[RotationSample] = []
    for eid in ranked[:limit]:
        history = sorted(recent.get(eid, {}).items(), key=lambda kv: kv[1])
        samples.append(
            RotationSample(
                enrolment_id=eid,
                student_name=students[eid]["name"],
                assigned_items=[menu[i]["name"] for i in student_items.get(eid, [])],
                recent_items=[menu[i]["name"] for i, _ in history],
            )
        )
    return samples


# --- Cost helper -------------------------------------------------------------


def _gst_normalise(base_cents: int, *, includes_gst: bool, gst_rate_percent: float) -> int:
    """Return a GST-inclusive integer-cents total.

    If the caterer's prices already include GST, the base is returned unchanged;
    otherwise GST is added at the caterer's rate and rounded to whole cents.
    """
    if includes_gst:
        return base_cents
    return round(base_cents * (1.0 + float(gst_rate_percent) / 100.0))


# --- Top-level composition ---------------------------------------------------


def compose_week(week_of: date, *, run_id: int | None = None, only_caterer_id: int | None = None) -> ToolResult:
    """Compose and persist every caterer's consolidated order for ``week_of``.

    ``week_of`` should be the Monday of the target week. Returns a ``found``
    result whose ``data`` is ``{"week_of", "caterers": [<CatererWeekSummary> …]}``
    — each caterer either ``composed`` or ``escalated`` — or the first read
    failure (``unavailable`` / ``error``) encountered. Idempotent per (caterer,
    week): existing orders / escalations for the week are replaced.
    """
    week_of = monday_of_week(week_of)  # tolerate a mid-week date being passed in.

    caterers = _caterers(only_caterer_id)
    if _failed(caterers):
        return caterers
    if not caterers:
        return found({"week_of": week_of.isoformat(), "caterers": []}, "No active caterers.")

    summaries: list[dict] = []
    for caterer in caterers:
        result = _compose_caterer_week(caterer, week_of, run_id)
        if _failed(result):
            return result
        summaries.append(result)

    composed = sum(1 for c in summaries if c["status"] == "composed")
    escalated = len(summaries) - composed
    return found(
        {"week_of": week_of.isoformat(), "caterers": summaries},
        f"Composed week of {week_of.isoformat()}: {composed} caterer(s) composed, "
        f"{escalated} escalated.",
    )


@dataclass
class CatererWeekPlan:
    """Everything gathered + sized + offered for one caterer-week, BEFORE the
    per-student assignment and persistence. This is the single source of truth for
    the offered set: ``compose_week`` consumes it to assign + persist, and the
    student choose-and-rate email reads the SAME plan so the options a kid is shown
    are exactly the set compose later honours their pick against.

    Pure (reads only, no writes). When the caterer can't compose at all,
    ``escalation`` is a ``(reason, detail, context)`` triple the caller turns into a
    caterer-wide escalation; otherwise it is ``None`` and ``offered`` is the chosen
    set.
    """

    caterer: dict
    week_of: date
    run_id: int | None
    menu: dict[int, dict]
    tiers: list[dict]
    all_items: set[int]
    slot_ids: list[int]
    session_dates: list[date]
    school_names: dict[int, str]
    num_sessions: int
    gst_rate: float
    includes_gst: bool
    delivery_fee: int
    students: dict[int, dict]
    safe_students: dict[int, dict]
    stragglers: list[dict]
    student_sessions: dict[int, list[int]]
    session_meta: dict[int, tuple[date, str]]
    recent: dict[int, dict[int, date]]
    popularity: dict[int, float]
    offered: set[int]
    ceiling: int
    sizing: dict
    base: dict
    escalation: tuple[str, str, dict] | None = None


def plan_caterer_week(
    caterer: dict, week_of: date, run_id: int | None = None
) -> CatererWeekPlan | ToolResult:
    """Gather, size V_max, and choose the offered set for one caterer-week — PURE.

    Returns a ``CatererWeekPlan`` (with ``escalation`` set when the caterer can't
    compose: nothing safe to order, the safe order can't meet any MOQ floor, or
    dietary coverage is infeasible within V_max), or a typed read failure. Does NOT
    assign meals or write anything — that is ``compose_week``'s job. Extracted so
    the offered set is computed once and reused by the student choose-and-rate flow.
    """
    caterer_id = caterer["id"]
    includes_gst = caterer["price_includes_gst"]
    gst_rate = float(caterer["gst_rate_percent"])
    delivery_fee = caterer["delivery_fee_cents"]

    sessions = _sessions_for_caterer(caterer_id)
    if _failed(sessions):
        return sessions
    menu = _menu(caterer_id)
    if _failed(menu):
        return menu
    tiers_rows = _read(
        f"loading MOQ tiers for caterer {caterer_id}",
        "SELECT variety_count, min_total_items FROM caterer_moq_tier WHERE caterer_id = %s ORDER BY variety_count",
        (caterer_id,),
    )
    if _failed(tiers_rows):
        return tiers_rows
    tiers = list(tiers_rows)
    all_items = set(menu)

    school_names: dict[int, str] = {}
    session_cohorts: list[tuple[dict, list[dict], date]] = []
    enrolment_ids: set[int] = set()
    for sess in sessions:
        school_names[sess["school_id"]] = sess["school_name"]
        s_date = _session_date(week_of, sess["day_of_week"])
        cohort = _active_cohort(sess["session_slot_id"], s_date)
        if _failed(cohort):
            return cohort
        session_cohorts.append((sess, cohort, s_date))
        enrolment_ids.update(r["id"] for r in cohort)

    slot_ids = [s["session_slot_id"] for s in sessions]
    session_dates = [_session_date(week_of, s["day_of_week"]) for s in sessions]

    ids = list(enrolment_ids)
    prefs = _ranked_preferences(ids, caterer_id)
    if _failed(prefs):
        return prefs
    pool = _eligible_pool(ids)
    if _failed(pool):
        return pool
    recent = _recent_meals(ids, week_of, settings.rotation_lookback_weeks)
    if _failed(recent):
        return recent

    # --- Per-student view + each student's sessions this week. ---
    students: dict[int, dict] = {}
    student_sessions: dict[int, list[int]] = defaultdict(list)
    session_meta: dict[int, tuple[date, str]] = {}
    cohort_candidates = 0
    for sess, cohort, s_date in session_cohorts:
        session_meta[sess["session_slot_id"]] = (s_date, sess["school_name"])
        for st in cohort:
            eid = st["id"]
            if eid not in students:
                safe = pool.get(eid, set()) & all_items
                eligible_prefs = [i for i in prefs.get(eid, []) if i in safe]
                students[eid] = {
                    "name": st["student_name"],
                    "parent_name": st["parent_name"],
                    "parent_email": st["parent_email"],
                    "school_name": sess["school_name"],
                    "pool": safe,
                    "eligible_prefs": eligible_prefs,
                    "has_dietary": st["has_dietary"],
                    "dietary_unknown": st["dietary_unknown"],
                }
            student_sessions[eid].append(sess["session_slot_id"])
            cohort_candidates += 1

    base = dict(
        caterer=caterer, week_of=week_of, run_id=run_id,
        slot_ids=slot_ids, session_dates=session_dates, school_names=school_names,
        num_sessions=len(sessions), gst_rate=gst_rate, includes_gst=includes_gst,
    )

    # --- Partition: safe majority (get a line) vs stragglers (no line, escalated
    # individually). A student with an empty safe pool is NEVER given a default —
    # unknown dietary must be confirmed by a human; a known requirement with no
    # safe meal needs the menu resolved. The rest of the caterer still composes. ---
    safe_students: dict[int, dict] = {}
    stragglers: list[dict] = []   # {enrolment_id, student_name, reason, detail}
    for eid, s in students.items():
        if s["pool"]:
            safe_students[eid] = s
            continue
        if s["dietary_unknown"]:
            reason = STUDENT_DIETARY_UNCONFIRMED
            detail = (
                f"{s['name']} ({s['school_name']}): dietary unconfirmed — confirm "
                f"with the parent before ordering. Never default an unknown-allergy "
                f"child onto a meal."
            )
        else:
            reason = STUDENT_NO_SAFE_MEAL
            detail = (
                f"{s['name']} ({s['school_name']}): dietary requirement but no safe "
                f"meal on this caterer's menu; confirm dietary or extend the menu."
            )
        stragglers.append(
            {
                "enrolment_id": eid,
                "student_name": s["name"],
                "parent_name": s["parent_name"],
                "parent_email": s["parent_email"],
                "school_name": s["school_name"],
                "reason": reason,
                "detail": detail,
            }
        )

    # --- MOQ ceiling, sized against the SAFE order (the meals we'll actually
    # place); stragglers contribute no line, so they don't inflate the budget. ---
    safe_candidates = sum(len(student_sessions[eid]) for eid in safe_students)
    typical_absences = round(safe_candidates * settings.typical_absence_rate)
    expected_orders = max(0, safe_candidates - typical_absences)
    safety_margin = round(safe_candidates * settings.moq_safety_margin)
    vmax_budget = max(0, expected_orders - safety_margin)
    v_max = _vmax(tiers, vmax_budget) if tiers else len(all_items)
    sizing = dict(
        cohort_candidates=cohort_candidates, safe_candidates=safe_candidates,
        no_line_count=len(stragglers), typical_absences=typical_absences,
        safety_margin=safety_margin, expected_orders=expected_orders,
        vmax_budget=vmax_budget, v_max=v_max,
    )

    popularity: dict[int, float] = {}
    offered: set[int] = set()
    ceiling = 0
    escalation: tuple[str, str, dict] | None = None

    # --- Caterer-WIDE escalation: nothing safe to order at all this week. ---
    if not safe_students:
        detail = (
            f"No student has a safe, orderable meal this week — {len(stragglers)} "
            f"need dietary confirmation or a menu fix before any order can be sent."
        )
        escalation = (ESC_COVERAGE_INFEASIBLE, detail, {"escalated_students": stragglers})
    # --- Caterer-WIDE escalation: the safe order can't meet any MOQ floor. ---
    elif tiers and v_max == 0:
        smallest = min(t["min_total_items"] for t in tiers)
        detail = (
            f"The remaining safe order ({vmax_budget} after the safety margin) "
            f"cannot meet the smallest MOQ floor ({smallest}); any order would "
            f"breach the tier. Too few orderable students this week."
        )
        escalation = (ESC_MOQ_BREACH, detail,
                      {"smallest_floor": smallest, "escalated_students": stragglers})
    else:
        ceiling = v_max if tiers else len(all_items)
        # --- Offered set: cover the safe dietary students, then maximize
        # preference. Empty-pool stragglers are excluded — escalated, not covered. ---
        popularity = _rank_weighted_popularity(safe_students)
        dietary_ids = [eid for eid, s in safe_students.items() if s["pool"] != all_items]
        offered, uncovered = _select_offered_set(safe_students, dietary_ids, all_items, popularity, ceiling)
        if uncovered:
            names = [safe_students[e]["name"] for e in uncovered]
            detail = (
                f"No set of <= V_max ({ceiling}) varieties can give every dietary "
                f"student a safe meal; {len(uncovered)} remain uncovered: {', '.join(names)}."
            )
            escalation = (
                ESC_COVERAGE_INFEASIBLE, detail,
                {"uncovered_enrolment_ids": uncovered, "uncovered_students": names,
                 "escalated_students": stragglers},
            )

    return CatererWeekPlan(
        caterer=caterer, week_of=week_of, run_id=run_id, menu=menu, tiers=tiers,
        all_items=all_items, slot_ids=slot_ids, session_dates=session_dates,
        school_names=school_names, num_sessions=len(sessions), gst_rate=gst_rate,
        includes_gst=includes_gst, delivery_fee=delivery_fee, students=students,
        safe_students=safe_students, stragglers=stragglers,
        student_sessions=student_sessions, session_meta=session_meta, recent=recent,
        popularity=popularity, offered=offered, ceiling=ceiling, sizing=sizing,
        base=base, escalation=escalation,
    )


def _assign_one(
    plan: CatererWeekPlan, eid: int, forced: dict[int, int] | None = None
) -> list[tuple[int, int, str]]:
    """Assign one safe student's meal sequence for the plan's week — PURE.

    ``forced`` maps a slot to the meal the student PICKED. A pick is honoured ONLY
    when it is dietary-safe (in the student's pool) AND in the offered set — an
    invalid/stale pick is dropped and the student falls back to rotation / default
    (so a free choice can never breach safety or the MOQ ceiling). Assignment
    priority per session: (a) the student's pick; (b) their rotated eligible
    preference; (c) a tentative safe default. Returns ``[(slot_id, item_id,
    source)]`` in chronological session order; ``source`` is ``request`` for a
    honoured pick, ``rotation`` for a preference pick, ``defaulted_pending_
    confirmation`` for a default.
    """
    s = plan.safe_students[eid]
    session_order = sorted(
        plan.student_sessions[eid], key=lambda slot: (plan.session_meta[slot][0], slot)
    )
    forced = forced or {}
    valid_forced = {
        slot: item
        for slot, item in forced.items()
        if slot in session_order and item in s["pool"] and item in plan.offered
    }

    pref_candidates = [i for i in s["eligible_prefs"] if i in plan.offered]
    if pref_candidates:
        rank = {item: pos for pos, item in enumerate(s["eligible_prefs"])}
        seq = _assign_sequence(
            [(slot, plan.session_meta[slot][0]) for slot in session_order],
            pref_candidates, plan.recent.get(eid, {}), rank, forced=valid_forced,
        )
        base_source = _LINE_SOURCE
    else:
        # No usable preference: a tentative safe default, but honour any pick first.
        default_item = _default_pick(s["pool"] & plan.offered, plan.popularity)
        seq = {
            slot: (valid_forced[slot] if slot in valid_forced else default_item)
            for slot in session_order
        }
        base_source = _DEFAULT_SOURCE

    out: list[tuple[int, int, str]] = []
    for slot in session_order:
        item = seq[slot]
        source = _REQUEST_SOURCE if valid_forced.get(slot) == item else base_source
        out.append((slot, item, source))
    return out


def _compose_caterer_week(caterer: dict, week_of: date, run_id: int | None) -> dict | ToolResult:
    """Compose one caterer's week (or escalate): plan (gather, size V_max, choose
    the offered set), honour each student's weekly PICK over the fallback, rotate
    the rest, persist, summarise."""
    plan = plan_caterer_week(caterer, week_of, run_id)
    if _failed(plan):
        return plan
    if plan.escalation:
        reason, detail, context = plan.escalation
        return _escalate(plan.base, reason, detail, context, plan.sizing)

    caterer_id = caterer["id"]
    menu = plan.menu
    safe_students = plan.safe_students
    offered = plan.offered
    sizing = plan.sizing

    # --- The students' own weekly picks (validated inside _assign_one). ---
    picks = _student_picks(list(plan.students), week_of)
    if _failed(picks):
        return picks

    # --- Assign each safe student a meal SEQUENCE — one meal per rostered session.
    # Priority: (a) the student's PICK if eligible + in the offered set; (b) a
    # rotated eligible preference; (c) a tentative safe default flagged for parent
    # confirmation. Picks are within V_max by construction, so they never push
    # variety past the MOQ ceiling. ---
    lines_by_session: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    student_items: dict[int, list[int]] = {}     # items per student, session order
    student_sources: dict[int, list[str]] = {}   # parallel sources, session order
    for eid in safe_students:
        assigned = _assign_one(plan, eid, picks.get(eid, {}))
        student_items[eid] = [item for _, item, _ in assigned]
        student_sources[eid] = [src for _, _, src in assigned]
        for slot, item, src in assigned:
            lines_by_session[slot].append((eid, item, src))

    # --- Cost + actual variety (one row per student-session line). ---
    assigned = [line for lines in lines_by_session.values() for line in lines]
    assigned_items = [item for _, item, _ in assigned]
    total_items = len(assigned_items)
    variety_count = len(set(assigned_items))
    defaulted_count = sum(1 for _, _, src in assigned if src == _DEFAULT_SOURCE)
    request_count = sum(1 for _, _, src in assigned if src == _REQUEST_SOURCE)
    meal_base = sum(menu[item]["price_cents"] for item in assigned_items)
    delivery_base = plan.delivery_fee * len(lines_by_session)

    moq_min_total = _floor_for_variety(plan.tiers, variety_count) if plan.tiers else None
    shortfall = max(0, (moq_min_total or 0) - total_items)
    # By construction distinct items <= V_max so the floor is met; a shortfall
    # would mean a breach slipped through — refuse to compose and escalate.
    if shortfall > 0:
        detail = (
            f"Composed safe order would breach the MOQ floor ({total_items} meals "
            f"< floor {moq_min_total} at {variety_count} varieties)."
        )
        return _escalate(plan.base, ESC_MOQ_BREACH, detail,
                         {"total_items": total_items, "moq_min_total": moq_min_total,
                          "variety_count": variety_count, "escalated_students": plan.stragglers}, sizing)

    week_total = _gst_normalise(
        meal_base + delivery_base, includes_gst=plan.includes_gst, gst_rate_percent=plan.gst_rate
    )

    # --- Persist (idempotent; writes per-student escalations, clears stale ones). ---
    session_summaries, cwo_id = _persist_caterer_week(
        caterer=caterer, week_of=week_of, run_id=run_id,
        slot_ids=plan.slot_ids, session_dates=plan.session_dates,
        lines_by_session=lines_by_session,
        session_meta=plan.session_meta, menu=menu,
        total_items=total_items, variety_count=variety_count,
        moq_min_total=moq_min_total, total_cost_cents=week_total,
        gst_rate=plan.gst_rate, includes_gst=plan.includes_gst, delivery_fee=plan.delivery_fee,
        stragglers=plan.stragglers,
    )
    if _failed(session_summaries):
        return session_summaries

    # Meal-by-meal breakdown for the order email: distinct item -> total qty,
    # most-ordered first (ties by name for a stable, readable order).
    item_counts: dict[int, int] = defaultdict(int)
    for item in assigned_items:
        item_counts[item] += 1
    meal_breakdown = [
        {"menu_item_id": item, "item": menu[item]["name"], "quantity": qty}
        for item, qty in sorted(
            item_counts.items(), key=lambda kv: (-kv[1], menu[kv[0]]["name"])
        )
    ]

    # Every defaulted line in full (parent contact included) so the agent can
    # email each parent; ordered by student name for a stable read. A student is
    # "defaulted" when ANY of their session lines is a tentative default (the first
    # such item is the one the prefs follow-up references).
    def _first_default_item(eid: int) -> str:
        for item, src in zip(student_items[eid], student_sources[eid]):
            if src == _DEFAULT_SOURCE:
                return menu[item]["name"]
        return ""

    defaulted_eids = sorted(
        {eid for eid in safe_students if _DEFAULT_SOURCE in student_sources[eid]},
        key=lambda e: safe_students[e]["name"],
    )
    defaulted_lines = [
        {
            "enrolment_id": eid,
            "student_name": safe_students[eid]["name"],
            "parent_name": safe_students[eid]["parent_name"],
            "parent_email": safe_students[eid]["parent_email"],
            "school_name": safe_students[eid]["school_name"],
            "item": _first_default_item(eid),
        }
        for eid in defaulted_eids
    ]

    summary = CatererWeekSummary(
        caterer_id=caterer_id,
        caterer_name=caterer["name"],
        caterer_contact_email=caterer.get("contact_email"),
        schools=[plan.school_names[sid] for sid in sorted(plan.school_names)],
        week_of=week_of.isoformat(),
        num_sessions=plan.num_sessions,
        status="composed",
        offered_items=[menu[i]["name"] for i in sorted(offered, key=lambda i: (-plan.popularity.get(i, 0.0), i))],
        offered_count=len(offered),
        variety_count=variety_count,
        total_items=total_items,
        defaulted_count=defaulted_count,
        moq_min_total=moq_min_total,
        moq_floor_applied=False,
        moq_variance_cents=0,
        total_cost_cents=week_total,
        gst_rate_percent=plan.gst_rate,
        price_includes_gst=plan.includes_gst,
        caterer_week_orders_id=cwo_id,
        meal_breakdown=meal_breakdown,
        sessions=session_summaries,
        rotation_sample=_rotation_sample(safe_students, student_items, plan.recent, menu),
        defaulted_lines=defaulted_lines,
        escalated_students=plan.stragglers,
        request_count=request_count,
        **sizing,
    )
    return summary.as_dict()


def _clear_open_escalations(cur: psycopg.Cursor, caterer_id: int, week_of: date) -> None:
    """Remove our own prior open escalations (caterer-wide AND per-student) for
    this (caterer, week), so a re-run replaces rather than duplicates and flips
    cleanly between the composed and escalated states."""
    cur.execute(
        """
        DELETE FROM escalations
        WHERE related_caterer_id = %s AND status = 'open'
          AND context->>'week_of' = %s
          AND context->>'kind' = ANY(%s)
        """,
        (caterer_id, week_of.isoformat(), [_ESC_KIND, _ESC_STUDENT_KIND]),
    )


def _escalate(
    base: dict, reason: str, detail: str, context: dict, sizing: dict
) -> dict | ToolResult:
    """Caterer-WIDE escalation: compose nothing sendable, raise (idempotently
    replace) one escalation naming the specifics, and clear any orders previously
    composed for this week plus any prior per-student escalations.

    Returns an ``escalated`` summary dict, or a typed failure if the DB is down.
    """
    caterer = base["caterer"]
    caterer_id = caterer["id"]
    week_of = base["week_of"]
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # A previously composed order for this week is no longer sendable.
            cur.execute(
                """
                DELETE FROM order_lines ol USING orders o
                WHERE ol.order_id = o.id AND o.caterer_id = %s
                  AND o.session_slot_id = ANY(%s) AND o.session_date = ANY(%s)
                """,
                (caterer_id, base["slot_ids"], base["session_dates"]),
            )
            cur.execute(
                """
                DELETE FROM orders WHERE caterer_id = %s
                  AND session_slot_id = ANY(%s) AND session_date = ANY(%s)
                """,
                (caterer_id, base["slot_ids"], base["session_dates"]),
            )
            cur.execute(
                "DELETE FROM caterer_week_orders WHERE caterer_id = %s AND week_of = %s",
                (caterer_id, week_of),
            )
            _clear_open_escalations(cur, caterer_id, week_of)
            ctx = {"kind": _ESC_KIND, "week_of": week_of.isoformat(), "reason": reason, **context}
            cur.execute(
                """
                INSERT INTO escalations (run_id, question, context, status, related_caterer_id)
                VALUES (%s, %s, %s, 'open', %s)
                RETURNING id
                """,
                (base["run_id"], detail, Jsonb(ctx), caterer_id),
            )
            esc_id = cur.fetchone()["id"]
            conn.commit()
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while escalating caterer {caterer_id}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while escalating caterer {caterer_id}: {exc}")

    summary = CatererWeekSummary(
        caterer_id=caterer_id,
        caterer_name=caterer["name"],
        caterer_contact_email=caterer.get("contact_email"),
        schools=[base["school_names"][sid] for sid in sorted(base["school_names"])],
        week_of=week_of.isoformat(),
        num_sessions=base["num_sessions"],
        status="escalated",
        offered_items=[],
        offered_count=0,
        variety_count=0,
        total_items=0,
        defaulted_count=0,
        moq_min_total=None,
        moq_floor_applied=False,
        moq_variance_cents=0,
        total_cost_cents=0,
        gst_rate_percent=base["gst_rate"],
        price_includes_gst=base["includes_gst"],
        caterer_week_orders_id=None,
        escalation_id=esc_id,
        escalation_reason=reason,
        escalation_detail=detail,
        escalated_students=context.get("escalated_students", []),
        **sizing,
    )
    return summary.as_dict()


def _persist_caterer_week(
    *, caterer, week_of, run_id, slot_ids, session_dates,
    lines_by_session, session_meta, menu,
    total_items, variety_count, moq_min_total, total_cost_cents,
    gst_rate, includes_gst, delivery_fee, stragglers,
) -> tuple[list[SessionSummary], int | None] | ToolResult:
    """Write orders + order_lines + caterer_week_orders + per-student straggler
    escalations in one transaction.

    Idempotent: deletes any existing orders (and their lines) for this caterer's
    session slots on the week's dates, the prior ``caterer_week_orders`` row for
    (caterer, week), and ALL our prior open escalations (caterer-wide and
    per-student) for the week, then re-inserts. ``lines_by_session`` maps a slot to
    ``[(enrolment_id, menu_item_id, source)]`` and each line carries its OWN source
    (``request`` for a student pick, ``rotation``, or ``defaulted_pending_
    confirmation``). The MOQ floor is met by construction, so floor/variance are
    recorded as not applied. Mutates each straggler dict with the ``escalation_id``
    raised for it. Returns the per-session summaries and the new
    ``caterer_week_orders`` id.
    """
    caterer_id = caterer["id"]

    def work(cur: psycopg.Cursor) -> tuple[list[SessionSummary], int]:
        # --- Clear this caterer's week (children before parents) + escalations. ---
        cur.execute(
            """
            DELETE FROM order_lines ol
            USING orders o
            WHERE ol.order_id = o.id
              AND o.caterer_id = %s
              AND o.session_slot_id = ANY(%s)
              AND o.session_date = ANY(%s)
            """,
            (caterer_id, slot_ids, session_dates),
        )
        cur.execute(
            """
            DELETE FROM orders
            WHERE caterer_id = %s
              AND session_slot_id = ANY(%s)
              AND session_date = ANY(%s)
            """,
            (caterer_id, slot_ids, session_dates),
        )
        cur.execute(
            "DELETE FROM caterer_week_orders WHERE caterer_id = %s AND week_of = %s",
            (caterer_id, week_of),
        )
        _clear_open_escalations(cur, caterer_id, week_of)

        # --- Per-session orders + lines (each line keeps its own source). ---
        summaries: list[SessionSummary] = []
        for slot_id, lines in sorted(lines_by_session.items()):
            s_date, school_name = session_meta[slot_id]
            meal_base = sum(menu[item]["price_cents"] for _, item, _ in lines)
            session_cost = _gst_normalise(
                meal_base + delivery_fee,
                includes_gst=includes_gst, gst_rate_percent=gst_rate,
            )
            cur.execute(
                """
                INSERT INTO orders
                    (session_slot_id, caterer_id, session_date, total_items,
                     total_cost_cents, gst_rate_percent)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (slot_id, caterer_id, s_date, len(lines), session_cost, gst_rate),
            )
            order_id = cur.fetchone()["id"]
            cur.executemany(
                """
                INSERT INTO order_lines (order_id, enrolment_id, menu_item_id, source)
                VALUES (%s, %s, %s, %s)
                """,
                [(order_id, eid, item, source) for eid, item, source in lines],
            )
            summaries.append(
                SessionSummary(
                    session_slot_id=slot_id,
                    school_name=school_name,
                    session_date=s_date.isoformat(),
                    total_items=len(lines),
                    total_cost_cents=session_cost,
                )
            )

        # --- Weekly summary row (floor met by construction). ---
        cur.execute(
            """
            INSERT INTO caterer_week_orders
                (caterer_id, week_of, run_id, total_items, variety_count,
                 moq_min_total, moq_floor_applied, moq_variance_cents,
                 total_cost_cents, gst_rate_percent)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE, 0, %s, %s)
            RETURNING id
            """,
            (caterer_id, week_of, run_id, total_items, variety_count,
             moq_min_total, total_cost_cents, gst_rate),
        )
        cwo_id = cur.fetchone()["id"]

        # --- Per-student escalations for the stragglers (no line composed). ---
        for st in stragglers:
            ctx = {
                "kind": _ESC_STUDENT_KIND, "week_of": week_of.isoformat(),
                "reason": st["reason"], "enrolment_id": st["enrolment_id"],
                "student_name": st["student_name"],
            }
            cur.execute(
                """
                INSERT INTO escalations
                    (run_id, question, context, status, related_caterer_id, related_enrolment_id)
                VALUES (%s, %s, %s, 'open', %s, %s)
                RETURNING id
                """,
                (run_id, st["detail"], Jsonb(ctx), caterer_id, st["enrolment_id"]),
            )
            st["escalation_id"] = cur.fetchone()["id"]
        return summaries, cwo_id

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            result = work(cur)
            conn.commit()
            return result
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while persisting caterer {caterer_id}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while persisting caterer {caterer_id}: {exc}")


# --- Public helpers for the student choose-and-rate flow ---------------------
# These read the SAME plan compose_week uses, so the options a student is offered
# (and the pick-vs-fallback preview) are computed by exactly one algorithm.


def plan_caterer_week_by_id(
    caterer_id: int, week_of: date, run_id: int | None = None,
    cache: dict | None = None,
) -> CatererWeekPlan | ToolResult:
    """``plan_caterer_week`` keyed by caterer id (loads the caterer first). Returns
    the plan, or a typed failure (``error`` if no such active caterer).

    ``cache`` is an OPTIONAL caller-owned dict memoising the (read-only, per-week
    deterministic) plan by ``(caterer_id, week)`` — pass one when resolving many
    students of the same caterer-week in a single operation (e.g. a weekly choice
    send) so the caterer-week is gathered once, not once per student. It is scoped
    to the caller (never a module global), so it can't go stale across the worker's
    separate invocations."""
    week_of = monday_of_week(week_of)
    key = (caterer_id, week_of.isoformat())
    if cache is not None and key in cache:
        return cache[key]
    caterers = _caterers(caterer_id)
    if _failed(caterers):
        return caterers
    if not caterers:
        return error(f"No active caterer {caterer_id} currently serving a school.")
    plan = plan_caterer_week(caterers[0], week_of, run_id)
    if cache is not None and not _failed(plan):
        cache[key] = plan
    return plan


def assign_student(
    plan: CatererWeekPlan, enrolment_id: int, forced_pick: int | None = None
) -> list[tuple[int, int, str]] | None:
    """Preview one safe student's assignment for the plan's week — PURE, no writes.

    ``forced_pick`` (a menu_item_id) is applied to the student's NEXT upcoming
    session this week — the same session the choose-and-rate email asks them to
    pick for — and is honoured only when dietary-safe AND in the offered set (else
    the student falls back, exactly as ``compose_week`` would). Returns
    ``[(slot_id, item_id, source)]`` in session order, or ``None`` when the student
    isn't a safe, orderable member of this caterer-week (escalated / not rostered).
    """
    if enrolment_id not in plan.safe_students:
        return None
    forced: dict[int, int] = {}
    if forced_pick is not None:
        order = sorted(
            plan.student_sessions[enrolment_id],
            key=lambda slot: (plan.session_meta[slot][0], slot),
        )
        if order:
            forced = {order[0]: forced_pick}
    return _assign_one(plan, enrolment_id, forced)
