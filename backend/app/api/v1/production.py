"""BOM / 공정 마스터·라우팅 / 제조지시 API."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit, snapshot
from app.core.db import get_db
from app.core.deps import CurrentUser, require
from app.core.errors import BusinessRule, Conflict, InvalidState, NotFound
from app.core.idempotency import Idem, idempotency
from app.core.responses import ok, paged
from app.models import BomHeader, ProcessDefinition, Product, ProductionOrder, ProductionRouting, RoutingStep, RoutingVersion
from app.repositories.common import PageParams, get_owned, paginate, ser
from app.services.production import bom as bom_svc
from app.services.production import orders as order_svc

router = APIRouter(tags=["BOM·생산"])


# ================================================================ BOM

class BomItemIn(BaseModel):
    item_type: Literal["FORMULA", "PACKAGING", "PROCESS_AUXILIARY"]
    material_id: uuid.UUID
    quantity_basis: Literal["RATIO", "FIXED", "PER_UNIT"] = "RATIO"
    ratio_percent: Decimal | None = Field(default=None, gt=0, le=100)
    standard_quantity: Decimal | None = Field(default=None, gt=0)
    unit_id: uuid.UUID
    tolerance_pct: Decimal = Field(default=Decimal("1"), ge=0, le=100)
    sequence: int | None = None
    process_step_code: str | None = None
    weighing_group: str | None = None
    is_required: bool = True
    substitute_allowed: bool = False
    substitute_material_ids: list[uuid.UUID] | None = None
    note: str | None = None


class BomVersionIn(BaseModel):
    base_quantity: Decimal = Field(gt=0)
    base_unit_id: uuid.UUID
    base_fg_quantity: Decimal | None = Field(default=None, gt=0)
    effective_from: date | None = None
    change_note: str | None = None
    items: list[BomItemIn]


class BomCreateIn(BaseModel):
    product_id: uuid.UUID
    name: str | None = None
    version: BomVersionIn


class BomVersionCreateIn(BaseModel):
    copy_from_version_id: uuid.UUID | None = None
    version: BomVersionIn | None = None


class BomVersionPatch(BaseModel):
    base_quantity: Decimal | None = Field(default=None, gt=0)
    base_fg_quantity: Decimal | None = None
    effective_from: date | None = None
    change_note: str | None = None
    items: list[BomItemIn] | None = None


class BomApproveIn(BaseModel):
    version_id: uuid.UUID


class BomCalcIn(BaseModel):
    planned_quantity: Decimal | None = Field(default=None, gt=0)
    target_fg_quantity: Decimal | None = Field(default=None, gt=0)
    version_id: uuid.UUID | None = None


@router.get("/boms")
def list_boms(p: PageParams = Depends(), product_id: uuid.UUID | None = None, db: Session = Depends(get_db),
              cu: CurrentUser = Depends(require("bom:read"))):
    stmt = select(BomHeader).where(BomHeader.organization_id == cu.organization_id)
    if product_id:
        stmt = stmt.where(BomHeader.product_id == product_id)
    rows, total = paginate(db, stmt, p, BomHeader, [BomHeader.bom_code, BomHeader.name], BomHeader.bom_code)
    out = []
    for h in rows:
        pr = db.get(Product, h.product_id)
        approved = next((v for v in sorted(h.versions, key=lambda x: -x.version_no) if v.status == "APPROVED"), None)
        out.append(ser(h, product_code=pr.product_code, product_name=pr.product_name, version_count=len(h.versions),
                       approved_version_no=approved.version_no if approved else None,
                       latest_status=h.versions[-1].status if h.versions else None))
    return paged(out, total, p.page, p.size)


@router.post("/boms")
def create_bom(body: BomCreateIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("bom:write"))):
    data = body.model_dump()
    h = bom_svc.create(db, cu, data)
    db.commit()
    return ok(bom_svc.detail(db, h), "BOM 이 등록되었습니다. (승인 전 사용 불가)")


@router.get("/boms/{bom_id}")
def get_bom(bom_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("bom:read"))):
    return ok(bom_svc.detail(db, bom_svc.get_header(db, cu.organization_id, bom_id)))


@router.post("/boms/{bom_id}/versions")
def add_version(bom_id: uuid.UUID, body: BomVersionCreateIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("bom:write"))):
    h = bom_svc.get_header(db, cu.organization_id, bom_id)
    v = bom_svc.add_version(db, cu, h, body.version.model_dump() if body.version else None, body.copy_from_version_id)
    db.commit()
    return ok(bom_svc.detail(db, h), f"버전 {v.version_no} 이(가) 생성되었습니다.")


@router.patch("/boms/versions/{version_id}")
def patch_version(version_id: uuid.UUID, body: BomVersionPatch, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("bom:write"))):
    v = bom_svc.get_version(db, cu.organization_id, version_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("items") is not None:
        data["items"] = [BomItemIn(**i).model_dump() if isinstance(i, dict) else i for i in data["items"]]
    bom_svc.update_version(db, cu, v, data)
    db.commit()
    return ok(bom_svc.detail(db, bom_svc.get_header(db, cu.organization_id, v.bom_header_id)))


@router.post("/boms/{bom_id}/approve")
def approve_bom(bom_id: uuid.UUID, body: BomApproveIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("bom:approve"))):
    h = bom_svc.get_header(db, cu.organization_id, bom_id)
    v = bom_svc.approve(db, cu, h, body.version_id)
    db.commit()
    return ok(bom_svc.detail(db, h), f"BOM 버전 {v.version_no} 이(가) 승인되었습니다.")


@router.post("/boms/{bom_id}/calculate")
def calculate(bom_id: uuid.UUID, body: BomCalcIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("bom:read"))):
    h = bom_svc.get_header(db, cu.organization_id, bom_id)
    if body.version_id:
        v = bom_svc.get_version(db, cu.organization_id, body.version_id)
    else:
        v = next((x for x in sorted(h.versions, key=lambda x: -x.version_no) if x.status == "APPROVED"), None) or h.versions[-1]
    p = db.get(Product, h.product_id)
    planned, target = bom_svc.derive_quantities(p, v, body.planned_quantity, body.target_fg_quantity)
    items = [ser(i) for i in v.items]
    rows = bom_svc.calculate(db, cu.organization_id, cu.facility_id, items, v.base_quantity, v.base_unit_id, planned, target)
    for r in rows:
        r.pop("_req", None)
    from app.repositories.common import ser_value

    return ok({"version_no": v.version_no, "status": v.status, "planned_quantity": ser_value(planned), "target_fg_quantity": ser_value(target),
               "ratio_total": ser_value(bom_svc.ratio_total(v.items)), "items": rows})


# ================================================================ 공정 마스터 / 라우팅

class ProcessIn(BaseModel):
    code: str | None = Field(default=None, max_length=30)
    name: str | None = None
    process_type: str | None = None
    default_sequence: int | None = None
    work_center: str | None = None
    equipment_id: uuid.UUID | None = None
    standard_minutes: Decimal | None = None
    standard_temperature: Decimal | None = None
    standard_pressure: Decimal | None = None
    standard_speed: Decimal | None = None
    parameter_specs: list[dict[str, Any]] | None = None
    inspection_criteria: str | None = None
    required_records: list[str] | None = None
    approval_required: bool | None = None
    qr_required: bool | None = None
    is_ccp: bool | None = None
    output_type: Literal["WIP", "FG", "NONE"] | None = None
    is_active: bool | None = None


@router.get("/processes")
def list_processes(db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:read"))):
    rows = db.execute(select(ProcessDefinition).where(ProcessDefinition.organization_id == cu.organization_id)
                      .order_by(ProcessDefinition.default_sequence)).scalars()
    return ok([ser(r) for r in rows])


@router.post("/processes")
def create_process(body: ProcessIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:write"))):
    if not body.code or not body.name or not body.process_type:
        raise BusinessRule("REQUIRED", "공정 코드, 공정명, 공정 유형은 필수입니다.")
    if db.execute(select(ProcessDefinition.id).where(ProcessDefinition.organization_id == cu.organization_id, ProcessDefinition.code == body.code)).first():
        raise Conflict("DUPLICATE_CODE", "이미 존재하는 공정 코드입니다.")
    obj = ProcessDefinition(organization_id=cu.organization_id, created_by=cu.id, **body.model_dump(exclude_none=True))
    db.add(obj)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "PROCESS_DEFINITION", obj.id, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


@router.patch("/processes/{pid}")
def patch_process(pid: uuid.UUID, body: ProcessIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:write"))):
    obj = get_owned(db, ProcessDefinition, pid, cu.organization_id, "공정")
    before = snapshot(obj)
    data = body.model_dump(exclude_unset=True)
    data.pop("code", None)
    for k, v in data.items():
        setattr(obj, k, v)
    db.flush()
    audit(db, cu, "MASTER_UPDATE", "PROCESS_DEFINITION", obj.id, before=before, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


class RoutingStepIn(BaseModel):
    process_definition_id: uuid.UUID
    sequence: int = Field(ge=1)
    step_name: str | None = None
    is_required: bool = True
    equipment_id: uuid.UUID | None = None
    parameter_specs: list[dict[str, Any]] | None = None
    output_type: Literal["WIP", "FG", "NONE"] | None = None


class RoutingIn(BaseModel):
    product_id: uuid.UUID
    name: str | None = None
    steps: list[RoutingStepIn] = Field(min_length=1)


def _routing_out(db: Session, r: ProductionRouting) -> dict:
    vers = []
    for v in r.versions:
        steps = []
        for s in v.steps:
            pd = db.get(ProcessDefinition, s.process_definition_id)
            steps.append(ser(s, process_code=pd.code, process_name=pd.name, is_ccp=pd.is_ccp))
        vers.append(ser(v, steps=steps))
    return ser(r, versions=vers)


@router.get("/routings")
def list_routings(product_id: uuid.UUID | None = None, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:read"))):
    stmt = select(ProductionRouting).where(ProductionRouting.organization_id == cu.organization_id)
    if product_id:
        stmt = stmt.where(ProductionRouting.product_id == product_id)
    return ok([_routing_out(db, r) for r in db.execute(stmt).scalars()])


@router.post("/routings")
def create_routing(body: RoutingIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:write"))):
    p = get_owned(db, Product, body.product_id, cu.organization_id, "제품")
    seqs = [s.sequence for s in body.steps]
    if len(seqs) != len(set(seqs)):
        raise BusinessRule("DUPLICATE_SEQUENCE", "공정 순서가 중복되었습니다.")
    r = db.execute(select(ProductionRouting).where(ProductionRouting.organization_id == cu.organization_id,
                                                   ProductionRouting.product_id == p.id)).scalars().first()
    if not r:
        r = ProductionRouting(organization_id=cu.organization_id, routing_code=f"RT-{p.product_code}", product_id=p.id,
                              name=body.name or f"{p.product_name} 공정", created_by=cu.id)
        db.add(r)
        db.flush()
    last = max((v.version_no for v in r.versions), default=0)
    v = RoutingVersion(organization_id=cu.organization_id, routing_id=r.id, version_no=last + 1, status="DRAFT", created_by=cu.id)
    db.add(v)
    db.flush()
    ordered = sorted(body.steps, key=lambda s: s.sequence)
    for i, s in enumerate(ordered):
        pd = get_owned(db, ProcessDefinition, s.process_definition_id, cu.organization_id, "공정")
        out_type = s.output_type or pd.output_type
        db.add(RoutingStep(organization_id=cu.organization_id, routing_version_id=v.id, process_definition_id=pd.id, sequence=s.sequence,
                           step_name=s.step_name or pd.name, is_required=s.is_required, equipment_id=s.equipment_id,
                           parameter_specs=s.parameter_specs, output_type=out_type))
    db.flush()
    db.refresh(r)
    audit(db, cu, "ROUTING_CREATE", "ROUTING_VERSION", v.id)
    db.commit()
    return ok(_routing_out(db, r))


@router.post("/routings/{routing_id}/approve")
def approve_routing(routing_id: uuid.UUID, body: BomApproveIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:write"))):
    r = get_owned(db, ProductionRouting, routing_id, cu.organization_id, "라우팅")
    v = get_owned(db, RoutingVersion, body.version_id, cu.organization_id, "라우팅 버전")
    if v.routing_id != r.id or v.status != "DRAFT":
        raise InvalidState(v.status, "라우팅 승인")
    if not any(s.output_type == "FG" for s in v.steps):
        raise BusinessRule("ROUTING_NO_FG_STEP", "완제품(FG)을 산출하는 공정이 1개 이상 필요합니다.")
    for o in r.versions:
        if o.status == "APPROVED":
            o.status = "OBSOLETE"
    v.status = "APPROVED"
    v.approved_by = cu.id
    v.approved_at = datetime.now(timezone.utc)
    audit(db, cu, "ROUTING_APPROVE", "ROUTING_VERSION", v.id)
    db.commit()
    db.refresh(r)
    return ok(_routing_out(db, r))


# ================================================================ 제조지시

class OrderIn(BaseModel):
    production_date: date
    product_id: uuid.UUID
    planned_quantity: Decimal | None = Field(default=None, gt=0)
    target_fg_quantity: Decimal | None = Field(default=None, gt=0)
    batch_count: int = Field(default=1, ge=1, le=100)
    line_code: str | None = None
    work_center: str | None = None
    assignee_id: uuid.UUID | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    routing_version_id: uuid.UUID | None = None
    note: str | None = None


class ApproveOrderIn(BaseModel):
    require_full_stock: bool = False


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1)


class CloseIn(BaseModel):
    remaining_wip_quantity: Decimal | None = Field(default=None, ge=0)
    confirmed_loss_quantity: Decimal = Field(default=Decimal(0), ge=0)
    override_reason: str | None = None


@router.get("/production-orders")
def list_orders(p: PageParams = Depends(), status: str | None = None, product_id: uuid.UUID | None = None, date_from: date | None = None,
                date_to: date | None = None, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:read"))):
    stmt = select(ProductionOrder).where(ProductionOrder.organization_id == cu.organization_id, ProductionOrder.facility_id == cu.facility_id)
    if status:
        stmt = stmt.where(ProductionOrder.status.in_(status.split(",")))
    if product_id:
        stmt = stmt.where(ProductionOrder.product_id == product_id)
    if date_from:
        stmt = stmt.where(ProductionOrder.production_date >= date_from)
    if date_to:
        stmt = stmt.where(ProductionOrder.production_date <= date_to)
    rows, total = paginate(db, stmt, p, ProductionOrder, [ProductionOrder.order_number], ProductionOrder.production_date.desc())
    out = []
    for o in rows:
        pr = db.get(Product, o.product_id)
        out.append(ser(o, exclude=("bom_snapshot", "routing_snapshot"), product_code=pr.product_code, product_name=pr.product_name,
                       bom_version_no=o.bom_snapshot.get("version_no")))
    return paged(out, total, p.page, p.size)


@router.post("/production-orders")
def create_order(body: OrderIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                 cu: CurrentUser = Depends(require("production:write"))):
    if (replay := idem.begin()) is not None:
        return replay
    o = order_svc.create(db, cu, body.model_dump())
    resp = idem.store(ok(order_svc.detail(db, cu, o), f"제조지시 {o.order_number} 이(가) 생성되었습니다."))
    db.commit()
    return resp


@router.get("/production-orders/{order_id}")
def get_order(order_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:read"))):
    return ok(order_svc.detail(db, cu, order_svc.get_order(db, cu, order_id)))


@router.get("/production-orders/{order_id}/requirements")
def order_requirements(order_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:read"))):
    return ok(order_svc.requirements(db, cu, order_svc.get_order(db, cu, order_id)))


@router.post("/production-orders/{order_id}/approve")
def approve_order(order_id: uuid.UUID, body: ApproveOrderIn | None = None, db: Session = Depends(get_db),
                  cu: CurrentUser = Depends(require("production:approve"))):
    o = order_svc.get_order(db, cu, order_id, lock=True)
    res = order_svc.approve(db, cu, o, body.require_full_stock if body else False)
    db.commit()
    msg = "제조지시가 승인되고 재고가 예약되었습니다." + (" (일부 원료 부족 — 확인 필요)" if res["shortages"] else "")
    return ok({**order_svc.detail(db, cu, o), "shortages": res["shortages"]}, msg)


def _simple(action: str, perm: str, msg: str, needs_reason: bool = False):
    if needs_reason:
        def handler(order_id: uuid.UUID, body: ReasonIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require(perm))):
            o = order_svc.get_order(db, cu, order_id, lock=True)
            order_svc.transition(db, cu, o, action, body.reason)
            db.commit()
            return ok(order_svc.detail(db, cu, o), msg)
    else:
        def handler(order_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require(perm))):  # type: ignore[misc]
            o = order_svc.get_order(db, cu, order_id, lock=True)
            order_svc.transition(db, cu, o, action)
            db.commit()
            return ok(order_svc.detail(db, cu, o), msg)
    return handler


router.post("/production-orders/{order_id}/start")(_simple("start", "production:execute", "작업이 시작되었습니다."))
router.post("/production-orders/{order_id}/hold")(_simple("hold", "production:execute", "보류되었습니다.", True))
router.post("/production-orders/{order_id}/release-hold")(_simple("release_hold", "production:execute", "보류가 해제되었습니다."))
router.post("/production-orders/{order_id}/complete")(_simple("complete", "production:execute", "생산이 완료되었습니다."))
router.post("/production-orders/{order_id}/cancel")(_simple("cancel", "production:write", "제조지시가 취소되었습니다.", True))


@router.post("/production-orders/{order_id}/close")
def close_order(order_id: uuid.UUID, body: CloseIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:close"))):
    o = order_svc.get_order(db, cu, order_id, lock=True)
    res = order_svc.close(db, cu, o, body.remaining_wip_quantity, body.confirmed_loss_quantity, body.override_reason)
    db.commit()
    return ok(res, "제조지시가 마감되었습니다.")


@router.get("/production-orders/{order_id}/yield")
def order_yield(order_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:read"))):
    return ok(order_svc.yield_report(db, order_svc.get_order(db, cu, order_id)))
