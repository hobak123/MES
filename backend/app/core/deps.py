from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import Forbidden, Unauthorized
from app.core.security import decode_access_token
from app.models import Permission, RolePermission, User, UserRole


@dataclass
class CurrentUser:
    user: User
    organization_id: uuid.UUID
    facility_id: uuid.UUID | None
    roles: list[str]
    permissions: set[str] = field(default_factory=set)
    facility_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def is_super(self) -> bool:
        return "SUPER_ADMIN" in self.roles

    def has(self, perm: str) -> bool:
        return self.is_super or perm in self.permissions

    def require(self, perm: str) -> None:
        if not self.has(perm):
            raise Forbidden(f"권한이 없습니다. (필요 권한: {perm})")

    def can_access_facility(self, facility_id: uuid.UUID | None) -> bool:
        if facility_id is None or self.is_super:
            return True
        return facility_id in self.facility_ids

    def require_facility(self, facility_id: uuid.UUID | None) -> None:
        if not self.can_access_facility(facility_id):
            raise Forbidden("해당 공장에 대한 접근 권한이 없습니다.", "FACILITY_FORBIDDEN")


def load_permissions(db: Session, user_id: uuid.UUID) -> set[str]:
    rows = db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == user_id)
    ).scalars()
    return set(rows)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_facility_id: str | None = Header(default=None),
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized()
    token = authorization.split(" ", 1)[1].strip()
    try:
        data = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise Unauthorized("인증이 만료되었습니다. 다시 로그인하세요.", "TOKEN_EXPIRED")
    except jwt.InvalidTokenError:
        raise Unauthorized("유효하지 않은 인증 정보입니다.", "INVALID_TOKEN")
    user = db.get(User, uuid.UUID(data["sub"]))
    if not user or not user.is_active:
        raise Unauthorized("사용할 수 없는 계정입니다.", "ACCOUNT_DISABLED")
    if user.locked_until and user.locked_until > datetime.now(timezone.utc):
        raise Unauthorized("계정이 잠겨 있습니다.", "ACCOUNT_LOCKED")
    roles = user.role_codes
    facility_ids = [f.id for f in user.facilities]
    facility_id: uuid.UUID | None = None
    if x_facility_id:
        try:
            facility_id = uuid.UUID(x_facility_id)
        except ValueError:
            raise Forbidden("잘못된 공장 식별자입니다.", "FACILITY_FORBIDDEN")
    else:
        facility_id = user.default_facility_id or (facility_ids[0] if facility_ids else None)
    cu = CurrentUser(
        user=user,
        organization_id=user.organization_id,
        facility_id=facility_id,
        roles=roles,
        permissions=load_permissions(db, user.id),
        facility_ids=facility_ids,
    )
    cu.require_facility(facility_id)
    if facility_id is not None and cu.is_super:
        from app.models import Facility

        fac = db.get(Facility, facility_id)
        if not fac or fac.organization_id != cu.organization_id:
            raise Forbidden("해당 공장에 대한 접근 권한이 없습니다.", "FACILITY_FORBIDDEN")
    request.state.user = cu
    return cu


def require(perm: str):
    def _dep(cu: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        cu.require(perm)
        return cu

    return _dep
