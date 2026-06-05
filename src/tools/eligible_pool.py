"""Dietary eligible-pool tool — the first real safety intelligence.

Responsibility: decide, per student, which of their caterer's menu items are
safe to serve, persist that decision to ``student_eligible_meals``, and ESCALATE
(never guess) whenever the data is too thin to be sure. This is the spine of
"dietary students always receive a safe meal" (handbook).

How a student is classified (three distinct dietary states):
  - NO requirements (dietary_raw explicitly says so — "No requirements" — and no
    tag) -> every active menu item is eligible (needs_tweak false, rationale "no
    dietary restrictions"). No LLM call.
  - UNKNOWN (dietary_raw is blank/NULL and no tag) -> we have NOT been told this
    student is unrestricted, so we never assume any meal is safe. The pool is
    left UNPOPULATED and the student is surfaced as needing dietary confirmation.
    No fabricated safety, no LLM guess. (Distinct from "No requirements".)
  - Otherwise (a dietary tag and/or a real dietary note) claude-sonnet-4-6
    classifies EACH item against the student's requirements (dietary_raw +
    required tag names) and the item's contents_text + tweaks_text, returning
    one of:
        safe | safe_with_tweak | unsafe | uncertain

Safety rules (critical):
  * Deterministic backstop — if a required restriction is *clearly* violated by
    an ingredient stated in contents_text, the item is forced to ``unsafe`` even
    if the model called it safe. The backstop only ever downgrades toward
    unsafe; it never upgrades. (Lack of a positive item tag is NOT treated as a
    violation — that is the model's "uncertain" territory, not a hard no.)
  * NEVER mark safe by guessing about ingredients that are not stated. When the
    data is ambiguous or insufficient the verdict is ``uncertain`` -> an
    escalation, never a silent pool-as-safe.

Persistence (per verdict):
  - safe / safe_with_tweak -> student_eligible_meals (eligible=true, needs_tweak
    set, rationale).
  - unsafe                 -> student_eligible_meals (eligible=false, rationale)
                              kept as an audit row.
  - uncertain              -> an ``escalations`` row (status 'open') naming the
                              student, item, and requirement. NOT pooled.

Idempotent: a student's existing student_eligible_meals rows (and any open
dietary escalations this tool raised) are deleted before re-inserting, so the
tool is safely re-runnable.

Conventions: absolute imports; money is integer cents (not relevant here);
timestamps are timezone-aware (DB defaults to now()); reads go through the typed
query tools; this tool never raises at the caller — failures come back as typed
``ToolResult``s.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import anthropic
from psycopg.types.json import Jsonb

from config.settings import settings
from src.db.connection import get_conn
from src.tools import query
from src.tools.results import ToolResult, error, found, unavailable

MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 4096
# Bound every classification call so one slow/overloaded response can never stall
# the whole batch; the SDK retries a couple of times within this budget.
_REQUEST_TIMEOUT = 90.0
_MAX_RETRIES = 2
# Default cohort concurrency — enough to hide per-call latency (DB + LLM) without
# hammering the pooler or the API.
_DEFAULT_WORKERS = 8


def _new_client() -> anthropic.Anthropic:
    """An Anthropic client with a bounded per-request timeout and retry budget."""
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        timeout=_REQUEST_TIMEOUT,
        max_retries=_MAX_RETRIES,
    )

# The four verdicts the classifier may return for a menu item.
_VERDICTS = ("safe", "safe_with_tweak", "unsafe", "uncertain")


# --- Deterministic safety backstop -------------------------------------------
# Ingredient keywords that, when present (and not negated) in an item's
# contents_text, clearly violate a required dietary tag. This is a SAFETY NET on
# top of the LLM, not the primary classifier: it can only force a model "safe"
# verdict down to "unsafe". It is deliberately conservative — specific nut names
# rather than the bare word "nut" (so "nut-free" never trips it), and it ignores
# hedged mentions like "may contain" (those are the model's uncertain territory).
_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "no_pork": ("pork", "bacon", "ham", "prosciutto", "chorizo", "pancetta", "salami"),
    "no_beef": ("beef", "steak"),
    "no_red_meat": ("beef", "steak", "lamb", "pork", "bacon", "ham", "veal", "mutton"),
    "no_seafood": (
        "fish", "shrimp", "prawn", "shellfish", "seafood", "tuna", "salmon",
        "crab", "squid", "calamari", "anchovy", "oyster", "mussel", "scallop",
    ),
    "no_nuts": (
        "peanut", "almond", "cashew", "walnut", "hazelnut", "pecan",
        "pistachio", "macadamia",
    ),
    "no_dairy": (
        "cheese", "cream", "milk", "butter", "parmesan", "yoghurt", "yogurt", "dairy",
    ),
    "gluten_free": ("wheat", "gluten", "barley", "rye", "breadcrumb", "soy sauce"),
    "vegetarian": (
        "chicken", "beef", "steak", "pork", "fish", "bacon", "ham", "lamb",
        "shrimp", "prawn", "seafood", "meat", "mince",
    ),
    "vegan": (
        "chicken", "beef", "steak", "pork", "fish", "bacon", "ham", "lamb",
        "shrimp", "prawn", "seafood", "meat", "mince", "cheese", "cream", "milk",
        "butter", "parmesan", "egg", "honey", "dairy", "yoghurt", "yogurt",
    ),
    "halal": ("pork", "bacon", "ham", "prosciutto", "chorizo", "pancetta", "salami", "alcohol", "wine"),
}

# Phrases that, appearing just before a keyword, negate it (a claim of absence).
_NEG_BEFORE = ("no", "without", "not", "free of", "free from", "minus")
# Hedged-presence phrases — real risk, but NOT a *clear* violation, so the
# deterministic backstop stays out and lets the model return "uncertain".
_HEDGE = ("may contain", "traces of", "trace of", "may include")


def _keyword_present(contents: str, keyword: str) -> bool:
    """True if `keyword` appears in `contents` as a stated, non-negated ingredient.

    Word-boundary matched (so "fish" does not fire inside "shellfish"), and an
    occurrence is discounted when it is negated ("beef-free", "no beef") or
    merely hedged ("may contain nuts"). Returns True only on a clear, present
    mention — the bar the deterministic backstop requires.
    """
    text = contents.lower()
    for match in re.finditer(rf"\b{re.escape(keyword)}\b", text):
        start, end = match.start(), match.end()
        after = text[end:end + 6]
        before = text[max(0, start - 16):start]

        # "<kw>-free" / "<kw> free" -> a claim the item lacks it.
        if after.lstrip(" ,").startswith("free") or after.startswith("-free"):
            continue
        # "no <kw>" / "without <kw>" / "free of <kw>" just before.
        stripped = before.rstrip(" ,")
        if any(stripped.endswith(marker) for marker in _NEG_BEFORE):
            continue
        # Hedged mention ("may contain X") anywhere in the lead-up -> not clear.
        if any(hedge in before for hedge in _HEDGE):
            continue
        return True
    return False


def _deterministic_violation(contents: str | None, required_tags: list[str]) -> str | None:
    """Return the first required tag clearly violated by `contents`, else None."""
    if not contents:
        return None
    for tag in required_tags:
        for keyword in _FORBIDDEN.get(tag, ()):
            if _keyword_present(contents, keyword):
                return f"{tag} (contains '{keyword}')"
    return None


# --- Result types ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StudentPoolResult:
    """Per-student outcome of building the eligible pool. Returned inside a
    ``found`` ToolResult's data as a plain dict (see ``as_dict``)."""

    enrolment_id: int
    student_name: str
    had_requirements: bool
    used_llm: bool
    eligible_count: int        # rows written eligible=true (incl. needs_tweak)
    needs_tweak_count: int
    unsafe_count: int          # audit rows written eligible=false
    escalations_raised: int
    dietary_unknown: bool = False  # blank/NULL dietary -> pool left empty, confirm

    def as_dict(self) -> dict:
        return {
            "enrolment_id": self.enrolment_id,
            "student_name": self.student_name,
            "had_requirements": self.had_requirements,
            "used_llm": self.used_llm,
            "eligible_count": self.eligible_count,
            "needs_tweak_count": self.needs_tweak_count,
            "unsafe_count": self.unsafe_count,
            "escalations_raised": self.escalations_raised,
            "dietary_unknown": self.dietary_unknown,
        }


