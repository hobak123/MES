"""기준정보: 원료/분류/공급업체/거래처/제품/창고/위치/설비/단위/검수양식."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit, snapshot
from app.core.db import get_db
from app.core.deps import CurrentUser, require
from app.core.errors import BusinessRule, Conflict
from app.core.responses import ok, paged
from app.models import (
    Customer, Equipment, InspectionTemplate, Material, MaterialCategory, Product, StorageLocation, Supplier, Unit,
    UnitConversion, Warehouse,
)
from app.models.master import MAJOR_PREFIX
from app.repositories.common import PageParams, get_owned, paginate, ser

router = APIRouter(tags=["기준정보"])

Major = Literal["MAIN_MATERIAL", "SUB_MATERIAL", "OTHER_MATERIAL"]


def _apply(obj: Any, data: dict[str, Any]) -> None:
    for k, v in data.items():
        setattr(obj, k, v)


def _next_code(db: Session, model: Any, col: Any, org_id: uuid.UUID, prefix: str) -> str:
    rows = db.execute(select(col).where(model.organization_id == org_id, col.like(f"{prefix}-%")).with_for_update()).scalars().all()
    nums = [int(r.split("-")[-1]) for r in rows if r.split("-")[-1].isdigit()]
    return f"{prefix}-{(max(nums) if nums else 0) + 1:06d}"


def _unique(db: Session, model: Any, col: Any, value: str, org_id: uuid.UUID, exclude_id: uuid.UUID | None = None) -> None:
    stmt = select(model.id).where(col == value, model.organization_id == org_id)
    if exclude_id:
        stmt = stmt.where(model.id != exclude_id)
    if db.execute(stmt).first():
        raise Conflict("DUPLICATE_CODE", f"이미 사용 중인 코드입니다: {value}")


# ------------------------------------------------------------------ units

@router.get("/units")
def list_units(db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    return ok([ser(u) for u in db.execute(select(Unit).order_by(Unit.dimension, Unit.code)).scalars()])


class UnitConversionIn(BaseModel):
    material_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    from_unit_id: uuid.UUID
    to_unit_id: uuid.UUID
    factor: Decimal = Field(gt=0)
    is_density: bool = False


@router.get("/unit-conversions")
def list_conversions(db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    rows = db.execute(select(UnitConversion).where(UnitConversion.organization_id == cu.organization_id)).scalars()
    return ok([ser(r) for r in rows])


@router.post("/unit-conversions")
def create_conversion(body: UnitConversionIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    obj = UnitConversion(organization_id=cu.organization_id, created_by=cu.id, verified_by=cu.id, **body.model_dump())
    db.add(obj)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "UNIT_CONVERSION", obj.id, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


# ------------------------------------------------------------------ categories

class CategoryIn(BaseModel):
    major: Major
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 0
    is_active: bool = True


@router.get("/material-categories")
def list_categories(major: Major | None = None, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    stmt = select(MaterialCategory).where(MaterialCategory.organization_id == cu.organization_id)
    if major:
        stmt = stmt.where(MaterialCategory.major == major)
    return ok([ser(c) for c in db.execute(stmt.order_by(MaterialCategory.major, MaterialCategory.sort_order)).scalars()])


@router.post("/material-categories")
def create_category(body: CategoryIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    if db.execute(select(MaterialCategory.id).where(MaterialCategory.organization_id == cu.organization_id,
                                                    MaterialCategory.major == body.major, MaterialCategory.code == body.code)).first():
        raise Conflict("DUPLICATE_CODE", "이미 존재하는 분류 코드입니다.")
    obj = MaterialCategory(organization_id=cu.organization_id, created_by=cu.id, **body.model_dump())
    db.add(obj)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "MATERIAL_CATEGORY", obj.id, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


# ------------------------------------------------------------------ materials

class MaterialBase(BaseModel):
    material_name: str | None = Field(default=None, max_length=200)
    category_id: uuid.UUID | None = None
    food_type: str | None = None
    is_food_additive: bool | None = None
    specification: str | None = None
    base_unit_id: uuid.UUID | None = None
    purchase_unit_id: uuid.UUID | None = None
    stock_unit_id: uuid.UUID | None = None
    conversion_note: str | None = None
    density_kg_per_l: Decimal | None = Field(default=None, gt=0)
    default_supplier_id: uuid.UUID | None = None
    manufacturer_name: str | None = None
    default_country_of_origin: str | None = None
    storage_condition: str | None = None
    default_warehouse_id: uuid.UUID | None = None
    safety_stock: Decimal | None = Field(default=None, ge=0)
    max_stock: Decimal | None = Field(default=None, ge=0)
    allergen_information: str | None = None
    coa_required: bool | None = None
    inspection_required: bool | None = None
    expiry_tracking_required: bool | None = None
    lot_tracking_required: bool | None = None
    production_usable: bool | None = None
    inspection_template_id: uuid.UUID | None = None
    receiving_temp_min: Decimal | None = None
    receiving_temp_max: Decimal | None = None
    image_file_id: uuid.UUID | None = None
    note: str | None = None
    is_active: bool | None = None


class MaterialCreate(MaterialBase):
    material_code: str | None = Field(default=None, max_length=30)
    material_name: str = Field(min_length=1, max_length=200)
    major_category: Major
    base_unit_id: uuid.UUID


class MaterialPatch(MaterialBase):
    reason: str | None = None


def _material_out(db: Session, m: Material) -> dict:
    return ser(m, base_unit_code=m.base_unit.code if m.base_unit else None,
               category_name=m.category.name if m.category else None)


@router.get("/materials")
def list_materials(
    p: PageParams = Depends(), major_category: Major | None = None, category_id: uuid.UUID | None = None,
    is_active: bool | None = None, production_usable: bool | None = None,
    db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read")),
):
    stmt = select(Material).where(Material.organization_id == cu.organization_id)
    if major_category:
        stmt = stmt.where(Material.major_category == major_category)
    if category_id:
        stmt = stmt.where(Material.category_id == category_id)
    if is_active is not None:
        stmt = stmt.where(Material.is_active == is_active)
    if production_usable is not None:
        stmt = stmt.where(Material.production_usable == production_usable)
    rows, total = paginate(db, stmt, p, Material, [Material.material_code, Material.material_name, Material.food_type], Material.material_code)
    return paged([_material_out(db, m) for m in rows], total, p.page, p.size)


@router.post("/materials")
def create_material(body: MaterialCreate, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    data = body.model_dump(exclude_none=True)
    prefix = MAJOR_PREFIX[body.major_category]
    code = data.pop("material_code", None) or _next_code(db, Material, Material.material_code, cu.organization_id, prefix)
    _unique(db, Material, Material.material_code, code, cu.organization_id)
    if body.major_category == "OTHER_MATERIAL":
        data.setdefault("production_usable", False)
        data.setdefault("expiry_tracking_required", False)
    if body.major_category == "SUB_MATERIAL":
        data.setdefault("expiry_tracking_required", False)
    if body.category_id:
        cat = get_owned(db, MaterialCategory, body.category_id, cu.organization_id, "원료 분류")
        if cat.major != body.major_category:
            raise BusinessRule("CATEGORY_MISMATCH", "중분류가 선택한 대분류에 속하지 않습니다.")
    m = Material(organization_id=cu.organization_id, material_code=code, created_by=cu.id, **data)
    db.add(m)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "MATERIAL", m.id, after=snapshot(m))
    db.commit()
    db.refresh(m)
    return ok(_material_out(db, m), "원료가 등록되었습니다. (재고는 입고 확정 시 생성됩니다)")


@router.get("/materials/{material_id}")
def get_material(material_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    return ok(_material_out(db, get_owned(db, Material, material_id, cu.organization_id, "원료")))


@router.patch("/materials/{material_id}")
def patch_material(material_id: uuid.UUID, body: MaterialPatch, db: Session = Depends(get_db),
                   cu: CurrentUser = Depends(require("master:write"))):
    m = get_owned(db, Material, material_id, cu.organization_id, "원료")
    before = snapshot(m)
    data = body.model_dump(exclude_unset=True)
    reason = data.pop("reason", None)
    if "base_unit_id" in data and data["base_unit_id"] != m.base_unit_id:
        from app.models import InventoryBalance

        if db.execute(select(InventoryBalance.id).where(InventoryBalance.material_id == m.id).limit(1)).first():
            raise BusinessRule("UNIT_LOCKED", "재고 이력이 있는 원료의 기본 단위는 변경할 수 없습니다.")
    _apply(m, data)
    m.updated_by = cu.id
    db.flush()
    audit(db, cu, "MASTER_UPDATE", "MATERIAL", m.id, before=before, after=snapshot(m), reason=reason)
    db.commit()
    db.refresh(m)
    return ok(_material_out(db, m))


# ------------------------------------------------------------------ suppliers / customers

class PartnerBase(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    business_number: str | None = Field(default=None, max_length=20)
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    trade_status: Literal["ACTIVE", "SUSPENDED", "TERMINATED"] | None = None
    note: str | None = None
    is_active: bool | None = None


class SupplierIn(PartnerBase):
    code: str | None = Field(default=None, max_length=30)
    supplied_materials: list[uuid.UUID] | None = None


class CustomerIn(PartnerBase):
    code: str | None = Field(default=None, max_length=30)
    delivery_address: str | None = None


def _partner_routes(model: Any, path: str, label: str, prefix: str, schema: type[BaseModel], write_perm: str, audit_type: str):
    @router.get(f"/{path}", name=f"list_{path}")
    def _list(p: PageParams = Depends(), is_active: bool | None = None, db: Session = Depends(get_db),
              cu: CurrentUser = Depends(require("master:read"))):
        stmt = select(model).where(model.organization_id == cu.organization_id)
        if is_active is not None:
            stmt = stmt.where(model.is_active == is_active)
        rows, total = paginate(db, stmt, p, model, [model.code, model.name, model.business_number], model.code)
        return paged([ser(r) for r in rows], total, p.page, p.size)

    @router.post(f"/{path}", name=f"create_{path}")
    def _create(body: schema, db: Session = Depends(get_db), cu: CurrentUser = Depends(require(write_perm))):  # type: ignore[valid-type]
        data = body.model_dump(exclude_none=True)
        if not data.get("name"):
            raise BusinessRule("NAME_REQUIRED", f"{label}명은 필수입니다.")
        if "supplied_materials" in data:
            data["supplied_materials"] = [str(x) for x in data["supplied_materials"]]
        code = data.pop("code", None) or _next_code(db, model, model.code, cu.organization_id, prefix)
        _unique(db, model, model.code, code, cu.organization_id)
        obj = model(organization_id=cu.organization_id, code=code, created_by=cu.id, **data)
        db.add(obj)
        db.flush()
        audit(db, cu, "MASTER_CREATE", audit_type, obj.id, after=snapshot(obj))
        db.commit()
        return ok(ser(obj), f"{label}이(가) 등록되었습니다.")

    @router.get(f"/{path}/{{obj_id}}", name=f"get_{path}")
    def _get(obj_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
        return ok(ser(get_owned(db, model, obj_id, cu.organization_id, label)))

    @router.patch(f"/{path}/{{obj_id}}", name=f"patch_{path}")
    def _patch(obj_id: uuid.UUID, body: schema, db: Session = Depends(get_db), cu: CurrentUser = Depends(require(write_perm))):  # type: ignore[valid-type]
        obj = get_owned(db, model, obj_id, cu.organization_id, label)
        before = snapshot(obj)
        data = body.model_dump(exclude_unset=True)
        if "code" in data and data["code"] and data["code"] != obj.code:
            _unique(db, model, model.code, data["code"], cu.organization_id, obj.id)
        if "supplied_materials" in data and data["supplied_materials"] is not None:
            data["supplied_materials"] = [str(x) for x in data["supplied_materials"]]
        _apply(obj, {k: v for k, v in data.items() if not (k == "code" and not v)})
        obj.updated_by = cu.id
        db.flush()
        audit(db, cu, "MASTER_UPDATE", audit_type, obj.id, before=before, after=snapshot(obj))
        db.commit()
        return ok(ser(obj))


_partner_routes(Supplier, "suppliers", "공급업체", "SUP", SupplierIn, "master:write", "SUPPLIER")
_partner_routes(Customer, "customers", "거래처", "CUS", CustomerIn, "customer:write", "CUSTOMER")


# ------------------------------------------------------------------ products

class ProductBase(BaseModel):
    product_name: str | None = Field(default=None, max_length=200)
    food_type: str | None = None
    report_number: str | None = None
    specification: str | None = None
    content_quantity: Decimal | None = Field(default=None, gt=0)
    content_unit_id: uuid.UUID | None = None
    base_unit_id: uuid.UUID | None = None
    packaging_unit: str | None = None
    units_per_box: int | None = Field(default=None, ge=1)
    standard_batch_size: Decimal | None = Field(default=None, gt=0)
    standard_batch_unit_id: uuid.UUID | None = None
    shelf_life_days: int | None = Field(default=None, ge=1)
    shelf_life_basis: Literal["MANUFACTURED_DATE"] | None = None
    storage_condition: str | None = None
    standard_yield_pct: Decimal | None = Field(default=None, gt=0, le=200)
    default_warehouse_id: uuid.UUID | None = None
    image_file_id: uuid.UUID | None = None
    description: str | None = None
    is_active: bool | None = None


class ProductCreate(ProductBase):
    product_code: str | None = Field(default=None, max_length=30)
    product_name: str = Field(min_length=1, max_length=200)
    content_quantity: Decimal = Field(gt=0)
    content_unit_id: uuid.UUID
    base_unit_id: uuid.UUID


def _product_out(p: Product) -> dict:
    return ser(p, base_unit_code=p.base_unit.code if p.base_unit else None, content_unit_code=p.content_unit.code if p.content_unit else None)


@router.get("/products")
def list_products(p: PageParams = Depends(), is_active: bool | None = None, db: Session = Depends(get_db),
                  cu: CurrentUser = Depends(require("master:read"))):
    stmt = select(Product).where(Product.organization_id == cu.organization_id)
    if is_active is not None:
        stmt = stmt.where(Product.is_active == is_active)
    rows, total = paginate(db, stmt, p, Product, [Product.product_code, Product.product_name], Product.product_code)
    return paged([_product_out(r) for r in rows], total, p.page, p.size)


@router.post("/products")
def create_product(body: ProductCreate, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    data = body.model_dump(exclude_none=True)
    code = data.pop("product_code", None) or _next_code(db, Product, Product.product_code, cu.organization_id, "FP")
    _unique(db, Product, Product.product_code, code, cu.organization_id)
    obj = Product(organization_id=cu.organization_id, product_code=code, created_by=cu.id, **data)
    db.add(obj)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "PRODUCT", obj.id, after=snapshot(obj))
    db.commit()
    db.refresh(obj)
    return ok(_product_out(obj), "제품이 등록되었습니다.")


@router.get("/products/{product_id}")
def get_product(product_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    return ok(_product_out(get_owned(db, Product, product_id, cu.organization_id, "제품")))


@router.patch("/products/{product_id}")
def patch_product(product_id: uuid.UUID, body: ProductBase, db: Session = Depends(get_db),
                  cu: CurrentUser = Depends(require("master:write"))):
    obj = get_owned(db, Product, product_id, cu.organization_id, "제품")
    before = snapshot(obj)
    _apply(obj, body.model_dump(exclude_unset=True))
    obj.updated_by = cu.id
    db.flush()
    audit(db, cu, "MASTER_UPDATE", "PRODUCT", obj.id, before=before, after=snapshot(obj))
    db.commit()
    db.refresh(obj)
    return ok(_product_out(obj))


# ------------------------------------------------------------------ warehouses / locations / equipment

WarehouseType = Literal["AMBIENT", "REFRIGERATED", "FROZEN", "PACKAGING", "CONSUMABLE", "WIP", "FINISHED", "QUARANTINE"]


class WarehouseIn(BaseModel):
    code: str | None = Field(default=None, max_length=30)
    name: str | None = Field(default=None, max_length=100)
    warehouse_type: WarehouseType | None = None
    facility_id: uuid.UUID | None = None
    temperature_min: Decimal | None = None
    temperature_max: Decimal | None = None
    is_active: bool | None = None


@router.get("/warehouses")
def list_warehouses(p: PageParams = Depends(), warehouse_type: WarehouseType | None = None, all_facilities: bool = False,
                    db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    stmt = select(Warehouse).where(Warehouse.organization_id == cu.organization_id)
    if not all_facilities and cu.facility_id:
        stmt = stmt.where(Warehouse.facility_id == cu.facility_id)
    if warehouse_type:
        stmt = stmt.where(Warehouse.warehouse_type == warehouse_type)
    rows, total = paginate(db, stmt, p, Warehouse, [Warehouse.code, Warehouse.name], Warehouse.code)
    return paged([ser(r) for r in rows], total, p.page, p.size)


@router.post("/warehouses")
def create_warehouse(body: WarehouseIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    if not body.code or not body.name or not body.warehouse_type:
        raise BusinessRule("REQUIRED", "창고 코드, 창고명, 창고 유형은 필수입니다.")
    _unique(db, Warehouse, Warehouse.code, body.code, cu.organization_id)
    fac = body.facility_id or cu.facility_id
    cu.require_facility(fac)
    obj = Warehouse(organization_id=cu.organization_id, created_by=cu.id, **{**body.model_dump(exclude_none=True), "facility_id": fac})
    db.add(obj)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "WAREHOUSE", obj.id, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


@router.patch("/warehouses/{wh_id}")
def patch_warehouse(wh_id: uuid.UUID, body: WarehouseIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    obj = get_owned(db, Warehouse, wh_id, cu.organization_id, "창고", cu.require_facility)
    before = snapshot(obj)
    data = body.model_dump(exclude_unset=True)
    data.pop("code", None)
    data.pop("facility_id", None)
    _apply(obj, data)
    db.flush()
    audit(db, cu, "MASTER_UPDATE", "WAREHOUSE", obj.id, before=before, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


class LocationIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    zone: str | None = None
    rack: str | None = None
    name: str | None = None
    is_active: bool = True


@router.get("/warehouses/{wh_id}/locations")
def list_locations(wh_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    get_owned(db, Warehouse, wh_id, cu.organization_id, "창고")
    rows = db.execute(select(StorageLocation).where(StorageLocation.warehouse_id == wh_id).order_by(StorageLocation.code)).scalars()
    return ok([ser(r) for r in rows])


@router.post("/warehouses/{wh_id}/locations")
def create_location(wh_id: uuid.UUID, body: LocationIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    get_owned(db, Warehouse, wh_id, cu.organization_id, "창고", cu.require_facility)
    if db.execute(select(StorageLocation.id).where(StorageLocation.warehouse_id == wh_id, StorageLocation.code == body.code)).first():
        raise Conflict("DUPLICATE_CODE", "이미 존재하는 위치 코드입니다.")
    obj = StorageLocation(organization_id=cu.organization_id, warehouse_id=wh_id, created_by=cu.id, **body.model_dump())
    db.add(obj)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "STORAGE_LOCATION", obj.id, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


class EquipmentIn(BaseModel):
    code: str | None = Field(default=None, max_length=30)
    name: str | None = None
    equipment_type: str | None = None
    line_code: str | None = None
    interface_type: Literal["USB", "BLUETOOTH", "RS232", "TCP"] | None = None
    interface_config: dict[str, Any] | None = None
    is_active: bool | None = None


@router.get("/equipments")
def list_equipments(p: PageParams = Depends(), equipment_type: str | None = None, db: Session = Depends(get_db),
                    cu: CurrentUser = Depends(require("master:read"))):
    stmt = select(Equipment).where(Equipment.organization_id == cu.organization_id)
    if equipment_type:
        stmt = stmt.where(Equipment.equipment_type == equipment_type)
    rows, total = paginate(db, stmt, p, Equipment, [Equipment.code, Equipment.name], Equipment.code)
    return paged([ser(r) for r in rows], total, p.page, p.size)


@router.post("/equipments")
def create_equipment(body: EquipmentIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    if not body.code or not body.name or not body.equipment_type:
        raise BusinessRule("REQUIRED", "설비 코드, 설비명, 유형은 필수입니다.")
    _unique(db, Equipment, Equipment.code, body.code, cu.organization_id)
    obj = Equipment(organization_id=cu.organization_id, facility_id=cu.facility_id, created_by=cu.id, **body.model_dump(exclude_none=True))
    db.add(obj)
    db.flush()
    audit(db, cu, "MASTER_CREATE", "EQUIPMENT", obj.id, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


@router.patch("/equipments/{eq_id}")
def patch_equipment(eq_id: uuid.UUID, body: EquipmentIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:write"))):
    obj = get_owned(db, Equipment, eq_id, cu.organization_id, "설비")
    before = snapshot(obj)
    data = body.model_dump(exclude_unset=True)
    data.pop("code", None)
    _apply(obj, data)
    db.flush()
    audit(db, cu, "MASTER_UPDATE", "EQUIPMENT", obj.id, before=before, after=snapshot(obj))
    db.commit()
    return ok(ser(obj))


@router.get("/inspection-templates")
def list_templates(target_type: str | None = None, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("master:read"))):
    stmt = select(InspectionTemplate).where(InspectionTemplate.organization_id == cu.organization_id)
    if target_type:
        stmt = stmt.where(InspectionTemplate.target_type == target_type)
    out = []
    for t in db.execute(stmt).scalars():
        out.append(ser(t, items=[ser(i) for i in t.items]))
    return ok(out)
