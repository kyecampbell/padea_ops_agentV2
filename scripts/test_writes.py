"""Prove the write tools + the order-state "money line" gate against the DB.

Uses a TEMPORARY test enrolment (added with the real ``add_enrolment`` tool, then
torn down in a finally block) and a throwaway sent order to demonstrate:

  (a) a preference change with NO sent order  -> gate says AUTONOMOUS and applies;
  (b) the same change once an order is SENT    -> gate says REQUIRES_APPROVAL
      (we only confirm the verdict — queue-and-wait enforcement is wired later);
  (c) add_enrolment                            -> REQUIRES_APPROVAL;
  (d) an ineligible item                       -> rejected with a non-ok ToolResult.

Everything created is cleaned up afterwards even on failure. Prints a PASS/FAIL
line per check and exits non-zero if anything fails.

Run: uv run python scripts/test_writes.py
"""

from __future__ import annotations

import sys
from datetime import date

from src.agent.gates import gate
from src.db.connection import fetch_all, get_conn
from src.tools import writes
from src.tools.order_state import has_order_been_sent
from src.tools.results import ToolResult

# A far-future session so our throwaway order never collides with seeded data.
_SESSION_DATE = date(2099, 1, 1)

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


def expect(label: str, result: ToolResult, status: str) -> bool:
    ok = isinstance(result, ToolResult) and result.status == status
    detail = f"status={getattr(result, 'status', '?')!r} msg={getattr(result, 'message', '')!r}"
    check(label, ok, detail)
    return ok


def _pick_fixtures() -> dict:
    """A school with a caterer, two of that caterer's menu items, and one menu
    item belonging to a DIFFERENT caterer (guaranteed ineligible / off-menu)."""
    (school_id, caterer_id) = fetch_all(
        """
        SELECT id, current_caterer_id
        FROM schools
        WHERE current_caterer_id IS NOT NULL
        ORDER BY id LIMIT 1
        """
    )[0]
    own_all = [
        r[0]
        for r in fetch_all(
            "SELECT id FROM menu_items WHERE caterer_id = %s AND active = TRUE ORDER BY id LIMIT 3",
            (caterer_id,),
        )
    ]
    own_items = own_all[:2]  # made eligible in the test fixture
    # A third own-caterer item we deliberately DON'T mark eligible -> on-menu but
    # dietary-ineligible (the safety-critical rejection branch).
    on_menu_ineligible = own_all[2] if len(own_all) > 2 else None
    other = fetch_all(
        "SELECT id FROM menu_items WHERE caterer_id <> %s ORDER BY id LIMIT 1",
        (caterer_id,),
    )
    foreign_item = other[0][0] if other else None
    a_session_slot = fetch_all(
        "SELECT id FROM session_slots WHERE school_id = %s ORDER BY id LIMIT 1", (school_id,)
    )
    return {
        "school_id": school_id,
        "caterer_id": caterer_id,
        "own_items": own_items,
        "on_menu_ineligible": on_menu_ineligible,
        "foreign_item": foreign_item,
        "session_slot_id": a_session_slot[0][0] if a_session_slot else None,
    }


def _seed_eligible(enrolment_id: int, menu_item_ids: list[int]) -> None:
    """Mark the given menu items dietary-eligible for the test enrolment."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO student_eligible_meals (enrolment_id, menu_item_id, eligible, rationale)
            VALUES (%s, %s, TRUE, 'test fixture')
            """,
            [(enrolment_id, mid) for mid in menu_item_ids],
        )
        conn.commit()


