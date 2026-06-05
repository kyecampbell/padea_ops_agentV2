"""Application settings loader.

Responsibility: load configuration from two sources and expose it as a single
typed, validated settings object:
  1. Secrets / environment from `.env` (DATABASE_URL, ANTHROPIC_API_KEY, Gmail
     paths, EMAIL_MODE, DEMO_SINK_EMAIL).
  2. Tunable runtime thresholds from `config/runtime_config.yaml` (per-incident
     call cap, poll interval, etc.).

Built on pydantic-settings so values are type-checked at startup and missing
required secrets fail loudly rather than at first use.

Conventions:
  - Money is integer cents; any monetary setting is an int, never a float.
  - EMAIL_MODE defaults to "demo": outbound mail is redirected to DEMO_SINK_EMAIL.
"""

from __future__ import annotations

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

    @classmethod
    def load(cls) -> "Settings":
        """Construct settings, merging YAML runtime config over the defaults."""
        return cls(**_load_runtime_config())


# Module-level singleton — import `settings` everywhere.
settings = Settings.load()
