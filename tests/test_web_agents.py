def _csrf(client, path="/agents"):
    return client.get(path).text.split('name="csrf" value="')[1].split('"')[0]


def test_minting_shows_the_token_once(human):
    r = human.post(
        "/agents", data={"csrf": _csrf(human), "name": "opus-laptop"}, follow_redirects=True
    )
    assert "opus-laptop" in r.text
    token = r.text.split('class="token">')[1].split("<")[0].strip()
    assert len(token) == 36

    assert token not in human.get("/agents").text


def test_minted_token_authenticates(human):
    r = human.post(
        "/agents", data={"csrf": _csrf(human), "name": "opus-laptop"}, follow_redirects=True
    )
    token = r.text.split('class="token">')[1].split("<")[0].strip()

    headers = {"Authorization": f"Bearer {token}"}
    assert human.get("/api/folders", headers=headers).status_code == 200


def test_revoked_token_stops_working(human):
    r = human.post(
        "/agents", data={"csrf": _csrf(human), "name": "opus-laptop"}, follow_redirects=True
    )
    token = r.text.split('class="token">')[1].split("<")[0].strip()
    agent_id = r.text.split('action="/agents/')[1].split("/revoke")[0]

    human.post(f"/agents/{agent_id}/revoke", data={"csrf": _csrf(human)}, follow_redirects=False)

    headers = {"Authorization": f"Bearer {token}"}
    assert human.get("/api/folders", headers=headers).status_code == 401


def test_duplicate_agent_name_is_reported_not_crashed(human):
    human.post("/agents", data={"csrf": _csrf(human), "name": "dup"}, follow_redirects=True)
    r = human.post("/agents", data={"csrf": _csrf(human), "name": "dup"}, follow_redirects=True)
    assert r.status_code == 200
    assert "already in use" in r.text


def test_mint_requires_csrf(human):
    r = human.post("/agents", data={"name": "x"}, follow_redirects=False)
    assert r.status_code == 403


def test_agents_page_requires_login(client):
    assert client.get("/agents", follow_redirects=False).status_code == 303