@dataclass
class PoolCache:
    """Optional cross-student memo for a cohort run (thread-safe).

    - ``classifications`` keys an LLM verdict map on (dietary signature, caterer)
      so two students with identical requirements at the same caterer cost one
      call, not two.
    - ``menus`` / ``caterers`` cache the per-caterer menu and per-school caterer
      reads, so the cohort hits the DB once per caterer/school, not per student.
    - ``tag_descriptions`` caches the dietary vocabulary (one read for the run).
    A lock guards all mutation so the cohort runner can fan out across threads.
    """

    classifications: dict = field(default_factory=dict)
    menus: dict = field(default_factory=dict)
    caterers: dict = field(default_factory=dict)
    tag_descriptions: dict[str, str] | None = None
    llm_calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


# --- LLM classifier ----------------------------------------------------------

_CLASSIFY_TOOL = {
    "name": "record_classifications",
    "description": "Record the dietary safety verdict for every menu item considered.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "description": "One entry per menu item, by menu_item_id.",
                "items": {
                    "type": "object",
                    "properties": {
                        "menu_item_id": {"type": "integer"},
                        "verdict": {"type": "string", "enum": list(_VERDICTS)},
                        "needs_tweak": {
                            "type": "boolean",
                            "description": "True only for safe_with_tweak (a stated tweak makes it safe).",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Brief reason grounded in the stated contents/tweaks vs the requirement.",
                        },
                    },
                    "required": ["menu_item_id", "verdict", "rationale"],
                },
            }
        },
        "required": ["classifications"],
    },
}

