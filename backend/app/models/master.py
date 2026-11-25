from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, FacilityMixin, IdMixin, OrgMixin, TimestampMixin

MATERIAL_MAJOR = ("MAIN_MATERIAL", "SUB_MATERIAL", "OTHER_MATERIAL")
MAJOR_PREFIX = {"MAIN_MATERIAL": "RM", "SUB_MATERIAL": "PM", "OTHER_MATERIAL": "OM"}


class Unit(IdMixin, Base):
    __tablename__ = "units"
    code: Mapped[str] = mapped_column(String(20), unique=True)  # kg, g, L, mL, EA, BOX ...
    name: Mapped[str] = mapped_column(String(50))
    dimension: Mapped[str] = mapped_column(String(20))  # MASS | VOLUME | COUNT
    factor_to_base: Mapped[Decimal]  # 차원 기준 단위(kg, L, EA) 대비 배율
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UnitConversion(IdMixin, OrgMixin, TimestampMixin, Base):
    """품목별 단위 환산 (예: 1 BOX = 20 EA, 밀도 기반 kg↔L). 검증자 기록 필수."""

    __tablename__ = "unit_conversions"
    material_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("materials.id"), index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    from_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    to_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    factor: Mapped[Decimal]
    is_density: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MaterialCategory(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "material_categories"
    __table_args__ = (UniqueConstraint("organization_id", "major", "code"),)
    major: Mapped[str] = mapped_column(String(30))  # MAIN_MATERIAL | SUB_MATERIAL | OTHER_MATERIAL
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Supplier(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200), index=True)
    business_number: Mapped[str | None] = mapped_column(String(20))
    contact_name: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(500))
    supplied_materials: Mapped[list[Any] | None]  # material_id 목록
    trade_status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | SUSPENDED | TERMINATED
    note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Customer(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200), index=True)
    business_number: Mapped[str | None] = mapped_column(String(20))
    contact_name: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(500))
    delivery_address: Mapped[str | None] = mapped_column(String(500))
    trade_status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Warehouse(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    # AMBIENT | REFRIGERATED | FROZEN | PACKAGING | CONSUMABLE | WIP | FINISHED | QUARANTINE
    warehouse_type: Mapped[str] = mapped_column(String(20))
    temperature_min: Mapped[Decimal | None]
    temperature_max: Mapped[Decimal | None]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StorageLocation(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "storage_locations"
    __table_args__ = (UniqueConstraint("warehouse_id", "code"),)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    zone: Mapped[str | None] = mapped_column(String(50))
    rack: Mapped[str | None] = mapped_column(String(50))
    name: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Equipment(IdMixin, OrgMixin, FacilityMixin, TimestampMixin, Base):
    __tablename__ = "equipments"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    equipment_type: Mapped[str] = mapped_column(String(30))  # SCALE | MIXER | HEATER | FILLER | METAL_DETECTOR | LINE ...
    line_code: Mapped[str | None] = mapped_column(String(30))
    interface_type: Mapped[str | None] = mapped_column(String(20))  # USB | BLUETOOTH | RS232 | TCP
    interface_config: Mapped[dict[str, Any] | None]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Material(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "materials"
    __table_args__ = (
        UniqueConstraint("organization_id", "material_code"),
        CheckConstraint("major_category IN ('MAIN_MATERIAL','SUB_MATERIAL','OTHER_MATERIAL')", name="major"),
        CheckConstraint("safety_stock IS NULL OR safety_stock >= 0", name="safety_nonneg"),
    )
    material_code: Mapped[str] = mapped_column(String(30), index=True)
    material_name: Mapped[str] = mapped_column(String(200), index=True)
    major_category: Mapped[str] = mapped_column(String(30))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("material_categories.id"))
    food_type: Mapped[str | None] = mapped_column(String(100))
    is_food_additive: Mapped[bool] = mapped_column(Boolean, default=False)
    specification: Mapped[str | None] = mapped_column(String(200))
    base_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))
    purchase_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("units.id"))
    stock_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("units.id"))
    conversion_note: Mapped[str | None] = mapped_column(String(200))
    density_kg_per_l: Mapped[Decimal | None]
    default_supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"))
    manufacturer_name: Mapped[str | None] = mapped_column(String(200))
    default_country_of_origin: Mapped[str | None] = mapped_column(String(100))
    storage_condition: Mapped[str | None] = mapped_column(String(50))  # AMBIENT | REFRIGERATED | FROZEN
    default_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    safety_stock: Mapped[Decimal | None]
    max_stock: Mapped[Decimal | None]
    allergen_information: Mapped[str | None] = mapped_column(String(300))
    coa_required: Mapped[bool] = mapped_column(Boolean, default=False)
    inspection_required: Mapped[bool] = mapped_column(Boolean, default=True)
    expiry_tracking_required: Mapped[bool] = mapped_column(Boolean, default=True)
    lot_tracking_required: Mapped[bool] = mapped_column(Boolean, default=True)
    production_usable: Mapped[bool] = mapped_column(Boolean, default=True)
    inspection_template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inspection_templates.id", use_alter=True))
    receiving_temp_min: Mapped[Decimal | None]
    receiving_temp_max: Mapped[Decimal | None]
    image_file_id: Mapped[uuid.UUID | None]
    note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    base_unit: Mapped[Unit] = relationship(foreign_keys=[base_unit_id], lazy="joined")
    category: Mapped[MaterialCategory | None] = relationship(lazy="joined")


class Product(IdMixin, OrgMixin, TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("organization_id", "product_code"),)
    product_code: Mapped[str] = mapped_column(String(30), index=True)
    product_name: Mapped[str] = mapped_column(String(200), index=True)
    food_type: Mapped[str | None] = mapped_column(String(100))
    report_number: Mapped[str | None] = mapped_column(String(50))  # 품목제조보고번호
    specification: Mapped[str | None] = mapped_column(String(200))
    content_quantity: Mapped[Decimal]  # 내용량 (예: 1)
    content_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))  # 예: kg
    base_unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"))  # 완제품 재고 단위 (EA)
    packaging_unit: Mapped[str | None] = mapped_column(String(50))
    units_per_box: Mapped[int | None]
    standard_batch_size: Mapped[Decimal | None]
    standard_batch_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("units.id"))
    shelf_life_days: Mapped[int | None]
    shelf_life_basis: Mapped[str] = mapped_column(String(30), default="MANUFACTURED_DATE")
    storage_condition: Mapped[str | None] = mapped_column(String(50))
    standard_yield_pct: Mapped[Decimal | None]
    default_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    image_file_id: Mapped[uuid.UUID | None]
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    base_unit: Mapped[Unit] = relationship(foreign_keys=[base_unit_id], lazy="joined")
    content_unit: Mapped[Unit] = relationship(foreign_keys=[content_unit_id], lazy="joined")
