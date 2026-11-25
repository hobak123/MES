from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.db import get_db
from app.core.deps import CurrentUser, require
from app.core.errors import BusinessRule, InvalidState, NotFound
from app.core.idempotency import Idem, idempotency
from app.core.numbering import next_number, today_seoul
from app.core.responses import ok, paged
from app.models import (
    ApprovalRequest, InventoryBalance, InventoryReservation, InventoryTransaction, Material, MaterialPackage, Product,
    StockAdjustment, StockCount, StockTransfer, StorageLocation, TraceNode, Unit, Warehouse,
)
from app.repositories.common import PageParams, paginate, ser, ser_value
from app.services.inventory import service as inv

router = APIRouter(prefix="/inventory", tags=["재고"])
SEOUL = ZoneInfo("Asia/Seoul")


def _bal(db: Session, cu: CurrentUser, balance_id: uuid.UUID) -> InventoryBalance:
    b = db.get(InventoryBalance, balance_id)
    if not b or b.organization_id != cu.organization_id:
        raise NotFound("재고")
    cu.require_facility(b.facility_id)
    return b


def _balance_out(db: Session, b: InventoryBalance, cache: dict) -> dict:
    def get(model, i):
        if i is None:
            return None
        k = (model, i)
        if k not in cache:
            cache[k] = db.get(model, i)
        return cache[k]

    wh = get(Warehouse, b.warehouse_id)
    loc = get(StorageLocation, b.storage_location_id)
    m = get(Material, b.material_id)
    p = get(Product, b.product_id)
    u = get(Unit, b.unit_id)
    today = today_seoul()
    usable = (b.quality_status in inv.AVAILABLE_QUALITY and (b.expiry_date is None or b.expiry_date >= today)
              and wh.warehouse_type != "QUARANTINE")
    return ser(b, warehouse_name=wh.name, warehouse_type=wh.warehouse_type, location_code=loc.code if loc else None,
               item_code=m.material_code if m else (p.product_code if p else None),
               item_name=m.material_name if m else (p.product_name if p else None),
               major_category=m.major_category if m else None, unit=u.code if u else None,
               available_quantity=(b.quantity - b.reserved_quantity) if usable else Decimal(0),
               days_to_expiry=(b.expiry_date - today).days if b.expiry_date else None, usable=usable)


def _balance_query(cu: CurrentUser, item_type, material_id, product_id, warehouse_id, quality_status, major_category, lot_number,
                   include_zero: bool):
    stmt = select(InventoryBalance).where(InventoryBalance.organization_id == cu.organization_id)
    if cu.facility_id:
        stmt = stmt.where(InventoryBalance.facility_id == cu.facility_id)
    if not include_zero:
        stmt = stmt.where(InventoryBalance.quantity > 0)
    if item_type:
        stmt = stmt.where(InventoryBalance.item_type == item_type)
    if material_id:
        stmt = stmt.where(InventoryBalance.material_id == material_id)
    if product_id:
        stmt = stmt.where(InventoryBalance.product_id == product_id)
    if warehouse_id:
        stmt = stmt.where(InventoryBalance.warehouse_id == warehouse_id)
    if quality_status:
        stmt = stmt.where(InventoryBalance.quality_status.in_(quality_status.split(",")))
    if major_category:
        stmt = stmt.where(InventoryBalance.material_id.in_(select(Material.id).where(Material.major_category == major_category)))
    if lot_number:
        stmt = stmt.where(InventoryBalance.lot_number == lot_number)
    return stmt


