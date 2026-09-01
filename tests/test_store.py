import sqlite3

import pytest

from handoff import clock, db, store

DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32


@pytest.mark.parametrize("slug", ["a", "myslop-pr-42", "kypost-tls-migration", "x" * 64])
def test_valid_slugs_accepted(slug):
    assert store.valid_slug(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "-leading",
        "UPPER",
        "has space",
        "has_underscore",
        "../etc/passwd",
        "/absolute",
        "a/b",
        "x" * 65,
        "café",
        "a.b",
        "dot.",
    ],
)
def test_invalid_slugs_rejected(slug):
    assert not store.valid_slug(slug)


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_sniff_recognises_allowed_images(data, expected):
    assert store.sniff_mime(data) == expected


@pytest.mark.parametrize("data", [b"<html>hi</html>", b"", b"%PDF-1.4", b"MZ\x90\x00"])
def test_sniff_rejects_everything_else(data):
    assert store.sniff_mime(data) is None


def test_safe_filename_strips_paths_and_exotic_characters():
    assert store.safe_filename("../../etc/passwd") == "passwd"
    assert store.safe_filename("a b;c.png") == "a-b-c.png"
    assert store.safe_filename("") == "file"


def test_safe_filename_never_returns_dot_or_dotdot():
    # Only reaches a Content-Disposition header today, but "." or ".." surviving
    # as a "safe" filename is a footgun waiting for a future caller.
    assert store.safe_filename(".") == "file"
    assert store.safe_filename("..") == "file"


def test_create_folder_is_idempotent(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 1000)
    first = store.create_folder(conn, "myslop-pr-42", "PR 42", 7)
    second = store.create_folder(conn, "myslop-pr-42", "different title", 7)

    assert second["slug"] == first["slug"]
    assert second["title"] == "PR 42"
    assert conn.execute("SELECT count(*) c FROM folders").fetchone()["c"] == 1


def test_create_folder_rejects_bad_slug(conn):
    with pytest.raises(store.Invalid):
        store.create_folder(conn, "../etc", "nope", 7)


def test_create_folder_raises_not_found_instead_of_none_if_reaped_immediately(conn, monkeypatch):
    """If a reap lands between create_folder's own commit and its read-back (or,
    degenerately, ttl_days=0 expires the row the instant it's written), the read
    must not hand back None for the caller to crash on -- it's a 404."""
    monkeypatch.setattr(store, "get_folder", lambda c, s: None)
    with pytest.raises(store.NotFound):
        store.create_folder(conn, "s", "S", 7)


def test_create_folder_accepts_title_at_the_length_cap(conn):
    store.create_folder(conn, "s", "x" * store.MAX_FOLDER_TITLE_CHARS, 7)


def test_create_folder_rejects_title_over_the_length_cap(conn):
    with pytest.raises(store.Invalid):
        store.create_folder(conn, "s", "x" * (store.MAX_FOLDER_TITLE_CHARS + 1), 7)
    assert store.get_folder(conn, "s") is None


def test_add_post_extends_expiry(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)
    assert store.get_folder(conn, "s")["expires_at"] == 1000 + 7 * DAY

    t[0] = 1000 + 6 * DAY
    store.add_post(conn, "s", "opus", "agent", "t", "md", "body", ttl_days=7)
    assert store.get_folder(conn, "s")["expires_at"] == 1000 + 13 * DAY


def test_expired_folder_is_invisible_before_the_reaper_runs(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)

    t[0] = 1000 + 8 * DAY
    assert store.get_folder(conn, "s") is None
    assert store.list_folders(conn) == []
    assert conn.execute("SELECT count(*) c FROM folders").fetchone()["c"] == 1


def test_add_post_stores_source_and_rendered_html(conn):
    store.create_folder(conn, "s", "S", 7)
    pid = store.add_post(conn, "s", "opus", "agent", "T", "md", "# hi", ttl_days=7)

    row = conn.execute("SELECT * FROM posts WHERE id = ?", (pid,)).fetchone()
    assert row["source"] == "# hi"
    assert "<h1>hi</h1>" in row["html"]


