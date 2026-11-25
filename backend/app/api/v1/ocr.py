from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import BusinessRule, Forbidden, InvalidState, NotFound
from app.core.responses import ok
from app.models import (
    DocumentLink, FileAttachment, Material, MaterialReceipt, MaterialReceiptItem, OcrExtractedField, OcrJob,
    QualityInspection, QualityInspectionResult, ReceiptDocument,
)
from app.core.numbering import next_number
from app.repositories.common import ser
from app.services.ocr import service as ocr_svc
from app.services.ocr.parser import normalize_date
from app.services.storage import read_file, save_file, validate_upload

router = APIRouter(tags=["OCR"])

DocType = Literal["COA", "RECEIPT", "TRANSACTION_STATEMENT", "DELIVERY_NOTE", "MATERIAL_LABEL", "OTHER"]


def _job(db: Session, cu: CurrentUser, job_id: uuid.UUID) -> OcrJob:
    j = db.get(OcrJob, job_id)
    if not j or j.organization_id != cu.organization_id:
        raise NotFound("OCR 작업")
    cu.require_facility(j.facility_id)
    return j


@router.post("/ocr/upload")
async def upload(background: BackgroundTasks, file: UploadFile = File(...), document_type: DocType = Form("COA"),
                 receipt_id: uuid.UUID | None = Form(None), db: Session = Depends(get_db), cu: CurrentUser = Depends(require("ocr:use"))):
    data = await file.read()
    mime = validate_upload(file.filename or "", data)
    h = hashlib.sha256(data).hexdigest()
    dup = db.execute(select(ReceiptDocument).where(ReceiptDocument.organization_id == cu.organization_id, ReceiptDocument.file_hash == h)).scalars().first()
    receipt = None
    if receipt_id:
        receipt = db.get(MaterialReceipt, receipt_id)
        if not receipt or receipt.organization_id != cu.organization_id:
            raise NotFound("입고")
    fa = save_file(db, org_id=cu.organization_id, facility_id=cu.facility_id, user_id=cu.id, filename=file.filename or "doc", data=data,
                   mime=mime, entity_type="OCR")
    doc = ReceiptDocument(organization_id=cu.organization_id, facility_id=cu.facility_id, document_type=document_type, file_id=fa.id,
                          file_hash=h, is_duplicate_of=dup.id if dup else None, created_by=cu.id)
    db.add(doc)
    db.flush()
    if receipt:
        db.add(DocumentLink(organization_id=cu.organization_id, document_id=doc.id, entity_type="RECEIPT", entity_id=receipt.id))
        if receipt.status in ("DRAFT", "OCR_REVIEW"):
            receipt.status = "OCR_PROCESSING"
    job = OcrJob(organization_id=cu.organization_id, facility_id=cu.facility_id, document_id=doc.id, receipt_id=receipt_id,
                 provider=settings.OCR_PROVIDER, document_type=document_type, created_by=cu.id)
    db.add(job)
    db.commit()
    mode = ocr_svc.enqueue(job.id, background)
    return ok({"job_id": str(job.id), "document_id": str(doc.id), "status": job.status, "mode": mode,
               "duplicate_of": str(dup.id) if dup else None, "warning": "이미 등록된 동일 문서입니다." if dup else None},
              "업로드되었습니다. OCR 분석을 진행합니다.")


class JobIn(BaseModel):
    document_id: uuid.UUID
    receipt_id: uuid.UUID | None = None
    document_type: DocType | None = None


@router.post("/ocr/jobs")
def create_job(body: JobIn, background: BackgroundTasks, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("ocr:use"))):
    doc = db.get(ReceiptDocument, body.document_id)
    if not doc or doc.organization_id != cu.organization_id:
        raise NotFound("문서")
    job = OcrJob(organization_id=cu.organization_id, facility_id=cu.facility_id, document_id=doc.id, receipt_id=body.receipt_id,
                 provider=settings.OCR_PROVIDER, document_type=body.document_type or doc.document_type, created_by=cu.id)
    db.add(job)
    db.commit()
    ocr_svc.enqueue(job.id, background)
    return ok(ser(job, exclude=("raw_text",)))


