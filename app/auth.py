"""
Accounts, invite codes and sessions.

Invite-only: registration requires an unused invite code. Everything uses the
Python standard library — scrypt for password hashing, secrets for tokens — so
there are no new dependencies to audit or install.

Sessions are opaque random tokens stored server-side and handed out as an
HttpOnly cookie. That is deliberately simpler than signed JWTs: logout and
revocation actually work, because the server owns the state.

Bootstrapping: set ADMIN_EMAIL in .env. The first account registered with that
address becomes an admin and can mint invite codes; an initial code is printed
on first run so there is always a way in.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from .store import _connect  # same SQLite database

SESSION_DAYS = 30
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


class AuthError(Exception):
    """Raised for any credential, invite or permission failure."""


@dataclass
class User:
    id: str
    email: str
    plan: str            # "free" | "pro"
    is_admin: int
    created_at: float

    def public(self) -> dict:
        return {
            "id": self.id, "email": self.email, "plan": self.plan,
            "is_admin": bool(self.is_admin), "created_at": self.created_at,
        }


# The single owner used when authentication is switched off.
LOCAL_USER_ID = "local"


_OFF = ("0", "false", "no", "off")


def auth_required() -> bool:
    """
    On unless explicitly switched off.

    This used to default to off, which was right while the app only ever ran
    on localhost and wrong the moment it got a public URL: with auth off every
    anonymous visitor is served local_user(), who is an admin. Forgetting one
    environment variable therefore handed strangers the owner's notes and
    API credits.

    Defaulting the other way makes the failure mode safe. Local development
    sets REQUIRE_AUTH=0 in .env and is unaffected.
    """
    return (os.environ.get("REQUIRE_AUTH", "") or "").strip().lower() not in _OFF


def local_user() -> "User":
    return User(id=LOCAL_USER_ID, email="local", plan="free", is_admin=1, created_at=0.0)


def adopt_orphan_notes() -> int:
    """
    With auth off there is one owner, so earlier notes belong to them.

    Guarded on there being no registered accounts. Without that guard this
    reassigns *every* user's notes to the local owner, and it runs at import
    time — so a single deploy with REQUIRE_AUTH missing or misspelled would
    silently empty twenty people's libraries with no backup and no warning.
    """
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return 0
        moved = conn.execute("UPDATE saved_notes SET user_id = ? WHERE user_id != ?",
                             (LOCAL_USER_ID, LOCAL_USER_ID)).rowcount
    return moved


# ─────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────

def init_auth() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                email       TEXT NOT NULL UNIQUE,
                pw_hash     TEXT NOT NULL,
                pw_salt     TEXT NOT NULL,
                plan        TEXT NOT NULL DEFAULT 'free',
                is_admin    INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                code        TEXT PRIMARY KEY,
                created_by  TEXT,
                used_by     TEXT,
                created_at  REAL NOT NULL,
                used_at     REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                created_at  REAL NOT NULL,
                expires_at  REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id)")

        # Saved notes predate accounts — add the owner column if it is missing.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(saved_notes)")}
        if cols and "user_id" not in cols:
            conn.execute("ALTER TABLE saved_notes ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_notes(user_id)")


def bootstrap_invite() -> Optional[str]:
    """Guarantee a way in: mint one invite when there are no users yet."""
    with _connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return None
        row = conn.execute("SELECT code FROM invites WHERE used_by IS NULL LIMIT 1").fetchone()
        if row:
            return row["code"]
    return create_invite(created_by=None)


# ─────────────────────────────────────────────────────────────
# Passwords
# ─────────────────────────────────────────────────────────────

def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT).hex()


def _check(password: str, salt: str, expected: str) -> bool:
    return secrets.compare_digest(_hash(password, salt), expected)


# ─────────────────────────────────────────────────────────────
# Invites
# ─────────────────────────────────────────────────────────────

def create_invite(created_by: Optional[str]) -> str:
    code = secrets.token_urlsafe(9)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO invites (code, created_by, created_at) VALUES (?,?,?)",
            (code, created_by, time.time()),
        )
    return code


def list_invites() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT code, used_by, created_at, used_at FROM invites ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────
# Accounts
# ─────────────────────────────────────────────────────────────

def register(email: str, password: str, invite_code: str) -> User:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise AuthError("That does not look like an email address.")
    if len(password or "") < 8:
        raise AuthError("Use a password of at least 8 characters.")

    code = (invite_code or "").strip()
    with _connect() as conn:
        inv = conn.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
        if not inv:
            raise AuthError("That invite code is not valid.")
        if inv["used_by"]:
            raise AuthError("That invite code has already been used.")
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AuthError("There is already an account with that email.")

        salt = secrets.token_hex(16)
        uid = secrets.token_hex(8)
        admin = 1 if email == (os.environ.get("ADMIN_EMAIL") or "").strip().lower() else 0
        try:
            conn.execute(
                "INSERT INTO users (id,email,pw_hash,pw_salt,plan,is_admin,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, email, _hash(password, salt), salt, "free", admin, time.time()),
            )
        except sqlite3.IntegrityError:
            raise AuthError("There is already an account with that email.")
        conn.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?",
                     (uid, time.time(), code))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    return _user_from_row(row)


def login(email: str, password: str) -> tuple[User, str]:
    email = (email or "").strip().lower()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # Same message either way — never reveal which accounts exist.
    if not row or not _check(password or "", row["pw_salt"], row["pw_hash"]):
        raise AuthError("Wrong email or password.")
    user = _user_from_row(row)
    return user, _new_session(user.id)


def _user_from_row(row: sqlite3.Row) -> User:
    return User(id=row["id"], email=row["email"], plan=row["plan"],
                is_admin=row["is_admin"], created_at=row["created_at"])


# ─────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────

def _new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
            (token, user_id, now, now + SESSION_DAYS * 86400),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def user_for_token(token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, time.time()),
        ).fetchone()
    return _user_from_row(row) if row else None


def logout(token: Optional[str]) -> None:
    if token:
        with _connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def set_plan(user_id: str, plan: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))