def _ensure_session_slot(school_id: int, existing: int | None) -> tuple[int, bool]:
    """Reuse an existing session slot, or create a throwaway one. Returns
    (session_slot_id, created_here)."""
    if existing is not None:
        return existing, False
    used = {r[0] for r in fetch_all("SELECT day_of_week FROM session_slots WHERE school_id = %s", (school_id,))}
    day = next(d for d in range(1, 8) if d not in used)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO session_slots (school_id, day_of_week, start_time, dinner_time, end_time)
            VALUES (%s, %s, '16:00', '18:00', '19:00')
            RETURNING id
            """,
            (school_id, day),
        )
        slot_id = cur.fetchone()[0]
        conn.commit()
    return slot_id, True


def _mark_order_sent(session_slot_id: int, caterer_id: int, enrolment_id: int, menu_item_id: int) -> int:
    """Insert a SENT order with a line covering the test enrolment. Returns order id."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders
                (session_slot_id, caterer_id, session_date, total_items,
                 total_cost_cents, gst_rate_percent, sent_at)
            VALUES (%s, %s, %s, 1, 0, 10.0, now())
            RETURNING id
            """,
            (session_slot_id, caterer_id, _SESSION_DATE),
        )
        order_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO order_lines (order_id, enrolment_id, menu_item_id, source)
            VALUES (%s, %s, %s, 'rotation')
            """,
            (order_id, enrolment_id, menu_item_id),
        )
        conn.commit()
    return order_id


def _cleanup(
    enrolment_id: int | None,
    order_id: int | None,
    slot_id: int | None,
    created_slot: bool,
    menu_restore: tuple[int, str | None, str | None] | None = None,
) -> None:
    """Remove every row this test created, children before parents, and restore
    the original text of any menu item we mutated in place."""
    with get_conn() as conn, conn.cursor() as cur:
        if menu_restore is not None:
            mid, orig_contents, orig_tweaks = menu_restore
            cur.execute(
                "UPDATE menu_items SET contents_text = %s, tweaks_text = %s WHERE id = %s",
                (orig_contents, orig_tweaks, mid),
            )
        if order_id is not None:
            cur.execute("DELETE FROM order_lines WHERE order_id = %s", (order_id,))
            cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
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
        if created_slot and slot_id is not None:
            cur.execute("DELETE FROM session_slots WHERE id = %s", (slot_id,))
        conn.commit()


