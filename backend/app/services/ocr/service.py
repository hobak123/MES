"""OCR 처리 파이프라인: 원본 저장 → 전처리 → OCR → 구조화 → 필드 매핑 → 마스터 매칭 → 정규화 → 신뢰도 → 검토 → 저장."""
from __future__ import annotations

import difflib
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import (
    FileAttachment, Material, MaterialReceipt, OcrExtractedField, OcrJob, ReceiptDocument, Supplier, SystemSetting,
)
from app.services.ocr.parser import KEY_FIELDS, FieldResult, parse
from app.services.ocr.providers import OCRProviderError, get_provider, preprocess_image
from app.services.storage import read_file

log = logging.getLogger("ocr")
MAX_ATTEMPTS = 3


def review_threshold(db: Session, org_id: uuid.UUID) -> Decimal:
    s = db.execute(select(SystemSetting).where(SystemSetting.organization_id == org_id, SystemSetting.key == "ocr.review_threshold")).scalar_one_or_none()
    try:
        return Decimal(str(s.value["value"])) if s else settings.OCR_REVIEW_THRESHOLD
    except Exception:
        return settings.OCR_REVIEW_THRESHOLD


def _best_match(name: str | None, candidates: list[tuple[uuid.UUID, str]]) -> tuple[uuid.UUID | None, float]:
    if not name:
        return None, 0.0
    best, score = None, 0.0
    n = name.replace(" ", "").lower()
    for cid, cname in candidates:
        c = cname.replace(" ", "").lower()
        s = 1.0 if n == c else (0.92 if (n in c or c in n) and min(len(n), len(c)) >= 2 else difflib.SequenceMatcher(None, n, c).ratio())
        if s > score:
            best, score = cid, s
    return (best, score) if score >= 0.8 else (None, score)


