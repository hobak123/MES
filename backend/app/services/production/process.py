"""공정 실행: 생성 → 시작 → 투입(WB/WIP QR) → 조건 → 포장재 → 완료(WIP/FG LOT·QR·재고·추적) / 보류 / 승인.

분할(출력 복수), 병합(다른 배치 입력: 승인 필요), 재작업(is_rework) 지원.
"""
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
    FinishedGoodsLot, InventoryBalance, Material, MaterialLot, MaterialPackage, PackagingConsumption, ProcessDefinition,
    ProcessExecution, ProcessInput, ProcessOutput, ProcessParameterRecord, Product, ProductionBatch, ProductionOrder,
    ProductionOrderItem, TraceNode, Unit, User, Warehouse, WeighingBatch, WeighingRecord, WipLot,
)
from app.repositories.common import ser, ser_value
from app.services.inventory import service as inv
from app.services.inventory.units import convert
from app.services.qr.resolver import find_qr
from app.services.qr.service import ensure_qr
from app.services.traceability import service as trace

ZERO = Decimal(0)
EPS = Decimal("0.001")
RUNNABLE_ORDER = ("WEIGHING", "WEIGHING_COMPLETED", "PRODUCING", "QUALITY_PENDING")


def get_exec(db: Session, cu: Any, exec_id: uuid.UUID, lock: bool = False) -> ProcessExecution:
    stmt = select(ProcessExecution).where(ProcessExecution.id == exec_id, ProcessExecution.organization_id == cu.organization_id)
    if lock:
        stmt = stmt.with_for_update()
    e = db.execute(stmt).scalar_one_or_none()
    if not e:
        raise NotFound("공정 실행")
    cu.require_facility(e.facility_id)
    return e


def _step(o: ProductionOrder, seq: int | None) -> dict[str, Any] | None:
    for s in (o.routing_snapshot or {}).get("steps", []):
        if s["sequence"] == seq:
            return s
    return None


def _specs(db: Session, e: ProcessExecution, o: ProductionOrder) -> list[dict[str, Any]]:
    st = _step(o, e.routing_step_sequence)
    if st and st.get("parameter_specs") is not None:
        return st["parameter_specs"] or []
    pd = db.get(ProcessDefinition, e.process_definition_id)
    return pd.parameter_specs or []


def create(db: Session, cu: Any, body: dict[str, Any]) -> ProcessExecution:
    o = db.execute(select(ProductionOrder).where(ProductionOrder.id == body["production_order_id"],
                                                 ProductionOrder.organization_id == cu.organization_id).with_for_update()).scalar_one_or_none()
    if not o:
        raise NotFound("제조지시")
    cu.require_facility(o.facility_id)
    rework = bool(body.get("is_rework"))
    if o.status not in RUNNABLE_ORDER and not (rework and o.status in ("COMPLETED",)):
        raise BusinessRule("ORDER_NOT_RUNNABLE", f"공정을 진행할 수 없는 지시 상태입니다: {o.status}")
    seq = body.get("routing_step_sequence")
    st = _step(o, seq) if seq is not None else None
    if seq is not None and not st:
        raise BusinessRule("STEP_NOT_FOUND", "지시 공정(라우팅)에 없는 순서입니다.")
    pd_id = uuid.UUID(st["process_definition_id"]) if st else body.get("process_definition_id")
    if not pd_id:
        raise BusinessRule("PROCESS_REQUIRED", "공정(라우팅 순서 또는 공정 마스터)을 지정하세요.")
    pd = db.get(ProcessDefinition, pd_id)
    if not pd or pd.organization_id != cu.organization_id:
        raise NotFound("공정")
    batch_id = body.get("production_batch_id")
    if batch_id:
        b = db.get(ProductionBatch, batch_id)
        if not b or b.production_order_id != o.id:
            raise BusinessRule("BATCH_MISMATCH", "지시에 속하지 않은 배치입니다.")
    if st and not rework:
        dup = db.execute(select(ProcessExecution).where(ProcessExecution.production_order_id == o.id,
                                                        ProcessExecution.routing_step_sequence == seq,
                                                        ProcessExecution.production_batch_id == batch_id if batch_id else ProcessExecution.production_batch_id.is_(None),
                                                        ProcessExecution.status.in_(("WAITING", "IN_PROGRESS", "PAUSED", "HOLD")))).scalars().first()
        if dup:
            raise BusinessRule("EXECUTION_EXISTS", "같은 배치·공정의 진행 중인 실행이 이미 있습니다.", {"execution_id": str(dup.id)})
    e = ProcessExecution(organization_id=cu.organization_id, facility_id=o.facility_id, execution_number=next_number(db, cu.organization_id, "PE"),
                         production_order_id=o.id, production_batch_id=batch_id,
                         routing_step_id=uuid.UUID(st["routing_step_id"]) if st else None, routing_step_sequence=seq,
                         process_definition_id=pd.id, equipment_id=body.get("equipment_id") or (uuid.UUID(st["equipment_id"]) if st and st.get("equipment_id") else pd.equipment_id),
                         is_rework=rework, unit_id=o.planned_unit_id, status="WAITING", note=body.get("note"), created_by=cu.id)
    db.add(e)
    db.flush()
    audit(db, cu, "PROCESS_CREATE", "PROCESS_EXECUTION", e.id, after={"order": o.order_number, "process": pd.code, "sequence": seq})
    return e


