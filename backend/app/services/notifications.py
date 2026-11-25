"""알림 생성(스캔): 소비기한 D-90/60/30/7·경과, 안전재고 미달, 검수/COA 미완료, 장기 미사용, OCR 실패, 보류.

dedup_key 로 중복 생성 방지. 주기 실행: Celery beat (workers/tasks.py) 또는 POST /notifications/scan.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.numbering import today_seoul
from app.models import (
    FinishedGoodsLot, InventoryBalance, InventoryTransaction, Material, MaterialReceipt, Notification, OcrJob, ProductionOrder,
    SystemSetting,
)
from app.services.inventory import service as inv


def _setting(db: Session, org_id: uuid.UUID, key: str, default):
    s = db.execute(select(SystemSetting).where(SystemSetting.organization_id == org_id, SystemSetting.key == key)).scalar_one_or_none()
    return s.value.get("value", default) if s else default


def _add(db: Session, org_id: uuid.UUID, facility_id, category: str, severity: str, title: str, message: str, dedup: str,
         target_type: str | None = None, target_id=None, role: str | None = None) -> int:
    res = db.execute(insert(Notification).values(
        id=uuid.uuid4(), organization_id=org_id, facility_id=facility_id, category=category, severity=severity, title=title,
        message=message, target_type=target_type, target_id=target_id, target_role=role, dedup_key=dedup[:200], is_read=False,
        created_at=datetime.now(timezone.utc)).on_conflict_do_nothing(index_elements=["organization_id", "dedup_key"]))
    return res.rowcount or 0


def scan(db: Session, org_id: uuid.UUID) -> int:
    today = today_seoul()
    n = 0
    days = sorted([int(x) for x in _setting(db, org_id, "alerts.expiry_days", [90, 60, 30, 7])])
    for b in db.execute(select(InventoryBalance).where(InventoryBalance.organization_id == org_id, InventoryBalance.quantity > 0,
                                                       InventoryBalance.expiry_date.is_not(None),
                                                       InventoryBalance.expiry_date <= today + timedelta(days=max(days))))).scalars():
        left = (b.expiry_date - today).days
        if left < 0:
            n += _add(db, org_id, b.facility_id, "EXPIRY", "DANGER", "소비기한 경과", f"{b.lot_number} 기한 경과({b.expiry_date}) — 사용 금지",
                      f"EXP:{b.lot_node_id}:EXPIRED", "LOT", b.lot_node_id)
        else:
            bucket = next(d for d in days if left <= d)
            n += _add(db, org_id, b.facility_id, "EXPIRY", "WARNING" if bucket > 7 else "DANGER", f"소비기한 D-{bucket} 이내",
                      f"{b.lot_number} 기한 {b.expiry_date} (D-{left})", f"EXP:{b.lot_node_id}:{bucket}", "LOT", b.lot_node_id)
    for m in db.execute(select(Material).where(Material.organization_id == org_id, Material.is_active, Material.safety_stock.is_not(None))).scalars():
        avail = inv.available_quantity(db, org_id, None, material_id=m.id)
        if avail < m.safety_stock:
            n += _add(db, org_id, None, "LOW_STOCK", "WARNING", "안전재고 미달", f"{m.material_name}: 가용 {avail.normalize()} < 안전재고 {m.safety_stock.normalize()}",
                      f"LOW:{m.id}:{today}", "MATERIAL", m.id, "WAREHOUSE_MANAGER")
    for r in db.execute(select(MaterialReceipt).where(MaterialReceipt.organization_id == org_id,
                                                      MaterialReceipt.status.in_(("INSPECTION_PENDING", "HOLD"))))).scalars():
        n += _add(db, org_id, r.facility_id, "INSPECTION", "WARNING", "검수 미완료", f"입고 {r.receipt_number} 검수/승인 대기",
                  f"INSP:{r.id}:{r.status}", "RECEIPT", r.id, "QUALITY_MANAGER")
    for r in db.execute(select(MaterialReceipt).where(MaterialReceipt.organization_id == org_id, MaterialReceipt.status == "POSTED",
                                                      MaterialReceipt.approved_at.is_(None))).scalars():
        n += _add(db, org_id, r.facility_id, "COA", "WARNING", "품질 승인/COA 미완료", f"입고 {r.receipt_number} 확정되었으나 품질 승인 전(격리재고)",
                  f"COA:{r.id}", "RECEIPT", r.id, "QUALITY_MANAGER")
    for j in db.execute(select(OcrJob).where(OcrJob.organization_id == org_id, OcrJob.status == "FAILED")).scalars():
        n += _add(db, org_id, j.facility_id, "OCR", "WARNING", "OCR 실패", f"OCR 처리 실패: {j.error_message or ''} — 재시도 또는 수동 입력",
                  f"OCR:{j.id}:{j.attempts}", "OCR_JOB", j.id)
    for f in db.execute(select(FinishedGoodsLot).where(FinishedGoodsLot.organization_id == org_id, FinishedGoodsLot.quality_status.in_(("HOLD", "QUALITY_PENDING"))))).scalars():
        n += _add(db, org_id, f.facility_id, "HOLD" if f.quality_status == "HOLD" else "INSPECTION", "WARNING",
                  "완제품 보류" if f.quality_status == "HOLD" else "완제품 검사 대기", f"{f.lot_number} ({f.quality_status})",
                  f"FG:{f.id}:{f.quality_status}", "FG_LOT", f.id, "QUALITY_MANAGER")
    for o in db.execute(select(ProductionOrder).where(ProductionOrder.organization_id == org_id, ProductionOrder.scheduled_end.is_not(None),
                                                      ProductionOrder.scheduled_end < datetime.now(timezone.utc),
                                                      ProductionOrder.status.in_(("APPROVED", "WAITING", "WEIGHING", "WEIGHING_COMPLETED", "PRODUCING"))))).scalars():
        n += _add(db, org_id, o.facility_id, "DELAY", "WARNING", "생산 지연", f"{o.order_number} 예정 종료 경과 ({o.status})",
                  f"DELAY:{o.id}", "PRODUCTION_ORDER", o.id, "PRODUCTION_MANAGER")
    # 장기 미사용
    long_days = int(_setting(db, org_id, "alerts.long_unused_days", 180))
    cutoff = datetime.now(timezone.utc) - timedelta(days=long_days)
    for b in db.execute(select(InventoryBalance).where(InventoryBalance.organization_id == org_id, InventoryBalance.quantity > 0,
                                                       InventoryBalance.last_movement_at < cutoff)).scalars():
        n += _add(db, org_id, b.facility_id, "LONG_UNUSED", "INFO", "장기 미사용 재고", f"{b.lot_number} {long_days}일 이상 미사용",
                  f"LONG:{b.id}:{today.isoformat()[:7]}", "LOT", b.lot_node_id)
    return n
