from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, FacilityMixin, IdMixin, OrgMixin, TimestampMixin

ORDER_STATUSES = (
    "DRAFT", "APPROVED", "WAITING", "WEIGHING", "WEIGHING_COMPLETED", "PRODUCING",
    "QUALITY_PENDING", "COMPLETED", "CLOSED", "HOLD", "CANCELLED",
)


class ProcessDefinition(IdMixin, OrgMixin, TimestampMixin, Base):
    """공정 마스터 (계량/투입/혼합/가열/살균/냉각/여과/숙성/건조/충진/금속검출/포장/완제품검사)."""

    __tablename__ = "process_definitions"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    process_type: Mapped[str] = mapped_column(String(30))  # WEIGHING | MIXING | HEATING | ... | PACKAGING | FG_INSPECTION
    default_sequence: Mapped[int] = mapped_column(Integer, default=10)
    work_center: Mapped[str | None] = mapped_column(String(50))
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipments.id"))
    standard_minutes: Mapped[Decimal | None]
    standard_temperature: Mapped[Decimal | None]
    standard_pressure: Mapped[Decimal | None]
    standard_speed: Mapped[Decimal | None]
    parameter_specs: Mapped[list[Any] | None]  # [{code,name,unit,min,max,required}]
    inspection_criteria: Mapped[str | None] = mapped_column(Text)
    required_records: Mapped[list[Any] | None]
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    qr_required: Mapped[bool] = mapped_column(Boolean, default=True)
    is_ccp: Mapped[bool] = mapped_column(Boolean, default=False)
    output_type: Mapped[str] = mapped_column(String(10), default="WIP")  # WIP | FG | NONE
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductionRouting(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "production_routings"
    __table_args__ = (UniqueConstraint("organization_id", "routing_code"),)
    routing_code: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    versions: Mapped[list["RoutingVersion"]] = relationship(lazy="selectin", order_by="RoutingVersion.version_no")


class RoutingVersion(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "routing_versions"
    __table_args__ = (UniqueConstraint("routing_id", "version_no"),)
    routing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_routings.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT | APPROVED | OBSOLETE
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    steps: Mapped[list["RoutingStep"]] = relationship(lazy="selectin", order_by="RoutingStep.sequence")


class RoutingStep(IdMixin, OrgMixin, Base):
    __tablename__ = "routing_steps"
    __table_args__ = (UniqueConstraint("routing_version_id", "sequence"),)
    routing_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("routing_versions.id"), index=True)
    process_definition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_definitions.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    step_name: Mapped[str] = mapped_column(String(100))
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipments.id"))
    parameter_specs: Mapped[list[Any] | None]  # 공정 마스터 조건 덮어쓰기
    output_type: Mapped[str] = mapped_column(String(10), default="WIP")


class ProductionOrder(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "production_orders"
    __table_args__ = (
        UniqueConstraint("organization_id", "order_number"),
        CheckConstraint("planned_quantity > 0", name="planned_pos"),
        CheckConstraint("batch_count >= 1", name="batch_pos"),
    )
    order_number: Mapped[str] = mapped_column(String(40), index=True)
    production_date: Mapped[date] = mapped_column(index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    bom_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bom_versions.id"))
    bom_snapshot: Mapped[dict[str, Any]]  # 승인 BOM 스냅샷 (불변)
    routing_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("routing_versions.id"))
    routing_snapshot: Mapped[dict[str, Any] | None]
    planned_quantity: Mapped[Decimal]  # 계획 배합량
    planned_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    target_fg_quantity: Mapped[Decimal | None]  # 목표 완제품량 (EA)
    batch_count: Mapped[int] = mapped_column(Integer, default=1)
    batch_size: Mapped[Decimal]
    line_code: Mapped[str | None] = mapped_column(String(30))
    work_center: Mapped[str | None] = mapped_column(String(50))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    scheduled_start: Mapped[datetime | None]
    scheduled_end: Mapped[datetime | None]
    actual_start: Mapped[datetime | None]
    actual_end: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    status_before_hold: Mapped[str | None] = mapped_column(String(30))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    closed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None]
    mass_balance: Mapped[dict[str, Any] | None]
    note: Mapped[str | None] = mapped_column(Text)
    qr_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qr_codes.id", use_alter=True))

    items: Mapped[list["ProductionOrderItem"]] = relationship(lazy="selectin", order_by="ProductionOrderItem.sequence")
    batches: Mapped[list["ProductionBatch"]] = relationship(lazy="selectin", order_by="ProductionBatch.batch_no")


class ProductionOrderItem(IdMixin, OrgMixin, Base):
    __tablename__ = "production_order_items"
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    bom_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bom_items.id"))
    item_type: Mapped[str] = mapped_column(String(20))
    material_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("materials.id"), index=True)
    quantity_basis: Mapped[str] = mapped_column(String(10))
    ratio_percent: Mapped[Decimal | None]
    required_quantity: Mapped[Decimal]  # 지시 전체 필요량
    per_batch_quantity: Mapped[Decimal]
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    tolerance_pct: Mapped[Decimal]
    sequence: Mapped[int] = mapped_column(Integer)
    weighing_group: Mapped[str | None] = mapped_column(String(30))
    process_step_code: Mapped[str | None] = mapped_column(String(30))
    substitute_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    substitute_material_ids: Mapped[list[Any] | None]
    issued_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))