def start(db: Session, cu: Any, e: ProcessExecution) -> None:
    if e.status != "WAITING":
        raise InvalidState(e.status, "공정 시작")
    o = db.get(ProductionOrder, e.production_order_id)
    if o.status not in RUNNABLE_ORDER and not (e.is_rework and o.status == "COMPLETED"):
        raise BusinessRule("ORDER_NOT_RUNNABLE", f"공정을 진행할 수 없는 지시 상태입니다: {o.status}")
    if e.routing_step_sequence is not None and not e.is_rework:
        for s in (o.routing_snapshot or {}).get("steps", []):
            if s["sequence"] >= e.routing_step_sequence or not s["is_required"] or s["process_type"] == "WEIGHING":
                continue
            stmt = select(func.count()).select_from(ProcessExecution).where(
                ProcessExecution.production_order_id == o.id, ProcessExecution.routing_step_sequence == s["sequence"],
                ProcessExecution.status == "COMPLETED")
            if e.production_batch_id:
                stmt = stmt.where((ProcessExecution.production_batch_id == e.production_batch_id) | ProcessExecution.production_batch_id.is_(None))
            if db.execute(stmt).scalar_one() == 0:
                raise BusinessRule("PREVIOUS_STEP_INCOMPLETE", f"필수 이전 공정이 완료되지 않았습니다: {s['sequence']} {s['step_name']}")
    now = datetime.now(timezone.utc)
    e.status = "IN_PROGRESS"
    e.started_at = now
    e.operated_by = cu.id
    if o.status in ("WEIGHING", "WEIGHING_COMPLETED"):
        o.status = "PRODUCING"
    if e.production_batch_id:
        b = db.get(ProductionBatch, e.production_batch_id)
        if b.status in ("WAITING", "WEIGHING", "WEIGHED"):
            b.status = "PRODUCING"
    audit(db, cu, "PROCESS_START", "PROCESS_EXECUTION", e.id)


def _require_running(e: ProcessExecution, action: str) -> None:
    if e.status != "IN_PROGRESS":
        raise InvalidState(e.status, action)


def _resolve_input(db: Session, cu: Any, raw: str | None, node_id: uuid.UUID | None) -> TraceNode:
    if node_id:
        n = db.get(TraceNode, node_id)
        if not n or n.organization_id != cu.organization_id:
            raise NotFound("투입 LOT")
        return n
    qr = find_qr(db, cu.organization_id, raw or "")
    if not qr:
        raise BusinessRule("INVALID_QR", "인식할 수 없는 QR 입니다. 반제품 QR 을 스캔하세요.")
    mapping = {"WEIGHING_BATCH": "WEIGHING_BATCH", "PROCESS_WIP": "PROCESS_WIP", "FINISHED_GOODS_LOT": "FINISHED_GOODS_LOT"}
    if qr.qr_type not in mapping:
        raise BusinessRule("INVALID_QR", f"공정 투입에는 계량/공정 반제품 QR 을 스캔하세요. (스캔: {qr.qr_type})")
    n = db.execute(select(TraceNode).where(TraceNode.node_type == mapping[qr.qr_type], TraceNode.entity_id == qr.entity_id)).scalar_one()
    return n


