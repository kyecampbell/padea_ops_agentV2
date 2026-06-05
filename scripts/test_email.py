"""Prove the email + escalate tools against the real DB (and a real demo send).

Demonstrates, end to end:
  (a) a factual email (type 'other') is AUTONOMOUS -> it actually sends, demo-routed
      to DEMO_SINK_EMAIL with a `[DEMO — Intended for: <real to>]` banner, and is
      logged 'sent' with a gmail_message_id (the REAL recipient is preserved in
      intended_to_address);
  (b) a commercial email (type 'warning') is REQUIRES_APPROVAL -> it does NOT send
      and is logged 'queued_for_approval' (no gmail_message_id);
  (c) escalate_to_human creates an OPEN escalation.

Every row this script creates is printed, then cleaned up in a finally block.
Prints a PASS/FAIL line per check; exits non-zero if anything fails.

NOTE: check (a) sends a real email via Gmail to DEMO_SINK_EMAIL.

Run: uv run python scripts/test_email.py
"""

from __future__ import annotations

import sys

from config.settings import settings
from src.agent.gates import classify_email
from src.db.connection import fetch_all, get_conn
from src.gmail.client import GmailClient
from src.tools.email import send_email
from src.tools.escalate import escalate_to_human
from src.tools.results import ToolResult

# A real recipient we DON'T actually want to email — demo mode redirects it to
# the sink. Distinct + obviously fake so the banner is easy to spot.
_REAL_RECIPIENT = "caterer.real@example.com"

_passes = 0
_failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _passes, _failures
    mark = "PASS" if ok else "FAIL"
    if ok:
        _passes += 1
    else:
        _failures += 1
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))


def expect(label: str, result: ToolResult, status: str) -> bool:
    ok = isinstance(result, ToolResult) and result.status == status
    detail = f"status={getattr(result, 'status', '?')!r} msg={getattr(result, 'message', '')!r}"
    check(label, ok, detail)
    return ok


def _row(email_id: int) -> dict:
    cols = (
        "id, email_type, status, intended_to_address, intended_cc_addresses, "
        "subject, gmail_message_id, sent_at, queued_for_approval_at, failed_at, failure_reason"
    )
    (r,) = fetch_all(f"SELECT {cols} FROM outbound_emails WHERE id = %s", (email_id,))
    keys = [c.strip() for c in cols.split(",")]
    return dict(zip(keys, r))


def _cleanup(email_ids: list[int], escalation_ids: list[int]) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        if email_ids:
            cur.execute("DELETE FROM outbound_emails WHERE id = ANY(%s)", (email_ids,))
        if escalation_ids:
            cur.execute("DELETE FROM escalations WHERE id = ANY(%s)", (escalation_ids,))
        conn.commit()


def main() -> int:
    print(f"EMAIL_MODE={settings.email_mode!r}  DEMO_SINK_EMAIL={settings.demo_sink_email!r}\n")
    if settings.email_mode != "demo":
        print("This test expects EMAIL_MODE=demo (it sends to the sink).", file=sys.stderr)
        return 1

    email_ids: list[int] = []
    escalation_ids: list[int] = []
    try:
        # --- (a) Factual email ('other'): autonomous -> actually sends, demo-routed. ---
        check("(a) classify_email('other') == autonomous", classify_email("other") == "autonomous")
        res_a = send_email(
            email_type="other",
            to=_REAL_RECIPIENT,
            subject="Operational update",
            body="This is a factual operational note.",
        )
        if expect("(a) send_email('other') -> found", res_a, "found"):
            email_ids.append(res_a.data["email_id"])
            check("    result says sent", res_a.data.get("sent") is True, f"data={res_a.data}")
            check("    demo_routed flag set", res_a.data.get("demo_routed") is True)
            check("    gmail_message_id present", bool(res_a.data.get("gmail_message_id")))
            row = _row(res_a.data["email_id"])
            print(f"    outbound_emails row: {row}")
            check("    logged status 'sent'", row["status"] == "sent")
            check("    intended_to_address is the REAL recipient",
                  row["intended_to_address"] == _REAL_RECIPIENT, f"got {row['intended_to_address']!r}")
            check("    sent_at stamped", row["sent_at"] is not None)
            # Prove the demo banner reached the actual sent message in the sink inbox.
            try:
                msg = GmailClient().get_message(row["gmail_message_id"])
                subj = next(
                    (h["value"] for h in msg["payload"]["headers"] if h["name"].lower() == "subject"),
                    "",
                )
                to_hdr = next(
                    (h["value"] for h in msg["payload"]["headers"] if h["name"].lower() == "to"),
                    "",
                )
                print(f"    sent message: To={to_hdr!r} Subject={subj!r}")
                check("    actual subject carries the [DEMO …] banner",
                      subj.startswith(f"[DEMO — Intended for: {_REAL_RECIPIENT}]"), f"subj={subj!r}")
                check("    actual recipient is the sink",
                      settings.demo_sink_email in to_hdr, f"to={to_hdr!r}")
            except Exception as exc:  # fetch is a nice-to-have proof, not the core check
                check("    could fetch sent message for banner proof", False, str(exc))

        # --- (b) Commercial email ('warning'): requires approval -> NOT sent. ---
        check("(b) classify_email('warning') == requires_approval",
              classify_email("warning") == "requires_approval")
        res_b = send_email(
            email_type="warning",
            to=_REAL_RECIPIENT,
            subject="Quality concern",
            body="We have noticed a decline in meal quality.",
        )
        if expect("(b) send_email('warning') -> queued", res_b, "queued"):
            email_ids.append(res_b.data["email_id"])
            check("    result says NOT sent", res_b.data.get("sent") is False, f"data={res_b.data}")
            row = _row(res_b.data["email_id"])
            print(f"    outbound_emails row: {row}")
            check("    logged status 'queued_for_approval'", row["status"] == "queued_for_approval")
            check("    queued_for_approval_at stamped", row["queued_for_approval_at"] is not None)
            check("    no gmail_message_id (never sent)", row["gmail_message_id"] is None)

        # --- (c) escalate_to_human creates an OPEN escalation. ---
        res_c = escalate_to_human(
            question="Caterer quality is declining — switch caterers for this school?",
            context={"school": "Test School", "signal": "two low-rated orders"},
        )
        if expect("(c) escalate_to_human -> found", res_c, "found"):
            escalation_ids.append(res_c.data["escalation_id"])
            check("    status 'open'", res_c.data.get("status") == "open", f"data={res_c.data}")
            (erow,) = fetch_all(
                "SELECT id, status, question, context FROM escalations WHERE id = %s",
                (res_c.data["escalation_id"],),
            )
            print(f"    escalations row: id={erow[0]} status={erow[1]!r} "
                  f"question={erow[2]!r} context={erow[3]!r}")
            check("    persisted as open", erow[1] == "open")

    finally:
        _cleanup(email_ids, escalation_ids)
        print(f"\nCleaned up {len(email_ids)} outbound_email row(s) and "
              f"{len(escalation_ids)} escalation row(s).")

    print(f"\n{_passes} passed, {_failures} failed.")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
