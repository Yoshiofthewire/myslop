from starlette.requests import Request

from handoff import auth, db, web


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "password" in r.text


def test_index_redirects_when_logged_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def _make_user(db_path):
    c = db.connect(db_path)
    db.init_schema(c)
    auth.create_user(c, "yoshi", "hunter2")
    c.close()
    auth.reset_throttle()


def test_login_sets_a_hardened_cookie(client, db_path):
    _make_user(db_path)

    r = client.post(
        "/login", data={"username": "yoshi", "password": "hunter2"}, follow_redirects=False
    )
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_cookie_is_secure_under_tls(db_path):
    from fastapi.testclient import TestClient

    from handoff import app as app_module

    _make_user(db_path)
    with TestClient(app_module.create_app(db_path), base_url="https://testserver") as tls:
        r = tls.post(
            "/login", data={"username": "yoshi", "password": "hunter2"}, follow_redirects=False
        )
    assert "secure" in r.headers["set-cookie"].lower()


def test_cookie_is_not_secure_over_plain_http(client, db_path):
    # Verified against httpx/TestClient: a Secure cookie set over http is never sent back,
    # exactly as a browser behaves. Marking it Secure on the documented plain-http tailnet
    # deployment would not harden the session -- it would make login impossible.
    _make_user(db_path)

    r = client.post(
        "/login", data={"username": "yoshi", "password": "hunter2"}, follow_redirects=False
    )
    assert "secure" not in r.headers["set-cookie"].lower()


def test_bad_password_does_not_log_in(client, db_path):
    c = db.connect(db_path)
    auth.create_user(c, "yoshi", "hunter2")
    c.close()
    auth.reset_throttle()

    r = client.post(
        "/login", data={"username": "yoshi", "password": "nope"}, follow_redirects=False
    )
    assert r.status_code == 200
    assert auth.COOKIE_NAME not in r.cookies


def test_logged_in_user_reaches_the_index(human):
    r = human.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_logout_clears_the_session(human):
    token = human.get("/").text
    csrf = token.split('name="csrf" value="')[1].split('"')[0]

    human.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert human.get("/", follow_redirects=False).status_code == 303


def test_logout_without_csrf_is_rejected(human):
    r = human.post("/logout", data={}, follow_redirects=False)
    assert r.status_code == 403
    assert human.get("/", follow_redirects=False).status_code == 200


def test_no_page_contains_a_script_tag(human):
    for path in ["/login", "/"]:
        assert "<script" not in human.get(path).text.lower()


def _request_with_cookie(value: str) -> Request:
    scope = {"type": "http", "headers": [(b"cookie", f"{auth.COOKIE_NAME}={value}".encode())]}
    return Request(scope)


def test_stale_cookie_does_not_mint_a_csrf_token_without_a_validated_user():
    request = _request_with_cookie("attacker-supplied-garbage")
    response = web.page(request, "login.html", error=None)
    assert response.context["csrf"] == ""


def test_validated_user_still_gets_a_working_csrf_token(conn):
    uid = auth.create_user(conn, "yoshi", "hunter2")
    sid = auth.create_session(conn, uid)
    user = auth.session_user(conn, sid)

    request = _request_with_cookie(sid)
    response = web.page(request, "index.html", user=user, folders=[])

    assert response.context["csrf"] != ""
    assert auth.check_csrf(sid, response.context["csrf"])
