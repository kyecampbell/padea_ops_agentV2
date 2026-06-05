"""Monday per-caterer QUALITY SUMMARY — the warm "scorecard from a partner".

Once a week each caterer gets a human, appreciative scorecard (NOT a report):
genuine specific praise first, then STUDENT satisfaction per school as the
headline, the recurring (noise-filtered) themes behind it, a gentle manager-
reliability service note, and a capacity ask ONLY for a clean strong performer.

Design principles (encoded here, deterministic + provable):
  - PRAISE FIRST, and only TRUE praise (assembled from real positives: dietary
    correct, a standout school, clean reliability).
  - SPECIFIC OVER SCORE — every line is tied to a real number (per-school average,
    counts, failed-check counts).
  - FILTER NOISE — student free-text is aggregated and only RECURRING themes
    (>= RECURRING_MIN distinct mentions) are surfaced; one-offs ("soft drinks pls")
    are dropped. The agent may refine these (pass ``themes=``), else the frequency
    filter decides.
  - FORGIVING + PROPORTIONAL — tone stays warm; a real concern (a dietary-miss
    pattern, weak scores) tempers it and withholds the capacity ask, but the
    summary never issues a warning. A SUSTAINED decline is escalated to a human by
    the SEPARATE quality-review path — this email never goes commercial.

GATING: the ``caterer_weekly_summary`` kind is autonomous (gates.classify_email),
but the commercial-intent backstop still scans the body, so a summary that ever
drifted into warning/termination/price language would be re-gated to approval.

Conventions: integer cents (n/a here); parameterised SQL; timezone-aware DB
timestamps; never raises at the caller (DB failures come back as typed
``ToolResult``s).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools import email as email_tool
from src.tools.results import ToolResult, error, found, unavailable

logger = logging.getLogger(__name__)

SUMMARY_EMAIL_TYPE = "caterer_weekly_summary"
_ACTIVE_EMAIL_STATES = ("sent", "queued_for_approval", "approved", "drafted")

# A student free-text comment must recur at least this many times (distinct
# ratings) to count as a real theme worth surfacing; fewer = noise, dropped.
RECURRING_MIN = 3
# A clean strong performer (-> capacity ask): student satisfaction at/above this,
# AND zero dietary misses, AND no other reliability failures.
STRONG_AVG = 4.5
DEFAULT_WEEKS = 8

# Manager checklist -> the four POSITIVE reliability signals (value_bool=false is a
# problem). ``visibly_wrong`` is deliberately excluded: it has inverted polarity and
# is not one of the brief's late/count/dietary/temp signals.
_RELIABILITY_LABELS: dict[str, str] = {
    "food_on_time": "on-time delivery",
    "correct_count_received": "correct meal counts",
    "correct_dietary_delivered": "dietary-correct meals",
    "food_temperature_ok": "food served at temperature",
}
_DIETARY_CODE = "correct_dietary_delivered"


# --- DB helpers --------------------------------------------------------------


def _read(describe: str, sql: str, params=None) -> list[dict] | ToolResult:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except psycopg.OperationalError as exc:
        return unavailable(f"Database unavailable while {describe}: {exc}")
    except psycopg.Error as exc:
        return error(f"Database error while {describe}: {exc}")


def _failed(value) -> bool:
    return isinstance(value, ToolResult)


# --- Noise-filtering of student free-text ------------------------------------


def _normalise(comment: str) -> str:
    """Lowercase, strip punctuation/emoji, collapse whitespace — so near-identical
    comments group together for frequency counting."""
    text = re.sub(r"[^a-z0-9 ]+", " ", comment.lower())
    return re.sub(r"\s+", " ", text).strip()


def recurring_themes(comments: list[str], min_count: int = RECURRING_MIN) -> tuple[list[dict], list[dict]]:
    """Split student comments into RECURRING themes vs dropped NOISE — PURE.

    Groups by normalised text and counts distinct mentions. A group with
    ``>= min_count`` mentions is a real theme (kept, most-frequent first, shown in
    its most common original wording); everything below is noise (dropped). Returns
    ``(themes, dropped)`` where each is ``[{"theme", "count"}]``."""
    groups: dict[str, list[str]] = {}
    for c in comments:
        key = _normalise(c)
        if key:
            groups.setdefault(key, []).append(c.strip())
    themes: list[dict] = []
    dropped: list[dict] = []
    for key, originals in groups.items():
        # representative wording = the most common original form in the group
        rep = Counter(originals).most_common(1)[0][0]
        entry = {"theme": rep, "count": len(originals)}
        (themes if len(originals) >= min_count else dropped).append(entry)
    themes.sort(key=lambda e: (-e["count"], e["theme"]))
    dropped.sort(key=lambda e: (-e["count"], e["theme"]))
    return themes, dropped


# --- Aggregated data (every claim a real number) -----------------------------


@dataclass
class SchoolScore:
    school_id: int
    school_name: str
    student_avg: float
    student_count: int


@dataclass
class ReliabilitySignal:
    code: str
    label: str
    failed: int
    total: int


@dataclass
class SummaryData:
    caterer_id: int
    caterer_name: str
    caterer_contact_email: str | None
    week_of: str
    weeks: int
    meals_served: int                       # order lines for the caterer's week
    per_school: list[SchoolScore]
    overall_avg: float | None
    overall_count: int
    standout: SchoolScore | None
    soft_spot: SchoolScore | None
    themes: list[dict]                      # recurring (surfaced)
    dropped_noise: list[dict]              # one-offs (filtered out)
    reliability: list[ReliabilitySignal]   # the four signals, failed counts
    dietary_failed: int
    strong_performer: bool
    has_concern: bool                      # tempers tone, withholds capacity ask

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


def _caterer(caterer_id: int) -> dict | None | ToolResult:
    rows = _read(
        f"loading caterer {caterer_id}",
        "SELECT id, name, contact_email FROM caterers WHERE id = %s",
        (caterer_id,),
    )
    if _failed(rows):
        return rows
    return rows[0] if rows else None


def summary_data(caterer_id: int, week_of: date, weeks: int = DEFAULT_WEEKS) -> SummaryData | ToolResult:
    """Aggregate one caterer's week into the numbers behind the scorecard — PURE.

    Student satisfaction per school + overall, the recurring (noise-filtered)
    themes, the four manager reliability signals, and the derived
    strong-performer / concern flags. Reads the review window (``weeks`` back)."""
    caterer = _caterer(caterer_id)
    if _failed(caterer):
        return caterer
    if caterer is None:
        return error(f"No caterer with id {caterer_id}.")

    week_start, week_end = week_of, week_of + timedelta(days=7)
    served = _read(
        f"counting meals served for caterer {caterer_id}",
        """
        SELECT count(*) AS n FROM order_lines ol JOIN orders o ON o.id = ol.order_id
        WHERE o.caterer_id = %s AND o.session_date >= %s AND o.session_date < %s
        """,
        (caterer_id, week_start, week_end),
    )
    if _failed(served):
        return served
    meals_served = served[0]["n"]

    per_school_rows = _read(
        f"per-school student ratings for caterer {caterer_id}",
        """
        SELECT e.school_id, s.name AS school_name,
               round(avg(f.rating)::numeric, 2)::float AS avg, count(*) AS n
        FROM feedback f
        JOIN order_lines ol ON ol.id = f.order_line_id
        JOIN enrolments e ON e.id = ol.enrolment_id
        JOIN schools s ON s.id = e.school_id
        WHERE f.source = 'student' AND f.caterer_id = %s AND f.rating IS NOT NULL
          AND f.submitted_at >= now() - make_interval(weeks => %s)
        GROUP BY e.school_id, s.name
        ORDER BY s.name
        """,
        (caterer_id, weeks),
    )
    if _failed(per_school_rows):
        return per_school_rows
    per_school = [
        SchoolScore(r["school_id"], r["school_name"], r["avg"], r["n"]) for r in per_school_rows
    ]
    total_score = sum(s.student_avg * s.student_count for s in per_school)
    overall_count = sum(s.student_count for s in per_school)
    overall_avg = round(total_score / overall_count, 2) if overall_count else None
    ranked = sorted(per_school, key=lambda s: s.student_avg)
    soft_spot = ranked[0] if len(ranked) >= 2 else None
    standout = ranked[-1] if len(ranked) >= 2 else (ranked[-1] if ranked else None)

    comment_rows = _read(
        f"student comments for caterer {caterer_id}",
        """
        SELECT f.comment FROM feedback f
        WHERE f.source = 'student' AND f.caterer_id = %s
          AND f.comment IS NOT NULL AND btrim(f.comment) <> ''
          AND f.submitted_at >= now() - make_interval(weeks => %s)
        """,
        (caterer_id, weeks),
    )
    if _failed(comment_rows):
        return comment_rows
    themes, dropped = recurring_themes([r["comment"] for r in comment_rows])

    rel_rows = _read(
        f"manager reliability for caterer {caterer_id}",
        """
        SELECT ci.code,
               count(*) FILTER (WHERE r.value_bool = false) AS failed,
               count(*) AS total
        FROM feedback f
        JOIN feedback_checklist_response r ON r.feedback_id = f.id
        JOIN checklist_item ci ON ci.id = r.checklist_item_id
        WHERE f.source = 'manager' AND f.caterer_id = %s
          AND f.submitted_at >= now() - make_interval(weeks => %s)
          AND ci.code = ANY(%s)
        GROUP BY ci.code
        """,
        (caterer_id, weeks, list(_RELIABILITY_LABELS)),
    )
    if _failed(rel_rows):
        return rel_rows
    by_code = {r["code"]: r for r in rel_rows}
    reliability = [
        ReliabilitySignal(code, label, by_code.get(code, {}).get("failed", 0),
                          by_code.get(code, {}).get("total", 0))
        for code, label in _RELIABILITY_LABELS.items()
    ]
    dietary_failed = next((s.failed for s in reliability if s.code == _DIETARY_CODE), 0)
    other_failed = sum(s.failed for s in reliability if s.code != _DIETARY_CODE)

    # Duty of care: a dietary-miss pattern (or weak scores) tempers tone and BLOCKS
    # the capacity ask regardless of how good the averages look.
    has_concern = dietary_failed > 0 or (overall_avg is not None and overall_avg < 4.0)
    strong_performer = (
        overall_avg is not None and overall_avg >= STRONG_AVG
        and dietary_failed == 0 and other_failed == 0
    )

    return SummaryData(
        caterer_id=caterer_id, caterer_name=caterer["name"],
        caterer_contact_email=caterer.get("contact_email"),
        week_of=week_of.isoformat(), weeks=weeks, meals_served=meals_served,
        per_school=per_school, overall_avg=overall_avg, overall_count=overall_count,
        standout=standout, soft_spot=soft_spot, themes=themes, dropped_noise=dropped,
        reliability=reliability, dietary_failed=dietary_failed,
        strong_performer=strong_performer, has_concern=has_concern,
    )


# --- Render (warm, consistent skeleton, real numbers) ------------------------


def summary_subject(caterer_name: str, week_of: date) -> str:
    """The DETERMINISTIC scorecard subject (same caterer-week -> same string, which
    the idempotency check matches on). Warm, never commercial."""
    return f"Your Padea weekly scorecard — {caterer_name}, week of {week_of.isoformat()} 🍽"


def _f1(x: float | None) -> str:
    return f"{x:.1f}" if x is not None else "—"


def _join_and(parts: list[str]) -> str:
    """Join phrases into a natural list: 'a', 'a and b', 'a, b, and c'."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def render_caterer_weekly_summary(
    caterer_id: int, week_of: date, themes: list[str] | None = None,
    weeks: int = DEFAULT_WEEKS, data: SummaryData | None = None,
) -> tuple[str, str] | ToolResult:
    """The warm per-caterer scorecard (subject, body) — deterministic skeleton, real
    numbers. ``themes`` (optional) lets the agent supply its own filtered recurring
    themes; without it, the frequency filter's themes are used. ``data`` (optional)
    reuses an already-computed ``summary_data`` to avoid a second aggregation pass.
    Returns a typed failure on a read error."""
    if data is None:
        data = summary_data(caterer_id, week_of, weeks)
        if _failed(data):
            return data
    return summary_subject(data.caterer_name, week_of), _render(data, themes)


