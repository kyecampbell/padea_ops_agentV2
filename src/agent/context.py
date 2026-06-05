"""Per-task context assembly.

Responsibility: build the context window the orchestrator reasons over for a
single incident. Combines, in priority order:
  - the handbook core (always-on hard invariants from config/handbook.md),
  - the operator's ACTIVE policies (authoritative business rules from the
    ``policies`` table — injected like the handbook, treated as binding), and
  - the 2-3 most relevant past cases recalled from the case-book (keyword +
    recency) so prior operator guidance shapes the new run.

Keeps context focused and bounded so the agent stays grounded in current policy
and prior resolutions without drowning in irrelevant data. The result is plain
system-prompt text appended after the orchestrator's base instructions in
``loop.py``.

Citation traceability (parallel for lessons and policies): each recalled case is
surfaced as ``[Lesson #<id>]`` and each active policy as ``[Policy #<id>]`` so the
model can reference them by number, and the handbook asks the model to note
``(applying Lesson #<id>: <why>)`` / ``(applying Policy #<id>: <why>)`` whenever
one actually shaped a decision. ``assemble_context`` returns the recalled-lesson
ids AND the active-policy ids alongside the text so ``loop.py`` can validate
citations against what was really in context; ``parse_lesson_citations`` /
``parse_policy_citations`` extract the cited ids + reasons for persistence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from config.settings import PROJECT_ROOT
from src.tools.casebook import recall_cases
from src.tools.policybook import active_policies

# How many recalled cases to fold into context. Small on purpose (the spec's
# "2-3 most relevant") so a couple of prior decisions guide the run without
# crowding out the live task.
_RECALL_LIMIT = 3

# How much of a case's free-text fields to keep, so one verbose case can't blow
# the context budget.
_FIELD_CLIP = 600

# The citation convention the model is asked to follow when a recalled lesson
# shapes a decision: "(applying Lesson #<id>: <why>)". This regex pulls the id and
# the why back out of the reasoning so we can persist what was cited. Tolerant of
# casing, an optional "#", and a ":"/"-"/"—" separator; the why runs to the
# closing paren.
_CITATION_RE = re.compile(
    r"applying\s+lesson\s+#?(\d+)\s*[:\-–—]?\s*([^)]*)",
    re.IGNORECASE,
)

# The same convention for an authoritative policy: "(applying Policy #<id>:
# <why>)". Parallel regex so ``loop.py`` can persist which policies were applied.
_POLICY_CITATION_RE = re.compile(
    r"applying\s+policy\s+#?(\d+)\s*[:\-–—]?\s*([^)]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TaskContext:
    """The assembled per-task context: the system-prompt text plus the ids that
    were put into it — the cases actually recalled AND the active policies
    injected — so ``loop.py`` can validate the model's lesson / policy citations
    against what was genuinely surfaced."""

    system_text: str
    recalled_case_ids: tuple[int, ...]
    active_policy_ids: tuple[int, ...]


def _parse_citations(pattern: re.Pattern[str], text: str | None) -> list[tuple[int, str]]:
    """Extract ``(applying <kind> #<id>: <why>)`` citations matching ``pattern``.

    Returns ``(id, why)`` pairs in order of appearance, de-duplicated on ``id``
    (first mention wins). The ``why`` is trimmed; empty when no reason was given.
    Caller filters these against the ids that were actually in context, so a
    hallucinated number never becomes a stored citation."""
    if not text:
        return []
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in pattern.finditer(text):
        ident = int(match.group(1))
        if ident in seen:
            continue
        seen.add(ident)
        out.append((ident, match.group(2).strip()))
    return out


def parse_lesson_citations(text: str | None) -> list[tuple[int, str]]:
    """Extract ``(applying Lesson #<id>: <why>)`` citations from model reasoning.

    Returns ``(case_id, why)`` pairs (see ``_parse_citations``)."""
    return _parse_citations(_CITATION_RE, text)


def parse_policy_citations(text: str | None) -> list[tuple[int, str]]:
    """Extract ``(applying Policy #<id>: <why>)`` citations from model reasoning.

    Returns ``(policy_id, why)`` pairs (see ``_parse_citations``)."""
    return _parse_citations(_POLICY_CITATION_RE, text)


def _handbook_core() -> str:
    """The always-on handbook text (empty string if it can't be read)."""
    try:
        return (PROJECT_ROOT / "config" / "handbook.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _clip(text: str | None) -> str:
    """Trim a free-text field to a bounded length for prompt inclusion."""
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= _FIELD_CLIP else text[: _FIELD_CLIP - 1].rstrip() + "…"


def _format_case(case: dict) -> str:
    """Render one recalled case as a compact prompt bullet.

    Leads with a visible ``[Lesson #<id>]`` marker so the model can cite the
    lesson by number (see the handbook's citation convention)."""
    lines = [f"- [Lesson #{case['id']}] {_clip(case.get('situation'))}"]
    decision = _clip(case.get("decision"))
    rationale = _clip(case.get("rationale"))
    if decision:
        lines.append(f"  Decision: {decision}")
    if rationale:
        lines.append(f"  Rationale: {rationale}")
    tags = case.get("tags") or []
    if tags:
        lines.append(f"  Tags: {', '.join(tags)}")
    return "\n".join(lines)


def _active_policies_block() -> tuple[str, tuple[int, ...]]:
    """The "operator policies" section plus the ids it injected.

    Every ACTIVE policy is included (this is authoritative standing policy, not
    similarity-recalled), each tagged with a visible ``[Policy #<id>]`` marker.
    Returns ``(text, policy_ids)`` — ``("", ())`` when the policy-book is empty
    (the start state) or unreadable; a missing policy section must never block a
    run, and an empty policy-book is the normal blank-slate case.
    """
    result = active_policies()
    if not result.ok or not result.data:
        return "", ()

    policy_ids = tuple(int(p["id"]) for p in result.data)
    policies = "\n".join(
        f"- [Policy #{p['id']}] {_clip(p.get('text'))}" for p in result.data
    )
    text = (
        "# Operator policies (authoritative)\n\n"
        "These are authoritative business rules the operator has set, each tagged "
        "with a visible policy number. Treat them as BINDING standing policy — if a "
        "policy here answers the question, follow it (they override your default "
        "judgment, though never the hard safety/approval invariants in the handbook "
        "above). When a policy actually governs a decision, cite it inline in your "
        "reasoning as `(applying Policy #<id>: <why>)` so the operator can see which "
        "rule you applied — ALWAYS include the colon and a brief why (e.g. "
        "`(applying Policy #3: operator caps autonomous goodwill credits at $20)`), "
        "never a bare `(applying Policy #3)`:\n\n"
        f"{policies}"
    )
    return text, policy_ids


def _recalled_cases_block(
    task: str,
    related_caterer_id: int | None,
    related_enrolment_id: int | None,
) -> tuple[str, tuple[int, ...]]:
    """The "relevant past cases" section plus the ids it surfaced.

    Returns ``(text, recalled_ids)`` — ``("", ())`` when there's nothing to recall.
    Recall failures (DB down, empty case-book) degrade silently to no section —
    missing prior guidance must never block a run.
    """
    result = recall_cases(
        query=task,
        related_caterer_id=related_caterer_id,
        related_enrolment_id=related_enrolment_id,
        limit=_RECALL_LIMIT,
    )
    if not result.ok or not result.data:
        return "", ()

    recalled_ids = tuple(int(case["id"]) for case in result.data)
    cases = "\n".join(_format_case(case) for case in result.data)
    text = (
        "# Relevant past cases\n\n"
        "Operator-trained guidance from similar situations, each tagged with a "
        "visible lesson number. Treat these as precedent — follow them unless the "
        "current facts clearly differ. When a lesson here actually shapes a "
        "decision, cite it inline in your reasoning as "
        "`(applying Lesson #<id>: <why>)` so the operator can see which precedent "
        "you used — ALWAYS include the colon and a brief why (e.g. "
        "`(applying Lesson #5: operator wants the parent acknowledged first)`), "
        "never a bare `(applying Lesson #5)`:\n\n"
        f"{cases}"
    )
    return text, recalled_ids


def assemble_context(
    task: str,
    *,
    related_caterer_id: int | None = None,
    related_enrolment_id: int | None = None,
) -> TaskContext:
    """Build the per-task context: handbook core + active policies + the most
    relevant cases.

    Returns a ``TaskContext`` carrying the system-prompt text (to append after the
    orchestrator's base instructions), the ids of the cases actually recalled, and
    the ids of the active policies injected. Ordering is deliberate — handbook
    (hard invariants) first, then operator policies (authoritative business
    rules), then recalled precedent. The handbook is always included (when
    readable); the policies section appears only when the policy-book has active
    rules (it starts empty); the recalled-cases section only when there's relevant
    precedent. ``related_*`` ids, when known, sharpen case recall.
    """
    sections: list[str] = []

    handbook = _handbook_core()
    if handbook:
        sections.append(f"# Always-on handbook\n\n{handbook}")

    policies_block, policy_ids = _active_policies_block()
    if policies_block:
        sections.append(policies_block)

    cases_block, recalled_ids = _recalled_cases_block(
        task, related_caterer_id, related_enrolment_id
    )
    if cases_block:
        sections.append(cases_block)

    return TaskContext("\n\n".join(sections), recalled_ids, policy_ids)
