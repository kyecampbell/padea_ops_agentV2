"""Demo: policy-citation traceability end-to-end.

Shows the authoritative-policy layer the Policies system adds, parallel to the
lesson-citation demo:
  1. add ONE operator policy to the (empty) policy-book — a concrete business rule
     the handbook does NOT spell out, so the agent must lean on the policy itself,
  2. run a real incident whose task is governed by it — the context assembler
     injects every active policy as "[Policy #<id>]" and the handbook asks the
     model to cite it as "(applying Policy #<id>: <why>)" when it actually applies,
  3. the orchestrator parses that citation and records ONLY the cited policy
     (+ the why) in step_policy_citations, validated against what was in context,
  4. print the recorded citations exactly as the cockpit's ⓘ info icon reads them
     (via ui.server._policy_citations_for_runs), with the Manage-Policies deep link.

Sends are DRY: EMAIL_MODE=dry is forced before any import, so any email the agent
chooses to send is logged 'drafted' and NOT delivered.

Run: uv run python scripts/demo_policy_citation.py
"""

from __future__ import annotations

import os

# Force dry sends BEFORE importing settings (module-level singleton). Nothing is
# delivered; the demo is about citations, not mail.
os.environ["EMAIL_MODE"] = "dry"

import sys

from src.agent.loop import run_incident
from src.db.connection import fetch_all
from src.tools.policybook import add_policy
from ui.server import _policy_citations_for_runs

# A concrete BUSINESS rule with a number the handbook does not state — so when the
# agent decides how much goodwill to extend, the only place that $20 line exists is
# this policy, forcing it to apply (and cite) the policy rather than its own prior.
POLICY_TEXT = (
    "Goodwill: when a parent complains about a spoiled or missed catered meal, you "
    "may offer a goodwill meal credit of up to $20 autonomously as part of the "
    "apology. A credit above $20 is a money decision and must be escalated to Dylan "
    "for approval — never offer more than $20 yourself."
)

TASK = (
    "An inbound email just arrived from a parent at MacGregor State High School: "
    "\"My daughter's catered dinner last night was completely inedible — it was "
    "spoiled and she couldn't eat any of it. I'd like a refund or credit for it.\" "
    "Decide how to handle this complaint, including what goodwill (if any) to offer, "
    "and take the appropriate first steps."
)


def _print_citations(run_id: int) -> None:
    cites = _policy_citations_for_runs([run_id]).get(run_id, [])
    if not cites:
        print("  (no policies were cited as applied on this run)")
        return
    print(f"  Applied {len(cites)} polic(ies) — exactly what the feed's ⓘ icon shows:")
    for c in cites:
        where = (
            f"step {c['step_index']}" if c["step_index"] is not None else "final answer"
        )
        print(f"   • Policy #{c['policy_id']}  (cited at {where})")
        print(f"       why : {c['reason'] or '—'}")
        print(f"       link: /policies#policy-{c['policy_id']}  (Manage Policies tab)")


def main() -> int:
    print("Adding the policy to the policy-book…")
    # Reuse the policy if a prior demo run already added it (keeps the policy-book
    # from filling with near-identical duplicates).
    existing = fetch_all(
        "SELECT id FROM policies WHERE text = %s AND active = TRUE ORDER BY id LIMIT 1",
        (POLICY_TEXT,),
    )
    if existing:
        policy_id = existing[0][0]
        print(f"  reusing existing Policy #{policy_id}\n")
    else:
        added = add_policy(POLICY_TEXT)
        if not added.ok:
            print(f"  could not add the policy: {added.message}")
            return 1
        policy_id = added.data["policy_id"]
        print(f"  added Policy #{policy_id}\n")

    print(f"Task:\n  {TASK}\n")
    print("Running the incident (EMAIL_MODE=dry — nothing is sent)…\n")
    result = run_incident("demo_policy_citation", TASK)

    print(f"agent_runs.id = {result.run_id}  ({result.step_count} tool step(s) logged)\n")

    print("=== model reasoning containing the citation ===")
    rows = fetch_all(
        """
        SELECT DISTINCT reasoning
        FROM agent_steps
        WHERE run_id = %s AND reasoning ILIKE '%%applying policy%%'
        """,
        (result.run_id,),
    )
    final = fetch_all("SELECT notes FROM agent_runs WHERE id = %s", (result.run_id,))
    snippets = [r[0] for r in rows] + [
        n[0] for n in final if n[0] and "applying policy" in n[0].lower()
    ]
    if snippets:
        for s in snippets:
            print(f"  …{s.strip()[:400]}…")
    else:
        print("  (the model did not emit a citation this run)")

    print("\n=== recorded citations (step_policy_citations) ===")
    _print_citations(result.run_id)

    print(
        "\nOpen the cockpit (uv run python ui/server.py) → Decision feed → "
        f"Run {result.run_id}: the ⓘ 'Applied policies' line lists the above, "
        "each linking into Manage Policies."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
