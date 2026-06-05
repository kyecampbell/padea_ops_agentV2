"""Policy-book tool — operator-authored authoritative rules.

Responsibility: the operator's editable BUSINESS-rule layer that sits ON TOP of
the always-on handbook. Where the case-book (``casebook.py``) holds lessons
recalled by similarity, policies are PRESCRIPTIVE: every ACTIVE policy is injected
into the agent's context for every task (see ``src.agent.context``) and the agent
treats it as binding. The hard safety/operational invariants (dietary safety, the
approval gate, the order-state money line, demo routing) stay in the handbook /
code and are NOT editable here — policies are the operator's layer on top.

Operations (all return typed results from ``results.py`` and NEVER raise at the
agent / cockpit):
  - active_policies — the active rules, in operator order, for context injection.
  - list_policies   — every policy (active + disabled) for the Manage Policies tab.
  - add_policy / update_policy / set_policy_active / delete_policy — cockpit edits,
    effective immediately (the next task's context reflects them).

Conventions:
  - Timestamps are timezone-aware (DB ``now()`` / ``timestamptz``; ``updated_at``
    is maintained by the OPT-01 trigger).
  - All SQL is parameterised — never string-built.
  - Ordering is ``sort_order`` then ``id`` (a stable, operator-controllable order).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from config.settings import PROJECT_ROOT
from src.db.connection import get_conn
from src.tools.results import ToolResult, empty, error, found, unavailable

logger = logging.getLogger(__name__)

# Where the optional human-readable export is written. The TABLE is the source of
# truth; this file is a convenience mirror only (see ``export_policies_md``).
_EXPORT_PATH = PROJECT_ROOT / "config" / "policies.md"


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


# --- Read --------------------------------------------------------------------


def active_policies() -> ToolResult:
    """The ACTIVE policies in operator order — what gets injected into context.

    Disabled policies are skipped (reversible). Returns ``found`` with the list
    (``sort_order`` then ``id``), or ``empty`` when no active policy exists (the
    common start state — the policy-book begins blank)."""

    def fetch(cur: psycopg.Cursor) -> list[dict]:
        cur.execute(
            """
            SELECT id, text, sort_order, created_at, updated_at
            FROM policies
            WHERE active = TRUE
            ORDER BY sort_order, id
            """
        )
        return cur.fetchall()

    rows = _transaction("reading active policies", fetch)
    if _failed(rows):
        return rows
    if not rows:
        return empty("No active policies — the policy-book is empty.")
    return found([dict(r) for r in rows], f"{len(rows)} active policy(ies).")


def list_policies() -> ToolResult:
    """Every policy (active and disabled), in operator order — for the Manage
    Policies tab. Unlike ``active_policies`` this does NOT filter on ``active``:
    the operator manages disabled policies too. ``empty`` when there are none."""

    def fetch(cur: psycopg.Cursor) -> list[dict]:
        cur.execute(
            """
            SELECT id, text, active, sort_order, created_at, updated_at
            FROM policies
            ORDER BY sort_order, id
            """
        )
        return cur.fetchall()

    rows = _transaction("listing policies", fetch)
    if _failed(rows):
        return rows
    if not rows:
        return empty("The policy-book is empty — no policies yet.")
    return found([dict(r) for r in rows], f"Listed {len(rows)} policy(ies).")


# --- Management (cockpit "Manage Policies") ----------------------------------
# Explicit operator actions on policies. Reads are above; add / edit / disable /
# delete are the only mutations and are driven from the UI, never the agent.


def add_policy(text: str, sort_order: int | None = None) -> ToolResult:
    """Create one policy. ``text`` is the rule; ``sort_order`` controls where it
    reads in context (defaults to after the current last policy). Effective
    immediately — the next task's context includes it. Returns the new id."""
    if not (text or "").strip():
        return error("text is required to add a policy.")

    def work(cur: psycopg.Cursor) -> dict:
        order = sort_order
        if order is None:
            cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS next FROM policies")
            order = cur.fetchone()["next"]
        cur.execute(
            "INSERT INTO policies (text, sort_order) VALUES (%s, %s) RETURNING id, sort_order",
            (text.strip(), order),
        )
        return cur.fetchone()

    row = _transaction("adding a policy", work)
    if _failed(row):
        return row
    logger.info("add_policy: stored policy %s", row["id"])
    return found(
        {"policy_id": row["id"], "sort_order": row["sort_order"]},
        f"Added policy {row['id']}.",
    )