def _render(data: SummaryData, themes_override: list[str] | None = None) -> str:
    name = data.caterer_name
    n_schools = len(data.per_school)
    L: list[str] = [f"Hi {name} team,", ""]

    # 1. Warm open + GENUINE SPECIFIC PRAISE (only true positives).
    praise: list[str] = []
    if data.meals_served:
        praise.append(
            f"you served {data.meals_served} Padea dinners across "
            f"{n_schools} school{'s' if n_schools != 1 else ''} this week")
    if data.dietary_failed == 0:
        praise.append("every dietary-specific meal was spot on — exactly what we ask for most")
    if data.standout is not None and data.standout.student_avg >= 4.5:
        praise.append(
            f"{data.standout.school_name} students rated you {_f1(data.standout.student_avg)}/5")
    opener = "Thank you — a genuinely strong week. " if not data.has_concern else "Thank you for this week's service. "
    praise_text = _join_and(praise)
    if praise_text:
        praise_text = praise_text[0].upper() + praise_text[1:] + "."
    else:
        praise_text = "We appreciate the partnership."
    L += [opener + praise_text, ""]

    # 2. STUDENT SATISFACTION PER SCHOOL — the headline.
    L += ["── How students rated you, by school ──"]
    if data.per_school:
        for s in data.per_school:
            L.append(f"   • {s.school_name}: {_f1(s.student_avg)}/5  ({s.student_count} students)")
        if data.overall_avg is not None:
            L.append(f"   Overall: {_f1(data.overall_avg)}/5 across {data.overall_count} ratings.")
        if data.standout and data.soft_spot and data.standout.school_id != data.soft_spot.school_id:
            L += [
                "",
                f"   {data.standout.school_name} is your standout ({_f1(data.standout.student_avg)}/5). "
                f"The one to nurture is {data.soft_spot.school_name} "
                f"({_f1(data.soft_spot.student_avg)}/5).",
            ]
    else:
        L.append("   (no student ratings in yet this week)")
    L.append("")

    # 3. THE WHY — recurring themes only (noise filtered out).
    theme_texts = themes_override if themes_override is not None else [t["theme"] for t in data.themes]
    if theme_texts:
        L += ["── What students kept telling us ──"]
        for t in theme_texts:
            L.append(f"   • {t}")
        L.append("   (one-off requests aside — these are the comments that came up again and again.)")
        L.append("")

    # 4. SERVICE NOTE — manager reliability, gentle + specific.
    issues = [s for s in data.reliability if s.failed > 0]
    if issues:
        L += ["── One or two things to keep an eye on ──"]
        for s in issues:
            if s.code == _DIETARY_CODE:
                L.append(
                    f"   • Dietary-correct meals: {s.failed} of {s.total} checks flagged a miss. "
                    "This is the one that matters most to us — a wrong dietary meal can harm a "
                    "child — so let's tighten it together.")
            else:
                L.append(f"   • {s.label.capitalize()}: {s.failed} of {s.total} checks flagged it — "
                         "nothing major, just flagging so we can keep it sharp.")
        L.append("")

    # 5. CAPACITY ASK — only a clean strong performer, framed as opportunity.
    if data.strong_performer:
        L += [
            "── An opportunity ──",
            "   You're one of our strongest partners right now. If you have capacity to take on "
            "more Padea sessions, we'd love to explore growing together — no pressure, just an "
            "open door.",
            "",
        ]

    # 6. Appreciative close.
    L += [
        "Thanks for looking after our students so well. We're grateful to have you on the team.",
        "",
        "Warmly,",
        "Padea Operations",
    ]
    return "\n".join(L)


