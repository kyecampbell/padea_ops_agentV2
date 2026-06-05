"""Exercise the read tools against the seeded Supabase DB.

For each tool we run a real call against seeded data and assert the result has
the expected status and shape, then deliberately hit one not-found case to
confirm it returns status='empty' (not a crash). Prints a pass/fail line per
check and exits non-zero if anything fails.

Run: uv run python scripts/test_query_tools.py
"""

from __future__ import annotations

import sys

from src.db.connection import fetch_all
from src.tools import query
from src.tools.results import ToolResult

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


def expect(label: str, result: ToolResult, status: str, detail: str = "") -> bool:
    ok = isinstance(result, ToolResult) and result.status == status
    shown = detail or f"status={getattr(result, 'status', '?')!r} msg={getattr(result, 'message', '')!r}"
    check(label, ok, shown)
    return ok


def _anchor_ids() -> dict[str, int]:
    """Pull real ids out of the seed so the test is independent of fixed values."""
    (school_id,) = fetch_all(
        "SELECT id FROM schools WHERE current_caterer_id IS NOT NULL ORDER BY id LIMIT 1"
    )[0]
    (caterer_id,) = fetch_all("SELECT current_caterer_id FROM schools WHERE id = %s", (school_id,))[0]
    # An enrolment that actually has dietary tags, so dietary_tag_names is non-trivial.
    tagged = fetch_all(
        """
        SELECT e.id
        FROM enrolments e
        JOIN enrolment_dietary_tags edt ON edt.enrolment_id = e.id
        ORDER BY e.id LIMIT 1
        """
    )
    enrolment_id = tagged[0][0] if tagged else fetch_all("SELECT id FROM enrolments ORDER BY id LIMIT 1")[0][0]
    return {"school_id": school_id, "caterer_id": caterer_id, "enrolment_id": enrolment_id}


def main() -> int:
    ids = _anchor_ids()
    print(f"Anchor ids: {ids}\n")

    # get_enrolment — found, with dietary fields present.
    r = query.get_enrolment(ids["enrolment_id"])
    if expect("get_enrolment(found)", r, "found"):
        check(
            "  enrolment carries dietary fields",
            "dietary_raw" in r.data and "dietary_tag_names" in r.data,
            f"tags={r.data.get('dietary_tag_names')}",
        )

    # get_caterer — found.
    r = query.get_caterer(ids["caterer_id"])
    expect("get_caterer(found)", r, "found")

    # get_caterer_for_school — found.
    r = query.get_caterer_for_school(ids["school_id"])
    expect("get_caterer_for_school(found)", r, "found")

    # get_menu_items — found, with price_cents (int) and dietary_tag_names.
    r = query.get_menu_items(ids["caterer_id"])
    if expect("get_menu_items(found)", r, "found"):
        item = r.data[0]
        check(
            "  menu item shape",
            isinstance(item.get("price_cents"), int) and "dietary_tag_names" in item,
            f"price_cents={item.get('price_cents')} tags={item.get('dietary_tag_names')}",
        )

    # get_caterer_moq_tiers — found.
    r = query.get_caterer_moq_tiers(ids["caterer_id"])
    expect("get_caterer_moq_tiers(found)", r, "found")

    # list_active_enrolments — found (school has active enrolments in seed).
    r = query.list_active_enrolments(ids["school_id"])
    if expect("list_active_enrolments(found)", r, "found", f"count={len(r.data) if r.ok else 'n/a'}"):
        check("  enrolment rows carry dietary_tag_names", "dietary_tag_names" in r.data[0])

    # get_all_dietary_tags — found.
    r = query.get_all_dietary_tags()
    expect("get_all_dietary_tags(found)", r, "found", f"count={len(r.data) if r.ok else 'n/a'}")

    # --- Deliberate empty case: a non-existent enrolment id must not crash. ---
    bogus = 99_999_999
    r = query.get_enrolment(bogus)
    expect(f"get_enrolment({bogus}) -> empty (no crash)", r, "empty")

    print(f"\n{_passes} passed, {_failures} failed.")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