def add_input(db: Session, cu: Any, e: ProcessExecution, body: dict[str, Any], idempotency_key: str | None = None) -> ProcessInput:
    _require_running(e, "투입")
    node = _resolve_input(db, cu, body.get("raw"), body.get("trace_node_id"))
    qty = Decimal(str(body["quantity"]))
    if qty <= 0:
        raise BusinessRule("INVALID_QUANTITY", "투입 수량은 0보다 커야 합니다.")
    o = db.get(ProductionOrder, e.production_order_id)
    if node.node_type == "WEIGHING_BATCH":
        ent, input_type = db.get(WeighingBatch, node.entity_id), "WB"
    elif node.node_type == "PROCESS_WIP":
        ent, input_type = db.get(WipLot, node.entity_id), "WIP"
    elif node.node_type == "FINISHED_GOODS_LOT" and e.is_rework:
        ent, input_type = db.get(FinishedGoodsLot, node.entity_id), "FG"
    else:
        raise BusinessRule("INVALID_INPUT", "이 공정에 투입할 수 없는 LOT 유형입니다.")
    status = getattr(ent, "status", "ACTIVE")
    if status in ("CONSUMED", "DISCARDED", "CANCELLED", "WEIGHING", "DEPLETED"):
        raise BusinessRule("INPUT_NOT_USABLE", f"사용할 수 없는 반제품입니다 ({status}). 소진된 QR 은 재투입할 수 없습니다.")
    q_status = ent.quality_status
    if q_status not in ("PASSED", "RELEASED") and not (e.is_rework and q_status in ("HOLD", "REJECTED", "QUALITY_PENDING")):
        raise BusinessRule("QUALITY_NOT_RELEASED", f"보류/부적합 반제품은 투입할 수 없습니다 ({q_status}).")
    if isinstance(ent, WeighingBatch) and ent.usable_until and ent.usable_until < datetime.now(timezone.utc):
        raise BusinessRule("EXPIRED_WB", "계량 반제품의 사용 가능 시간이 지났습니다.")
    # 지시/배치 일치 (다른 배치·지시는 승인 병합만)
    ent_order = getattr(ent, "production_order_id", None)
    ent_batch = getattr(ent, "production_batch_id", None)
    other = (ent_order != o.id) or (e.production_batch_id and ent_batch and ent_batch != e.production_batch_id)
    if other and not e.is_rework:
        approver_id = body.get("merge_approved_by")
        if not approver_id:
            raise BusinessRule("BATCH_MISMATCH", "다른 배치/지시의 반제품입니다. 병합하려면 승인권자 승인이 필요합니다.")
        from app.core.deps import load_permissions

        approver = db.get(User, uuid.UUID(str(approver_id)))
        if not approver or approver.organization_id != cu.organization_id or (
                "process:approve" not in load_permissions(db, approver.id) and "SUPER_ADMIN" not in approver.role_codes):
            raise Forbidden("병합 승인 권한이 없는 사용자입니다.")
        e.merge_approved_by = approver.id
    unit_id = ent.unit_id if hasattr(ent, "unit_id") else ent.base_unit_id
    bals = db.execute(select(InventoryBalance).where(InventoryBalance.lot_node_id == node.id, InventoryBalance.quantity > 0)
                      .order_by(InventoryBalance.quantity.desc())).scalars().all()
    on_hand = sum((b.quantity for b in bals), ZERO)
    if qty > on_hand:
        raise BusinessRule("INSUFFICIENT_STOCK", f"반제품 잔량({ser_value(on_hand)})보다 많이 투입할 수 없습니다.", {"available": ser_value(on_hand)})
    ctx = inv.Ctx(cu.organization_id, e.facility_id, cu.id, reference_type="PROCESS_EXECUTION", reference_id=e.id,
                  reason=f"공정 투입 {e.execution_number}")
    rem, tx_id = qty, None
    tx_type = "WIP_ISSUE" if input_type in ("WB", "WIP") else "PRODUCTION_ISSUE"
    for i, b in enumerate(bals):
        if rem <= 0:
            break
        take = min(b.quantity - b.reserved_quantity, rem)
        if take <= 0:
            continue
        mv = inv.issue(db, ctx, b.id, take, tx_type, idempotency_key=f"{idempotency_key}:{i}" if idempotency_key else None,
                       ignore_reservations=False)
        tx_id = tx_id or mv.transaction.id
        rem -= take
    if rem > 0:
        raise BusinessRule("INSUFFICIENT_STOCK", "예약되지 않은 가용 반제품 잔량이 부족합니다.")
    left = on_hand - qty
    if hasattr(ent, "status") and input_type in ("WB", "WIP"):
        ent.status = "CONSUMED" if left == 0 else "PARTIALLY_CONSUMED"
    qty_exec = convert(db, qty, unit_id, e.unit_id)
    pi = ProcessInput(organization_id=cu.organization_id, process_execution_id=e.id, input_type=input_type, trace_node_id=node.id,
                      lot_number=node.lot_number, production_batch_id=ent_batch, quantity=qty, unit_id=unit_id,
                      inventory_transaction_id=tx_id, input_by=cu.id, input_at=datetime.now(timezone.utc))
    db.add(pi)
    e.total_input_quantity = e.total_input_quantity + qty_exec
    db.flush()
    audit(db, cu, "PROCESS_INPUT", "PROCESS_EXECUTION", e.id, after={"lot": node.lot_number, "quantity": str(qty)})
    return pi


