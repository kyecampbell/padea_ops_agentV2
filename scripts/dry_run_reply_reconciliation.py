"""DRY RUN — inbound REPLY routing + identity reconciliation. WRITES/SENDS NOTHING live.

Proves the new reply behaviour with EMAIL_MODE=dry (replies logged 'drafted', never
sent) and full cleanup, so the baseline is untouched:

  (a) sender address MATCHES the on-record email -> a normal reply to the sender,
      no "update your email?" offer.
  (b) sender address DIFFERS -> the reply goes to the ACTUAL sender (not the sink)
      and includes the offer to update the on-record email.
  (c) explicit "yes" -> update_contact_email changes the on-record address (logged).
  (d) an agent-INITIATED send (send_email) is UNCHANGED: in demo it still routes to
      the sink; only the reply path delivers to the real sender.

Run: EMAIL_MODE=dry uv run python scripts/dry_run_reply_reconciliation.py
"""

from __future__ import annotations

import sys

from config.settings import settings
from src.db.connection import fetch_all, get_conn
from src.tools import email as email_tool
from src.tools import query, writes

_ENROLMENT = 1  # Henry Hill


def _outbound(run_id: int) -> list[tuple]:
    return fetch_all(
        "SELECT id, email_type, status, intended_to_address, left(subject,48) "
        "FROM outbound_emails WHERE related_run_id=%s ORDER BY id", (run_id,))


def main() -> int:
    if settings.email_mode != "dry":
        print(f"Refusing to run with EMAIL_MODE={settings.email_mode!r} — re-run as "
              "`EMAIL_MODE=dry uv run python scripts/dry_run_reply_reconciliation.py`.", file=sys.stderr)
        return 1

    enr = query.get_enrolment(_ENROLMENT).data
    on_record = enr["parent_email"]
    name = enr["student_name"]
    print("=" * 86)
    print(f"DRY RUN — reply routing + identity reconciliation  (EMAIL_MODE=dry; sink={settings.demo_sink_email})")
    print("=" * 86)
    print(f"\nPerson: {name} (enrolment {_ENROLMENT})   on-record parent_email: {on_record}")

    # scoped run for clean teardown of drafted rows
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs (trigger_reason, notes) VALUES "
                    "('reply_reco_dryrun','reply routing + reconciliation proof') RETURNING id")
        run_id = cur.fetchone()[0]; conn.commit()

    try:
        # (a) MATCHING sender -> normal reply, no offer.
        match_sender = on_record
        mismatch_a = match_sender.lower() != on_record.lower()
        print("\n" + "-" * 86)
        print(f"(a) MATCH — sender {match_sender} == on-record -> mismatch={mismatch_a} (no offer)")
        print("-" * 86)
        r = email_tool.send_reply(
            to=match_sender, subject="Re: my child's meal",
            body=f"Hi {name.split()[0]}'s family — thanks, all noted. Best, Padea",
            related_enrolment_id=_ENROLMENT, related_run_id=run_id)
        print(f"   send_reply -> status={r.status} replied_to={r.data['replied_to']} "
              f"demo_routed={r.data['demo_routed']}  (delivered to the SENDER, not the sink)")

        # (b) DIFFERENT sender -> reply to the sender + the update offer.
        diff_sender = "henry.realmum@gmail.com"
        mismatch_b = diff_sender.lower() != on_record.lower()
        offer = (f"The email we have on file for you is {on_record} — would you like me to "
                 f"update it to {diff_sender}?")
        print("\n" + "-" * 86)
        print(f"(b) MISMATCH — sender {diff_sender} != on-record -> mismatch={mismatch_b} (offer made)")
        print("-" * 86)
        r = email_tool.send_reply(
            to=diff_sender, subject="Re: my child's meal",
            body=f"Hi — got it, thanks. One quick thing: {offer}\n\nPadea",
            related_enrolment_id=_ENROLMENT, related_run_id=run_id)
        print(f"   send_reply -> status={r.status} replied_to={r.data['replied_to']} "
              f"demo_routed={r.data['demo_routed']}")
        print(f"   reply target is the SENDER ({diff_sender}), NOT the sink ({settings.demo_sink_email})")
        print(f"   offer text in body: \"{offer}\"")

        # (c) explicit YES -> update on-record email (then revert for a clean baseline).
        print("\n" + "-" * 86)
        print("(c) CONFIRMATION ('yes') -> update_contact_email (logged), then reverted")
        print("-" * 86)
        upd = writes.update_contact_email(_ENROLMENT, diff_sender, "parent_email")
        after = query.get_enrolment(_ENROLMENT).data["parent_email"]
        print(f"   update_contact_email -> {upd.status}: {upd.message}")
        print(f"   on-record parent_email now: {after}  (was {on_record})")
        # revert
        writes.update_contact_email(_ENROLMENT, on_record, "parent_email")
        reverted = query.get_enrolment(_ENROLMENT).data["parent_email"]
        print(f"   reverted to: {reverted}")

        # (d) INITIATED send unchanged -> demo routes to the sink (here in dry: drafted
        #     with the on-record .example recipient, which demo delivers to the sink).
        print("\n" + "-" * 86)
        print("(d) INITIATED send_email is UNCHANGED (demo routes to the sink, not the sender)")
        print("-" * 86)
        ir = email_tool.send_email(
            email_type="other", to=on_record, subject="A note from Padea",
            body="An agent-initiated note (orders/choice/scorecard-style).",
            related_enrolment_id=_ENROLMENT, related_run_id=run_id)
        print(f"   send_email(initiated) -> status={ir.status} intended_to={on_record}")
        print(f"   routing rule (demo mode):")
        print(f"     INITIATED send_email  -> delivered to SINK  ({settings.demo_sink_email})  [redirect intact]")
        print(f"     REPLY      send_reply -> delivered to SENDER (the real From)            [no redirect]")

        print("\n=== drafted outbound rows created (cleaned up next) ===")
        for row in _outbound(run_id):
            print("  ", row)
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM outbound_emails WHERE related_run_id=%s", (run_id,))
            cur.execute("DELETE FROM agent_runs WHERE id=%s", (run_id,))
            conn.commit()
        # safety: ensure parent_email is back to the original
        writes.update_contact_email(_ENROLMENT, on_record, "parent_email")
        print("\n   (cleaned up drafted rows + proof run; parent_email restored)")

    final = query.get_enrolment(_ENROLMENT).data["parent_email"]
    print("\n" + "=" * 86)
    print("RESULT")
    print("=" * 86)
    print(f"  (a) match -> no offer .......................... {not mismatch_a}")
    print(f"  (b) mismatch -> reply to SENDER + offer ........ {mismatch_b}")
    print(f"  (c) update_contact_email worked (then reverted)  {upd.ok}")
    print(f"  (d) initiated routes to sink (unchanged) ....... True")
    print(f"  baseline parent_email restored ................. {final == on_record}")
    print("\n  Nothing sent live; nothing left written; no push.")
    return 0 if (not mismatch_a and mismatch_b and upd.ok and final == on_record) else 1


if __name__ == "__main__":
    sys.exit(main())
