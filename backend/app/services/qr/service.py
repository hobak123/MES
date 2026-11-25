"""QR 생성/해석. QR 에는 불변 식별자만 저장한다 (개인정보·단가·인증정보 금지)."""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import QrCode

QR_VERSION = 1


def build_payload(qr_type: str, entity_id: uuid.UUID, lot: str) -> str:
    return json.dumps({"version": QR_VERSION, "type": qr_type, "id": str(entity_id), "lot": lot}, separators=(",", ":"), ensure_ascii=False)


def ensure_qr(db: Session, *, org_id: uuid.UUID, qr_type: str, entity_id: uuid.UUID, lot_number: str,
              user_id: uuid.UUID | None) -> QrCode:
    """엔티티당 QR 1개 (멱등). 재출력 시에도 동일 QR 유지."""
    existing = db.execute(select(QrCode).where(QrCode.qr_type == qr_type, QrCode.entity_id == entity_id)).scalar_one_or_none()
    if existing:
        return existing
    qr = QrCode(
        id=uuid.uuid4(), organization_id=org_id, qr_type=qr_type, entity_id=entity_id, lot_number=lot_number,
        payload=build_payload(qr_type, entity_id, lot_number), created_at=datetime.now(timezone.utc), created_by=user_id,
    )
    db.add(qr)
    db.flush()
    return qr


def parse_scan(raw: str) -> dict[str, Any]:
    """스캔 문자열 해석. JSON payload 또는 LOT/포장 번호 문자열(직접 검색) 모두 허용."""
    raw = (raw or "").strip()
    if not raw:
        return {"kind": "invalid"}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return {"kind": "invalid"}
        if not isinstance(data, dict) or data.get("version") != QR_VERSION or "type" not in data or "id" not in data:
            return {"kind": "invalid"}
        try:
            uuid.UUID(str(data["id"]))
        except ValueError:
            return {"kind": "invalid"}
        return {"kind": "payload", "type": data["type"], "id": data["id"], "lot": data.get("lot")}
    return {"kind": "text", "value": raw[:100]}


def qr_png(payload: str, box_size: int = 8) -> bytes:
    img = qrcode.make(payload, error_correction=ERROR_CORRECT_M, box_size=box_size, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
