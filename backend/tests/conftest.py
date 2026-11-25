from __future__ import annotations

import os
import uuid

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://foodmes:foodmes@127.0.0.1:55432/foodmes_test"
)
os.environ.setdefault("STORAGE_LOCAL_DIR", os.path.join(os.path.dirname(__file__), "..", "..", ".devdata", "test-storage"))
os.environ["OCR_PROVIDER"] = "mock"
os.environ["CELERY_ENABLED"] = "false"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

PASSWORD = "Passw0rd!"


@pytest.fixture(scope="session", autouse=True)
def _database():
    url = os.environ["DATABASE_URL"]
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    eng.dispose()
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = url
    command.upgrade(cfg, "head")
    from app.core.db import SessionLocal
    from app.seed import seed_base, seed_demo

    with SessionLocal() as db:
        org = seed_base(db)
        seed_demo(db, org)
        db.commit()
    yield


@pytest.fixture(scope="session")
def client(_database):
    from app.main import app

    with TestClient(app) as c:
        yield c


_token_cache: dict[str, dict] = {}


def auth(client: TestClient, username: str, facility: str | None = None) -> dict[str, str]:
    if username not in _token_cache:
        r = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        assert r.status_code == 200, r.text
        _token_cache[username] = r.json()["data"]
    h = {"Authorization": f"Bearer {_token_cache[username]['access_token']}"}
    if facility:
        h["X-Facility-Id"] = facility
    return h


def idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture(scope="session")
def admin(client):
    return auth(client, "admin")
