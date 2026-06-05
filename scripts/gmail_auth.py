"""One-off Gmail OAuth consent — mint / re-mint config/gmail_token.json.

The app's ``GmailClient`` deliberately does NOT run a consent flow: it expects a
cached token (with a refresh_token) provisioned OUT OF BAND. This script is that
out-of-band step — run it locally, once, to (re)create the cached token when there
is none or the refresh_token has expired / been revoked.

It launches the standard Google "installed app" browser consent flow using the
OAuth client in ``config/gmail_credentials.json`` and the EXACT scopes the app
uses (imported from ``src.gmail.client`` so they can never drift), then writes the
resulting credentials — including the refresh_token — to ``config/gmail_token.json``
(the path from settings). For Render, ship that file's CONTENTS via GMAIL_TOKEN_JSON.

Requires the OAuth helper lib (NOT a runtime dependency; the cloud worker only
refreshes, it never consents):

    uv pip install google-auth-oauthlib

Run:  uv run python scripts/gmail_auth.py
"""

from __future__ import annotations

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from src.gmail.client import SCOPES, _resolve
from config.settings import settings


def main() -> int:
    creds_path = _resolve(settings.gmail_credentials_path, "GMAIL_CREDENTIALS_PATH")
    token_path = _resolve(settings.gmail_token_path, "GMAIL_TOKEN_PATH")

    if not creds_path.exists():
        print(f"OAuth client not found at {creds_path}.", file=sys.stderr)
        return 1

    print(f"Requesting scopes:\n  " + "\n  ".join(SCOPES))
    print(f"Using OAuth client: {creds_path}")
    print("Launching browser consent (a local server will catch the redirect)...\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh_token is returned,
    # even if this account previously consented (Google otherwise omits it).
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")

    data = json.loads(creds.to_json())
    has_refresh = bool(data.get("refresh_token"))
    print(f"\nWrote token -> {token_path}")
    print(f"  has refresh_token: {has_refresh}")
    print(f"  granted scopes:    {data.get('scopes')}")
    if not has_refresh:
        print(
            "  WARNING: no refresh_token in the token — a headless worker will not "
            "be able to refresh. Re-run; ensure prompt=consent.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
