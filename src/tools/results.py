"""Typed tool results.

Responsibility: define the single vocabulary of outcomes every tool returns, so
the orchestrator reasons over typed states instead of catching exceptions.

Outcome kinds:
  - found       — the operation succeeded and returned data.
  - empty       — the operation succeeded but there was nothing to return.
  - ambiguous   — multiple plausible matches; the agent must disambiguate.
  - conflict    — data contradicts itself or a precondition (e.g. order already sent).
  - queued      — recorded and awaiting operator approval; NOT yet applied/sent.
  - unavailable — a dependency (DB, Gmail, script) is temporarily down.
  - error       — an unexpected failure, captured and returned as data, not raised.

Tools must NEVER raise to the caller. DB / transient failures become
`unavailable` or `error` results; "nothing found" becomes `empty`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Status = Literal[
    "found",
    "empty",
    "ambiguous",
    "conflict",
    "queued",
    "unavailable",
    "error",
]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A single typed outcome the agent reasons over.

    Attributes:
      status:  one of the Status literals above.
      data:    the payload on success (``found``), otherwise ``None``.
      message: a short human-readable explanation for the agent.
    """

    status: Status
    data: Any = None
    message: str = ""

    @property
    def ok(self) -> bool:
        """True only when data was actually returned."""
        return self.status == "found"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"ToolResult(status={self.status!r}, message={self.message!r})"


# --- Tiny constructors -------------------------------------------------------
# Each builds a ToolResult of one status. Use these instead of the raw
# constructor so call sites read as the outcome they express.


def found(data: Any, message: str = "") -> ToolResult:
    """The operation succeeded and returned `data`."""
    return ToolResult("found", data=data, message=message)


def empty(message: str = "") -> ToolResult:
    """The operation succeeded but there was nothing to return."""
    return ToolResult("empty", data=None, message=message)


def ambiguous(message: str, data: Any = None) -> ToolResult:
    """Multiple plausible matches; the agent must disambiguate."""
    return ToolResult("ambiguous", data=data, message=message)


def conflict(message: str, data: Any = None) -> ToolResult:
    """Data contradicts itself or a precondition."""
    return ToolResult("conflict", data=data, message=message)


def queued(message: str, data: Any = None) -> ToolResult:
    """Recorded and awaiting operator approval; not yet applied or sent."""
    return ToolResult("queued", data=data, message=message)


def unavailable(message: str) -> ToolResult:
    """A dependency (DB, Gmail, script) is temporarily down."""
    return ToolResult("unavailable", data=None, message=message)


def error(message: str) -> ToolResult:
    """An unexpected failure, captured and returned rather than raised."""
    return ToolResult("error", data=None, message=message)
