"""스캔 해석 및 라벨 데이터. 최신 상태는 항상 서버에서 조회한다."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.numbering import today_seoul
from app.models import (
    Equipment, FinishedGoodsLot, FinishedGoodsPackage, InventoryBalance, Material, MaterialLot, MaterialPackage, Product,
    ProductionOrder, QrCode, StorageLocation, Supplier, TraceNode, Unit, Warehouse, WeighingBatch, WipLot,
)
from app.repositories.common import ser_value
from app.services.qr.service import parse_scan

QUALITY_LABEL = {"PENDING": "검수대기", "PASSED": "합격", "HOLD": "보류", "REJECTED": "부적합", "RELEASED": "출고승인",
                 "QUALITY_PENDING": "품질대기"}
STORAGE_LABEL = {"AMBIENT": "상온", "REFRIGERATED": "냉장", "FROZEN": "냉동"}


def _sum_balance(db: Session, **cond: Any) -> tuple[Decimal, Decimal]:
    stmt = select(func.coalesce(func.sum(InventoryBalance.quantity), 0), func.coalesce(func.sum(InventoryBalance.reserved_quantity), 0))
    for k, v in cond.items():
        stmt = stmt.where(getattr(InventoryBalance, k) == v)
    q, r = db.execute(stmt).one()
    return Decimal(q), Decimal(r)


def _locations(db: Session, **cond: Any) -> list[dict[str, Any]]:
    stmt = select(InventoryBalance).where(InventoryBalance.quantity > 0)
    for k, v in cond.items():
        stmt = stmt.where(getattr(InventoryBalance, k) == v)
    out = []
    for b in db.execute(stmt).scalars():
        wh = db.get(Warehouse, b.warehouse_id)
        loc = db.get(StorageLocation, b.storage_location_id) if b.storage_location_id else None
        out.append({"balance_id": str(b.id), "warehouse_id": str(wh.id), "warehouse_name": wh.name, "warehouse_type": wh.warehouse_type,
                    "location_code": loc.code if loc else None, "quantity": ser_value(b.quantity),
                    "reserved_quantity": ser_value(b.reserved_quantity)})
    return out


def _unit(db: Session, uid: uuid.UUID | None) -> str | None:
    u = db.get(Unit, uid) if uid else None
    return u.code if u else None


def find_qr(db: Session, org_id: uuid.UUID, raw: str) -> QrCode | None:
    p = parse_scan(raw)
    if p["kind"] == "payload":
        qr = db.execute(select(QrCode).where(QrCode.qr_type == p["type"], QrCode.entity_id == uuid.UUID(p["id"]),
                                             QrCode.organization_id == org_id)).scalar_one_or_none()
        return qr
    if p["kind"] == "text":
        v = p["value"]
        # 포장번호(가장 구체적) → LOT 번호 순으로 직접 검색
        for model, col, qtype in [(MaterialPackage, MaterialPackage.package_number, "RAW_MATERIAL_PACKAGE"),
                                  (FinishedGoodsPackage, FinishedGoodsPackage.package_number, "FINISHED_GOODS_PACKAGE"),
                                  (MaterialLot, MaterialLot.lot_number, "RAW_MATERIAL_LOT"),
                                  (WeighingBatch, WeighingBatch.lot_number, "WEIGHING_BATCH"),
                                  (WipLot, WipLot.lot_number, "PROCESS_WIP"),
                                  (FinishedGoodsLot, FinishedGoodsLot.lot_number, "FINISHED_GOODS_LOT"),
                                  (ProductionOrder, ProductionOrder.order_number, "PRODUCTION_ORDER")]:
            ent = db.execute(select(model).where(col == v, model.organization_id == org_id)).scalar_one_or_none()
            if ent:
                return db.execute(select(QrCode).where(QrCode.qr_type == qtype, QrCode.entity_id == ent.id)).scalar_one_or_none()
        try:
            qid = uuid.UUID(v)
            qr = db.get(QrCode, qid)
            return qr if qr and qr.organization_id == org_id else None
        except ValueError:
            return None
    return None


def describe(db: Session, qr: QrCode) -> dict[str, Any]:
    """QR 엔티티 최신 상태 + 라벨 데이터."""
    t, eid = qr.qr_type, qr.entity_id
    today = today_seoul()
    d: dict[str, Any] = {"qr_code_id": str(qr.id), "qr_type": t, "entity_id": str(eid), "payload": qr.payload,
                         "lot_number": qr.lot_number, "company": settings.COMPANY_NAME, "warnings": [], "usable": False}
    if t in ("RAW_MATERIAL_LOT", "RAW_MATERIAL_PACKAGE"):
        pkg = db.get(MaterialPackage, eid) if t == "RAW_MATERIAL_PACKAGE" else None
        lot = db.get(MaterialLot, pkg.material_lot_id if pkg else eid)
        m = db.get(Material, lot.material_id)
        sup = db.get(Supplier, lot.supplier_id) if lot.supplier_id else None
        lot_qty, lot_res = _sum_balance(db, lot_node_id=lot.trace_node_id)
        d.update({
            "name": m.material_name, "code": m.material_code, "material_id": str(m.id), "major_category": m.major_category,
            "material_lot_id": str(lot.id), "lot_node_id": str(lot.trace_node_id), "supplier_lot": lot.supplier_lot_number,
            "supplier_name": sup.name if sup else None, "manufacturer_name": lot.manufacturer_name,
            "country_of_origin": lot.country_of_origin, "manufactured_date": ser_value(lot.manufactured_date),
            "received_date": ser_value(lot.received_date), "expiry_date": ser_value(lot.expiry_date), "expiry_type": lot.expiry_type,
            "quality_status": lot.quality_status, "quality_label": QUALITY_LABEL.get(lot.quality_status),
            "storage_condition": STORAGE_LABEL.get(m.storage_condition or "", m.storage_condition), "unit": _unit(db, lot.base_unit_id),
            "lot_original_quantity": ser_value(lot.original_quantity), "lot_quantity": ser_value(lot_qty), "lot_status": lot.status,
        })
        if pkg:
            pq, pr = _sum_balance(db, package_node_id=pkg.trace_node_id)
            d.update({"package_id": str(pkg.id), "package_number": pkg.package_number, "package_node_id": str(pkg.trace_node_id),
                      "package_status": pkg.package_status, "quantity": ser_value(pkg.original_quantity),
                      "original_quantity": ser_value(pkg.original_quantity), "current_quantity": ser_value(pq),
                      "reserved_quantity": ser_value(pr), "locations": _locations(db, package_node_id=pkg.trace_node_id)})
        else:
            d.update({"quantity": ser_value(lot.original_quantity), "current_quantity": ser_value(lot_qty),
                      "locations": _locations(db, lot_node_id=lot.trace_node_id)})
        usable = lot.quality_status == "PASSED" and lot.status == "ACTIVE"
        if lot.quality_status != "PASSED":
            d["warnings"].append(f"품질 상태: {QUALITY_LABEL.get(lot.quality_status, lot.quality_status)}")
        if lot.expiry_date and lot.expiry_date < today:
            usable = False
            d["warnings"].append("소비/유통기한 경과")
        if pkg and pkg.package_status in ("EMPTY", "DAMAGED", "CONTAMINATED", "CANCELLED"):
            usable = False
            d["warnings"].append(f"사용 불가 포장 ({pkg.package_status})")
        d["usable"] = usable
    elif t == "WEIGHING_BATCH":
        wb = db.get(WeighingBatch, eid)
        p = db.get(Product, wb.product_id)
        o = db.get(ProductionOrder, wb.production_order_id)
        q, _ = _sum_balance(db, lot_node_id=wb.trace_node_id) if wb.trace_node_id else (Decimal(0), Decimal(0))
        from app.models import WeighingRecord

        recs = db.execute(select(WeighingRecord).where(WeighingRecord.weighing_batch_id == wb.id, WeighingRecord.status == "CONFIRMED")).scalars().all()
        mats = []
        for rec in recs:
            m = db.get(Material, rec.material_id)
            lot = db.get(MaterialLot, rec.material_lot_id)
            mats.append({"material_code": m.material_code, "material_name": m.material_name, "lot_number": lot.lot_number,
                         "quantity": ser_value(rec.actual_quantity)})
        d.update({"name": f"{p.product_name} 계량반제품", "code": p.product_code, "product_id": str(p.id),
                  "production_order_id": str(o.id), "order_number": o.order_number, "production_batch_id": str(wb.production_batch_id),
                  "weighing_group": wb.weighing_group, "quantity": ser_value(wb.total_quantity), "current_quantity": ser_value(q),
                  "unit": _unit(db, wb.unit_id), "status": wb.status, "quality_status": wb.quality_status,
                  "quality_label": QUALITY_LABEL.get(wb.quality_status), "lot_node_id": str(wb.trace_node_id) if wb.trace_node_id else None,
                  "completed_at": ser_value(wb.completed_at), "usable_until": ser_value(wb.usable_until), "materials": mats,
                  "next_process_code": wb.next_process_code, "manufactured_date": ser_value(wb.completed_at.date()) if wb.completed_at else None})
        d["usable"] = wb.status in ("READY", "PARTIALLY_CONSUMED") and wb.quality_status == "PASSED" and q > 0
        if wb.status in ("CONSUMED", "DISCARDED", "CANCELLED"):
            d["warnings"].append(f"사용 불가 상태: {wb.status} (재투입 금지)")
    elif t == "PROCESS_WIP":
        w = db.get(WipLot, eid)
        p = db.get(Product, w.product_id)
        o = db.get(ProductionOrder, w.production_order_id)
        q, _ = _sum_balance(db, lot_node_id=w.trace_node_id)
        d.update({"name": f"{p.product_name} 공정반제품", "code": p.product_code, "product_id": str(p.id), "production_order_id": str(o.id),
                  "order_number": o.order_number, "production_batch_id": str(w.production_batch_id) if w.production_batch_id else None,
                  "quantity": ser_value(w.quantity), "current_quantity": ser_value(q), "unit": _unit(db, w.unit_id), "status": w.status,
                  "quality_status": w.quality_status, "quality_label": QUALITY_LABEL.get(w.quality_status), "lot_node_id": str(w.trace_node_id),
                  "routing_step_sequence": w.routing_step_sequence, "manufactured_date": ser_value(w.created_at.date())})
        d["usable"] = w.status in ("READY", "PARTIALLY_CONSUMED") and w.quality_status == "PASSED" and q > 0
        if w.status in ("CONSUMED", "DISCARDED"):
            d["warnings"].append(f"사용 불가 상태: {w.status} (재투입 금지)")
    elif t in ("FINISHED_GOODS_LOT", "FINISHED_GOODS_PACKAGE"):
        pkg = db.get(FinishedGoodsPackage, eid) if t == "FINISHED_GOODS_PACKAGE" else None
        fg = db.get(FinishedGoodsLot, pkg.fg_lot_id if pkg else eid)
        p = db.get(Product, fg.product_id)
        q, r = _sum_balance(db, lot_node_id=fg.trace_node_id)
        d.update({"name": p.product_name, "code": p.product_code, "product_id": str(p.id), "fg_lot_id": str(fg.id),
                  "lot_node_id": str(fg.trace_node_id), "manufactured_date": ser_value(fg.manufactured_date),
                  "expiry_date": ser_value(fg.expiry_date), "quality_status": fg.quality_status,
                  "quality_label": QUALITY_LABEL.get(fg.quality_status), "unit": _unit(db, fg.base_unit_id),
                  "quantity": ser_value(pkg.quantity if pkg else fg.good_quantity), "current_quantity": ser_value(q),
                  "storage_condition": STORAGE_LABEL.get(p.storage_condition or "", p.storage_condition),
                  "locations": _locations(db, lot_node_id=fg.trace_node_id)})
        if pkg:
            d.update({"package_id": str(pkg.id), "package_number": pkg.package_number, "package_level": pkg.package_level,
                      "package_status": pkg.status})
        d["usable"] = fg.quality_status == "RELEASED" and (not fg.expiry_date or fg.expiry_date >= today) and q > 0
        if fg.quality_status != "RELEASED":
            d["warnings"].append("품질 승인 전 — 출고 불가")
    elif t == "PRODUCTION_ORDER":
        o = db.get(ProductionOrder, eid)
        p = db.get(Product, o.product_id)
        d.update({"name": p.product_name, "code": p.product_code, "order_number": o.order_number, "status": o.status,
                  "production_order_id": str(o.id), "quantity": ser_value(o.planned_quantity), "unit": _unit(db, o.planned_unit_id),
                  "manufactured_date": ser_value(o.production_date)})
        d["usable"] = o.status not in ("CLOSED", "CANCELLED")
    elif t == "EQUIPMENT":
        e = db.get(Equipment, eid)
        d.update({"name": e.name, "code": e.code, "equipment_type": e.equipment_type})
        d["usable"] = e.is_active
    elif t == "STORAGE_LOCATION":
        loc = db.get(StorageLocation, eid)
        wh = db.get(Warehouse, loc.warehouse_id)
        d.update({"name": f"{wh.name} {loc.code}", "code": loc.code, "warehouse_id": str(wh.id)})
        d["usable"] = loc.is_active
    return d


def search_nodes(db: Session, org_id: uuid.UUID, q: str, node_type: str | None = None, limit: int = 50) -> list[TraceNode]:
    like = f"%{q.strip()}%"
    stmt = select(TraceNode).where(TraceNode.organization_id == org_id, or_(TraceNode.lot_number.ilike(like), TraceNode.label.ilike(like)))
    if node_type:
        stmt = stmt.where(TraceNode.node_type == node_type)
    nodes = list(db.execute(stmt.order_by(TraceNode.created_at.desc()).limit(limit)).scalars())
    # 공급업체 LOT 로도 검색
    lots = db.execute(select(MaterialLot).where(MaterialLot.organization_id == org_id, MaterialLot.supplier_lot_number.ilike(like)).limit(limit)).scalars()
    ids = {n.id for n in nodes}
    for lot in lots:
        if lot.trace_node_id and lot.trace_node_id not in ids:
            n = db.get(TraceNode, lot.trace_node_id)
            if n:
                nodes.append(n)
    return nodes
