from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, FacilityMixin, IdMixin, OrgMixin, TimestampMixin

RECEIPT_STATUSES = (
    "DRAFT", "OCR_PROCESSING", "OCR_REVIEW", "INSPECTION_PENDING", "HOLD",
    "ACCEPTED", "REJECTED", "POSTED", "CANCELLED",
)


class MaterialReceipt(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "material_receipts"
    __table_args__ = (UniqueConstraint("organization_id", "receipt_number"),)
    receipt_number: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[datetime]  # 서버 시각
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"), index=True)
    purchase_order_number: Mapped[str | None] = mapped_column(String(50))
    receipt_document_number: Mapped[str | None] = mapped_column(String(50))  # 영수증/거래명세서 번호
    coa_number: Mapped[str | None] = mapped_column(String(50))
    received_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    inspected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    inspection_result: Mapped[str | None] = mapped_column(String(20))  # PASS | FAIL | HOLD | NOT_APPLICABLE
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    posted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)

    items: Mapped[list["MaterialReceiptItem"]] = relationship(
        back_populates="receipt", lazy="selectin", order_by="MaterialReceiptItem.line_no"
    )


class MaterialReceiptItem(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "material_receipt_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="qty_pos"),
        CheckConstraint("package_count >= 1", name="pkg_pos"),
    )
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("material_receipts.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    material_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("materials.id"), index=True)
    supplier_lot_number: Mapped[str | None] = mapped_column(String(100))
    manufacturer_name: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[Decimal]
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    package_count: Mapped[int] = mapped_column(Integer, default=1)
    package_quantity: Mapped[Decimal | None]  # 포장당 수량 (미지정 시 균등 분할)
    package_weight: Mapped[Decimal | None]
    country_of_origin: Mapped[str | None] = mapped_column(String(100))
    manufactured_date: Mapped[date | None]
    expiry_date: Mapped[date | None]
    expiry_type: Mapped[str | None] = mapped_column(String(20))  # CONSUME_BY(소비기한) | SELL_BY(유통기한) | NOT_APPLICABLE
    storage_condition: Mapped[str | None] = mapped_column(String(50))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    storage_location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("storage_locations.id"))
    unit_price: Mapped[Decimal | None]
    amount: Mapped[Decimal | None]
    material_lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("material_lots.id", use_alter=True))
    ocr_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ocr_jobs.id", use_alter=True))
    note: Mapped[str | None] = mapped_column(Text)

    receipt: Mapped[MaterialReceipt] = relationship(back_populates="items")


class MaterialLot(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "material_lots"
    __table_args__ = (UniqueConstraint("organization_id", "lot_number"),)
    material_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("materials.id"), index=True)
    receipt_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("material_receipt_items.id"))
    lot_number: Mapped[str] = mapped_column(String(40), index=True)
    supplier_lot_number: Mapped[str | None] = mapped_column(String(100), index=True)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"))
    manufacturer_name: Mapped[str | None] = mapped_column(String(200))
    country_of_origin: Mapped[str | None] = mapped_column(String(100))
    received_date: Mapped[date]
    manufactured_date: Mapped[date | None]
    expiry_date: Mapped[date | None] = mapped_column(index=True)
    expiry_type: Mapped[str | None] = mapped_column(String(20))
    original_quantity: Mapped[Decimal]
    base_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    # PENDING(검수대기) | PASSED | HOLD | REJECTED
    quality_status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | DEPLETED | CANCELLED
    qr_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qr_codes.id", use_alter=True))
    trace_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id", use_alter=True), index=True)


class MaterialPackage(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "material_packages"
    __table_args__ = (UniqueConstraint("organization_id", "package_number"),)
    material_lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("material_lots.id"), index=True)
    package_number: Mapped[str] = mapped_column(String(40), index=True)
    sequence_no: Mapped[int] = mapped_column(Integer)
    original_quantity: Mapped[Decimal]
    base_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    # AVAILABLE | IN_USE | EMPTY | DAMAGED | CONTAMINATED | CANCELLED
    package_status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")
    qr_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qr_codes.id", use_alter=True))
    trace_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id", use_alter=True), index=True)

    lot: Mapped[MaterialLot] = relationship(lazy="joined")


