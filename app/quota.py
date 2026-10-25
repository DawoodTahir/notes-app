"""
Per-user daily caps on the endpoints that cost real money.

Registration is invite-only, so this is not about strangers. It is about the
blast radius of a mistake: a client stuck in a retry loop, a script someone
points at the API, or one enthusiastic user working through a playlist. Each
call to /api/notes can be dozens of model calls plus Apify credits, and
nothing anywhere else in the app puts a ceiling on that.

Counts live in the same SQLite database as everything else, keyed by UTC day,
so they survive a restart and need no extra service. Old rows are swept as
they are passed.
"""

from __future__ import annotations

import os
import time

from .store import _connect

# Generous for a person, ruinous for a loop. Override per deployment.
_DEFAULTS = {
    "notes": 60,
    "research": 40,
    "transcript": 60,
    "refile": 10,
    "digest": 15,
}


class QuotaExceeded(Exception):
    """Raised when a user has used up a day's allowance of something."""

    def __init__(self, kind: str, limit: int):
        self.kind, self.limit = kind, limit
        super().__init__(
            f"You have reached today's limit of {limit} {kind} requests. "
            f"It resets at midnight UTC."
        )


def limit_for(kind: str) -> int:
    """0 or less disables the cap for that kind."""
    raw = os.environ.get(f"LIMIT_{kind.upper()}")
    if raw is None or not raw.strip():
        return _DEFAULTS.get(kind, 0)
    try:
        return int(raw)
    except ValueError:
        return _DEFAULTS.get(kind, 0)


def init_quota() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id TEXT NOT NULL,
                day     TEXT NOT NULL,
                kind    TEXT NOT NULL,
                n       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day, kind)
            )
        """)


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def consume(user_id: str, kind: str) -> None:
    """
    Count one use, or raise QuotaExceeded.

    The read and the increment share one transaction, so two requests racing
    cannot both slip through on the last unit of allowance.
    """
    limit = limit_for(kind)
    if limit <= 0:
        return

    day = _today()
    with _connect() as conn:
        # One statement, so the check and the increment cannot be interleaved.
        # Reading the count and then writing it left a window where several
        # concurrent requests all saw the same "one left" and all went through
        # — four got past a limit of three in testing. SQLite serialises
        # writers, so the conditional upsert is the whole guard.
        changed = conn.execute(
            "INSERT INTO usage (user_id, day, kind, n) VALUES (?,?,?,1) "
            "ON CONFLICT(user_id, day, kind) DO UPDATE SET n = n + 1 WHERE n < ?",
            (user_id, day, kind, limit),
        ).rowcount
        if not changed:
            raise QuotaExceeded(kind, limit)
        conn.execute("DELETE FROM usage WHERE day < ?", (day,))


def today(user_id: str) -> dict:
    """What this user has used so far, for display."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT kind, n FROM usage WHERE user_id = ? AND day = ?",
            (user_id, _today()),
        ).fetchall()
    used = {r["kind"]: r["n"] for r in rows}
    return {k: {"used": used.get(k, 0), "limit": limit_for(k)} for k in _DEFAULTS}
