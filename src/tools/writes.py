"""Data-write tools.

Responsibility: perform validated, logged writes to operational data — DATA only,
never DDL. Each write validates its inputs, records an audit log line, and returns
a typed ``ToolResult``; it never raises at the agent. Writes the hard-rules gate
marks ``requires_approval`` are NOT blocked here — enforcement (queue-and-wait) is
wired with the approval UI; this layer just performs the write when asked.

Conventions:
  - Money is integer cents; reject floats. (None of these tools touch money yet.)
  - Timestamps are timezone-aware (DB ``now()`` / ``timestamptz``).
  - All SQL is parameterised — never string-built.
  - No schema changes here — migrations live under database/migrations/.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

import psycopg
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.results import ToolResult, conflict, error, found, unavailable

logger = logging.getLogger(__name__)


# --- DB helpers --------------------------------------------------------------


def _read(describe: str, sql: str, params: Sequence[Any] | None = None) -> list[dict] | ToolResult:
    """Run a validation SELECT; translate any psycopg failure into a typed result."""
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


def _transaction(describe: str, work: Callable[[psycopg.Cursor], Any]) -> Any | ToolResult:
    """Run ``work(cur)`` inside one committed transaction; translate failures.

    Returns whatever ``work`` returns on success, or an ``unavailable`` / ``error``
    ToolResult on a DB failure (the transaction is rolled back on exit). This is
    how the write tools keep their "never raise at the caller" contract.
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            result = work(cur)
            conn.commit()
            return result
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


def _failed(value: Any) -> bool:
    """True when a helper returned a failure ToolResult rather than data."""
    return isinstance(value, ToolResult)


# --- Term meal preference ----------------------------------------------------