# --- Plan + send (idempotent, one per caterer) -------------------------------


def _caterers_with_schools() -> list[dict] | ToolResult:
    return _read(
        "listing caterers serving a school",
        """
        SELECT c.id, c.name, c.contact_email
        FROM caterers c
        WHERE EXISTS (SELECT 1 FROM schools s WHERE s.current_caterer_id = c.id)
        ORDER BY c.id
        """,
    )


def _already_sent(caterer_id: int, subject: str) -> bool | ToolResult:
    rows = _read(
        f"checking prior summary for caterer {caterer_id}",
        """
        SELECT 1 FROM outbound_emails
        WHERE related_caterer_id = %s AND email_type = %s
          AND status = ANY(%s) AND subject = %s LIMIT 1
        """,
        (caterer_id, SUMMARY_EMAIL_TYPE, list(_ACTIVE_EMAIL_STATES), subject),
    )
    if _failed(rows):
        return rows
    return bool(rows)


def plan_weekly_summaries(week_of: date, weeks: int = DEFAULT_WEEKS) -> dict | ToolResult:
    """Decide, per caterer, whether a scorecard WOULD send — without sending. A
    caterer with no student ratings this window is skipped (nothing to summarise);
    one already sent for the week is skipped (idempotent)."""
    caterers = _caterers_with_schools()
    if _failed(caterers):
        return caterers
    would: list[dict] = []
    skipped: list[dict] = []
    for c in caterers:
        data = summary_data(c["id"], week_of, weeks)
        if _failed(data):
            return data
        subject = summary_subject(c["name"], week_of)
        if not data.overall_count:
            skipped.append({"caterer_id": c["id"], "caterer_name": c["name"],
                            "reason": "no student ratings to summarise"})
            continue
        if not c["contact_email"]:
            skipped.append({"caterer_id": c["id"], "caterer_name": c["name"],
                            "reason": "no contact email"})
            continue
        already = _already_sent(c["id"], subject)
        if _failed(already):
            return already
        if already:
            skipped.append({"caterer_id": c["id"], "caterer_name": c["name"],
                            "reason": "already sent this week (idempotent skip)"})
            continue
        would.append({"caterer_id": c["id"], "caterer_name": c["name"],
                      "overall_avg": data.overall_avg, "strong_performer": data.strong_performer,
                      "themes": [t["theme"] for t in data.themes]})
    return {"week_of": week_of.isoformat(), "would_send": would, "skipped": skipped}