class ProductionBatch(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "production_batches"
    __table_args__ = (UniqueConstraint("organization_id", "batch_number"), UniqueConstraint("production_order_id", "batch_no"))
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    batch_number: Mapped[str] = mapped_column(String(40))
    batch_no: Mapped[int] = mapped_column(Integer)
    planned_quantity: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(30), default="WAITING")  # WAITING | WEIGHING | WEIGHED | PRODUCING | COMPLETED | CANCELLED
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]


class WeighingBatch(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    """계량 반제품 (WB)."""

    __tablename__ = "weighing_batches"
    __table_args__ = (UniqueConstraint("organization_id", "lot_number"),)
    lot_number: Mapped[str | None] = mapped_column(String(40), index=True)
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    production_batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_batches.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    bom_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bom_versions.id"))
    weighing_group: Mapped[str] = mapped_column(String(30), default="DEFAULT")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("weighing_batches.id"))  # 개별 용기 → 상위 WB
    total_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    completed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None]
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    storage_location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("storage_locations.id"))
    usable_until: Mapped[datetime | None]
    quality_status: Mapped[str] = mapped_column(String(20), default="PASSED")  # PASSED | HOLD | REJECTED
    # WEIGHING | READY | HOLD | PARTIALLY_CONSUMED | CONSUMED | DISCARDED | CANCELLED
    status: Mapped[str] = mapped_column(String(30), default="WEIGHING", index=True)
    next_process_code: Mapped[str | None] = mapped_column(String(30))
    qr_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qr_codes.id", use_alter=True))
    trace_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id", use_alter=True), index=True)


class WeighingRecord(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "weighing_records"
    __table_args__ = (CheckConstraint("actual_quantity > 0", name="actual_pos"),)
    weighing_batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("weighing_batches.id"), index=True)
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    production_batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_batches.id"), index=True)
    production_order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_order_items.id"))
    material_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("materials.id"), index=True)
    material_lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("material_lots.id"), index=True)
    material_package_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("material_packages.id"), index=True)
    is_substitute: Mapped[bool] = mapped_column(Boolean, default=False)
    target_quantity: Mapped[Decimal]
    actual_quantity: Mapped[Decimal]
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    tolerance_min: Mapped[Decimal]
    tolerance_max: Mapped[Decimal]
    gross_weight: Mapped[Decimal | None]
    tare_weight: Mapped[Decimal | None]
    is_stable: Mapped[bool | None]
    scale_equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipments.id"))
    input_method: Mapped[str] = mapped_column(String(20), default="MANUAL")  # MANUAL | SCALE
    inventory_transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inventory_transactions.id"))
    weighed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    weighed_at: Mapped[datetime]
    approval_status: Mapped[str] = mapped_column(String(20), default="NOT_REQUIRED")  # NOT_REQUIRED | APPROVED
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # CONFIRMED | REVERSED
    status: Mapped[str] = mapped_column(String(20), default="CONFIRMED", index=True)
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reversed_at: Mapped[datetime | None]
    reversal_reason: Mapped[str | None] = mapped_column(Text)