def update_term_meal_preference(enrolment_id: int, ranked_menu_item_ids: Sequence[int]) -> ToolResult:
    """Replace a student's ranked term meal-preference items.

    Each item must be on the student's current caterer's menu AND dietary-eligible
    for the student (``student_eligible_meals.eligible``). If any item fails either
    check the whole change is rejected (``conflict``) — we never set a preference to
    an unsafe or off-menu item. ``ranked_menu_item_ids`` is highest-preference
    first; its position becomes the stored ``rank`` (1 = top).

    On success replaces the items under the student's current (non-superseded)
    preference set for that caterer, creating one if none exists.
    """
    # --- Validate the input list. ---
    try:
        ranked = [int(x) for x in ranked_menu_item_ids]
    except (TypeError, ValueError):
        return error("ranked_menu_item_ids must be a list of integer menu item ids.")
    if not ranked:
        return error("ranked_menu_item_ids is empty; provide at least one menu item.")
    if len(set(ranked)) != len(ranked):
        return error(f"ranked_menu_item_ids contains duplicate ids: {ranked}.")

    # --- Resolve the student's current caterer. ---
    who = _read(
        f"resolving caterer for enrolment {enrolment_id}",
        """
        SELECT e.id, s.current_caterer_id AS caterer_id
        FROM enrolments e
        JOIN schools s ON s.id = e.school_id
        WHERE e.id = %s
        """,
        (enrolment_id,),
    )
    if _failed(who):
        return who
    if not who:
        return conflict(f"No enrolment with id {enrolment_id}; cannot set preference.")
    caterer_id = who[0]["caterer_id"]
    if caterer_id is None:
        return conflict(f"Enrolment {enrolment_id}'s school has no caterer assigned.")

    # --- Validate every item: on the caterer's menu AND dietary-eligible. ---
    checks = _read(
        f"validating menu items for enrolment {enrolment_id}",
        """
        SELECT mi.id,
               (mi.caterer_id = %s)              AS at_caterer,
               COALESCE(sem.eligible, FALSE)     AS eligible
        FROM menu_items mi
        LEFT JOIN student_eligible_meals sem
               ON sem.menu_item_id = mi.id AND sem.enrolment_id = %s
        WHERE mi.id = ANY(%s)
        """,
        (caterer_id, enrolment_id, ranked),
    )
    if _failed(checks):
        return checks

    by_id = {r["id"]: r for r in checks}
    unknown = [i for i in ranked if i not in by_id]
    wrong_caterer = [i for i in ranked if i in by_id and not by_id[i]["at_caterer"]]
    ineligible = [
        i for i in ranked if i in by_id and by_id[i]["at_caterer"] and not by_id[i]["eligible"]
    ]
    problems: list[str] = []
    if unknown:
        problems.append(f"unknown menu item id(s) {unknown}")
    if wrong_caterer:
        problems.append(f"item(s) {wrong_caterer} are not on caterer {caterer_id}'s menu")
    if ineligible:
        problems.append(f"item(s) {ineligible} are not dietary-eligible for this student")
    if problems:
        return conflict(
            f"Cannot set preference for enrolment {enrolment_id}: " + "; ".join(problems) + ".",
            data={"unknown": unknown, "wrong_caterer": wrong_caterer, "ineligible": ineligible},
        )

    # --- Replace the items under the current preference set (create if absent). ---
    def work(cur: psycopg.Cursor) -> int:
        cur.execute(
            """
            SELECT id FROM term_meal_preferences
            WHERE enrolment_id = %s AND caterer_id = %s AND superseded_at IS NULL
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (enrolment_id, caterer_id),
        )
        row = cur.fetchone()
        if row:
            preference_id = row["id"]
        else:
            cur.execute(
                """
                INSERT INTO term_meal_preferences (enrolment_id, caterer_id, captured_by)
                VALUES (%s, %s, 'operator')
                RETURNING id
                """,
                (enrolment_id, caterer_id),
            )
            preference_id = cur.fetchone()["id"]

        cur.execute(
            "DELETE FROM term_meal_preference_items WHERE preference_id = %s",
            (preference_id,),
        )
        cur.executemany(
            """
            INSERT INTO term_meal_preference_items (preference_id, menu_item_id, rank)
            VALUES (%s, %s, %s)
            """,
            [(preference_id, item_id, rank) for rank, item_id in enumerate(ranked, start=1)],
        )
        return preference_id

    preference_id = _transaction(
        f"replacing preference items for enrolment {enrolment_id}", work
    )
    if _failed(preference_id):
        return preference_id

    logger.info(
        "update_term_meal_preference: enrolment=%s caterer=%s preference=%s items=%s",
        enrolment_id,
        caterer_id,
        preference_id,
        ranked,
    )
    return found(
        {
            "enrolment_id": enrolment_id,
            "caterer_id": caterer_id,
            "preference_id": preference_id,
            "ranked_menu_item_ids": ranked,
            "item_count": len(ranked),
        },
        f"Set {len(ranked)} ranked preference item(s) for enrolment {enrolment_id}.",
    )


# --- Dietary update ----------------------------------------------------------


def record_dietary_update(enrolment_id: int, new_dietary_raw: str) -> ToolResult:
    """Update a student's raw dietary note (``enrolments.dietary_raw``).

    This records the new verbatim note only; the caller is responsible for
    re-running the eligible-pool computation afterwards so derived tags and
    ``student_eligible_meals`` reflect the change.
    """
    if new_dietary_raw is None:
        return error("new_dietary_raw must be a string (use '' to clear the note).")
    text = str(new_dietary_raw)

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            """
            UPDATE enrolments
            SET dietary_raw = %s
            WHERE id = %s
            RETURNING id, dietary_raw
            """,
            (text, enrolment_id),
        )
        return cur.fetchone()

    row = _transaction(f"updating dietary note for enrolment {enrolment_id}", work)
    if _failed(row):
        return row
    if row is None:
        return conflict(f"No enrolment with id {enrolment_id}; cannot update dietary note.")

    logger.info("record_dietary_update: enrolment=%s dietary_raw=%r", enrolment_id, text)
    return found(
        {"enrolment_id": enrolment_id, "dietary_raw": row["dietary_raw"]},
        f"Updated dietary note for enrolment {enrolment_id}.",
    )


# --- Add enrolment -----------------------------------------------------------


def add_enrolment(
    school_id: int,
    student_name: str,
    parent_name: str,
    parent_email: str,
    year_level: int | None = None,
    dietary_raw: str | None = None,
) -> ToolResult:
    """Insert a new enrolment (student-at-school) and return its new id.

    Start dates default to the current date (the enrolment begins today). The
    school must exist. Adding a student is always a ``requires_approval`` action
    (billing + identity) — see ``src.agent.gates``.
    """
    if not (student_name or "").strip():
        return error("student_name is required.")
    if not (parent_name or "").strip():
        return error("parent_name is required.")
    if not (parent_email or "").strip():
        return error("parent_email is required.")
    if year_level is not None:
        try:
            year_level = int(year_level)
        except (TypeError, ValueError):
            return error("year_level must be an integer or omitted.")

    # Validate the school exists (a clean conflict beats a raw FK violation).
    school = _read(f"checking school {school_id}", "SELECT id FROM schools WHERE id = %s", (school_id,))
    if _failed(school):
        return school
    if not school:
        return conflict(f"No school with id {school_id}; cannot add enrolment.")

    def work(cur: psycopg.Cursor) -> int:
        cur.execute(
            """
            INSERT INTO enrolments
                (school_id, student_name, student_year_level, parent_name,
                 parent_email, dietary_raw, original_start_date, current_period_start_date)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE, CURRENT_DATE)
            RETURNING id
            """,
            (school_id, student_name.strip(), year_level, parent_name.strip(),
             parent_email.strip(), dietary_raw),
        )
        return cur.fetchone()["id"]

    new_id = _transaction(f"adding enrolment at school {school_id}", work)
    if _failed(new_id):
        return new_id

    logger.info(
        "add_enrolment: id=%s school=%s student=%r parent_email=%r",
        new_id,
        school_id,
        student_name,
        parent_email,
    )
    return found(
        {"enrolment_id": new_id, "school_id": school_id, "student_name": student_name.strip()},
        f"Added enrolment {new_id} ({student_name.strip()}) at school {school_id}.",
    )


# --- Menu item description ----------------------------------------------------


def update_menu_item_description(
    menu_item_id: int,
    contents_text: str | None = None,
    tweaks_text: str | None = None,
) -> ToolResult:
    """Record a caterer's authoritative clarification of a menu item's text.

    Updates ``contents_text`` and/or ``tweaks_text`` — only the argument(s)
    supplied (a ``None`` arg leaves that column untouched). This is how the agent
    captures a caterer's factual reply before recomputing the eligible pool, so
    it is an autonomous action (see ``src.agent.gates``) but is logged so it
    surfaces in the decision feed.
    """
    sets: list[str] = []
    params: list[Any] = []
    if contents_text is not None:
        sets.append("contents_text = %s")
        params.append(str(contents_text))
    if tweaks_text is not None:
        sets.append("tweaks_text = %s")
        params.append(str(tweaks_text))
    if not sets:
        return error("Provide contents_text and/or tweaks_text to update.")
    params.append(menu_item_id)

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            f"""
            UPDATE menu_items
            SET {', '.join(sets)}
            WHERE id = %s
            RETURNING id, contents_text, tweaks_text
            """,
            params,
        )
        return cur.fetchone()

    row = _transaction(f"updating menu item {menu_item_id} description", work)
    if _failed(row):
        return row
    if row is None:
        return conflict(f"No menu item with id {menu_item_id}; cannot update description.")

    logger.info(
        "update_menu_item_description: id=%s fields=%s",
        menu_item_id,
        [s.split(" =")[0] for s in sets],
    )
    return found(
        {
            "menu_item_id": menu_item_id,
            "contents_text": row["contents_text"],
            "tweaks_text": row["tweaks_text"],
        },
        f"Updated description for menu item {menu_item_id}.",
    )


# --- Resolve escalation ------------------------------------------------------


def resolve_escalation(escalation_id: int, resolution: str, resolved_by: str) -> ToolResult:
    """Mark an escalation resolved with a resolution note and resolver.

    Sets ``status = 'resolved'``, the resolution text, ``resolved_by`` and
    ``resolved_at = now()``. Resolving is always a ``requires_approval`` action.
    """
    if not (resolution or "").strip():
        return error("resolution is required.")
    if not (resolved_by or "").strip():
        return error("resolved_by is required.")

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            """
            UPDATE escalations
            SET status = 'resolved',
                resolution = %s,
                resolved_by = %s,
                resolved_at = now()
            WHERE id = %s
            RETURNING id, status, resolved_at
            """,
            (resolution.strip(), resolved_by.strip(), escalation_id),
        )
        return cur.fetchone()

    row = _transaction(f"resolving escalation {escalation_id}", work)
    if _failed(row):
        return row
    if row is None:
        return conflict(f"No escalation with id {escalation_id}; nothing to resolve.")

    logger.info(
        "resolve_escalation: id=%s resolved_by=%r", escalation_id, resolved_by
    )
    return found(
        {"escalation_id": escalation_id, "status": row["status"], "resolved_at": row["resolved_at"]},
        f"Resolved escalation {escalation_id}.",
    )
