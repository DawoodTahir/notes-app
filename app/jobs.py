"""
Background jobs.

Generating notes takes anywhere from five seconds to four and a half minutes,
which is longer than a phone will hold a request open: iOS suspends network
activity as soon as the app goes to the background, and every hosting proxy
has its own idle timeout well under that.

So the slow work moves off the request. A client POSTs, gets a job id back
immediately, and polls until it is done. The same contract serves the web page
and the iOS app.

Jobs live in the same SQLite database as everything else and are executed by a
small thread pool in this process. That is deliberate for ~20 users: no broker,
no second service to deploy or pay for. It does mean jobs are bound to one
instance — if this ever runs more than one replica, the worker belongs in a
separate process reading the same table with a claim query.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from .store import _connect

# Jobs are I/O-bound (HTTP to Apify, HTTP to the model), so threads are the
# right shape. Four at once is plenty for a handful of people and keeps us
# well inside the model provider's per-minute token budget.
MAX_WORKERS = 4
JOB_TTL_SECONDS = 24 * 3600

QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"

_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()

# kind -> callable(payload: dict, progress: callable) -> dict
_handlers: dict[str, Callable[..., dict]] = {}


def init_jobs() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL,
                status      TEXT NOT NULL,
                step        TEXT NOT NULL DEFAULT '',
                payload     TEXT NOT NULL DEFAULT '{}',
                result      TEXT NOT NULL DEFAULT '',
                error       TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC)")
        # A job left RUNNING is one the process died during; it will never
        # finish, so fail it honestly rather than leave a client polling.
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE status IN (?, ?)",
            (ERROR, "The server restarted while this job was running.", time.time(),
             RUNNING, QUEUED),
        )


def register(kind: str, fn: Callable[..., dict]) -> None:
    _handlers[kind] = fn


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                       thread_name_prefix="job")
    return _pool


# ─────────────────────────────────────────────────────────────
# Row access
# ─────────────────────────────────────────────────────────────

def _row(job_id: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(r) if r else None


def _set(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?",
                     [*fields.values(), job_id])


def public(row: dict) -> dict:
    """The shape a client polls. Result is only attached once it exists."""
    out = {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "step": row["step"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["status"] == DONE and row["result"]:
        try:
            out["result"] = json.loads(row["result"])
        except Exception:
            out["result"] = None
    if row["status"] == ERROR:
        out["error"] = row["error"]
    return out


# ─────────────────────────────────────────────────────────────
# Running
# ─────────────────────────────────────────────────────────────

def submit(kind: str, payload: dict, user_id: str = "") -> str:
    if kind not in _handlers:
        raise ValueError(f"No handler registered for job kind {kind!r}")

    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id,user_id,kind,status,payload,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, user_id, kind, QUEUED, json.dumps(payload), now, now),
        )
    _get_pool().submit(_run, job_id)
    return job_id


def _run(job_id: str) -> None:
    row = _row(job_id)
    if not row or row["status"] != QUEUED:
        return

    _set(job_id, status=RUNNING, step="starting")

    def progress(step: str) -> None:
        _set(job_id, step=step[:120])

    try:
        payload = json.loads(row["payload"] or "{}")
        result = _handlers[row["kind"]](payload, progress)
        _set(job_id, status=DONE, step="", result=json.dumps(result))
    except Exception as e:
        # The message reaches a user, so keep it to the message — the traceback
        # goes to the log where it is useful.
        traceback.print_exc()
        _set(job_id, status=ERROR, step="", error=str(e)[:600] or e.__class__.__name__)


def get(job_id: str, user_id: str = "") -> Optional[dict]:
    row = _row(job_id)
    if not row:
        return None
    if user_id and row["user_id"] and row["user_id"] != user_id:
        return None            # someone else's job is simply not found
    return row


def recent(user_id: str, limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [public(dict(r)) for r in rows]


def sweep() -> int:
    """Drop finished jobs older than the TTL so the table stays small."""
    cutoff = time.time() - JOB_TTL_SECONDS
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE updated_at < ? AND status IN (?, ?)",
            (cutoff, DONE, ERROR),
        )
    return cur.rowcount