@router.get("/ocr/jobs/{job_id}")
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("ocr:use"))):
    j = _job(db, cu, job_id)
    progress = {"QUEUED": 10, "PROCESSING": 50, "REVIEW_REQUIRED": 100, "COMPLETED": 100, "CONFIRMED": 100, "FAILED": 100}[j.status]
    return ok(ser(j, exclude=("raw_text", "structured_result"), progress=progress))


@router.get("/ocr/jobs/{job_id}/result")
def get_result(job_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("ocr:use"))):
    j = _job(db, cu, job_id)
    fields = db.execute(select(OcrExtractedField).where(OcrExtractedField.ocr_job_id == j.id)
                        .order_by(OcrExtractedField.item_index, OcrExtractedField.field_name)).scalars().all()
    doc = db.get(ReceiptDocument, j.document_id)
    fa = db.get(FileAttachment, doc.file_id)
    return ok({
        "job": ser(j, exclude=("structured_result",)),
        "structured": j.structured_result,
        "fields": [ser(f) for f in fields],
        "threshold": str(ocr_svc.review_threshold(db, cu.organization_id)),
        "document": ser(doc, file_name=fa.original_name, mime_type=fa.mime_type, file_url=f"/api/v1/files/{fa.id}"),
        "mismatches": ocr_svc.mismatches(db, j.receipt_id) if j.receipt_id else [],
        "provider_notice": "개발용 Mock OCR 결과입니다 (실제 문서 내용이 아님)." if (j.structured_result or {}).get("provider") == "mock" else None,
    })


class ConfirmIn(BaseModel):
    fields: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    apply_to_receipt: bool = True


def _dec(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        raise BusinessRule("INVALID_NUMBER", f"숫자 형식이 올바르지 않습니다: {v}")


def _date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    d = normalize_date(str(v))
    if not d:
        raise BusinessRule("INVALID_DATE", f"날짜 형식이 올바르지 않습니다: {v}")
    return d


@router.post("/ocr/jobs/{job_id}/confirm")
def confirm(job_id: uuid.UUID, body: ConfirmIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("ocr:use"))):
    j = _job(db, cu, job_id)
    if j.status not in ("REVIEW_REQUIRED", "COMPLETED", "FAILED"):
        raise InvalidState(j.status, "OCR 결과 확정")
    st = j.structured_result or {}
    merged: dict[str, Any] = {k: (v.get("value") if isinstance(v, dict) else None) for k, v in st.items() if isinstance(v, dict)}
    merged.update(body.fields)
    # 검증/정규화
    for dk in ("manufactured_date", "expiry_date", "issued_date"):
        merged[dk] = _date(merged.get(dk))
    merged["quantity"] = _dec(merged.get("quantity"))
    if merged.get("manufactured_date") and merged.get("expiry_date") and merged["expiry_date"] <= merged["manufactured_date"]:
        raise BusinessRule("INVALID_DATES", "소비/유통기한은 제조일자 이후여야 합니다.")
    # 수정 이력
    for f in db.execute(select(OcrExtractedField).where(OcrExtractedField.ocr_job_id == j.id, OcrExtractedField.item_index == 0)).scalars():
        if f.field_name in body.fields:
            f.corrected_value = None if body.fields[f.field_name] is None else str(body.fields[f.field_name])
            f.corrected_by = cu.id
    doc = db.get(ReceiptDocument, j.document_id)
    if merged.get("coa_number"):
        doc.document_number = str(merged["coa_number"])[:100]
    if merged.get("issued_date"):
        doc.issued_date = merged["issued_date"]
    tests = body.fields.get("tests", st.get("tests") or [])
    if j.document_type == "COA" and tests:
        doc.coa_test_results = [{k: (str(v) if v is not None else None) for k, v in t.items()} for t in tests]
    j.status = "CONFIRMED"
    j.confirmed_by = cu.id
    j.confirmed_at = datetime.now(timezone.utc)
    applied = None
    if body.apply_to_receipt and j.receipt_id:
        applied = _apply_to_receipt(db, cu, j, merged, body.items, tests)
    audit(db, cu, "OCR_CONFIRM", "OCR_JOB", j.id, after={k: str(v) if v is not None else None for k, v in merged.items() if k != "items"})
    db.commit()
    return ok({"job_id": str(j.id), "status": j.status, "applied": applied}, "OCR 결과가 확정되었습니다.")


