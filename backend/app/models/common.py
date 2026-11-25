from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, FacilityMixin, IdMixin, OrgMixin, TimestampMixin

QR_TYPES = (
    "RAW_MATERIAL_LOT", "RAW_MATERIAL_PACKAGE", "WEIGHING_BATCH", "PROCESS_WIP",
    "FINISHED_GOODS_LOT", "FINISHED_GOODS_PACKAGE", "EQUIPMENT", "STORAGE_LOCATION", "PRODUCTION_ORDER",
)


class QrCode(IdMixin, OrgMixin, Base):
    __tablename__ = "qr_codes"
    __table_args__ = (UniqueConstraint("qr_type", "entity_id"), UniqueConstraint("payload"))
    qr_type: Mapped[str] = mapped_column(String(30), index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(index=True)
    lot_number: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[str] = mapped_column(String(300))  # 불변 JSON 문자열
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | VOID
    print_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class PrinterSetting(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "printer_settings"
    name: Mapped[str] = mapped_column(String(100))
    printer_type: Mapped[str] = mapped_column(String(20))  # PDF | BROWSER | NETWORK_ZPL | NETWORK_TSPL | USB | BLUETOOTH
    host: Mapped[str | None] = mapped_column(String(200))
    port: Mapped[int | None] = mapped_column(Integer)
    label_width_mm: Mapped[int] = mapped_column(Integer, default=80)
    label_height_mm: Mapped[int] = mapped_column(Integer, default=50)
    dpi: Mapped[int] = mapped_column(Integer, default=203)
    auto_print: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class QrPrintLog(IdMixin, OrgMixin, FacilityMixin, Base):
    __tablename__ = "qr_print_logs"
    qr_code_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("qr_codes.id"), index=True)
    printer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("printer_settings.id"))
    print_type: Mapped[str] = mapped_column(String(10))  # PRINT | REPRINT
    output_format: Mapped[str] = mapped_column(String(10))  # PDF | ZPL | TSPL
    label_size: Mapped[str] = mapped_column(String(20))
    copies: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10))  # SUCCESS | FAILED | GENERATED
    error_message: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)  # 재출력/훼손 사유
    printed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    printed_at: Mapped[datetime]


class FileAttachment(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "file_attachments"
    original_name: Mapped[str] = mapped_column(String(300))
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=True)


class ApprovalRequest(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "approval_requests"
    request_type: Mapped[str] = mapped_column(String(40), index=True)  # WEIGHING_REVERSAL | STOCK_ADJUSTMENT | TOLERANCE_OVERRIDE | RECORD_CHANGE | MASS_BALANCE
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    payload: Mapped[dict[str, Any] | None]
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)  # PENDING | APPROVED | REJECTED | EXECUTED
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)


class Notification(IdMixin, OrgMixin, FacilityMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("organization_id", "dedup_key"),)
    category: Mapped[str] = mapped_column(String(30), index=True)  # EXPIRY | LOW_STOCK | INSPECTION | COA | LONG_UNUSED | OCR | HOLD | DELAY | ANOMALY
    severity: Mapped[str] = mapped_column(String(10), default="WARNING")  # INFO | WARNING | DANGER
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[uuid.UUID | None]
    target_role: Mapped[str | None] = mapped_column(String(50))
    dedup_key: Mapped[str] = mapped_column(String(200))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    read_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(index=True)


class ReportExportLog(IdMixin, OrgMixin, FacilityMixin, Base):
    __tablename__ = "report_export_logs"
    report_type: Mapped[str] = mapped_column(String(50), index=True)
    export_format: Mapped[str] = mapped_column(String(10))  # XLSX | PDF
    filters: Mapped[dict[str, Any] | None]
    row_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")  # QUEUED | PROCESSING | COMPLETED | FAILED
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("file_attachments.id"))
    error_message: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    requested_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class AuditLog(IdMixin, Base):
    """감사로그 — 수정/삭제 API 없음, DB 트리거로 UPDATE/DELETE 차단."""

    __tablename__ = "audit_logs"
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("facilities.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    username: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str | None] = mapped_column(String(60), index=True)
    target_id: Mapped[str | None] = mapped_column(String(60), index=True)
    before_data: Mapped[dict[str, Any] | None]
    after_data: Mapped[dict[str, Any] | None]
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(index=True)


class IdempotencyKey(IdMixin, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("organization_id", "key"),)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(300))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]


class NumberSequence(Base):
    """원자적 번호 발급 (prefix + 일자별 증가)."""

    __tablename__ = "number_sequences"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(20), primary_key=True)
    seq_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger, default=0)


class SystemSetting(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("organization_id", "key"),)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[dict[str, Any]]
    description: Mapped[str | None] = mapped_column(String(300))