@router.get("")
def summary(item_type: str | None = None, major_category: str | None = None, material_id: uuid.UUID | None = None,
            product_id: uuid.UUID | None = None, warehouse_id: uuid.UUID | None = None, q: str | None = None,
            db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:read"))):
    """품목별 집계: 현재고 / 예약 / 가용 / 격리·미승인."""
    today = today_seoul()
    qw = inv.quarantine_warehouse_ids(db, cu.organization_id)
    usable = and_(InventoryBalance.quality_status.in_(inv.AVAILABLE_QUALITY),
                  (InventoryBalance.expiry_date.is_(None)) | (InventoryBalance.expiry_date >= today),
                  InventoryBalance.warehouse_id.not_in(qw) if qw else True)
    stmt = select(
        InventoryBalance.item_type, InventoryBalance.material_id, InventoryBalance.product_id, InventoryBalance.unit_id,
        func.sum(InventoryBalance.quantity).label("on_hand"), func.sum(InventoryBalance.reserved_quantity).label("reserved"),
        func.sum(case((usable, InventoryBalance.quantity - InventoryBalance.reserved_quantity), else_=0)).label("available"),
        func.sum(case((usable, 0), else_=InventoryBalance.quantity)).label("blocked"),
        func.count(func.distinct(InventoryBalance.lot_node_id)).label("lot_count"),
        func.min(InventoryBalance.expiry_date).label("nearest_expiry"),
    ).where(InventoryBalance.organization_id == cu.organization_id, InventoryBalance.quantity > 0)
    if cu.facility_id:
        stmt = stmt.where(InventoryBalance.facility_id == cu.facility_id)
    for col, val in [(InventoryBalance.item_type, item_type), (InventoryBalance.material_id, material_id),
                     (InventoryBalance.product_id, product_id), (InventoryBalance.warehouse_id, warehouse_id)]:
        if val:
            stmt = stmt.where(col == val)
    if major_category:
        stmt = stmt.where(InventoryBalance.material_id.in_(select(Material.id).where(Material.major_category == major_category)))
    stmt = stmt.group_by(InventoryBalance.item_type, InventoryBalance.material_id, InventoryBalance.product_id, InventoryBalance.unit_id)
    out = []
    for r in db.execute(stmt).all():
        m = db.get(Material, r.material_id) if r.material_id else None
        p = db.get(Product, r.product_id) if r.product_id else None
        name = m.material_name if m else (p.product_name if p else "")
        code = m.material_code if m else (p.product_code if p else "")
        if q and q.lower() not in (name + code).lower():
            continue
        out.append({
            "item_type": r.item_type, "material_id": ser_value(r.material_id), "product_id": ser_value(r.product_id),
            "item_code": code, "item_name": name, "major_category": m.major_category if m else None,
            "unit": db.get(Unit, r.unit_id).code, "on_hand": ser_value(r.on_hand), "reserved": ser_value(r.reserved),
            "available": ser_value(r.available), "blocked": ser_value(r.blocked), "lot_count": r.lot_count,
            "nearest_expiry": ser_value(r.nearest_expiry),
            "safety_stock": ser_value(m.safety_stock) if m else None,
            "below_safety": bool(m and m.safety_stock is not None and r.available < m.safety_stock),
        })
    out.sort(key=lambda x: (x["item_type"], x["item_code"]))
    return ok(out)


@router.get("/lots")
def lots(p: PageParams = Depends(), item_type: str | None = None, material_id: uuid.UUID | None = None, product_id: uuid.UUID | None = None,
         warehouse_id: uuid.UUID | None = None, quality_status: str | None = None, major_category: str | None = None,
         db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:read"))):
    stmt = select(
        InventoryBalance.lot_node_id, InventoryBalance.item_type, InventoryBalance.material_id, InventoryBalance.product_id,
        InventoryBalance.lot_number, InventoryBalance.quality_status, InventoryBalance.expiry_date, InventoryBalance.unit_id,
        func.sum(InventoryBalance.quantity).label("quantity"), func.sum(InventoryBalance.reserved_quantity).label("reserved"),
        func.count(InventoryBalance.package_node_id).label("package_count"),
    ).where(InventoryBalance.organization_id == cu.organization_id, InventoryBalance.quantity > 0)
    if cu.facility_id:
        stmt = stmt.where(InventoryBalance.facility_id == cu.facility_id)
    for col, val in [(InventoryBalance.item_type, item_type), (InventoryBalance.material_id, material_id),
                     (InventoryBalance.product_id, product_id), (InventoryBalance.warehouse_id, warehouse_id)]:
        if val:
            stmt = stmt.where(col == val)
    if quality_status:
        stmt = stmt.where(InventoryBalance.quality_status.in_(quality_status.split(",")))
    if major_category:
        stmt = stmt.where(InventoryBalance.material_id.in_(select(Material.id).where(Material.major_category == major_category)))
    if p.q:
        stmt = stmt.where(InventoryBalance.lot_number.ilike(f"%{p.q}%"))
    stmt = stmt.group_by(InventoryBalance.lot_node_id, InventoryBalance.item_type, InventoryBalance.material_id, InventoryBalance.product_id,
                         InventoryBalance.lot_number, InventoryBalance.quality_status, InventoryBalance.expiry_date, InventoryBalance.unit_id)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(InventoryBalance.expiry_date.asc().nulls_last(), InventoryBalance.lot_number)
                      .offset((p.page - 1) * p.size).limit(p.size)).all()
    out = []
    for r in rows:
        m = db.get(Material, r.material_id) if r.material_id else None
        pr = db.get(Product, r.product_id) if r.product_id else None
        node = db.get(TraceNode, r.lot_node_id)
        out.append({"lot_node_id": str(r.lot_node_id), "entity_id": str(node.entity_id), "node_type": node.node_type,
                    "item_type": r.item_type, "lot_number": r.lot_number, "item_code": m.material_code if m else pr.product_code,
                    "item_name": m.material_name if m else pr.product_name, "quality_status": r.quality_status,
                    "expiry_date": ser_value(r.expiry_date), "quantity": ser_value(r.quantity), "reserved": ser_value(r.reserved),
                    "package_count": r.package_count, "unit": db.get(Unit, r.unit_id).code})
    return paged(out, total, p.page, p.size)


@router.get("/packages")
def packages(p: PageParams = Depends(), item_type: str | None = None, material_id: uuid.UUID | None = None,
             product_id: uuid.UUID | None = None, warehouse_id: uuid.UUID | None = None, quality_status: str | None = None,
             major_category: str | None = None, lot_number: str | None = None, include_zero: bool = False,
             db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:read"))):
    stmt = _balance_query(cu, item_type, material_id, product_id, warehouse_id, quality_status, major_category, lot_number, include_zero)
    rows, total = paginate(db, stmt, p, InventoryBalance, [InventoryBalance.lot_number, InventoryBalance.package_number],
                           InventoryBalance.lot_number)
    cache: dict = {}
    return paged([_balance_out(db, b, cache) for b in rows], total, p.page, p.size)


@router.get("/transactions")
def transactions(p: PageParams = Depends(), transaction_type: str | None = None, material_id: uuid.UUID | None = None,
                 product_id: uuid.UUID | None = None, lot_number: str | None = None, date_from: date | None = None,
                 date_to: date | None = None, reference_type: str | None = None, reference_id: uuid.UUID | None = None,
                 item_type: str | None = None, major_category: str | None = None,
                 db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:read"))):
    stmt = select(InventoryTransaction).where(InventoryTransaction.organization_id == cu.organization_id)
    if cu.facility_id:
        stmt = stmt.where(InventoryTransaction.facility_id == cu.facility_id)
    if transaction_type:
        stmt = stmt.where(InventoryTransaction.transaction_type.in_(transaction_type.split(",")))
    for col, val in [(InventoryTransaction.material_id, material_id), (InventoryTransaction.product_id, product_id),
                     (InventoryTransaction.reference_type, reference_type), (InventoryTransaction.reference_id, reference_id),
                     (InventoryTransaction.item_type, item_type)]:
        if val:
            stmt = stmt.where(col == val)
    if major_category:
        stmt = stmt.where(InventoryTransaction.material_id.in_(select(Material.id).where(Material.major_category == major_category)))
    if lot_number:
        stmt = stmt.where(InventoryTransaction.lot_node_id.in_(select(TraceNode.id).where(TraceNode.lot_number == lot_number)))
    if date_from:
        stmt = stmt.where(InventoryTransaction.performed_at >= datetime.combine(date_from, time.min, SEOUL))
    if date_to:
        stmt = stmt.where(InventoryTransaction.performed_at < datetime.combine(date_to + timedelta(days=1), time.min, SEOUL))
    rows, total = paginate(db, stmt, p, InventoryTransaction, [InventoryTransaction.transaction_number], InventoryTransaction.performed_at.desc())
    cache: dict = {}
    out = []
    for t in rows:
        b = cache.get(t.balance_id) or db.get(InventoryBalance, t.balance_id)
        cache[t.balance_id] = b
        m = db.get(Material, t.material_id) if t.material_id else None
        pr = db.get(Product, t.product_id) if t.product_id else None
        from app.models import User

        u = db.get(User, t.performed_by)
        out.append(ser(t, lot_number=b.lot_number, package_number=b.package_number,
                       item_code=m.material_code if m else (pr.product_code if pr else None),
                       item_name=m.material_name if m else (pr.product_name if pr else None),
                       unit=db.get(Unit, t.base_unit_id).code, performed_by_name=u.full_name if u else None,
                       warehouse_name=db.get(Warehouse, t.warehouse_id).name))
    return paged(out, total, p.page, p.size)


@router.get("/expiring")
def expiring(days: int = Query(90, ge=0, le=3650), include_expired: bool = True, db: Session = Depends(get_db),
             cu: CurrentUser = Depends(require("inventory:read"))):
    today = today_seoul()
    stmt = select(InventoryBalance).where(InventoryBalance.organization_id == cu.organization_id, InventoryBalance.quantity > 0,
                                          InventoryBalance.expiry_date.is_not(None),
                                          InventoryBalance.expiry_date <= today + timedelta(days=days))
    if cu.facility_id:
        stmt = stmt.where(InventoryBalance.facility_id == cu.facility_id)
    if not include_expired:
        stmt = stmt.where(InventoryBalance.expiry_date >= today)
    cache: dict = {}
    rows = [_balance_out(db, b, cache) for b in db.execute(stmt.order_by(InventoryBalance.expiry_date)).scalars()]
    for r in rows:
        d = r["days_to_expiry"]
        r["alert_level"] = "EXPIRED" if d < 0 else next((f"D-{x}" for x in (7, 30, 60, 90) if d <= x), "OK")
    return ok(rows)


@router.get("/low-stock")
def low_stock(db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:read"))):
    out = []
    for m in db.execute(select(Material).where(Material.organization_id == cu.organization_id, Material.is_active,
                                               Material.safety_stock.is_not(None))).scalars():
        avail = inv.available_quantity(db, cu.organization_id, cu.facility_id, material_id=m.id)
        if avail < m.safety_stock:
            out.append({"material_id": str(m.id), "material_code": m.material_code, "material_name": m.material_name,
                        "major_category": m.major_category, "safety_stock": ser_value(m.safety_stock), "available": ser_value(avail),
                        "shortage": ser_value(m.safety_stock - avail), "unit": m.base_unit.code})
    return ok(out)


@router.get("/recommend")
def recommend(material_id: uuid.UUID, quantity: Decimal = Query(gt=0), db: Session = Depends(get_db),
              cu: CurrentUser = Depends(require("inventory:read"))):
    m = db.get(Material, material_id)
    if not m or m.organization_id != cu.organization_id:
        raise NotFound("원료")
    rem = quantity
    picks = []
    cache: dict = {}
    for b in inv.available_balances(db, cu.organization_id, cu.facility_id, material_id=m.id, fefo=m.expiry_tracking_required):
        if rem <= 0:
            break
        avail = b.quantity - b.reserved_quantity
        take = min(avail, rem)
        picks.append({**_balance_out(db, b, cache), "suggested_quantity": ser_value(take)})
        rem -= take
    return ok({"method": "FEFO" if m.expiry_tracking_required else "FIFO", "requested": ser_value(quantity),
               "shortage": ser_value(max(rem, Decimal(0))), "picks": picks})


class TransferIn(BaseModel):
    balance_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    to_warehouse_id: uuid.UUID
    to_storage_location_id: uuid.UUID | None = None
    reason: str | None = None


@router.post("/transfer")
def transfer(body: TransferIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
             cu: CurrentUser = Depends(require("inventory:move"))):
    if (replay := idem.begin()) is not None:
        return replay
    b = _bal(db, cu, body.balance_id)
    wh = db.get(Warehouse, body.to_warehouse_id)
    if not wh or wh.organization_id != cu.organization_id or not wh.is_active:
        raise NotFound("도착 창고")
    cu.require_facility(wh.facility_id)
    if wh.facility_id != b.facility_id:
        raise BusinessRule("CROSS_FACILITY", "공장 간 이동은 출고/입고 절차로 처리하세요.")
    if wh.warehouse_type == "QUARANTINE" or db.get(Warehouse, b.warehouse_id).warehouse_type == "QUARANTINE":
        if not cu.has("quality:hold") and not cu.has("inventory:adjust_approve"):
            raise BusinessRule("QUARANTINE_TRANSFER", "격리 창고 입출은 품질관리 권한이 필요합니다.")
    st = StockTransfer(organization_id=cu.organization_id, facility_id=b.facility_id, transfer_number=next_number(db, cu.organization_id, "TRF"),
                       balance_id=b.id, to_warehouse_id=wh.id, to_storage_location_id=body.to_storage_location_id, quantity=body.quantity,
                       reason=body.reason, created_by=cu.id)
    db.add(st)
    db.flush()
    ctx = inv.Ctx(cu.organization_id, b.facility_id, cu.id, reference_type="STOCK_TRANSFER", reference_id=st.id, reason=body.reason)
    st.transaction_group_id = ctx.group_id
    out, inn = inv.transfer(db, ctx, b.id, body.quantity, wh.id, body.to_storage_location_id, reason=body.reason)
    audit(db, cu, "STOCK_TRANSFER", "INVENTORY_BALANCE", b.id, after={"quantity": str(body.quantity), "to": str(wh.id)}, reason=body.reason)
    cache: dict = {}
    resp = idem.store(ok({"transfer": ser(st), "from": _balance_out(db, out.balance, cache), "to": _balance_out(db, inn.balance, cache)},
                         "재고가 이동되었습니다."))
    db.commit()
    return resp


class AdjustIn(BaseModel):
    balance_id: uuid.UUID
    direction: Literal["IN", "OUT"]
    quantity: Decimal = Field(gt=0)
    reason: str = Field(min_length=1)


def _create_adjustment(db: Session, cu: CurrentUser, b: InventoryBalance, direction: str, qty: Decimal, reason: str,
                       kind: str = "ADJUSTMENT", stock_count_id: uuid.UUID | None = None) -> StockAdjustment:
    adj = StockAdjustment(organization_id=cu.organization_id, facility_id=b.facility_id,
                          adjustment_number=next_number(db, cu.organization_id, "ADJ"), balance_id=b.id, direction=direction,
                          adjustment_kind=kind, quantity=qty, reason=reason, stock_count_id=stock_count_id, requested_by=cu.id,
                          created_by=cu.id)
    db.add(adj)
    db.flush()
    db.add(ApprovalRequest(organization_id=cu.organization_id, facility_id=b.facility_id, request_type="STOCK_ADJUSTMENT",
                           target_type="STOCK_ADJUSTMENT", target_id=adj.id, reason=reason, requested_by=cu.id,
                           payload={"kind": kind, "direction": direction, "quantity": str(qty), "lot_number": b.lot_number}))
    audit(db, cu, "STOCK_ADJUSTMENT_REQUEST", "STOCK_ADJUSTMENT", adj.id, after={"kind": kind, "direction": direction, "quantity": str(qty)},
          reason=reason)
    return adj


def execute_adjustment(db: Session, cu: CurrentUser, adj: StockAdjustment) -> None:
    if adj.status != "PENDING":
        raise InvalidState(adj.status, "조정 승인")
    if adj.requested_by == cu.id and not cu.is_super:
        raise BusinessRule("SELF_APPROVAL", "본인이 요청한 조정은 승인할 수 없습니다.")
    ctx = inv.Ctx(cu.organization_id, adj.facility_id, cu.id, reference_type="STOCK_ADJUSTMENT", reference_id=adj.id, reason=adj.reason)
    b = db.get(InventoryBalance, adj.balance_id)
    if adj.direction == "IN":
        key = inv.StockKey(item_type=b.item_type, lot_node_id=b.lot_node_id, package_node_id=b.package_node_id, warehouse_id=b.warehouse_id,
                           storage_location_id=b.storage_location_id, unit_id=b.unit_id, lot_number=b.lot_number,
                           package_number=b.package_number, quality_status=b.quality_status, material_id=b.material_id,
                           product_id=b.product_id, expiry_date=b.expiry_date)
        inv.receive(db, ctx, key, adj.quantity, "ADJUSTMENT_IN")
    else:
        tx_type = {"ADJUSTMENT": "ADJUSTMENT_OUT"}.get(adj.adjustment_kind, adj.adjustment_kind)
        inv.issue(db, ctx, b.id, adj.quantity, tx_type, ignore_reservations=False)
        _update_package_status(db, b)
    adj.status = "APPROVED"
    adj.approved_by = cu.id
    adj.approved_at = datetime.now(timezone.utc)
    adj.transaction_group_id = ctx.group_id
    for ar in db.execute(select(ApprovalRequest).where(ApprovalRequest.target_id == adj.id, ApprovalRequest.status == "PENDING")).scalars():
        ar.status = "EXECUTED"
        ar.decided_by = cu.id
        ar.decided_at = datetime.now(timezone.utc)
    audit(db, cu, "STOCK_ADJUSTMENT_APPROVE", "STOCK_ADJUSTMENT", adj.id, reason=adj.reason)


def _update_package_status(db: Session, b: InventoryBalance) -> None:
    if b.item_type == "MATERIAL" and b.package_node_id:
        node = db.get(TraceNode, b.package_node_id)
        pkg = db.get(MaterialPackage, node.entity_id)
        total = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(InventoryBalance.package_node_id == b.package_node_id)).scalar_one()
        if pkg and pkg.package_status in ("AVAILABLE", "IN_USE", "EMPTY"):
            pkg.package_status = "EMPTY" if total == 0 else ("IN_USE" if total < pkg.original_quantity else "AVAILABLE")


@router.post("/adjustment")
def adjustment(body: AdjustIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:adjust_request"))):
    b = _bal(db, cu, body.balance_id)
    if body.direction == "OUT" and body.quantity > b.quantity:
        raise BusinessRule("INSUFFICIENT_STOCK", "조정 수량이 현재고보다 많습니다.", {"on_hand": ser_value(b.quantity)})
    adj = _create_adjustment(db, cu, b, body.direction, body.quantity, body.reason)
    db.commit()
    return ok(ser(adj), "재고 조정이 요청되었습니다. 관리자 승인 후 반영됩니다.")


@router.get("/adjustments")
def list_adjustments(p: PageParams = Depends(), status: str | None = None, db: Session = Depends(get_db),
                     cu: CurrentUser = Depends(require("inventory:read"))):
    stmt = select(StockAdjustment).where(StockAdjustment.organization_id == cu.organization_id)
    if cu.facility_id:
        stmt = stmt.where(StockAdjustment.facility_id == cu.facility_id)
    if status:
        stmt = stmt.where(StockAdjustment.status == status)
    rows, total = paginate(db, stmt, p, StockAdjustment, [StockAdjustment.adjustment_number], StockAdjustment.created_at.desc())
    out = []
    for a in rows:
        b = db.get(InventoryBalance, a.balance_id)
        out.append(ser(a, lot_number=b.lot_number, package_number=b.package_number))
    return paged(out, total, p.page, p.size)


@router.post("/adjustments/{adj_id}/approve")
def approve_adjustment(adj_id: uuid.UUID, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                       cu: CurrentUser = Depends(require("inventory:adjust_approve"))):
    if (replay := idem.begin()) is not None:
        return replay
    adj = db.execute(select(StockAdjustment).where(StockAdjustment.id == adj_id, StockAdjustment.organization_id == cu.organization_id)
                     .with_for_update()).scalar_one_or_none()
    if not adj:
        raise NotFound("재고 조정")
    cu.require_facility(adj.facility_id)
    execute_adjustment(db, cu, adj)
    resp = idem.store(ok(ser(adj), "재고 조정이 승인·반영되었습니다."))
    db.commit()
    return resp


class RejectIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/adjustments/{adj_id}/reject")
def reject_adjustment(adj_id: uuid.UUID, body: RejectIn, db: Session = Depends(get_db),
                      cu: CurrentUser = Depends(require("inventory:adjust_approve"))):
    adj = db.get(StockAdjustment, adj_id)
    if not adj or adj.organization_id != cu.organization_id:
        raise NotFound("재고 조정")
    if adj.status != "PENDING":
        raise InvalidState(adj.status, "반려")
    adj.status = "REJECTED"
    adj.approved_by = cu.id
    adj.approved_at = datetime.now(timezone.utc)
    for ar in db.execute(select(ApprovalRequest).where(ApprovalRequest.target_id == adj.id, ApprovalRequest.status == "PENDING")).scalars():
        ar.status = "REJECTED"
        ar.decided_by = cu.id
        ar.decided_at = datetime.now(timezone.utc)
        ar.decision_note = body.reason
    audit(db, cu, "STOCK_ADJUSTMENT_REJECT", "STOCK_ADJUSTMENT", adj.id, reason=body.reason)
    db.commit()
    return ok(ser(adj), "반려되었습니다.")


class CountLine(BaseModel):
    balance_id: uuid.UUID
    counted_quantity: Decimal = Field(ge=0)


class StockCountIn(BaseModel):
    warehouse_id: uuid.UUID | None = None
    count_date: date | None = None
    lines: list[CountLine] = Field(min_length=1)
    note: str | None = None


@router.post("/stock-count")
def stock_count(body: StockCountIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:count"))):
    sc = StockCount(organization_id=cu.organization_id, facility_id=cu.facility_id, count_number=next_number(db, cu.organization_id, "CNT"),
                    count_date=body.count_date or today_seoul(), warehouse_id=body.warehouse_id, lines=[], note=body.note, created_by=cu.id)
    db.add(sc)
    db.flush()
    lines, n_adj = [], 0
    for ln in body.lines:
        b = _bal(db, cu, ln.balance_id)
        diff = ln.counted_quantity - b.quantity
        adj_id = None
        if diff != 0:
            adj = _create_adjustment(db, cu, b, "IN" if diff > 0 else "OUT", abs(diff), f"재고 실사 {sc.count_number} 차이",
                                     stock_count_id=sc.id)
            adj_id = str(adj.id)
            n_adj += 1
        lines.append({"balance_id": str(b.id), "lot_number": b.lot_number, "package_number": b.package_number,
                      "system_quantity": ser_value(b.quantity), "counted_quantity": ser_value(ln.counted_quantity),
                      "difference": ser_value(diff), "adjustment_id": adj_id})
    sc.lines = lines
    sc.status = "ADJUSTMENT_REQUESTED" if n_adj else "CLOSED"
    audit(db, cu, "STOCK_COUNT", "STOCK_COUNT", sc.id, after={"lines": len(lines), "adjustments": n_adj})
    db.commit()
    return ok(ser(sc), f"실사가 등록되었습니다. 차이 {n_adj}건은 승인 후 반영됩니다.")


class ReserveIn(BaseModel):
    balance_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    production_order_id: uuid.UUID | None = None
    reason: str | None = None


@router.post("/reservation")
def reservation(body: ReserveIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:reserve"))):
    b = _bal(db, cu, body.balance_id)
    ctx = inv.Ctx(cu.organization_id, b.facility_id, cu.id)
    r = inv.reserve(db, ctx, b.id, body.quantity, order_id=body.production_order_id, reason=body.reason)
    audit(db, cu, "STOCK_RESERVE", "INVENTORY_BALANCE", b.id, after={"quantity": str(body.quantity)})
    db.commit()
    return ok(ser(r), "예약되었습니다.")


class ReturnIn(BaseModel):
    balance_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    production_order_id: uuid.UUID | None = None
    reason: str = Field(min_length=1)
    contaminated: bool = False


@router.post("/return")
def production_return(body: ReturnIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                      cu: CurrentUser = Depends(require("inventory:move"))):
    """생산 반납: 원 포장(잔량 행)으로 PRODUCTION_RETURN. 혼합/오염 원료는 정상 반납 금지."""
    if (replay := idem.begin()) is not None:
        return replay
    if body.contaminated:
        raise BusinessRule("CONTAMINATED_RETURN", "혼합/오염된 원료는 정상 재고로 반납할 수 없습니다. 폐기로 처리하세요.")
    b = _bal(db, cu, body.balance_id)
    if b.item_type != "MATERIAL":
        raise BusinessRule("INVALID_RETURN", "원료 포장만 반납할 수 있습니다.")
    # 반납 가능 수량: 해당 포장에서 생산 출고된 누적 - 이미 반납/정정된 수량
    issued = db.execute(select(func.coalesce(func.sum(InventoryTransaction.quantity), 0)).where(
        InventoryTransaction.balance_id == b.id, InventoryTransaction.transaction_type == "PRODUCTION_ISSUE")).scalar_one()
    returned = db.execute(select(func.coalesce(func.sum(InventoryTransaction.quantity), 0)).where(
        InventoryTransaction.balance_id == b.id, InventoryTransaction.transaction_type.in_(("PRODUCTION_RETURN",)))).scalar_one()
    if body.quantity > issued - returned:
        raise BusinessRule("RETURN_EXCEEDS_ISSUED", "반납 수량이 생산 출고 수량을 초과합니다.", {"returnable": ser_value(issued - returned)})
    ctx = inv.Ctx(cu.organization_id, b.facility_id, cu.id, reference_type="PRODUCTION_RETURN", reference_id=body.production_order_id,
                  reason=body.reason)
    key = inv.StockKey(item_type=b.item_type, lot_node_id=b.lot_node_id, package_node_id=b.package_node_id, warehouse_id=b.warehouse_id,
                       storage_location_id=b.storage_location_id, unit_id=b.unit_id, lot_number=b.lot_number, package_number=b.package_number,
                       quality_status=b.quality_status, material_id=b.material_id, expiry_date=b.expiry_date)
    mv = inv.receive(db, ctx, key, body.quantity, "PRODUCTION_RETURN", idempotency_key=idem.key)
    _update_package_status(db, mv.balance)
    audit(db, cu, "PRODUCTION_RETURN", "INVENTORY_BALANCE", b.id, after={"quantity": str(body.quantity)}, reason=body.reason)
    resp = idem.store(ok(_balance_out(db, mv.balance, {}), "반납되었습니다."))
    db.commit()
    return resp


class DisposalIn(BaseModel):
    balance_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    kind: Literal["DISPOSAL", "SUPPLIER_RETURN", "SAMPLE_USE", "GENERAL_USE"] = "DISPOSAL"
    reason: str = Field(min_length=1)


@router.post("/disposal")
def disposal(body: DisposalIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:dispose"))):
    b = _bal(db, cu, body.balance_id)
    if body.quantity > b.quantity:
        raise BusinessRule("INSUFFICIENT_STOCK", "요청 수량이 현재고보다 많습니다.", {"on_hand": ser_value(b.quantity)})
    adj = _create_adjustment(db, cu, b, "OUT", body.quantity, body.reason, kind=body.kind)
    db.commit()
    return ok(ser(adj), "요청되었습니다. 승인 후 수불에 반영됩니다.")


@router.post("/verify-ledger")
def verify_ledger(db: Session = Depends(get_db), cu: CurrentUser = Depends(require("inventory:adjust_approve"))):
    """원장 정합성 검증: 잔량 = Σ(IN) - Σ(OUT), 마지막 balance_after 와 일치."""
    from sqlalchemy import text

    rows = db.execute(text("""
        SELECT b.id, b.lot_number, b.package_number, b.quantity,
               COALESCE(SUM(CASE WHEN t.direction='IN' THEN t.quantity ELSE -t.quantity END), 0) AS ledger
        FROM inventory_balances b LEFT JOIN inventory_transactions t ON t.balance_id = b.id
        WHERE b.organization_id = :org
        GROUP BY b.id HAVING b.quantity <> COALESCE(SUM(CASE WHEN t.direction='IN' THEN t.quantity ELSE -t.quantity END), 0)
    """), {"org": cu.organization_id}).mappings().all()
    total = db.execute(select(func.count()).select_from(InventoryBalance).where(InventoryBalance.organization_id == cu.organization_id)).scalar_one()
    return ok({"checked": total, "mismatches": [dict(ser_value(dict(r))) for r in rows], "consistent": not rows})
