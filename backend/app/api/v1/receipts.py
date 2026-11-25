from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.db import get_db
from app.core.deps import CurrentUser, require
from app.core.errors import BusinessRule
from app.core.idempotency import Idem, idempotency
from app.core.responses import ok, paged
from app.models import DocumentLink, MaterialReceipt, MaterialReceiptItem, OcrJob, ReceiptDocument
from app.repositories.common import PageParams, paginate, ser
from app.services import receiving as svc
from app.services.ocr import service as ocr_svc
from app.services.storage import save_file, validate_upload

router = APIRouter(prefix="/receipts", tags=["입고"])

DocType = Literal["COA", "RECEIPT", "TRANSACTION_STATEMENT", "DELIVERY_NOTE", "MATERIAL_LABEL", "OTHER"]


class ItemIn(BaseModel):
    material_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    unit_id: uuid.UUID | None = None
    package_count: int = Field(default=1, ge=1, le=1000)
    package_quantity: Decimal | None = Field(default=None, gt=0)
    package_weight: Decimal | None = None
    supplier_lot_number: str | None = Field(default=None, max_length=100)
    manufacturer_name: str | None = None
    country_of_origin: str | None = None
    manufactured_date: date | None = None
    expiry_date: date | None = None
    expiry_type: Literal["CONSUME_BY", "SELL_BY", "NOT_APPLICABLE"] | None = None
    storage_condition: str | None = None
    warehouse_id: uuid.UUID | None = None
    storage_location_id: uuid.UUID | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    note: str | None = None


class ReceiptIn(BaseModel):
    supplier_id: uuid.UUID | None = None
    purchase_order_number: str | None = None
    receipt_document_number: str | None = None
    coa_number: str | None = None
    note: str | None = None
    items: list[ItemIn] = []


class ReceiptPatch(BaseModel):
    supplier_id: uuid.UUID | None = None
    purchase_order_number: str | None = None
    receipt_document_number: str | None = None
    coa_number: str | None = None
    note: str | None = None
    items: list[ItemIn] | None = None


class InspectionResultIn(BaseModel):
    item_code: str
    item_name: str | None = None
    result: Literal["PASS", "FAIL", "HOLD", "NOT_APPLICABLE"] = "PASS"
    measured_value: Decimal | None = None
    text_value: str | None = None
    standard: str | None = None
    note: str | None = None


class InspectionIn(BaseModel):
    source: Literal["FIELD", "COA"] = "FIELD"
    template_id: uuid.UUID | None = None
    overall_result: Literal["PASS", "FAIL", "HOLD", "NOT_APPLICABLE"]
    results: list[InspectionResultIn] = []
    nonconformance_reason: str | None = None
    disposition: Literal["QUARANTINE", "RETURN", "DISPOSE"] | None = None
    photo_file_ids: list[uuid.UUID] | None = None
    note: str | None = None


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class NoteIn(BaseModel):
    note: str | None = None


@router.get("")
def list_receipts(p: PageParams = Depends(), status: str | None = None, supplier_id: uuid.UUID | None = None,
                  date_from: date | None = None, date_to: date | None = None, material_id: uuid.UUID | None = None,
                  db: Session = Depends(get_db), cu: CurrentUser = Depends(require("receipt:read"))):
    from datetime import datetime, time, timedelta, timezone
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Seoul")
    stmt = select(MaterialReceipt).where(MaterialReceipt.organization_id == cu.organization_id, MaterialReceipt.facility_id == cu.facility_id)
    if status:
        stmt = stmt.where(MaterialReceipt.status.in_(status.split(",")))
    if supplier_id:
        stmt = stmt.where(MaterialReceipt.supplier_id == supplier_id)
    if date_from:
        stmt = stmt.where(MaterialReceipt.received_at >= datetime.combine(date_from, time.min, tz))
    if date_to:
        stmt = stmt.where(MaterialReceipt.received_at < datetime.combine(date_to + timedelta(days=1), time.min, tz))
    if material_id:
        stmt = stmt.where(MaterialReceipt.id.in_(select(MaterialReceiptItem.receipt_id).where(MaterialReceiptItem.material_id == material_id)))
    rows, total = paginate(db, stmt, p, MaterialReceipt, [MaterialReceipt.receipt_number, MaterialReceipt.coa_number,
                                                          MaterialReceipt.purchase_order_number], MaterialReceipt.received_at.desc())
    from app.models import Material, Supplier

    out = []
    for r in rows:
        sup = db.get(Supplier, r.supplier_id) if r.supplier_id else None
        names = [db.get(Material, it.material_id).material_name for it in r.items]
        out.append(ser(r, supplier_name=sup.name if sup else None, item_count=len(r.items),
                       material_names=", ".join(names[:3]) + (f" 외 {len(names) - 3}건" if len(names) > 3 else "")))
    return paged(out, total, p.page, p.size)


@router.post("")
def create_receipt(body: ReceiptIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                   cu: CurrentUser = Depends(require("receipt:write"))):
    if (replay := idem.begin()) is not None:
        return replay
    r = svc.create(db, cu, body.model_dump())
    resp = idem.store(ok(svc.detail(db, r), "입고가 등록되었습니다."))
    db.commit()
    return resp


@router.get("/{receipt_id}")
def get_receipt(receipt_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("receipt:read"))):
    return ok(svc.detail(db, svc.get_receipt(db, cu, receipt_id)))


@router.patch("/{receipt_id}")
def patch_receipt(receipt_id: uuid.UUID, body: ReceiptPatch, db: Session = Depends(get_db),
                  cu: CurrentUser = Depends(require("receipt:write"))):
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    data = body.model_dump(exclude_unset=True)
    if data.get("items") is not None:
        data["items"] = [ItemIn(**x).model_dump() if isinstance(x, dict) else x for x in data["items"]]
    svc.update(db, cu, r, data)
    db.commit()
    return ok(svc.detail(db, r), "수정되었습니다.")


