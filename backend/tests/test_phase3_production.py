"""Phase 3: 제품/BOM/버전, 제조지시, 배치, 소요량, 예약. 필수 테스트 015."""
from __future__ import annotations

import uuid
from datetime import date

from tests.conftest import auth, idem
from tests.helpers import API, data, err, material, receive_material, warehouse


def _units(client):
    return {u["code"]: u["id"] for u in data(client.get(f"{API}/units", headers=auth(client, "admin")))}


def new_product(client, name: str) -> dict:
    u = _units(client)
    return data(client.post(f"{API}/products", json={
        "product_name": name, "content_quantity": "1", "content_unit_id": u["kg"], "base_unit_id": u["EA"],
        "standard_batch_size": "100", "shelf_life_days": 365, "standard_yield_pct": "98"}, headers=auth(client, "admin")))


def bom_items(client, ratios: dict[str, str], with_packaging: bool = True) -> list[dict]:
    u = _units(client)
    items = [{"item_type": "FORMULA", "material_id": material(client, code)["id"], "quantity_basis": "RATIO", "ratio_percent": r,
              "unit_id": u["kg"], "tolerance_pct": "1", "weighing_group": "G1"} for code, r in ratios.items()]
    if with_packaging:
        items.append({"item_type": "PACKAGING", "material_id": material(client, "PM-000001")["id"], "quantity_basis": "PER_UNIT",
                      "standard_quantity": "1", "unit_id": u["EA"], "tolerance_pct": "0"})
    return items


def test_bom_ratio_validation_calculation_and_approval(client):
    pm, adm = auth(client, "pm"), auth(client, "admin")
    u = _units(client)
    p = new_product(client, "BOM 계산 테스트 제품")
    bad = bom_items(client, {"RM-000001": "50", "RM-000002": "30", "RM-000003": "15", "RM-000004": "4"})
    b = data(client.post(f"{API}/boms", json={"product_id": p["id"], "version": {"base_quantity": "100", "base_unit_id": u["kg"],
                                                                                 "items": bad}}, headers=pm))
    v1 = b["versions"][0]
    assert v1["status"] == "DRAFT" and v1["ratio_total"] == "99"
    # 생산관리자는 BOM 승인 권한 없음
    err(client.post(f"{API}/boms/{b['id']}/approve", json={"version_id": v1["id"]}, headers=pm), "FORBIDDEN", 403)
    # 합계 100% 아님 → 승인 불가
    err(client.post(f"{API}/boms/{b['id']}/approve", json={"version_id": v1["id"]}, headers=adm), "BOM_RATIO_INVALID", 422)
    # 승인 전 제조지시 불가
    err(client.post(f"{API}/production-orders", json={"production_date": str(date.today()), "product_id": p["id"],
                                                      "planned_quantity": "200"}, headers={**pm, **idem()}), "BOM_NOT_APPROVED", 422)
    good = bom_items(client, {"RM-000001": "50", "RM-000002": "30", "RM-000003": "15", "RM-000004": "5"})
    data(client.patch(f"{API}/boms/versions/{v1['id']}", json={"items": good}, headers=pm))
    data(client.post(f"{API}/boms/{b['id']}/approve", json={"version_id": v1["id"]}, headers=adm))
    # 승인 버전 수정 불가
    err(client.patch(f"{API}/boms/versions/{v1['id']}", json={"change_note": "x"}, headers=pm), "INVALID_STATE_TRANSITION", 409)
    # 계산: 200kg → 100/60/30/10, 포장재(1kg 기준) 200EA 는 100% 산정 제외
    calc = data(client.post(f"{API}/boms/{b['id']}/calculate", json={"planned_quantity": "200", "target_fg_quantity": "200"}, headers=pm))
    req = {r["material_code"]: r["required_quantity"] for r in calc["items"]}
    assert req == {"RM-000001": "100", "RM-000002": "60", "RM-000003": "30", "RM-000004": "10", "PM-000001": "200"}
    assert calc["ratio_total"] == "100"
    # 목표 완제품량 기준: 표준수율 98% 반영 → 98EA → 100kg
    calc2 = data(client.post(f"{API}/boms/{b['id']}/calculate", json={"target_fg_quantity": "98"}, headers=pm))
    assert calc2["planned_quantity"] == "100"


