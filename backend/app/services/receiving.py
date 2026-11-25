"""원료 입고: 등록 → (OCR) → 검수 → 품질승인 → 확정(LOT/포장/QR/재고/수불/추적) → 정정."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit, snapshot
from app.core.deps import CurrentUser
from app.core.errors import BusinessRule, InvalidState, NotFound
from app.core.numbering import next_number, today_seoul
from app.models import (
    DocumentLink, InspectionTemplate, InventoryBalance, InventoryTransaction, Material, MaterialLot, MaterialPackage,
    MaterialReceipt, MaterialReceiptItem, QualityInspection, QualityInspectionResult, ReceiptDocument, Supplier,
    TraceEvent, Warehouse,
)
from app.models.master import MAJOR_PREFIX
from app.services.inventory import service as inv
from app.services.inventory.units import convert
from app.services.qr.service import ensure_qr
from app.services.traceability import service as trace
from app.repositories.common import ser_value

EDITABLE = ("DRAFT", "OCR_REVIEW", "INSPECTION_PENDING", "HOLD")
POSTABLE = ("ACCEPTED", "INSPECTION_PENDING", "HOLD", "DRAFT", "OCR_REVIEW")
RESULTS = ("PASS", "FAIL", "HOLD", "NOT_APPLICABLE")


def get_receipt(db: Session, cu: CurrentUser, receipt_id: uuid.UUID, lock: bool = False) -> MaterialReceipt:
    stmt = select(MaterialReceipt).where(MaterialReceipt.id == receipt_id, MaterialReceipt.organization_id == cu.organization_id)
    if lock:
        stmt = stmt.with_for_update()
    r = db.execute(stmt).scalar_one_or_none()
    if not r:
        raise NotFound("입고")
    cu.require_facility(r.facility_id)
    return r


def _validate_item(db: Session, cu: CurrentUser, it: dict[str, Any]) -> dict[str, Any]:
    m = db.get(Material, it["material_id"])
    if not m or m.organization_id != cu.organization_id:
        raise NotFound("원료")
    if not m.is_active:
        raise BusinessRule("MATERIAL_INACTIVE", f"비활성 원료입니다: {m.material_name}")
    it = dict(it)
    it.setdefault("unit_id", m.base_unit_id)
    if not it.get("unit_id"):
        it["unit_id"] = m.base_unit_id
    wh_id = it.get("warehouse_id") or m.default_warehouse_id
    if not wh_id:
        raise BusinessRule("WAREHOUSE_REQUIRED", f"입고 창고를 지정하세요: {m.material_name}")
    wh = db.get(Warehouse, wh_id)
    if not wh or wh.organization_id != cu.organization_id or not wh.is_active:
        raise NotFound("창고")
    if wh.facility_id != cu.facility_id:
        raise BusinessRule("WAREHOUSE_FACILITY_MISMATCH", "현재 공장의 창고만 선택할 수 있습니다.")
    it["warehouse_id"] = wh_id
    it.setdefault("manufacturer_name", m.manufacturer_name)
    it.setdefault("country_of_origin", m.default_country_of_origin)
    it.setdefault("storage_condition", m.storage_condition)
    if not m.expiry_tracking_required and not it.get("expiry_type"):
        it["expiry_type"] = "NOT_APPLICABLE"
    pc = int(it.get("package_count") or 1)
    if pc < 1:
        raise BusinessRule("INVALID_PACKAGE_COUNT", "포장 수량은 1 이상이어야 합니다.")
    it["package_count"] = pc
    qty = Decimal(str(it["quantity"]))
    if qty <= 0:
        raise BusinessRule("INVALID_QUANTITY", "입고 수량은 0보다 커야 합니다.")
    if it.get("package_quantity"):
        pq = Decimal(str(it["package_quantity"]))
        if pq * pc != qty:
            raise BusinessRule("PACKAGE_QUANTITY_MISMATCH", f"포장당 수량 × 포장 수({pq}×{pc})가 입고 수량({qty})과 다릅니다.")
    if it.get("unit_price") is not None and it.get("amount") is None:
        it["amount"] = Decimal(str(it["unit_price"])) * qty
    if it.get("manufactured_date") and it.get("expiry_date") and it["expiry_date"] <= it["manufactured_date"]:
        raise BusinessRule("INVALID_DATES", "소비/유통기한은 제조일자 이후여야 합니다.")
    return it


def create(db: Session, cu: CurrentUser, data: dict[str, Any]) -> MaterialReceipt:
    if data.get("supplier_id"):
        s = db.get(Supplier, data["supplier_id"])
        if not s or s.organization_id != cu.organization_id:
            raise NotFound("공급업체")
        if s.trade_status != "ACTIVE":
            raise BusinessRule("SUPPLIER_INACTIVE", "거래 중지된 공급업체입니다.")
    items = data.pop("items", []) or []
    r = MaterialReceipt(
        organization_id=cu.organization_id, facility_id=cu.facility_id,
        receipt_number=next_number(db, cu.organization_id, "RCV"), received_at=datetime.now(timezone.utc),
        received_by=cu.id, created_by=cu.id, status="DRAFT", **data,
    )
    db.add(r)
    db.flush()
    _replace_items(db, cu, r, items)
    audit(db, cu, "RECEIPT_CREATE", "RECEIPT", r.id, after=snapshot(r))
    return r


def _replace_items(db: Session, cu: CurrentUser, r: MaterialReceipt, items: list[dict[str, Any]]) -> None:
    for old in list(r.items):
        db.delete(old)
    db.flush()
    for i, it in enumerate(items, start=1):
        v = _validate_item(db, cu, it)
        db.add(MaterialReceiptItem(organization_id=cu.organization_id, receipt_id=r.id, line_no=i, created_by=cu.id, **v))
    db.flush()
    db.refresh(r)


def update(db: Session, cu: CurrentUser, r: MaterialReceipt, data: dict[str, Any]) -> MaterialReceipt:
    if r.status not in EDITABLE:
        raise InvalidState(r.status, "수정")
    before = snapshot(r)
    items = data.pop("items", None)
    for k, v in data.items():
        setattr(r, k, v)
    r.updated_by = cu.id
    if items is not None:
        _replace_items(db, cu, r, items)
    r.version += 1
    audit(db, cu, "RECEIPT_UPDATE", "RECEIPT", r.id, before=before, after=snapshot(r))
    return r


def submit_inspection(db: Session, cu: CurrentUser, r: MaterialReceipt) -> MaterialReceipt:
    if r.status not in ("DRAFT", "OCR_REVIEW", "HOLD"):
        raise InvalidState(r.status, "검수 요청")
    if not r.items:
        raise BusinessRule("NO_ITEMS", "입고 품목이 없습니다.")
    for it in r.items:
        _check_item_required(db, it)
    r.status = "INSPECTION_PENDING"
    audit(db, cu, "RECEIPT_SUBMIT_INSPECTION", "RECEIPT", r.id)
    return r


def _check_item_required(db: Session, it: MaterialReceiptItem) -> Material:
    m = db.get(Material, it.material_id)
    if m.expiry_tracking_required:
        if not it.expiry_date:
            raise BusinessRule("EXPIRY_REQUIRED", f"소비/유통기한은 필수입니다: {m.material_name}")
        if not it.expiry_type or it.expiry_type == "NOT_APPLICABLE":
            raise BusinessRule("EXPIRY_TYPE_REQUIRED", f"기한 유형(소비기한/유통기한)을 선택하세요: {m.material_name}")
    if m.lot_tracking_required and m.major_category == "MAIN_MATERIAL" and not it.supplier_lot_number:
        raise BusinessRule("SUPPLIER_LOT_REQUIRED", f"공급업체 LOT 는 필수입니다: {m.material_name}")
    return m


def inspect(db: Session, cu: CurrentUser, r: MaterialReceipt, body: dict[str, Any]) -> QualityInspection:
    if r.status not in ("INSPECTION_PENDING", "HOLD", "POSTED"):
        raise InvalidState(r.status, "검수")
    source = body.get("source", "FIELD")
    overall = body["overall_result"]
    if overall not in RESULTS:
        raise BusinessRule("INVALID_RESULT", "검수 결과는 PASS/FAIL/HOLD/NOT_APPLICABLE 중 하나입니다.")
    results = body.get("results", [])
    tpl = None
    if body.get("template_id"):
        tpl = db.get(InspectionTemplate, body["template_id"])
    else:
        tpl = db.execute(select(InspectionTemplate).where(InspectionTemplate.organization_id == cu.organization_id,
                                                          InspectionTemplate.code == "RCV-STD")).scalar_one_or_none()
    names = {i.item_code: i for i in (tpl.items if tpl else [])}
    # 필수 항목 누락 검사 (현장 검수)
    if source == "FIELD" and tpl:
        provided = {x["item_code"] for x in results}
        missing = [i.item_name for i in tpl.items if i.is_required and i.item_code not in provided]
        if missing:
            raise BusinessRule("REQUIRED_INSPECTION_ITEM_MISSING", f"필수 검수 항목 누락: {', '.join(missing)}", {"missing": missing})
    qi = QualityInspection(
        organization_id=cu.organization_id, facility_id=r.facility_id,
        inspection_number=next_number(db, cu.organization_id, "QI"), target_type="RECEIPT", target_id=r.id,
        template_id=tpl.id if tpl else None, source=source, overall_result=overall,
        nonconformance_reason=body.get("nonconformance_reason"), disposition=body.get("disposition"),
        inspected_by=cu.id, inspected_at=datetime.now(timezone.utc), photo_file_ids=[str(x) for x in body.get("photo_file_ids") or []],
        note=body.get("note"), created_by=cu.id,
    )
    db.add(qi)
    db.flush()
    warnings = []
    mats = {it.material_id: db.get(Material, it.material_id) for it in r.items}
    for res in results:
        oor = False
        mv = res.get("measured_value")
        if res["item_code"] == "TEMPERATURE" and mv is not None:
            mv = Decimal(str(mv))
            for m in mats.values():
                if (m.receiving_temp_min is not None and mv < m.receiving_temp_min) or (m.receiving_temp_max is not None and mv > m.receiving_temp_max):
                    oor = True
                    warnings.append(f"입고 온도 {mv}℃ 가 허용 범위({m.receiving_temp_min}~{m.receiving_temp_max}℃)를 벗어났습니다: {m.material_name}")
        ti = names.get(res["item_code"])
        db.add(QualityInspectionResult(
            inspection_id=qi.id, item_code=res["item_code"], item_name=res.get("item_name") or (ti.item_name if ti else res["item_code"]),
            result=res.get("result", "PASS"), measured_value=mv, text_value=res.get("text_value"),
            standard=res.get("standard") or (ti.criteria if ti else None), out_of_range=oor, note=res.get("note"),
        ))
    if warnings and overall == "PASS":
        raise BusinessRule("OUT_OF_RANGE", "허용 범위를 벗어난 항목이 있어 합격 처리할 수 없습니다. 보류 또는 불합격으로 판정하세요.",
                           {"warnings": warnings})
    if overall == "FAIL" and not body.get("nonconformance_reason"):
        raise BusinessRule("REASON_REQUIRED", "부적합 사유를 입력하세요.")
    if source == "FIELD":
        r.inspection_result = overall
        r.inspected_by = cu.id
        if r.status != "POSTED":
            r.status = {"PASS": "INSPECTION_PENDING", "NOT_APPLICABLE": "INSPECTION_PENDING", "HOLD": "HOLD", "FAIL": "REJECTED"}[overall]
            r.approved_by = None
            r.approved_at = None
        else:
            new_q = {"FAIL": "REJECTED", "HOLD": "HOLD"}.get(overall)
            for lot in _lots_of(db, r):
                if new_q:
                    _set_lot_quality(db, cu, lot, new_q)
    db.flush()
    audit(db, cu, "RECEIPT_INSPECTION", "RECEIPT", r.id, after={"inspection_id": str(qi.id), "source": source, "result": overall},
          reason=body.get("nonconformance_reason"))
    qi._warnings = warnings  # type: ignore[attr-defined]
    return qi


def _lots_of(db: Session, r: MaterialReceipt) -> list[MaterialLot]:
    ids = [it.material_lot_id for it in r.items if it.material_lot_id]
    return list(db.execute(select(MaterialLot).where(MaterialLot.id.in_(ids))).scalars()) if ids else []


def _set_lot_quality(db: Session, cu: CurrentUser, lot: MaterialLot, status: str) -> None:
    if lot.quality_status == "REJECTED" and status == "PASSED":
        raise BusinessRule("REJECTED_LOT", "부적합 LOT 는 가용재고로 전환할 수 없습니다.")
    before = lot.quality_status
    lot.quality_status = status
    inv.set_lot_quality(db, cu.organization_id, lot.trace_node_id, status)
    audit(db, cu, "LOT_QUALITY_CHANGE", "MATERIAL_LOT", lot.id, before={"quality_status": before}, after={"quality_status": status})


def _has_coa(db: Session, r: MaterialReceipt) -> bool:
    q = select(func.count()).select_from(DocumentLink).join(ReceiptDocument, ReceiptDocument.id == DocumentLink.document_id).where(
        DocumentLink.entity_type == "RECEIPT", DocumentLink.entity_id == r.id, ReceiptDocument.document_type == "COA")
    return db.execute(q).scalar_one() > 0


def approve(db: Session, cu: CurrentUser, r: MaterialReceipt, note: str | None = None) -> MaterialReceipt:
    if r.status not in ("INSPECTION_PENDING", "POSTED"):
        raise InvalidState(r.status, "품질 승인")
    if r.inspection_result not in ("PASS", "NOT_APPLICABLE"):
        raise BusinessRule("INSPECTION_NOT_PASSED", "현장 검수 합격 후 승인할 수 있습니다.", {"inspection_result": r.inspection_result})
    for it in r.items:
        m = db.get(Material, it.material_id)
        if m.coa_required and not _has_coa(db, r):
            raise BusinessRule("COA_REQUIRED", f"COA 가 필요한 원료입니다. COA 문서를 첨부하세요: {m.material_name}")
    r.approved_by = cu.id
    r.approved_at = datetime.now(timezone.utc)
    if r.status == "INSPECTION_PENDING":
        r.status = "ACCEPTED"
    else:
        for lot in _lots_of(db, r):
            if lot.quality_status != "REJECTED":
                _set_lot_quality(db, cu, lot, "PASSED")
    audit(db, cu, "RECEIPT_APPROVE", "RECEIPT", r.id, reason=note)
    return r


def _split(qty: Decimal, count: int, per: Decimal | None) -> list[Decimal]:
    if per:
        return [per] * count
    base = (qty / count).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    parts = [base] * count
    parts[-1] = qty - base * (count - 1)
    return parts


def post(db: Session, cu: CurrentUser, r: MaterialReceipt, idempotency_key: str | None = None) -> dict[str, Any]:
    if r.status not in POSTABLE:
        raise InvalidState(r.status, "입고 확정")
    if not r.items:
        raise BusinessRule("NO_ITEMS", "입고 품목이 없습니다.")
    today = today_seoul()
    accepted = r.status == "ACCEPTED"
    ctx = inv.Ctx(organization_id=cu.organization_id, facility_id=r.facility_id, user_id=cu.id,
                  reference_type="RECEIPT", reference_id=r.id, reason=f"입고 확정 {r.receipt_number}")
    result_lots = []
    for it in r.items:
        m = _check_item_required(db, it)
        if r.status in ("DRAFT", "OCR_REVIEW") and m.inspection_required:
            raise BusinessRule("INSPECTION_REQUIRED", f"검수가 필요한 원료입니다. 검수 요청 후 확정하세요: {m.material_name}")
        if it.expiry_date and it.expiry_date < today:
            raise BusinessRule("EXPIRED_MATERIAL", f"소비/유통기한이 지난 원료는 입고 확정할 수 없습니다: {m.material_name}")
        qty_base = convert(db, Decimal(it.quantity), it.unit_id, m.base_unit_id, material=m)
        if accepted or not m.inspection_required:
            qstatus = "PASSED"
        elif r.status == "HOLD":
            qstatus = "HOLD"
        else:
            qstatus = "PENDING"
        lot_no = next_number(db, cu.organization_id, MAJOR_PREFIX[m.major_category])
        lot = MaterialLot(
            id=uuid.uuid4(), organization_id=cu.organization_id, facility_id=r.facility_id, material_id=m.id, receipt_item_id=it.id,
            lot_number=lot_no, supplier_lot_number=it.supplier_lot_number, supplier_id=r.supplier_id,
            manufacturer_name=it.manufacturer_name, country_of_origin=it.country_of_origin, received_date=today,
            manufactured_date=it.manufactured_date, expiry_date=it.expiry_date, expiry_type=it.expiry_type,
            original_quantity=qty_base, base_unit_id=m.base_unit_id, quality_status=qstatus, created_by=cu.id,
        )
        db.add(lot)
        db.flush()
        lot_node = trace.create_node(db, cu.organization_id, r.facility_id, "RAW_MATERIAL_LOT", lot.id, lot_no, m.material_name)
        lot.trace_node_id = lot_node.id
        lot.qr_code_id = ensure_qr(db, org_id=cu.organization_id, qr_type="RAW_MATERIAL_LOT", entity_id=lot.id, lot_number=lot_no, user_id=cu.id).id
        it.material_lot_id = lot.id
        trace.record_event(db, org_id=cu.organization_id, facility_id=r.facility_id, user_id=cu.id, event_type="RECEIPT",
                           inputs=[], outputs=[trace.NodeRef(lot_node.id, qty_base, m.base_unit_id)],
                           reference_type="RECEIPT", reference_id=r.id)
        per = convert(db, Decimal(it.package_quantity), it.unit_id, m.base_unit_id, material=m) if it.package_quantity else None
        parts = _split(qty_base, it.package_count, per)
        pkg_refs, pkgs_out = [], []
        for seq, pq in enumerate(parts, start=1):
            pkg_no = next_number(db, cu.organization_id, "PKG")
            pkg = MaterialPackage(id=uuid.uuid4(), organization_id=cu.organization_id, material_lot_id=lot.id, package_number=pkg_no,
                                  sequence_no=seq, original_quantity=pq, base_unit_id=m.base_unit_id, created_by=cu.id)
            db.add(pkg)
            db.flush()
            pnode = trace.create_node(db, cu.organization_id, r.facility_id, "RAW_MATERIAL_PACKAGE", pkg.id, pkg_no, m.material_name)
            pkg.trace_node_id = pnode.id
            qr = ensure_qr(db, org_id=cu.organization_id, qr_type="RAW_MATERIAL_PACKAGE", entity_id=pkg.id, lot_number=lot_no, user_id=cu.id)
            pkg.qr_code_id = qr.id
            key = inv.StockKey(item_type="MATERIAL", lot_node_id=lot_node.id, package_node_id=pnode.id, warehouse_id=it.warehouse_id,
                               storage_location_id=it.storage_location_id, unit_id=m.base_unit_id, lot_number=lot_no,
                               package_number=pkg_no, quality_status=qstatus, material_id=m.id, expiry_date=it.expiry_date,
                               material_lot_id=lot.id, package_id=pkg.id)
            mv = inv.receive(db, ctx, key, pq, "RECEIPT",
                             idempotency_key=f"{idempotency_key}:{it.id}:{seq}" if idempotency_key else None)
            pkg_refs.append(trace.NodeRef(pnode.id, pq, m.base_unit_id))
            pkgs_out.append({"package_id": str(pkg.id), "package_number": pkg_no, "quantity": ser_value(pq), "qr_code_id": str(qr.id),
                             "qr_payload": qr.payload, "balance_id": str(mv.balance.id)})
        trace.record_event(db, org_id=cu.organization_id, facility_id=r.facility_id, user_id=cu.id, event_type="SPLIT",
                           inputs=[trace.NodeRef(lot_node.id, qty_base, m.base_unit_id)], outputs=pkg_refs,
                           reference_type="RECEIPT", reference_id=r.id, note="입고 포장 분할")
        # 문서 → LOT 연결 (COA 원본 조회용)
        for link in db.execute(select(DocumentLink).where(DocumentLink.entity_type == "RECEIPT", DocumentLink.entity_id == r.id)).scalars().all():
            db.add(DocumentLink(organization_id=cu.organization_id, document_id=link.document_id, entity_type="MATERIAL_LOT", entity_id=lot.id))
        result_lots.append({"lot_id": str(lot.id), "lot_number": lot_no, "material_id": str(m.id), "material_name": m.material_name,
                            "quantity": ser_value(qty_base), "quality_status": qstatus, "lot_qr_code_id": str(lot.qr_code_id), "packages": pkgs_out})
    r.status = "POSTED"
    r.posted_by = cu.id
    r.posted_at = datetime.now(timezone.utc)
    db.flush()
    audit(db, cu, "RECEIPT_POST", "RECEIPT", r.id, after={"lots": [x["lot_number"] for x in result_lots]})
    return {"receipt_id": str(r.id), "receipt_number": r.receipt_number, "status": r.status, "lots": result_lots}


def reverse(db: Session, cu: CurrentUser, r: MaterialReceipt, reason: str) -> MaterialReceipt:
    if r.status != "POSTED":
        raise InvalidState(r.status, "입고 정정")
    if not reason:
        raise BusinessRule("REASON_REQUIRED", "정정 사유를 입력하세요.")
    ctx = inv.Ctx(organization_id=cu.organization_id, facility_id=r.facility_id, user_id=cu.id, reference_type="RECEIPT_REVERSAL",
                  reference_id=r.id, reason=reason)
    lots = _lots_of(db, r)
    for lot in lots:
        txs = db.execute(select(InventoryTransaction).where(InventoryTransaction.lot_node_id == lot.trace_node_id)).scalars().all()
        if any(t.transaction_type != "RECEIPT" for t in txs):
            raise BusinessRule("LOT_ALREADY_USED", f"이미 사용/이동된 LOT 는 입고 정정할 수 없습니다: {lot.lot_number}. 조정 또는 반품으로 처리하세요.")
    for lot in lots:
        for t in db.execute(select(InventoryTransaction).where(InventoryTransaction.lot_node_id == lot.trace_node_id,
                                                               InventoryTransaction.transaction_type == "RECEIPT")).scalars().all():
            inv.reverse_transaction(db, ctx, t.id, reason)
        lot.status = "CANCELLED"
        for p in db.execute(select(MaterialPackage).where(MaterialPackage.material_lot_id == lot.id)).scalars():
            p.package_status = "CANCELLED"
    for ev in db.execute(select(TraceEvent).where(TraceEvent.reference_type == "RECEIPT", TraceEvent.reference_id == r.id,
                                                  TraceEvent.event_type != "REVERSAL")).scalars().all():
        trace.reverse_event(db, org_id=cu.organization_id, facility_id=r.facility_id, user_id=cu.id, event_id=ev.id, note=reason)
    r.status = "CANCELLED"
    r.status_reason = reason
    audit(db, cu, "RECEIPT_REVERSE", "RECEIPT", r.id, reason=reason)
    return r


def cancel(db: Session, cu: CurrentUser, r: MaterialReceipt, reason: str) -> MaterialReceipt:
    if r.status in ("POSTED", "CANCELLED"):
        raise InvalidState(r.status, "취소")
    r.status = "CANCELLED"
    r.status_reason = reason
    audit(db, cu, "RECEIPT_CANCEL", "RECEIPT", r.id, reason=reason)
    return r


def detail(db: Session, r: MaterialReceipt) -> dict[str, Any]:
    from app.models import FileAttachment, OcrJob
    from app.repositories.common import ser

    items = []
    for it in r.items:
        m = db.get(Material, it.material_id)
        lot = db.get(MaterialLot, it.material_lot_id) if it.material_lot_id else None
        pkgs = []
        if lot:
            for p in db.execute(select(MaterialPackage).where(MaterialPackage.material_lot_id == lot.id).order_by(MaterialPackage.sequence_no)).scalars():
                bal = db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(InventoryBalance.package_node_id == p.trace_node_id)).scalar_one()
                pkgs.append(ser(p, current_quantity=bal))
        items.append(ser(it, material_code=m.material_code, material_name=m.material_name, major_category=m.major_category,
                         lot=ser(lot, current_quantity=inv.lot_total(db, lot.organization_id, lot.trace_node_id)) if lot else None, packages=pkgs))
    docs = []
    for link in db.execute(select(DocumentLink).where(DocumentLink.entity_type == "RECEIPT", DocumentLink.entity_id == r.id)).scalars():
        d = db.get(ReceiptDocument, link.document_id)
        fa = db.get(FileAttachment, d.file_id)
        docs.append(ser(d, file_name=fa.original_name, mime_type=fa.mime_type))
    jobs = [ser(j, exclude=("raw_text",)) for j in db.execute(select(OcrJob).where(OcrJob.receipt_id == r.id).order_by(OcrJob.created_at)).scalars()]
    insps = [ser(q, results=[ser(x) for x in q.results]) for q in db.execute(select(QualityInspection).where(
        QualityInspection.target_type == "RECEIPT", QualityInspection.target_id == r.id).order_by(QualityInspection.inspected_at)).scalars()]
    sup = db.get(Supplier, r.supplier_id) if r.supplier_id else None
    return ser(r, supplier_name=sup.name if sup else None, items=items, documents=docs, ocr_jobs=jobs, inspections=insps)
