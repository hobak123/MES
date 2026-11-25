from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, FacilityMixin, IdMixin, OrgMixin, TimestampMixin

NODE_TYPES = (
    "RAW_MATERIAL_LOT", "RAW_MATERIAL_PACKAGE", "WEIGHING_BATCH", "PROCESS_WIP",
    "FINISHED_GOODS_LOT", "FINISHED_GOODS_PACKAGE", "SHIPMENT", "CUSTOMER",
)
EVENT_TYPES = (
    "RECEIPT", "SPLIT", "WEIGHING", "MIXING", "PROCESSING", "MERGE", "PACKAGING",
    "FINISHING", "REWORK", "SHIPMENT", "RETURN", "REVERSAL",
)


class TraceNode(IdMixin, OrgMixin, FacilityMixin, Base):
    __tablename__ = "trace_nodes"
    __table_args__ = (UniqueConstraint("node_type", "entity_id"),)
    node_type: Mapped[str] = mapped_column(String(30), index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(index=True)
    lot_number: Mapped[str] = mapped_column(String(60), index=True)
    label: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime]


class TraceEvent(IdMixin, OrgMixin, FacilityMixin, Base):
    """불변 추적 이벤트. 취소/재작업은 새 이벤트로 추가 (덮어쓰기 금지)."""

    __tablename__ = "trace_events"
    event_type: Mapped[str] = mapped_column(String(20), index=True)
    production_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_orders.id"), index=True)
    production_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_batches.id"), index=True)
    reference_type: Mapped[str | None] = mapped_column(String(40))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    is_reversed: Mapped[bool] = mapped_column(default=False)
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_events.id"))
    performed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    performed_at: Mapped[datetime] = mapped_column(index=True)
    note: Mapped[str | None] = mapped_column(Text)
    inputs: Mapped[list["TraceEventInput"]] = relationship(lazy="selectin")
    outputs: Mapped[list["TraceEventOutput"]] = relationship(lazy="selectin")


class TraceEventInput(IdMixin, Base):
    __tablename__ = "trace_event_inputs"
    trace_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_events.id"), index=True)
    trace_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"), index=True)
    quantity: Mapped[Decimal | None]
    unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("units.id"))


class TraceEventOutput(IdMixin, Base):
    __tablename__ = "trace_event_outputs"
    trace_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_events.id"), index=True)
    trace_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"), index=True)
    quantity: Mapped[Decimal | None]
    unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("units.id"))


class RecallCase(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "recall_cases"
    __table_args__ = (UniqueConstraint("organization_id", "case_number"),)
    case_number: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), default="HIGH")
    source_node_ids: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | HOLDING | IN_PROGRESS | CLOSED
    opened_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None]
    lots: Mapped[list["RecallCaseLot"]] = relationship(lazy="selectin")
    shipments: Mapped[list["RecallCaseShipment"]] = relationship(lazy="selectin")


class RecallCaseLot(IdMixin, Base):
    __tablename__ = "recall_case_lots"
    __table_args__ = (UniqueConstraint("recall_case_id", "trace_node_id"),)
    recall_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recall_cases.id"), index=True)
    trace_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"))
    node_type: Mapped[str] = mapped_column(String(30))
    lot_number: Mapped[str] = mapped_column(String(60))
    stock_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    hold_applied: Mapped[bool] = mapped_column(default=False)
    previous_quality_status: Mapped[str | None] = mapped_column(String(20))


class RecallCaseShipment(IdMixin, Base):
    __tablename__ = "recall_case_shipments"
    __table_args__ = (
        UniqueConstraint("recall_case_id", "shipment_item_id"),
        CheckConstraint("recovered_quantity >= 0", name="recovered_nonneg"),
    )
    recall_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recall_cases.id"), index=True)
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipment_headers.id"))
    shipment_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipment_items.id"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    fg_lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finished_goods_lots.id"))
    shipped_quantity: Mapped[Decimal]
    target_quantity: Mapped[Decimal]
    recovered_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING | PARTIAL | RECOVERED
