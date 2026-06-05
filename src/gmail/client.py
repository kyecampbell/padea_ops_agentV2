"""Thin Gmail API client.

Responsibility: a minimal wrapper over the Gmail API exposing just what the agent
needs — send a message and read the inbox. Credentials/token paths come from
settings (GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH).

OAuth is already provisioned: the cached token at GMAIL_TOKEN_PATH carries a
refresh token plus the gmail.send + gmail.modify scopes, so there is NO browser
consent flow here. The client loads the cached token, refreshes the access token
in-process via the stored refresh token when expired, and writes the refreshed
token back to disk.

Note on demo mode: this client performs the actual send/read. Demo-mode
redirection (routing real sends to the sink with the `[DEMO — Intended for: ...]`
prefix) is applied by the email tools (`src/tools/email.py`), not here. The
inbound read always reads the real inbox.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from config.settings import PROJECT_ROOT, settings

# The cached token already carries these scopes; declared for clarity/validation.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _resolve(path_str: str | None, label: str) -> Path:
    """Resolve a settings path (absolute, or relative to the project root)."""
    if not path_str:
        raise RuntimeError(f"{label} is not configured in settings/.env")
    path = Path(path_str)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


class GmailClient:
    """Minimal Gmail wrapper: send a message and read the inbox.

    A single instance loads credentials once and reuses the built service. The
    access token is auto-refreshed (and persisted) on construction if expired.
    """

    def __init__(self) -> None:
        self._creds = self._load_credentials()
        self._service = build("gmail", "v1", credentials=self._creds)
        self._address: str | None = None

    # --- identity ---------------------------------------------------------

    def address(self) -> str:
        """The authenticated account's own email address.

        This is BOTH the inbox the inbound poller reads and the account every
        outbound message is sent AS (``userId='me'``). The inbound poll uses it to
        recognise — and skip — our own sent/demo mail so it is never reprocessed as
        a new inbound message. Resolved once via the lightweight profile endpoint
        (no message access) and cached.
        """
        if self._address is None:
            profile = self._service.users().getProfile(userId="me").execute()
            self._address = profile.get("emailAddress", "") or ""
        return self._address

    # --- credentials / OAuth ---------------------------------------------

    def _load_credentials(self) -> Credentials:
        """Load the cached token and refresh it in-process if needed.

        No consent flow: a missing/invalid token without a refresh token is a
        hard error, since OAuth is expected to be provisioned out of band.
        """
        token_path = _resolve(settings.gmail_token_path, "GMAIL_TOKEN_PATH")
        if not token_path.exists():
            raise RuntimeError(
                f"Gmail token not found at {token_path}. OAuth must be "
                "provisioned out of band; this client does not run a consent flow."
            )

        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise RuntimeError(
                    "Gmail credentials are invalid and cannot be refreshed "
                    "(no refresh token). Re-provision the token out of band."
                )

        return creds

    # --- sending ----------------------------------------------------------

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
    ) -> dict:
        """Send a plain-text email; returns the Gmail API send response."""
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        if cc:
            message["Cc"] = cc
        message.set_content(body)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        return (
            self._service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )

    # --- reading ----------------------------------------------------------

    def list_unread(self, max_results: int = 25) -> list[dict]:
        """List unread inbox messages (id/threadId stubs from the API)."""
        return self._list(query="is:unread in:inbox", max_results=max_results)

    def list_recent(self, max_results: int = 25) -> list[dict]:
        """List the most recent inbox messages (id/threadId stubs)."""
        return self._list(query="in:inbox", max_results=max_results)

    def _list(self, query: str, max_results: int) -> list[dict]:
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return resp.get("messages", [])

    def get_message(self, message_id: str) -> dict:
        """Fetch a full message by id (format=full)."""
        return (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    def mark_read(self, message_id: str) -> dict:
        """Remove the UNREAD label from a message."""
        return (
            self._service.users()
            .messages()
            .modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            )
            .execute()
        )