_CLASSIFY_SYSTEM = """\
You are the dietary-safety classifier for a school catering operator. A student
has dietary requirements; you must decide, for EACH menu item, whether it is
safe to serve. A wrong "safe" can harm a child, so err toward caution.

Classify each item as exactly one of:
  - safe            : safe to serve as-is for this student.
  - safe_with_tweak : safe only via a tweak STATED in the item's tweaks_text
                      (e.g. "vegetarian option available", "can omit bacon")
                      that removes/avoids the offending ingredient. Set
                      needs_tweak=true.
  - unsafe          : a required restriction is violated by the stated contents
                      and no stated tweak fixes it.
  - uncertain       : the contents/tweaks data is ambiguous or insufficient to
                      be sure (e.g. "may contain nuts" for a nut-free student, or
                      an ingredient that is neither clearly present nor clearly
                      absent).

Hard rules:
  * NEVER mark an item "safe" by guessing about ingredients that are not stated.
    If you are not sure, use "uncertain" — do NOT pool it as safe.
  * Only use "safe_with_tweak" when the tweaks_text genuinely removes the
    offending ingredient for THIS requirement.
  * Reason over EVERY requirement the student has; an item must satisfy all of
    them to be safe.
Return your answer by calling record_classifications with one entry per item."""


def _classify(
    client: anthropic.Anthropic,
    *,
    dietary_raw: str | None,
    required_tags: list[str],
    tag_descriptions: dict[str, str],
    items: list[dict],
) -> dict[int, dict]:
    """Ask the model to classify every item; return {menu_item_id: verdict-dict}.

    Items the model fails to return are defaulted to ``uncertain`` (safe choice).
    Raises only on a genuine API failure — the caller maps that to ``unavailable``.
    """
    req_lines = []
    for tag in required_tags:
        desc = tag_descriptions.get(tag)
        req_lines.append(f"  - {tag}" + (f" ({desc})" if desc else ""))
    requirements_block = "\n".join(req_lines) if req_lines else "  (none tagged)"

    item_lines = []
    for it in items:
        satisfies = ", ".join(it.get("dietary_tag_names") or []) or "none recorded"
        item_lines.append(
            f"- id={it['id']} | {it['name']}\n"
            f"    contents: {it.get('contents_text') or '(none given)'}\n"
            f"    tweaks: {it.get('tweaks_text') or '(none)'}\n"
            f"    item certified to satisfy: {satisfies}"
        )
    items_block = "\n".join(item_lines)

    user = (
        f"Student's raw dietary note: {dietary_raw or '(none)'}\n"
        f"Required dietary restrictions (the student needs ALL of these):\n"
        f"{requirements_block}\n\n"
        f"Menu items to classify:\n{items_block}"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=_MAX_TOKENS,
        system=_CLASSIFY_SYSTEM,
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "record_classifications"},
        messages=[{"role": "user", "content": user}],
    )

    verdicts: dict[int, dict] = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_classifications":
            for entry in block.input.get("classifications", []):
                try:
                    item_id = int(entry["menu_item_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                verdict = entry.get("verdict")
                if verdict not in _VERDICTS:
                    verdict = "uncertain"
                verdicts[item_id] = {
                    "verdict": verdict,
                    "needs_tweak": bool(entry.get("needs_tweak", False)),
                    "rationale": (entry.get("rationale") or "").strip(),
                }

    # Any item the model skipped is treated as uncertain — never silently safe.
    for it in items:
        verdicts.setdefault(
            it["id"],
            {
                "verdict": "uncertain",
                "needs_tweak": False,
                "rationale": "Model did not return a verdict for this item.",
            },
        )
    return verdicts


# --- Persistence -------------------------------------------------------------


def _persist(
    *,
    enrolment_id: int,
    student_name: str,
    dietary_raw: str | None,
    required_tags: list[str],
    items_by_id: dict[int, dict],
    verdicts: dict[int, dict],
    run_id: int | None,
) -> StudentPoolResult:
    """Write the pool for one student inside a single transaction (idempotent).

    Deletes the student's prior eligible-meal rows and prior open dietary
    escalations this tool raised, then inserts fresh rows / escalations from the
    (backstop-adjusted) verdicts.
    """
    eligible = needs_tweak = unsafe = escalated = 0
    sem_rows: list[tuple] = []          # (enrolment_id, menu_item_id, eligible, needs_tweak, rationale)
    escalation_rows: list[tuple] = []   # (run_id, question, context, related_enrolment_id)

    for item_id, item in items_by_id.items():
        v = verdicts[item_id]
        verdict, rationale = v["verdict"], v["rationale"]
        tweak = v["needs_tweak"]

        # Deterministic backstop: if the model called it plainly safe but a
        # required restriction is clearly violated by the stated contents,
        # force unsafe. (We leave safe_with_tweak to the model — the tweak is
        # presumed to remove the offending ingredient.)
        if verdict == "safe":
            violated = _deterministic_violation(item.get("contents_text"), required_tags)
            if violated:
                verdict = "unsafe"
                tweak = False
                rationale = (
                    f"Deterministic safety backstop: {violated}; "
                    f"overrode model verdict 'safe'. ({rationale})"
                ).strip()

        if verdict in ("safe", "safe_with_tweak"):
            row_tweak = verdict == "safe_with_tweak" or tweak
            sem_rows.append((enrolment_id, item_id, True, row_tweak, rationale or None))
            eligible += 1
            if row_tweak:
                needs_tweak += 1
        elif verdict == "unsafe":
            sem_rows.append((enrolment_id, item_id, False, False, rationale or None))
            unsafe += 1
        else:  # uncertain -> escalate, do NOT pool
            req_text = ", ".join(required_tags) if required_tags else (dietary_raw or "unspecified")
            question = (
                f"Is '{item['name']}' safe for {student_name} "
                f"(requires: {req_text})? Menu data is ambiguous: "
                f"{rationale or 'insufficient information to be sure.'}"
            )
            context = {
                "kind": "dietary_uncertain",
                "enrolment_id": enrolment_id,
                "student_name": student_name,
                "menu_item_id": item_id,
                "menu_item_name": item["name"],
                "dietary_raw": dietary_raw,
                "required_tags": required_tags,
                "item_contents_text": item.get("contents_text"),
                "item_tweaks_text": item.get("tweaks_text"),
                "model_rationale": rationale,
            }
            escalation_rows.append((run_id, question, Jsonb(context), enrolment_id))
            escalated += 1

    with get_conn() as conn, conn.cursor() as cur:
        # Idempotency: clear this student's prior pool + our prior open dietary
        # escalations, so a re-run replaces rather than duplicates.
        cur.execute(
            "DELETE FROM student_eligible_meals WHERE enrolment_id = %s",
            (enrolment_id,),
        )
        cur.execute(
            """
            DELETE FROM escalations
            WHERE related_enrolment_id = %s
              AND status = 'open'
              AND context->>'kind' = 'dietary_uncertain'
            """,
            (enrolment_id,),
        )
        if sem_rows:
            cur.executemany(
                """
                INSERT INTO student_eligible_meals
                    (enrolment_id, menu_item_id, eligible, needs_tweak, rationale)
                VALUES (%s, %s, %s, %s, %s)
                """,
                sem_rows,
            )
        if escalation_rows:
            cur.executemany(
                """
                INSERT INTO escalations
                    (run_id, question, context, status, related_enrolment_id)
                VALUES (%s, %s, %s, 'open', %s)
                """,
                escalation_rows,
            )
        conn.commit()

    return StudentPoolResult(
        enrolment_id=enrolment_id,
        student_name=student_name,
        had_requirements=bool(required_tags or (dietary_raw or "").strip()),
        used_llm=True,
        eligible_count=eligible,
        needs_tweak_count=needs_tweak,
        unsafe_count=unsafe,
        escalations_raised=escalated,
    )


# --- Public API --------------------------------------------------------------


# dietary_raw values that EXPLICITLY assert no dietary restrictions (matched
# case-insensitively after stripping). A student carrying one of these — and no
# dietary tag — takes the fast path: every active menu item is eligible. This is
# DISTINCT from a blank/NULL dietary_raw, which means "we don't yet know" and is
# never assumed safe (see ``_dietary_state``).
_NO_REQUIREMENT_VALUES = frozenset(
    {
        "no requirements", "no requirement", "no dietary requirements",
        "no dietary requirement", "no dietary needs", "no restrictions",
        "no dietary restrictions", "none", "no", "n/a", "na", "nil",
    }
)

# The three dietary states a student's data can be in.
_STATE_NONE = "no_requirements"        # explicitly unrestricted -> all items safe
_STATE_UNKNOWN = "unknown"             # blank/NULL dietary, no tag -> must confirm
_STATE_REQUIREMENTS = "requirements"   # tag and/or a real note -> classify


def _dietary_state(enrolment: dict) -> str:
    """Classify a student's dietary data into one of three states.

    - Any dietary tag, or a free-text note that is NOT an explicit "no
      requirements" value -> ``requirements`` (the LLM classifies each item).
    - An explicit "no requirements" note (and no tag) -> ``no_requirements``
      (fast path: every active item eligible).
    - A blank/NULL dietary_raw and no tag -> ``unknown``: we have NOT been told
      this student is unrestricted, so we never assume any meal is safe. The
      pool is left unpopulated and the student is surfaced for confirmation.

    (Opted-out notes never reach the active cohort.)
    """
    if enrolment.get("dietary_tag_names"):
        return _STATE_REQUIREMENTS
    raw = (enrolment.get("dietary_raw") or "").strip()
    if not raw:
        return _STATE_UNKNOWN
    if raw.lower() in _NO_REQUIREMENT_VALUES:
        return _STATE_NONE
    return _STATE_REQUIREMENTS


def _caterer_for_school(school_id: int, cache: PoolCache | None) -> ToolResult:
    """Cached lookup of a school's caterer (one read per school for a cohort)."""
    if cache is not None:
        with cache.lock:
            hit = cache.caterers.get(school_id)
        if hit is not None:
            return hit
    res = query.get_caterer_for_school(school_id)
    if cache is not None and res.ok:
        with cache.lock:
            cache.caterers[school_id] = res
    return res


def _menu_for_caterer(caterer_id: int, cache: PoolCache | None) -> ToolResult:
    """Cached lookup of a caterer's active menu (one read per caterer)."""
    if cache is not None:
        with cache.lock:
            hit = cache.menus.get(caterer_id)
        if hit is not None:
            return hit
    res = query.get_menu_items(caterer_id)
    if cache is not None and res.ok:
        with cache.lock:
            cache.menus[caterer_id] = res
    return res


def compute_eligible_meals(
    enrolment_id: int,
    *,
    run_id: int | None = None,
    client: anthropic.Anthropic | None = None,
    cache: PoolCache | None = None,
    enrolment: dict | None = None,
) -> ToolResult:
    """Compute and persist the dietary-safe pool for one student.

    Reads the student and their school's caterer's active menu via the query
    tools, classifies each item (no LLM when the student has no requirements),
    applies the deterministic safety backstop, and writes
    ``student_eligible_meals`` rows / ``escalations``. Idempotent.

    ``enrolment`` may be supplied (e.g. from ``list_active_enrolments``) to skip
    the per-student read; it must carry id, school_id, student_name, dietary_raw,
    and dietary_tag_names. ``cache`` memoises caterer/menu reads and LLM verdicts
    across a cohort and makes the call safe to run from multiple threads.

    Returns ``found`` with a ``StudentPoolResult`` dict on success; ``empty`` /
    ``unavailable`` / ``error`` (propagated from the reads) otherwise.
    """
    if enrolment is None:
        enrolment_res = query.get_enrolment(enrolment_id)
        if not enrolment_res.ok:
            return enrolment_res  # empty / unavailable / error — pass through.
        enrolment = enrolment_res.data
    student_name = enrolment["student_name"]

    caterer_res = _caterer_for_school(enrolment["school_id"], cache)
    if not caterer_res.ok:
        return caterer_res
    caterer_id = caterer_res.data["id"]

    menu_res = _menu_for_caterer(caterer_id, cache)
    if not menu_res.ok:
        # No menu (empty) or DB down (unavailable/error) — cannot build a pool.
        return menu_res
    items = menu_res.data
    items_by_id = {it["id"]: it for it in items}

    required_tags = list(enrolment.get("dietary_tag_names") or [])
    dietary_raw = enrolment.get("dietary_raw")

    state = _dietary_state(enrolment)

    # --- Fast path: EXPLICITLY no requirements -> everything eligible, no LLM. ---
    if state == _STATE_NONE:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM student_eligible_meals WHERE enrolment_id = %s",
                (enrolment_id,),
            )
            cur.executemany(
                """
                INSERT INTO student_eligible_meals
                    (enrolment_id, menu_item_id, eligible, needs_tweak, rationale)
                VALUES (%s, %s, TRUE, FALSE, %s)
                """,
                [(enrolment_id, item_id, "no dietary restrictions") for item_id in items_by_id],
            )
            conn.commit()
        result = StudentPoolResult(
            enrolment_id=enrolment_id,
            student_name=student_name,
            had_requirements=False,
            used_llm=False,
            eligible_count=len(items_by_id),
            needs_tweak_count=0,
            unsafe_count=0,
            escalations_raised=0,
        )
        return found(result.as_dict(), f"{student_name}: no restrictions, {len(items_by_id)} eligible.")

    # --- Unknown dietary: blank/NULL note, no tag. We have NOT been told this
    # student is unrestricted, so we never fabricate safety. Leave the pool
    # UNPOPULATED (clearing any stale rows, idempotently) and report it as
    # dietary-unknown so the batch surfaces it as a confirmation gap. No LLM. ---
    if state == _STATE_UNKNOWN:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM student_eligible_meals WHERE enrolment_id = %s",
                (enrolment_id,),
            )
            conn.commit()
        result = StudentPoolResult(
            enrolment_id=enrolment_id,
            student_name=student_name,
            had_requirements=False,
            used_llm=False,
            eligible_count=0,
            needs_tweak_count=0,
            unsafe_count=0,
            escalations_raised=0,
            dietary_unknown=True,
        )
        return found(
            result.as_dict(),
            f"{student_name}: dietary unknown — pool left empty pending confirmation.",
        )

    # --- Dietary path: classify each item with the LLM (cached if possible). ---
    if cache is not None and cache.tag_descriptions is not None:
        tag_descriptions = cache.tag_descriptions
    else:
        tag_descriptions = _load_tag_descriptions()
        if cache is not None:
            cache.tag_descriptions = tag_descriptions

    cache_key = (dietary_raw or "", tuple(sorted(required_tags)), caterer_id)
    verdicts: dict[int, dict] | None = None
    if cache is not None:
        with cache.lock:
            verdicts = cache.classifications.get(cache_key)

    if verdicts is None:
        if client is None:
            client = _new_client()
        try:
            verdicts = _classify(
                client,
                dietary_raw=dietary_raw,
                required_tags=required_tags,
                tag_descriptions=tag_descriptions,
                items=items,
            )
        except anthropic.APIError as exc:
            return unavailable(f"Classifier unavailable for {student_name}: {exc}")
        except Exception as exc:  # backstop — never raise at the caller.
            return error(f"Classification failed for {student_name}: {exc!r}")
        if cache is not None:
            with cache.lock:
                cache.classifications[cache_key] = verdicts
                cache.llm_calls += 1

    result = _persist(
        enrolment_id=enrolment_id,
        student_name=student_name,
        dietary_raw=dietary_raw,
        required_tags=required_tags,
        items_by_id=items_by_id,
        verdicts=verdicts,
        run_id=run_id,
    )
    return found(
        result.as_dict(),
        f"{student_name}: {result.eligible_count} eligible "
        f"({result.needs_tweak_count} need a tweak), {result.unsafe_count} unsafe, "
        f"{result.escalations_raised} escalated.",
    )


