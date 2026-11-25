"""단위 환산: 동일 차원은 factor_to_base, 중량↔부피는 검증된 품목별 밀도 또는 품목별 환산표 필요."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BusinessRule
from app.models import Material, Unit, UnitConversion


def convert(db: Session, qty: Decimal, from_unit_id: uuid.UUID, to_unit_id: uuid.UUID, *, material: Material | None = None,
            product_id: uuid.UUID | None = None) -> Decimal:
    if from_unit_id == to_unit_id:
        return qty
    fu, tu = db.get(Unit, from_unit_id), db.get(Unit, to_unit_id)
    if not fu or not tu:
        raise BusinessRule("UNIT_NOT_FOUND", "단위를 찾을 수 없습니다.")
    # 1) 품목별 명시 환산표 우선
    stmt = select(UnitConversion).where(UnitConversion.is_active.is_(True))
    if material is not None:
        stmt = stmt.where(UnitConversion.material_id == material.id)
    elif product_id is not None:
        stmt = stmt.where(UnitConversion.product_id == product_id)
    else:
        stmt = None  # type: ignore[assignment]
    if stmt is not None:
        for c in db.execute(stmt).scalars():
            if c.from_unit_id == from_unit_id and c.to_unit_id == to_unit_id:
                return qty * c.factor
            if c.from_unit_id == to_unit_id and c.to_unit_id == from_unit_id:
                return qty / c.factor
    # 2) 동일 차원
    if fu.dimension == tu.dimension and fu.dimension != "COUNT":
        return qty * fu.factor_to_base / tu.factor_to_base
    if fu.dimension == tu.dimension == "COUNT" and fu.code == tu.code:
        return qty
    # 3) 중량↔부피 (검증된 밀도 kg/L)
    dims = {fu.dimension, tu.dimension}
    if dims == {"MASS", "VOLUME"} and material is not None and material.density_kg_per_l:
        if fu.dimension == "VOLUME":
            liters = qty * fu.factor_to_base
            kg = liters * material.density_kg_per_l
            return kg / tu.factor_to_base
        kg = qty * fu.factor_to_base
        liters = kg / material.density_kg_per_l
        return liters / tu.factor_to_base
    raise BusinessRule(
        "UNIT_CONVERSION_REQUIRED",
        f"'{fu.code}' → '{tu.code}' 단위 환산 기준이 없습니다. 품목별 환산 또는 검증된 밀도를 등록하세요.",
        {"from": fu.code, "to": tu.code},
    )


def unit_code(db: Session, unit_id: uuid.UUID | None) -> str | None:
    if not unit_id:
        return None
    u = db.get(Unit, unit_id)
    return u.code if u else None
