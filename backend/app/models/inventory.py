from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, FacilityMixin, IdMixin, OrgMixin, TimestampMixin

TRANSACTION_TYPES = (
    "OPENING", "RECEIPT", "PRODUCTION_ISSUE", "PRODUCTION_RETURN", "SUPPLIER_RETURN", "DISPOSAL",
    "ADJUSTMENT_IN", "ADJUSTMENT_OUT", "TRANSFER_IN", "TRANSFER_OUT", "SAMPLE_USE", "GENERAL_USE",
    "WIP_RECEIPT", "WIP_ISSUE", "FG_RECEIPT", "FG_SHIPMENT", "FG_RETURN", "REVERSAL",
)
ITEM_TYPES = ("MATERIAL", "WB", "WIP", "FG")


class InventoryBalance(IdMixin, OrgMixin, FacilityMixin, Base):
    """현재 잔량. 직접 수정 금지 — inventory service 의 post_movement 로만 변경."""

    __tablename__ = "inventory_balances"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="qty_nonneg"),
        CheckConstraint("reserved_quantity >= 0", name="reserved_nonneg"),
        CheckConstraint("reserved_quantity <= quantity", name="reserved_le_qty"),
        UniqueConstraint(
            "lot_node_id", "package_node_id", "warehouse_id", "storage_location_id",
            name="uq_inventory_balances_stock_key", postgresql_nulls_not_distinct=True,
        ),
    )
    item_type: Mapped[str] = mapped_column(String(10), index=True)  # MATERIAL | WB | WIP | FG
    material_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("materials.id"), index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    lot_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"), index=True)
    package_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    storage_location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("storage_locations.id"))
    quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    reserved_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    # 조회용 비정규화 (원천: 각 LOT 엔티티, 상태 변경 시 동일 트랜잭션에서 동기화)
    lot_number: Mapped[str] = mapped_column(String(40), index=True)
    package_number: Mapped[str | None] = mapped_column(String(40), index=True)
    quality_status: Mapped[str] = mapped_column(String(20), index=True)
    expiry_date: Mapped[date | None] = mapped_column(index=True)
    last_movement_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(Integer, default=1)


class InventoryTransaction(IdMixin, OrgMixin, FacilityMixin, Base):
    """불변 수불 원장 (UPDATE/DELETE 금지 — DB 트리거로 차단)."""

    __tablename__ = "inventory_transactions"
    __table_args__ = (
        UniqueConstraint("organization_id", "transaction_number"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_inventory_transactions_idem"),
        CheckConstraint("quantity > 0", name="qty_pos"),
        CheckConstraint("direction IN ('IN','OUT')", name="direction"),
        CheckConstraint("balance_after >= 0", name="after_nonneg"),
        Index("ix_inventory_transactions_ref", "reference_type", "reference_id"),
    )
    transaction_number: Mapped[str] = mapped_column(String(40))
    transaction_type: Mapped[str] = mapped_column(String(30), index=True)
    item_type: Mapped[str] = mapped_column(String(10))
    material_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("materials.id"), index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    balance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_balances.id"), index=True)
    trace_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"), index=True)  # 잔량 보유 노드(포장 또는 LOT)
    lot_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"), index=True)
    material_lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("material_lots.id"), index=True)
    package_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    storage_location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("storage_locations.id"))
    quality_status: Mapped[str | None] = mapped_column(String(20))
    quantity: Mapped[Decimal]
    base_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    direction: Mapped[str] = mapped_column(String(3))
    balance_before: Mapped[Decimal]
    balance_after: Mapped[Decimal]
    reference_type: Mapped[str | None] = mapped_column(String(40))
    reference_id: Mapped[uuid.UUID | None]
    transaction_group_id: Mapped[uuid.UUID] = mapped_column(index=True)
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inventory_transactions.id"), unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    performed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    performed_at: Mapped[datetime] = mapped_column(index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(index=True)


class InventoryReservation(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (CheckConstraint("quantity > 0", name="qty_pos"),)
    balance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_balances.id"), index=True)
    production_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_orders.id", use_alter=True), index=True)
    production_order_item_id: Mapped[uuid.UUID | None]
    shipment_id: Mapped[uuid.UUID | None]
    quantity: Mapped[Decimal]
    consumed_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | RELEASED | CONSUMED
    reason: Mapped[str | None] = mapped_column(Text)


class StockTransfer(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "stock_transfers"
    transfer_number: Mapped[str] = mapped_column(String(40), unique=True)
    balance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_balances.id"))
    to_warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    to_storage_location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("storage_locations.id"))
    quantity: Mapped[Decimal]
    transaction_group_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")
    reason: Mapped[str | None] = mapped_column(Text)


class StockCount(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    """재고 실사. 차이는 승인된 조정(stock_adjustments)으로만 반영."""

    __tablename__ = "stock_counts"
    count_number: Mapped[str] = mapped_column(String(40), unique=True)
    count_date: Mapped[date]
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    lines: Mapped[list[Any]]  # [{balance_id, system_qty, counted_qty, diff, adjustment_id}]
    status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")  # SUBMITTED | ADJUSTMENT_REQUESTED | CLOSED
    note: Mapped[str | None] = mapped_column(Text)


class StockAdjustment(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "stock_adjustments"
    __table_args__ = (CheckConstraint("quantity > 0", name="qty_pos"),)
    adjustment_number: Mapped[str] = mapped_column(String(40), unique=True)
    balance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inventory_balances.id"))
    direction: Mapped[str] = mapped_column(String(3))  # IN | OUT
    adjustment_kind: Mapped[str] = mapped_column(String(20), default="ADJUSTMENT")  # ADJUSTMENT | DISPOSAL | SUPPLIER_RETURN | SAMPLE_USE | GENERAL_USE
    quantity: Mapped[Decimal]
    reason: Mapped[str] = mapped_column(Text)
    stock_count_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_counts.id"))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING | APPROVED | REJECTED
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    transaction_group_id: Mapped[uuid.UUID | None]
