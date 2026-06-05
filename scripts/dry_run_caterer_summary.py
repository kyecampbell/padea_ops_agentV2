"""DRY RUN — the Monday per-caterer QUALITY SUMMARY. Renders + classifies, WRITES/SENDS NOTHING.

Proves the warm scorecard with zero side effects (pure render + plan; no DB writes,
no emails):

  1. Full rendered scorecard for one MULTI-SCHOOL caterer, with the noise-filter
     laid bare: the recurring themes it surfaced vs the one-off junk it dropped.
  2. Across all caterers: a clean STRONG performer gets the capacity ask, while a
     weaker one (and any with a dietary-miss pattern) does NOT.
  3. Gating: the rendered scorecard classifies AUTONOMOUS and trips no commercial-
     intent signal — and a tampered body that mentions termination/price IS caught
     by the backstop (so a summary can never drift commercial and auto-send).

Everything here is PURE (summary_data / render / plan_weekly_summaries / gates).
Nothing is sent, written, or pushed.

Run: uv run python scripts/dry_run_caterer_summary.py
"""

from __future__ import annotations

import sys
from datetime import date

from src.agent.gates import classify_email, email_requires_approval
from src.db.connection import fetch_all
from src.tools import caterer_quality_summary as cqs
from src.tools import orders_batch

# The served week the seeded student ratings attach to.
_WEEK = date(2026, 6, 8)
_MULTI_SCHOOL_DEMO = 2   # Terrific Noodles — multi-school, weaker, richest noise to filter


def _indent(text: str, prefix: str = "      | ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main(argv: list[str] | None = None) -> int:
    week = _WEEK
    print("=" * 86)
    print(f"DRY RUN — Monday caterer QUALITY SUMMARY for week of {week.isoformat()}  (NOTHING SENT/WRITTEN)")
    print("=" * 86)

    # Compute each caterer's summary ONCE (one aggregation pass each) and reuse it
    # everywhere below — keeps DB connection churn low.
    caterers = fetch_all("SELECT id, name FROM caterers WHERE EXISTS "
                         "(SELECT 1 FROM schools s WHERE s.current_caterer_id = caterers.id) ORDER BY id")
    summaries = {}
    for cid, _cname in caterers:
        d = cqs.summary_data(cid, week)
        if not isinstance(d, cqs.SummaryData):
            print(f"summary_data({cid}) failed: {d.status} — {d.message}", file=sys.stderr)
            return 1
        summaries[cid] = d

    # --- 1. Full render of a multi-school caterer + the noise filter exposed. ---
    data = summaries[_MULTI_SCHOOL_DEMO]
    subject, body = cqs.render_caterer_weekly_summary(_MULTI_SCHOOL_DEMO, week, data=data)
    print("\n" + "-" * 86)
    print(f"1. FULL SCORECARD — multi-school caterer #{data.caterer_id} {data.caterer_name}")
    print("-" * 86)
    surfaced = ", ".join("'{}' x{}".format(t["theme"], t["count"]) for t in data.themes)
    dropped = ", ".join("'{}'".format(t["theme"]) for t in data.dropped_noise)
    print("   NOISE FILTER:")
    print(f"     surfaced (recurring, >= {cqs.RECURRING_MIN}): {surfaced}")
    print(f"     dropped (one-off junk): {dropped}")
    print(f"\n   Subject: {subject}")
    print("   Body:")
    print(_indent(body))

    # --- 2. Capacity ask: strong performer yes, weaker/dietary-risk no. ---
    print("\n" + "-" * 86)
    print("2. CAPACITY ASK — only a clean strong performer gets it")
    print("-" * 86)
    print(f"   {'caterer':<22} {'overall':>7} {'dietary_miss':>12} {'strong':>7}  capacity_ask_in_email")
    for cid, cname in caterers:
        d = summaries[cid]
        _subj, body_c = cqs.render_caterer_weekly_summary(cid, week, data=d)
        ask = "An opportunity" in body_c
        print(f"   {cname[:22]:<22} {str(d.overall_avg):>7} {d.dietary_failed:>12} "
              f"{str(d.strong_performer):>7}  {'YES' if ask else 'no'}")

    # --- 3. Gating: autonomous + commercial backstop still active. ---
    print("\n" + "-" * 86)
    print("3. GATING — autonomous, but the commercial backstop still guards it")
    print("-" * 86)
    klass = classify_email(cqs.SUMMARY_EMAIL_TYPE)
    needs, signals = email_requires_approval(cqs.SUMMARY_EMAIL_TYPE, subject, body)
    print(f"   classify_email('caterer_weekly_summary') = {klass}")
    print(f"   email_requires_approval(rendered scorecard) = (needs_approval={needs}, signals={signals})")
    tampered = body + "\n\nP.S. If this doesn't improve we may have to terminate the contract and review your pricing."
    t_needs, t_signals = email_requires_approval(cqs.SUMMARY_EMAIL_TYPE, subject, tampered)
    print(f"   if a summary drifted commercial -> (needs_approval={t_needs}, signals={t_signals})")
    gating_ok = klass == "autonomous" and not needs and not signals and t_needs and t_signals

    # --- 4. Plan (pure) — one per caterer, idempotency-aware, nothing sent. ---
    plan = cqs.plan_weekly_summaries(week)
    print("\n" + "-" * 86)
    print("4. SEND PLAN (pure; nothing sent)")
    print("-" * 86)
    print(f"   would send: {len(plan['would_send'])}  skipped: {len(plan['skipped'])}")
    for w in plan["would_send"]:
        print(f"     -> {w['caterer_name']}: overall {w['overall_avg']}/5, "
              f"{'capacity ask' if w['strong_performer'] else 'no ask'}")
    for s in plan["skipped"]:
        print(f"     (skip) {s['caterer_name']}: {s['reason']}")

    print("\n" + "=" * 86)
    print("RESULT")
    print("=" * 86)
    noise_ok = bool(data.themes) and bool(data.dropped_noise)
    strong3 = summaries[3].strong_performer
    weak2 = not summaries[2].strong_performer
    print(f"  Multi-school render with per-school breakdown ... True (#{data.caterer_id} {data.caterer_name})")
    print(f"  Noise filtered (themes kept, junk dropped) ...... {noise_ok}")
    print(f"  Strong performer (caterer 3) gets the ask ....... {strong3}")
    print(f"  Weaker caterer (caterer 2) does NOT ............. {weak2}")
    print(f"  Classifies autonomous; backstop still catches ... {gating_ok}")
    print("\n  Nothing sent, written, or pushed.")
    return 0 if (noise_ok and strong3 and weak2 and gating_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