class WipLot(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    """공정 반제품 LOT (WIP)."""

    __tablename__ = "wip_lots"
    __table_args__ = (UniqueConstraint("organization_id", "lot_number"),)
    lot_number: Mapped[str] = mapped_column(String(40), index=True)
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    production_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_batches.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    process_execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_executions.id", use_alter=True), index=True)
    routing_step_sequence: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[Decimal]
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    quality_status: Mapped[str] = mapped_column(String(20), default="PASSED")
    status: Mapped[str] = mapped_column(String(30), default="READY")  # READY | HOLD | PARTIALLY_CONSUMED | CONSUMED | DISCARDED
    qr_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qr_codes.id", use_alter=True))
    trace_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trace_nodes.id", use_alter=True), index=True)


class ProcessExecution(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "process_executions"
    execution_number: Mapped[str] = mapped_column(String(40), unique=True)
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    production_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_batches.id"), index=True)
    routing_step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("routing_steps.id"))
    routing_step_sequence: Mapped[int | None] = mapped_column(Integer)
    process_definition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_definitions.id"))
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipments.id"))
    is_rework: Mapped[bool] = mapped_column(Boolean, default=False)
    merge_approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime | None]
    paused_at: Mapped[datetime | None]
    paused_minutes: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    completed_at: Mapped[datetime | None]
    total_input_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    good_output_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    defective_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    waste_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    process_loss_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    moisture_change_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))  # +흡수/물투입, -증발
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    process_yield: Mapped[Decimal | None]
    quality_status: Mapped[str] = mapped_column(String(20), default="PASSED")
    inspection_result: Mapped[str | None] = mapped_column(String(20))
    operated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    # WAITING | IN_PROGRESS | PAUSED | COMPLETED | HOLD | FAILED | CANCELLED
    status: Mapped[str] = mapped_column(String(20), default="WAITING", index=True)
    note: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    photo_file_ids: Mapped[list[Any] | None]


class ProcessInput(IdMixin, OrgMixin, Base):
    __tablename__ = "process_inputs"
    __table_args__ = (CheckConstraint("quantity > 0", name="qty_pos"),)
    process_execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_executions.id"), index=True)
    input_type: Mapped[str] = mapped_column(String(10))  # WB | WIP | FG | MATERIAL
    trace_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"), index=True)
    lot_number: Mapped[str] = mapped_column(String(40))
    production_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_batches.id"))
    quantity: Mapped[Decimal]
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    inventory_transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inventory_transactions.id"))
    input_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    input_at: Mapped[datetime]


class ProcessOutput(IdMixin, OrgMixin, Base):
    __tablename__ = "process_outputs"
    process_execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_executions.id"), index=True)
    output_type: Mapped[str] = mapped_column(String(10))  # WIP | FG
    trace_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trace_nodes.id"), index=True)
    lot_number: Mapped[str] = mapped_column(String(40))
    quantity: Mapped[Decimal]
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    inventory_transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inventory_transactions.id"))


class ProcessParameterRecord(IdMixin, OrgMixin, Base):
    __tablename__ = "process_parameter_records"
    process_execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_executions.id"), index=True)
    parameter_code: Mapped[str] = mapped_column(String(40))  # TEMPERATURE | PRESSURE | SPEED | TIME | METAL_DETECT ...
    parameter_name: Mapped[str] = mapped_column(String(100))
    value_numeric: Mapped[Decimal | None]
    value_text: Mapped[str | None] = mapped_column(String(300))
    unit: Mapped[str | None] = mapped_column(String(20))
    min_value: Mapped[Decimal | None]
    max_value: Mapped[Decimal | None]
    out_of_range: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="MANUAL")  # MANUAL | SENSOR | PLC
    is_ccp: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime]


class PackagingConsumption(IdMixin, OrgMixin, FacilityMixin, Base):
    __tablename__ = "packaging_consumptions"
    process_execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_executions.id"), index=True)
    production_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("production_orders.id"), index=True)
    material_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("materials.id"))
    material_lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("material_lots.id"), index=True)
    material_package_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("material_packages.id"))
    issued_quantity: Mapped[Decimal]  # 수불 차감량 = used + defective + waste
    used_quantity: Mapped[Decimal]
    defective_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    waste_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    returned_quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    fg_lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("finished_goods_lots.id", use_alter=True))
    inventory_transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inventory_transactions.id"))
    consumed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    consumed_at: Mapped[datetime]
