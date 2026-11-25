from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IdMixin, OrgMixin, TimestampMixin


class BomHeader(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "bom_headers"
    __table_args__ = (UniqueConstraint("organization_id", "bom_code"),)
    bom_code: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    versions: Mapped[list["BomVersion"]] = relationship(lazy="selectin", order_by="BomVersion.version_no")


class BomVersion(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "bom_versions"
    __table_args__ = (UniqueConstraint("bom_header_id", "version_no"),)
    bom_header_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bom_headers.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    base_quantity: Mapped[Decimal]  # 기준 배합량 (예: 100)
    base_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    base_fg_quantity: Mapped[Decimal | None]  # 기준 배합량 당 표준 완제품 수량 (예: 100kg → 98EA)
    # DRAFT | PENDING_APPROVAL | APPROVED | OBSOLETE | REJECTED
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    effective_from: Mapped[date | None]
    effective_to: Mapped[date | None]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    change_note: Mapped[str | None] = mapped_column(Text)
    copied_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bom_versions.id"))
    items: Mapped[list["BomItem"]] = relationship(lazy="selectin", order_by="BomItem.sequence")


class BomItem(IdMixin, OrgMixin, Base):
    __tablename__ = "bom_items"
    __table_args__ = (
        CheckConstraint("item_type IN ('FORMULA','PACKAGING','PROCESS_AUXILIARY')", name="item_type"),
        CheckConstraint("quantity_basis IN ('RATIO','FIXED','PER_UNIT')", name="basis"),
    )
    bom_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bom_versions.id"), index=True)
    item_type: Mapped[str] = mapped_column(String(20))
    material_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("materials.id"))
    quantity_basis: Mapped[str] = mapped_column(String(10), default="RATIO")
    ratio_percent: Mapped[Decimal | None]
    standard_quantity: Mapped[Decimal | None]  # FIXED: 기준배합량 당 사용량 / PER_UNIT: 완제품 1단위 당 사용량
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    tolerance_pct: Mapped[Decimal] = mapped_column(default=Decimal("1"))
    sequence: Mapped[int] = mapped_column(Integer, default=10)
    process_step_code: Mapped[str | None] = mapped_column(String(30))
    weighing_group: Mapped[str | None] = mapped_column(String(30))
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    substitute_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    substitute_material_ids: Mapped[list[Any] | None]
    note: Mapped[str | None] = mapped_column(Text)