class ReceiptDocument(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "receipt_documents"
    # COA | RECEIPT | TRANSACTION_STATEMENT | DELIVERY_NOTE | MATERIAL_LABEL | OTHER
    document_type: Mapped[str] = mapped_column(String(30))
    document_number: Mapped[str | None] = mapped_column(String(100))
    issued_date: Mapped[date | None]  # 문서 발행일 (입고일과 구분)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("file_attachments.id"))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    is_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("receipt_documents.id"))
    coa_test_results: Mapped[list[Any] | None]  # [{item, standard, result, judgement}]
    note: Mapped[str | None] = mapped_column(Text)


class DocumentLink(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "document_links"
    __table_args__ = (UniqueConstraint("document_id", "entity_type", "entity_id"),)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt_documents.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))  # RECEIPT | RECEIPT_ITEM | MATERIAL_LOT
    entity_id: Mapped[uuid.UUID] = mapped_column(index=True)


class OcrJob(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "ocr_jobs"
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt_documents.id"), index=True)
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("material_receipts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    document_type: Mapped[str] = mapped_column(String(30))
    # QUEUED | PROCESSING | REVIEW_REQUIRED | COMPLETED | FAILED | CONFIRMED
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    raw_text: Mapped[str | None] = mapped_column(Text)
    structured_result: Mapped[dict[str, Any] | None]
    overall_confidence: Mapped[Decimal | None]
    review_required: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None]


class OcrExtractedField(IdMixin, OrgMixin, Base):
    __tablename__ = "ocr_extracted_fields"
    ocr_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ocr_jobs.id"), index=True)
    item_index: Mapped[int] = mapped_column(Integer, default=0)  # 영수증 다중 품목 분리
    field_name: Mapped[str] = mapped_column(String(50))
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None]
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True)
    corrected_value: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    matched_entity_id: Mapped[uuid.UUID | None]


class InspectionTemplate(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "inspection_templates"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(30))  # RECEIPT | PROCESS | FINISHED_GOODS
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    items: Mapped[list["InspectionTemplateItem"]] = relationship(lazy="selectin", order_by="InspectionTemplateItem.sort_order")


class InspectionTemplateItem(IdMixin, Base):
    __tablename__ = "inspection_template_items"
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inspection_templates.id"), index=True)
    item_code: Mapped[str] = mapped_column(String(40))  # APPEARANCE, PACKAGE_DAMAGE, TEMPERATURE ...
    item_name: Mapped[str] = mapped_column(String(100))
    value_type: Mapped[str] = mapped_column(String(20), default="RESULT")  # RESULT | NUMBER | TEXT
    min_value: Mapped[Decimal | None]
    max_value: Mapped[Decimal | None]
    criteria: Mapped[str | None] = mapped_column(String(300))
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class QualityInspection(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "quality_inspections"
    inspection_number: Mapped[str] = mapped_column(String(40), unique=True)
    target_type: Mapped[str] = mapped_column(String(30))  # RECEIPT | MATERIAL_LOT | PROCESS | FINISHED_GOODS | RETURN
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inspection_templates.id"))
    source: Mapped[str] = mapped_column(String(20), default="FIELD")  # FIELD(현장) | COA
    overall_result: Mapped[str] = mapped_column(String(20))  # PASS | FAIL | HOLD | NOT_APPLICABLE
    nonconformance_reason: Mapped[str | None] = mapped_column(Text)
    disposition: Mapped[str | None] = mapped_column(String(20))  # QUARANTINE | RETURN | DISPOSE
    inspected_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    inspected_at: Mapped[datetime]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    photo_file_ids: Mapped[list[Any] | None]
    note: Mapped[str | None] = mapped_column(Text)
    results: Mapped[list["QualityInspectionResult"]] = relationship(lazy="selectin")


class QualityInspectionResult(IdMixin, Base):
    __tablename__ = "quality_inspection_results"
    inspection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quality_inspections.id"), index=True)
    item_code: Mapped[str] = mapped_column(String(40))
    item_name: Mapped[str] = mapped_column(String(100))
    result: Mapped[str] = mapped_column(String(20))  # PASS | FAIL | HOLD | NOT_APPLICABLE
    measured_value: Mapped[Decimal | None]
    text_value: Mapped[str | None] = mapped_column(String(300))
    standard: Mapped[str | None] = mapped_column(String(300))
    out_of_range: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
