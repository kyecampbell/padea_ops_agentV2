"""Database connection helper.

Responsibility: manage Postgres (Supabase) connectivity via psycopg v3. Provide a
connection helper configured from DATABASE_URL, with sensible defaults for the
agent's mostly-short-lived queries.

Conventions:
  - psycopg[binary], psycopg v3 API.
  - Money columns are integer cents; timestamp columns are timezone-aware.

Pooler safety: connections are opened with prepare_threshold=None so we never
issue server-side prepared statements. Supabase's transaction pooler (port 6543)
does not support them; disabling keeps us compatible with either pooler mode.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qsl

import psycopg
from psycopg import Connection

from config.settings import settings

# Bounded retry only for transient connection failures.
_MAX_RETRIES = 2
_BACKOFF_SECONDS = 0.5


def _parse_database_url(url: str) -> dict[str, str]:
    """Split a postgres URL into explicit psycopg connection kwargs.

    libpq's own URI parser silently mangles credentials that contain reserved
    characters ($ & ] @ : …) unless they are percent-encoded. Our DATABASE_URL
    carries a raw, unencoded Supabase password, so we split the components out
    ourselves and treat them as literal values: split host off at the LAST '@'
    (the password may contain '@'), then split user from password at the FIRST
    ':' (the user has no ':'). Returns a kwargs dict for psycopg.connect().
    """
    if "://" not in url:
        # Not a URL (e.g. a key=value conninfo) — let psycopg handle it as-is.
        return {"conninfo": url}

    _, rest = url.split("://", 1)
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)

    userinfo, sep, hostpart = rest.rpartition("@")
    if not sep:  # no credentials present
        hostpart = rest

    kwargs: dict[str, str] = {}
    if sep:
        user, colon, password = userinfo.partition(":")
        if user:
            kwargs["user"] = user
        if colon:
            kwargs["password"] = password

    hostport, slash, dbname = hostpart.partition("/")
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        kwargs["host"], kwargs["port"] = host, port
    elif hostport:
        kwargs["host"] = hostport
    if dbname:
        kwargs["dbname"] = dbname

    for key, value in parse_qsl(query):
        kwargs[key] = value

    return kwargs


@contextmanager
def get_conn() -> Iterator[Connection]:
    """Yield a psycopg v3 connection built from settings.DATABASE_URL.

    Connection establishment is retried up to _MAX_RETRIES times with a short
    backoff, but ONLY for transient errors (OperationalError). Programming /
    SQL errors are never retried — they surface immediately. The connection is
    always closed on exit.
    """
    conn = _connect_with_retry()
    try:
        yield conn
    finally:
        conn.close()


# Markers of non-transient connection failures we must NOT retry. Retrying an
# auth failure against the Supabase pooler trips its circuit breaker and blocks
# *all* new connections for a while, so these surface immediately.
_NON_TRANSIENT_MARKERS = (
    "authentication failed",
    "password",
    "circuitbreaker",
    "no pg_hba.conf entry",
    "database",  # e.g. "database ... does not exist"
)


def _is_transient(exc: psycopg.OperationalError) -> bool:
    """True only for connectivity/handshake failures worth a bounded retry.

    A returned SQLSTATE means the server answered with a definite error — auth
    (class 28), invalid catalog (3D000), etc. — so we don't retry. Likewise any
    auth / circuit-breaker message. Errors with no SQLSTATE are pre-handshake
    network failures (refused, timeout, DNS) and are retryable.
    """
    if exc.sqlstate is not None:
        return False
    message = str(exc).lower()
    return not any(marker in message for marker in _NON_TRANSIENT_MARKERS)


def _connect_with_retry() -> Connection:
    """Open a connection, retrying only transient failures with bounded backoff."""
    conn_kwargs = _parse_database_url(settings.database_url)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return psycopg.connect(
                prepare_threshold=None,  # pooler safety — no server-side prepares
                **conn_kwargs,
            )
        except psycopg.OperationalError as exc:
            if attempt < _MAX_RETRIES and _is_transient(exc):
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
    # Unreachable: the loop either returns a connection or raises.
    raise AssertionError("unreachable")


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """Run a read query and return all rows as a list of tuples."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
