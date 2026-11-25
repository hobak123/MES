"""제조지시: 생성(BOM/공정 스냅샷) → 승인(예약) → 진행 → 완료 → 마감(물질수지)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit, snapshot as audit_snap
from app.core.errors import BusinessRule, InvalidState, NotFound
from app.core.numbering import next_number
from app.models import (
    FinishedGoodsLot, InventoryBalance, InventoryReservation, Material, ProcessDefinition, ProcessExecution, Product,
    ProductionBatch, ProductionOrder, ProductionOrderItem, ProductionRouting, RoutingVersion, SystemSetting, Unit,
    WeighingBatch, WeighingRecord, WipLot,
)
from app.repositories.common import ser, ser_value
from app.services.inventory import service as inv
from app.services.production import bom as bom_svc
from app.services.qr.service import ensure_qr

ZERO = Decimal(0)
HOLDABLE = ("APPROVED", "WAITING", "WEIGHING", "WEIGHING_COMPLETED", "PRODUCING", "QUALITY_PENDING")


def get_order(db: Session, cu: Any, order_id: uuid.UUID, lock: bool = False) -> ProductionOrder:
    stmt = select(ProductionOrder).where(ProductionOrder.id == order_id, ProductionOrder.organization_id == cu.organization_id)
    if lock:
        stmt = stmt.with_for_update()
    o = db.execute(stmt).scalar_one_or_none()
    if not o:
        raise NotFound("제조지시")
    cu.require_facility(o.facility_id)
    return o


def approved_routing(db: Session, org_id: uuid.UUID, product_id: uuid.UUID) -> RoutingVersion | None:
    r = db.execute(select(ProductionRouting).where(ProductionRouting.organization_id == org_id, ProductionRouting.product_id == product_id,
                                                   ProductionRouting.is_active)).scalars().first()
    if not r:
        return None
    for v in sorted(r.versions, key=lambda x: -x.version_no):
        if v.status == "APPROVED":
            return v
    return None


def routing_snapshot(db: Session, rv: RoutingVersion) -> dict[str, Any]:
    steps = []
    for s in rv.steps:
        pd = db.get(ProcessDefinition, s.process_definition_id)
        steps.append({"routing_step_id": str(s.id), "sequence": s.sequence, "step_name": s.step_name, "is_required": s.is_required,
                      "process_definition_id": str(pd.id), "process_code": pd.code, "process_type": pd.process_type,
                      "is_ccp": pd.is_ccp, "approval_required": pd.approval_required, "output_type": s.output_type,
                      "parameter_specs": s.parameter_specs if s.parameter_specs is not None else pd.parameter_specs,
                      "equipment_id": str(s.equipment_id or pd.equipment_id) if (s.equipment_id or pd.equipment_id) else None})
    return {"routing_version_id": str(rv.id), "version_no": rv.version_no, "steps": steps}


def create(db: Session, cu: Any, data: dict[str, Any]) -> ProductionOrder:
    p = db.get(Product, data["product_id"])
    if not p or p.organization_id != cu.organization_id or not p.is_active:
        raise NotFound("제품")
    v = bom_svc.approved_version(db, cu.organization_id, p.id, data["production_date"])
    planned, target = bom_svc.derive_quantities(p, v, data.get("planned_quantity"), data.get("target_fg_quantity"))
    batch_count = int(data.get("batch_count") or 1)
    rv = None
    if data.get("routing_version_id"):
        rv = db.get(RoutingVersion, data["routing_version_id"])
        if not rv or rv.organization_id != cu.organization_id or rv.status != "APPROVED":
            raise BusinessRule("ROUTING_NOT_APPROVED", "승인된 공정 라우팅만 사용할 수 있습니다.")
    else:
        rv = approved_routing(db, cu.organization_id, p.id)
    snap = bom_svc.snapshot(db, v)
    o = ProductionOrder(
        organization_id=cu.organization_id, facility_id=cu.facility_id, order_number=next_number(db, cu.organization_id, "MO", data["production_date"]),
        production_date=data["production_date"], product_id=p.id, bom_version_id=v.id, bom_snapshot=snap,
        routing_version_id=rv.id if rv else None, routing_snapshot=routing_snapshot(db, rv) if rv else None,
        planned_quantity=planned, planned_unit_id=v.base_unit_id, target_fg_quantity=target, batch_count=batch_count,
        batch_size=(planned / batch_count).quantize(Decimal("0.000001")), line_code=data.get("line_code"),
        work_center=data.get("work_center"), assignee_id=data.get("assignee_id"), scheduled_start=data.get("scheduled_start"),
        scheduled_end=data.get("scheduled_end"), note=data.get("note"), status="DRAFT", created_by=cu.id,
    )
    db.add(o)
    db.flush()
    reqs = bom_svc.calculate(db, cu.organization_id, cu.facility_id, snap["items"], v.base_quantity, v.base_unit_id, planned, target,
                             with_stock=False)
    for it, r in zip(snap["items"], reqs):
        db.add(ProductionOrderItem(
            organization_id=cu.organization_id, production_order_id=o.id, bom_item_id=uuid.UUID(it["id"]), item_type=it["item_type"],
            material_id=uuid.UUID(it["material_id"]), quantity_basis=it["quantity_basis"],
            ratio_percent=Decimal(it["ratio_percent"]) if it.get("ratio_percent") else None, required_quantity=r["_req"],
            per_batch_quantity=(r["_req"] / batch_count).quantize(Decimal("0.000001")), unit_id=uuid.UUID(r["unit_id"]),
            tolerance_pct=Decimal(it.get("tolerance_pct") or "1"), sequence=it.get("sequence") or 10,
            weighing_group=it.get("weighing_group") or ("DEFAULT" if it["item_type"] == "FORMULA" else None),
            process_step_code=it.get("process_step_code"), substitute_allowed=bool(it.get("substitute_allowed")),
            substitute_material_ids=it.get("substitute_material_ids"),
        ))
    for n in range(1, batch_count + 1):
        db.add(ProductionBatch(organization_id=cu.organization_id, facility_id=cu.facility_id, production_order_id=o.id,
                               batch_number=next_number(db, cu.organization_id, "BATCH", data["production_date"]), batch_no=n,
                               planned_quantity=o.batch_size, created_by=cu.id))
    db.flush()
    o.qr_code_id = ensure_qr(db, org_id=cu.organization_id, qr_type="PRODUCTION_ORDER", entity_id=o.id, lot_number=o.order_number, user_id=cu.id).id
    db.flush()
    db.refresh(o)
    audit(db, cu, "PRODUCTION_ORDER_CREATE", "PRODUCTION_ORDER", o.id, after={"order_number": o.order_number, "bom_version": v.version_no})
    return o


def requirements(db: Session, cu: Any, o: ProductionOrder) -> list[dict[str, Any]]:
    out = []
    for it in o.items:
        m = db.get(Material, it.material_id)
        avail = inv.available_quantity(db, cu.organization_id, o.facility_id, material_id=m.id)
        reserved = db.execute(select(func.coalesce(func.sum(InventoryReservation.quantity - InventoryReservation.consumed_quantity), 0)).where(
            InventoryReservation.production_order_id == o.id, InventoryReservation.status == "ACTIVE",
            InventoryReservation.balance_id.in_(select(InventoryBalance.id).where(InventoryBalance.material_id == m.id)))).scalar_one()
        need = max(it.required_quantity - it.issued_quantity, ZERO)
        recs = []
        rem = max(need - Decimal(reserved), ZERO)
        for b in inv.available_balances(db, cu.organization_id, o.facility_id, material_id=m.id, fefo=m.expiry_tracking_required):
            if rem <= 0:
                break
            take = min(b.quantity - b.reserved_quantity, rem)
            recs.append({"balance_id": str(b.id), "lot_number": b.lot_number, "package_number": b.package_number,
                         "expiry_date": ser_value(b.expiry_date), "available": ser_value(b.quantity - b.reserved_quantity),
                         "suggested": ser_value(take)})
            rem -= take
        out.append(ser(it, material_code=m.material_code, material_name=m.material_name, unit=db.get(Unit, it.unit_id).code,
                       remaining_quantity=need, available_quantity=avail, reserved_quantity=Decimal(reserved),
                       shortage=max(need - Decimal(reserved) - avail, ZERO), is_short=(need - Decimal(reserved)) > avail,
                       method="FEFO" if m.expiry_tracking_required else "FIFO", recommendations=recs))
    return out


def approve(db: Session, cu: Any, o: ProductionOrder, require_full_stock: bool = False) -> dict[str, Any]:
    if o.status != "DRAFT":
        raise InvalidState(o.status, "제조지시 승인")
    ctx = inv.Ctx(cu.organization_id, o.facility_id, cu.id, reference_type="PRODUCTION_ORDER", reference_id=o.id)
    shortages = []
    plan: list[tuple[ProductionOrderItem, list[tuple[uuid.UUID, Decimal]]]] = []
    for it in o.items:
        m = db.get(Material, it.material_id)
        rem = it.required_quantity
        picks = []
        for b in inv.available_balances(db, cu.organization_id, o.facility_id, material_id=m.id, fefo=m.expiry_tracking_required):
            if rem <= 0:
                break
            take = min(b.quantity - b.reserved_quantity, rem)
            picks.append((b.id, take))
            rem -= take
        if rem > 0:
            shortages.append({"material_code": m.material_code, "material_name": m.material_name, "shortage": ser_value(rem)})
        plan.append((it, picks))
    if shortages and require_full_stock:
        raise BusinessRule("INSUFFICIENT_STOCK", "가용 재고가 부족하여 승인할 수 없습니다.", {"shortages": shortages})
    for it, picks in plan:
        for bid, qty in picks:
            inv.reserve(db, ctx, bid, qty, order_id=o.id, order_item_id=it.id, reason=f"제조지시 {o.order_number}")
    o.status = "APPROVED"
    o.approved_by = cu.id
    o.approved_at = datetime.now(timezone.utc)
    audit(db, cu, "PRODUCTION_ORDER_APPROVE", "PRODUCTION_ORDER", o.id, after={"shortages": shortages})
    return {"shortages": shortages}


def transition(db: Session, cu: Any, o: ProductionOrder, action: str, reason: str | None = None) -> None:
    before = o.status
    if action == "start":
        if o.status != "APPROVED":
            raise InvalidState(o.status, "작업 시작")
        o.status = "WAITING"
        o.actual_start = o.actual_start or datetime.now(timezone.utc)
    elif action == "hold":
        if o.status not in HOLDABLE:
            raise InvalidState(o.status, "보류")
        if not reason:
            raise BusinessRule("REASON_REQUIRED", "보류 사유를 입력하세요.")
        o.status_before_hold = o.status
        o.status = "HOLD"
    elif action == "release_hold":
        if o.status != "HOLD":
            raise InvalidState(o.status, "보류 해제")
        o.status = o.status_before_hold or "APPROVED"
        o.status_before_hold = None
    elif action == "complete":
        if o.status != "QUALITY_PENDING":
            raise InvalidState(o.status, "완료")
        fgs = db.execute(select(FinishedGoodsLot).where(FinishedGoodsLot.production_order_id == o.id)).scalars().all()
        if not fgs:
            raise BusinessRule("NO_FINISHED_GOODS", "완제품 LOT 이 없습니다.")
        pending = [f.lot_number for f in fgs if f.quality_status in ("QUALITY_PENDING", "HOLD")]
        if pending:
            raise BusinessRule("FG_QUALITY_PENDING", f"품질 판정이 끝나지 않은 완제품 LOT 이 있습니다: {', '.join(pending)}")
        o.status = "COMPLETED"
        o.actual_end = datetime.now(timezone.utc)
    elif action == "cancel":
        if o.status not in ("DRAFT", "APPROVED", "WAITING", "HOLD"):
            raise InvalidState(o.status, "취소")
        if not reason:
            raise BusinessRule("REASON_REQUIRED", "취소 사유를 입력하세요.")
        used = db.execute(select(func.count()).select_from(WeighingRecord).where(WeighingRecord.production_order_id == o.id,
                                                                                WeighingRecord.status == "CONFIRMED")).scalar_one()
        if used:
            raise BusinessRule("ORDER_HAS_ISSUES", "확정된 계량 이력이 있는 지시는 취소할 수 없습니다. 계량 정정 후 취소하세요.")
        inv.release_reservations(db, inv.Ctx(cu.organization_id, o.facility_id, cu.id), o.id)
        o.status = "CANCELLED"
        for b in o.batches:
            b.status = "CANCELLED"
    else:
        raise ValueError(action)
    audit(db, cu, f"PRODUCTION_ORDER_{action.upper()}", "PRODUCTION_ORDER", o.id, before={"status": before}, after={"status": o.status},
          reason=reason)


def mass_balance(db: Session, o: ProductionOrder, remaining_wip: Decimal | None = None, confirmed_loss: Decimal = ZERO) -> dict[str, Any]:
    """물질수지 = 총투입 + 수분변화 - 정상산출 - 불량 - 폐기 - 확인손실 - 남은 공정재고 (kg). 항목 중복 집계 없음."""
    total_input = ZERO
    for rec in db.execute(select(WeighingRecord).where(WeighingRecord.production_order_id == o.id, WeighingRecord.status == "CONFIRMED")).scalars():
        it = db.get(ProductionOrderItem, rec.production_order_item_id)
        if it.item_type == "FORMULA":
            total_input += rec.actual_quantity
    execs = db.execute(select(ProcessExecution).where(ProcessExecution.production_order_id == o.id, ProcessExecution.status == "COMPLETED")).scalars().all()
    defective = sum((e.defective_quantity for e in execs), ZERO)
    waste = sum((e.waste_quantity for e in execs), ZERO)
    loss = sum((e.process_loss_quantity for e in execs), ZERO) + confirmed_loss
    moisture = sum((e.moisture_change_quantity for e in execs), ZERO)
    fgs = db.execute(select(FinishedGoodsLot).where(FinishedGoodsLot.production_order_id == o.id)).scalars().all()
    good = sum(((f.net_weight or ZERO) for f in fgs), ZERO)
    if remaining_wip is None:
        node_ids = [w.trace_node_id for w in db.execute(select(WeighingBatch).where(WeighingBatch.production_order_id == o.id)).scalars() if w.trace_node_id]
        node_ids += [w.trace_node_id for w in db.execute(select(WipLot).where(WipLot.production_order_id == o.id)).scalars()]
        remaining_wip = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(
            InventoryBalance.lot_node_id.in_(node_ids))).scalar_one() if node_ids else ZERO
        remaining_wip = Decimal(remaining_wip)
    diff = total_input + moisture - good - defective - waste - loss - remaining_wip
    pct = (abs(diff) / total_input * 100) if total_input else None
    return {"total_input": ser_value(total_input), "moisture_change": ser_value(moisture), "good_output": ser_value(good),
            "defective": ser_value(defective), "waste": ser_value(waste), "confirmed_loss": ser_value(loss),
            "remaining_wip": ser_value(remaining_wip), "difference": ser_value(diff),
            "difference_pct": ser_value(pct.quantize(Decimal("0.01"))) if pct is not None else None,
            "needs_review": total_input == 0}


def tolerance_pct(db: Session, org_id: uuid.UUID) -> Decimal:
    s = db.execute(select(SystemSetting).where(SystemSetting.organization_id == org_id, SystemSetting.key == "production.mass_balance_tolerance_pct")).scalar_one_or_none()
    return Decimal(str(s.value["value"])) if s else Decimal("2")


def close(db: Session, cu: Any, o: ProductionOrder, remaining_wip: Decimal | None, confirmed_loss: Decimal, override_reason: str | None) -> dict:
    if o.status != "COMPLETED":
        raise InvalidState(o.status, "마감")
    mb = mass_balance(db, o, remaining_wip, confirmed_loss)
    tol = tolerance_pct(db, cu.organization_id)
    over = mb["difference_pct"] is not None and Decimal(mb["difference_pct"]) > tol
    if over or mb["needs_review"]:
        if not override_reason:
            raise BusinessRule("MASS_BALANCE_EXCEEDED", f"물질수지 차이가 허용치({tol}%)를 초과합니다. 관리자 확인 사유를 입력하세요.", mb)
        if not cu.has("production:close") or not (cu.has("approval:decide") or cu.is_super):
            raise BusinessRule("MANAGER_CONFIRM_REQUIRED", "물질수지 초과 마감은 관리자 확인이 필요합니다.")
    mb.update({"tolerance_pct": ser_value(tol), "override_reason": override_reason, "confirmed_by": str(cu.id)})
    o.mass_balance = mb
    released = inv.release_reservations(db, inv.Ctx(cu.organization_id, o.facility_id, cu.id), o.id)
    o.status = "CLOSED"
    o.closed_by = cu.id
    o.closed_at = datetime.now(timezone.utc)
    audit(db, cu, "PRODUCTION_ORDER_CLOSE", "PRODUCTION_ORDER", o.id, after=mb, reason=override_reason)
    return {**mb, "released_reservations": ser_value(released)}


def yield_report(db: Session, o: ProductionOrder) -> dict[str, Any]:
    fgs = db.execute(select(FinishedGoodsLot).where(FinishedGoodsLot.production_order_id == o.id)).scalars().all()
    mb = mass_balance(db, o)
    total_input = Decimal(mb["total_input"])
    net = sum(((f.net_weight or ZERO) for f in fgs), ZERO)
    good = sum((f.good_quantity for f in fgs), ZERO)
    defective = sum((f.defective_quantity for f in fgs), ZERO)
    produced = sum((f.total_produced_quantity for f in fgs), ZERO)

    def pct(a: Decimal, b: Decimal | None) -> str | None:
        return ser_value((a / b * 100).quantize(Decimal("0.01"))) if b else None

    execs = []
    for e in db.execute(select(ProcessExecution).where(ProcessExecution.production_order_id == o.id).order_by(ProcessExecution.routing_step_sequence)).scalars():
        pd = db.get(ProcessDefinition, e.process_definition_id)
        execs.append({"execution_number": e.execution_number, "process": pd.name, "status": e.status,
                      "input": ser_value(e.total_input_quantity), "good_output": ser_value(e.good_output_quantity),
                      "loss": ser_value(e.process_loss_quantity), "process_yield": ser_value(e.process_yield)})
    return {
        "production_yield_pct": pct(net, total_input), "production_yield_needs_review": total_input == 0,
        "plan_achievement_pct": pct(good, o.target_fg_quantity), "defect_rate_pct": pct(defective, produced),
        "net_weight": ser_value(net), "total_input": ser_value(total_input), "good_quantity": ser_value(good),
        "target_quantity": ser_value(o.target_fg_quantity), "processes": execs, "mass_balance": mb,
    }


def detail(db: Session, cu: Any, o: ProductionOrder) -> dict[str, Any]:
    p = db.get(Product, o.product_id)
    items = requirements(db, cu, o)
    wbs = [ser(w) for w in db.execute(select(WeighingBatch).where(WeighingBatch.production_order_id == o.id).order_by(WeighingBatch.created_at)).scalars()]
    execs = [ser(e) for e in db.execute(select(ProcessExecution).where(ProcessExecution.production_order_id == o.id).order_by(ProcessExecution.created_at)).scalars()]
    fgs = [ser(f) for f in db.execute(select(FinishedGoodsLot).where(FinishedGoodsLot.production_order_id == o.id)).scalars()]
    formula = [i for i in o.items if i.item_type == "FORMULA"]
    req = sum((i.required_quantity for i in formula), ZERO)
    issued = sum((i.issued_quantity for i in formula), ZERO)
    steps = (o.routing_snapshot or {}).get("steps", [])
    done_steps = {e["routing_step_sequence"] for e in execs if e["status"] == "COMPLETED"}
    progress = {
        "weighing_pct": ser_value((issued / req * 100).quantize(Decimal("0.1"))) if req else None,
        "process_pct": ser_value(Decimal(len([s for s in steps if s["sequence"] in done_steps]) * 100 / len(steps)).quantize(Decimal("0.1"))) if steps else None,
    }
    return ser(o, product_code=p.product_code, product_name=p.product_name, unit=db.get(Unit, o.planned_unit_id).code,
               items=items, batches=[ser(b) for b in o.batches], weighing_batches=wbs, process_executions=execs,
               finished_goods=fgs, progress=progress)