def test_015_bom_revision_keeps_order_snapshot(client):
    """015: BOM v1 지시 후 v2 개정 → 기존 지시 v1 유지, 신규 지시 v2."""
    pm, adm = auth(client, "pm"), auth(client, "admin")
    u = _units(client)
    p = new_product(client, "BOM 개정 테스트 제품")
    b = data(client.post(f"{API}/boms", json={"product_id": p["id"], "version": {
        "base_quantity": "100", "base_unit_id": u["kg"], "items": bom_items(client, {"RM-000001": "60", "RM-000002": "40"})}}, headers=pm))
    data(client.post(f"{API}/boms/{b['id']}/approve", json={"version_id": b["versions"][0]["id"]}, headers=adm))
    o1 = data(client.post(f"{API}/production-orders", json={"production_date": str(date.today()), "product_id": p["id"],
                                                            "planned_quantity": "100"}, headers={**pm, **idem()}))
    assert o1["bom_snapshot"]["version_no"] == 1
    # v2: v1 복사 후 비율 변경
    b2 = data(client.post(f"{API}/boms/{b['id']}/versions", json={"copy_from_version_id": b["versions"][0]["id"]}, headers=pm))
    v2 = b2["versions"][1]
    assert v2["status"] == "DRAFT" and v2["copied_from_id"] == b["versions"][0]["id"]
    data(client.patch(f"{API}/boms/versions/{v2['id']}", json={"items": bom_items(client, {"RM-000001": "70", "RM-000002": "30"})}, headers=pm))
    data(client.post(f"{API}/boms/{b['id']}/approve", json={"version_id": v2["id"]}, headers=adm))
    det = data(client.get(f"{API}/boms/{b['id']}", headers=pm))
    assert [v["status"] for v in det["versions"]] == ["OBSOLETE", "APPROVED"]
    o1b = data(client.get(f"{API}/production-orders/{o1['id']}", headers=pm))
    assert o1b["bom_snapshot"]["version_no"] == 1
    assert {i["material_code"]: i["required_quantity"] for i in o1b["items"] if i["item_type"] == "FORMULA"} == {"RM-000001": "60", "RM-000002": "40"}
    o2 = data(client.post(f"{API}/production-orders", json={"production_date": str(date.today()), "product_id": p["id"],
                                                            "planned_quantity": "100"}, headers={**pm, **idem()}))
    assert o2["bom_snapshot"]["version_no"] == 2
    assert {i["material_code"]: i["required_quantity"] for i in o2["items"] if i["item_type"] == "FORMULA"} == {"RM-000001": "70", "RM-000002": "30"}


def test_order_batches_reservation_fefo_and_cancel(client):
    pm, adm, worker = auth(client, "pm"), auth(client, "admin"), auth(client, "worker")
    u = _units(client)
    # 별도 원료로 재고 독립성 확보
    mats = {}
    for name in ("예약테스트 원료 X", "예약테스트 원료 Y"):
        mats[name] = data(client.post(f"{API}/materials", json={"material_name": name, "major_category": "MAIN_MATERIAL",
                                                                "base_unit_id": u["kg"], "inspection_required": False,
                                                                "expiry_tracking_required": True,
                                                                "default_warehouse_id": warehouse(client, "WH-AMB")["id"]}, headers=adm))
    late = receive_material(client, mats["예약테스트 원료 X"]["material_code"], "30", 1, expiry_days=300)
    early = receive_material(client, mats["예약테스트 원료 X"]["material_code"], "30", 1, expiry_days=100)
    receive_material(client, mats["예약테스트 원료 Y"]["material_code"], "10", 1)
    p = new_product(client, "예약 테스트 제품")
    items = [{"item_type": "FORMULA", "material_id": mats["예약테스트 원료 X"]["id"], "quantity_basis": "RATIO", "ratio_percent": "80",
              "unit_id": u["kg"]},
             {"item_type": "FORMULA", "material_id": mats["예약테스트 원료 Y"]["id"], "quantity_basis": "RATIO", "ratio_percent": "20",
              "unit_id": u["kg"]}]
    b = data(client.post(f"{API}/boms", json={"product_id": p["id"], "version": {"base_quantity": "100", "base_unit_id": u["kg"],
                                                                                 "items": items}}, headers=pm))
    data(client.post(f"{API}/boms/{b['id']}/approve", json={"version_id": b["versions"][0]["id"]}, headers=adm))
    o = data(client.post(f"{API}/production-orders", json={"production_date": str(date.today()), "product_id": p["id"],
                                                           "planned_quantity": "60", "batch_count": 2}, headers={**pm, **idem()}))
    assert o["order_number"].startswith("MO-") and len(o["batches"]) == 2
    assert all(bt["batch_number"].startswith("BATCH-") and bt["planned_quantity"] == "30" for bt in o["batches"])
    req = {i["material_name"]: i for i in o["items"]}
    assert req["예약테스트 원료 X"]["required_quantity"] == "48" and req["예약테스트 원료 Y"]["required_quantity"] == "12"
    assert req["예약테스트 원료 Y"]["is_short"] is True  # 10kg 재고, 12kg 필요
    # FEFO 추천: 기한 빠른 LOT 먼저
    assert req["예약테스트 원료 X"]["recommendations"][0]["lot_number"] == early["lots"][0]["lot_number"]
    # 작업자 승인 불가 (018)
    err(client.post(f"{API}/production-orders/{o['id']}/approve", json={}, headers=worker), "FORBIDDEN", 403)
    err(client.post(f"{API}/production-orders/{o['id']}/approve", json={"require_full_stock": True}, headers=pm), "INSUFFICIENT_STOCK", 422)
    ap = data(client.post(f"{API}/production-orders/{o['id']}/approve", json={}, headers=pm))
    assert ap["status"] == "APPROVED" and ap["shortages"][0]["shortage"] == "2"
    pk = data(client.get(f"{API}/inventory/packages", params={"material_id": mats["예약테스트 원료 X"]["id"]}, headers=pm))["items"]
    by_lot = {r["lot_number"]: r for r in pk}
    assert by_lot[early["lots"][0]["lot_number"]]["reserved_quantity"] == "30"
    assert by_lot[late["lots"][0]["lot_number"]]["reserved_quantity"] == "18"
    data(client.post(f"{API}/production-orders/{o['id']}/cancel", json={"reason": "계획 변경"}, headers=pm))
    pk = data(client.get(f"{API}/inventory/packages", params={"material_id": mats["예약테스트 원료 X"]["id"]}, headers=pm))["items"]
    assert all(r["reserved_quantity"] == "0" for r in pk)
