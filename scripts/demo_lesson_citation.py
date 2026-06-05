"""Demo: lesson-citation traceability end-to-end.

Shows the full loop the feature adds:
  1. seed one operator-trained lesson into the case-book,
  2. run a real incident whose task matches it — the context assembler surfaces
     the lesson as "[Lesson #<id>]" and the handbook asks the model to cite it as
     "(applying Lesson #<id>: <why>)" when it actually applies it,
  3. the orchestrator parses that citation and records ONLY the cited lesson
     (+ the why) in step_lesson_citations, validated against what was recalled,
  4. print the recorded citations exactly as the cockpit's info icon reads them
     (via ui.server._citations_for_runs), with the Manage-Lessons deep link.

Sends are DRY: EMAIL_MODE=dry is forced before any import, so any email the agent
chooses to send is logged 'drafted' and NOT delivered.

Run: uv run python scripts/demo_lesson_citation.py
"""

from __future__ import annotations

import os

# Force dry sends BEFORE importing settings (module-level singleton). Nothing is
# delivered; the demo is about citations, not mail.
os.environ["EMAIL_MODE"] = "dry"

import sys

from src.agent.loop import run_incident
from src.db.connection import fetch_all
from src.tools.casebook import store_case
from ui.server import _citations_for_runs

# A complaint scenario the handbook already has a clear stance on (acknowledge the
# parent first, then gather facts) — the seeded lesson reinforces it so recall is
# strong and the model has precedent to cite.
LESSON = {
    "situation": (
        "A parent emailed complaining their child's dinner from the school caterer "
        "arrived cold and late."
    ),
    "decision": (
        "Always acknowledge the complaining parent FIRST — thank them, take it "
        "seriously, say we're looking into it — before judging the caterer. Then "
        "ask the session manager for specifics (which meal, which session, how bad) "
        "before deciding anything about the caterer."
    ),
    "rationale": "Operator guidance: never leave a complaining parent waiting, and never judge a caterer off one vague report.",
    "tags": ["complaint", "parent", "caterer", "quality"],
}

TASK = (
    "An inbound email just arrived from a parent at MacGregor State High School: "
    "\"My son had the catered dinner last night and it turned up cold and very "
    "late again. This isn't the first time. Can you sort it out?\" "
    "Decide how to handle this complaint and take the appropriate first steps."
)


def _print_citations(run_id: int) -> None:
    cites = _citations_for_runs([run_id]).get(run_id, [])
    if not cites:
        print("  (no lessons were cited as used on this run)")
        return
    print(f"  Referenced {len(cites)} lesson(s) — exactly what the feed's ⓘ icon shows:")
    for c in cites:
        where = (
            f"step {c['step_index']}" if c["step_index"] is not None else "final answer"
        )
        print(f"   • Lesson #{c['case_id']}  (cited at {where})")
        print(f"       why : {c['reason'] or '—'}")
        print(f"       link: /lessons#lesson-{c['case_id']}  (Manage Lessons tab)")


def main() -> int:
    print("Seeding the lesson into the case-book…")
    # Reuse the lesson if a prior demo run already stored it (keeps recall from
    # splitting across near-identical duplicates).
    existing = fetch_all(
        "SELECT id FROM cases WHERE situation = %s AND active = TRUE ORDER BY id LIMIT 1",
        (LESSON["situation"],),
    )
    if existing:
        lesson_id = existing[0][0]
        print(f"  reusing existing Lesson #{lesson_id}\n")
    else:
        stored = store_case(**LESSON)
        if not stored.ok:
            print(f"  could not store the lesson: {stored.message}")
            return 1
        lesson_id = stored.data["case_id"]
        print(f"  stored Lesson #{lesson_id}\n")

    print(f"Task:\n  {TASK}\n")
    print("Running the incident (EMAIL_MODE=dry — nothing is sent)…\n")
    result = run_incident("demo_lesson_citation", TASK)

    print(f"agent_runs.id = {result.run_id}  ({result.step_count} tool step(s) logged)\n")

    print("=== model reasoning containing the citation ===")
    rows = fetch_all(
        """
        SELECT DISTINCT reasoning
        FROM agent_steps
        WHERE run_id = %s AND reasoning ILIKE '%%applying lesson%%'
        """,
        (result.run_id,),
    )
    final = fetch_all("SELECT notes FROM agent_runs WHERE id = %s", (result.run_id,))
    snippets = [r[0] for r in rows] + [
        n[0] for n in final if n[0] and "applying lesson" in n[0].lower()
    ]
    if snippets:
        for s in snippets:
            print(f"  …{s.strip()[:400]}…")
    else:
        print("  (the model did not emit a citation this run)")

    print("\n=== recorded citations (step_lesson_citations) ===")
    _print_citations(result.run_id)

    print(
        "\nOpen the cockpit (uv run python ui/server.py) → Decision feed → "
        f"Run {result.run_id}: the ⓘ 'Referenced lessons' line lists the above, "
        "each linking into Manage Lessons."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
