import pytest

from handoff import clock, db

DAY = 86400
STAGES = ["spec", "build", "review", "approve", "needs-human", "merged"]


def _job(**over):
    job = {
        "id": "003-rate-limiting",
        "title": "Rate limiting",
        "repo": "KyDrive-server",
        "branch": "factory/003-rate-limiting",
        "stage": "review",
        "reviews": [
            {"security": "CHANGES", "ux": "PASS", "ui": "PASS", "code": "PASS"},
            {"security": "pending", "ux": "PASS", "ui": "pending", "code": "PASS"},
        ],
    }
    return job | over


def _sync(client, agent, jobs=None, files=None, name="busnes-app"):
    body = {
        "board": {"stages": STAGES, "jobs": [_job()] if jobs is None else jobs},
        "files": {"003-rate-limiting/spec.md": "# Spec\n\n- [ ] limits"}
        if files is None
        else files,
    }
    return client.put(f"/api/factory/{name}", json=body, headers=agent)


def test_sync_requires_a_token(client):
    assert (
        client.put("/api/factory/x", json={"board": {"stages": [], "jobs": []}}).status_code == 401
    )


def test_sync_is_idempotent_and_replaces_the_snapshot(human, agent):
    assert _sync(human, agent).status_code == 200
    assert _sync(human, agent).status_code == 200
    page = human.get("/factory/busnes-app/003-rate-limiting").text
    assert "<h1>Spec</h1>" in page

    _sync(human, agent, files={"003-rate-limiting/build.md": "# Build"})
    page = human.get("/factory/busnes-app/003-rate-limiting").text
    assert "<h1>Build</h1>" in page
    assert "<h1>Spec</h1>" not in page


def test_dashboard_shows_cards_in_their_lane_with_round_chips(human, agent):
    _sync(human, agent)
    page = human.get("/").text
    assert page.count('class="lane"') == len(STAGES)
    review_lane = page.split('data-stage="review"')[1].split('class="lane"')[0]
    assert 'href="/factory/busnes-app/003-rate-limiting"' in review_lane
    assert ">R1<" in page and ">R2<" in page
    assert 'class="chip chip-changes" title="security: CHANGES"' in page
    assert 'class="chip chip-pending" title="ui: pending"' in page
    assert 'http-equiv="refresh"' in page


def test_needs_human_job_stands_out(human, agent):
    _sync(human, agent, jobs=[_job(stage="needs-human")])
    page = human.get("/").text
    assert 'class="card needs-human-review"' in page
    assert "1 job needs a human" in page
    assert (
        "/factory approve 003-rate-limiting"
        in human.get("/factory/busnes-app/003-rate-limiting").text
    )


def test_job_files_are_ordered_spec_build_rounds_decision(human, agent):
    names = ["decision", "round-10/review-code", "round-2/review-code", "build", "spec"]
    _sync(human, agent, files={f"003-rate-limiting/{n}.md": n for n in names})
    page = human.get("/factory/busnes-app/003-rate-limiting").text
    order = ["spec.md", "build.md", "round-2/", "round-10/", "decision.md"]
    positions = [page.index(f">{n}") for n in order]
    assert positions == sorted(positions)


def test_job_markdown_is_sanitized(human, agent):
    _sync(human, agent, files={"003-rate-limiting/spec.md": "<script>alert(1)</script>\n\nok"})
    page = human.get("/factory/busnes-app/003-rate-limiting").text
    assert "<script" not in page.lower()
    assert "ok" in page


@pytest.mark.parametrize(
    "jobs, files",
    [
        ([_job(stage="shipped")], None),
        ([_job(id="../etc")], {}),
        ([_job(), _job()], None),
        ([_job(reviews=[{"security": "MAYBE"}])], None),
        (None, {"003-rate-limiting/../../x.md": "x"}),
        (None, {"other-job/spec.md": "x"}),
        (None, {"003-rate-limiting/spec.html": "x"}),
        (None, {"003-rate-limiting/spec.md": "x" * (256 * 1024 + 1)}),
    ],
)
def test_sync_rejects_bad_snapshots(client, agent, jobs, files):
    assert _sync(client, agent, jobs=jobs, files=files).status_code == 400


def test_sync_rejects_a_bad_factory_name(client, agent):
    assert _sync(client, agent, name="Bad_Name").status_code == 400


def test_factory_expires_after_its_last_sync(human, agent, db_path, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    _sync(human, agent)
    t[0] += 8 * DAY
    assert "busnes-app" not in human.get("/").text
    assert human.get("/factory/busnes-app/003-rate-limiting").status_code == 404

    conn = db.connect(db_path)
    assert conn.execute("SELECT count(*) FROM factory_files").fetchone()[0] == 0
    conn.close()


def test_missing_job_is_404(human, agent):
    _sync(human, agent)
    assert human.get("/factory/busnes-app/nope").status_code == 404
    assert human.get("/factory/nope/003-rate-limiting").status_code == 404


def test_factory_pages_require_login(client, agent):
    _sync(client, agent)
    assert (
        client.get("/factory/busnes-app/003-rate-limiting", follow_redirects=False).status_code
        == 303
    )


def test_tabs_mark_the_current_page(human):
    assert 'href="/" class="tab" aria-current="page"' in human.get("/").text
    assert 'href="/handoffs" class="tab" aria-current="page"' in human.get("/handoffs").text
    assert 'href="/agents" class="tab" aria-current="page"' in human.get("/agents").text
