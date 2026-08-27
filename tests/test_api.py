import base64

from handoff import clock, store

DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_unauthenticated_request_is_rejected(client):
    assert client.get("/api/folders").status_code == 401


def test_bad_token_is_rejected(client, agent_token):
    r = client.get("/api/folders", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_create_and_list_folder(client, agent):
    r = client.post("/api/folders", json={"slug": "myslop-pr-42", "title": "PR 42"}, headers=agent)
    assert r.status_code == 200
    assert r.json()["slug"] == "myslop-pr-42"
    assert r.json()["status"] == "open"

    listed = client.get("/api/folders", headers=agent).json()
    assert [f["slug"] for f in listed] == ["myslop-pr-42"]


def test_create_folder_is_idempotent(client, agent):
    body = {"slug": "s", "title": "S"}
    assert client.post("/api/folders", json=body, headers=agent).status_code == 200
    assert client.post("/api/folders", json=body, headers=agent).status_code == 200
    assert len(client.get("/api/folders", headers=agent).json()) == 1


def test_create_folder_rejects_traversal_slug(client, agent):
    r = client.post("/api/folders", json={"slug": "../etc", "title": "x"}, headers=agent)
    assert r.status_code == 400


def test_post_and_read_back_source_not_html(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={
            "title": "handoff",
            "format": "md",
            "body": "# done",
            "author_note": "opus-5 on desktop",
        },
        headers=agent,
    )
    assert r.status_code == 200

    folder = client.get("/api/folders/s", headers=agent).json()
    post = folder["posts"][0]
    assert post["body"] == "# done"
    assert "<h1>" not in post["body"]
    assert post["author"] == "opus-desktop"
    assert post["author_note"] == "opus-5 on desktop"


def test_author_cannot_be_spoofed_from_the_body(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    client.post(
        "/api/folders/s/posts",
        json={"format": "md", "body": "x", "author": "someone-else", "author_kind": "human"},
        headers=agent,
    )
    post = client.get("/api/folders/s", headers=agent).json()["posts"][0]
    assert post["author"] == "opus-desktop"


def test_since_returns_only_newer_posts(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    client.post("/api/folders/s/posts", json={"format": "md", "body": "a"}, headers=agent)
    client.post("/api/folders/s/posts", json={"format": "md", "body": "b"}, headers=agent)

    first_id = client.get("/api/folders/s", headers=agent).json()["posts"][0]["id"]
    later = client.get(f"/api/folders/s?since={first_id}", headers=agent).json()["posts"]
    assert [p["body"] for p in later] == ["b"]


def test_post_can_set_status_and_owner_atomically(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    client.post(
        "/api/folders/s/posts",
        json={"format": "md", "body": "taking this", "status": "claimed", "owner": "opus-desktop"},
        headers=agent,
    )
    folder = client.get("/api/folders/s", headers=agent).json()
    assert folder["status"] == "claimed"
    assert folder["owner"] == "opus-desktop"


def test_post_with_invalid_status_writes_nothing(client, agent):
    """Claiming work and saying you claimed it must not half-succeed: an invalid
    status must reject the whole request before the post itself is written."""
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={"format": "md", "body": "hello", "status": "sideways"},
        headers=agent,
    )
    assert r.status_code == 400
    folder = client.get("/api/folders/s", headers=agent).json()
    assert folder["posts"] == []


def test_status_endpoint_returns_404_not_500_if_reaped_right_after(client, agent, monkeypatch):
    """A reap can land between the status UPDATE's commit and the route's read-back
    of the updated folder. That must surface as a 404, not a None-propagated 500."""
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    monkeypatch.setattr(store, "get_folder", lambda c, s: None)
    r = client.post("/api/folders/s/status", json={"status": "claimed"}, headers=agent)
    assert r.status_code == 404


def test_status_endpoint_rejects_unknown_status(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post("/api/folders/s/status", json={"status": "sideways"}, headers=agent)
    assert r.status_code == 400


def test_image_upload_round_trip(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={
            "format": "md",
            "body": "![a](img:arch.png)",
            "images": [{"filename": "arch.png", "content_b64": base64.b64encode(PNG).decode()}],
        },
        headers=agent,
    )
    assert r.status_code == 200
    assert r.json()["images"][0]["url"].startswith("/f/s/blob/")


def test_image_that_is_not_an_image_is_rejected(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={
            "format": "md",
            "body": "x",
            "images": [
                {"filename": "evil.png", "content_b64": base64.b64encode(b"<script>").decode()}
            ],
        },
        headers=agent,
    )
    assert r.status_code == 400


def test_malformed_base64_is_rejected(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={
            "format": "md",
            "body": "x",
            "images": [{"filename": "a.png", "content_b64": "!!!not base64!!!"}],
        },
        headers=agent,
    )
    assert r.status_code == 400


def test_missing_folder_returns_404(client, agent):
    assert client.get("/api/folders/nope", headers=agent).status_code == 404


def test_posting_to_missing_folder_returns_404(client, agent):
    r = client.post("/api/folders/nope/posts", json={"format": "md", "body": "x"}, headers=agent)
    assert r.status_code == 404


def test_status_update_on_missing_folder_returns_404(client, agent):
    r = client.post("/api/folders/nope/status", json={"status": "claimed"}, headers=agent)
    assert r.status_code == 404


def test_expired_folder_is_gone_from_the_api(client, agent, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)

    t[0] += 8 * DAY
    assert client.get("/api/folders/s", headers=agent).status_code == 404
    assert client.get("/api/folders", headers=agent).json() == []


def test_security_headers_are_present(client, agent):
    r = client.get("/api/folders", headers=agent)
    assert "script-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_body_too_large_response_still_has_security_headers(client, agent):
    headers = {**agent, "Content-Length": str(20 * 1024 * 1024)}
    r = client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=headers)
    assert r.status_code == 413
    assert "script-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_api_ignores_cookie_credentials(client, agent_token, db_path):
    from handoff import auth, db

    c = db.connect(db_path)
    uid = auth.create_user(c, "yoshi", "hunter2")
    sid = auth.create_session(c, uid)
    c.close()

    client.cookies.set(auth.COOKIE_NAME, sid)
    assert client.get("/api/folders").status_code == 401
