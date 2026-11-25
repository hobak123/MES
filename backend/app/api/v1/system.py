"""시스템: 승인 요청, 사용자/역할, 공장, 설정, 프린터, 알림, 감사로그."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit, snapshot
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import BusinessRule, Conflict, Forbidden, InvalidState, NotFound
from app.core.responses import ok, paged
from app.core.security import hash_password, validate_password_policy
from app.models import (
    ApprovalRequest, AuditLog, Facility, Notification, PrinterSetting, Role, StockAdjustment, SystemSetting, User, UserFacility,
    UserRole,
)
from app.repositories.common import PageParams, get_owned, paginate, ser

router = APIRouter(tags=["시스템"])
SEOUL = ZoneInfo("Asia/Seoul")

APPROVAL_PERM = {"WEIGHING_REVERSAL": "weighing:reverse_approve", "STOCK_ADJUSTMENT": "inventory:adjust_approve"}


# ---------------------------------------------------------------- approvals

@router.get("/approvals")
def list_approvals(p: PageParams = Depends(), status: str | None = "PENDING", request_type: str | None = None,
                   db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    stmt = select(ApprovalRequest).where(ApprovalRequest.organization_id == cu.organization_id)
    if cu.facility_id:
        stmt = stmt.where(ApprovalRequest.facility_id == cu.facility_id)
    if status:
        stmt = stmt.where(ApprovalRequest.status.in_(status.split(",")))
    if request_type:
        stmt = stmt.where(ApprovalRequest.request_type == request_type)
    if not (cu.has("approval:decide") or cu.has("inventory:adjust_approve") or cu.has("weighing:reverse_approve")):
        stmt = stmt.where(ApprovalRequest.requested_by == cu.id)
    rows, total = paginate(db, stmt, p, ApprovalRequest, [ApprovalRequest.reason], ApprovalRequest.created_at.desc())
    out = []
    for a in rows:
        u = db.get(User, a.requested_by)
        out.append(ser(a, requested_by_name=u.full_name if u else None, can_decide=cu.has(APPROVAL_PERM.get(a.request_type, "approval:decide"))))
    return paged(out, total, p.page, p.size)


class DecideIn(BaseModel):
    note: str | None = None


def _get_pending(db: Session, cu: CurrentUser, ar_id: uuid.UUID) -> ApprovalRequest:
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.id == ar_id, ApprovalRequest.organization_id == cu.organization_id)
                    .with_for_update()).scalar_one_or_none()
    if not ar:
        raise NotFound("승인 요청")
    cu.require_facility(ar.facility_id)
    if ar.status != "PENDING":
        raise InvalidState(ar.status, "승인 처리")
    cu.require(APPROVAL_PERM.get(ar.request_type, "approval:decide"))
    if ar.requested_by == cu.id and not cu.is_super:
        raise BusinessRule("SELF_APPROVAL", "본인이 요청한 건은 승인할 수 없습니다.")
    return ar


@router.post("/approvals/{ar_id}/approve")
def approve(ar_id: uuid.UUID, body: DecideIn | None = None, db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    ar = _get_pending(db, cu, ar_id)
    ar.decided_by = cu.id
    ar.decided_at = datetime.now(timezone.utc)
    ar.decision_note = body.note if body else None
    ar.status = "APPROVED"
    result: Any = None
    if ar.request_type == "WEIGHING_REVERSAL":
        from app.services.weighing import service as wsvc

        result = wsvc.reverse_record(db, cu, ar.target_id, ar.reason, bool((ar.payload or {}).get("contaminated")), via_approval=ar)
    elif ar.request_type == "STOCK_ADJUSTMENT":
        from app.api.v1.inventory import execute_adjustment

        adj = db.execute(select(StockAdjustment).where(StockAdjustment.id == ar.target_id).with_for_update()).scalar_one()
        ar.status = "PENDING"  # execute_adjustment 가 EXECUTED 로 갱신
        execute_adjustment(db, cu, adj)
        result = ser(adj)
    audit(db, cu, "APPROVAL_APPROVE", "APPROVAL_REQUEST", ar.id, after={"type": ar.request_type}, reason=ar.decision_note)
    db.commit()
    return ok({"approval": ser(ar), "result": result}, "승인되었습니다.")


@router.post("/approvals/{ar_id}/reject")
def reject(ar_id: uuid.UUID, body: DecideIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    ar = _get_pending(db, cu, ar_id)
    if not body.note:
        raise BusinessRule("REASON_REQUIRED", "반려 사유를 입력하세요.")
    ar.status = "REJECTED"
    ar.decided_by = cu.id
    ar.decided_at = datetime.now(timezone.utc)
    ar.decision_note = body.note
    if ar.request_type == "STOCK_ADJUSTMENT":
        adj = db.get(StockAdjustment, ar.target_id)
        if adj and adj.status == "PENDING":
            adj.status = "REJECTED"
            adj.approved_by = cu.id
            adj.approved_at = ar.decided_at
    audit(db, cu, "APPROVAL_REJECT", "APPROVAL_REQUEST", ar.id, reason=body.note)
    db.commit()
    return ok(ser(ar), "반려되었습니다.")


# ---------------------------------------------------------------- users / roles

class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    full_name: str = Field(min_length=1, max_length=100)
    email: str | None = None
    phone: str | None = None
    password: str = Field(min_length=8, max_length=200)
    roles: list[str] = Field(min_length=1)
    facility_ids: list[uuid.UUID] = Field(min_length=1)
    default_facility_id: uuid.UUID | None = None


class UserPatch(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    password: str | None = Field(default=None, min_length=8)
    roles: list[str] | None = None
    facility_ids: list[uuid.UUID] | None = None
    default_facility_id: uuid.UUID | None = None
    is_active: bool | None = None
    unlock: bool | None = None


def _user_out(u: User) -> dict:
    return ser(u, exclude=("password_hash",), roles=u.role_codes, facilities=[{"id": str(f.id), "code": f.code, "name": f.name} for f in u.facilities])


def _assign(db: Session, cu: CurrentUser, u: User, roles: list[str] | None, facility_ids: list[uuid.UUID] | None) -> None:
    if roles is not None:
        if "SUPER_ADMIN" in roles and not cu.is_super:
            raise Forbidden("최고관리자 역할은 최고관리자만 부여할 수 있습니다.")
        role_objs = db.execute(select(Role).where(Role.code.in_(roles))).scalars().all()
        if len(role_objs) != len(set(roles)):
            raise BusinessRule("INVALID_ROLE", "존재하지 않는 역할이 포함되어 있습니다.")
        for ur in db.execute(select(UserRole).where(UserRole.user_id == u.id)).scalars().all():
            db.delete(ur)
        for r in role_objs:
            db.add(UserRole(user_id=u.id, role_id=r.id))
    if facility_ids is not None:
        for fid in facility_ids:
            f = db.get(Facility, fid)
            if not f or f.organization_id != cu.organization_id:
                raise NotFound("공장")
        for uf in db.execute(select(UserFacility).where(UserFacility.user_id == u.id)).scalars().all():
            db.delete(uf)
        for fid in set(facility_ids):
            db.add(UserFacility(user_id=u.id, facility_id=fid))
    db.flush()
    db.expire(u, ["roles", "facilities"])


@router.get("/users")
def list_users(p: PageParams = Depends(), db: Session = Depends(get_db), cu: CurrentUser = Depends(require("user:manage"))):
    stmt = select(User).where(User.organization_id == cu.organization_id)
    rows, total = paginate(db, stmt, p, User, [User.username, User.full_name], User.username)
    return paged([_user_out(u) for u in rows], total, p.page, p.size)


@router.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("user:manage"))):
    validate_password_policy(body.password)
    if db.execute(select(User.id).where(User.organization_id == cu.organization_id, User.username == body.username)).first():
        raise Conflict("DUPLICATE_USERNAME", "이미 사용 중인 아이디입니다.")
    u = User(organization_id=cu.organization_id, username=body.username, full_name=body.full_name, email=body.email, phone=body.phone,
             password_hash=hash_password(body.password), default_facility_id=body.default_facility_id or body.facility_ids[0], created_by=cu.id)
    db.add(u)
    db.flush()
    _assign(db, cu, u, body.roles, body.facility_ids)
    audit(db, cu, "USER_CREATE", "USER", u.id, after={"username": u.username, "roles": body.roles})
    db.commit()
    db.refresh(u)
    return ok(_user_out(u), "사용자가 등록되었습니다.")


@router.patch("/users/{user_id}")
def patch_user(user_id: uuid.UUID, body: UserPatch, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("user:manage"))):
    u = get_owned(db, User, user_id, cu.organization_id, "사용자")
    if "SUPER_ADMIN" in u.role_codes and not cu.is_super:
        raise Forbidden("최고관리자 계정은 최고관리자만 수정할 수 있습니다.")
    before = {"roles": u.role_codes, "is_active": u.is_active, "facilities": [str(f.id) for f in u.facilities]}
    data = body.model_dump(exclude_unset=True)
    if data.get("password"):
        validate_password_policy(data["password"])
        u.password_hash = hash_password(data["password"])
    for k in ("full_name", "email", "phone", "default_facility_id", "is_active"):
        if k in data:
            setattr(u, k, data[k])
    if data.get("unlock"):
        u.locked_until = None
        u.failed_login_count = 0
    _assign(db, cu, u, data.get("roles"), data.get("facility_ids"))
    audit(db, cu, "USER_UPDATE", "USER", u.id, before=before, after={"roles": u.role_codes, "is_active": u.is_active,
                                                                       "password_changed": bool(data.get("password"))})
    db.commit()
    db.refresh(u)
    return ok(_user_out(u))


@router.get("/roles")
def list_roles(db: Session = Depends(get_db), cu: CurrentUser = Depends(require("user:manage"))):
    from app.models import Permission, RolePermission

    out = []
    for r in db.execute(select(Role).order_by(Role.code)).scalars():
        perms = db.execute(select(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
                           .where(RolePermission.role_id == r.id)).scalars().all()
        out.append(ser(r, permissions=sorted(perms)))
    return ok(out)


# ---------------------------------------------------------------- facilities / settings / printers

class FacilityIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None


@router.get("/facilities")
def list_facilities(db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    stmt = select(Facility).where(Facility.organization_id == cu.organization_id)
    if not cu.is_super:
        stmt = stmt.where(Facility.id.in_(cu.facility_ids))
    return ok([ser(f) for f in db.execute(stmt.order_by(Facility.code)).scalars()])


@router.post("/facilities")
def create_facility(body: FacilityIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("system:manage"))):
    if db.execute(select(Facility.id).where(Facility.organization_id == cu.organization_id, Facility.code == body.code)).first():
        raise Conflict("DUPLICATE_CODE", "이미 존재하는 공장 코드입니다.")
    f = Facility(organization_id=cu.organization_id, created_by=cu.id, **body.model_dump())
    db.add(f)
    db.flush()
    audit(db, cu, "FACILITY_CREATE", "FACILITY", f.id, after=snapshot(f))
    db.commit()
    return ok(ser(f))


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), cu: CurrentUser = Depends(require("system:manage"))):
    rows = db.execute(select(SystemSetting).where(SystemSetting.organization_id == cu.organization_id).order_by(SystemSetting.key)).scalars()
    return ok([ser(r) for r in rows])


class SettingIn(BaseModel):
    key: str = Field(max_length=100)
    value: Any


ALLOWED_SETTINGS = {"ocr.review_threshold", "alerts.expiry_days", "alerts.long_unused_days", "production.mass_balance_tolerance_pct", "company.name"}


@router.put("/settings")
def put_settings(items: list[SettingIn], db: Session = Depends(get_db), cu: CurrentUser = Depends(require("system:manage"))):
    for it in items:
        if it.key not in ALLOWED_SETTINGS:
            raise BusinessRule("INVALID_SETTING", f"변경할 수 없는 설정입니다: {it.key}")
        if it.key == "ocr.review_threshold":
            v = float(it.value)
            if not 0 < v <= 1:
                raise BusinessRule("INVALID_SETTING", "OCR 검토 기준은 0~1 사이여야 합니다.")
        s = db.execute(select(SystemSetting).where(SystemSetting.organization_id == cu.organization_id, SystemSetting.key == it.key)).scalar_one_or_none()
        before = dict(s.value) if s else None
        if s:
            s.value = {"value": it.value}
            s.updated_by = cu.id
        else:
            db.add(SystemSetting(organization_id=cu.organization_id, key=it.key, value={"value": it.value}, created_by=cu.id))
        audit(db, cu, "SETTING_UPDATE", "SYSTEM_SETTING", it.key, before=before, after={"value": it.value})
    db.commit()
    return get_settings(db, cu)


class PrinterIn(BaseModel):
    name: str | None = None
    printer_type: Literal["PDF", "BROWSER", "NETWORK_ZPL", "NETWORK_TSPL", "USB", "BLUETOOTH"] | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    label_width_mm: int | None = Field(default=None, ge=20, le=200)
    label_height_mm: int | None = Field(default=None, ge=15, le=200)
    dpi: int | None = None
    auto_print: bool | None = None
    is_default: bool | None = None
    is_active: bool | None = None


@router.get("/printers")
def list_printers(db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    rows = db.execute(select(PrinterSetting).where(PrinterSetting.organization_id == cu.organization_id)).scalars()
    return ok([ser(r) for r in rows if r.facility_id in (None, cu.facility_id) or cu.is_super])


@router.post("/printers")
def create_printer(body: PrinterIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("system:manage"))):
    if not body.name or not body.printer_type:
        raise BusinessRule("REQUIRED", "프린터명과 유형은 필수입니다.")
    if body.printer_type.startswith("NETWORK") and (not body.host or not body.port):
        raise BusinessRule("REQUIRED", "네트워크 프린터는 주소와 포트가 필요합니다.")
    if body.is_default:
        for p in db.execute(select(PrinterSetting).where(PrinterSetting.organization_id == cu.organization_id,
                                                         PrinterSetting.facility_id == cu.facility_id)).scalars():
            p.is_default = False
    p = PrinterSetting(organization_id=cu.organization_id, facility_id=cu.facility_id, created_by=cu.id, **body.model_dump(exclude_none=True))
    db.add(p)
    db.flush()
    audit(db, cu, "PRINTER_CREATE", "PRINTER", p.id, after=snapshot(p))
    db.commit()
    return ok(ser(p))


@router.patch("/printers/{pid}")
def patch_printer(pid: uuid.UUID, body: PrinterIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("system:manage"))):
    p = get_owned(db, PrinterSetting, pid, cu.organization_id, "프린터")
    before = snapshot(p)
    data = body.model_dump(exclude_unset=True)
    if data.get("is_default"):
        for o in db.execute(select(PrinterSetting).where(PrinterSetting.organization_id == cu.organization_id,
                                                         PrinterSetting.facility_id == p.facility_id)).scalars():
            o.is_default = False
    for k, v in data.items():
        setattr(p, k, v)
    audit(db, cu, "PRINTER_UPDATE", "PRINTER", p.id, before=before, after=snapshot(p))
    db.commit()
    return ok(ser(p))


# ---------------------------------------------------------------- notifications

@router.get("/notifications")
def list_notifications(p: PageParams = Depends(), unread_only: bool = False, category: str | None = None,
                       db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    stmt = select(Notification).where(Notification.organization_id == cu.organization_id)
    if cu.facility_id:
        stmt = stmt.where((Notification.facility_id == cu.facility_id) | Notification.facility_id.is_(None))
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    if category:
        stmt = stmt.where(Notification.category == category)
    rows, total = paginate(db, stmt, p, Notification, [Notification.title], Notification.created_at.desc())
    return paged([ser(n) for n in rows], total, p.page, p.size)


@router.post("/notifications/{nid}/read")
def read_notification(nid: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    n = get_owned(db, Notification, nid, cu.organization_id, "알림")
    n.is_read = True
    n.read_by = cu.id
    db.commit()
    return ok(ser(n))


@router.post("/notifications/scan")
def scan_notifications(db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    from app.services.notifications import scan

    created = scan(db, cu.organization_id)
    db.commit()
    return ok({"created": created}, f"알림 {created}건이 생성되었습니다.")


# ---------------------------------------------------------------- audit logs (read only)

@router.get("/audit-logs")
def audit_logs(p: PageParams = Depends(), action: str | None = None, target_type: str | None = None, target_id: str | None = None,
               user_id: uuid.UUID | None = None, date_from: date | None = None, date_to: date | None = None,
               db: Session = Depends(get_db), cu: CurrentUser = Depends(require("audit:read"))):
    stmt = select(AuditLog).where(AuditLog.organization_id == cu.organization_id)
    for col, val in [(AuditLog.action, action), (AuditLog.target_type, target_type), (AuditLog.target_id, target_id), (AuditLog.user_id, user_id)]:
        if val:
            stmt = stmt.where(col == val)
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(date_from, time.min, SEOUL))
    if date_to:
        stmt = stmt.where(AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min, SEOUL))
    rows, total = paginate(db, stmt, p, AuditLog, [AuditLog.username, AuditLog.action, AuditLog.target_id], AuditLog.created_at.desc())
    return paged([ser(a) for a in rows], total, p.page, p.size)