def add_packaging(db: Session, cu: Any, e: ProcessExecution, body: dict[str, Any], idempotency_key: str | None = None) -> PackagingConsumption:
    _require_running(e, "포장재 투입")
    o = db.get(ProductionOrder, e.production_order_id)
    pkg = None
    if body.get("package_id"):
        pkg = db.get(MaterialPackage, body["package_id"])
    else:
        qr = find_qr(db, cu.organization_id, body.get("raw") or "")
        if qr and qr.qr_type == "RAW_MATERIAL_PACKAGE":
            pkg = db.get(MaterialPackage, qr.entity_id)
        elif qr:
            raise BusinessRule("INVALID_QR", "포장재 포장 QR 을 스캔하세요.")
    if not pkg or pkg.organization_id != cu.organization_id:
        raise BusinessRule("INVALID_QR", "등록되지 않은 포장재 QR 입니다.")
    lot = db.get(MaterialLot, pkg.material_lot_id)
    m = db.get(Material, lot.material_id)
    item = next((i for i in o.items if i.item_type == "PACKAGING" and (i.material_id == m.id or (
        i.substitute_allowed and str(m.id) in [str(x) for x in (i.substitute_material_ids or [])]))), None)
    if not item:
        raise BusinessRule("MATERIAL_NOT_IN_ORDER", f"제조지시에 없는 포장재입니다: {m.material_name}")
    if lot.quality_status != "PASSED":
        raise BusinessRule("QUALITY_NOT_RELEASED", "품질 미승인 포장재입니다.")
    if lot.expiry_date and lot.expiry_date < today_seoul():
        raise BusinessRule("EXPIRED_MATERIAL", "기한이 경과한 포장재입니다.")
    if pkg.package_status in ("EMPTY", "DAMAGED", "CONTAMINATED", "CANCELLED"):
        raise BusinessRule("PACKAGE_NOT_USABLE", f"사용할 수 없는 포장입니다 ({pkg.package_status}).")
    used = Decimal(str(body["used_quantity"]))
    defective = Decimal(str(body.get("defective_quantity") or 0))
    waste = Decimal(str(body.get("waste_quantity") or 0))
    issued = used + defective + waste
    if used < 0 or defective < 0 or waste < 0 or issued <= 0:
        raise BusinessRule("INVALID_QUANTITY", "사용/불량/폐기 수량을 확인하세요.")
    bal = db.execute(select(InventoryBalance).where(InventoryBalance.package_node_id == pkg.trace_node_id, InventoryBalance.quantity > 0,
                                                    InventoryBalance.facility_id == e.facility_id).order_by(InventoryBalance.quantity.desc())).scalars().first()
    if not bal:
        raise BusinessRule("INSUFFICIENT_STOCK", "포장재 잔량이 없습니다.")
    ctx = inv.Ctx(cu.organization_id, e.facility_id, cu.id, reference_type="PACKAGING", reference_id=e.id, reason=f"포장재 투입 {e.execution_number}")
    mv = inv.issue(db, ctx, bal.id, issued, "PRODUCTION_ISSUE", allow_reserved_for_order=o.id, require_available_quality=True,
                   material_lot_id=lot.id, package_id=pkg.id, idempotency_key=idempotency_key)
    rem = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(InventoryBalance.package_node_id == pkg.trace_node_id)).scalar_one()
    pkg.package_status = "EMPTY" if rem == 0 else "IN_USE"
    pc = PackagingConsumption(organization_id=cu.organization_id, facility_id=e.facility_id, process_execution_id=e.id, production_order_id=o.id,
                              material_id=m.id, material_lot_id=lot.id, material_package_id=pkg.id, issued_quantity=issued, used_quantity=used,
                              defective_quantity=defective, waste_quantity=waste, unit_id=pkg.base_unit_id,
                              inventory_transaction_id=mv.transaction.id, consumed_by=cu.id, consumed_at=datetime.now(timezone.utc))
    db.add(pc)
    item.issued_quantity = item.issued_quantity + issued
    db.flush()
    audit(db, cu, "PACKAGING_CONSUME", "PROCESS_EXECUTION", e.id, after={"package": pkg.package_number, "issued": str(issued)})
    return pc


