"""Casebook memory tool.

Responsibility: the agent's learned memory of how past situations were resolved.
Two operations:
  - store_case  — persist a resolved incident (situation, action taken, outcome,
                  and any operator feedback/edits/approvals from the UI).
  - recall_cases — retrieve similar past cases for the current task, ranked by
                  keyword overlap and recency. No vector DB.

Cases are how operator feedback trains the agent over time. Both return typed
results from `results.py` and NEVER raise at the agent.

Retrieval is deliberately simple (keyword + recency, no embeddings): we pull a
bounded window of recent cases and score each by how many distinct query terms it
mentions (situation + decision + rationale + tags), with tag and related-id
matches weighted higher and recency as the tiebreak. With no query terms it
degrades gracefully to "the most recent cases".

Conventions:
  - Timestamps are timezone-aware (DB ``now()`` / ``timestamptz``).
  - All SQL is parameterised — never string-built.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Sequence

import psycopg
from psycopg.rows import dict_row

from config.settings import settings
from src.db.connection import get_conn
from src.tools.results import ToolResult, empty, error, found, unavailable

logger = logging.getLogger(__name__)

# How many recent cases to pull as scoring candidates. Bounds the work while
# staying well above any realistic `limit`; older cases age out of recall.
_CANDIDATE_WINDOW = 500

# Tiny stopword set so common words don't dominate keyword overlap. Kept small on
# purpose — this is keyword matching, not NLP.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by for from has have in into is it its of on or
    that the their them then there these this to was were will with would you
    your has have had not no do does did can could should would may might
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")


# --- DB helpers --------------------------------------------------------------


def _transaction(describe: str, work: Callable[[psycopg.Cursor], Any]) -> Any | ToolResult:
    """Run ``work(cur)`` in one committed transaction; translate failures to a
    typed ``unavailable`` / ``error`` result (rolled back on exit)."""
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


# --- Tokenisation + scoring --------------------------------------------------


def _tokens(*texts: str | None) -> set[str]:
    """The set of meaningful lowercase word tokens across some text fragments."""
    out: set[str] = set()
    for text in texts:
        if not text:
            continue
        for word in _WORD_RE.findall(text.lower()):
            if len(word) > 2 and word not in _STOPWORDS:
                out.add(word)
    return out


def _clean_tags(tags: Sequence[str] | None) -> list[str]:
    """Normalise a tag list: stringified, stripped, de-duplicated, non-empty."""
    if not tags:
        return []
    seen: list[str] = []
    for tag in tags:
        t = str(tag).strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def _score_case(
    case: dict[str, Any],
    query_tokens: set[str],
    query_tags: set[str],
    related_caterer_id: int | None,
    related_enrolment_id: int | None,
    related_run_id: int | None,
) -> float:
    """Relevance score for one candidate case against the recall query.

    Keyword overlap is the core signal; matching tags and shared related ids add
    bounded boosts. Recency is applied by the caller as the final tiebreak (rows
    arrive newest-first), so this score is purely about topical relevance.
    """
    case_tags = _clean_tags(case.get("tags"))
    case_tokens = _tokens(
        case.get("situation"), case.get("decision"), case.get("rationale")
    ) | _tokens(*case_tags)

    score = 0.0
    score += 1.0 * len(query_tokens & case_tokens)          # keyword overlap
    score += 2.0 * len(query_tags & {t.lower() for t in case_tags})  # tag overlap (stronger)

    if related_caterer_id is not None and case.get("related_caterer_id") == related_caterer_id:
        score += 3.0
    if related_enrolment_id is not None and case.get("related_enrolment_id") == related_enrolment_id:
        score += 3.0
    if related_run_id is not None and case.get("related_run_id") == related_run_id:
        score += 1.0
    return score


# --- Store -------------------------------------------------------------------


def store_case(
    situation: str,
    decision: str | None = None,
    rationale: str | None = None,
    tags: Sequence[str] | None = None,
    related_caterer_id: int | None = None,
    related_enrolment_id: int | None = None,
    related_run_id: int | None = None,
    created_by: str | None = None,
) -> ToolResult:
    """Persist one case (situation/decision/rationale + tags + related ids).

    ``situation`` is the only required field — the context that was faced.
    ``decision`` is what was done (or the operator's guidance), ``rationale`` is
    why. Tags drive keyword recall. Returns the new case id (``found``).
    """
    if not (situation or "").strip():
        return error("situation is required to store a case.")

    clean_tags = _clean_tags(tags)

    def work(cur: psycopg.Cursor) -> dict:
        cur.execute(
            """
            INSERT INTO cases
                (situation, decision, rationale, tags,
                 related_caterer_id, related_enrolment_id, related_run_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (
                situation.strip(),
                (decision or None),
                (rationale or None),
                clean_tags or None,
                related_caterer_id,
                related_enrolment_id,
                related_run_id,
                (created_by or None),
            ),
        )
        return cur.fetchone()

    row = _transaction("storing a case", work)
    if _failed(row):
        return row

    logger.info("store_case: stored case %s (tags=%s)", row["id"], clean_tags)
    return found(
        {"case_id": row["id"], "created_at": row["created_at"], "tags": clean_tags},
        f"Stored case {row['id']}.",
    )


# --- Recall ------------------------------------------------------------------


def recall_cases(
    query: str | None = None,
    tags: Sequence[str] | None = None,
    related_caterer_id: int | None = None,
    related_enrolment_id: int | None = None,
    related_run_id: int | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Retrieve the most relevant prior cases by keyword overlap + recency.

    Pulls a bounded window of recent cases and ranks them by how many query terms
    and tags they share (plus related-id boosts), breaking ties by recency. Any of
    ``query`` / ``tags`` / related ids may be supplied; with none, returns simply
    the most recent cases. ``limit`` defaults to ``settings.recall_case_limit``.

    Returns ``found`` with a relevance-ordered list of cases (each carrying its
    ``relevance_score``), or ``empty`` when the case-book has nothing to offer.
    """
    if limit is None or limit <= 0:
        limit = settings.recall_case_limit

    query_tags = {t.lower() for t in _clean_tags(tags)}
    # Tags participate in keyword overlap too, so a tag-only recall still matches.
    query_tokens = _tokens(query) | {t for tag in query_tags for t in _tokens(tag)}

    def fetch_candidates(cur: psycopg.Cursor) -> list[dict]:
        # Only active cases influence recall — a case DISABLED in the cockpit
        # stays in the table but is skipped here (reversible; see migration 005).
        cur.execute(
            """
            SELECT id, situation, decision, rationale, tags,
                   related_caterer_id, related_enrolment_id, related_run_id,
                   created_at, created_by
            FROM cases
            WHERE active = TRUE
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (_CANDIDATE_WINDOW,),
        )
        return cur.fetchall()

    rows = _transaction("recalling cases", fetch_candidates)
    if _failed(rows):
        return rows
    if not rows:
        return empty("The case-book is empty — no prior cases to recall.")

    # Score (candidates are already newest-first, so a stable sort by score keeps
    # recency as the natural tiebreak).
    scored = [
        (
            _score_case(
                case,
                query_tokens,
                query_tags,
                related_caterer_id,
                related_enrolment_id,
                related_run_id,
            ),
            case,
        )
        for case in rows
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    have_query = bool(query_tokens or related_caterer_id or related_enrolment_id or related_run_id)
    top: list[dict[str, Any]] = []
    for score, case in scored[:limit]:
        # When the caller gave us something to match on, drop zero-overlap cases
        # rather than padding with irrelevant recency. With no query, keep recency.
        if have_query and score <= 0:
            continue
        case = dict(case)
        case["relevance_score"] = score
        top.append(case)

    if not top:
        return empty("No prior cases were relevant to this query.")

    return found(
        top,
        f"Recalled {len(top)} relevant prior case(s).",
    )


# --- Case-book management (cockpit "Manage Lessons") -------------------------
# Explicit operator actions on stored cases. List is read-only; edit / disable /
# delete are the only mutations and are driven from the UI, never the agent.


def list_cases() -> ToolResult:
    """Every case (active and inactive), newest first — for the Manage Lessons tab.

    Unlike ``recall_cases`` this does NOT filter on ``active`` or score relevance:
    the operator needs to see and manage disabled lessons too. Returns ``found``
    with the full list (``empty`` when the case-book has none).
    """

    def fetch(cur: psycopg.Cursor) -> list[dict]:
        cur.execute(
            """
            SELECT id, situation, decision, rationale, tags, active,
                   related_caterer_id, related_enrolment_id, related_run_id,
                   created_at, created_by
            FROM cases
            ORDER BY created_at DESC
            """
        )
        return cur.fetchall()

    rows = _transaction("listing cases", fetch)
    if _failed(rows):
        return rows
    if not rows:
        return empty("The case-book is empty — no lessons yet.")
    return found([dict(r) for r in rows], f"Listed {len(rows)} case(s).")


def update_case(
    case_id: int,
    situation: str,
    decision: str | None = None,
    rationale: str | None = None,
    tags: Sequence[str] | None = None,
) -> ToolResult:
    """Edit a lesson's wording (situation / decision / rationale / tags).

    Takes effect immediately — the edited text is what ``recall_cases`` surfaces
    next. ``situation`` stays required. Returns ``found`` with the case id, or
    ``empty`` when no such case exists.
    """
    if not (situation or "").strip():
        return error("situation is required to update a case.")

    clean_tags = _clean_tags(tags)

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            """
            UPDATE cases
            SET situation = %s, decision = %s, rationale = %s, tags = %s
            WHERE id = %s
            RETURNING id
            """,
            (
                situation.strip(),
                (decision or None),
                (rationale or None),
                clean_tags or None,
                case_id,
            ),
        )
        return cur.fetchone()

    row = _transaction(f"updating case {case_id}", work)
    if _failed(row):
        return row
    if row is None:
        return empty(f"No case {case_id} to update.")
    logger.info("update_case: edited case %s", case_id)
    return found({"case_id": case_id}, f"Updated case {case_id}.")


def set_case_active(case_id: int, active: bool) -> ToolResult:
    """Enable / DISABLE a lesson. A disabled case is skipped by ``recall_cases``
    but kept in the table (reversible). Returns ``found`` with the new state, or
    ``empty`` when no such case exists."""

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            "UPDATE cases SET active = %s WHERE id = %s RETURNING id, active",
            (active, case_id),
        )
        return cur.fetchone()

    row = _transaction(f"toggling case {case_id}", work)
    if _failed(row):
        return row
    if row is None:
        return empty(f"No case {case_id} to update.")
    state = "enabled" if active else "disabled"
    logger.info("set_case_active: %s case %s", state, case_id)
    return found({"case_id": case_id, "active": row["active"]}, f"Case {case_id} {state}.")


def delete_case(case_id: int) -> ToolResult:
    """Permanently delete a lesson. Irreversible — prefer ``set_case_active`` to
    disable. Returns ``found`` on delete, ``empty`` when no such case exists."""

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute("DELETE FROM cases WHERE id = %s RETURNING id", (case_id,))
        return cur.fetchone()

    row = _transaction(f"deleting case {case_id}", work)
    if _failed(row):
        return row
    if row is None:
        return empty(f"No case {case_id} to delete.")
    logger.info("delete_case: deleted case %s", case_id)
    return found({"case_id": case_id}, f"Deleted case {case_id}.")
