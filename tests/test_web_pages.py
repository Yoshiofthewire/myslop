import base64

from handoff import clock
from handoff.web import _expires_in

DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _seed(client, agent, slug="myslop-pr-42", body="# hello", images=None):
    client.post("/api/folders", json={"slug": slug, "title": "PR 42"}, headers=agent)
    payload = {"format": "md", "body": body, "title": "handoff", "author_note": "opus-5 on desktop"}
    if images:
        payload["images"] = images
    return client.post(f"/api/folders/{slug}/posts", json=payload, headers=agent).json()


def test_index_lists_live_folders_with_status_and_expiry(human, agent):
    _seed(human, agent)
    body = human.get("/").text
    assert "myslop-pr-42" in body
    assert "open" in body
    assert "expires in" in body


def test_index_hides_expired_folders(human, agent, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    _seed(human, agent)

    t[0] += 8 * DAY
    assert "myslop-pr-42" not in human.get("/").text


def test_thread_renders_markdown_as_html(human, agent):
    _seed(human, agent, body="# hello")
    body = human.get("/f/myslop-pr-42").text
    assert "<h1>hello</h1>" in body


def test_thread_shows_author_and_self_reported_note(human, agent):
    _seed(human, agent)
    body = human.get("/f/myslop-pr-42").text
    assert "opus-desktop" in body
    assert "opus-5 on desktop" in body


def test_thread_does_not_execute_injected_script(human, agent):
    human.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    human.post(
        "/api/folders/s/posts",
        json={"format": "html", "body": "<script>alert(1)</script><p>ok</p>"},
        headers=agent,
    )

    body = human.get("/f/s").text
    assert "<script" not in body.lower()
    assert "<p>ok</p>" in body


def test_index_and_thread_pages_are_not_cached(human, agent):
    _seed(human, agent)
    assert human.get("/").headers["cache-control"] == "no-store"
    assert human.get("/f/myslop-pr-42").headers["cache-control"] == "no-store"


def test_login_page_is_not_marked_no_store(client):
    assert "cache-control" not in client.get("/login").headers


def test_missing_folder_returns_404(human):
    assert human.get("/f/nope").status_code == 404


def test_blob_is_served_with_sniffed_type_and_hardening(human, agent):
    result = _seed(
        human,
        agent,
        slug="s",
        body="![a](img:a.png)",
        images=[{"filename": "a.png", "content_b64": base64.b64encode(PNG).decode()}],
    )
    url = result["images"][0]["url"]

    r = human.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_blob_from_another_folder_does_not_resolve(human, agent):
    result = _seed(
        human,
        agent,
        slug="a",
        body="x",
        images=[{"filename": "a.png", "content_b64": base64.b64encode(PNG).decode()}],
    )
    blob_id = result["images"][0]["url"].rsplit("/", 1)[-1]
    human.post("/api/folders", json={"slug": "b", "title": "B"}, headers=agent)

    assert human.get(f"/f/b/blob/{blob_id}").status_code == 404


def test_pages_require_login(client, agent):
    _seed(client, agent)
    assert client.get("/f/myslop-pr-42", follow_redirects=False).status_code == 303


def test_blob_is_inaccessible_after_folder_expiry_without_reaping(human, agent, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    result = _seed(
        human,
        agent,
        slug="s",
        body="x",
        images=[{"filename": "a.png", "content_b64": base64.b64encode(PNG).decode()}],
    )
    url = result["images"][0]["url"]
    assert human.get(url).status_code == 200

    t[0] += 8 * DAY
    assert human.get("/f/s").status_code == 404
    assert human.get(url).status_code == 404


def test_expires_in_renders_sub_hour_remnants_as_minutes(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 1000)
    assert _expires_in(1000 + 50 * 60) == "50 minutes"


def test_expires_in_renders_exact_zero_remaining(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 1000)
    assert _expires_in(1000) == "under a minute"
