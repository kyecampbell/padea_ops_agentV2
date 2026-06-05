"""Demo: run the orchestrator loop on a read-only task.

Drives one incident end-to-end through the real read tools and the real
Anthropic model, then prints the agent's final answer and the `agent_steps` rows
the run logged (so you can see exactly which tools it called and how each typed
result came back).

Task: "List Kenko Sushi House's (caterer 3) menu and tell me which items are safe
for a gluten-free student."

Run: uv run python scripts/demo_loop.py
"""

from __future__ import annotations

import json
import sys

from src.agent.loop import run_incident
from src.db.connection import fetch_all

TASK = (
    "List Kenko Sushi House's (caterer 3) menu and tell me which items are safe "
    "for a gluten-free student."
)


def _print_steps(run_id: int) -> None:
    rows = fetch_all(
        """
        SELECT step_index, tool_name, tool_input,
               tool_output_full ->> 'status'  AS status,
               tool_output_full ->> 'message' AS message,
               urgency, action_class
        FROM agent_steps
        WHERE run_id = %s
        ORDER BY step_index
        """,
        (run_id,),
    )
    if not rows:
        print("  (no tool steps logged)")
        return
    for step_index, tool_name, tool_input, status, message, urgency, action_class in rows:
        args = json.dumps(tool_input)
        print(f"  [{step_index}] {tool_name}({args})")
        print(f"        -> status={status!r}  urgency={urgency!r}  action_class={action_class!r}")
        print(f"        {message}")


def main() -> int:
    print(f"Task:\n  {TASK}\n")
    result = run_incident("demo_read_only", TASK)

    print(f"agent_runs.id = {result.run_id}  ({result.step_count} tool step(s) logged)\n")
    print("=== agent_steps ===")
    _print_steps(result.run_id)

    print("\n=== final answer ===")
    print(result.final_text or "(no final text)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
