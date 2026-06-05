"""Build the dietary eligible-meal pool for every active student.

Opens one agent_runs row (so any escalations link to a run), then walks every
school and every active enrolment, computing and persisting each student's
eligible pool via ``src.tools.eligible_pool``:

  - students with NO dietary requirements take the fast path (all items eligible,
    no LLM call);
  - students WITH requirements are classified by claude-sonnet-4-6, with verdicts
    cached by identical (dietary signature, caterer) so duplicate requirement
    sets cost one call, not many.

Prints: students processed, total eligible rows, students with fewer than four
eligible items, escalations raised, and LLM calls actually made — plus a handful
of sample student_eligible_meals rows and any escalations.

Run: uv run python scripts/build_eligible_pools.py
"""

from __future__ import annotations

import sys

from src.db.connection import fetch_all, get_conn
from src.tools import eligible_pool

_MIN_EXPECTED_ELIGIBLE = 4


def _open_run() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (trigger_reason) VALUES (%s) RETURNING id",
            ("build_eligible_pools",),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return int(run_id)


def _close_run(run_id: int, notes: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runs SET completed_at = now(), notes = %s WHERE id = %s",
            (notes, run_id),
        )
        conn.commit()


def _active_school_ids() -> list[int]:
    """Schools that have a caterer assigned (only they can have a pool built)."""
    rows = fetch_all(
        "SELECT id FROM schools WHERE current_caterer_id IS NOT NULL ORDER BY id"
    )
    return [r[0] for r in rows]


def main() -> int:
    run_id = _open_run()
    client = eligible_pool._new_client()
    cache = eligible_pool.PoolCache()

    school_ids = _active_school_ids()
    print(f"Run {run_id}: building pools across {len(school_ids)} school(s)...", flush=True)

    all_results: list[dict] = []
    all_failures: list[dict] = []

    for school_id in school_ids:
        res = eligible_pool.compute_for_school(
            school_id, run_id=run_id, client=client, cache=cache
        )
        if not res.ok:
            print(f"  [school {school_id}] {res.status}: {res.message}", file=sys.stderr, flush=True)
            continue
        all_results.extend(res.data["results"])
        all_failures.extend(res.data["failures"])
        print(
            f"  [school {school_id}] {res.data['students_processed']} students "
            f"({len(all_results)} total) — {cache.llm_calls} LLM calls so far",
            flush=True,
        )

    # --- Aggregate the per-student summaries. ---
    processed = len(all_results)
    with_reqs = sum(1 for r in all_results if r["had_requirements"])
    total_eligible = sum(r["eligible_count"] for r in all_results)
    total_needs_tweak = sum(r["needs_tweak_count"] for r in all_results)
    total_unsafe = sum(r["unsafe_count"] for r in all_results)
    total_escalations = sum(r["escalations_raised"] for r in all_results)
    thin = [r for r in all_results if r["eligible_count"] < _MIN_EXPECTED_ELIGIBLE]

    print("=" * 70)
    print("ELIGIBLE-POOL BUILD SUMMARY")
    print("=" * 70)
    print(f"Run id ...................... {run_id}")
    print(f"Students processed .......... {processed}")
    print(f"  with dietary requirements . {with_reqs}")
    print(f"  no restrictions (fast path) {processed - with_reqs}")
    print(f"LLM classification calls .... {cache.llm_calls}  (cached duplicates skipped)")
    print(f"Total eligible rows ......... {total_eligible}")
    print(f"  of which need a tweak ..... {total_needs_tweak}")
    print(f"Unsafe audit rows ........... {total_unsafe}")
    print(f"Escalations raised .......... {total_escalations}")
    print(f"Students with < {_MIN_EXPECTED_ELIGIBLE} eligible .. {len(thin)}")
    if all_failures:
        print(f"Read/compute failures ....... {len(all_failures)}")
        for f in all_failures[:10]:
            print(f"    enrolment {f['enrolment_id']}: {f['status']} — {f['message']}")

    if thin:
        print("\nStudents with fewer than 4 eligible items:")
        for r in sorted(thin, key=lambda r: r["eligible_count"]):
            print(
                f"  - {r['student_name']} (enrolment {r['enrolment_id']}): "
                f"{r['eligible_count']} eligible, {r['escalations_raised']} escalated"
            )

    # --- Sample rows: a few dietary students' eligible meals. ---
    print("\n" + "-" * 70)
    print("SAMPLE student_eligible_meals (dietary students)")
    print("-" * 70)
    sample = fetch_all(
        """
        SELECT e.student_name, mi.name, sem.eligible, sem.needs_tweak, sem.rationale
        FROM student_eligible_meals sem
        JOIN enrolments e ON e.id = sem.enrolment_id
        JOIN menu_items mi ON mi.id = sem.menu_item_id
        WHERE e.id IN (
            SELECT DISTINCT enrolment_id FROM enrolment_dietary_tags
        )
        ORDER BY e.student_name, sem.eligible DESC, mi.name
        LIMIT 18
        """
    )
    for student, item, eligible, needs_tweak, rationale in sample:
        flag = "ELIGIBLE" if eligible else "  unsafe"
        tweak = " (tweak)" if needs_tweak else ""
        print(f"  [{flag}{tweak}] {student} — {item}")
        print(f"             {rationale}")

    # --- Escalations raised this run. ---
    print("\n" + "-" * 70)
    print("ESCALATIONS (open)")
    print("-" * 70)
    escalations = fetch_all(
        """
        SELECT id, question
        FROM escalations
        WHERE run_id = %s AND status = 'open'
        ORDER BY id
        """,
        (run_id,),
    )
    if not escalations:
        print("  (none)")
    for esc_id, question in escalations:
        print(f"  #{esc_id}: {question}")

    _close_run(
        run_id,
        f"processed={processed}, eligible_rows={total_eligible}, "
        f"escalations={total_escalations}, llm_calls={cache.llm_calls}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
