from handoff import clock

DAY = 86400


def _csrf(client, path="/"):
    return client.get(path).text.split('name="csrf" value="')[1].split('"')[0]


def _seed(client, agent, slug="s"):
    client.post("/api/folders", json={"slug": slug, "title": "S"}, headers=agent)


def test_human_post_appears_in_the_thread(human, agent):
    _seed(human, agent)
    csrf = _csrf(human, "/f/s")

    r = human.post("/f/s/post",
                   data={"csrf": csrf, "title": "answer", "body": "**do it**"},
                   follow_redirects=False)
    assert r.status_code == 303

    body = human.get("/f/s").text
    assert "<strong>do it</strong>" in body
    assert "yoshi" in body


def test_human_post_is_attributed_to_the_logged_in_user(human, agent):
    _seed(human, agent)
    human.post("/f/s/post", data={"csrf": _csrf(human, "/f/s"), "body": "x",
                                  "author": "somebody-else"},
               follow_redirects=False)

    post = human.get("/api/folders/s", headers=agent).json()["posts"][0]
    assert post["author"] == "yoshi"
    assert post["author_kind"] == "human"


def test_human_post_is_sanitized(human, agent):
    _seed(human, agent)
    human.post("/f/s/post",
               data={"csrf": _csrf(human, "/f/s"), "body": "<script>alert(1)</script>",
                     "format": "html"},
               follow_redirects=False)
    assert "<script" not in human.get("/f/s").text.lower()


def test_human_post_extends_expiry(human, agent, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    _seed(human, agent)

    t[0] += 6 * DAY
    human.post("/f/s/post", data={"csrf": _csrf(human, "/f/s"), "body": "still alive"},
               follow_redirects=False)

    folder = human.get("/api/folders/s", headers=agent).json()
    assert folder["expires_at"] == t[0] + 7 * DAY


def test_human_can_set_status(human, agent):
    _seed(human, agent)
    human.post("/f/s/status",
               data={"csrf": _csrf(human, "/f/s"), "status": "done", "owner": "yoshi"},
               follow_redirects=False)

    folder = human.get("/api/folders/s", headers=agent).json()
    assert folder["status"] == "done"
    assert folder["owner"] == "yoshi"


def test_post_without_csrf_is_rejected(human, agent):
    _seed(human, agent)
    r = human.post("/f/s/post", data={"body": "x"}, follow_redirects=False)
    assert r.status_code == 403
    assert human.get("/api/folders/s", headers=agent).json()["posts"] == []


def test_status_without_csrf_is_rejected(human, agent):
    _seed(human, agent)
    r = human.post("/f/s/status", data={"status": "done"}, follow_redirects=False)
    assert r.status_code == 403


def test_bad_status_is_rejected(human, agent):
    _seed(human, agent)
    r = human.post("/f/s/status", data={"csrf": _csrf(human, "/f/s"), "status": "sideways"},
                   follow_redirects=False)
    assert r.status_code == 400


def test_logged_out_user_cannot_post(client, agent):
    _seed(client, agent)
    r = client.post("/f/s/post", data={"csrf": "x", "body": "x"}, follow_redirects=False)
    assert r.status_code == 303


def test_there_is_no_folder_delete_route(human, agent):
    _seed(human, agent)
    assert human.delete("/f/s").status_code in (404, 405)
    assert human.post("/f/s/delete", data={"csrf": _csrf(human, "/f/s")}).status_code == 404
