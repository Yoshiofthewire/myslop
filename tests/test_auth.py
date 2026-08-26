import pytest

from handoff import auth, clock


def test_mint_agent_returns_a_usable_token(conn):
    agent_id, token = auth.mint_agent(conn, "opus-desktop")

    row = auth.agent_by_token(conn, token)
    assert row is not None
    assert row["id"] == agent_id
    assert row["name"] == "opus-desktop"


def test_plaintext_token_is_never_stored(conn):
    _, token = auth.mint_agent(conn, "opus-desktop")

    stored = conn.execute("SELECT token_hash FROM agents").fetchone()["token_hash"]
    assert token.encode() not in stored
    assert stored == auth.hash_token(token)


def test_unknown_token_is_rejected(conn):
    auth.mint_agent(conn, "opus-desktop")
    assert auth.agent_by_token(conn, "00000000-0000-0000-0000-000000000000") is None


def test_revoked_token_is_rejected(conn):
    agent_id, token = auth.mint_agent(conn, "opus-desktop")
    auth.revoke_agent(conn, agent_id)
    assert auth.agent_by_token(conn, token) is None


def test_duplicate_agent_name_is_rejected(conn):
    auth.mint_agent(conn, "opus-desktop")
    with pytest.raises(ValueError):
        auth.mint_agent(conn, "opus-desktop")


def test_password_round_trip(conn):
    auth.create_user(conn, "yoshi", "correct horse battery staple")

    assert auth.verify_user(conn, "yoshi", "correct horse battery staple") is not None
    assert auth.verify_user(conn, "yoshi", "wrong") is None
    assert auth.verify_user(conn, "nobody", "correct horse battery staple") is None


def test_password_is_hashed_with_argon2id(conn):
    auth.create_user(conn, "yoshi", "hunter2")
    stored = conn.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
    assert stored.startswith("$argon2id$")
    assert "hunter2" not in stored


def test_repeated_failures_throttle_then_recover(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    auth.create_user(conn, "yoshi", "hunter2")
    auth.reset_throttle()

    for _ in range(5):
        assert auth.verify_user(conn, "yoshi", "wrong") is None

    # Correct password is refused while throttled.
    assert auth.verify_user(conn, "yoshi", "hunter2") is None

    t[0] += 3600
    assert auth.verify_user(conn, "yoshi", "hunter2") is not None


def test_session_round_trip(conn):
    uid = auth.create_user(conn, "yoshi", "hunter2")
    sid = auth.create_session(conn, uid)

    user = auth.session_user(conn, sid)
    assert user is not None
    assert user["username"] == "yoshi"

    auth.delete_session(conn, sid)
    assert auth.session_user(conn, sid) is None


def test_expired_session_is_rejected(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    uid = auth.create_user(conn, "yoshi", "hunter2")
    sid = auth.create_session(conn, uid)

    t[0] += auth.SESSION_TTL + 1
    assert auth.session_user(conn, sid) is None


def test_csrf_token_is_bound_to_the_session(conn):
    a = auth.csrf_token("session-a")
    b = auth.csrf_token("session-b")

    assert a != b
    assert auth.check_csrf("session-a", a)
    assert not auth.check_csrf("session-a", b)
    assert not auth.check_csrf("session-a", "")


class _SpyHasher:
    """Wraps the real PasswordHasher to count verify() calls. argon2-cffi's
    PasswordHasher has read-only attributes, so its methods can't be monkeypatched
    directly -- replace the module-level `_ph` reference instead."""

    def __init__(self, real):
        self._real = real
        self.calls = []

    def verify(self, *args, **kwargs):
        self.calls.append(args)
        return self._real.verify(*args, **kwargs)


def test_verify_user_reaches_argon2_for_unknown_username_too(conn, monkeypatch):
    """Wrong password and unknown username must pay the same argon2 cost, or the
    response time leaks whether an account exists."""
    auth.create_user(conn, "yoshi", "hunter2")
    auth.reset_throttle()

    spy = _SpyHasher(auth._ph)
    monkeypatch.setattr(auth, "_ph", spy)

    assert auth.verify_user(conn, "yoshi", "wrong") is None
    assert len(spy.calls) == 1

    spy.calls.clear()
    assert auth.verify_user(conn, "nobody", "wrong") is None
    assert len(spy.calls) == 1


def test_create_user_rejects_overlong_username(conn):
    with pytest.raises(ValueError):
        auth.create_user(conn, "a" * 65, "hunter2")


def test_verify_user_rejects_overlong_username_without_touching_throttle(conn):
    auth.reset_throttle()
    long_username = "a" * 65

    assert auth.verify_user(conn, long_username, "hunter2") is None
    assert long_username not in auth._throttle
