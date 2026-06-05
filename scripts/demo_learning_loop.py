"""Demo: prove the learning loop end-to-end (BEFORE → teach → AFTER).

The whole point of the case-book is that operator feedback changes how the agent
behaves next time. This script demonstrates exactly that, in three acts, on a
real ambiguous request ("change my child's meal to something with chicken"):

  1. BASELINE — with the lesson ABSENT, run the incident for a clearly-identified
     no-restriction student (Henry Hill, Moreton Bay Boys' College). The request
     is genuinely ambiguous — the caterer has several chicken meals — so we expect
     the agent to ASK the parent which chicken meal they mean.
  2. TEACH — store the operator's lesson (as if typed into the cockpit): "for a
     clearly-identified, no-restriction student asking for a general meal type,
     pick the most popular matching meal and confirm it — don't ask which one."
  3. AFTER — with that lesson now in the case-book, run a SIMILAR incident for a
     DIFFERENT no-restriction student at a different school (Emily Wilson, Loreto
     College — caterer GyG, also chicken-rich). We expect the agent to now PICK a
     chicken meal + confirm it, and to CITE the lesson as "(applying Lesson #N)".

Finally it prints BEFORE vs AFTER side by side, plus the citation the cockpit's ⓘ
icon records — proving the behaviour change traces back to the lesson.

To keep the BEFORE genuinely "before", any prior copy of this demo's lesson is
DISABLED before the baseline run and (re)enabled only for the AFTER run, so the
script is safe to re-run and the contrast is always clean.

Sends are DRY: EMAIL_MODE=dry is forced before any import, so every email the
agent chooses to send is logged 'drafted' and NOT delivered.

Run: uv run python scripts/demo_learning_loop.py
"""

from __future__ import annotations

import os

# Force dry sends BEFORE importing settings (module-level singleton). Nothing is
# delivered; the demo is about the behaviour change, not mail.
os.environ["EMAIL_MODE"] = "dry"

import sys
import textwrap

from src.agent.loop import run_incident
from src.db.connection import fetch_all
from src.tools.casebook import set_case_active, store_case
from ui.server import _citations_for_runs

# --- The operator lesson being taught (verbatim from the cockpit comment) -----
# The sentinel tag lets the demo find/reuse its own lesson across re-runs without
# splitting recall across near-identical duplicates.
LESSON_TAG = "demo:general-meal-type"
LESSON = {
    "situation": (
        "A parent asked to change a clearly-identified student's meal to a general "
        "meal type (e.g. 'something with chicken'), and the student has no dietary "
        "restrictions."
    ),
    "decision": (
        "Don't ask the parent which specific meal they mean. Choose the most "
        "popular matching meal on that school's caterer menu and confirm THAT "
        "meal back to the parent."
    ),
    "rationale": (
        "Operator guidance: for a clearly-identified, no-restriction student a "
        "general meal-type request is not really ambiguous — every chicken meal is "
        "safe for them, so picking the popular one and confirming is faster and "
        "better service than bouncing a 'which one?' question back to the parent."
    ),
    "tags": ["meal", "preference", "chicken", "general meal type", "no restrictions", LESSON_TAG],
}

# --- The two incidents (inbound parent emails) -------------------------------
BASELINE_TASK = (
    "An inbound email just arrived from Ryan Hill, parent of Henry Hill (Year-level "
    "student at Moreton Bay Boys' College, our Lakehouse Victoria Point caterer):\n"
    '"Hi — could you please change Henry\'s dinners to something with chicken from '
    'now on? Thanks, Ryan."\n'
    "Henry has no dietary restrictions. Decide how to handle this meal-preference "
    "request and take the appropriate first steps."
)

AFTER_TASK = (
    "An inbound email just arrived from Hannah Wilson, parent of Emily Wilson "
    "(student at Loreto College, our Guzman y Gomez caterer):\n"
    '"Hello — can you switch Emily\'s dinners to something with chicken going '
    'forward? Cheers, Hannah."\n'
    "Emily has no dietary restrictions. Decide how to handle this meal-preference "
    "request and take the appropriate first steps."
)