def main() -> int:
    fx = _pick_fixtures()
    print(f"Fixtures: {fx}\n")
    if len(fx["own_items"]) < 2 or fx["foreign_item"] is None:
        print("Seed lacks enough menu items to run this test.", file=sys.stderr)
        return 1

    enrolment_id: int | None = None
    order_id: int | None = None
    slot_id: int | None = None
    created_slot = False
    menu_restore: tuple[int, str | None, str | None] | None = None
    try:
        # --- (c) add_enrolment: REQUIRES_APPROVAL verdict + it inserts. ---
        check("(c) gate(add_enrolment) == requires_approval", gate("add_enrolment") == "requires_approval")
        add = writes.add_enrolment(
            school_id=fx["school_id"],
            student_name="Test Student (temp)",
            year_level=7,
            parent_name="Test Parent",
            parent_email="test.parent@example.com",
            dietary_raw=None,
        )
        if expect("(c) add_enrolment applies", add, "found"):
            enrolment_id = add.data["enrolment_id"]
        if enrolment_id is None:
            print("\nCould not create test enrolment; aborting.", file=sys.stderr)
            return 1

        # Make the two own-caterer items dietary-eligible for this student.
        _seed_eligible(enrolment_id, fx["own_items"])

        # --- (a) No order sent -> AUTONOMOUS, and the change applies. ---
        sent = has_order_been_sent(enrolment_id, _SESSION_DATE)
        order_sent = sent.data["order_sent"] if sent.ok else None
        check("(a) has_order_been_sent == False (no order yet)", order_sent is False,
              f"payload={sent.data if sent.ok else sent.message}")
        check("(a) gate(update_term_meal_preference, order_sent=False) == autonomous",
              gate("update_term_meal_preference", order_sent=order_sent) == "autonomous")
        applied = writes.update_term_meal_preference(enrolment_id, fx["own_items"])
        if expect("(a) update_term_meal_preference applies", applied, "found"):
            rows = fetch_all(
                """
                SELECT tmpi.menu_item_id, tmpi.rank
                FROM term_meal_preference_items tmpi
                JOIN term_meal_preferences tmp ON tmp.id = tmpi.preference_id
                WHERE tmp.enrolment_id = %s
                ORDER BY tmpi.rank
                """,
                (enrolment_id,),
            )
            check("    items persisted in ranked order",
                  [r[0] for r in rows] == fx["own_items"] and [r[1] for r in rows] == [1, 2],
                  f"rows={rows}")

        # --- (b) Order now sent -> REQUIRES_APPROVAL verdict. ---
        slot_id, created_slot = _ensure_session_slot(fx["school_id"], fx["session_slot_id"])
        order_id = _mark_order_sent(slot_id, fx["caterer_id"], enrolment_id, fx["own_items"][0])
        sent2 = has_order_been_sent(enrolment_id, _SESSION_DATE)
        order_sent2 = sent2.data["order_sent"] if sent2.ok else None
        check("(b) has_order_been_sent == True (order marked sent)", order_sent2 is True,
              f"payload={sent2.data if sent2.ok else sent2.message}")
        check("(b) gate(update_term_meal_preference, order_sent=True) == requires_approval",
              gate("update_term_meal_preference", order_sent=order_sent2) == "requires_approval")

        # --- (d) Ineligible item -> rejected, non-ok ToolResult. ---
        # On-menu but NOT dietary-eligible (the safety-critical branch).
        if fx["on_menu_ineligible"] is not None:
            rej = writes.update_term_meal_preference(enrolment_id, [fx["on_menu_ineligible"]])
            if expect("(d) on-menu but ineligible item rejected (conflict)", rej, "conflict"):
                check("    rejection is non-ok", not rej.ok, f"msg={rej.message}")
                check("    preference unchanged after rejection",
                      [r[0] for r in fetch_all(
                          """
                          SELECT tmpi.menu_item_id FROM term_meal_preference_items tmpi
                          JOIN term_meal_preferences tmp ON tmp.id = tmpi.preference_id
                          WHERE tmp.enrolment_id = %s ORDER BY tmpi.rank
                          """, (enrolment_id,))] == fx["own_items"])
        # Off-menu item (different caterer) -> also rejected.
        rejected = writes.update_term_meal_preference(enrolment_id, [fx["foreign_item"]])
        if expect("(d) off-menu item rejected (conflict)", rejected, "conflict"):
            check("    rejection is non-ok", not rejected.ok, f"msg={rejected.message}")

        # --- (e) update_menu_item_description: AUTONOMOUS, records + persists. ---
        check("(e) gate(update_menu_item_description) == autonomous",
              gate("update_menu_item_description") == "autonomous")
        mid = fx["on_menu_ineligible"]
        orig = fetch_all(
            "SELECT contents_text, tweaks_text FROM menu_items WHERE id = %s", (mid,)
        )[0]
        menu_restore = (mid, orig[0], orig[1])  # restored in _cleanup
        new_contents = "TEST contents — caterer clarified: no peanuts"
        upd = writes.update_menu_item_description(mid, contents_text=new_contents)
        if expect("(e) update_menu_item_description applies", upd, "found"):
            after = fetch_all(
                "SELECT contents_text, tweaks_text FROM menu_items WHERE id = %s", (mid,)
            )[0]
            check("    contents_text persisted", after[0] == new_contents, f"contents={after[0]!r}")
            check("    tweaks_text untouched (not supplied)", after[1] == orig[1], f"tweaks={after[1]!r}")
        # No fields supplied -> error, no write.
        none_given = writes.update_menu_item_description(mid)
        expect("(e) no fields supplied -> error", none_given, "error")

    finally:
        _cleanup(enrolment_id, order_id, slot_id, created_slot, menu_restore)
        print("\nCleaned up test enrolment / order / slot.")

    print(f"\n{_passes} passed, {_failures} failed.")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
