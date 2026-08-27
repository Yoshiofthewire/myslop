"""Credentials, sessions, and CSRF.

All credential verification lives here. Adding OIDC later means adding a function
to this module and two routes -- nothing above it changes.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from handoff import clock

SESSION_TTL = 30 * 86400
COOKIE_NAME = "handoff_session"
MAX_USERNAME_LEN = 64

# Lowercase slug only: no whitespace, no case variants, no invisible or confusable
# characters. A rendered name is what a human trusts to attribute a post -- a charset
# this narrow is the whole defense, not a first line of one, so it stays total rather
# than trying to enumerate the many ways Unicode can render two names identically.
AGENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

_ph = PasswordHasher()
# Verified on every login attempt for an unknown/passwordless username, so that path
# pays the same argon2 cost as a real one and can't be used to enumerate accounts.
_DUMMY_HASH = _ph.hash("handoff-dummy-password-for-constant-time-login")

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
    if not AGENT_NAME_RE.fullmatch(name):
        raise ValueError(
            "agent name must be lowercase letters, digits, and hyphens, 1-64 characters, "
            f"starting with a letter or digit: {name!r}"
        )
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
    # The SQL lookup above is an indexed equality match, not constant-time -- this
    # compare doesn't make the lookup itself timing-safe. Kept anyway per spec: the
    # compared value is a sha256 digest of attacker-supplied input, so any timing
    # signal about digest proximity gives no preimage advantage.
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        return None
    return row


def revoke_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute("UPDATE agents SET revoked_at = ? WHERE id = ?", (clock.now(), agent_id))
    conn.commit()


def list_agents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    if len(username) > MAX_USERNAME_LEN:
        raise ValueError(f"username exceeds {MAX_USERNAME_LEN} characters")
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, _ph.hash(password), clock.now()),
    )
    conn.commit()
    return cur.lastrowid


def verify_user(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    if len(username) > MAX_USERNAME_LEN:
        return None

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
    else:
        # No such user (or no password set): still pay the argon2 cost, so an
        # unknown username can't be distinguished from a wrong password by timing.
        try:
            _ph.verify(_DUMMY_HASH, password)
        except VerifyMismatchError:
            pass

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