def recompute_eligible_meals(enrolment_id: int, *, run_id: int | None = None) -> ToolResult:
    """Tool entry point: recompute and persist one student's dietary-safe pool.

    A thin, agent-facing wrapper over ``compute_eligible_meals`` (single-student,
    no cohort cache). Idempotent and autonomous — recomputing dietary safety is a
    reversible safety calculation, so the hard-rules gate marks it ``autonomous``
    (see ``src.agent.gates``). Returns the same typed ``ToolResult``.
    """
    return compute_eligible_meals(enrolment_id, run_id=run_id)


def _load_tag_descriptions() -> dict[str, str]:
    """Map dietary tag name -> description (best-effort; empty on failure)."""
    res = query.get_all_dietary_tags()
    if not res.ok:
        return {}
    return {t["name"]: (t.get("description") or t.get("label") or "") for t in res.data}


def compute_for_school(
    school_id: int,
    *,
    run_id: int | None = None,
    client: anthropic.Anthropic | None = None,
    cache: PoolCache | None = None,
    max_workers: int = _DEFAULT_WORKERS,
) -> ToolResult:
    """Build eligible pools for every active student at one school.

    Students are processed concurrently (``max_workers`` threads) so per-student
    DB and LLM latency overlaps; pass a shared ``cache`` to dedupe caterer/menu
    reads and identical-requirement classifications across the whole run.

    Returns ``found`` with an aggregate summary and the per-student results, or
    the read failure (``empty`` / ``unavailable`` / ``error``) from listing the
    school's enrolments.
    """
    listing = query.list_active_enrolments(school_id)
    if not listing.ok:
        return listing
    if cache is None:
        cache = PoolCache()
    if client is None:
        client = _new_client()

    def _one(enrolment: dict) -> tuple[dict, ToolResult]:
        return enrolment, compute_eligible_meals(
            enrolment["id"], run_id=run_id, client=client, cache=cache, enrolment=enrolment
        )

    per_student: list[dict] = []
    failures: list[dict] = []
    workers = max(1, min(max_workers, len(listing.data)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for enrolment, res in pool.map(_one, listing.data):
            if res.ok:
                per_student.append(res.data)
            else:
                failures.append(
                    {"enrolment_id": enrolment["id"], "status": res.status, "message": res.message}
                )

    summary = {
        "school_id": school_id,
        "students_processed": len(per_student),
        "students_failed": len(failures),
        "failures": failures,
        "results": per_student,
        "llm_calls": cache.llm_calls,
    }
    return found(summary, f"School {school_id}: {len(per_student)} student(s) processed.")
