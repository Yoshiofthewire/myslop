import pytest
from fastapi.testclient import TestClient

from handoff import app as app_module
from handoff import auth, db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "handoff.db")


@pytest.fixture
def client(db_path):
    application = app_module.create_app(db_path, ttl_days=7)
    with TestClient(application) as c:
        yield c


@pytest.fixture
def agent_token(db_path):
    c = db.connect(db_path)
    db.init_schema(c)
    _, token = auth.mint_agent(c, "opus-desktop")
    c.close()
    return token


@pytest.fixture
def agent(agent_token):
    return {"Authorization": f"Bearer {agent_token}"}
