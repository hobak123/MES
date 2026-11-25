"""시드 데이터: python -m app.seed [--demo]

- 기본: 조직/공장/역할/권한/단위/분류/창고/공정/검수양식/프린터/설정/사용자
- --demo: 샘플 공급업체·거래처·원료·제품 (재고는 생성하지 않음 — 재고는 실제 입고 거래로만 생성)
운영 환경에서는 SEED_DEFAULT_PASSWORD 환경변수가 필수이다.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.permissions import PERMISSIONS, ROLE_NAMES, ROLE_PERMISSIONS
from app.core.security import hash_password
from app.models import (
    Customer, Equipment, Facility, InspectionTemplate, InspectionTemplateItem, Material, MaterialCategory, Organization,
    Permission, PrinterSetting, ProcessDefinition, Product, Role, RolePermission, StorageLocation, Supplier,
    SystemSetting, Unit, User, UserFacility, UserRole, Warehouse,
)

UNITS = [
    ("kg", "킬로그램", "MASS", "1"), ("g", "그램", "MASS", "0.001"), ("t", "톤", "MASS", "1000"),
    ("L", "리터", "VOLUME", "1"), ("mL", "밀리리터", "VOLUME", "0.001"),
    ("EA", "개", "COUNT", "1"), ("BOX", "박스", "COUNT", "1"), ("ROLL", "롤", "COUNT", "1"),
]

CATEGORIES = [
    ("MAIN_MATERIAL", "POWDER", "분말"), ("MAIN_MATERIAL", "LIQUID", "액상"), ("MAIN_MATERIAL", "AGRI", "농산물"),
    ("MAIN_MATERIAL", "MARINE", "수산물"), ("MAIN_MATERIAL", "LIVESTOCK", "축산물"), ("MAIN_MATERIAL", "ETC", "기타 식품 배합 원료"),
    ("SUB_MATERIAL", "PACKAGING", "포장재"), ("SUB_MATERIAL", "CONTAINER", "용기"), ("SUB_MATERIAL", "FILM", "필름"),
    ("SUB_MATERIAL", "LID", "뚜껑"), ("SUB_MATERIAL", "LABEL", "라벨"), ("SUB_MATERIAL", "BOX", "박스"),
    ("OTHER_MATERIAL", "CLEANING", "청소도구"), ("OTHER_MATERIAL", "FILTER", "필터"), ("OTHER_MATERIAL", "MAINTENANCE", "유지보수 부품"),
    ("OTHER_MATERIAL", "HYGIENE", "위생용품"), ("OTHER_MATERIAL", "CONSUMABLE", "공장 소모품"),
]

WAREHOUSES = [
    ("WH-AMB", "상온 원료창고", "AMBIENT", None, None), ("WH-REF", "냉장 창고", "REFRIGERATED", "0", "10"),
    ("WH-FRZ", "냉동 창고", "FROZEN", "-25", "-18"), ("WH-PKG", "부자재 창고", "PACKAGING", None, None),
    ("WH-CON", "소모품 창고", "CONSUMABLE", None, None), ("WH-WIP", "반제품 보관", "WIP", None, None),
    ("WH-FG", "완제품 창고", "FINISHED", None, None), ("WH-QUA", "격리 창고", "QUARANTINE", None, None),
]

PROCESSES = [
    ("WEIGH", "계량", "WEIGHING", 10, "NONE", False, []),
    ("CHARGE", "투입", "CHARGING", 20, "WIP", False, []),
    ("MIX", "혼합", "MIXING", 30, "WIP", False, [
        {"code": "MIX_TIME", "name": "혼합시간", "unit": "min", "min": 10, "max": 30, "required": True},
        {"code": "SPEED", "name": "교반속도", "unit": "rpm", "min": 50, "max": 200, "required": True},
    ]),
    ("HEAT", "가열", "HEATING", 40, "WIP", True, [
        {"code": "TEMPERATURE", "name": "가열온도", "unit": "℃", "min": 85, "max": 95, "required": True},
        {"code": "HOLD_TIME", "name": "유지시간", "unit": "min", "min": 15, "max": 60, "required": True},
    ]),
    ("STERILIZE", "살균", "STERILIZING", 50, "WIP", True, [
        {"code": "TEMPERATURE", "name": "살균온도", "unit": "℃", "min": 85, "max": 121, "required": True},
    ]),
    ("COOL", "냉각", "COOLING", 60, "WIP", False, [{"code": "TEMPERATURE", "name": "냉각온도", "unit": "℃", "min": 0, "max": 10, "required": True}]),
    ("FILTER", "여과", "FILTERING", 70, "WIP", False, []),
    ("AGE", "숙성", "AGING", 80, "WIP", False, []),
    ("DRY", "건조", "DRYING", 90, "WIP", False, []),
    ("FILL", "충진", "FILLING", 100, "WIP", False, []),
    ("METAL", "금속검출", "METAL_DETECTION", 110, "WIP", True, [
        {"code": "FE_TEST", "name": "Fe 테스트피스", "unit": "", "required": True, "type": "PASS_FAIL"},
        {"code": "SUS_TEST", "name": "SUS 테스트피스", "unit": "", "required": True, "type": "PASS_FAIL"},
    ]),
    ("PACK", "충진/포장", "PACKAGING", 120, "FG", False, []),
    ("FG_QC", "완제품검사/입고", "FG_INSPECTION", 130, "NONE", False, []),
]

RECEIPT_CHECKS = [
    ("APPEARANCE", "외관"), ("PACKAGE_DAMAGE", "포장 파손"), ("CONTAMINATION", "오염"), ("FOREIGN_MATTER", "이물"),
    ("ODOR", "냄새"), ("TEMPERATURE", "온도"), ("TRANSPORT", "운송 상태"), ("EXPIRY", "소비기한"), ("LABELING", "표시사항"),
    ("COA", "COA"), ("ORIGIN", "원산지"), ("LOT", "LOT"), ("QUANTITY", "수량"),
]

USERS = [
    ("admin", "시스템관리자", ["SUPER_ADMIN"]),
    ("manager", "기준정보관리자", ["ADMIN"]),
    ("qm", "품질관리자", ["QUALITY_MANAGER"]),
    ("wh", "창고관리자", ["WAREHOUSE_MANAGER"]),
    ("pm", "생산관리자", ["PRODUCTION_MANAGER"]),
    ("worker", "현장작업자", ["WORKER"]),
    ("ship", "출고관리자", ["SHIPPING_MANAGER"]),
    ("viewer", "조회자", ["VIEWER"]),
]


def _default_password() -> str:
    pw = os.environ.get("SEED_DEFAULT_PASSWORD")
    if not pw:
        if settings.is_production:
            raise RuntimeError("운영 환경 시드에는 SEED_DEFAULT_PASSWORD 환경변수가 필요합니다.")
        pw = "Passw0rd!"  # 개발/테스트 전용 기본값 (README 참조, 최초 로그인 후 변경)
    return pw


def get_or_create(db: Session, model, defaults: dict | None = None, **filters):
    obj = db.execute(select(model).filter_by(**filters)).scalar_one_or_none()
    if obj:
        return obj, False
    obj = model(**filters, **(defaults or {}))
    db.add(obj)
    db.flush()
    return obj, True


def seed_base(db: Session) -> Organization:
    org, _ = get_or_create(db, Organization, {"name": settings.COMPANY_NAME}, code="DEFAULT")
    f1, _ = get_or_create(db, Facility, {"name": "제1공장"}, organization_id=org.id, code="F1")
    f2, _ = get_or_create(db, Facility, {"name": "제2공장"}, organization_id=org.id, code="F2")

    perm_ids = {}
    for code, (module, name) in PERMISSIONS.items():
        p, _ = get_or_create(db, Permission, {"module": module, "name": name}, code=code)
        perm_ids[code] = p.id
    for rcode, perms in ROLE_PERMISSIONS.items():
        role, _ = get_or_create(db, Role, {"name": ROLE_NAMES[rcode]}, code=rcode)
        existing = set(db.execute(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)).scalars())
        for pc in perms:
            if perm_ids[pc] not in existing:
                db.add(RolePermission(role_id=role.id, permission_id=perm_ids[pc]))
    db.flush()

    for code, name, dim, factor in UNITS:
        get_or_create(db, Unit, {"name": name, "dimension": dim, "factor_to_base": Decimal(factor)}, code=code)
    for i, (major, code, name) in enumerate(CATEGORIES):
        get_or_create(db, MaterialCategory, {"name": name, "sort_order": i}, organization_id=org.id, major=major, code=code)

    for fac in (f1, f2):
        for code, name, wtype, tmin, tmax in WAREHOUSES:
            wcode = code if fac.code == "F1" else f"{fac.code}-{code}"
            wh, created = get_or_create(
                db, Warehouse,
                {"name": f"{name}" if fac.code == "F1" else f"{fac.name} {name}", "warehouse_type": wtype, "facility_id": fac.id,
                 "temperature_min": Decimal(tmin) if tmin else None, "temperature_max": Decimal(tmax) if tmax else None},
                organization_id=org.id, code=wcode,
            )
            if created:
                db.add(StorageLocation(organization_id=org.id, warehouse_id=wh.id, code="A-01-01", zone="A", rack="01", name="A구역 01랙"))

    for code, name, ptype, seq, out, ccp, specs in PROCESSES:
        get_or_create(
            db, ProcessDefinition,
            {"name": name, "process_type": ptype, "default_sequence": seq, "output_type": out, "is_ccp": ccp,
             "parameter_specs": specs, "qr_required": True},
            organization_id=org.id, code=code,
        )

    tpl, created = get_or_create(db, InspectionTemplate, {"name": "원료 입고 검수 기본양식", "target_type": "RECEIPT"}, organization_id=org.id, code="RCV-STD")
    if created:
        for i, (c, n) in enumerate(RECEIPT_CHECKS):
            db.add(InspectionTemplateItem(template_id=tpl.id, item_code=c, item_name=n, sort_order=i,
                                          value_type="NUMBER" if c == "TEMPERATURE" else "RESULT", is_required=c not in ("COA",)))
    tpl2, created = get_or_create(db, InspectionTemplate, {"name": "완제품 검사 기본양식", "target_type": "FINISHED_GOODS"}, organization_id=org.id, code="FG-STD")
    if created:
        for i, (c, n) in enumerate([("APPEARANCE", "외관"), ("WEIGHT", "중량"), ("SEAL", "실링"), ("LABELING", "표시사항"),
                                    ("METAL", "금속검출"), ("MICRO", "미생물")]):
            db.add(InspectionTemplateItem(template_id=tpl2.id, item_code=c, item_name=n, sort_order=i))

    get_or_create(db, PrinterSetting, {"printer_type": "PDF", "label_width_mm": 80, "label_height_mm": 50, "is_default": True,
                                       "facility_id": f1.id}, organization_id=org.id, name="기본 PDF 라벨")
    for key, value, desc in [
        ("ocr.review_threshold", {"value": str(settings.OCR_REVIEW_THRESHOLD)}, "OCR 검토 신뢰도 기준"),
        ("alerts.expiry_days", {"value": settings.expiry_alert_days}, "소비기한 알림 일수"),
        ("alerts.long_unused_days", {"value": settings.LONG_UNUSED_DAYS}, "장기 미사용 기준일"),
        ("production.mass_balance_tolerance_pct", {"value": str(settings.MASS_BALANCE_TOLERANCE_PCT)}, "물질수지 허용차(%)"),
        ("company.name", {"value": settings.COMPANY_NAME}, "회사명"),
    ]:
        get_or_create(db, SystemSetting, {"value": value, "description": desc}, organization_id=org.id, key=key)

    pw = _default_password()
    roles = {r.code: r for r in db.execute(select(Role)).scalars()}
    for username, full_name, rcodes in USERS:
        u, created = get_or_create(
            db, User, {"full_name": full_name, "password_hash": hash_password(pw), "default_facility_id": f1.id},
            organization_id=org.id, username=username,
        )
        if created:
            for rc in rcodes:
                db.add(UserRole(user_id=u.id, role_id=roles[rc].id))
            db.add(UserFacility(user_id=u.id, facility_id=f1.id))
            if username == "admin":
                db.add(UserFacility(user_id=u.id, facility_id=f2.id))
    # 타 공장 작업자 (공장 격리 테스트용)
    u2, created = get_or_create(db, User, {"full_name": "제2공장 작업자", "password_hash": hash_password(pw), "default_facility_id": f2.id},
                                organization_id=org.id, username="worker2")
    if created:
        db.add(UserRole(user_id=u2.id, role_id=roles["WORKER"].id))
        db.add(UserFacility(user_id=u2.id, facility_id=f2.id))
    db.flush()
    return org


def seed_demo(db: Session, org: Organization) -> None:
    units = {u.code: u for u in db.execute(select(Unit)).scalars()}
    cats = {(c.major, c.code): c for c in db.execute(select(MaterialCategory).where(MaterialCategory.organization_id == org.id)).scalars()}
    whs = {w.code: w for w in db.execute(select(Warehouse).where(Warehouse.organization_id == org.id)).scalars()}
    f1 = db.execute(select(Facility).where(Facility.organization_id == org.id, Facility.code == "F1")).scalar_one()

    sup, _ = get_or_create(db, Supplier, {"name": "ABC 원료", "business_number": "123-45-67890", "contact_name": "김공급",
                                          "phone": "02-000-0000"}, organization_id=org.id, code="SUP-001")
    sup2, _ = get_or_create(db, Supplier, {"name": "한국포장", "business_number": "222-33-44444"}, organization_id=org.id, code="SUP-002")
    get_or_create(db, Customer, {"name": "서울유통", "contact_name": "이고객", "phone": "02-111-1111",
                                 "delivery_address": "서울시 중구 물류센터"}, organization_id=org.id, code="CUS-001")
    get_or_create(db, Customer, {"name": "부산마트", "delivery_address": "부산시 해운대구"}, organization_id=org.id, code="CUS-002")

    def mat(code, name, major, cat, unit, wh, **kw):
        m, _ = get_or_create(
            db, Material,
            {"material_name": name, "major_category": major, "category_id": cats[(major, cat)].id, "base_unit_id": units[unit].id,
             "purchase_unit_id": units[unit].id, "stock_unit_id": units[unit].id, "default_supplier_id": sup.id,
             "default_warehouse_id": whs[wh].id, **kw},
            organization_id=org.id, material_code=code,
        )
        return m

    mat("RM-000001", "혼합분말 A", "MAIN_MATERIAL", "POWDER", "kg", "WH-AMB", food_type="혼합분말", coa_required=True,
        default_country_of_origin="대한민국", allergen_information="대두", storage_condition="AMBIENT", safety_stock=Decimal("50"))
    mat("RM-000002", "정제당 B", "MAIN_MATERIAL", "POWDER", "kg", "WH-AMB", food_type="설탕", storage_condition="AMBIENT",
        safety_stock=Decimal("30"))
    mat("RM-000003", "식물성유 C", "MAIN_MATERIAL", "LIQUID", "kg", "WH-AMB", food_type="식용유지", storage_condition="AMBIENT")
    mat("RM-000004", "향료 D", "MAIN_MATERIAL", "ETC", "kg", "WH-REF", food_type="착향료", is_food_additive=True,
        storage_condition="REFRIGERATED", receiving_temp_min=Decimal("0"), receiving_temp_max=Decimal("10"))
    mat("PM-000001", "1kg 파우치 필름", "SUB_MATERIAL", "FILM", "EA", "WH-PKG", expiry_tracking_required=False,
        production_usable=True, default_supplier_id=sup2.id)
    mat("PM-000002", "10입 박스", "SUB_MATERIAL", "BOX", "EA", "WH-PKG", expiry_tracking_required=False, default_supplier_id=sup2.id)
    mat("OM-000001", "충진기 필터", "OTHER_MATERIAL", "FILTER", "EA", "WH-CON", expiry_tracking_required=False,
        production_usable=False, inspection_required=False)

    get_or_create(
        db, Product,
        {"product_name": "프리믹스 1kg", "food_type": "혼합음료베이스", "report_number": "2026-0001", "content_quantity": Decimal("1"),
         "content_unit_id": units["kg"].id, "base_unit_id": units["EA"].id, "packaging_unit": "파우치", "units_per_box": 10,
         "standard_batch_size": Decimal("100"), "standard_batch_unit_id": units["kg"].id, "shelf_life_days": 365,
         "storage_condition": "AMBIENT", "standard_yield_pct": Decimal("98"), "default_warehouse_id": whs["WH-FG"].id},
        organization_id=org.id, product_code="FP-000001",
    )
    for code, name, et, iface in [("SCL-01", "계량대 저울 1", "SCALE", "USB"), ("MIX-01", "리본 믹서 1", "MIXER", None),
                                  ("FIL-01", "자동 충진기 1", "FILLER", None), ("MD-01", "금속검출기 1", "METAL_DETECTOR", None)]:
        get_or_create(db, Equipment, {"name": name, "equipment_type": et, "interface_type": iface, "facility_id": f1.id, "line_code": "L1"},
                      organization_id=org.id, code=code)


def main(demo: bool) -> None:
    with SessionLocal() as db:
        org = seed_base(db)
        if demo:
            seed_demo(db, org)
        db.commit()
    print("seed completed", "(demo)" if demo else "")


if __name__ == "__main__":
    main("--demo" in sys.argv)
