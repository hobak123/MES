"""QR 기반 계량: 검증 → 확정(차감/기록/추적) → WB 완료(LOT/QR/재고) → 정정(승인 역거래)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import BusinessRule, Forbidden, InvalidState, NotFound
from app.core.numbering import next_number, today_seoul
from app.models import (
    ApprovalRequest, InventoryBalance, Material, MaterialLot, MaterialPackage, ProductionBatch, ProductionOrder,
    ProductionOrderItem, TraceEvent, Unit, User, Warehouse, WeighingBatch, WeighingRecord,
)
from app.repositories.common import ser, ser_value
from app.services.inventory import service as inv
from app.services.inventory.units import convert
from app.services.qr.resolver import find_qr
from app.services.qr.service import ensure_qr, parse_scan
from app.services.traceability import service as trace

ZERO = Decimal(0)
WEIGHABLE = ("WAITING", "WEIGHING")


class Check:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.warnings: list[str] = []

    def fail(self, code: str, msg: str) -> None:
        self.errors.append({"error_code": code, "message": msg})


def _order(db: Session, cu: Any, order_id: uuid.UUID, lock: bool = False) -> ProductionOrder:
    stmt = select(ProductionOrder).where(ProductionOrder.id == order_id, ProductionOrder.organization_id == cu.organization_id)
    if lock:
        stmt = stmt.with_for_update()
    o = db.execute(stmt).scalar_one_or_none()
    if not o:
        raise NotFound("제조지시")
    cu.require_facility(o.facility_id)
    return o


def _cumulative(db: Session, batch_id: uuid.UUID, item_id: uuid.UUID) -> Decimal:
    return Decimal(db.execute(select(func.coalesce(func.sum(WeighingRecord.actual_quantity), 0)).where(
        WeighingRecord.production_batch_id == batch_id, WeighingRecord.production_order_item_id == item_id,
        WeighingRecord.status == "CONFIRMED")).scalar_one())


def _package_balance(db: Session, org_id: uuid.UUID, pkg: MaterialPackage, facility_id: uuid.UUID | None) -> InventoryBalance | None:
    stmt = select(InventoryBalance).where(InventoryBalance.organization_id == org_id, InventoryBalance.package_node_id == pkg.trace_node_id,
                                          InventoryBalance.quantity > 0)
    if facility_id:
        stmt = stmt.where(InventoryBalance.facility_id == facility_id)
    return db.execute(stmt.order_by(InventoryBalance.quantity.desc())).scalars().first()


def evaluate(db: Session, cu: Any, order_id: uuid.UUID, batch_id: uuid.UUID, raw: str | None = None,
             package_id: uuid.UUID | None = None, actual: Decimal | None = None) -> tuple[Check, dict[str, Any]]:
    """모든 차단 조건을 검사하고 화면 표시 정보를 반환 (재고 변경 없음)."""
    chk = Check()
    info: dict[str, Any] = {}
    o = _order(db, cu, order_id)
    info.update({"production_order_id": str(o.id), "order_number": o.order_number, "order_status": o.status})
    if o.status not in WEIGHABLE:
        chk.fail("ORDER_NOT_WEIGHABLE", f"계량할 수 없는 지시 상태입니다: {o.status}")
    batch = db.get(ProductionBatch, batch_id)
    if not batch or batch.production_order_id != o.id:
        chk.fail("BATCH_MISMATCH", "지시에 속하지 않은 배치입니다.")
        return chk, info
    if batch.status in ("COMPLETED", "CANCELLED"):
        chk.fail("BATCH_CLOSED", "완료/취소된 배치입니다.")
    info.update({"production_batch_id": str(batch.id), "batch_number": batch.batch_number})
    pkg: MaterialPackage | None = None
    if package_id:
        pkg = db.get(MaterialPackage, package_id)
        if not pkg or pkg.organization_id != cu.organization_id:
            pkg = None
    elif raw:
        if parse_scan(raw)["kind"] == "invalid":
            chk.fail("INVALID_QR", "인식할 수 없는 QR 입니다.")
            return chk, info
        qr = find_qr(db, cu.organization_id, raw)
        if qr and qr.qr_type == "RAW_MATERIAL_PACKAGE":
            pkg = db.get(MaterialPackage, qr.entity_id)
        elif qr:
            chk.fail("INVALID_QR", "원료 포장 QR 을 스캔하세요. (스캔한 QR: " + qr.qr_type + ")")
            return chk, info
    if not pkg:
        if not chk.errors:
            chk.fail("INVALID_QR", "등록되지 않은 QR 입니다. 다시 스캔하거나 포장번호로 검색하세요.")
        return chk, info
    lot = db.get(MaterialLot, pkg.material_lot_id)
    m = db.get(Material, lot.material_id)
    info.update({"material_id": str(m.id), "material_code": m.material_code, "material_name": m.material_name,
                 "material_lot_id": str(lot.id), "lot_number": lot.lot_number, "package_id": str(pkg.id),
                 "package_number": pkg.package_number, "expiry_date": ser_value(lot.expiry_date), "quality_status": lot.quality_status,
                 "package_status": pkg.package_status, "unit": db.get(Unit, pkg.base_unit_id).code})
    # 지시 품목 매칭 (대체 원료 포함)
    item: ProductionOrderItem | None = None
    is_sub = False
    weighables = [i for i in o.items if i.item_type in ("FORMULA", "PROCESS_AUXILIARY")]
    for it in weighables:
        if it.material_id == m.id:
            item = it
            break
    if item is None:
        for it in weighables:
            subs = [str(x) for x in (it.substitute_material_ids or [])]
            if str(m.id) in subs:
                if not it.substitute_allowed:
                    chk.fail("SUBSTITUTE_NOT_ALLOWED", f"승인되지 않은 대체 원료입니다: {m.material_name}")
                else:
                    item, is_sub = it, True
                    chk.warnings.append("승인된 대체 원료로 계량합니다.")
                break
    if item is None and not any(e["error_code"] == "SUBSTITUTE_NOT_ALLOWED" for e in chk.errors):
        chk.fail("MATERIAL_NOT_IN_ORDER", f"제조지시에 없는 원료입니다: {m.material_name} ({m.material_code})")
    if lot.quality_status != "PASSED":
        chk.fail("QUALITY_NOT_RELEASED", f"품질 미승인/보류/부적합 원료입니다 ({lot.quality_status}).")
    if lot.status != "ACTIVE" or pkg.package_status in ("EMPTY", "DAMAGED", "CONTAMINATED", "CANCELLED"):
        chk.fail("PACKAGE_NOT_USABLE", f"사용할 수 없는 포장입니다 ({pkg.package_status}).")
    if lot.expiry_date and lot.expiry_date < today_seoul():
        chk.fail("EXPIRED_MATERIAL", f"소비/유통기한이 경과한 원료입니다 ({lot.expiry_date}).")
    bal = _package_balance(db, cu.organization_id, pkg, o.facility_id)
    current = bal.quantity if bal else ZERO
    info["current_quantity"] = ser_value(current)
    info["balance_id"] = str(bal.id) if bal else None
    if item is not None:
        if item.unit_id != pkg.base_unit_id:
            chk.fail("UNIT_MISMATCH", "지시 품목 단위와 포장 단위가 다릅니다.")
        target = item.per_batch_quantity
        cum = _cumulative(db, batch.id, item.id)
        tol = item.tolerance_pct / 100
        tmin, tmax = target * (1 - tol), target * (1 + tol)
        remaining = max(target - cum, ZERO)
        info.update({"production_order_item_id": str(item.id), "is_substitute": is_sub, "weighing_group": item.weighing_group,
                     "target_quantity": ser_value(target), "cumulative_quantity": ser_value(cum),
                     "remaining_quantity": ser_value(remaining), "tolerance_pct": ser_value(item.tolerance_pct),
                     "tolerance_min": ser_value(tmin), "tolerance_max": ser_value(tmax),
                     "expected_package_remaining": ser_value(max(current - min(remaining, current), ZERO))})
        if remaining <= 0 and cum >= tmin:
            chk.warnings.append("이미 목표량 계량이 완료된 원료입니다.")
        if actual is not None:
            if actual <= 0:
                chk.fail("INVALID_QUANTITY", "계량값은 0보다 커야 합니다.")
            if bal is None or actual > current:
                chk.fail("INSUFFICIENT_STOCK", f"포장 잔량({ser_value(current)})이 계량값보다 적습니다.")
            new_cum = cum + actual
            info["error_quantity"] = ser_value(new_cum - target)
            info["expected_package_remaining"] = ser_value(max(current - actual, ZERO))
            if new_cum > tmax:
                info["tolerance_exceeded"] = True
    if bal is None and not any(e["error_code"] == "PACKAGE_NOT_USABLE" for e in chk.errors):
        chk.fail("INSUFFICIENT_STOCK", "포장 잔량이 없습니다.")
    info["valid"] = not chk.errors
    info["errors"] = chk.errors
    info["warnings"] = chk.warnings
    return chk, info


def _wip_warehouse(db: Session, org_id: uuid.UUID, facility_id: uuid.UUID | None, wtype: str = "WIP") -> Warehouse:
    wh = db.execute(select(Warehouse).where(Warehouse.organization_id == org_id, Warehouse.facility_id == facility_id,
                                            Warehouse.warehouse_type == wtype, Warehouse.is_active)).scalars().first()
    if not wh:
        raise BusinessRule("WAREHOUSE_NOT_CONFIGURED", f"{wtype} 유형 창고가 설정되지 않았습니다.")
    return wh


def record(db: Session, cu: Any, body: dict[str, Any], idempotency_key: str | None = None) -> WeighingRecord:
    o = _order(db, cu, body["production_order_id"], lock=True)  # 지시 단위 직렬화
    actual = Decimal(str(body["actual_quantity"]))
    if body.get("gross_weight") is not None and body.get("tare_weight") is not None:
        net = Decimal(str(body["gross_weight"])) - Decimal(str(body["tare_weight"]))
        if abs(net - actual) > Decimal("0.0005"):
            raise BusinessRule("NET_WEIGHT_MISMATCH", "총중량 - 용기중량이 순중량(계량값)과 다릅니다.")
    if body.get("is_stable") is False:
        raise BusinessRule("SCALE_NOT_STABLE", "저울이 안정화되지 않았습니다. 안정 후 확정하세요.")
    chk, info = evaluate(db, cu, o.id, body["production_batch_id"], body.get("raw"), body.get("package_id"), actual)
    if chk.errors:
        e = chk.errors[0]
        raise BusinessRule(e["error_code"], e["message"], {"errors": chk.errors, "info": info})
    approver_id = None
    if info.get("tolerance_exceeded"):
        approver_id = body.get("override_approved_by")
        if not approver_id:
            raise BusinessRule("TOLERANCE_EXCEEDED", f"허용오차 초과입니다. (목표 {info['target_quantity']}, 최대 {info['tolerance_max']}) 승인권자 승인이 필요합니다.", info)
        from app.core.deps import load_permissions

        approver = db.get(User, uuid.UUID(str(approver_id)))
        perms = load_permissions(db, approver.id) if approver else set()
        if not approver or approver.organization_id != cu.organization_id or (
                "weighing:reverse_approve" not in perms and "SUPER_ADMIN" not in approver.role_codes):
            raise Forbidden("허용오차 초과 승인 권한이 없는 사용자입니다.")
    item = db.get(ProductionOrderItem, uuid.UUID(info["production_order_item_id"]))
    pkg = db.get(MaterialPackage, uuid.UUID(info["package_id"]))
    lot = db.get(MaterialLot, pkg.material_lot_id)
    group = item.weighing_group or "DEFAULT"
    wb = db.execute(select(WeighingBatch).where(WeighingBatch.production_batch_id == body["production_batch_id"],
                                                WeighingBatch.weighing_group == group, WeighingBatch.status == "WEIGHING",
                                                WeighingBatch.parent_id.is_(None))).scalar_one_or_none()
    if wb is None:
        wb = WeighingBatch(id=uuid.uuid4(), organization_id=cu.organization_id, facility_id=o.facility_id,
                           lot_number=next_number(db, cu.organization_id, "WB"), production_order_id=o.id,
                           production_batch_id=body["production_batch_id"], product_id=o.product_id, bom_version_id=o.bom_version_id,
                           weighing_group=group, unit_id=o.planned_unit_id, status="WEIGHING", created_by=cu.id)
        db.add(wb)
        db.flush()
        node = trace.create_node(db, cu.organization_id, o.facility_id, "WEIGHING_BATCH", wb.id, wb.lot_number, "계량반제품")
        wb.trace_node_id = node.id
    ctx = inv.Ctx(cu.organization_id, o.facility_id, cu.id, reference_type="WEIGHING", reference_id=wb.id,
                  reason=f"계량 {o.order_number}")
    mv = inv.issue(db, ctx, uuid.UUID(info["balance_id"]), actual, "PRODUCTION_ISSUE", allow_reserved_for_order=o.id,
                   require_available_quality=True, material_lot_id=lot.id, package_id=pkg.id, idempotency_key=idempotency_key)
    remaining_pkg = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(
        InventoryBalance.package_node_id == pkg.trace_node_id)).scalar_one()
    pkg.package_status = "EMPTY" if remaining_pkg == 0 else "IN_USE"
    tol = item.tolerance_pct / 100
    rec = WeighingRecord(
        organization_id=cu.organization_id, facility_id=o.facility_id, weighing_batch_id=wb.id, production_order_id=o.id,
        production_batch_id=body["production_batch_id"], production_order_item_id=item.id, material_id=lot.material_id,
        material_lot_id=lot.id, material_package_id=pkg.id, is_substitute=bool(info.get("is_substitute")),
        target_quantity=item.per_batch_quantity, actual_quantity=actual, unit_id=item.unit_id,
        tolerance_min=item.per_batch_quantity * (1 - tol), tolerance_max=item.per_batch_quantity * (1 + tol),
        gross_weight=body.get("gross_weight"), tare_weight=body.get("tare_weight"), is_stable=body.get("is_stable"),
        scale_equipment_id=body.get("scale_equipment_id"), input_method="SCALE" if body.get("scale_equipment_id") else "MANUAL",
        inventory_transaction_id=mv.transaction.id, weighed_by=cu.id, weighed_at=datetime.now(timezone.utc),
        approval_status="APPROVED" if approver_id else "NOT_REQUIRED", approved_by=uuid.UUID(str(approver_id)) if approver_id else None,
        status="CONFIRMED", created_by=cu.id,
    )
    db.add(rec)
    db.flush()
    trace.record_event(db, org_id=cu.organization_id, facility_id=o.facility_id, user_id=cu.id, event_type="WEIGHING",
                       inputs=[trace.NodeRef(pkg.trace_node_id, actual, item.unit_id)],
                       outputs=[trace.NodeRef(wb.trace_node_id, convert(db, actual, item.unit_id, o.planned_unit_id,
                                                                        material=db.get(Material, lot.material_id)), o.planned_unit_id)],
                       production_order_id=o.id, production_batch_id=rec.production_batch_id, reference_type="WEIGHING_RECORD",
                       reference_id=rec.id)
    item.issued_quantity = item.issued_quantity + actual
    wb.total_quantity = wb.total_quantity + convert(db, actual, item.unit_id, o.planned_unit_id, material=db.get(Material, lot.material_id))
    if o.status == "WAITING":
        o.status = "WEIGHING"
    batch = db.get(ProductionBatch, rec.production_batch_id)
    if batch.status == "WAITING":
        batch.status = "WEIGHING"
        batch.started_at = datetime.now(timezone.utc)
    audit(db, cu, "WEIGHING_CONFIRM", "WEIGHING_RECORD", rec.id,
          after={"order": o.order_number, "lot": lot.lot_number, "package": pkg.package_number, "actual": str(actual)})
    return rec


def complete_wb(db: Session, cu: Any, wb_id: uuid.UUID, usable_hours: int | None = None) -> WeighingBatch:
    wb = db.execute(select(WeighingBatch).where(WeighingBatch.id == wb_id, WeighingBatch.organization_id == cu.organization_id)
                    .with_for_update()).scalar_one_or_none()
    if not wb:
        raise NotFound("계량 반제품")
    cu.require_facility(wb.facility_id)
    if wb.status != "WEIGHING":
        raise InvalidState(wb.status, "계량 완료")
    o = _order(db, cu, wb.production_order_id, lock=True)
    issues = []
    total_target = ZERO
    total_actual = ZERO
    max_tol = ZERO
    any_approved = False
    for it in o.items:
        if it.item_type not in ("FORMULA", "PROCESS_AUXILIARY") or (it.weighing_group or "DEFAULT") != wb.weighing_group:
            continue
        cum = _cumulative(db, wb.production_batch_id, it.id)
        tol = it.tolerance_pct / 100
        tmin, tmax = it.per_batch_quantity * (1 - tol), it.per_batch_quantity * (1 + tol)
        total_target += it.per_batch_quantity
        total_actual += cum
        max_tol = max(max_tol, tol)
        m = db.get(Material, it.material_id)
        approved_over = db.execute(select(func.count()).select_from(WeighingRecord).where(
            WeighingRecord.production_batch_id == wb.production_batch_id, WeighingRecord.production_order_item_id == it.id,
            WeighingRecord.status == "CONFIRMED", WeighingRecord.approval_status == "APPROVED")).scalar_one()
        any_approved = any_approved or bool(approved_over)
        if cum < tmin:
            issues.append({"material_code": m.material_code, "material_name": m.material_name, "target": ser_value(it.per_batch_quantity),
                           "actual": ser_value(cum), "problem": "UNDER"})
        elif cum > tmax and not approved_over:
            issues.append({"material_code": m.material_code, "material_name": m.material_name, "target": ser_value(it.per_batch_quantity),
                           "actual": ser_value(cum), "problem": "OVER"})
    if issues:
        raise BusinessRule("WEIGHING_INCOMPLETE", "목표량 허용오차 범위를 충족하지 않은 원료가 있습니다.", {"items": issues})
    # 그룹 누적 오차
    if total_target and not any_approved and abs(total_actual - total_target) > total_target * max_tol:
        raise BusinessRule("TOLERANCE_EXCEEDED", "계량 그룹 누적 오차가 허용 범위를 초과합니다.",
                           {"target": ser_value(total_target), "actual": ser_value(total_actual)})
    wh = _wip_warehouse(db, cu.organization_id, o.facility_id)
    now = datetime.now(timezone.utc)
    wb.status = "READY"
    wb.completed_by = cu.id
    wb.completed_at = now
    wb.warehouse_id = wh.id
    wb.usable_until = now + timedelta(hours=usable_hours) if usable_hours else None
    steps = (o.routing_snapshot or {}).get("steps", [])
    first = next((s for s in steps if s["process_type"] != "WEIGHING"), None)
    wb.next_process_code = first["process_code"] if first else None
    wb.qr_code_id = ensure_qr(db, org_id=cu.organization_id, qr_type="WEIGHING_BATCH", entity_id=wb.id, lot_number=wb.lot_number, user_id=cu.id).id
    ctx = inv.Ctx(cu.organization_id, o.facility_id, cu.id, reference_type="WEIGHING_BATCH", reference_id=wb.id, reason="계량 반제품 생성")
    inv.receive(db, ctx, inv.StockKey(item_type="WB", lot_node_id=wb.trace_node_id, package_node_id=None, warehouse_id=wh.id,
                                      storage_location_id=None, unit_id=wb.unit_id, lot_number=wb.lot_number, quality_status="PASSED",
                                      product_id=o.product_id), wb.total_quantity, "WIP_RECEIPT")
    # 모든 배치·그룹 계량 완료 시 지시 상태 전환
    groups = {(i.weighing_group or "DEFAULT") for i in o.items if i.item_type in ("FORMULA", "PROCESS_AUXILIARY")}
    batch = db.get(ProductionBatch, wb.production_batch_id)
    done_groups = {w.weighing_group for w in db.execute(select(WeighingBatch).where(
        WeighingBatch.production_batch_id == batch.id, WeighingBatch.status.in_(("READY", "PARTIALLY_CONSUMED", "CONSUMED")))).scalars()}
    if groups <= done_groups:
        batch.status = "WEIGHED"
    if all(b.status in ("WEIGHED", "PRODUCING", "COMPLETED", "CANCELLED") for b in o.batches) and o.status == "WEIGHING":
        o.status = "WEIGHING_COMPLETED"
    audit(db, cu, "WB_COMPLETE", "WEIGHING_BATCH", wb.id, after={"lot": wb.lot_number, "total": str(wb.total_quantity)})
    return wb


def reverse_record(db: Session, cu: Any, rec_id: uuid.UUID, reason: str, contaminated: bool = False,
                   via_approval: ApprovalRequest | None = None) -> dict[str, Any]:
    rec = db.execute(select(WeighingRecord).where(WeighingRecord.id == rec_id, WeighingRecord.organization_id == cu.organization_id)
                     .with_for_update()).scalar_one_or_none()
    if not rec:
        raise NotFound("계량 기록")
    cu.require_facility(rec.facility_id)
    if rec.status != "CONFIRMED":
        raise InvalidState(rec.status, "계량 정정")
    if not reason:
        raise BusinessRule("REASON_REQUIRED", "정정 사유를 입력하세요.")
    wb = db.get(WeighingBatch, rec.weighing_batch_id)
    if wb.status != "WEIGHING":
        raise BusinessRule("DOWNSTREAM_EXISTS", f"이미 완료/투입된 계량 반제품({wb.lot_number})에 포함된 계량은 직접 취소할 수 없습니다. 재작업/조정으로 처리하세요.")
    if via_approval is None and not cu.has("weighing:reverse_approve"):
        existing = db.execute(select(ApprovalRequest).where(ApprovalRequest.target_id == rec.id, ApprovalRequest.status == "PENDING")).scalars().first()
        if existing:
            return {"status": "PENDING_APPROVAL", "approval_request_id": str(existing.id)}
        ar = ApprovalRequest(organization_id=cu.organization_id, facility_id=rec.facility_id, request_type="WEIGHING_REVERSAL",
                             target_type="WEIGHING_RECORD", target_id=rec.id, reason=reason, requested_by=cu.id,
                             payload={"contaminated": contaminated, "actual_quantity": str(rec.actual_quantity)})
        db.add(ar)
        db.flush()
        audit(db, cu, "WEIGHING_REVERSAL_REQUEST", "WEIGHING_RECORD", rec.id, reason=reason)
        return {"status": "PENDING_APPROVAL", "approval_request_id": str(ar.id)}
    o = db.get(ProductionOrder, rec.production_order_id)
    ctx = inv.Ctx(cu.organization_id, rec.facility_id, cu.id, reference_type="WEIGHING_REVERSAL", reference_id=rec.id, reason=reason)
    rev = inv.reverse_transaction(db, ctx, rec.inventory_transaction_id, reason)
    disposal_tx = None
    if contaminated:
        d = inv.issue(db, ctx, rev.balance.id, rec.actual_quantity, "DISPOSAL", ignore_reservations=True,
                      material_lot_id=rec.material_lot_id, package_id=rec.material_package_id, reason=f"오염/혼합 원료 폐기: {reason}")
        disposal_tx = str(d.transaction.id)
    pkg = db.get(MaterialPackage, rec.material_package_id)
    rem = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(InventoryBalance.package_node_id == pkg.trace_node_id)).scalar_one()
    if pkg.package_status in ("EMPTY", "IN_USE", "AVAILABLE"):
        pkg.package_status = "EMPTY" if rem == 0 else ("IN_USE" if rem < pkg.original_quantity else "AVAILABLE")
    ev = db.execute(select(TraceEvent).where(TraceEvent.reference_type == "WEIGHING_RECORD", TraceEvent.reference_id == rec.id,
                                             TraceEvent.event_type == "WEIGHING")).scalar_one_or_none()
    if ev:
        trace.reverse_event(db, org_id=cu.organization_id, facility_id=rec.facility_id, user_id=cu.id, event_id=ev.id, note=reason)
    item = db.get(ProductionOrderItem, rec.production_order_item_id)
    item.issued_quantity = item.issued_quantity - rec.actual_quantity
    wb.total_quantity = wb.total_quantity - convert(db, rec.actual_quantity, rec.unit_id, o.planned_unit_id, material=db.get(Material, rec.material_id))
    rec.status = "REVERSED"
    rec.reversed_by = (via_approval.decided_by if via_approval else cu.id)
    rec.reversed_at = datetime.now(timezone.utc)
    rec.reversal_reason = reason + (" [오염 폐기]" if contaminated else "")
    if via_approval:
        via_approval.status = "EXECUTED"
    audit(db, cu, "WEIGHING_REVERSE", "WEIGHING_RECORD", rec.id, before={"status": "CONFIRMED"}, after={"status": "REVERSED",
          "reversal_tx": str(rev.transaction.id), "disposal_tx": disposal_tx}, reason=reason)
    return {"status": "REVERSED", "reversal_transaction_id": str(rev.transaction.id), "disposal_transaction_id": disposal_tx}


def wb_detail(db: Session, wb: WeighingBatch) -> dict[str, Any]:
    recs = []
    for r in db.execute(select(WeighingRecord).where(WeighingRecord.weighing_batch_id == wb.id).order_by(WeighingRecord.weighed_at)).scalars():
        m = db.get(Material, r.material_id)
        lot = db.get(MaterialLot, r.material_lot_id)
        pkg = db.get(MaterialPackage, r.material_package_id)
        u = db.get(User, r.weighed_by)
        recs.append(ser(r, material_code=m.material_code, material_name=m.material_name, lot_number=lot.lot_number,
                        package_number=pkg.package_number, weighed_by_name=u.full_name if u else None))
    q = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(InventoryBalance.lot_node_id == wb.trace_node_id)).scalar_one() if wb.trace_node_id else 0
    o = db.get(ProductionOrder, wb.production_order_id)
    return ser(wb, order_number=o.order_number, current_quantity=Decimal(q), records=recs,
               unit=db.get(Unit, wb.unit_id).code)
