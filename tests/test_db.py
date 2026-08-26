import sqlite3

import pytest

from handoff import clock, db

DAY = 86400


def _folder(conn, slug, expires_at):
    conn.execute(
        "INSERT INTO folders (slug, title, created_at, last_post_at, expires_at)"
        " VALUES (?, ?, 0, 0, ?)",
        (slug, slug, expires_at),
    )
    conn.commit()


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO posts (folder, author, author_kind, source_format,"
            " source, html, created_at) VALUES ('nope', 'a', 'agent', 'md', '', '', 0)"
        )


def test_status_check_constraint_rejects_garbage(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO folders (slug, title, status, created_at, last_post_at,"
            " expires_at) VALUES ('a', 'a', 'sideways', 0, 0, 0)"
        )


def test_reap_deletes_only_expired_folders(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 10 * DAY)
    _folder(conn, "old", 9 * DAY)
    _folder(conn, "fresh", 11 * DAY)

    assert db.reap(conn) == 1

    slugs = [r["slug"] for r in conn.execute("SELECT slug FROM folders")]
    assert slugs == ["fresh"]


def test_reap_cascades_to_posts_and_blobs(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 10 * DAY)
    _folder(conn, "old", 9 * DAY)
    conn.execute(
        "INSERT INTO posts (id, folder, author, author_kind, source_format,"
        " source, html, created_at) VALUES (1, 'old', 'a', 'agent', 'md', '', '', 0)"
    )
    conn.execute(
        "INSERT INTO blobs (id, folder, post_id, filename, mime, bytes, created_at)"
        " VALUES ('b1', 'old', 1, 'x.png', 'image/png', X'00', 0)"
    )
    conn.commit()

    db.reap(conn)

    assert conn.execute("SELECT count(*) c FROM posts").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM blobs").fetchone()["c"] == 0


def test_reap_is_idempotent(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 10 * DAY)
    _folder(conn, "old", 9 * DAY)

    assert db.reap(conn) == 1
    assert db.reap(conn) == 0
    assert db.reap(conn) == 0


def test_init_schema_runs_twice_without_error(conn):
    db.init_schema(conn)