def send_caterer_weekly_summaries(
    week_of: date, run_id: int | None = None, weeks: int = DEFAULT_WEEKS
) -> ToolResult:
    """Send EXACTLY ONE ``caterer_weekly_summary`` per caterer with student ratings
    for the week. Idempotent per (caterer, week): a re-run sends 0. Returns the
    caterers sent to, those skipped (with reasons), and any send failures."""
    plan = plan_weekly_summaries(week_of, weeks)
    if _failed(plan):
        return plan
    sent: list[dict] = []
    failed: list[dict] = []
    for w in plan["would_send"]:
        cid = w["caterer_id"]
        rendered = render_caterer_weekly_summary(cid, week_of, weeks=weeks)
        if _failed(rendered):
            failed.append({"caterer_id": cid, "caterer_name": w["caterer_name"],
                           "status": rendered.status, "message": rendered.message})
            continue
        subject, body = rendered
        result = email_tool.send_email(
            email_type=SUMMARY_EMAIL_TYPE, to=_contact_for(cid),
            subject=subject, body=body, related_caterer_id=cid, related_run_id=run_id,
        )
        if result.ok:
            sent.append({"caterer_id": cid, "caterer_name": w["caterer_name"],
                         "email_id": (result.data or {}).get("email_id")})
            logger.info("send_caterer_weekly_summaries: scorecard to caterer %s.", cid)
        else:
            failed.append({"caterer_id": cid, "caterer_name": w["caterer_name"],
                           "status": result.status, "message": result.message})
    return found(
        {"week_of": week_of.isoformat(), "sent": sent,
         "skipped": plan["skipped"], "failed": failed},
        f"Caterer weekly summaries for week of {week_of.isoformat()}: {len(sent)} sent, "
        f"{len(plan['skipped'])} skipped, {len(failed)} failed.",
    )


def _contact_for(caterer_id: int) -> str | None:
    c = _caterer(caterer_id)
    return None if _failed(c) or c is None else c.get("contact_email")