def test_add_post_sanitizes(conn):
    store.create_folder(conn, "s", "S", 7)
    pid = store.add_post(
        conn, "s", "opus", "agent", "T", "html", "<script>alert(1)</script>", ttl_days=7
    )
    row = conn.execute("SELECT html FROM posts WHERE id = ?", (pid,)).fetchone()
    assert "<script" not in row["html"]


def test_add_post_stores_images_and_resolves_references(conn):
    store.create_folder(conn, "s", "S", 7)
    pid = store.add_post(
        conn,
        "s",
        "opus",
        "agent",
        "T",
        "md",
        "![a](img:arch.png)",
        images=[("arch.png", PNG)],
        ttl_days=7,
    )

    blob = conn.execute("SELECT * FROM blobs WHERE post_id = ?", (pid,)).fetchone()
    assert blob["mime"] == "image/png"
    assert blob["folder"] == "s"

    html = conn.execute("SELECT html FROM posts WHERE id = ?", (pid,)).fetchone()["html"]
    assert f'src="/f/s/blob/{blob["id"]}"' in html


def test_add_post_rejects_a_png_that_is_actually_html(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(
            conn,
            "s",
            "opus",
            "agent",
            "T",
            "md",
            "x",
            images=[("evil.png", b"<html><script>alert(1)</script></html>")],
            ttl_days=7,
        )


def test_add_post_to_missing_folder_raises(conn):
    with pytest.raises(store.NotFound):
        store.add_post(conn, "nope", "opus", "agent", "T", "md", "x", ttl_days=7)


def test_add_post_rejects_unknown_format(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(conn, "s", "opus", "agent", "T", "pdf", "x", ttl_days=7)


def test_list_posts_since_returns_only_newer(conn):
    store.create_folder(conn, "s", "S", 7)
    first = store.add_post(conn, "s", "opus", "agent", "1", "md", "a", ttl_days=7)
    second = store.add_post(conn, "s", "opus", "agent", "2", "md", "b", ttl_days=7)

    assert [p["id"] for p in store.list_posts(conn, "s")] == [first, second]
    assert [p["id"] for p in store.list_posts(conn, "s", since=first)] == [second]


def test_set_status_rejects_unknown_status(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.set_status(conn, "s", "sideways", None)


def test_set_status_updates_status_and_owner(conn):
    store.create_folder(conn, "s", "S", 7)
    store.set_status(conn, "s", "claimed", "opus-desktop")

    row = store.get_folder(conn, "s")
    assert row["status"] == "claimed"
    assert row["owner"] == "opus-desktop"


def test_blob_is_scoped_to_its_folder(conn):
    store.create_folder(conn, "a", "A", 7)
    store.create_folder(conn, "b", "B", 7)
    store.add_post(conn, "a", "opus", "agent", "T", "md", "x", images=[("i.png", PNG)], ttl_days=7)
    blob_id = conn.execute("SELECT id FROM blobs").fetchone()["id"]

    assert store.get_blob(conn, "a", blob_id) is not None
    assert store.get_blob(conn, "b", blob_id) is None


def test_add_post_rejects_colliding_sanitized_filenames(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(
            conn,
            "s",
            "opus",
            "agent",
            "T",
            "md",
            "![a](img:a-b.png) and ![b](img:a b.png)",
            images=[("a-b.png", PNG), ("a b.png", JPEG)],
            ttl_days=7,
        )


def test_add_post_resolves_distinct_filenames_to_distinct_blobs(conn):
    store.create_folder(conn, "s", "S", 7)
    pid = store.add_post(
        conn,
        "s",
        "opus",
        "agent",
        "T",
        "md",
        "![a](img:cat.png) and ![b](img:dog.png)",
        images=[("cat.png", PNG), ("dog.png", JPEG)],
        ttl_days=7,
    )

    blobs = {
        b["filename"]: b["id"]
        for b in conn.execute("SELECT * FROM blobs WHERE post_id = ?", (pid,)).fetchall()
    }
    assert len(blobs) == 2

    html = conn.execute("SELECT html FROM posts WHERE id = ?", (pid,)).fetchone()["html"]
    assert f'src="/f/s/blob/{blobs["cat.png"]}"' in html
    assert f'src="/f/s/blob/{blobs["dog.png"]}"' in html


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../../etc/passwd",
        "a b;c.png",
        "café.png",
        "a" * 100,
        "a" * 63 + "-" + "b" * 20 + ".png",
        "---",
        ".hidden",
        "a" * 64 + "-" * 10,
        ".",
        "..",
    ],
)
def test_safe_filename_is_idempotent(name):
    once = store.safe_filename(name)
    assert store.safe_filename(once) == once


def test_add_post_rejects_a_name_that_sanitizes_to_a_prior_raw_name(conn):
    # Truncation exposes a trailing hyphen that a second sanitize pass would
    # strip, so safe_filename(raw1) is not stable under re-sanitizing unless
    # safe_filename itself is idempotent. raw2 is exactly raw1's sanitized
    # form, so it must not be allowed to alias raw1's blob_urls entry.
    raw1 = "a" * 63 + "-" + "b" * 20 + ".png"
    raw2 = store.safe_filename(raw1)

    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(
            conn,
            "s",
            "opus",
            "agent",
            "T",
            "md",
            "x",
            images=[(raw1, PNG), (raw2, JPEG)],
            ttl_days=7,
        )


def test_recreating_an_expired_slug_does_not_resurrect_its_posts(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)
    store.add_post(
        conn,
        "s",
        "opus",
        "agent",
        "old",
        "md",
        "secret from the dead",
        images=[("evidence.png", PNG)],
        ttl_days=7,
    )

    t[0] = 1000 + 8 * DAY
    store.create_folder(conn, "s", "S again", 7)

    assert store.list_posts(conn, "s") == []
    assert conn.execute("SELECT count(*) c FROM posts").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM blobs").fetchone()["c"] == 0


def test_add_post_survives_a_reap_race_as_not_found(tmp_path, monkeypatch):
    """A folder can be reaped by another connection between add_post's own existence
    check and its INSERT. That must surface as NotFound, not a raw IntegrityError."""
    path = str(tmp_path / "handoff.db")
    conn = db.connect(path)
    db.init_schema(conn)

    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)

    # The folder expires and a second connection reaps it for real.
    t[0] += 8 * DAY
    reaper = db.connect(path)
    assert db.reap(reaper) == 1
    reaper.close()

    # Simulate add_post's existence check having run a moment earlier, before the
    # reap committed -- the row it saw is now gone by the time the INSERT runs.
    monkeypatch.setattr(store, "get_folder", lambda c, s: {"slug": s})

    with pytest.raises(store.NotFound):
        store.add_post(conn, "s", "opus", "agent", "t", "md", "body", ttl_days=7)

    conn.close()


def test_set_status_survives_a_reap_race_as_not_found(tmp_path, monkeypatch):
    """set_status must not check existence and then update separately: if a reap
    lands in between, the UPDATE would match zero rows and silently report success.
    Folding the expiry condition into the UPDATE itself closes that window -- proven
    here against a folder actually reaped out from under it by a second connection."""
    path = str(tmp_path / "handoff.db")
    conn = db.connect(path)
    db.init_schema(conn)

    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)

    # The folder expires and a second connection reaps it for real.
    t[0] += 8 * DAY
    reaper = db.connect(path)
    assert db.reap(reaper) == 1
    reaper.close()

    # Simulate set_status's own existence check having run a moment earlier, before
    # the reap committed -- the row it saw is now gone by the time the UPDATE runs.
    monkeypatch.setattr(store, "get_folder", lambda c, s: {"slug": s})

    with pytest.raises(store.NotFound):
        store.set_status(conn, "s", "claimed", "opus")

    conn.close()


