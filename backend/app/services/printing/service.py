"""라벨 출력 오케스트레이션. 출력 실패는 업무 트랜잭션과 분리되며 동일 QR 로 재출력한다."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import BusinessRule, NotFound
from app.models import PrinterSetting, QrCode, QrPrintLog
from app.services.printing.labels import PrinterError, label_pdf, label_tspl, label_zpl, parse_size, send_raw
from app.services.qr.resolver import describe

log = logging.getLogger("printing")


def get_qr(db: Session, org_id: uuid.UUID, qr_id: uuid.UUID) -> QrCode:
    qr = db.get(QrCode, qr_id)
    if not qr or qr.organization_id != org_id:
        raise NotFound("QR")
    return qr


def default_printer(db: Session, org_id: uuid.UUID, facility_id: uuid.UUID | None) -> PrinterSetting | None:
    return db.execute(select(PrinterSetting).where(PrinterSetting.organization_id == org_id, PrinterSetting.is_active,
                                                   PrinterSetting.is_default,
                                                   (PrinterSetting.facility_id == facility_id) | PrinterSetting.facility_id.is_(None))
                      ).scalars().first()


def print_qr(db: Session, cu: Any, qr: QrCode, *, printer_id: uuid.UUID | None = None, size: str | None = None, copies: int = 1,
             reprint: bool = False, reason: str | None = None) -> dict[str, Any]:
    if reprint and not reason:
        raise BusinessRule("REASON_REQUIRED", "재출력 사유를 입력하세요.")
    if copies < 1 or copies > 100:
        raise BusinessRule("INVALID_COPIES", "출력 매수는 1~100 사이여야 합니다.")
    printer = None
    if printer_id:
        printer = db.get(PrinterSetting, printer_id)
        if not printer or printer.organization_id != cu.organization_id:
            raise NotFound("프린터")
    else:
        printer = default_printer(db, cu.organization_id, cu.facility_id)
    if not size and printer:
        size = f"{printer.label_width_mm}x{printer.label_height_mm}"
    try:
        parse_size(size)
    except ValueError as e:
        raise BusinessRule("INVALID_LABEL_SIZE", str(e))
    data = describe(db, qr)
    status, err, fmt = "GENERATED", None, "PDF"
    if printer and printer.printer_type in ("NETWORK_ZPL", "NETWORK_TSPL"):
        fmt = "ZPL" if printer.printer_type == "NETWORK_ZPL" else "TSPL"
        payload = (label_zpl(data, size, printer.dpi) if fmt == "ZPL" else label_tspl(data, size)).encode("utf-8") * copies
        try:
            if not printer.host or not printer.port:
                raise PrinterError("프린터 주소/포트가 설정되지 않았습니다.")
            send_raw(printer.host, printer.port, payload)
            status = "SUCCESS"
        except PrinterError as e:
            status, err = "FAILED", str(e)
            log.warning("print failed: %s", e)
    plog = QrPrintLog(
        organization_id=cu.organization_id, facility_id=cu.facility_id, qr_code_id=qr.id, printer_id=printer.id if printer else None,
        print_type="REPRINT" if reprint else "PRINT", output_format=fmt, label_size=size or "80x50", copies=copies,
        status=status, error_message=err, reason=reason, printed_by=cu.id, printed_at=datetime.now(timezone.utc),
    )
    db.add(plog)
    qr.print_count = (qr.print_count or 0) + copies
    if reprint:
        audit(db, cu, "QR_REPRINT", "QR", qr.id, after={"copies": copies, "size": size}, reason=reason)
    db.flush()
    return {
        "print_log_id": str(plog.id), "status": status, "error_message": err, "output_format": fmt, "qr_code_id": str(qr.id),
        "print_count": qr.print_count, "pdf_url": f"/api/v1/qr/{qr.id}/label?size={size or '80x50'}&format=pdf",
        "fallback": "PDF 라벨로 출력하세요." if status == "FAILED" else None,
    }


def auto_print_after_receipt(db: Session, cu: Any, result: dict[str, Any]) -> dict[str, Any] | None:
    printer = default_printer(db, cu.organization_id, cu.facility_id)
    if not printer or not printer.auto_print:
        return None
    outcomes = []
    for lot in result.get("lots", []):
        for p in lot["packages"]:
            try:
                qr = get_qr(db, cu.organization_id, uuid.UUID(p["qr_code_id"]))
                outcomes.append(print_qr(db, cu, qr, printer_id=printer.id))
                db.commit()
            except Exception as e:  # noqa: BLE001 - 인쇄 실패는 입고에 영향 없음
                db.rollback()
                outcomes.append({"qr_code_id": p["qr_code_id"], "status": "FAILED", "error_message": str(e)})
    return {"printer": printer.name, "results": outcomes}


def labels_pdf(db: Session, qrs: list[QrCode], size: str | None) -> bytes:
    return label_pdf([describe(db, q) for q in qrs], size)