def _apply_to_receipt(db: Session, cu: CurrentUser, j: OcrJob, f: dict[str, Any], items: list[dict[str, Any]], tests: list) -> dict[str, Any]:
    from app.services import receiving

    r = db.get(MaterialReceipt, j.receipt_id)
    if r.status not in receiving.EDITABLE and r.status != "OCR_PROCESSING":
        raise InvalidState(r.status, "OCR 결과 반영")
    sup_match = (j.structured_result or {}).get("supplier_name", {}).get("matched_entity_id")
    if f.get("supplier_id") or sup_match:
        r.supplier_id = uuid.UUID(str(f.get("supplier_id") or sup_match))
    if j.document_type == "COA" and f.get("coa_number"):
        r.coa_number = str(f["coa_number"])[:50]
    if j.document_type in ("RECEIPT", "TRANSACTION_STATEMENT", "DELIVERY_NOTE") and f.get("coa_number"):
        r.receipt_document_number = str(f["coa_number"])[:50]
    created = 0
    # 다품목(영수증) → 개별 입고 항목 분리
    for it in items:
        if not it.get("material_id"):
            raise BusinessRule("MATERIAL_REQUIRED", f"품목 '{it.get('material_name')}' 의 원료를 선택하세요. (마스터 매칭 실패)")
        m = db.get(Material, uuid.UUID(str(it["material_id"])))
        if not m or m.organization_id != cu.organization_id:
            raise NotFound("원료")
        v = receiving._validate_item(db, cu, {
            "material_id": m.id, "quantity": _dec(it.get("quantity")) or Decimal(0), "package_count": int(it.get("package_count") or 1),
            "supplier_lot_number": it.get("supplier_lot"), "manufactured_date": _date(it.get("manufactured_date")),
            "expiry_date": _date(it.get("expiry_date")), "expiry_type": it.get("expiry_type"),
            "unit_price": _dec(it.get("unit_price")), "amount": _dec(it.get("amount")), "ocr_job_id": j.id,
            "country_of_origin": it.get("country_of_origin"), "warehouse_id": it.get("warehouse_id"),
        })
        db.add(MaterialReceiptItem(organization_id=cu.organization_id, receipt_id=r.id, line_no=len(r.items) + created + 1,
                                   created_by=cu.id, **v))
        created += 1
    # 단일 품목 문서(COA/라벨): 원료 매칭되는 기존 품목 갱신 또는 신규
    if not items:
        mat_match = f.get("material_id") or (j.structured_result or {}).get("material_name", {}).get("matched_entity_id")
        target = None
        for it in r.items:
            if mat_match and str(it.material_id) == str(mat_match):
                target = it
        if target is None and len(r.items) == 1:
            target = r.items[0]
        if target is not None:
            for src, dst in [("supplier_lot", "supplier_lot_number"), ("manufacturer_name", "manufacturer_name"),
                             ("country_of_origin", "country_of_origin"), ("manufactured_date", "manufactured_date"),
                             ("expiry_date", "expiry_date"), ("expiry_type", "expiry_type")]:
                if f.get(src) not in (None, ""):
                    setattr(target, dst, f[src])
            target.ocr_job_id = j.id
        elif mat_match and f.get("quantity"):
            v = receiving._validate_item(db, cu, {
                "material_id": uuid.UUID(str(mat_match)), "quantity": f["quantity"], "package_count": int(f.get("package_count") or 1),
                "supplier_lot_number": f.get("supplier_lot"), "manufacturer_name": f.get("manufacturer_name"),
                "country_of_origin": f.get("country_of_origin"), "manufactured_date": f.get("manufactured_date"),
                "expiry_date": f.get("expiry_date"), "expiry_type": f.get("expiry_type"), "ocr_job_id": j.id,
            })
            db.add(MaterialReceiptItem(organization_id=cu.organization_id, receipt_id=r.id, line_no=len(r.items) + 1, created_by=cu.id, **v))
            created += 1
    # COA 시험 결과는 현장검사와 분리 저장 (source=COA)
    if j.document_type == "COA" and tests:
        overall = "FAIL" if any(str(t.get("judgement")).upper() == "FAIL" for t in tests) else "PASS"
        qi = QualityInspection(organization_id=cu.organization_id, facility_id=r.facility_id, inspection_number=next_number(db, cu.organization_id, "QI"),
                               target_type="RECEIPT", target_id=r.id, source="COA", overall_result=overall, inspected_by=cu.id,
                               inspected_at=datetime.now(timezone.utc), note=f"COA {f.get('coa_number') or ''}".strip(), created_by=cu.id)
        db.add(qi)
        db.flush()
        for t in tests:
            db.add(QualityInspectionResult(inspection_id=qi.id, item_code=str(t.get("item"))[:40], item_name=str(t.get("item"))[:100],
                                           result="FAIL" if str(t.get("judgement")).upper() == "FAIL" else "PASS",
                                           text_value=str(t.get("result"))[:300], standard=str(t.get("standard"))[:300]))
    if r.status in ("OCR_REVIEW", "OCR_PROCESSING"):
        still = db.execute(select(OcrJob.id).where(OcrJob.receipt_id == r.id, OcrJob.status.in_(("QUEUED", "PROCESSING", "REVIEW_REQUIRED")),
                                                   OcrJob.id != j.id)).first()
        if not still:
            r.status = "DRAFT"
    db.flush()
    return {"receipt_id": str(r.id), "items_created": created, "receipt_status": r.status}


