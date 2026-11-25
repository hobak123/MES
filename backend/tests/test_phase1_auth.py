"""Phase 1: 인증/권한/공장 격리/번호 발급/원장 불변성."""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import PASSWORD, auth


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_login_and_me(client):
    r = client.post("/api/v1/auth/login", json={"username": "qm", "password": PASSWORD})
    body = r.json()
    assert r.status_code == 200 and body["success"] is True
    assert body["request_id"] and r.headers["X-Request-ID"] == body["request_id"]
    assert "QUALITY_MANAGER" in body["data"]["user"]["roles"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['data']['access_token']}"})
    assert me.status_code == 200 and me.json()["data"]["username"] == "qm"
    assert "fg:release" in me.json()["data"]["permissions"]


def test_login_wrong_password_korean_error(client):
    r = client.post("/api/v1/auth/login", json={"username": "viewer", "password": "nope"})
    assert r.status_code == 401
    b = r.json()
    assert b["success"] is False and b["error_code"] == "INVALID_CREDENTIALS" and "비밀번호" in b["message"]


def test_login_lockout(client):
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import Organization, User, UserFacility, UserRole, Role
    from sqlalchemy import select

    with SessionLocal() as db:
        org = db.execute(select(Organization)).scalars().first()
        u = User(organization_id=org.id, username="locktest", full_name="잠금", password_hash=hash_password(PASSWORD))
        db.add(u)
        db.flush()
        db.add(UserRole(user_id=u.id, role_id=db.execute(select(Role.id).where(Role.code == "VIEWER")).scalar_one()))
        db.commit()
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "locktest", "password": "bad"})
    r = client.post("/api/v1/auth/login", json={"username": "locktest", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["error_code"] == "ACCOUNT_LOCKED"


def test_refresh_rotation_and_reuse_detection(client):
    r = client.post("/api/v1/auth/login", json={"username": "viewer", "password": PASSWORD}).json()["data"]
    r1 = client.post("/api/v1/auth/refresh", json={"refresh_token": r["refresh_token"]})
    assert r1.status_code == 200
    new = r1.json()["data"]
    # 이전 토큰 재사용 → 거부 + 모든 세션 폐기
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": r["refresh_token"]})
    assert r2.status_code == 401 and r2.json()["error_code"] == "REFRESH_TOKEN_REUSED"
    r3 = client.post("/api/v1/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r3.status_code == 401


def test_logout_revokes(client):
    d = client.post("/api/v1/auth/login", json={"username": "viewer", "password": PASSWORD}).json()["data"]
    h = {"Authorization": f"Bearer {d['access_token']}"}
    assert client.post("/api/v1/auth/logout", json={"refresh_token": d["refresh_token"]}, headers=h).status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": d["refresh_token"]}).status_code == 401


def test_unauthenticated(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401 and r.json()["error_code"] == "UNAUTHORIZED"
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401 and r.json()["error_code"] == "INVALID_TOKEN"


def test_facility_isolation_header(client):
    """018: 작업자의 타 공장 접근 거부."""
    from app.core.db import SessionLocal
    from app.models import Facility
    from sqlalchemy import select

    with SessionLocal() as db:
        f2 = db.execute(select(Facility).where(Facility.code == "F2")).scalar_one()
    r = client.get("/api/v1/auth/me", headers=auth(client, "worker", facility=str(f2.id)))
    assert r.status_code == 403 and r.json()["error_code"] == "FACILITY_FORBIDDEN"


def test_atomic_numbering_concurrent():
    from app.core.db import SessionLocal
    from app.core.numbering import next_number
    from app.models import Organization
    from sqlalchemy import select

    with SessionLocal() as db:
        org_id = db.execute(select(Organization.id)).scalars().first()
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        with SessionLocal() as db:
            for _ in range(10):
                n = next_number(db, org_id, "TST")
                db.commit()
                with lock:
                    results.append(n)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 50 and len(set(results)) == 50


def test_audit_and_ledger_immutable():
    from app.core.db import SessionLocal
    from sqlalchemy.exc import DBAPIError

    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='LOGIN'")).scalar() > 0
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE audit_logs SET action='X'"))
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(text("DELETE FROM audit_logs"))
        db.rollback()
        # 감사로그에 비밀번호/토큰 미포함
        rows = db.execute(text("SELECT coalesce(after_data::text,'') FROM audit_logs")).scalars().all()
        assert not any(PASSWORD in r for r in rows)