def record_parameters(db: Session, cu: Any, e: ProcessExecution, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if e.status not in ("IN_PROGRESS", "PAUSED"):
        raise InvalidState(e.status, "공정 조건 기록")
    o = db.get(ProductionOrder, e.production_order_id)
    pd = db.get(ProcessDefinition, e.process_definition_id)
    specs = {s["code"]: s for s in _specs(db, e, o)}
    out, warnings = [], []
    now = datetime.now(timezone.utc)
    for r in records:
        sp = specs.get(r["parameter_code"], {})
        val = Decimal(str(r["value_numeric"])) if r.get("value_numeric") is not None else None
        mn = Decimal(str(sp["min"])) if sp.get("min") is not None else None
        mx = Decimal(str(sp["max"])) if sp.get("max") is not None else None
        oor = False
        if val is not None and ((mn is not None and val < mn) or (mx is not None and val > mx)):
            oor = True
        if sp.get("type") == "PASS_FAIL" and str(r.get("value_text", "")).upper() in ("FAIL", "NG", "부적합"):
            oor = True
        if oor:
            warnings.append(f"{sp.get('name', r['parameter_code'])} 기준 이탈: {val if val is not None else r.get('value_text')} (기준 {mn}~{mx})")
        rec = ProcessParameterRecord(organization_id=cu.organization_id, process_execution_id=e.id, parameter_code=r["parameter_code"],
                                     parameter_name=sp.get("name", r.get("parameter_name") or r["parameter_code"]), value_numeric=val,
                                     value_text=r.get("value_text"), unit=sp.get("unit") or r.get("unit"), min_value=mn, max_value=mx,
                                     out_of_range=oor, source=r.get("source", "MANUAL"), is_ccp=pd.is_ccp, recorded_by=cu.id, recorded_at=now)
        db.add(rec)
        out.append(rec)
    if warnings and pd.is_ccp:
        e.quality_status = "HOLD"
        e.status = "HOLD"
        e.reason = "CCP 기준 이탈: " + "; ".join(warnings)
    db.flush()
    audit(db, cu, "PROCESS_PARAMETERS", "PROCESS_EXECUTION", e.id, after={"count": len(out), "warnings": warnings})
    return [{**ser(x)} for x in out] + ([{"warnings": warnings, "held": pd.is_ccp}] if warnings else [])


def pause(db: Session, cu: Any, e: ProcessExecution, resume: bool = False) -> None:
    now = datetime.now(timezone.utc)
    if resume:
        if e.status != "PAUSED":
            raise InvalidState(e.status, "재개")
        e.paused_minutes = e.paused_minutes + Decimal(str(round((now - e.paused_at).total_seconds() / 60, 2)))
        e.paused_at = None
        e.status = "IN_PROGRESS"
    else:
        _require_running(e, "일시정지")
        e.status = "PAUSED"
        e.paused_at = now
    audit(db, cu, "PROCESS_RESUME" if resume else "PROCESS_PAUSE", "PROCESS_EXECUTION", e.id)


def hold(db: Session, cu: Any, e: ProcessExecution, reason: str) -> None:
    if e.status not in ("WAITING", "IN_PROGRESS", "PAUSED"):
        raise InvalidState(e.status, "보류")
    e.status = "HOLD"
    e.quality_status = "HOLD"
    e.reason = reason
    audit(db, cu, "PROCESS_HOLD", "PROCESS_EXECUTION", e.id, reason=reason)


def release_hold(db: Session, cu: Any, e: ProcessExecution, reason: str) -> None:
    if e.status != "HOLD":
        raise InvalidState(e.status, "보류 해제")
    e.status = "IN_PROGRESS" if e.started_at else "WAITING"
    e.quality_status = "PASSED"
    e.approved_by = cu.id
    audit(db, cu, "PROCESS_RELEASE_HOLD", "PROCESS_EXECUTION", e.id, reason=reason)


def _warehouse(db: Session, org_id: uuid.UUID, facility_id: uuid.UUID | None, wtype: str, preferred: uuid.UUID | None = None) -> Warehouse:
    if preferred:
        wh = db.get(Warehouse, preferred)
        if wh and wh.facility_id == facility_id:
            return wh
    wh = db.execute(select(Warehouse).where(Warehouse.organization_id == org_id, Warehouse.facility_id == facility_id,
                                            Warehouse.warehouse_type == wtype, Warehouse.is_active)).scalars().first()
    if not wh:
        raise BusinessRule("WAREHOUSE_NOT_CONFIGURED", f"{wtype} 유형 창고가 설정되지 않았습니다.")
    return wh


def fg_expiry(p: Product, mfg) -> Any:
    if not p.shelf_life_days:
        return None
    return mfg + timedelta(days=p.shelf_life_days - 1)


def complete(db: Session, cu: Any, e: ProcessExecution, body: dict[str, Any]) -> dict[str, Any]:
    _require_running(e, "공정 완료")
    o = db.execute(select(ProductionOrder).where(ProductionOrder.id == e.production_order_id).with_for_update()).scalar_one()
    pd = db.get(ProcessDefinition, e.process_definition_id)
    st = _step(o, e.routing_step_sequence)
    output_type = (st or {}).get("output_type") or pd.output_type
    inputs = db.execute(select(ProcessInput).where(ProcessInput.process_execution_id == e.id)).scalars().all()
    packs = db.execute(select(PackagingConsumption).where(PackagingConsumption.process_execution_id == e.id)).scalars().all()
    if not inputs:
        raise BusinessRule("NO_INPUT", "투입 기록이 없습니다. 반제품 QR 을 스캔하여 투입하세요.")
    # 필수 조건 누락
    recorded = {r.parameter_code for r in db.execute(select(ProcessParameterRecord).where(ProcessParameterRecord.process_execution_id == e.id)).scalars()}
    missing = [s.get("name", s["code"]) for s in _specs(db, e, o) if s.get("required") and s["code"] not in recorded]
    if missing:
        raise BusinessRule("REQUIRED_PARAMETER_MISSING", f"필수 공정 조건이 기록되지 않았습니다: {', '.join(missing)}", {"missing": missing})
    now = datetime.now(timezone.utc)
    if e.started_at and now <= e.started_at:
        raise BusinessRule("INVALID_TIME", "종료 시각은 시작 시각 이후여야 합니다.")
    defective = Decimal(str(body.get("defective_quantity") or 0))
    waste = Decimal(str(body.get("waste_quantity") or 0))
    loss = Decimal(str(body.get("process_loss_quantity") or 0))
    moisture = Decimal(str(body.get("moisture_change_quantity") or 0))
    if min(defective, waste, loss) < 0:
        raise BusinessRule("INVALID_QUANTITY", "불량/폐기/손실 수량은 음수일 수 없습니다.")
    product = db.get(Product, o.product_id)
    outputs_spec = body.get("outputs") or []
    fg_body = body.get("fg") or None
    if output_type == "FG":
        if not fg_body or Decimal(str(fg_body.get("good_quantity", 0))) <= 0:
            raise BusinessRule("FG_QUANTITY_REQUIRED", "완제품 정상 수량을 입력하세요.")
        good_ea = Decimal(str(fg_body["good_quantity"]))
        content_kg = convert(db, product.content_quantity, product.content_unit_id, e.unit_id)
        good_out = good_ea * content_kg
    elif output_type == "WIP":
        if not outputs_spec:
            raise BusinessRule("OUTPUT_REQUIRED", "산출량을 입력하세요.")
        good_out = sum((Decimal(str(x["quantity"])) for x in outputs_spec), ZERO)
        if any(Decimal(str(x["quantity"])) <= 0 for x in outputs_spec):
            raise BusinessRule("INVALID_QUANTITY", "산출량은 0보다 커야 합니다.")
    else:
        good_out = e.total_input_quantity - defective - waste - loss + moisture
    balance = e.total_input_quantity + moisture - good_out - defective - waste - loss
    if abs(balance) > EPS:
        raise BusinessRule("MASS_BALANCE_MISMATCH", "투입량과 산출량+불량+폐기+손실이 일치하지 않습니다. (수분 변화는 별도 입력)",
                           {"input": ser_value(e.total_input_quantity), "good_output": ser_value(good_out), "defective": ser_value(defective),
                            "waste": ser_value(waste), "loss": ser_value(loss), "moisture": ser_value(moisture), "difference": ser_value(balance)})
    e.good_output_quantity = good_out
    e.defective_quantity = defective
    e.waste_quantity = waste
    e.process_loss_quantity = loss
    e.moisture_change_quantity = moisture
    e.process_yield = (good_out / e.total_input_quantity * 100).quantize(Decimal("0.01")) if e.total_input_quantity else None
    e.inspection_result = body.get("inspection_result")
    e.completed_at = now
    e.note = body.get("note") or e.note
    if body.get("photo_file_ids"):
        e.photo_file_ids = [str(x) for x in body["photo_file_ids"]]
    out_quality = "HOLD" if (pd.approval_required or body.get("inspection_result") in ("FAIL", "HOLD")) else "PASSED"
    ctx = inv.Ctx(cu.organization_id, e.facility_id, cu.id, reference_type="PROCESS_EXECUTION", reference_id=e.id,
                  reason=f"공정 산출 {e.execution_number}")
    out_nodes: list[trace.NodeRef] = []
    created: list[dict[str, Any]] = []
    fg_lot = None
    if output_type == "WIP":
        wh = _warehouse(db, cu.organization_id, e.facility_id, "WIP")
        for spec in outputs_spec:
            q = Decimal(str(spec["quantity"]))
            w = WipLot(id=uuid.uuid4(), organization_id=cu.organization_id, facility_id=e.facility_id,
                       lot_number=next_number(db, cu.organization_id, "WIP"), production_order_id=o.id,
                       production_batch_id=e.production_batch_id or _single_batch(inputs), product_id=o.product_id,
                       process_execution_id=e.id, routing_step_sequence=e.routing_step_sequence, quantity=q, unit_id=e.unit_id,
                       quality_status=out_quality, status="READY" if out_quality == "PASSED" else "HOLD", created_by=cu.id)
            db.add(w)
            db.flush()
            node = trace.create_node(db, cu.organization_id, e.facility_id, "PROCESS_WIP", w.id, w.lot_number, f"{pd.name} 반제품")
            w.trace_node_id = node.id
            w.qr_code_id = ensure_qr(db, org_id=cu.organization_id, qr_type="PROCESS_WIP", entity_id=w.id, lot_number=w.lot_number, user_id=cu.id).id
            mv = inv.receive(db, ctx, inv.StockKey(item_type="WIP", lot_node_id=node.id, package_node_id=None, warehouse_id=wh.id,
                                                   storage_location_id=None, unit_id=e.unit_id, lot_number=w.lot_number,
                                                   quality_status=out_quality, product_id=o.product_id), q, "WIP_RECEIPT")
            db.add(ProcessOutput(organization_id=cu.organization_id, process_execution_id=e.id, output_type="WIP", trace_node_id=node.id,
                                 lot_number=w.lot_number, quantity=q, unit_id=e.unit_id, inventory_transaction_id=mv.transaction.id))
            out_nodes.append(trace.NodeRef(node.id, q, e.unit_id))
            created.append({"type": "WIP", "id": str(w.id), "lot_number": w.lot_number, "quantity": ser_value(q), "qr_code_id": str(w.qr_code_id)})
    elif output_type == "FG":
        fg_lot = _create_fg(db, cu, e, o, product, fg_body, good_out, inputs, ctx)
        out_nodes.append(trace.NodeRef(fg_lot.trace_node_id, fg_lot.good_quantity, fg_lot.base_unit_id))
        created.append({"type": "FG", "id": str(fg_lot.id), "lot_number": fg_lot.lot_number, "quantity": ser_value(fg_lot.good_quantity),
                        "net_weight": ser_value(fg_lot.net_weight), "production_yield": ser_value(fg_lot.production_yield),
                        "expiry_date": ser_value(fg_lot.expiry_date), "qr_code_id": str(fg_lot.qr_code_id)})
        for pc in packs:
            pc.fg_lot_id = fg_lot.id
    # 추적 이벤트
    in_refs = [trace.NodeRef(i.trace_node_id, i.quantity, i.unit_id) for i in inputs]
    for pc in packs:
        pnode = db.get(MaterialPackage, pc.material_package_id).trace_node_id
        in_refs.append(trace.NodeRef(pnode, pc.issued_quantity, pc.unit_id))
    batches = {i.production_batch_id for i in inputs if i.production_batch_id}
    if e.is_rework:
        ev_type = "REWORK"
    elif len(batches) > 1 or e.merge_approved_by:
        ev_type = "MERGE"
    elif output_type == "FG":
        ev_type = "PACKAGING"
    elif pd.process_type == "MIXING":
        ev_type = "MIXING"
    else:
        ev_type = "PROCESSING"
    if out_nodes:
        trace.record_event(db, org_id=cu.organization_id, facility_id=e.facility_id, user_id=cu.id, event_type=ev_type, inputs=in_refs,
                           outputs=out_nodes, production_order_id=o.id, production_batch_id=e.production_batch_id,
                           reference_type="PROCESS_EXECUTION", reference_id=e.id, note=pd.name)
    e.status = "COMPLETED"
    e.quality_status = out_quality
    if e.production_batch_id and output_type == "FG":
        db.get(ProductionBatch, e.production_batch_id).status = "COMPLETED"
    audit(db, cu, "PROCESS_COMPLETE", "PROCESS_EXECUTION", e.id, after={"outputs": created, "yield": ser_value(e.process_yield)})
    return {"execution": ser(e), "outputs": created, "approval_required": pd.approval_required}


def _single_batch(inputs: list[ProcessInput]) -> uuid.UUID | None:
    b = {i.production_batch_id for i in inputs if i.production_batch_id}
    return b.pop() if len(b) == 1 else None


def _create_fg(db: Session, cu: Any, e: ProcessExecution, o: ProductionOrder, p: Product, fg_body: dict[str, Any], good_kg: Decimal,
               inputs: list[ProcessInput], ctx: inv.Ctx) -> FinishedGoodsLot:
    good = Decimal(str(fg_body["good_quantity"]))
    defective = Decimal(str(fg_body.get("defective_quantity") or 0))
    waste = Decimal(str(fg_body.get("waste_quantity") or 0))
    mfg = o.production_date
    batch_id = e.production_batch_id or _single_batch(inputs)
    # 실제 총 식품원료 투입량 = 해당 배치(또는 지시)의 배합 원료 계량 실적
    stmt = select(WeighingRecord).where(WeighingRecord.production_order_id == o.id, WeighingRecord.status == "CONFIRMED")
    if batch_id:
        stmt = stmt.where(WeighingRecord.production_batch_id == batch_id)
    total_input = ZERO
    for rec in db.execute(stmt).scalars():
        it = db.get(ProductionOrderItem, rec.production_order_item_id)
        if it.item_type == "FORMULA":
            total_input += convert(db, rec.actual_quantity, rec.unit_id, e.unit_id, material=db.get(Material, rec.material_id))
    wh = _warehouse(db, cu.organization_id, e.facility_id, "FINISHED", p.default_warehouse_id)
    fg = FinishedGoodsLot(
        id=uuid.uuid4(), organization_id=cu.organization_id, facility_id=e.facility_id, lot_number=next_number(db, cu.organization_id, "FG"),
        product_id=p.id, production_order_id=o.id, production_batch_id=batch_id, process_execution_id=e.id, manufactured_date=mfg,
        production_started_at=o.actual_start, production_ended_at=datetime.now(timezone.utc), expiry_date=fg_expiry(p, mfg),
        expiry_basis_date=mfg, content_quantity=p.content_quantity, content_unit_id=p.content_unit_id,
        planned_quantity=o.target_fg_quantity, total_produced_quantity=good + defective + waste, good_quantity=good,
        defective_quantity=defective, waste_quantity=waste, total_input_quantity=total_input, net_weight=good_kg,
        loss_quantity=(total_input - good_kg) if total_input else None, base_unit_id=p.base_unit_id,
        production_yield=(good_kg / total_input * 100).quantize(Decimal("0.01")) if total_input else None,
        quality_status="QUALITY_PENDING", responsible_id=cu.id, warehouse_id=wh.id, created_by=cu.id,
    )
    db.add(fg)
    db.flush()
    node = trace.create_node(db, cu.organization_id, e.facility_id, "FINISHED_GOODS_LOT", fg.id, fg.lot_number, p.product_name)
    fg.trace_node_id = node.id
    fg.qr_code_id = ensure_qr(db, org_id=cu.organization_id, qr_type="FINISHED_GOODS_LOT", entity_id=fg.id, lot_number=fg.lot_number, user_id=cu.id).id
    mv = inv.receive(db, ctx, inv.StockKey(item_type="FG", lot_node_id=node.id, package_node_id=None, warehouse_id=wh.id, storage_location_id=None,
                                           unit_id=p.base_unit_id, lot_number=fg.lot_number, quality_status="QUALITY_PENDING", product_id=p.id,
                                           expiry_date=fg.expiry_date), good, "FG_RECEIPT")
    db.add(ProcessOutput(organization_id=cu.organization_id, process_execution_id=e.id, output_type="FG", trace_node_id=node.id,
                         lot_number=fg.lot_number, quantity=good, unit_id=p.base_unit_id, inventory_transaction_id=mv.transaction.id))
    if o.status in ("PRODUCING", "WEIGHING_COMPLETED"):
        o.status = "QUALITY_PENDING"
    return fg


def approve(db: Session, cu: Any, e: ProcessExecution, note: str | None) -> None:
    if e.status != "COMPLETED":
        raise InvalidState(e.status, "공정 승인")
    if e.approved_by:
        raise BusinessRule("ALREADY_APPROVED", "이미 승인된 공정입니다.")
    e.approved_by = cu.id
    e.approved_at = datetime.now(timezone.utc)
    if e.quality_status == "HOLD":
        e.quality_status = "PASSED"
        for w in db.execute(select(WipLot).where(WipLot.process_execution_id == e.id)).scalars():
            if w.quality_status == "HOLD":
                w.quality_status = "PASSED"
                w.status = "READY"
                inv.set_lot_quality(db, cu.organization_id, w.trace_node_id, "PASSED")
    audit(db, cu, "PROCESS_APPROVE", "PROCESS_EXECUTION", e.id, reason=note)


def detail(db: Session, e: ProcessExecution) -> dict[str, Any]:
    pd = db.get(ProcessDefinition, e.process_definition_id)
    o = db.get(ProductionOrder, e.production_order_id)
    ins = [ser(i) for i in db.execute(select(ProcessInput).where(ProcessInput.process_execution_id == e.id)).scalars()]
    outs = [ser(x) for x in db.execute(select(ProcessOutput).where(ProcessOutput.process_execution_id == e.id)).scalars()]
    params = [ser(x) for x in db.execute(select(ProcessParameterRecord).where(ProcessParameterRecord.process_execution_id == e.id)
                                         .order_by(ProcessParameterRecord.recorded_at)).scalars()]
    packs = []
    for pc in db.execute(select(PackagingConsumption).where(PackagingConsumption.process_execution_id == e.id)).scalars():
        m = db.get(Material, pc.material_id)
        packs.append(ser(pc, material_code=m.material_code, material_name=m.material_name,
                         lot_number=db.get(MaterialLot, pc.material_lot_id).lot_number,
                         package_number=db.get(MaterialPackage, pc.material_package_id).package_number))
    work_min = None
    if e.started_at and e.completed_at:
        work_min = Decimal(str(round((e.completed_at - e.started_at).total_seconds() / 60, 2))) - e.paused_minutes
    return ser(e, process_code=pd.code, process_name=pd.name, is_ccp=pd.is_ccp, order_number=o.order_number,
               parameter_specs=_specs(db, e, o), inputs=ins, outputs=outs, parameters=params, packaging=packs,
               work_minutes=work_min, unit=db.get(Unit, e.unit_id).code)