# --- Case-book setup so BEFORE is genuinely "before" -------------------------


def _demo_lesson_ids() -> list[int]:
    """Ids of every case this demo previously stored (matched on the sentinel tag),
    newest first."""
    rows = fetch_all(
        "SELECT id FROM cases WHERE %s = ANY(tags) ORDER BY id DESC",
        (LESSON_TAG,),
    )
    return [r[0] for r in rows]


def _disable_demo_lesson() -> None:
    """Disable any prior copy of the demo lesson so the BASELINE run can't recall
    it (idempotent — safe on first run when there's nothing to disable)."""
    for case_id in _demo_lesson_ids():
        set_case_active(case_id, False)


def _enable_demo_lesson() -> int:
    """Make the lesson active for the AFTER run: re-enable the existing copy if we
    have one, otherwise store it fresh. Returns its case id."""
    existing = _demo_lesson_ids()
    if existing:
        lesson_id = existing[0]
        set_case_active(lesson_id, True)
        return lesson_id
    stored = store_case(created_by="operator (cockpit comment)", **LESSON)
    if not stored.ok:
        raise RuntimeError(f"could not store the lesson: {stored.message}")
    return stored.data["case_id"]


# --- Reading back what an incident actually did ------------------------------


def _steps(run_id: int) -> list[dict]:
    """The tool calls an incident logged, in order, as dicts."""
    rows = fetch_all(
        """
        SELECT step_index, tool_name, action_class,
               tool_output_full->>'status' AS status, tool_input
        FROM agent_steps WHERE run_id = %s ORDER BY step_index
        """,
        (run_id,),
    )
    return [
        {"i": i, "tool": t, "klass": k, "status": s, "input": inp}
        for (i, t, k, s, inp) in rows
    ]


def _drafted_emails(run_id: int) -> list[dict]:
    """The emails this incident drafted (DRY: drafted, never sent)."""
    rows = fetch_all(
        """
        SELECT email_type, status, intended_to_address, subject, rendered_body
        FROM outbound_emails WHERE related_run_id = %s ORDER BY id
        """,
        (run_id,),
    )
    return [
        {"type": t, "status": s, "to": to, "subject": subj, "body": body}
        for (t, s, to, subj, body) in rows
    ]


def _citation_lines(run_id: int) -> list[str]:
    """One human line per recorded lesson citation for this run (what the feed's ⓘ
    icon shows)."""
    cites = _citations_for_runs([run_id]).get(run_id, [])
    out = []
    for c in cites:
        where = f"step {c['step_index']}" if c["step_index"] is not None else "final answer"
        why = c["reason"] or "—"
        out.append(f"Lesson #{c['case_id']} (cited at {where}) — why: {why}")
    return out


# --- Rendering ---------------------------------------------------------------

_COL = 52


def _decision_block(run_id: int, final_text: str) -> str:
    """A compact, readable summary of one incident's decision: the actions it took,
    any email it drafted, and its closing answer."""
    lines: list[str] = []

    steps = _steps(run_id)
    if steps:
        lines.append("Actions:")
        for s in steps:
            lines.append(f"  • {s['tool']} [{s['klass']}] → {s['status']}")
    else:
        lines.append("Actions: (none — answered in text only)")

    emails = _drafted_emails(run_id)
    if emails:
        lines.append("")
        lines.append("Email drafted (DRY — not sent):")
        for e in emails:
            lines.append(f"  → {e['to']}  [{e['type']}/{e['status']}]")
            lines.append(f"    Subj: {e['subject']}")
            body = " ".join((e["body"] or "").split())
            lines.append(f"    {body[:200]}{'…' if len(body) > 200 else ''}")

    lines.append("")
    lines.append("Decision (final answer):")
    lines.append(final_text.strip() or "(no final text)")
    return "\n".join(lines)


