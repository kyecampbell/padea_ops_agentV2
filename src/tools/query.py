"""Read tools.

Responsibility: provide read-only access to operational data (schools, caterers,
students, dietary needs, meal preferences, orders, finances). Every function
returns a typed result from `results.py` (found / empty / ambiguous / conflict /
unavailable / error) and never raises at the agent.

Conventions:
  - Money is returned as integer cents.
  - Timestamps are timezone-aware.
  - Read-only: SELECT only, no writes.
"""

from __future__ import annotations

from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.results import ToolResult, error, empty, found, unavailable

# --- DB access wrapper -------------------------------------------------------


def _fetch(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """Run a SELECT and return rows as dicts. Raises only psycopg errors,
    which callers translate into typed results via `_run`.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _run(describe: str, sql: str, params: Sequence[Any] | None = None) -> list[dict] | ToolResult:
    """Execute a query, translating any psycopg failure into a typed result.

    Returns the list of rows on success, or a `unavailable`/`error` ToolResult
    that the caller should return as-is. `describe` names the read for messages.

    - OperationalError (connection/transport down) -> `unavailable`.
    - Any other psycopg error (bad SQL, type, etc.) -> `error`.
    """
    try:
        return _fetch(sql, params)
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


def _failed(rows: list[dict] | ToolResult) -> bool:
    """True when `_run` returned a failure result rather than rows."""
    return isinstance(rows, ToolResult)


# --- Enrolments --------------------------------------------------------------


def get_enrolment(enrolment_id: int) -> ToolResult:
    """One enrolment by id, including dietary_raw and its dietary tag names."""
    rows = _run(
        f"fetching enrolment {enrolment_id}",
        """
        SELECT id, school_id, student_name, student_year_level,
               parent_name, parent_email, parent_phone,
               original_start_date, current_period_start_date,
               current_period_end_date, opted_out_of_catering, dietary_raw
        FROM enrolments
        WHERE id = %s
        """,
        (enrolment_id,),
    )
    if _failed(rows):
        return rows
    if not rows:
        return empty(f"No enrolment with id {enrolment_id}.")

    enrolment = rows[0]

    tags = _run(
        f"fetching dietary tags for enrolment {enrolment_id}",
        """
        SELECT dt.name
        FROM enrolment_dietary_tags edt
        JOIN dietary_tags dt ON dt.id = edt.dietary_tag_id
        WHERE edt.enrolment_id = %s
        ORDER BY dt.name
        """,
        (enrolment_id,),
    )
    if _failed(tags):
        return tags

    enrolment["dietary_tag_names"] = [t["name"] for t in tags]
    return found(enrolment, f"Enrolment {enrolment_id}: {enrolment['student_name']}.")


def list_active_enrolments(school_id: int) -> ToolResult:
    """Active enrolments at a school: not opted out of catering and within the
    current enrolment period — the period has started and has no end date yet,
    or its end date is still in the future. (A NULL end date means open-ended /
    still enrolled.) Each row carries its dietary_raw and dietary tag names so
    the eligible-pool tool can use them.
    """
    rows = _run(
        f"listing active enrolments for school {school_id}",
        """
        SELECT e.id, e.school_id, e.student_name, e.student_year_level,
               e.parent_name, e.parent_email, e.dietary_raw,
               COALESCE(
                   array_agg(dt.name ORDER BY dt.name)
                   FILTER (WHERE dt.name IS NOT NULL),
                   '{}'
               ) AS dietary_tag_names
        FROM enrolments e
        LEFT JOIN enrolment_dietary_tags edt ON edt.enrolment_id = e.id
        LEFT JOIN dietary_tags dt ON dt.id = edt.dietary_tag_id
        WHERE e.school_id = %s
          AND e.opted_out_of_catering = FALSE
          AND e.current_period_start_date <= CURRENT_DATE
          AND (e.current_period_end_date IS NULL
               OR e.current_period_end_date >= CURRENT_DATE)
        GROUP BY e.id
        ORDER BY e.student_name
        """,
        (school_id,),
    )
    if _failed(rows):
        return rows
    if not rows:
        return empty(f"No active enrolments for school {school_id}.")
    # array_agg yields a list already; normalise to plain lists.
    for r in rows:
        r["dietary_tag_names"] = list(r["dietary_tag_names"])
    return found(rows, f"{len(rows)} active enrolment(s) at school {school_id}.")


# --- Caterers ----------------------------------------------------------------


def get_caterer(caterer_id: int) -> ToolResult:
    """One caterer by id."""
    rows = _run(
        f"fetching caterer {caterer_id}",
        """
        SELECT id, name, contact_email, contact_phone, home_postcode,
               max_delivery_km, delivery_fee_cents, price_includes_gst,
               gst_rate_percent
        FROM caterers
        WHERE id = %s
        """,
        (caterer_id,),
    )
    if _failed(rows):
        return rows
    if not rows:
        return empty(f"No caterer with id {caterer_id}.")
    return found(rows[0], f"Caterer {caterer_id}: {rows[0]['name']}.")


def get_caterer_for_school(school_id: int) -> ToolResult:
    """The caterer currently assigned to a school (via schools.current_caterer_id)."""
    rows = _run(
        f"fetching caterer for school {school_id}",
        """
        SELECT c.id, c.name, c.contact_email, c.contact_phone, c.home_postcode,
               c.max_delivery_km, c.delivery_fee_cents, c.price_includes_gst,
               c.gst_rate_percent, s.id AS school_id, s.name AS school_name
        FROM schools s
        JOIN caterers c ON c.id = s.current_caterer_id
        WHERE s.id = %s
        """,
        (school_id,),
    )
    if _failed(rows):
        return rows
    if not rows:
        return empty(f"No caterer assigned to school {school_id}.")
    return found(rows[0], f"School {school_id} is catered by {rows[0]['name']}.")


def get_caterer_feedback(caterer_id: int, weeks: int = 4) -> ToolResult:
    """A caterer's recent feedback over the last ``weeks`` weeks, shaped so the
    agent can judge the TREND (improving / stable / declining), not just a snapshot.

    Returns, for the window (``submitted_at`` within the last ``weeks`` * 7 days):
      - ``overall``: total feedback count, count with a rating, and the mean rating;
      - ``by_week``: per-week count, mean rating, and min rating (oldest → newest)
        so a decline shows as a falling weekly mean — the pattern the policy keys on;
      - ``comments``: every rated row that carried a free-text comment (the manager's
        own words: late / cold / wrong / dietary), newest first;
      - ``manager_checklist_issues``: per checklist question, how many times the
        manager answered "no" (a failed quality check) in the window.

    Read-only. A caterer with no feedback in the window returns ``empty``.
    """
    weeks = max(1, int(weeks))

    caterer = _run(
        f"fetching caterer {caterer_id} for feedback",
        "SELECT id, name FROM caterers WHERE id = %s",
        (caterer_id,),
    )
    if _failed(caterer):
        return caterer
    if not caterer:
        return empty(f"No caterer with id {caterer_id}.")
    caterer_name = caterer[0]["name"]

    overall = _run(
        f"summarising feedback for caterer {caterer_id}",
        """
        SELECT count(*)                              AS total_feedback,
               count(rating)                         AS rated_count,
               round(avg(rating)::numeric, 2)::float AS avg_rating,
               min(rating)                           AS min_rating,
               min(submitted_at)                     AS earliest,
               max(submitted_at)                     AS latest
        FROM feedback
        WHERE caterer_id = %s
          AND submitted_at >= now() - make_interval(weeks => %s)
        """,
        (caterer_id, weeks),
    )
    if _failed(overall):
        return overall
    summary = overall[0]
    if not summary["total_feedback"]:
        return empty(
            f"No feedback for caterer {caterer_id} ({caterer_name}) in the last {weeks} week(s)."
        )

    by_week = _run(
        f"weekly feedback trend for caterer {caterer_id}",
        """
        SELECT date_trunc('week', submitted_at)::date    AS week_starting,
               count(*)                                   AS count,
               count(rating)                              AS rated_count,
               round(avg(rating)::numeric, 2)::float      AS avg_rating,
               min(rating)                                AS min_rating
        FROM feedback
        WHERE caterer_id = %s
          AND submitted_at >= now() - make_interval(weeks => %s)
        GROUP BY week_starting
        ORDER BY week_starting
        """,
        (caterer_id, weeks),
    )
    if _failed(by_week):
        return by_week

    comments = _run(
        f"feedback comments for caterer {caterer_id}",
        """
        SELECT submitted_at, source, rating, comment
        FROM feedback
        WHERE caterer_id = %s
          AND comment IS NOT NULL AND btrim(comment) <> ''
          AND submitted_at >= now() - make_interval(weeks => %s)
        ORDER BY submitted_at DESC
        """,
        (caterer_id, weeks),
    )
    if _failed(comments):
        return comments

    checklist = _run(
        f"manager checklist failures for caterer {caterer_id}",
        """
        SELECT ci.code, ci.prompt, count(*) AS failed_count
        FROM feedback_checklist_response r
        JOIN feedback f      ON f.id = r.feedback_id
        JOIN checklist_item ci ON ci.id = r.checklist_item_id
        WHERE f.caterer_id = %s
          AND r.value_bool = FALSE
          AND f.submitted_at >= now() - make_interval(weeks => %s)
        GROUP BY ci.code, ci.prompt
        ORDER BY failed_count DESC, ci.code
        """,
        (caterer_id, weeks),
    )
    if _failed(checklist):
        return checklist

    data = {
        "caterer_id": caterer_id,
        "caterer_name": caterer_name,
        "weeks": weeks,
        "overall": dict(summary),
        "by_week": list(by_week),
        "comments": list(comments),
        "manager_checklist_issues": list(checklist),
    }
    avg = summary["avg_rating"]
    return found(
        data,
        f"Caterer {caterer_id} ({caterer_name}): {summary['total_feedback']} feedback "
        f"row(s) over {weeks} week(s), mean rating {avg}; "
        f"{len(comments)} comment(s), {len(checklist)} checklist issue type(s).",
    )


def get_caterer_moq_tiers(caterer_id: int) -> ToolResult:
    """A caterer's minimum-order-quantity tiers (variety_count -> min_total_items)."""
    rows = _run(
        f"fetching MOQ tiers for caterer {caterer_id}",
        """
        SELECT caterer_id, variety_count, min_total_items
        FROM caterer_moq_tier
        WHERE caterer_id = %s
        ORDER BY variety_count
        """,
        (caterer_id,),
    )
    if _failed(rows):
        return rows
    if not rows:
        return empty(f"No MOQ tiers for caterer {caterer_id}.")
    return found(rows, f"{len(rows)} MOQ tier(s) for caterer {caterer_id}.")


# --- Menu --------------------------------------------------------------------


def get_menu_items(caterer_id: int) -> ToolResult:
    """A caterer's active menu items, each with the dietary tags it satisfies.

    Returns id, name, contents_text, tweaks_text, price_cents (integer cents),
    and dietary_tag_names — the tags this item is certified to satisfy.
    """
    rows = _run(
        f"fetching menu items for caterer {caterer_id}",
        """
        SELECT mi.id, mi.name, mi.contents_text, mi.tweaks_text, mi.price_cents,
               COALESCE(
                   array_agg(dt.name ORDER BY dt.name)
                   FILTER (WHERE dt.name IS NOT NULL),
                   '{}'
               ) AS dietary_tag_names
        FROM menu_items mi
        LEFT JOIN menu_item_dietary_tags midt ON midt.menu_item_id = mi.id
        LEFT JOIN dietary_tags dt ON dt.id = midt.dietary_tag_id
        WHERE mi.caterer_id = %s
          AND mi.active = TRUE
        GROUP BY mi.id
        ORDER BY mi.name
        """,
        (caterer_id,),
    )
    if _failed(rows):
        return rows
    if not rows:
        return empty(f"No active menu items for caterer {caterer_id}.")
    for r in rows:
        r["dietary_tag_names"] = list(r["dietary_tag_names"])
    return found(rows, f"{len(rows)} active menu item(s) for caterer {caterer_id}.")


# --- Dietary tags ------------------------------------------------------------


def get_all_dietary_tags() -> ToolResult:
    """All active dietary tags (id, name, label, description)."""
    rows = _run(
        "listing dietary tags",
        """
        SELECT id, name, label, description
        FROM dietary_tags
        WHERE active = TRUE
        ORDER BY name
        """,
    )
    if _failed(rows):
        return rows
    if not rows:
        return empty("No dietary tags defined.")
    return found(rows, f"{len(rows)} dietary tag(s).")