def update_policy(
    policy_id: int, text: str, sort_order: int | None = None
) -> ToolResult:
    """Edit a policy's wording (and optionally its ``sort_order``). Takes effect
    immediately — the edited text is what the next task's context carries.
    ``text`` stays required. ``empty`` when no such policy exists."""
    if not (text or "").strip():
        return error("text is required to update a policy.")

    def work(cur: psycopg.Cursor) -> dict | None:
        if sort_order is None:
            cur.execute(
                "UPDATE policies SET text = %s WHERE id = %s RETURNING id",
                (text.strip(), policy_id),
            )
        else:
            cur.execute(
                "UPDATE policies SET text = %s, sort_order = %s WHERE id = %s RETURNING id",
                (text.strip(), sort_order, policy_id),
            )
        return cur.fetchone()

    row = _transaction(f"updating policy {policy_id}", work)
    if _failed(row):
        return row
    if row is None:
        return empty(f"No policy {policy_id} to update.")
    logger.info("update_policy: edited policy %s", policy_id)
    return found({"policy_id": policy_id}, f"Updated policy {policy_id}.")


def set_policy_active(policy_id: int, active: bool) -> ToolResult:
    """Enable / DISABLE a policy. A disabled policy is dropped from context but
    kept in the table (reversible). Returns the new state, or ``empty`` when no
    such policy exists."""

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute(
            "UPDATE policies SET active = %s WHERE id = %s RETURNING id, active",
            (active, policy_id),
        )
        return cur.fetchone()

    row = _transaction(f"toggling policy {policy_id}", work)
    if _failed(row):
        return row
    if row is None:
        return empty(f"No policy {policy_id} to update.")
    state = "enabled" if active else "disabled"
    logger.info("set_policy_active: %s policy %s", state, policy_id)
    return found(
        {"policy_id": policy_id, "active": row["active"]}, f"Policy {policy_id} {state}."
    )


def delete_policy(policy_id: int) -> ToolResult:
    """Permanently delete a policy. Irreversible — prefer ``set_policy_active`` to
    disable. Returns ``found`` on delete, ``empty`` when no such policy exists."""

    def work(cur: psycopg.Cursor) -> dict | None:
        cur.execute("DELETE FROM policies WHERE id = %s RETURNING id", (policy_id,))
        return cur.fetchone()

    row = _transaction(f"deleting policy {policy_id}", work)
    if _failed(row):
        return row
    if row is None:
        return empty(f"No policy {policy_id} to delete.")
    logger.info("delete_policy: deleted policy %s", policy_id)
    return found({"policy_id": policy_id}, f"Deleted policy {policy_id}.")


# --- Optional export ---------------------------------------------------------


def export_policies_md() -> ToolResult:
    """Write the ACTIVE policies to ``config/policies.md`` (a convenience mirror).

    The policies TABLE is the source of truth — context injection reads the table,
    not this file. This export exists only so the current rules can be diffed /
    read as a flat file. Returns ``found`` with the path and count."""
    result = active_policies()
    if result.status in ("unavailable", "error"):
        return result
    rows = result.data or []
    lines = [
        "# Padea Operator Policies (active)",
        "",
        "Exported from the `policies` table — the TABLE is the source of truth; the",
        "agent injects these from the DB, not from this file. Edit policies in the",
        "cockpit's Manage Policies tab, not here.",
        "",
    ]
    if not rows:
        lines.append("_No active policies._")
    else:
        for p in rows:
            lines.append(f"- **[Policy #{p['id']}]** {p['text']}")
    try:
        _EXPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        return error(f"Could not write {_EXPORT_PATH}: {exc}")
    logger.info("export_policies_md: wrote %s active policy(ies)", len(rows))
    return found({"path": str(_EXPORT_PATH), "count": len(rows)}, f"Exported {len(rows)} policy(ies).")