def _side_by_side(left_title: str, left: str, right_title: str, right: str) -> str:
    """Render two text blocks in two wrapped columns separated by a gutter."""

    def wrap(block: str) -> list[str]:
        out: list[str] = []
        for raw in block.splitlines() or [""]:
            if not raw:
                out.append("")
                continue
            wrapped = textwrap.wrap(
                raw, width=_COL, subsequent_indent="    ", break_long_words=False
            )
            out.extend(wrapped or [""])
        return out

    lcol, rcol = wrap(left), wrap(right)
    rows = max(len(lcol), len(rcol))
    lcol += [""] * (rows - len(lcol))
    rcol += [""] * (rows - len(rcol))

    bar = "─" * _COL
    head = f"{left_title:<{_COL}} │ {right_title}"
    sep = f"{bar} │ {bar}"
    body = "\n".join(f"{l:<{_COL}} │ {r}" for l, r in zip(lcol, rcol))
    return f"{head}\n{sep}\n{body}"


def _h(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> int:
    _h("LEARNING-LOOP DEMO  (EMAIL_MODE=dry — nothing is sent)")
    print(
        "Proving operator feedback changes the agent's next decision on the same\n"
        "kind of ambiguous request ('change the meal to something with chicken')."
    )

    # --- 1. BASELINE (lesson absent) -----------------------------------------
    _h("1. BASELINE — lesson NOT in the case-book")
    _disable_demo_lesson()
    print("Disabled any prior copy of the demo lesson so this run can't recall it.\n")
    print(f"Incident:\n{textwrap.indent(BASELINE_TASK, '  ')}\n")
    print(
        "Running… (expect: with no lesson the agent has no steer toward the single\n"
        "popular pick — it resolves the ambiguity its own way: ask the parent which\n"
        "chicken meal, or set ALL the chicken options. It does NOT decisively pick\n"
        "the most popular one and cite a precedent.)\n"
    )
    before = run_incident("demo_learning_loop:baseline", BASELINE_TASK)
    print(f"agent_runs.id = {before.run_id}  ({before.step_count} tool step(s))")
    before_block = _decision_block(before.run_id, before.final_text)

    # --- 2. TEACH ------------------------------------------------------------
    _h("2. TEACH — store the operator's lesson")
    lesson_id = _enable_demo_lesson()
    print(f"Stored/enabled Lesson #{lesson_id} in the case-book:")
    print(textwrap.indent(f"situation: {LESSON['situation']}", "  "))
    print(textwrap.indent(f"decision : {LESSON['decision']}", "  "))

    # --- 3. AFTER (lesson present) -------------------------------------------
    _h("3. AFTER — same kind of request, DIFFERENT student, lesson now active")
    print(f"Incident:\n{textwrap.indent(AFTER_TASK, '  ')}\n")
    print(
        f"Running… (expect: PICK a popular chicken meal + confirm, citing Lesson #{lesson_id})\n"
    )
    after = run_incident("demo_learning_loop:after", AFTER_TASK)
    print(f"agent_runs.id = {after.run_id}  ({after.step_count} tool step(s))")
    after_block = _decision_block(after.run_id, after.final_text)

    # --- 4. BEFORE vs AFTER + citation ---------------------------------------
    _h("4. BEFORE vs AFTER")
    print(
        _side_by_side(
            f"BEFORE — no lesson (run {before.run_id})",
            before_block,
            f"AFTER — Lesson #{lesson_id} active (run {after.run_id})",
            after_block,
        )
    )

    _h("RECORDED CITATION (what the cockpit's ⓘ 'Referenced lessons' shows)")
    cites = _citation_lines(after.run_id)
    if cites:
        for line in cites:
            print(f"  ✓ {line}")
        print(
            f"\nThe AFTER decision is traceably attributed to Lesson #{lesson_id}: "
            "the agent learned."
        )
    else:
        print("  (no citation was recorded on the AFTER run)")
        print(
            f"\nThe agent did not emit '(applying Lesson #{lesson_id}: …)' this run, so "
            "nothing was recorded.\n  Compare the BEFORE/AFTER decisions above for the "
            "behaviour change; re-run to retry the citation."
        )

    print("\nNothing was sent (EMAIL_MODE=dry); every email above is a logged draft.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
