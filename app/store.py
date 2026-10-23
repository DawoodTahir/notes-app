"""
Persistent storage for saved notes — the "bucket".

SQLite because it needs no server, survives restarts, and one local user will
never out-scale it. The database path is configurable so a deployment can point
it at a mounted disk:

    DATA_DIR=/var/data        ->  /var/data/notes.db

Note for hosted deployments: a container's own filesystem is usually ephemeral,
so DATA_DIR must point at real persistent storage or saved notes vanish on the
next deploy.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, Optional


def _db_path() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "notes.db"


@dataclass
class SavedNote:
    id: str
    user_id: str
    title: str
    url: str
    platform: str
    source: str
    topic: str
    note_kind: str
    notes: str
    transcript: str
    created_at: float
    category: str = "Unfiled"
    category_reason: str = ""
    subject: str = ""            # what the material argues, not what it was titled

    def summary(self) -> dict:
        """Row for the list view — omits the heavy text fields."""
        d = asdict(self)
        d.pop("transcript", None)
        d["notes_chars"] = len(self.notes)
        d["transcript_chars"] = len(self.transcript)
        d["preview"] = _preview(self.notes)
        # What the card shows: the argument, not the uploader's headline.
        d["label"] = self.subject or self.title
        d.pop("notes", None)
        return d


def _preview(notes: str, limit: int = 140) -> str:
    """First real sentence of the notes, with Markdown furniture stripped."""
    for raw in (notes or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        line = line.lstrip("-*> ").replace("**", "").replace("`", "")
        if len(line) > 20:
            return line[:limit] + ("…" if len(line) > limit else "")
    return (notes or "")[:limit]


_PRAGMAS_APPLIED = False


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """
    A connection that commits on success, rolls back on error, and closes.

    `with sqlite3.connect(...) as conn` commits but does *not* close, leaving
    the connection to be reaped whenever the last reference drops. Under WAL
    a lingering reader holds back checkpointing, so the close is explicit.

    WAL itself is the reason this matters on a hosted disk. Render's volume is
    network-backed, and rollback-journal mode fsyncs about four times per
    commit against WAL's single append. The job runner commits on every
    progress update during a multi-minute job, and in rollback mode each of
    those holds an exclusive lock across the flush, stalling the very requests
    that are polling for progress.
    """
    global _PRAGMAS_APPLIED
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if not _PRAGMAS_APPLIED:
            # journal_mode is a property of the database file and survives;
            # synchronous is per-connection, so it is set every time.
            # The pragma reports the mode it ended up in rather than raising,
            # so only latch the flag once WAL actually took.
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode and str(mode[0]).lower() == "wal":
                _PRAGMAS_APPLIED = True
        conn.execute("PRAGMA synchronous=NORMAL")   # durable under WAL
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# Generation context
#
# What a note was made from — transcript, title, source URL — kept so that
# chat, research and Save can still reach it after the fact. This used to be
# a process-global dict capped at fifty entries, which meant the transcript
# was silently dropped whenever the cap was reached or the process restarted:
# the Save succeeded, returned 200, and wrote an empty transcript that
# nothing could ever recover.
# ─────────────────────────────────────────────────────────────

CONTEXT_TTL_SECONDS = 7 * 24 * 3600


def put_context(sid: str, data: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO contexts (sid, payload, created_at) VALUES (?,?,?)",
            (sid, json.dumps(data), time.time()),
        )
        conn.execute("DELETE FROM contexts WHERE created_at < ?",
                     (time.time() - CONTEXT_TTL_SECONDS,))


def get_context(sid: str) -> dict:
    if not sid:
        return {}
    with _connect() as conn:
        row = conn.execute("SELECT payload FROM contexts WHERE sid = ?", (sid,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["payload"])
    except (ValueError, TypeError):
        return {}


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contexts (
                sid         TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                created_at  REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_notes (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL DEFAULT '',
                title       TEXT NOT NULL DEFAULT '',
                url         TEXT NOT NULL DEFAULT '',
                platform    TEXT NOT NULL DEFAULT '',
                source      TEXT NOT NULL DEFAULT '',
                topic       TEXT NOT NULL DEFAULT '',
                note_kind   TEXT NOT NULL DEFAULT '',
                notes       TEXT NOT NULL DEFAULT '',
                transcript  TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                category    TEXT NOT NULL DEFAULT 'Unfiled',
                category_reason TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON saved_notes(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_notes(user_id)")

        # Filing came after the first notes were saved — add the columns if missing.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(saved_notes)")}
        if "category" not in cols:
            conn.execute("ALTER TABLE saved_notes ADD COLUMN category TEXT NOT NULL DEFAULT 'Unfiled'")
        if "category_reason" not in cols:
            conn.execute("ALTER TABLE saved_notes ADD COLUMN category_reason TEXT NOT NULL DEFAULT ''")
        if "subject" not in cols:
            conn.execute("ALTER TABLE saved_notes ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_cat ON saved_notes(user_id, category)")


def save_note(*, user_id: str, title: str, url: str, platform: str, source: str,
              topic: str, note_kind: str, notes: str, transcript: str,
              category: str = "Unfiled", category_reason: str = "",
              subject: str = "") -> SavedNote:
    row = SavedNote(
        id=uuid.uuid4().hex[:16], user_id=user_id,
        title=title or "Untitled", url=url or "", platform=platform or "",
        source=source or "", topic=topic or "", note_kind=note_kind or "",
        notes=notes or "", transcript=transcript or "", created_at=time.time(),
        category=category or "Unfiled", category_reason=category_reason or "",
        subject=subject or "",
    )
    with _connect() as conn:
        conn.execute(
            "INSERT INTO saved_notes (id,user_id,title,url,platform,source,topic,"
            "note_kind,notes,transcript,created_at,category,category_reason,subject) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row.id, row.user_id, row.title, row.url, row.platform, row.source,
             row.topic, row.note_kind, row.notes, row.transcript, row.created_at,
             row.category, row.category_reason, row.subject),
        )
    return row


def list_notes(user_id: str, limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM saved_notes WHERE user_id = ? "
            "ORDER BY category COLLATE NOCASE, created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [SavedNote(**dict(r)).summary() for r in rows]


def get_note(note_id: str, user_id: str) -> Optional[SavedNote]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM saved_notes WHERE id = ? AND user_id = ?",
                         (note_id, user_id)).fetchone()
    return SavedNote(**dict(r)) if r else None


def delete_note(note_id: str, user_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM saved_notes WHERE id = ? AND user_id = ?",
                           (note_id, user_id))
    return cur.rowcount > 0


def list_categories(user_id: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) n FROM saved_notes WHERE user_id = ? "
            "GROUP BY category ORDER BY n DESC", (user_id,),
        ).fetchall()
    return [r["category"] for r in rows if r["category"]]


def recategorise(note_id: str, user_id: str, category: str, reason: str = "",
                 subject: Optional[str] = None) -> bool:
    sets, args = ["category = ?", "category_reason = ?"], [category, reason]
    if subject:
        sets.append("subject = ?"); args.append(subject)
    args += [note_id, user_id]
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE saved_notes SET {', '.join(sets)} WHERE id = ? AND user_id = ?", args)
    return cur.rowcount > 0


def list_all(user_id: str) -> list[dict]:
    """Full rows, every folder — for maintenance passes over the library."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM saved_notes WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)).fetchall()
    return [dict(r) for r in rows]


def notes_in(user_id: str, category: str) -> list[dict]:
    """Full rows for one folder — used when refiling needs the note text."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM saved_notes WHERE user_id = ? AND category = ? "
            "ORDER BY created_at DESC", (user_id, category),
        ).fetchall()
    return [dict(r) for r in rows]


def count_notes(user_id: str) -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM saved_notes WHERE user_id = ?",
                            (user_id,)).fetchone()[0]


if __name__ == "__main__":
    init_db()
    print("db:", _db_path())
    import sys
    uid = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(list_notes(uid), indent=2)[:600])
