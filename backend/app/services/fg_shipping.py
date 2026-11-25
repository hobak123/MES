"""완제품 품질(검사/승인/보류/하위 포장) · 출고(확정/정정/반품) · 회수."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import BusinessRule, InvalidState, NotFound
from app.core.numbering import next_number, today_seoul
from app.models import (
    Customer, FinishedGoodsLot, FinishedGoodsPackage, InspectionTemplate, InventoryBalance, InventoryTransaction, MaterialLot,
    MaterialPackage, Product, QualityInspection, QualityInspectionResult, RecallCase, RecallCaseLot, RecallCaseShipment,
    ShipmentHeader, ShipmentItem, ShipmentReturn, TraceEvent, TraceNode, Warehouse, WeighingBatch, WipLot,
)
from app.repositories.common import ser, ser_value
from app.services.inventory import service as inv
from app.services.production.process import fg_expiry
from app.services.qr.resolver import find_qr
from app.services.qr.service import ensure_qr
from app.services.traceability import service as trace

ZERO = Decimal(0)


# ============================================================ FG quality

def get_fg(db: Session, cu: Any, fg_id: uuid.UUID, lock: bool = False) -> FinishedGoodsLot:
    stmt = select(FinishedGoodsLot).where(FinishedGoodsLot.id == fg_id, FinishedGoodsLot.organization_id == cu.organization_id)
    if lock:
        stmt = stmt.with_for_update()
    fg = db.execute(stmt).scalar_one_or_none()
    if not fg:
        raise NotFound("완제품 LOT")
    cu.require_facility(fg.facility_id)
    return fg


def _set_fg_quality(db: Session, cu: Any, fg: FinishedGoodsLot, status: str, reason: str | None = None) -> None:
    before = fg.quality_status
    fg.quality_status = status
    inv.set_lot_quality(db, cu.organization_id, fg.trace_node_id, status)
    audit(db, cu, "FG_QUALITY_CHANGE", "FG_LOT", fg.id, before={"quality_status": before}, after={"quality_status": status}, reason=reason)


def inspect_fg(db: Session, cu: Any, fg: FinishedGoodsLot, body: dict[str, Any]) -> QualityInspection:
    if fg.quality_status not in ("QUALITY_PENDING", "HOLD"):
        raise InvalidState(fg.quality_status, "완제품 검사")
    overall = body["overall_result"]
    if overall == "FAIL" and not body.get("nonconformance_reason"):
        raise BusinessRule("REASON_REQUIRED", "부적합 사유를 입력하세요.")
    tpl = db.execute(select(InspectionTemplate).where(InspectionTemplate.organization_id == cu.organization_id,
                                                      InspectionTemplate.code == "FG-STD")).scalar_one_or_none()
    qi = QualityInspection(organization_id=cu.organization_id, facility_id=fg.facility_id, inspection_number=next_number(db, cu.organization_id, "QI"),
                           target_type="FINISHED_GOODS", target_id=fg.id, template_id=tpl.id if tpl else None, source="FIELD",
                           overall_result=overall, nonconformance_reason=body.get("nonconformance_reason"), inspected_by=cu.id,
                           inspected_at=datetime.now(timezone.utc), note=body.get("note"), created_by=cu.id)
    db.add(qi)
    db.flush()
    for r in body.get("results", []):
        db.add(QualityInspectionResult(inspection_id=qi.id, item_code=r["item_code"], item_name=r.get("item_name") or r["item_code"],
                                       result=r.get("result", "PASS"), measured_value=r.get("measured_value"), text_value=r.get("text_value"),
                                       standard=r.get("standard"), note=r.get("note")))
    if overall == "FAIL":
        _set_fg_quality(db, cu, fg, "REJECTED", body.get("nonconformance_reason"))
    elif overall == "HOLD":
        _set_fg_quality(db, cu, fg, "HOLD", body.get("note"))
    audit(db, cu, "FG_INSPECTION", "FG_LOT", fg.id, after={"result": overall})
    return qi


def release_fg(db: Session, cu: Any, fg: FinishedGoodsLot, confirm_expiry_date: Any = None, note: str | None = None) -> None:
    if fg.quality_status not in ("QUALITY_PENDING", "HOLD"):
        raise InvalidState(fg.quality_status, "출고 승인(가용 전환)")
    last = db.execute(select(QualityInspection).where(QualityInspection.target_type == "FINISHED_GOODS", QualityInspection.target_id == fg.id)
                      .order_by(QualityInspection.inspected_at.desc())).scalars().first()
    if not last or last.overall_result != "PASS":
        raise BusinessRule("INSPECTION_NOT_PASSED", "완제품 검사 합격 후 승인할 수 있습니다.")
    p = db.get(Product, fg.product_id)
    expected = fg_expiry(p, fg.expiry_basis_date or fg.manufactured_date)
    if expected != fg.expiry_date:
        raise BusinessRule("EXPIRY_MISMATCH", f"소비기한이 제품 기준({expected})과 다릅니다. 확인 후 승인하세요.")
    if confirm_expiry_date is not None and str(confirm_expiry_date) != str(fg.expiry_date):
        raise BusinessRule("EXPIRY_MISMATCH", "확인한 소비기한이 LOT 소비기한과 다릅니다.")
    if fg.expiry_date and fg.expiry_date < today_seoul():
        raise BusinessRule("EXPIRED_PRODUCT", "소비기한이 경과한 완제품입니다.")
    _set_fg_quality(db, cu, fg, "RELEASED", note)
    fg.released_by = cu.id
    fg.released_at = datetime.now(timezone.utc)


def hold_fg(db: Session, cu: Any, fg: FinishedGoodsLot, reason: str) -> None:
    if fg.quality_status in ("REJECTED",):
        raise InvalidState(fg.quality_status, "보류")
    _set_fg_quality(db, cu, fg, "HOLD", reason)


def create_packages(db: Session, cu: Any, fg: FinishedGoodsLot, level: str, count: int, qty_each: Decimal,
                    parent_ids: list[uuid.UUID] | None = None) -> list[FinishedGoodsPackage]:
    existing = db.execute(select(func.coalesce(func.sum(FinishedGoodsPackage.quantity), 0)).where(
        FinishedGoodsPackage.fg_lot_id == fg.id, FinishedGoodsPackage.package_level == level)).scalar_one()
    if Decimal(existing) + qty_each * count > fg.good_quantity:
        raise BusinessRule("PACKAGE_EXCEEDS_LOT", "하위 포장 수량 합계가 LOT 정상 수량을 초과합니다.",
                           {"lot_quantity": ser_value(fg.good_quantity), "already": ser_value(Decimal(existing))})
    out, refs = [], []
    for _ in range(count):
        pk = FinishedGoodsPackage(id=uuid.uuid4(), organization_id=cu.organization_id, fg_lot_id=fg.id, package_level=level,
                                  package_number=next_number(db, cu.organization_id, {"UNIT": "FGU", "BOX": "BOX", "PALLET": "PLT"}[level]),
                                  quantity=qty_each, created_by=cu.id)
        db.add(pk)
        db.flush()
        node = trace.create_node(db, cu.organization_id, fg.facility_id, "FINISHED_GOODS_PACKAGE", pk.id, pk.package_number, level)
        pk.trace_node_id = node.id
        pk.qr_code_id = ensure_qr(db, org_id=cu.organization_id, qr_type="FINISHED_GOODS_PACKAGE", entity_id=pk.id, lot_number=fg.lot_number, user_id=cu.id).id
        refs.append(trace.NodeRef(node.id, qty_each, fg.base_unit_id))
        out.append(pk)
    trace.record_event(db, org_id=cu.organization_id, facility_id=fg.facility_id, user_id=cu.id, event_type="SPLIT",
                       inputs=[trace.NodeRef(fg.trace_node_id, qty_each * count, fg.base_unit_id)], outputs=refs,
                       reference_type="FG_PACKAGING", reference_id=fg.id, note=f"{level} 포장 단위 생성")
    audit(db, cu, "FG_PACKAGES_CREATE", "FG_LOT", fg.id, after={"level": level, "count": count, "qty_each": str(qty_each)})
    return out


def fg_detail(db: Session, fg: FinishedGoodsLot) -> dict[str, Any]:
    p = db.get(Product, fg.product_id)
    from app.models import PackagingConsumption, Material, ProductionOrder

    stock = db.execute(select(InventoryBalance).where(InventoryBalance.lot_node_id == fg.trace_node_id, InventoryBalance.quantity > 0)).scalars().all()
    stock_rows = [{"warehouse": db.get(Warehouse, b.warehouse_id).name, "warehouse_type": db.get(Warehouse, b.warehouse_id).warehouse_type,
                   "quantity": ser_value(b.quantity), "reserved": ser_value(b.reserved_quantity), "balance_id": str(b.id)} for b in stock]
    insps = [ser(q, results=[ser(r) for r in q.results]) for q in db.execute(select(QualityInspection).where(
        QualityInspection.target_type == "FINISHED_GOODS", QualityInspection.target_id == fg.id).order_by(QualityInspection.inspected_at)).scalars()]
    packs = []
    for pc in db.execute(select(PackagingConsumption).where(PackagingConsumption.fg_lot_id == fg.id)).scalars():
        packs.append({"material_name": db.get(Material, pc.material_id).material_name, "lot_number": db.get(MaterialLot, pc.material_lot_id).lot_number,
                      "used": ser_value(pc.used_quantity), "defective": ser_value(pc.defective_quantity)})
    ships = []
    for si in db.execute(select(ShipmentItem).where(ShipmentItem.fg_lot_id == fg.id)).scalars():
        sh = db.get(ShipmentHeader, si.shipment_id)
        ships.append({"shipment_number": sh.shipment_number, "status": sh.status, "customer": db.get(Customer, sh.customer_id).name,
                      "date": ser_value(sh.shipment_date), "quantity": ser_value(si.quantity), "returned": ser_value(si.returned_quantity)})
    pkgs = [ser(x) for x in db.execute(select(FinishedGoodsPackage).where(FinishedGoodsPackage.fg_lot_id == fg.id)).scalars()]
    o = db.get(ProductionOrder, fg.production_order_id)
    return ser(fg, product_code=p.product_code, product_name=p.product_name, unit=p.base_unit.code, order_number=o.order_number,
               current_stock=sum((b.quantity for b in stock), ZERO), stock=stock_rows, inspections=insps, packaging=packs,
               shipments=ships, packages=pkgs)


# ============================================================ shipments

def get_shipment(db: Session, cu: Any, sid: uuid.UUID, lock: bool = False) -> ShipmentHeader:
    stmt = select(ShipmentHeader).where(ShipmentHeader.id == sid, ShipmentHeader.organization_id == cu.organization_id)
    if lock:
        stmt = stmt.with_for_update()
    s = db.execute(stmt).scalar_one_or_none()
    if not s:
        raise NotFound("출고")
    cu.require_facility(s.facility_id)
    return s


def _fg_from_item(db: Session, cu: Any, it: dict[str, Any]) -> tuple[FinishedGoodsLot, FinishedGoodsPackage | None]:
    pkg = None
    if it.get("fg_lot_id"):
        fg = db.get(FinishedGoodsLot, it["fg_lot_id"])
    else:
        qr = find_qr(db, cu.organization_id, it.get("raw") or "")
        if not qr or qr.qr_type not in ("FINISHED_GOODS_LOT", "FINISHED_GOODS_PACKAGE"):
            raise BusinessRule("INVALID_QR", "완제품 LOT/포장 QR 을 스캔하세요.")
        if qr.qr_type == "FINISHED_GOODS_PACKAGE":
            pkg = db.get(FinishedGoodsPackage, qr.entity_id)
            fg = db.get(FinishedGoodsLot, pkg.fg_lot_id)
        else:
            fg = db.get(FinishedGoodsLot, qr.entity_id)
    if not fg or fg.organization_id != cu.organization_id:
        raise NotFound("완제품 LOT")
    return fg, pkg


def _sellable_balances(db: Session, org_id: uuid.UUID, fg: FinishedGoodsLot) -> list[InventoryBalance]:
    qw = inv.quarantine_warehouse_ids(db, org_id)
    stmt = select(InventoryBalance).where(InventoryBalance.lot_node_id == fg.trace_node_id, InventoryBalance.quantity > 0)
    if qw:
        stmt = stmt.where(InventoryBalance.warehouse_id.not_in(qw))
    return list(db.execute(stmt.order_by(InventoryBalance.quantity.desc())).scalars())


def create_shipment(db: Session, cu: Any, body: dict[str, Any]) -> ShipmentHeader:
    c = db.get(Customer, body["customer_id"])
    if not c or c.organization_id != cu.organization_id:
        raise NotFound("거래처")
    if not c.is_active or c.trade_status != "ACTIVE":
        raise BusinessRule("CUSTOMER_INACTIVE", "거래 중지된 거래처입니다.")
    sh = ShipmentHeader(organization_id=cu.organization_id, facility_id=cu.facility_id, shipment_number=next_number(db, cu.organization_id, "SHP"),
                        shipment_date=body.get("shipment_date") or today_seoul(), customer_id=c.id,
                        delivery_address=body.get("delivery_address") or c.delivery_address, delivery_info=body.get("delivery_info"),
                        handler_id=cu.id, note=body.get("note"), status="DRAFT", created_by=cu.id)
    db.add(sh)
    db.flush()
    for i, it in enumerate(body["items"], start=1):
        fg, pkg = _fg_from_item(db, cu, it)
        qty = Decimal(str(it.get("quantity") or (pkg.quantity if pkg else 0)))
        if qty <= 0:
            raise BusinessRule("INVALID_QUANTITY", "출고 수량은 0보다 커야 합니다.")
        bals = _sellable_balances(db, cu.organization_id, fg)
        wh = bals[0].warehouse_id if bals else fg.warehouse_id
        db.add(ShipmentItem(organization_id=cu.organization_id, shipment_id=sh.id, line_no=i, product_id=fg.product_id, fg_lot_id=fg.id,
                            fg_package_id=pkg.id if pkg else None, warehouse_id=wh, quantity=qty, unit_id=fg.base_unit_id))
    db.flush()
    db.refresh(sh)
    audit(db, cu, "SHIPMENT_CREATE", "SHIPMENT", sh.id, after={"number": sh.shipment_number})
    return sh


def confirm_shipment(db: Session, cu: Any, sh: ShipmentHeader, idempotency_key: str | None) -> None:
    if sh.status != "DRAFT":
        raise InvalidState(sh.status, "출고 확정")
    today = today_seoul()
    ctx = inv.Ctx(cu.organization_id, sh.facility_id, cu.id, reference_type="SHIPMENT", reference_id=sh.id, reason=f"출고 {sh.shipment_number}")
    fg_refs: dict[uuid.UUID, Decimal] = {}
    for it in sh.items:
        fg = db.execute(select(FinishedGoodsLot).where(FinishedGoodsLot.id == it.fg_lot_id).with_for_update()).scalar_one()
        if fg.quality_status != "RELEASED":
            raise BusinessRule("QUALITY_NOT_RELEASED", f"품질 승인되지 않은 완제품은 출고할 수 없습니다: {fg.lot_number} ({fg.quality_status})")
        if fg.expiry_date and fg.expiry_date < today:
            raise BusinessRule("EXPIRED_PRODUCT", f"소비기한이 경과한 완제품입니다: {fg.lot_number}")
        if it.fg_package_id:
            pk = db.get(FinishedGoodsPackage, it.fg_package_id)
            if pk.status != "IN_STOCK":
                raise BusinessRule("PACKAGE_NOT_USABLE", f"이미 출고/반품된 포장입니다: {pk.package_number}")
            pk.status = "SHIPPED"
        rem = it.quantity
        bals = _sellable_balances(db, cu.organization_id, fg)
        avail = sum((b.quantity - b.reserved_quantity for b in bals), ZERO)
        if rem > avail:
            raise BusinessRule("INSUFFICIENT_STOCK", f"출고 가능 재고가 부족합니다: {fg.lot_number}", {"requested": ser_value(rem), "available": ser_value(avail)})
        first_tx = None
        for n, b in enumerate(bals):
            if rem <= 0:
                break
            take = min(b.quantity - b.reserved_quantity, rem)
            if take <= 0:
                continue
            mv = inv.issue(db, ctx, b.id, take, "FG_SHIPMENT", require_available_quality=True,
                           idempotency_key=f"{idempotency_key}:{it.id}:{n}" if idempotency_key else None)
            first_tx = first_tx or mv.transaction.id
            rem -= take
        it.inventory_transaction_id = first_tx
        fg_refs[fg.trace_node_id] = fg_refs.get(fg.trace_node_id, ZERO) + it.quantity
        left = sum((b.quantity for b in db.execute(select(InventoryBalance).where(InventoryBalance.lot_node_id == fg.trace_node_id)).scalars()), ZERO)
        if left == 0:
            fg.status = "DEPLETED"
    c = db.get(Customer, sh.customer_id)
    snode = trace.create_node(db, cu.organization_id, sh.facility_id, "SHIPMENT", sh.id, sh.shipment_number, c.name)
    cnode = trace.create_node(db, cu.organization_id, None, "CUSTOMER", c.id, c.code, c.name)
    total = sum(fg_refs.values(), ZERO)
    trace.record_event(db, org_id=cu.organization_id, facility_id=sh.facility_id, user_id=cu.id, event_type="SHIPMENT",
                       inputs=[trace.NodeRef(k, v) for k, v in fg_refs.items()], outputs=[trace.NodeRef(snode.id, total)],
                       reference_type="SHIPMENT", reference_id=sh.id)
    trace.record_event(db, org_id=cu.organization_id, facility_id=sh.facility_id, user_id=cu.id, event_type="SHIPMENT",
                       inputs=[trace.NodeRef(snode.id, total)], outputs=[trace.NodeRef(cnode.id, total)],
                       reference_type="SHIPMENT", reference_id=sh.id, note="거래처 인도")
    sh.trace_node_id = snode.id
    sh.status = "CONFIRMED"
    sh.approved_by = cu.id
    sh.confirmed_at = datetime.now(timezone.utc)
    sh.transaction_group_id = ctx.group_id
    audit(db, cu, "SHIPMENT_CONFIRM", "SHIPMENT", sh.id, after={"number": sh.shipment_number, "total": str(total)})


def reverse_shipment(db: Session, cu: Any, sh: ShipmentHeader, reason: str) -> None:
    if sh.status != "CONFIRMED":
        raise InvalidState(sh.status, "출고 정정")
    if any(it.returned_quantity > 0 for it in sh.items):
        raise BusinessRule("HAS_RETURNS", "반품 이력이 있는 출고는 정정할 수 없습니다.")
    ctx = inv.Ctx(cu.organization_id, sh.facility_id, cu.id, reference_type="SHIPMENT_REVERSAL", reference_id=sh.id, reason=reason)
    for t in db.execute(select(InventoryTransaction).where(InventoryTransaction.reference_type == "SHIPMENT", InventoryTransaction.reference_id == sh.id,
                                                           InventoryTransaction.transaction_type == "FG_SHIPMENT")).scalars().all():
        inv.reverse_transaction(db, ctx, t.id, reason)
    for ev in db.execute(select(TraceEvent).where(TraceEvent.reference_type == "SHIPMENT", TraceEvent.reference_id == sh.id,
                                                  TraceEvent.event_type == "SHIPMENT")).scalars().all():
        trace.reverse_event(db, org_id=cu.organization_id, facility_id=sh.facility_id, user_id=cu.id, event_id=ev.id, note=reason)
    for it in sh.items:
        if it.fg_package_id:
            db.get(FinishedGoodsPackage, it.fg_package_id).status = "IN_STOCK"
        fg = db.get(FinishedGoodsLot, it.fg_lot_id)
        fg.status = "ACTIVE"
    sh.status = "REVERSED"
    audit(db, cu, "SHIPMENT_REVERSE", "SHIPMENT", sh.id, reason=reason)


def return_shipment(db: Session, cu: Any, sh: ShipmentHeader, item_id: uuid.UUID, qty: Decimal, reason: str) -> ShipmentReturn:
    if sh.status != "CONFIRMED":
        raise InvalidState(sh.status, "반품")
    it = next((x for x in sh.items if x.id == item_id), None)
    if not it:
        raise NotFound("출고 품목")
    if qty <= 0 or qty > it.quantity - it.returned_quantity:
        raise BusinessRule("RETURN_EXCEEDS_SHIPPED", "반품 수량이 출고 잔여 수량을 초과합니다.", {"returnable": ser_value(it.quantity - it.returned_quantity)})
    fg = db.get(FinishedGoodsLot, it.fg_lot_id)
    qwh = db.execute(select(Warehouse).where(Warehouse.organization_id == cu.organization_id, Warehouse.facility_id == sh.facility_id,
                                             Warehouse.warehouse_type == "QUARANTINE", Warehouse.is_active)).scalars().first()
    if not qwh:
        raise BusinessRule("WAREHOUSE_NOT_CONFIGURED", "격리 창고가 설정되지 않았습니다.")
    rt = ShipmentReturn(organization_id=cu.organization_id, facility_id=sh.facility_id, return_number=next_number(db, cu.organization_id, "RTN"),
                        shipment_id=sh.id, shipment_item_id=it.id, fg_lot_id=fg.id, quantity=qty, reason=reason,
                        quarantine_warehouse_id=qwh.id, status="QUARANTINED", created_by=cu.id)
    db.add(rt)
    db.flush()
    ctx = inv.Ctx(cu.organization_id, sh.facility_id, cu.id, reference_type="SHIPMENT_RETURN", reference_id=rt.id, reason=reason)
    inv.receive(db, ctx, inv.StockKey(item_type="FG", lot_node_id=fg.trace_node_id, package_node_id=None, warehouse_id=qwh.id,
                                      storage_location_id=None, unit_id=fg.base_unit_id, lot_number=fg.lot_number,
                                      quality_status=fg.quality_status, product_id=fg.product_id, expiry_date=fg.expiry_date), qty, "FG_RETURN")
    it.returned_quantity = it.returned_quantity + qty
    fg.status = "ACTIVE"
    audit(db, cu, "SHIPMENT_RETURN", "SHIPMENT", sh.id, after={"return": rt.return_number, "quantity": str(qty)}, reason=reason)
    return rt


def decide_return(db: Session, cu: Any, rt: ShipmentReturn, decision: str, reason: str) -> None:
    if rt.status != "QUARANTINED":
        raise InvalidState(rt.status, "반품 판정")
    fg = db.get(FinishedGoodsLot, rt.fg_lot_id)
    bal = db.execute(select(InventoryBalance).where(InventoryBalance.lot_node_id == fg.trace_node_id,
                                                    InventoryBalance.warehouse_id == rt.quarantine_warehouse_id)).scalars().first()
    if not bal or bal.quantity < rt.quantity:
        raise BusinessRule("INSUFFICIENT_STOCK", "격리 재고가 부족합니다.")
    ctx = inv.Ctx(cu.organization_id, rt.facility_id, cu.id, reference_type="SHIPMENT_RETURN", reference_id=rt.id, reason=reason)
    if decision == "RESTOCK":
        if fg.quality_status != "RELEASED":
            raise BusinessRule("QUALITY_NOT_RELEASED", "품질 승인 상태의 LOT 만 재입고할 수 있습니다.")
        if fg.expiry_date and fg.expiry_date < today_seoul():
            raise BusinessRule("EXPIRED_PRODUCT", "기한 경과 제품은 재입고할 수 없습니다. 폐기하세요.")
        inv.transfer(db, ctx, bal.id, rt.quantity, fg.warehouse_id, None, reason=reason, ignore_reservations=True)
        rt.status = "RESTOCKED"
    else:
        inv.issue(db, ctx, bal.id, rt.quantity, "DISPOSAL", ignore_reservations=True, reason=reason)
        rt.status = "DISPOSED"
    rt.decided_by = cu.id
    rt.decided_at = datetime.now(timezone.utc)
    audit(db, cu, "RETURN_DECIDE", "SHIPMENT_RETURN", rt.id, after={"decision": decision}, reason=reason)


def shipment_detail(db: Session, sh: ShipmentHeader) -> dict[str, Any]:
    c = db.get(Customer, sh.customer_id)
    items = []
    for it in sh.items:
        fg = db.get(FinishedGoodsLot, it.fg_lot_id)
        p = db.get(Product, it.product_id)
        items.append(ser(it, lot_number=fg.lot_number, product_code=p.product_code, product_name=p.product_name, unit=p.base_unit.code,
                         expiry_date=fg.expiry_date, quality_status=fg.quality_status,
                         package_number=db.get(FinishedGoodsPackage, it.fg_package_id).package_number if it.fg_package_id else None))
    rets = [ser(r) for r in db.execute(select(ShipmentReturn).where(ShipmentReturn.shipment_id == sh.id)).scalars()]
    return ser(sh, customer_code=c.code, customer_name=c.name, items=items, returns=rets,
               total_quantity=sum((i.quantity for i in sh.items), ZERO))


# ============================================================ recall

ENTITY_BY_NODE = {"RAW_MATERIAL_LOT": MaterialLot, "WEIGHING_BATCH": WeighingBatch, "PROCESS_WIP": WipLot, "FINISHED_GOODS_LOT": FinishedGoodsLot}


def create_recall(db: Session, cu: Any, body: dict[str, Any]) -> RecallCase:
    from app.services.traceability.report import resolve_node

    sources = [resolve_node(db, cu.organization_id, k) for k in body["lot_ids"]]
    rc = RecallCase(organization_id=cu.organization_id, facility_id=cu.facility_id, case_number=next_number(db, cu.organization_id, "RCL"),
                    title=body["title"], description=body.get("description"), severity=body.get("severity", "HIGH"),
                    source_node_ids=[str(s.id) for s in sources], status="OPEN", opened_by=cu.id, created_by=cu.id)
    db.add(rc)
    db.flush()
    seen: set[uuid.UUID] = set()
    fg_ids: set[uuid.UUID] = set()
    for s in sources:
        for row in trace.forward(db, cu.organization_id, s.id):
            nid = row["node_id"]
            if nid in seen or row["node_type"] not in ENTITY_BY_NODE:
                continue
            seen.add(nid)
            stock = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(InventoryBalance.lot_node_id == nid)).scalar_one()
            db.add(RecallCaseLot(recall_case_id=rc.id, trace_node_id=nid, node_type=row["node_type"], lot_number=row["lot_number"],
                                 stock_quantity=Decimal(stock)))
            if row["node_type"] == "FINISHED_GOODS_LOT":
                fg_ids.add(row["entity_id"])
    for fid in fg_ids:
        for si in db.execute(select(ShipmentItem).join(ShipmentHeader, ShipmentHeader.id == ShipmentItem.shipment_id)
                             .where(ShipmentItem.fg_lot_id == fid, ShipmentHeader.status == "CONFIRMED")).scalars():
            sh = db.get(ShipmentHeader, si.shipment_id)
            tgt = si.quantity - si.returned_quantity
            if tgt > 0:
                db.add(RecallCaseShipment(recall_case_id=rc.id, shipment_id=sh.id, shipment_item_id=si.id, customer_id=sh.customer_id,
                                          fg_lot_id=fid, shipped_quantity=si.quantity, target_quantity=tgt))
    db.flush()
    db.refresh(rc)
    audit(db, cu, "RECALL_CREATE", "RECALL", rc.id, after={"case": rc.case_number, "lots": len(rc.lots), "shipments": len(rc.shipments)})
    return rc


def _set_entity_quality(db: Session, cu: Any, node: TraceNode, status: str) -> str | None:
    model = ENTITY_BY_NODE.get(node.node_type)
    if not model:
        return None
    ent = db.get(model, node.entity_id)
    prev = ent.quality_status
    ent.quality_status = status
    inv.set_lot_quality(db, cu.organization_id, node.id, status)
    return prev


def hold_recall_stock(db: Session, cu: Any, rc: RecallCase) -> int:
    if rc.status == "CLOSED":
        raise InvalidState(rc.status, "재고 보류")
    n = 0
    for rl in rc.lots:
        if rl.hold_applied:
            continue
        node = db.get(TraceNode, rl.trace_node_id)
        prev = _set_entity_quality(db, cu, node, "HOLD")
        rl.previous_quality_status = prev
        rl.hold_applied = True
        n += 1
    rc.status = "HOLDING"
    audit(db, cu, "RECALL_HOLD", "RECALL", rc.id, after={"held_lots": n})
    return n


def record_recovery(db: Session, cu: Any, rc: RecallCase, rs_id: uuid.UUID, qty: Decimal) -> RecallCaseShipment:
    rs = next((x for x in rc.shipments if x.id == rs_id), None)
    if not rs:
        raise NotFound("회수 대상 출고")
    if qty < 0 or qty > rs.target_quantity:
        raise BusinessRule("RECOVERY_EXCEEDS_TARGET", "회수 수량이 회수 대상 수량을 초과합니다.")
    rs.recovered_quantity = qty
    rs.status = "RECOVERED" if qty == rs.target_quantity else ("PARTIAL" if qty > 0 else "PENDING")
    if rc.status in ("OPEN", "HOLDING"):
        rc.status = "IN_PROGRESS"
    audit(db, cu, "RECALL_RECOVERY", "RECALL", rc.id, after={"shipment_item": str(rs.shipment_item_id), "recovered": str(qty)})
    return rs


def close_recall(db: Session, cu: Any, rc: RecallCase, release_hold: bool, reason: str) -> None:
    if rc.status == "CLOSED":
        raise InvalidState(rc.status, "회수 종료")
    if release_hold:
        for rl in rc.lots:
            if rl.hold_applied and rl.previous_quality_status:
                _set_entity_quality(db, cu, db.get(TraceNode, rl.trace_node_id), rl.previous_quality_status)
                rl.hold_applied = False
    rc.status = "CLOSED"
    rc.closed_at = datetime.now(timezone.utc)
    audit(db, cu, "RECALL_CLOSE", "RECALL", rc.id, after={"release_hold": release_hold}, reason=reason)


def recall_detail(db: Session, rc: RecallCase) -> dict[str, Any]:
    tgt = sum((s.target_quantity for s in rc.shipments), ZERO)
    rec = sum((s.recovered_quantity for s in rc.shipments), ZERO)
    ships = []
    for s in rc.shipments:
        sh = db.get(ShipmentHeader, s.shipment_id)
        c = db.get(Customer, s.customer_id)
        fg = db.get(FinishedGoodsLot, s.fg_lot_id)
        ships.append(ser(s, shipment_number=sh.shipment_number, shipment_date=sh.shipment_date, customer_name=c.name, customer_phone=c.phone,
                         delivery_address=sh.delivery_address, lot_number=fg.lot_number))
    return ser(rc, lots=[ser(x) for x in rc.lots], shipments=ships, target_total=tgt, recovered_total=rec,
               progress_pct=(rec / tgt * 100).quantize(Decimal("0.1")) if tgt else None,
               customers=sorted({s["customer_name"] for s in ships}))