@router.post("/{receipt_id}/documents")
async def upload_documents(
    receipt_id: uuid.UUID, background: BackgroundTasks, files: list[UploadFile] = File(...),
    document_type: DocType = Form("COA"), run_ocr: bool = Form(True), force: bool = Form(False),
    db: Session = Depends(get_db), cu: CurrentUser = Depends(require("receipt:write")),
):
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    if r.status in ("CANCELLED",):
        raise BusinessRule("INVALID_STATE", "취소된 입고에는 문서를 첨부할 수 없습니다.")
    results, jobs = [], []
    for f in files:
        data = await f.read()
        mime = validate_upload(f.filename or "", data)
        h = hashlib.sha256(data).hexdigest()
        dup = db.execute(select(ReceiptDocument).where(ReceiptDocument.organization_id == cu.organization_id,
                                                       ReceiptDocument.file_hash == h).order_by(ReceiptDocument.created_at)).scalars().first()
        fa = save_file(db, org_id=cu.organization_id, facility_id=cu.facility_id, user_id=cu.id, filename=f.filename or "doc",
                       data=data, mime=mime, entity_type="RECEIPT", entity_id=r.id)
        doc = ReceiptDocument(organization_id=cu.organization_id, facility_id=cu.facility_id, document_type=document_type,
                              file_id=fa.id, file_hash=h, is_duplicate_of=dup.id if dup else None, created_by=cu.id)
        db.add(doc)
        db.flush()
        db.add(DocumentLink(organization_id=cu.organization_id, document_id=doc.id, entity_type="RECEIPT", entity_id=r.id))
        job_id = None
        if run_ocr and (not dup or force):
            from app.core.config import settings

            job = OcrJob(organization_id=cu.organization_id, facility_id=cu.facility_id, document_id=doc.id, receipt_id=r.id,
                         provider=settings.OCR_PROVIDER, document_type=document_type, created_by=cu.id)
            db.add(job)
            db.flush()
            job_id = job.id
            jobs.append(job.id)
            if r.status in ("DRAFT", "OCR_REVIEW"):
                r.status = "OCR_PROCESSING"
        results.append({"document_id": str(doc.id), "file_id": str(fa.id), "file_name": fa.original_name, "mime_type": mime,
                        "duplicate_of": str(dup.id) if dup else None,
                        "warning": "이미 등록된 동일 문서입니다. (중복 감지)" if dup else None, "ocr_job_id": str(job_id) if job_id else None})
    audit(db, cu, "RECEIPT_DOCUMENT_UPLOAD", "RECEIPT", r.id, after={"documents": [x["document_id"] for x in results]})
    db.commit()
    for jid in jobs:
        ocr_svc.enqueue(jid, background)
    return ok(results, "문서가 저장되었습니다." + (" OCR 분석을 시작합니다." if jobs else ""))


@router.post("/{receipt_id}/submit-inspection")
def submit_inspection(receipt_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("receipt:write"))):
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    svc.submit_inspection(db, cu, r)
    db.commit()
    return ok(svc.detail(db, r), "검수 요청되었습니다.")


@router.post("/{receipt_id}/inspection")
def inspection(receipt_id: uuid.UUID, body: InspectionIn, db: Session = Depends(get_db),
               cu: CurrentUser = Depends(require("receipt:inspect"))):
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    qi = svc.inspect(db, cu, r, body.model_dump())
    warnings = getattr(qi, "_warnings", [])
    db.commit()
    data = svc.detail(db, r)
    data["warnings"] = warnings
    return ok(data, "검수 결과가 저장되었습니다.")


@router.post("/{receipt_id}/approve")
def approve(receipt_id: uuid.UUID, body: NoteIn | None = None, db: Session = Depends(get_db),
            cu: CurrentUser = Depends(require("receipt:approve"))):
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    svc.approve(db, cu, r, body.note if body else None)
    db.commit()
    return ok(svc.detail(db, r), "품질 승인되었습니다.")


@router.post("/{receipt_id}/post")
def post_receipt(receipt_id: uuid.UUID, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                 cu: CurrentUser = Depends(require("receipt:post"))):
    if (replay := idem.begin()) is not None:
        return replay
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    result = svc.post(db, cu, r, idem.key)
    resp = idem.store(ok(result, "입고가 확정되었습니다. LOT·포장 QR 이 생성되고 재고에 반영되었습니다."))
    db.commit()
    # 자동 인쇄 (설정 시) — 인쇄 실패는 입고 확정에 영향 없음
    from app.services.printing.service import auto_print_after_receipt

    resp["data"]["auto_print"] = auto_print_after_receipt(db, cu, result)
    return resp


@router.post("/{receipt_id}/reverse")
def reverse(receipt_id: uuid.UUID, body: ReasonIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
            cu: CurrentUser = Depends(require("receipt:reverse"))):
    if (replay := idem.begin()) is not None:
        return replay
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    svc.reverse(db, cu, r, body.reason)
    resp = idem.store(ok(svc.detail(db, r), "입고가 정정(역거래) 처리되었습니다."))
    db.commit()
    return resp


@router.post("/{receipt_id}/cancel")
def cancel(receipt_id: uuid.UUID, body: ReasonIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("receipt:write"))):
    r = svc.get_receipt(db, cu, receipt_id, lock=True)
    svc.cancel(db, cu, r, body.reason)
    db.commit()
    return ok(svc.detail(db, r), "입고가 취소되었습니다.")
