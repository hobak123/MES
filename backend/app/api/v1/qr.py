from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import CurrentUser, require
from app.core.errors import BusinessRule, NotFound
from app.core.responses import ok
from app.models import (
    Equipment, FinishedGoodsLot, FinishedGoodsPackage, MaterialLot, MaterialPackage, ProductionOrder, QrCode, QrPrintLog,
    StorageLocation, WeighingBatch, WipLot,
)
from app.repositories.common import ser
from app.services.printing import service as psvc
from app.services.printing.labels import label_tspl, label_zpl, parse_size
from app.services.qr.resolver import describe, find_qr
from app.services.qr.service import ensure_qr, parse_scan, qr_png

router = APIRouter(prefix="/qr", tags=["QR"])

ENTITY = {
    "RAW_MATERIAL_LOT": (MaterialLot, "lot_number"), "RAW_MATERIAL_PACKAGE": (MaterialPackage, "package_number"),
    "WEIGHING_BATCH": (WeighingBatch, "lot_number"), "PROCESS_WIP": (WipLot, "lot_number"),
    "FINISHED_GOODS_LOT": (FinishedGoodsLot, "lot_number"), "FINISHED_GOODS_PACKAGE": (FinishedGoodsPackage, "package_number"),
    "EQUIPMENT": (Equipment, "code"), "STORAGE_LOCATION": (StorageLocation, "code"), "PRODUCTION_ORDER": (ProductionOrder, "order_number"),
}


class GenerateIn(BaseModel):
    qr_type: Literal["RAW_MATERIAL_LOT", "RAW_MATERIAL_PACKAGE", "WEIGHING_BATCH", "PROCESS_WIP", "FINISHED_GOODS_LOT",
                     "FINISHED_GOODS_PACKAGE", "EQUIPMENT", "STORAGE_LOCATION", "PRODUCTION_ORDER"]
    entity_id: uuid.UUID


class ResolveIn(BaseModel):
    raw: str = Field(min_length=1, max_length=1000)


class PrintIn(BaseModel):
    printer_id: uuid.UUID | None = None
    size: str | None = None
    copies: int = Field(default=1, ge=1, le=100)
    reason: str | None = None


class BatchLabelIn(BaseModel):
    qr_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    size: str | None = None


@router.post("/generate")
def generate(body: GenerateIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:print"))):
    model, col = ENTITY[body.qr_type]
    ent = db.get(model, body.entity_id)
    if not ent or ent.organization_id != cu.organization_id:
        raise NotFound("대상")
    lot = getattr(ent, col)
    if body.qr_type == "RAW_MATERIAL_PACKAGE":
        lot = db.get(MaterialLot, ent.material_lot_id).lot_number
    if not lot:
        raise BusinessRule("NOT_READY", "LOT 번호가 발급되지 않은 대상입니다.")
    qr = ensure_qr(db, org_id=cu.organization_id, qr_type=body.qr_type, entity_id=ent.id, lot_number=lot, user_id=cu.id)
    db.commit()
    return ok(ser(qr))


@router.post("/resolve")
def resolve(body: ResolveIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:read"))):
    parsed = parse_scan(body.raw)
    if parsed["kind"] == "invalid":
        raise BusinessRule("INVALID_QR", "인식할 수 없는 QR 입니다. 다시 스캔하거나 LOT 번호로 직접 검색하세요.")
    qr = find_qr(db, cu.organization_id, body.raw)
    if not qr:
        raise BusinessRule("QR_NOT_FOUND", "등록되지 않은 QR/LOT 입니다. 다시 스캔하거나 LOT 번호를 확인하세요.")
    return ok(describe(db, qr))


@router.post("/labels/batch")
def batch_labels(body: BatchLabelIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:print"))):
    qrs = [psvc.get_qr(db, cu.organization_id, i) for i in body.qr_ids]
    try:
        parse_size(body.size)
    except ValueError as e:
        raise BusinessRule("INVALID_LABEL_SIZE", str(e))
    pdf = psvc.labels_pdf(db, qrs, body.size)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="labels.pdf"'})


@router.get("/{qr_id}")
def get_qr(qr_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:read"))):
    qr = psvc.get_qr(db, cu.organization_id, qr_id)
    return ok({**ser(qr), "detail": describe(db, qr)})


@router.get("/{qr_id}/image")
def qr_image(qr_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:read"))):
    qr = psvc.get_qr(db, cu.organization_id, qr_id)
    return Response(qr_png(qr.payload), media_type="image/png")


@router.get("/{qr_id}/label")
def qr_label(qr_id: uuid.UUID, size: str = Query("80x50"), format: Literal["pdf", "zpl", "tspl"] = "pdf",
             db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:read"))):
    qr = psvc.get_qr(db, cu.organization_id, qr_id)
    try:
        parse_size(size)
    except ValueError as e:
        raise BusinessRule("INVALID_LABEL_SIZE", str(e))
    if format == "pdf":
        return Response(psvc.labels_pdf(db, [qr], size), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="label-{qr.lot_number}.pdf"'})
    d = describe(db, qr)
    text = label_zpl(d, size) if format == "zpl" else label_tspl(d, size)
    return Response(text, media_type="text/plain; charset=utf-8")


@router.post("/{qr_id}/print")
def print_label(qr_id: uuid.UUID, body: PrintIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:print"))):
    qr = psvc.get_qr(db, cu.organization_id, qr_id)
    res = psvc.print_qr(db, cu, qr, printer_id=body.printer_id, size=body.size, copies=body.copies)
    db.commit()
    msg = "라벨 출력 요청이 완료되었습니다." if res["status"] != "FAILED" else "프린터 출력에 실패했습니다. PDF 로 출력하세요. (입고/LOT 는 유지됩니다)"
    return ok(res, msg)


@router.post("/{qr_id}/reprint")
def reprint_label(qr_id: uuid.UUID, body: PrintIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:reprint"))):
    qr = psvc.get_qr(db, cu.organization_id, qr_id)
    res = psvc.print_qr(db, cu, qr, printer_id=body.printer_id, size=body.size, copies=body.copies, reprint=True, reason=body.reason)
    db.commit()
    return ok(res, "동일 QR 로 재출력되었습니다." if res["status"] != "FAILED" else "재출력에 실패했습니다. PDF 로 출력하세요.")


@router.get("/{qr_id}/print-logs")
def print_logs(qr_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("qr:read"))):
    psvc.get_qr(db, cu.organization_id, qr_id)
    rows = db.execute(select(QrPrintLog).where(QrPrintLog.qr_code_id == qr_id).order_by(QrPrintLog.printed_at)).scalars()
    return ok([ser(r) for r in rows])
