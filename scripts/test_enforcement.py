"""Prove the orchestrator ENFORCES the hard-rules gate before each tool call.

Drives ``loop._enforce_and_dispatch`` (the exact path ``run_incident`` uses) for
three proposed tool calls against the real DB, logging each as an ``agent_steps``
row, and proves:

  (a) AUTONOMOUS — a pre-order meal-preference change gates to ``autonomous`` and
      actually executes (the rows land in the DB);
  (b) REQUIRES_APPROVAL (write) — ``add_enrolment`` gates to ``requires_approval``,
      is NOT executed (no enrolment row appears), and comes back as a non-ok
      "queued — NOT applied" result, recorded as a pending proposal in agent_steps;
  (c) REQUIRES_APPROVAL (email) — a 'warning' email gates to ``requires_approval``,
      is logged 'queued_for_approval' in outbound_emails, and is NOT sent.

Setup uses the real ``writes.add_enrolment`` directly (bypassing the gate) to make
a TEMPORARY enrolment; everything created is torn down in a finally block. Prints
the agent_steps rows + the affected DB rows, a PASS/FAIL line per check, and exits
non-zero on any failure.

Run: uv run python scripts/test_enforcement.py
"""

from __future__ import annotations

import sys

from src.agent.loop import _enforce_and_dispatch, _log_step, _open_run
from src.db.connection import fetch_all, get_conn
from src.tools import writes
from src.tools.results import ToolResult

# A name we will PROPOSE to add but expect the gate to block — so it must never
# appear in the DB.
_BLOCKED_STUDENT = "Enforcement Test — SHOULD NOT BE ADDED"

_passes = 0
_failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _passes, _failures
    mark = "PASS" if ok else "FAIL"
    if ok:
        _passes += 1
    else:
        _failures += 1
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))


def _pick_fixtures() -> dict:
    """A school with a caterer plus two of that caterer's active menu items."""
    (school_id, caterer_id) = fetch_all(
        """
        SELECT id, current_caterer_id
        FROM schools
        WHERE current_caterer_id IS NOT NULL
        ORDER BY id LIMIT 1
        """
    )[0]
    own_items = [
        r[0]
        for r in fetch_all(
            "SELECT id FROM menu_items WHERE caterer_id = %s AND active = TRUE ORDER BY id LIMIT 2",
            (caterer_id,),
        )
    ]
    return {"school_id": school_id, "caterer_id": caterer_id, "own_items": own_items}