@router.post("/ocr/jobs/{job_id}/retry")
def retry(job_id: uuid.UUID, background: BackgroundTasks, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("ocr:use"))):
    j = _job(db, cu, job_id)
    if j.status not in ("FAILED", "REVIEW_REQUIRED"):
        raise InvalidState(j.status, "OCR 재처리")
    j.status = "QUEUED"
    j.error_message = None
    j.provider = settings.OCR_PROVIDER
    if j.receipt_id:
        r = db.get(MaterialReceipt, j.receipt_id)
        if r.status in ("DRAFT", "OCR_REVIEW"):
            r.status = "OCR_PROCESSING"
    db.commit()
    ocr_svc.enqueue(j.id, background)
    return ok(ser(j, exclude=("raw_text",)), "OCR 재처리를 시작합니다.")


@router.get("/files/{file_id}")
def download_file(file_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    fa = db.get(FileAttachment, file_id)
    if not fa or fa.organization_id != cu.organization_id:
        raise NotFound("파일")
    if fa.facility_id and not cu.can_access_facility(fa.facility_id):
        raise Forbidden("해당 공장 파일에 대한 권한이 없습니다.", "FACILITY_FORBIDDEN")
    perm = {"RECEIPT": "receipt:read", "OCR": "ocr:use", "REPORT": "report:read"}.get(fa.entity_type or "", "receipt:read")
    if not (cu.has(perm) or cu.has("receipt:read")):
        raise Forbidden()
    data = read_file(fa)
    from urllib.parse import quote

    return Response(data, media_type=fa.mime_type, headers={
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(fa.original_name)}", "Cache-Control": "private, no-store"})


@router.post("/files")
async def upload_file(file: UploadFile = File(...), entity_type: str = Form("PHOTO"), db: Session = Depends(get_db),
                      cu: CurrentUser = Depends(get_current_user)):
    data = await file.read()
    mime = validate_upload(file.filename or "", data)
    fa = save_file(db, org_id=cu.organization_id, facility_id=cu.facility_id, user_id=cu.id, filename=file.filename or "file",
                   data=data, mime=mime, entity_type=entity_type[:40])
    db.commit()
    return ok({"file_id": str(fa.id), "file_name": fa.original_name, "mime_type": mime, "url": f"/api/v1/files/{fa.id}"})
