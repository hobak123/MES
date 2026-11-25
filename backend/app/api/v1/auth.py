from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.errors import Unauthorized
from app.core.responses import ok
from app.core.security import create_access_token, hash_token, new_refresh_token, verify_password
from app.models import Organization, User, UserSession

router = APIRouter(prefix="/auth", tags=["인증"])


class LoginIn(BaseModel):
    organization_code: str = Field(default="DEFAULT", max_length=30)
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class RefreshIn(BaseModel):
    refresh_token: str


def _issue(db: Session, user: User, request: Request) -> dict:
    raw, h = new_refresh_token()
    now = datetime.now(timezone.utc)
    sess = UserSession(
        user_id=user.id, token_hash=h, issued_at=now, expires_at=now + timedelta(days=settings.REFRESH_TOKEN_DAYS),
        ip_address=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:300],
    )
    db.add(sess)
    db.flush()
    return {
        "access_token": create_access_token(user.id, user.organization_id),
        "refresh_token": raw,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_MINUTES * 60,
        "session_id": str(sess.id),
    }


def user_info(user: User, perms: set[str] | None = None) -> dict:
    return {
        "id": str(user.id),
        "organization_id": str(user.organization_id),
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "roles": user.role_codes,
        "facilities": [{"id": str(f.id), "code": f.code, "name": f.name} for f in user.facilities],
        "default_facility_id": str(user.default_facility_id) if user.default_facility_id else None,
        "permissions": sorted(perms) if perms is not None else None,
    }


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    org = db.execute(select(Organization).where(Organization.code == body.organization_code, Organization.is_active)).scalar_one_or_none()
    user = None
    if org:
        user = db.execute(select(User).where(User.organization_id == org.id, User.username == body.username).with_for_update()).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not user:
        audit(db, None, "LOGIN_FAILED", "USER", None, after={"username": body.username}, organization_id=org.id if org else None,
              username=body.username)
        db.commit()
        raise Unauthorized("아이디 또는 비밀번호가 올바르지 않습니다.", "INVALID_CREDENTIALS")
    if user.locked_until and user.locked_until > now:
        raise Unauthorized(f"로그인 실패가 반복되어 계정이 잠겼습니다. {settings.LOGIN_LOCK_MINUTES}분 후 다시 시도하세요.", "ACCOUNT_LOCKED")
    if not user.is_active or not verify_password(body.password, user.password_hash):
        if user.is_active:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= settings.LOGIN_MAX_FAILURES:
                user.locked_until = now + timedelta(minutes=settings.LOGIN_LOCK_MINUTES)
                user.failed_login_count = 0
        audit(db, None, "LOGIN_FAILED", "USER", user.id, organization_id=user.organization_id, user_id=user.id, username=user.username)
        db.commit()
        raise Unauthorized("아이디 또는 비밀번호가 올바르지 않습니다.", "INVALID_CREDENTIALS")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    tokens = _issue(db, user, request)
    audit(db, None, "LOGIN", "USER", user.id, organization_id=user.organization_id, user_id=user.id, username=user.username)
    from app.core.deps import load_permissions

    data = {**tokens, "user": user_info(user, load_permissions(db, user.id))}
    db.commit()
    return ok(data, "로그인되었습니다.")


@router.post("/refresh")
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_db)):
    h = hash_token(body.refresh_token)
    sess = db.execute(select(UserSession).where(UserSession.token_hash == h).with_for_update()).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not sess:
        raise Unauthorized("유효하지 않은 갱신 토큰입니다.", "INVALID_REFRESH_TOKEN")
    if sess.revoked_at is not None:
        # 재사용 탐지: 회전된 토큰이 다시 사용되면 해당 사용자의 모든 세션 폐기
        for s in db.execute(select(UserSession).where(UserSession.user_id == sess.user_id, UserSession.revoked_at.is_(None))).scalars():
            s.revoked_at = now
        db.commit()
        raise Unauthorized("갱신 토큰이 이미 사용되었습니다. 다시 로그인하세요.", "REFRESH_TOKEN_REUSED")
    if sess.expires_at < now:
        raise Unauthorized("갱신 토큰이 만료되었습니다.", "REFRESH_TOKEN_EXPIRED")
    user = db.get(User, sess.user_id)
    if not user or not user.is_active:
        raise Unauthorized("사용할 수 없는 계정입니다.", "ACCOUNT_DISABLED")
    tokens = _issue(db, user, request)
    sess.revoked_at = now
    sess.replaced_by = uuid.UUID(tokens["session_id"])
    db.commit()
    return ok(tokens, "토큰이 갱신되었습니다.")


@router.post("/logout")
def logout(body: RefreshIn | None = None, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    if body and body.refresh_token:
        sess = db.execute(select(UserSession).where(UserSession.token_hash == hash_token(body.refresh_token))).scalar_one_or_none()
        if sess and sess.user_id == cu.id:
            sess.revoked_at = now
    else:
        for s in db.execute(select(UserSession).where(UserSession.user_id == cu.id, UserSession.revoked_at.is_(None))).scalars():
            s.revoked_at = now
    audit(db, cu, "LOGOUT", "USER", cu.id)
    db.commit()
    return ok(None, "로그아웃되었습니다.")


@router.get("/me")
def me(cu: CurrentUser = Depends(get_current_user)):
    data = user_info(cu.user, cu.permissions if not cu.is_super else {"*", *cu.permissions})
    data["active_facility_id"] = str(cu.facility_id) if cu.facility_id else None
    return ok(data)