def _seed_eligible(enrolment_id: int, menu_item_ids: list[int]) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO student_eligible_meals (enrolment_id, menu_item_id, eligible, rationale)
            VALUES (%s, %s, TRUE, 'enforcement test fixture')
            """,
            [(enrolment_id, mid) for mid in menu_item_ids],
        )
        conn.commit()


def _step_row(run_id: int, step_index: int) -> dict:
    (r,) = fetch_all(
        """
        SELECT step_index, tool_name, action_class,
               tool_output_full->>'status'              AS result_status,
               tool_output_full->'data'->>'applied'     AS applied
        FROM agent_steps WHERE run_id = %s AND step_index = %s
        """,
        (run_id, step_index),
    )
    return {
        "step_index": r[0], "tool_name": r[1], "action_class": r[2],
        "result_status": r[3], "applied": r[4],
    }


def _cleanup(run_id: int | None, enrolment_id: int | None, email_ids: list[int]) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        if email_ids:
            cur.execute("DELETE FROM outbound_emails WHERE id = ANY(%s)", (email_ids,))
        if enrolment_id is not None:
            cur.execute(
                """
                DELETE FROM term_meal_preference_items
                WHERE preference_id IN (SELECT id FROM term_meal_preferences WHERE enrolment_id = %s)
                """,
                (enrolment_id,),
            )
            cur.execute("DELETE FROM term_meal_preferences WHERE enrolment_id = %s", (enrolment_id,))
            cur.execute("DELETE FROM student_eligible_meals WHERE enrolment_id = %s", (enrolment_id,))
            cur.execute("DELETE FROM enrolments WHERE id = %s", (enrolment_id,))
        # Belt-and-braces: drop any enrolment the blocked proposal might have created.
        cur.execute("DELETE FROM enrolments WHERE student_name = %s", (_BLOCKED_STUDENT,))
        if run_id is not None:
            cur.execute("DELETE FROM agent_steps WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM agent_runs WHERE id = %s", (run_id,))
        conn.commit()


def main() -> int:
    fx = _pick_fixtures()
    print(f"Fixtures: {fx}\n")
    if len(fx["own_items"]) < 2:
        print("Seed lacks enough menu items to run this test.", file=sys.stderr)
        return 1

    run_id: int | None = None
    enrolment_id: int | None = None
    email_ids: list[int] = []
    step_index = 0
    try:
        run_id = _open_run("test_enforcement")
        print(f"Opened agent_run {run_id}\n")

        # Scaffolding: create a temp enrolment directly (bypasses the gate).
        add = writes.add_enrolment(
            school_id=fx["school_id"],
            student_name="Enforcement Test Student (temp)",
            parent_name="Test Parent",
            parent_email="test.parent@example.com",
            year_level=7,
        )
        assert add.ok, f"setup add_enrolment failed: {add.message}"
        enrolment_id = add.data["enrolment_id"]
        _seed_eligible(enrolment_id, fx["own_items"])

        # --- (a) AUTONOMOUS: pre-order preference change executes. ---
        result, verdict = _enforce_and_dispatch(
            "update_term_meal_preference",
            {"enrolment_id": enrolment_id, "ranked_menu_item_ids": fx["own_items"]},
        )
        _log_step(run_id, step_index, "update_term_meal_preference",
                  {"enrolment_id": enrolment_id, "ranked_menu_item_ids": fx["own_items"]},
                  result, "autonomous pre-order change", verdict)
        check("(a) verdict == autonomous", verdict == "autonomous", f"verdict={verdict}")
        check("(a) result is found (executed)", result.status == "found", f"msg={result.message}")
        persisted = [
            r[0] for r in fetch_all(
                """
                SELECT tmpi.menu_item_id FROM term_meal_preference_items tmpi
                JOIN term_meal_preferences tmp ON tmp.id = tmpi.preference_id
                WHERE tmp.enrolment_id = %s ORDER BY tmpi.rank
                """, (enrolment_id,))
        ]
        check("(a) preference actually written to DB", persisted == fx["own_items"], f"rows={persisted}")
        print(f"    step row: {_step_row(run_id, step_index)}")
        step_index += 1

        # --- (b) REQUIRES_APPROVAL (write): add_enrolment is NOT executed. ---
        before = fetch_all(
            "SELECT count(*) FROM enrolments WHERE student_name = %s", (_BLOCKED_STUDENT,)
        )[0][0]
        proposed = {
            "school_id": fx["school_id"],
            "student_name": _BLOCKED_STUDENT,
            "parent_name": "Nope Parent",
            "parent_email": "nope@example.com",
        }
        result, verdict = _enforce_and_dispatch("add_enrolment", proposed)
        _log_step(run_id, step_index, "add_enrolment", proposed, result,
                  "proposing to add a student", verdict)
        check("(b) verdict == requires_approval", verdict == "requires_approval", f"verdict={verdict}")
        check("(b) result is queued (not applied)", result.status == "queued", f"status={result.status}")
        check("(b) queued result is non-ok", not result.ok, f"ok={result.ok}")
        check("(b) result flagged applied=False",
              result.data and result.data.get("applied") is False, f"data={result.data}")
        after = fetch_all(
            "SELECT count(*) FROM enrolments WHERE student_name = %s", (_BLOCKED_STUDENT,)
        )[0][0]
        check("(b) NO enrolment row created (gate blocked the write)", after == before,
              f"before={before} after={after}")
        srow = _step_row(run_id, step_index)
        print(f"    step row: {srow}")
        check("(b) agent_steps logged action_class=requires_approval",
              srow["action_class"] == "requires_approval")
        step_index += 1

        # --- (c) REQUIRES_APPROVAL (email): 'warning' is queued, NOT sent. ---
        email_args = {
            "email_type": "warning",
            "to": "caterer.real@example.com",
            "subject": "Quality concern",
            "body": "Noting a decline in meal quality.",
        }
        result, verdict = _enforce_and_dispatch("send_email", email_args)
        _log_step(run_id, step_index, "send_email", email_args, result,
                  "proposing a commercial warning email", verdict)
        check("(c) verdict == requires_approval", verdict == "requires_approval", f"verdict={verdict}")
        check("(c) result is queued (not sent)", result.status == "queued", f"status={result.status}")
        if result.data and result.data.get("email_id"):
            email_ids.append(result.data["email_id"])
        check("(c) email NOT sent", result.data and result.data.get("sent") is False, f"data={result.data}")
        if email_ids:
            (erow,) = fetch_all(
                "SELECT id, status, gmail_message_id FROM outbound_emails WHERE id = %s",
                (email_ids[0],),
            )
            print(f"    outbound_emails row: id={erow[0]} status={erow[1]!r} gmail_message_id={erow[2]!r}")
            check("(c) logged status 'queued_for_approval'", erow[1] == "queued_for_approval")
            check("(c) no gmail_message_id (never sent)", erow[2] is None)
        print(f"    step row: {_step_row(run_id, step_index)}")
        step_index += 1

    finally:
        _cleanup(run_id, enrolment_id, email_ids)
        print(f"\nCleaned up run {run_id}, temp enrolment, and {len(email_ids)} queued email row(s).")

    print(f"\n{_passes} passed, {_failures} failed.")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
