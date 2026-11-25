"""BOM: 버전/승인/복사/합계 검증/필요량 계산."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import BusinessRule, InvalidState, NotFound
from app.core.numbering import today_seoul
from app.models import BomHeader, BomItem, BomVersion, Material, Product, Unit
from app.repositories.common import ser, ser_value
from app.services.inventory import service as inv
from app.services.inventory.units import convert

ONE_HUNDRED = Decimal("100")
EPS = Decimal("0.0001")


def get_header(db: Session, org_id: uuid.UUID, bom_id: uuid.UUID) -> BomHeader:
    h = db.get(BomHeader, bom_id)
    if not h or h.organization_id != org_id:
        raise NotFound("BOM")
    return h


def get_version(db: Session, org_id: uuid.UUID, version_id: uuid.UUID) -> BomVersion:
    v = db.get(BomVersion, version_id)
    if not v or v.organization_id != org_id:
        raise NotFound("BOM 버전")
    return v


def _validate_items(db: Session, org_id: uuid.UUID, items: list[dict[str, Any]]) -> None:
    if not items:
        raise BusinessRule("BOM_EMPTY", "BOM 품목이 없습니다.")
    for it in items:
        m = db.get(Material, it["material_id"])
        if not m or m.organization_id != org_id:
            raise NotFound("원료")
        if it["item_type"] == "FORMULA":
            if m.major_category != "MAIN_MATERIAL" or not m.production_usable:
                raise BusinessRule("INVALID_FORMULA_MATERIAL", f"배합(FORMULA) 항목에는 배합 사용 가능한 주원료만 등록할 수 있습니다: {m.material_name}")
        if it["item_type"] == "PACKAGING" and m.major_category != "SUB_MATERIAL":
            raise BusinessRule("INVALID_PACKAGING_MATERIAL", f"포장재 항목에는 부원료만 등록할 수 있습니다: {m.material_name}")
        basis = it.get("quantity_basis", "RATIO")
        if basis == "RATIO" and (it.get("ratio_percent") is None or Decimal(str(it["ratio_percent"])) <= 0):
            raise BusinessRule("RATIO_REQUIRED", f"비율 기준 품목은 배합 비율이 필요합니다: {m.material_name}")
        if basis in ("FIXED", "PER_UNIT") and (it.get("standard_quantity") is None or Decimal(str(it["standard_quantity"])) <= 0):
            raise BusinessRule("STANDARD_QTY_REQUIRED", f"고정량/단위당 품목은 표준 사용량이 필요합니다: {m.material_name}")
        if basis == "RATIO" and it["item_type"] != "FORMULA":
            raise BusinessRule("RATIO_ONLY_FORMULA", "비율 기준은 배합(FORMULA) 품목에만 사용할 수 있습니다. 포장재는 고정량/단위당 기준을 사용하세요.")


def ratio_total(items: list[Any]) -> Decimal:
    tot = Decimal(0)
    for it in items:
        t = it["item_type"] if isinstance(it, dict) else it.item_type
        b = (it.get("quantity_basis") if isinstance(it, dict) else it.quantity_basis) or "RATIO"
        r = it.get("ratio_percent") if isinstance(it, dict) else it.ratio_percent
        if t == "FORMULA" and b == "RATIO" and r is not None:
            tot += Decimal(str(r))
    return tot


def _add_version(db: Session, cu: Any, header: BomHeader, vdata: dict[str, Any], copied_from: uuid.UUID | None = None) -> BomVersion:
    items = vdata.pop("items", [])
    _validate_items(db, cu.organization_id, items)
    last = max((v.version_no for v in header.versions), default=0)
    v = BomVersion(organization_id=cu.organization_id, bom_header_id=header.id, version_no=last + 1, status="DRAFT",
                   copied_from_id=copied_from, created_by=cu.id, **vdata)
    db.add(v)
    db.flush()
    for i, it in enumerate(items):
        it = dict(it)
        it.setdefault("sequence", (i + 1) * 10)
        if it.get("substitute_material_ids"):
            it["substitute_material_ids"] = [str(x) for x in it["substitute_material_ids"]]
        db.add(BomItem(organization_id=cu.organization_id, bom_version_id=v.id, **it))
    db.flush()
    db.refresh(v)
    db.refresh(header)
    return v


def create(db: Session, cu: Any, data: dict[str, Any]) -> BomHeader:
    p = db.get(Product, data["product_id"])
    if not p or p.organization_id != cu.organization_id:
        raise NotFound("제품")
    existing = db.execute(select(BomHeader).where(BomHeader.organization_id == cu.organization_id, BomHeader.product_id == p.id,
                                                  BomHeader.is_active)).scalars().first()
    if existing:
        raise BusinessRule("BOM_EXISTS", "이미 이 제품의 BOM 이 있습니다. 새 버전을 추가하세요.", {"bom_id": str(existing.id)})
    h = BomHeader(organization_id=cu.organization_id, bom_code=f"BOM-{p.product_code}", product_id=p.id,
                  name=data.get("name") or f"{p.product_name} 배합비", created_by=cu.id)
    db.add(h)
    db.flush()
    _add_version(db, cu, h, dict(data["version"]))
    audit(db, cu, "BOM_CREATE", "BOM", h.id)
    return h


def add_version(db: Session, cu: Any, h: BomHeader, vdata: dict[str, Any] | None, copy_from_version_id: uuid.UUID | None) -> BomVersion:
    if copy_from_version_id:
        src = get_version(db, cu.organization_id, copy_from_version_id)
        if src.bom_header_id != h.id:
            raise BusinessRule("VERSION_MISMATCH", "다른 BOM 의 버전은 복사할 수 없습니다.")
        base = {"base_quantity": src.base_quantity, "base_unit_id": src.base_unit_id, "base_fg_quantity": src.base_fg_quantity,
                "items": [{k: getattr(it, k) for k in ("item_type", "material_id", "quantity_basis", "ratio_percent", "standard_quantity",
                                                       "unit_id", "tolerance_pct", "sequence", "process_step_code", "weighing_group",
                                                       "is_required", "substitute_allowed", "substitute_material_ids", "note")}
                          for it in src.items]}
        if vdata:
            base.update({k: v for k, v in vdata.items() if v is not None})
        v = _add_version(db, cu, h, base, copied_from=src.id)
    else:
        if not vdata:
            raise BusinessRule("VERSION_DATA_REQUIRED", "버전 내용이 필요합니다.")
        v = _add_version(db, cu, h, dict(vdata))
    audit(db, cu, "BOM_VERSION_CREATE", "BOM_VERSION", v.id, after={"version_no": v.version_no})
    return v


def update_version(db: Session, cu: Any, v: BomVersion, vdata: dict[str, Any]) -> BomVersion:
    if v.status != "DRAFT":
        raise InvalidState(v.status, "BOM 수정 (승인된 버전은 수정 불가 — 복사하여 새 버전 작성)")
    items = vdata.pop("items", None)
    for k, val in vdata.items():
        setattr(v, k, val)
    if items is not None:
        _validate_items(db, cu.organization_id, items)
        for it in list(v.items):
            db.delete(it)
        db.flush()
        for i, it in enumerate(items):
            it = dict(it)
            it.setdefault("sequence", (i + 1) * 10)
            if it.get("substitute_material_ids"):
                it["substitute_material_ids"] = [str(x) for x in it["substitute_material_ids"]]
            db.add(BomItem(organization_id=cu.organization_id, bom_version_id=v.id, **it))
    db.flush()
    db.refresh(v)
    audit(db, cu, "BOM_VERSION_UPDATE", "BOM_VERSION", v.id)
    return v


def approve(db: Session, cu: Any, h: BomHeader, version_id: uuid.UUID) -> BomVersion:
    v = get_version(db, cu.organization_id, version_id)
    if v.bom_header_id != h.id:
        raise BusinessRule("VERSION_MISMATCH", "BOM 과 버전이 일치하지 않습니다.")
    if v.status not in ("DRAFT", "PENDING_APPROVAL"):
        raise InvalidState(v.status, "BOM 승인")
    tot = ratio_total(v.items)
    has_ratio = any(it.item_type == "FORMULA" and it.quantity_basis == "RATIO" for it in v.items)
    if has_ratio and abs(tot - ONE_HUNDRED) > EPS:
        raise BusinessRule("BOM_RATIO_INVALID", f"배합 비율 합계가 100% 가 아닙니다. (현재 {ser_value(tot)}%)", {"total": ser_value(tot)})
    if not any(it.item_type == "FORMULA" for it in v.items):
        raise BusinessRule("BOM_NO_FORMULA", "배합(FORMULA) 품목이 1개 이상 필요합니다.")
    now = datetime.now(timezone.utc)
    for other in h.versions:
        if other.id != v.id and other.status == "APPROVED":
            other.status = "OBSOLETE"
            other.effective_to = today_seoul()
    v.status = "APPROVED"
    v.approved_by = cu.id
    v.approved_at = now
    v.effective_from = v.effective_from or today_seoul()
    audit(db, cu, "BOM_APPROVE", "BOM_VERSION", v.id, after={"version_no": v.version_no})
    return v


def approved_version(db: Session, org_id: uuid.UUID, product_id: uuid.UUID, on: date | None = None) -> BomVersion:
    on = on or today_seoul()
    h = db.execute(select(BomHeader).where(BomHeader.organization_id == org_id, BomHeader.product_id == product_id, BomHeader.is_active)).scalars().first()
    if h:
        for v in sorted(h.versions, key=lambda x: -x.version_no):
            if v.status == "APPROVED" and (v.effective_from is None or v.effective_from <= on) and (v.effective_to is None or v.effective_to >= on):
                return v
    raise BusinessRule("BOM_NOT_APPROVED", "승인된 BOM 이 없습니다. BOM 승인 후 제조지시를 생성하세요.")


def derive_quantities(product: Product, v: BomVersion | dict, planned: Decimal | None, target_fg: Decimal | None) -> tuple[Decimal, Decimal | None]:
    """배합량과 목표 완제품량 산정 (표준 수율 반영). 두 값은 별도 관리."""
    yield_pct = product.standard_yield_pct or ONE_HUNDRED
    content = product.content_quantity
    base_q = Decimal(str(v["base_quantity"] if isinstance(v, dict) else v.base_quantity))
    base_fg = (v.get("base_fg_quantity") if isinstance(v, dict) else v.base_fg_quantity)
    if planned is None and target_fg is None:
        raise BusinessRule("QUANTITY_REQUIRED", "계획 배합량 또는 목표 완제품량을 입력하세요.")
    if planned is None:
        planned = (target_fg * content / (yield_pct / ONE_HUNDRED)).quantize(Decimal("0.000001"))
    if target_fg is None:
        if base_fg:
            target_fg = (planned / base_q * Decimal(str(base_fg))).quantize(Decimal("1"))
        else:
            target_fg = (planned * (yield_pct / ONE_HUNDRED) / content).quantize(Decimal("1"))
    return planned, target_fg


def calculate(db: Session, org_id: uuid.UUID, facility_id: uuid.UUID | None, items: list[dict[str, Any]], base_quantity: Decimal,
              base_unit_id: uuid.UUID, planned: Decimal, target_fg: Decimal | None, with_stock: bool = True) -> list[dict[str, Any]]:
    out = []
    planned_in_base = planned  # 계획 배합량은 BOM 기준 단위
    scale = planned_in_base / base_quantity
    for it in items:
        m = db.get(Material, uuid.UUID(str(it["material_id"])))
        unit_id = uuid.UUID(str(it["unit_id"]))
        basis = it.get("quantity_basis") or "RATIO"
        if basis == "RATIO":
            req_bom_unit = planned_in_base * Decimal(str(it["ratio_percent"])) / ONE_HUNDRED
            req = convert(db, req_bom_unit, base_unit_id, m.base_unit_id, material=m)
        elif basis == "FIXED":
            req = convert(db, Decimal(str(it["standard_quantity"])) * scale, unit_id, m.base_unit_id, material=m)
        else:
            if target_fg is None:
                raise BusinessRule("TARGET_FG_REQUIRED", "단위당 소요량 품목이 있어 목표 완제품량이 필요합니다.")
            req = convert(db, Decimal(str(it["standard_quantity"])) * target_fg, unit_id, m.base_unit_id, material=m)
        req = req.quantize(Decimal("0.000001"))
        row = {"bom_item_id": str(it.get("id")) if it.get("id") else None, "item_type": it["item_type"], "material_id": str(m.id),
               "material_code": m.material_code, "material_name": m.material_name, "quantity_basis": basis,
               "ratio_percent": ser_value(it.get("ratio_percent")), "required_quantity": ser_value(req),
               "unit": db.get(Unit, m.base_unit_id).code, "unit_id": str(m.base_unit_id),
               "tolerance_pct": ser_value(it.get("tolerance_pct")), "weighing_group": it.get("weighing_group"),
               "sequence": it.get("sequence"), "_req": req}
        if with_stock:
            avail = inv.available_quantity(db, org_id, facility_id, material_id=m.id)
            row["available_quantity"] = ser_value(avail)
            row["shortage"] = ser_value(max(req - avail, Decimal(0)))
            row["is_short"] = avail < req
        out.append(row)
    return out


def snapshot(db: Session, v: BomVersion) -> dict[str, Any]:
    return {
        "bom_version_id": str(v.id), "bom_header_id": str(v.bom_header_id), "version_no": v.version_no,
        "base_quantity": ser_value(v.base_quantity), "base_unit_id": str(v.base_unit_id),
        "base_fg_quantity": ser_value(v.base_fg_quantity), "approved_by": str(v.approved_by) if v.approved_by else None,
        "approved_at": ser_value(v.approved_at),
        "items": [ser(it) for it in v.items],
    }


def detail(db: Session, h: BomHeader) -> dict[str, Any]:
    p = db.get(Product, h.product_id)
    vers = []
    for v in h.versions:
        items = []
        for it in v.items:
            m = db.get(Material, it.material_id)
            items.append(ser(it, material_code=m.material_code, material_name=m.material_name, unit=db.get(Unit, it.unit_id).code))
        vers.append(ser(v, items=items, ratio_total=ser_value(ratio_total(v.items)), base_unit=db.get(Unit, v.base_unit_id).code))
    return ser(h, product_code=p.product_code, product_name=p.product_name, versions=vers)