def _sval(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def run_job(job_id: uuid.UUID) -> None:
    """작업 실행 (Celery 태스크 또는 BackgroundTasks 에서 호출). 자체 세션 사용."""
    with SessionLocal() as db:
        job = db.execute(select(OcrJob).where(OcrJob.id == job_id).with_for_update()).scalar_one_or_none()
        if not job or job.status not in ("QUEUED", "FAILED"):
            return
        job.status = "PROCESSING"
        job.started_at = datetime.now(timezone.utc)
        job.attempts += 1
        db.commit()
        try:
            _process(db, job)
            db.commit()
        except OCRProviderError as e:
            db.rollback()
            job = db.get(OcrJob, job_id)
            job.status = "FAILED"
            job.error_message = str(e)
            job.completed_at = datetime.now(timezone.utc)
            _reset_receipt_status(db, job)
            db.commit()
            if e.retryable and job.attempts < MAX_ATTEMPTS:
                raise
        except Exception as e:  # noqa: BLE001
            log.exception("OCR job failed")
            db.rollback()
            job = db.get(OcrJob, job_id)
            job.status = "FAILED"
            job.error_message = f"OCR 처리 중 오류: {e.__class__.__name__}"
            job.completed_at = datetime.now(timezone.utc)
            _reset_receipt_status(db, job)
            db.commit()


def _reset_receipt_status(db: Session, job: OcrJob) -> None:
    if job.receipt_id:
        r = db.get(MaterialReceipt, job.receipt_id)
        if r and r.status == "OCR_PROCESSING":
            r.status = "DRAFT"
            r.status_reason = "OCR 실패 — 수동 입력 가능"


def _process(db: Session, job: OcrJob) -> None:
    doc = db.get(ReceiptDocument, job.document_id)
    fa = db.get(FileAttachment, doc.file_id)
    data = read_file(fa)
    provider = get_provider(job.provider)
    raw = provider.extract(preprocess_image(data, fa.mime_type), "image/png" if fa.mime_type.startswith("image/") else fa.mime_type)
    parsed = parse(raw.lines, job.document_type)
    threshold = float(review_threshold(db, job.organization_id))

    suppliers = [(s.id, s.name) for s in db.execute(select(Supplier).where(Supplier.organization_id == job.organization_id, Supplier.is_active)).scalars()]
    materials = [(m.id, m.material_name) for m in db.execute(select(Material).where(Material.organization_id == job.organization_id, Material.is_active)).scalars()]

    db.execute(delete(OcrExtractedField).where(OcrExtractedField.ocr_job_id == job.id))
    structured: dict[str, Any] = {"document_type": job.document_type, "provider": raw.provider, "items": [], "tests": parsed.tests,
                                  "warnings": parsed.warnings}
    confs: list[float] = []

    def add_field(idx: int, fr: FieldResult) -> dict[str, Any]:
        matched = None
        if fr.name == "supplier_name":
            matched, _ = _best_match(fr.value, suppliers)
        elif fr.name == "material_name":
            matched, _ = _best_match(fr.value, materials)
        needs = fr.missing or fr.confidence < threshold
        db.add(OcrExtractedField(
            organization_id=job.organization_id, ocr_job_id=job.id, item_index=idx, field_name=fr.name,
            raw_value=fr.raw, normalized_value=_sval(fr.value), confidence=Decimal(str(round(fr.confidence, 4))),
            is_missing=fr.missing, needs_review=needs, matched_entity_id=matched,
        ))
        return {"value": _sval(fr.value), "raw": fr.raw, "confidence": round(fr.confidence, 4), "missing": fr.missing,
                "needs_review": needs, "matched_entity_id": str(matched) if matched else None, "note": fr.note}

    for name, fr in parsed.fields.items():
        structured[name] = add_field(0, fr)
        if name in KEY_FIELDS and not fr.missing:
            confs.append(fr.confidence)
    for i, item in enumerate(parsed.items, start=1):
        structured["items"].append({k: add_field(i, fr) for k, fr in item.items()})
    overall = sum(confs) / len(KEY_FIELDS) if confs else 0.0  # 누락 필드는 0 으로 반영
    structured["confidence"] = round(overall, 4)
    structured["review_required"] = True  # 고신뢰 결과도 확정 전 사용자 검토 필수
    structured["missing_fields"] = [k for k in KEY_FIELDS if parsed.fields[k].missing]

    job.raw_text = raw.text
    job.structured_result = structured
    job.overall_confidence = Decimal(str(round(overall, 4)))
    job.review_required = True
    job.status = "REVIEW_REQUIRED"
    job.error_message = None
    job.completed_at = datetime.now(timezone.utc)
    if parsed.fields.get("coa_number") and parsed.fields["coa_number"].value and not doc.document_number:
        doc.document_number = str(parsed.fields["coa_number"].value)[:100]
    if parsed.fields.get("issued_date") and parsed.fields["issued_date"].value and not doc.issued_date:
        doc.issued_date = parsed.fields["issued_date"].value
    if job.receipt_id:
        r = db.get(MaterialReceipt, job.receipt_id)
        if r and r.status in ("OCR_PROCESSING", "DRAFT"):
            r.status = "OCR_REVIEW"


def enqueue(job_id: uuid.UUID, background_tasks: Any | None = None) -> str:
    if settings.CELERY_ENABLED:
        from app.workers.tasks import ocr_task

        ocr_task.delay(str(job_id))
        return "celery"
    if background_tasks is not None:
        background_tasks.add_task(run_job, job_id)
        return "background"
    run_job(job_id)
    return "inline"


def mismatches(db: Session, receipt_id: uuid.UUID) -> list[dict[str, Any]]:
    """같은 입고에 연결된 여러 문서 간 LOT/수량/원료 불일치."""
    jobs = db.execute(select(OcrJob).where(OcrJob.receipt_id == receipt_id, OcrJob.structured_result.is_not(None))).scalars().all()
    out = []
    for fname in ("supplier_lot", "quantity", "material_name", "expiry_date"):
        vals = {}
        for j in jobs:
            v = (j.structured_result or {}).get(fname, {}).get("value")
            if v:
                vals.setdefault(v, []).append(str(j.id))
        if len(vals) > 1:
            out.append({"field": fname, "values": [{"value": k, "jobs": v} for k, v in vals.items()]})
    return out
