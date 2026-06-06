"""Unit tests for the feedback-driven re-execution router.

Covers ``decide_route`` — the pure (no LLM, no DB) core that maps a comment's
classified intent + its redo-chain depth + whether it's a rejection to the action
the sweep takes. The bounded-redo cap and the intent branches live here; the DB /
LLM / gate wiring is exercised by ``scripts/dry_run_feedback_rerun.py``.

Run: uv run python -m pytest tests/test_feedback.py -q
"""

from __future__ import annotations

from src.agent.feedback import Route, _REDO_CAP, decide_route


# --- Instruction / rejection -> re-run (until the cap) ------------------------


def test_instruction_reruns():
    assert decide_route("INSTRUCTION", depth=0, was_rejection=False) == Route(
        rerun=True, lesson=False, escalate=None
    )


def test_rejection_defaults_to_rerun_even_if_text_unclear():
    # A rejection-with-explanation is actionable regardless of free-text intent.
    assert decide_route("UNCLEAR", depth=0, was_rejection=True).rerun is True


def test_both_reruns_and_keeps_lesson():
    route = decide_route("BOTH", depth=1, was_rejection=True)
    assert route.rerun is True and route.lesson is True and route.escalate is None


# --- Lesson-only -> case-book, no redo ----------------------------------------


def test_lesson_only_no_rerun():
    assert decide_route("LESSON", depth=0, was_rejection=False) == Route(
        rerun=False, lesson=True, escalate=None
    )


# --- Unclear (not a rejection) -> escalate to ask -----------------------------


def test_unclear_escalates():
    assert decide_route("UNCLEAR", depth=0, was_rejection=False) == Route(
        rerun=False, lesson=False, escalate="unclear"
    )


# --- Bounded redo: the cap escalates "stuck" instead of looping ---------------


def test_under_cap_still_reruns():
    # depth 0 and 1 are allowed (two redo attempts) when cap == 2.
    assert decide_route("INSTRUCTION", depth=_REDO_CAP - 1, was_rejection=True).rerun is True


def test_at_cap_escalates_stuck_not_rerun():
    route = decide_route("INSTRUCTION", depth=_REDO_CAP, was_rejection=True)
    assert route.rerun is False and route.escalate == "stuck"


def test_cap_hit_with_both_still_captures_lesson():
    # Hitting the cap shouldn't drop a general lesson the operator also gave.
    route = decide_route("BOTH", depth=_REDO_CAP, was_rejection=True)
    assert route.rerun is False and route.escalate == "stuck" and route.lesson is True


def test_two_attempts_then_stuck_progression():
    # depth 0 -> rerun (attempt 1), depth 1 -> rerun (attempt 2), depth 2 -> stuck.
    assert decide_route("INSTRUCTION", 0, True).rerun is True
    assert decide_route("INSTRUCTION", 1, True).rerun is True
    assert decide_route("INSTRUCTION", 2, True).escalate == "stuck"


if __name__ == "__main__":
    # Runnable without pytest: exercise every test_* in this module.
    import sys

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