def test_add_post_propagates_check_constraint_violations(conn):
    """A CHECK-constraint violation (e.g. a bad author_kind) is a programming
    error, not a reap race, and must not be swallowed into NotFound or Invalid --
    the add_post except clause only translates FOREIGN KEY violations."""
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_post(conn, "s", "opus", "not-a-real-kind", "t", "md", "body", ttl_days=7)


def test_add_post_rejects_total_image_bytes_over_ten_mb(conn):
    store.create_folder(conn, "s", "S", 7)
    chunk = PNG + b"\x00" * (4 * 1024 * 1024)  # 4MB, legal alone
    with pytest.raises(store.Invalid):
        store.add_post(
            conn,
            "s",
            "opus",
            "agent",
            "T",
            "md",
            "x",
            images=[("a.png", chunk), ("b.png", chunk), ("c.png", chunk)],
            ttl_days=7,
        )


def test_add_post_accepts_ascii_body_at_the_byte_cap(conn):
    """Plain ASCII: char count and byte count coincide, so this also pins the
    common case isn't off by one now that the check is byte-based."""
    store.create_folder(conn, "s", "S", 7)
    store.add_post(conn, "s", "opus", "agent", "T", "md", "x" * store.MAX_BODY_BYTES, ttl_days=7)


def test_add_post_rejects_ascii_body_over_the_byte_cap(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(
            conn, "s", "opus", "agent", "T", "md", "x" * (store.MAX_BODY_BYTES + 1), ttl_days=7
        )


def test_add_post_rejects_multibyte_body_over_byte_cap(conn):
    """Char count comfortably under the byte cap, utf-8 byte count over it -- the
    cap must bound bytes actually written to storage, not code points."""
    store.create_folder(conn, "s", "S", 7)
    chars = store.MAX_BODY_BYTES // 4 + 1
    body = "\U0001f600" * chars  # 4 utf-8 bytes each: char count << cap, byte count > cap
    with pytest.raises(store.Invalid):
        store.add_post(conn, "s", "opus", "agent", "T", "md", body, ttl_days=7)


def test_add_post_accepts_title_at_the_length_cap(conn):
    store.create_folder(conn, "s", "S", 7)
    store.add_post(
        conn, "s", "opus", "agent", "x" * store.MAX_TITLE_CHARS, "md", "body", ttl_days=7
    )


def test_add_post_rejects_title_over_the_length_cap(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(
            conn, "s", "opus", "agent", "x" * (store.MAX_TITLE_CHARS + 1), "md", "body", ttl_days=7
        )


def test_add_post_accepts_author_note_at_the_length_cap(conn):
    store.create_folder(conn, "s", "S", 7)
    store.add_post(
        conn,
        "s",
        "opus",
        "agent",
        "T",
        "md",
        "body",
        author_note="x" * store.MAX_AUTHOR_NOTE_CHARS,
        ttl_days=7,
    )


def test_add_post_rejects_author_note_over_the_length_cap(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(
            conn,
            "s",
            "opus",
            "agent",
            "T",
            "md",
            "body",
            author_note="x" * (store.MAX_AUTHOR_NOTE_CHARS + 1),
            ttl_days=7,
        )


def test_set_status_accepts_owner_at_the_length_cap(conn):
    store.create_folder(conn, "s", "S", 7)
    store.set_status(conn, "s", "claimed", "x" * store.MAX_OWNER_CHARS)


def test_set_status_rejects_owner_over_the_length_cap(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.set_status(conn, "s", "claimed", "x" * (store.MAX_OWNER_CHARS + 1))


def test_blob_is_invisible_after_folder_expiry(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)
    store.add_post(conn, "s", "opus", "agent", "T", "md", "x", images=[("i.png", PNG)], ttl_days=7)
    blob_id = conn.execute("SELECT id FROM blobs").fetchone()["id"]

    t[0] += 8 * DAY
    assert store.get_blob(conn, "s", blob_id) is None


def test_list_folders_puts_the_soonest_to_expire_first(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    # Alphabetical order would put these the other way round.
    store.create_folder(conn, "a", "A", 7)
    t[0] = 1000 + DAY
    store.create_folder(conn, "b", "B", 3)

    assert [f["slug"] for f in store.list_folders(conn)] == ["b", "a"]
