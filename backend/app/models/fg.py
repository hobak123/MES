from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, FacilityMixin, IdMixin, OrgMixin, TimestampMixin


class FinishedGoodsLot(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "finished_goods_lots"
    __table_args__ = (UniqueConstraint("organization_id", "lot_number"),)
    lot_number: Mapped[str] = mapped_column(String(40), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    production_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_batches.id"), index=True)
    process_execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("process_executions.id"))
    manufactured_date: Mapped[date]
    production_started_at: Mapped[datetime | None]
    production_ended_at: Mapped[datetime | None]
    expiry_date: Mapped[date | None] = mapped_column(index=True)
    expiry_basis_date: Mapped[date | None]
    content_quantity: Mapped[Decimal]
    content_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    planned_quantity: Mapped[Decimal | None]
    total_produced_quantity: Mapped[Decimal]
    good_quantity: Mapped[Decimal]
    defective_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    waste_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    total_input_quantity: Mapped[Decimal | None]  # 실제 총 식품원료 투입량(kg)
    net_weight: Mapped[Decimal | None]  # 정상 순중량(kg)
    loss_quantity: Mapped[Decimal | None]
    base_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    production_yield: Mapped[Decimal | None]
    # QUALITY_PENDING | RELEASED | HOLD | REJECTED
    quality_status: Mapped[str] = mapped_column(String(20), default="QUALITY_PENDING", index=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    released_at: Mapped[datetime | None]
    responsible_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | DEPLETED
    qr_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qr_codes.id", use_alter=True))
    trace_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id", use_alter=True), index=True)


class FinishedGoodsPackage(IdMixin, OrgMixin, TimestampMixin, Base):
    """완제품 개별 제품/박스/팔레트 단위. 상위 LOT 에 수량과 함께 연결."""

    __tablename__ = "finished_goods_packages"
    __table_args__ = (UniqueConstraint("organization_id", "package_number"),)
    fg_lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finished_goods_lots.id"), index=True)
    parent_package_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finished_goods_packages.id"))
    package_level: Mapped[str] = mapped_column(String(10))  # UNIT | BOX | PALLET
    package_number: Mapped[str] = mapped_column(String(40))
    quantity: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(20), default="IN_STOCK")  # IN_STOCK | SHIPPED | RETURNED
    qr_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qr_codes.id", use_alter=True))
    trace_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id", use_alter=True))


class ShipmentHeader(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "shipment_headers"
    __table_args__ = (UniqueConstraint("organization_id", "shipment_number"),)
    shipment_number: Mapped[str] = mapped_column(String(40), index=True)
    shipment_date: Mapped[date] = mapped_column(index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    delivery_address: Mapped[str | None] = mapped_column(String(500))
    delivery_info: Mapped[dict[str, Any] | None]  # 차량/기사/운송장
    handler_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)  # DRAFT | CONFIRMED | REVERSED | CANCELLED
    transaction_group_id: Mapped[uuid.UUID | None]
    trace_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id", use_alter=True))
    note: Mapped[str | None] = mapped_column(Text)
    items: Mapped[list["ShipmentItem"]] = relationship(lazy="selectin", order_by="ShipmentItem.line_no")


class ShipmentItem(IdMixin, OrgMixin, Base):
    __tablename__ = "shipment_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="qty_pos"),)
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipment_headers.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    fg_lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finished_goods_lots.id"), index=True)
    fg_package_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finished_goods_packages.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    quantity: Mapped[Decimal]
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    returned_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    inventory_transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inventory_transactions.id"))


class ShipmentReturn(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "shipment_returns"
    __table_args__ = (CheckConstraint("quantity > 0", name="qty_pos"),)
    return_number: Mapped[str] = mapped_column(String(40), unique=True)
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipment_headers.id"), index=True)
    shipment_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipment_items.id"))
    fg_lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finished_goods_lots.id"), index=True)
    quantity: Mapped[Decimal]
    reason: Mapped[str] = mapped_column(Text)
    quarantine_warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    # QUARANTINED | RESTOCKED | DISPOSED
    status: Mapped[str] = mapped_column(String(20), default="QUARANTINED")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
