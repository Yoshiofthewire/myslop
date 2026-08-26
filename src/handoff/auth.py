"""Credentials, sessions, and CSRF.

All credential verification lives here. Adding OIDC later means adding a function
to this module and two routes -- nothing above it changes.
"""

import hashlib
import hmac
import secrets
import sqlite3
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from handoff import clock

SESSION_TTL = 30 * 86400
COOKIE_NAME = "handoff_session"

_ph = PasswordHasher()

# username -> (consecutive failures, unix time when attempts may resume)
_throttle: dict[str, tuple[int, int]] = {}
_THROTTLE_AFTER = 5
_THROTTLE_BASE = 2


def reset_throttle() -> None:
    """Clear login throttling. For tests and the CLI."""
    _throttle.clear()


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def mint_agent(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    """Create an agent and return (agent_id, plaintext token). The token is not recoverable."""
    agent_id = uuid.uuid4().hex
    token = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO agents (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, name, hash_token(token), clock.now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"agent name already in use: {name}") from exc
    conn.commit()
    return agent_id, token


def agent_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    digest = hash_token(token)
    row = conn.execute(
        "SELECT * FROM agents WHERE token_hash = ? AND revoked_at IS NULL", (digest,)
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        return None
    return row


def revoke_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute("UPDATE agents SET revoked_at = ? WHERE id = ?", (clock.now(), agent_id))
    conn.commit()


def list_agents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, _ph.hash(password), clock.now()),
    )
    conn.commit()
    return cur.lastrowid


def verify_user(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    failures, resume_at = _throttle.get(username, (0, 0))
    if clock.now() < resume_at:
        return None

    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    ok = False
    if row is not None and row["password_hash"]:
        try:
            ok = _ph.verify(row["password_hash"], password)
        except VerifyMismatchError:
            ok = False

    if not ok:
        failures += 1
        over = failures - _THROTTLE_AFTER + 1
        delay = 0 if failures < _THROTTLE_AFTER else _THROTTLE_BASE**over
        _throttle[username] = (failures, clock.now() + delay)
        return None

    _throttle.pop(username, None)
    return row


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    sid = secrets.token_urlsafe(32)
    now = clock.now()
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (sid, user_id, now, now + SESSION_TTL),
    )
    conn.commit()
    return sid


def session_user(conn: sqlite3.Connection, sid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id"
        " WHERE sessions.id = ? AND sessions.expires_at > ?",
        (sid, clock.now()),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    conn.commit()


def csrf_token(sid: str) -> str:
    return hmac.new(sid.encode(), b"csrf", hashlib.sha256).hexdigest()


def check_csrf(sid: str, token: str) -> bool:
    return hmac.compare_digest(csrf_token(sid), token or "")
