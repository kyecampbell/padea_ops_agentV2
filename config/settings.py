"""Application settings loader.

Responsibility: load configuration from two sources and expose it as a single
typed, validated settings object:
  1. Secrets / environment from `.env` (DATABASE_URL, ANTHROPIC_API_KEY, Gmail
     paths, EMAIL_MODE, DEMO_SINK_EMAIL).
  2. Tunable runtime thresholds from `config/runtime_config.yaml` (per-incident
     call cap, poll interval, etc.).

Built on pydantic-settings so values are type-checked at startup and missing
required secrets fail loudly rather than at first use.

Loading precedence (pydantic-settings): real OS environment variables OVERRIDE
the `.env` file, and the `.env` file is optional — in production (Render) every
value comes from `os.environ` and no `.env` need exist on disk. Locally, `.env`
fills the gaps.

Cloud secret delivery: the Gmail OAuth client + cached token may be shipped as
JSON *contents* in GMAIL_CREDENTIALS_JSON / GMAIL_TOKEN_JSON; at startup these are
written to a writable dir and the *_path settings repointed there (see
``_materialize_gmail_secrets``), so the in-process token refresh keeps working.

Conventions:
  - Money is integer cents; any monetary setting is an int, never a float.
  - EMAIL_MODE defaults to "demo": outbound mail is redirected to DEMO_SINK_EMAIL.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = parent of this config/ directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime_config.yaml"


def _load_runtime_config() -> dict:
    """Read tunable thresholds from runtime_config.yaml (empty dict if absent)."""
    if not RUNTIME_CONFIG_PATH.exists():
        return {}
    with RUNTIME_CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Settings(BaseSettings):
    """Typed, validated application settings.

    Secrets come from `.env`; tunable runtime thresholds are merged in from
    `runtime_config.yaml` at construction time.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Secrets / environment (.env) ---
    database_url: str = Field(..., alias="DATABASE_URL")
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")

    # demo  — autonomous sends are redirected to DEMO_SINK_EMAIL (real recipient
    #         preserved in the audit trail). The default.
    # live  — autonomous sends go to the real recipient.
    # dry   — NOTHING is sent: autonomous mail is logged with status 'drafted'
    #         (no Gmail call), so a dry run shows exactly what WOULD be sent.
    email_mode: Literal["demo", "live", "dry"] = Field("demo", alias="EMAIL_MODE")
    demo_sink_email: str | None = Field(None, alias="DEMO_SINK_EMAIL")

    gmail_credentials_path: str | None = Field(None, alias="GMAIL_CREDENTIALS_PATH")
    gmail_token_path: str | None = Field(None, alias="GMAIL_TOKEN_PATH")

    # Cloud (Render) secret delivery: the Gmail OAuth client + cached token can be
    # shipped as JSON *contents* in env vars instead of files on a (read-only) disk.
    # When set, ``_materialize_gmail_secrets`` writes them to a WRITABLE dir at
    # startup and repoints the *_path fields there — so the in-process token refresh
    # (which rewrites the token file) still works. Local dev leaves these unset and
    # keeps using the file paths above.
    gmail_credentials_json: str | None = Field(None, alias="GMAIL_CREDENTIALS_JSON")
    gmail_token_json: str | None = Field(None, alias="GMAIL_TOKEN_JSON")
    # Writable dir the JSON envs are materialized into (default: a temp subdir).
    gmail_secrets_dir: str | None = Field(None, alias="GMAIL_SECRETS_DIR")

    # --- Cockpit login (Flask) ---
    # Two fixed operator accounts; passwords come from env. The session secret falls
    # back to a random per-process value when unset (fine for local dev; set it in
    # prod so sessions survive a restart). No user DB.
    cockpit_secret_key: str | None = Field(None, alias="COCKPIT_SECRET_KEY")
    cockpit_password_kye: str | None = Field(None, alias="COCKPIT_PASSWORD_KYE")
    cockpit_password_dylan: str | None = Field(None, alias="COCKPIT_PASSWORD_DYLAN")

    # --- Worker schedule (runtime_config.yaml or env) ---
    # APScheduler cron fields for the two weekly incidents, in scheduler_timezone.
    # Defaults: Thursday 09:00 (order run), Monday 08:00 (quality review), Brisbane.
    scheduler_timezone: str = "Australia/Brisbane"
    thursday_batch_day: str = "thu"
    thursday_batch_hour: int = 9
    thursday_batch_minute: int = 0
    quality_review_day: str = "mon"
    quality_review_hour: int = 8
    quality_review_minute: int = 0
    # How late a fire may run after its scheduled time and still execute (vs. being
    # dropped as a misfire) — covers a long-running job or a worker restart.
    misfire_grace_seconds: int = 3600

    # --- Tunable runtime thresholds (runtime_config.yaml) ---
    per_incident_call_cap: int = 8
    poll_interval_seconds: int = 300
    recall_case_limit: int = 5
    # Fraction of a caterer's weekly cohort assumed absent when sizing the
    # Thursday batch's MOQ tier (see runtime_config.yaml).
    typical_absence_rate: float = 0.08
    # Extra buffer (fraction of the cohort) subtracted from expected orders when
    # choosing V_max, so an above-typical absence spike can't tip the order below
    # the MOQ floor of the variety tier we committed to (never breach).
    moq_safety_margin: float = 0.05
    # How many weeks of order history the per-student meal rotation looks back
    # over; a meal a student received within this window is avoided in favour of
    # one they have not had recently (the cross-week variety metric).
    rotation_lookback_weeks: int = 3

    def model_post_init(self, __context: object) -> None:
        """After validation, materialize any env-supplied Gmail secrets to disk."""
        self._materialize_gmail_secrets()

    def _materialize_gmail_secrets(self) -> None:
        """Write GMAIL_*_JSON env contents to a writable dir and repoint the paths.

        Cloud (Render) ships the Gmail OAuth client + token as JSON env vars rather
        than committed files. We write them to a WRITABLE directory at startup and
        set ``gmail_credentials_path`` / ``gmail_token_path`` to those files, so the
        Gmail client's in-process token refresh (which rewrites the token file) works
        even when the app image / mount is read-only. No-op when the JSON envs are
        unset (local dev keeps the existing file paths).
        """
        if not (self.gmail_credentials_json or self.gmail_token_json):
            return
        base = (
            Path(self.gmail_secrets_dir)
            if self.gmail_secrets_dir
            else Path(tempfile.gettempdir()) / "padea_gmail"
        )
        base.mkdir(parents=True, exist_ok=True)
        for contents, filename, attr in (
            (self.gmail_credentials_json, "gmail_credentials.json", "gmail_credentials_path"),
            (self.gmail_token_json, "gmail_token.json", "gmail_token_path"),
        ):
            if not contents:
                continue
            path = base / filename
            path.write_text(contents, encoding="utf-8")
            try:
                path.chmod(0o600)  # best-effort; some filesystems ignore chmod.
            except OSError:
                pass
            setattr(self, attr, str(path))

    @classmethod
    def load(cls) -> "Settings":
        """Construct settings, merging YAML runtime config over the defaults."""
        return cls(**_load_runtime_config())


# Module-level singleton — import `settings` everywhere.
settings = Settings.load()
