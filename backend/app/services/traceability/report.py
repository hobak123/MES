"""추적 결과 보강: 노드별 상세(공급업체/원산지/COA/검수, 재고, 출고/거래처, 공정/작업자/설비)."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.models import (
    Customer, DocumentLink, Equipment, FileAttachment, FinishedGoodsLot, FinishedGoodsPackage, InventoryBalance, Material,
    MaterialLot, MaterialPackage, ProcessDefinition, ProcessExecution, Product, ProductionOrder, QualityInspection,
    ReceiptDocument, ShipmentHeader, ShipmentItem, Supplier, TraceNode, User, WeighingBatch, WeighingRecord, WipLot,
)
from app.repositories.common import ser_value
from app.services.traceability import service as trace


def resolve_node(db: Session, org_id: uuid.UUID, key: str) -> TraceNode:
    try:
        kid = uuid.UUID(key)
        n = db.get(TraceNode, kid)
        if n and n.organization_id == org_id:
            return n
        n = db.execute(select(TraceNode).where(TraceNode.entity_id == kid, TraceNode.organization_id == org_id)).scalars().first()
        if n:
            return n
    except ValueError:
        pass
    # 포장번호가 LOT 번호보다 구체적이므로 정확 일치 우선, 그 다음 LOT 번호
    n = db.execute(select(TraceNode).where(TraceNode.organization_id == org_id, TraceNode.lot_number == key)
                   .order_by(TraceNode.created_at)).scalars().all()
    if n:
        lot_like = [x for x in n if x.node_type in ("RAW_MATERIAL_LOT", "WEIGHING_BATCH", "PROCESS_WIP", "FINISHED_GOODS_LOT", "SHIPMENT")]
        return lot_like[0] if lot_like else n[0]
    pkg = db.execute(select(MaterialPackage).where(MaterialPackage.organization_id == org_id, MaterialPackage.package_number == key)).scalar_one_or_none()
    if pkg and pkg.trace_node_id:
        return db.get(TraceNode, pkg.trace_node_id)
    lot = db.execute(select(MaterialLot).where(MaterialLot.organization_id == org_id, MaterialLot.supplier_lot_number == key)).scalars().first()
    if lot and lot.trace_node_id:
        return db.get(TraceNode, lot.trace_node_id)
    raise NotFound("추적 대상 LOT")


def _stock(db: Session, node: TraceNode) -> Decimal:
    col = InventoryBalance.package_node_id if node.node_type in ("RAW_MATERIAL_PACKAGE",) else InventoryBalance.lot_node_id
    return Decimal(db.execute(select(func.coalesce(func.sum(InventoryBalance.quantity), 0)).where(col == node.id)).scalar_one())


def node_detail(db: Session, node: TraceNode) -> dict[str, Any]:
    t, eid = node.node_type, node.entity_id
    d: dict[str, Any] = {}
    if t in ("RAW_MATERIAL_LOT", "RAW_MATERIAL_PACKAGE"):
        pkg = db.get(MaterialPackage, eid) if t == "RAW_MATERIAL_PACKAGE" else None
        lot = db.get(MaterialLot, pkg.material_lot_id if pkg else eid)
        m = db.get(Material, lot.material_id)
        sup = db.get(Supplier, lot.supplier_id) if lot.supplier_id else None
        d.update({"material_code": m.material_code, "material_name": m.material_name, "major_category": m.major_category,
                  "supplier_name": sup.name if sup else None, "supplier_lot": lot.supplier_lot_number,
                  "manufacturer_name": lot.manufacturer_name, "country_of_origin": lot.country_of_origin,
                  "received_date": ser_value(lot.received_date), "manufactured_date": ser_value(lot.manufactured_date),
                  "expiry_date": ser_value(lot.expiry_date), "quality_status": lot.quality_status,
                  "original_quantity": ser_value(pkg.original_quantity if pkg else lot.original_quantity), "unit": m.base_unit.code})
        if t == "RAW_MATERIAL_LOT":
            docs = []
            for link in db.execute(select(DocumentLink).where(DocumentLink.entity_type == "MATERIAL_LOT", DocumentLink.entity_id == lot.id)).scalars():
                doc = db.get(ReceiptDocument, link.document_id)
                fa = db.get(FileAttachment, doc.file_id)
                docs.append({"document_id": str(doc.id), "document_type": doc.document_type, "document_number": doc.document_number,
                             "issued_date": ser_value(doc.issued_date), "file_url": f"/api/v1/files/{fa.id}", "file_name": fa.original_name,
                             "coa_test_results": doc.coa_test_results})
            d["documents"] = docs
            from app.models import MaterialReceiptItem

            item = db.get(MaterialReceiptItem, lot.receipt_item_id) if lot.receipt_item_id else None
            if item:
                d["receipt_id"] = str(item.receipt_id)
                d["inspections"] = [{"inspection_number": q.inspection_number, "source": q.source, "result": q.overall_result,
                                     "inspected_at": ser_value(q.inspected_at)} for q in db.execute(select(QualityInspection).where(
                    QualityInspection.target_type == "RECEIPT", QualityInspection.target_id == item.receipt_id)).scalars()]
            d["used_quantity"] = ser_value(Decimal(db.execute(select(func.coalesce(func.sum(WeighingRecord.actual_quantity), 0)).where(
                WeighingRecord.material_lot_id == lot.id, WeighingRecord.status == "CONFIRMED")).scalar_one()))
    elif t == "WEIGHING_BATCH":
        wb = db.get(WeighingBatch, eid)
        o = db.get(ProductionOrder, wb.production_order_id)
        recs = []
        for r in db.execute(select(WeighingRecord).where(WeighingRecord.weighing_batch_id == wb.id, WeighingRecord.status == "CONFIRMED")).scalars():
            m = db.get(Material, r.material_id)
            u = db.get(User, r.weighed_by)
            recs.append({"material_name": m.material_name, "lot_number": db.get(MaterialLot, r.material_lot_id).lot_number,
                         "package_number": db.get(MaterialPackage, r.material_package_id).package_number,
                         "actual_quantity": ser_value(r.actual_quantity), "weighed_by": u.full_name if u else None,
                         "weighed_at": ser_value(r.weighed_at)})
        d.update({"order_number": o.order_number, "status": wb.status, "total_quantity": ser_value(wb.total_quantity),
                  "bom_version_no": o.bom_snapshot.get("version_no"), "weighing_records": recs, "completed_at": ser_value(wb.completed_at)})
    elif t == "PROCESS_WIP":
        w = db.get(WipLot, eid)
        e = db.get(ProcessExecution, w.process_execution_id)
        d.update(_exec_info(db, e))
        d.update({"status": w.status, "quantity": ser_value(w.quantity), "quality_status": w.quality_status})
    elif t in ("FINISHED_GOODS_LOT", "FINISHED_GOODS_PACKAGE"):
        fpk = db.get(FinishedGoodsPackage, eid) if t == "FINISHED_GOODS_PACKAGE" else None
        fg = db.get(FinishedGoodsLot, fpk.fg_lot_id if fpk else eid)
        p = db.get(Product, fg.product_id)
        o = db.get(ProductionOrder, fg.production_order_id)
        shipped = Decimal(db.execute(select(func.coalesce(func.sum(ShipmentItem.quantity - ShipmentItem.returned_quantity), 0))
                                     .join(ShipmentHeader, ShipmentHeader.id == ShipmentItem.shipment_id)
                                     .where(ShipmentItem.fg_lot_id == fg.id, ShipmentHeader.status == "CONFIRMED")).scalar_one())
        d.update({"product_code": p.product_code, "product_name": p.product_name, "order_number": o.order_number,
                  "bom_version_no": o.bom_snapshot.get("version_no"), "manufactured_date": ser_value(fg.manufactured_date),
                  "expiry_date": ser_value(fg.expiry_date), "produced_quantity": ser_value(fg.good_quantity),
                  "net_weight": ser_value(fg.net_weight), "production_yield": ser_value(fg.production_yield),
                  "quality_status": fg.quality_status, "shipped_quantity": ser_value(shipped), "unit": p.base_unit.code})
        if fg.process_execution_id:
            d.update(_exec_info(db, db.get(ProcessExecution, fg.process_execution_id)))
    elif t == "SHIPMENT":
        sh = db.get(ShipmentHeader, eid)
        c = db.get(Customer, sh.customer_id)
        d.update({"shipment_number": sh.shipment_number, "shipment_date": ser_value(sh.shipment_date), "customer_name": c.name,
                  "status": sh.status})
    elif t == "CUSTOMER":
        c = db.get(Customer, eid)
        d.update({"customer_code": c.code, "customer_name": c.name, "contact_name": c.contact_name, "phone": c.phone,
                  "delivery_address": c.delivery_address})
    if t not in ("SHIPMENT", "CUSTOMER"):
        d["current_stock"] = ser_value(_stock(db, node))
    return d


def _exec_info(db: Session, e: ProcessExecution) -> dict[str, Any]:
    pd = db.get(ProcessDefinition, e.process_definition_id)
    op = db.get(User, e.operated_by) if e.operated_by else None
    eq = db.get(Equipment, e.equipment_id) if e.equipment_id else None
    return {"process_name": pd.name, "execution_number": e.execution_number, "operator": op.full_name if op else None,
            "equipment": eq.name if eq else None, "started_at": ser_value(e.started_at), "completed_at": ser_value(e.completed_at),
            "process_input": ser_value(e.total_input_quantity), "process_output": ser_value(e.good_output_quantity),
            "process_loss": ser_value(e.process_loss_quantity), "process_yield": ser_value(e.process_yield)}


def _with_details(db: Session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        nid = str(r["node_id"])
        if nid in seen:
            continue
        node = db.get(TraceNode, r["node_id"])
        seen[nid] = {"node_id": nid, "node_type": r["node_type"], "entity_id": str(r["entity_id"]), "lot_number": r["lot_number"],
                     "label": r["label"], "depth": r["depth"], "via_event": r["event_type"],
                     "quantity": ser_value(r["out_qty"]) if r["out_qty"] is not None else None, "detail": node_detail(db, node)}
    return list(seen.values())


def forward_report(db: Session, org_id: uuid.UUID, node: TraceNode) -> dict[str, Any]:
    nodes = _with_details(db, trace.forward(db, org_id, node.id))
    fgs = [n for n in nodes if n["node_type"] == "FINISHED_GOODS_LOT"]
    shipments = [n for n in nodes if n["node_type"] == "SHIPMENT"]
    customers = [n for n in nodes if n["node_type"] == "CUSTOMER"]
    summary: dict[str, Any] = {
        "affected_wb": [n["lot_number"] for n in nodes if n["node_type"] == "WEIGHING_BATCH"],
        "affected_wip": [n["lot_number"] for n in nodes if n["node_type"] == "PROCESS_WIP"],
        "affected_fg": [{"lot_number": n["lot_number"], "produced": n["detail"].get("produced_quantity"),
                         "stock": n["detail"].get("current_stock"), "shipped": n["detail"].get("shipped_quantity")} for n in fgs],
        "shipments": [{"shipment_number": n["detail"].get("shipment_number"), "date": n["detail"].get("shipment_date"),
                       "customer": n["detail"].get("customer_name"), "quantity": n["quantity"]} for n in shipments],
        "customers": sorted({n["detail"].get("customer_name") for n in customers if n["detail"].get("customer_name")}),
        "fg_produced_total": ser_value(sum((Decimal(n["detail"].get("produced_quantity") or 0) for n in fgs), Decimal(0))),
        "fg_stock_total": ser_value(sum((Decimal(n["detail"].get("current_stock") or 0) for n in fgs), Decimal(0))),
        "fg_shipped_total": ser_value(sum((Decimal(n["detail"].get("shipped_quantity") or 0) for n in fgs), Decimal(0))),
    }
    root = nodes[0]["detail"] if nodes else {}
    if node.node_type == "RAW_MATERIAL_LOT":
        summary.update({"received": root.get("original_quantity"), "used": root.get("used_quantity"), "remaining": root.get("current_stock")})
    return {"root": nodes[0] if nodes else None, "nodes": nodes, "summary": summary}


def backward_report(db: Session, org_id: uuid.UUID, node: TraceNode) -> dict[str, Any]:
    nodes = _with_details(db, trace.backward(db, org_id, node.id))
    lots = [n for n in nodes if n["node_type"] == "RAW_MATERIAL_LOT"]
    raw = []
    root = db.get(TraceNode, node.id)
    # 실측: 계량 기록 / 비례 추정: FG 수량 비율
    fg_share = None
    if root.node_type == "FINISHED_GOODS_LOT":
        fg = db.get(FinishedGoodsLot, root.entity_id)
        fg_share = {"good_quantity": ser_value(fg.good_quantity)}
    for n in lots:
        lot_id = uuid.UUID(n["entity_id"])
        used_in_scope = Decimal(0)
        wb_ids = [uuid.UUID(x["entity_id"]) for x in nodes if x["node_type"] == "WEIGHING_BATCH"]
        if wb_ids:
            used_in_scope = Decimal(db.execute(select(func.coalesce(func.sum(WeighingRecord.actual_quantity), 0)).where(
                WeighingRecord.material_lot_id == lot_id, WeighingRecord.weighing_batch_id.in_(wb_ids),
                WeighingRecord.status == "CONFIRMED")).scalar_one())
        pkg_used = Decimal(0)
        for x in nodes:
            if x["node_type"] == "RAW_MATERIAL_PACKAGE" and x["detail"].get("material_code") == n["detail"].get("material_code") \
                    and x["via_event"] == "PACKAGING" and x["quantity"]:
                pass
        raw.append({"lot_number": n["lot_number"], **{k: n["detail"].get(k) for k in (
            "material_code", "material_name", "major_category", "supplier_name", "supplier_lot", "manufacturer_name",
            "country_of_origin", "received_date", "manufactured_date", "expiry_date", "quality_status", "documents", "inspections")},
            "measured_used_quantity": ser_value(used_in_scope) if used_in_scope else None})
    from app.models import PackagingConsumption

    packaging = []
    if root.node_type == "FINISHED_GOODS_LOT":
        for pc in db.execute(select(PackagingConsumption).where(PackagingConsumption.fg_lot_id == root.entity_id)).scalars():
            m = db.get(Material, pc.material_id)
            packaging.append({"material_name": m.material_name, "lot_number": db.get(MaterialLot, pc.material_lot_id).lot_number,
                              "package_number": db.get(MaterialPackage, pc.material_package_id).package_number,
                              "used": ser_value(pc.used_quantity), "defective": ser_value(pc.defective_quantity),
                              "issued": ser_value(pc.issued_quantity)})
    processes = [n["detail"] for n in nodes if n["node_type"] in ("PROCESS_WIP", "FINISHED_GOODS_LOT") and n["detail"].get("process_name")]
    weighing = [n["detail"] for n in nodes if n["node_type"] == "WEIGHING_BATCH"]
    return {"root": nodes[0] if nodes else None, "nodes": nodes,
            "summary": {"raw_materials": [r for r in raw if r["major_category"] == "MAIN_MATERIAL"],
                        "packaging_lots": [r for r in raw if r["major_category"] != "MAIN_MATERIAL"],
                        "packaging_consumption": packaging, "processes": processes, "weighing_batches": weighing,
                        "fg_basis": fg_share,
                        "note": "원료 사용량은 계량 실측값(배치 단위)입니다. 개별 제품 단위 원료량은 배치 실측량 × (제품 수량 / 배치 총 생산량)으로 비례 추정됩니다."}}
