"""Phase 4: 계량/차감/잔량, WB, 공정/QR, 분할/병합, 포장재. 필수 테스트 003, 004, 005, 006, 014, 017."""
from __future__ import annotations

from datetime import date, timedelta

from tests.conftest import auth, idem
from tests.helpers import API, data, err, lot_quantity, material, package_balance, receive_material
from tests.mes_helpers import MIX_PARAMS, run_step, setup_product, start_order, wb_of, weigh


def test_003_weigh_7kg_from_20kg_package(client):
    """003: 20kg 포장에서 7kg 계량 → 13kg, 수불/지시/LOT/작업자 연결."""
    p = setup_product(client, "계량003 제품", {"RM-000001": "50", "RM-000002": "50"}, packaging=False, tolerance="0")
    a = receive_material(client, "RM-000001", "100", 5)
    o = start_order(client, p["id"], "14")  # A 목표 7kg
    pkg = a["lots"][0]["packages"][0]
    v = data(client.post(f"{API}/weighing/validate-qr", json={"production_order_id": o["id"], "production_batch_id": o["batches"][0]["id"],
                                                              "raw": pkg["qr_payload"]}, headers=auth(client, "worker")))
    assert v["valid"] is True and v["target_quantity"] == "7" and v["remaining_quantity"] == "7" and v["current_quantity"] == "20"
    assert v["expected_package_remaining"] == "13"
    rec = data(weigh(client, o, pkg["qr_payload"], "7"))
    r = rec["record"]
    assert r["actual_quantity"] == "7" and r["production_order_id"] == o["id"] and r["material_lot_id"] == a["lots"][0]["lot_id"]
    assert package_balance(client, pkg["package_number"])["quantity"] == "13"
    assert lot_quantity(client, a["lots"][0]["lot_number"]) == "93"
    # 원장: 지시·LOT·작업자 연결
    txs = data(client.get(f"{API}/inventory/transactions", params={"lot_number": a["lots"][0]["lot_number"],
                                                                   "transaction_type": "PRODUCTION_ISSUE"}, headers=auth(client, "pm")))["items"]
    assert len(txs) == 1 and txs[0]["quantity"] == "7" and txs[0]["performed_by_name"] == "현장작업자"
    assert txs[0]["reference_type"] == "WEIGHING" and txs[0]["balance_after"] == "13"
    assert rec["weighing_batch"]["lot_number"].startswith("WB-")
    # 같은 포장 재스캔 → 조회는 허용, 목표 완료 경고
    v2 = data(client.post(f"{API}/weighing/validate-qr", json={"production_order_id": o["id"], "production_batch_id": o["batches"][0]["id"],
                                                               "raw": pkg["qr_payload"]}, headers=auth(client, "worker")))
    assert v2["cumulative_quantity"] == "7" and v2["warnings"]
    # 8kg + 5kg 사용 후 7kg 잔량, QR 유지 (PRD §12)
    o2 = start_order(client, p["id"], "26")  # A 목표 13
    data(weigh(client, o2, pkg["qr_payload"], "8"))
    data(weigh(client, o2, pkg["qr_payload"], "5"))
    assert package_balance(client, pkg["package_number"])["quantity"] == "0"


def test_004_split_weighing_two_lots_to_wb(client):
    """004: 10kg 목표, LOT A 6kg + LOT B 4kg → 두 LOT 차감 및 WB 연결."""
    p = setup_product(client, "계량004 제품", {"RM-000003": "50", "RM-000002": "50"}, packaging=False)
    lot_a = receive_material(client, "RM-000003", "6", 1)
    lot_b = receive_material(client, "RM-000003", "20", 1)
    b_mat = receive_material(client, "RM-000002", "10", 1)
    o = start_order(client, p["id"], "20")
    data(weigh(client, o, lot_a["lots"][0]["packages"][0]["qr_payload"], "6"))
    data(weigh(client, o, lot_b["lots"][0]["packages"][0]["qr_payload"], "4"))
    data(weigh(client, o, b_mat["lots"][0]["packages"][0]["qr_payload"], "10"))
    assert lot_quantity(client, lot_a["lots"][0]["lot_number"]) == "0"
    assert lot_quantity(client, lot_b["lots"][0]["lot_number"]) == "16"
    wb = wb_of(client, o)[0]
    done = data(client.post(f"{API}/weighing/batches/{wb['id']}/complete", json={}, headers={**auth(client, "worker"), **idem()}))
    assert done["status"] == "READY" and done["total_quantity"] == "20" and done["qr_code_id"]
    lots_in_wb = {r["lot_number"] for r in done["records"]}
    assert lots_in_wb == {lot_a["lots"][0]["lot_number"], lot_b["lots"][0]["lot_number"], b_mat["lots"][0]["lot_number"]}
    # 추적: WB 역방향에 두 LOT 모두 포함
    back = data(client.get(f"{API}/trace/backward/{done['lot_number']}", headers=auth(client, "qm")))
    back_lots = {n["lot_number"] for n in back["nodes"]}
    assert {lot_a["lots"][0]["lot_number"], lot_b["lots"][0]["lot_number"]} <= back_lots
    od = data(client.get(f"{API}/production-orders/{o['id']}", headers=auth(client, "pm")))
    assert od["status"] == "WEIGHING_COMPLETED"


def test_005_wrong_material_blocked_no_deduction(client):
    """005: 지시에 없는 원료 스캔 → 경고/차단, 차감 없음."""
    p = setup_product(client, "계량005 제품", {"RM-000002": "100"}, packaging=False)
    wrong = receive_material(client, "RM-000003", "10", 1)
    receive_material(client, "RM-000002", "10", 1)
    o = start_order(client, p["id"], "5")
    pkg = wrong["lots"][0]["packages"][0]
    v = data(client.post(f"{API}/weighing/validate-qr", json={"production_order_id": o["id"], "production_batch_id": o["batches"][0]["id"],
                                                              "raw": pkg["qr_payload"]}, headers=auth(client, "worker")))
    assert v["valid"] is False and v["errors"][0]["error_code"] == "MATERIAL_NOT_IN_ORDER"
    err(weigh(client, o, pkg["qr_payload"], "5"), "MATERIAL_NOT_IN_ORDER", 422)
    assert package_balance(client, pkg["package_number"])["quantity"] == "10"
    # 잘못된 QR
    err(weigh(client, o, '{"version":1,"type":"RAW_MATERIAL_PACKAGE","id":"00000000-0000-0000-0000-000000000000","lot":"X"}', "1"),
        "INVALID_QR", 422)
    err(weigh(client, o, "not-a-qr-#$", "1"), "INVALID_QR", 422)


def test_006_expired_material_blocked(client):
    """006: 기한 경과 원료 → 차단, 차감 없음."""
    p = setup_product(client, "계량006 제품", {"RM-000002": "100"}, packaging=False)
    res = receive_material(client, "RM-000002", "10", 1, expiry_days=10)
    from app.core.db import SessionLocal
    from sqlalchemy import text

    past = date.today() - timedelta(days=1)
    with SessionLocal() as db:  # 시간 경과 시뮬레이션 (테스트 전용)
        db.execute(text("UPDATE material_lots SET expiry_date=:d WHERE id=:id"), {"d": past, "id": res["lots"][0]["lot_id"]})
        db.execute(text("UPDATE inventory_balances SET expiry_date=:d WHERE lot_number=:l"), {"d": past, "l": res["lots"][0]["lot_number"]})
        db.commit()
    o = start_order(client, p["id"], "5")
    pkg = res["lots"][0]["packages"][0]
    err(weigh(client, o, pkg["qr_payload"], "5"), "EXPIRED_MATERIAL", 422)
    assert package_balance(client, pkg["package_number"])["quantity"] == "10"


def test_tolerance_and_quality_blocks(client):
    p = setup_product(client, "허용오차 제품", {"RM-000002": "100"}, packaging=False, tolerance="1")
    res = receive_material(client, "RM-000002", "20", 1)
    hold = receive_material(client, "RM-000002", "10", 1, approve=False)  # 검수대기(PENDING)
    o = start_order(client, p["id"], "10")
    pkg = res["lots"][0]["packages"][0]
    err(weigh(client, o, hold["lots"][0]["packages"][0]["qr_payload"], "1"), "QUALITY_NOT_RELEASED", 422)
    err(weigh(client, o, pkg["qr_payload"], "10.5"), "TOLERANCE_EXCEEDED", 422)
    from app.core.db import SessionLocal
    from app.models import User
    from sqlalchemy import select

    with SessionLocal() as db:
        pm_id = str(db.execute(select(User.id).where(User.username == "pm")).scalar_one())
        worker_id = str(db.execute(select(User.id).where(User.username == "worker")).scalar_one())
    err(weigh(client, o, pkg["qr_payload"], "10.5", override_approved_by=worker_id), "FORBIDDEN", 403)
    data(weigh(client, o, pkg["qr_payload"], "10.5", override_approved_by=pm_id))
    wb = wb_of(client, o)[0]
    assert data(client.post(f"{API}/weighing/batches/{wb['id']}/complete", json={}, headers={**auth(client, "worker"), **idem()}))["status"] == "READY"


def test_017_weighing_correction(client):
    """017: 확정 계량 정정 → 원본 유지, 승인 역거래, 후속 LOT 제한."""
    p = setup_product(client, "정정017 제품", {"RM-000002": "100"}, packaging=False)
    res = receive_material(client, "RM-000002", "20", 1)
    o = start_order(client, p["id"], "10")
    pkg = res["lots"][0]["packages"][0]
    rec = data(weigh(client, o, pkg["qr_payload"], "4"))["record"]
    # 작업자 → 승인 요청 (즉시 반영 안 됨)
    rq = data(client.post(f"{API}/weighing/records/{rec['id']}/reverse", json={"reason": "오계량"}, headers=auth(client, "worker")))
    assert rq["status"] == "PENDING_APPROVAL"
    assert package_balance(client, pkg["package_number"])["quantity"] == "16"
    # 승인권자 승인 → 역거래 실행
    data(client.post(f"{API}/approvals/{rq['approval_request_id']}/approve", json={"note": "확인"}, headers=auth(client, "pm")))
    assert package_balance(client, pkg["package_number"])["quantity"] == "20"
    recs = data(client.get(f"{API}/weighing/records", params={"production_order_id": o["id"]}, headers=auth(client, "pm")))
    assert recs[0]["status"] == "REVERSED" and recs[0]["actual_quantity"] == "4"  # 원본 유지
    txs = data(client.get(f"{API}/inventory/transactions", params={"lot_number": res["lots"][0]["lot_number"]}, headers=auth(client, "pm")))["items"]
    assert {t["transaction_type"] for t in txs} >= {"PRODUCTION_ISSUE", "REVERSAL"}
    # 재계량 후 WB 완료 → 후속 LOT 존재 시 직접 정정 금지
    rec2 = data(weigh(client, o, pkg["qr_payload"], "10"))["record"]
    wb = wb_of(client, o)[0]
    data(client.post(f"{API}/weighing/batches/{wb['id']}/complete", json={}, headers={**auth(client, "worker"), **idem()}))
    err(client.post(f"{API}/weighing/records/{rec2['id']}/reverse", json={"reason": "x"}, headers=auth(client, "pm")), "DOWNSTREAM_EXISTS", 422)
    # 오염 반납 → 폐기 처리 (정상 재고 미복귀)
    o2 = start_order(client, p["id"], "2")
    rec3 = data(weigh(client, o2, pkg["qr_payload"], "2"))["record"]
    before = package_balance(client, pkg["package_number"])["quantity"]
    data(client.post(f"{API}/weighing/records/{rec3['id']}/return", json={"reason": "바닥 낙하", "contaminated": True}, headers=auth(client, "pm")))
    assert package_balance(client, pkg["package_number"])["quantity"] == before


def test_014_split_merge_and_process_rules(client):
    """014: A+B→C→D/E 분할/병합 양방향 연결 유지 + 공정 규칙."""
    p = setup_product(client, "분할병합 제품", {"RM-000002": "100"}, packaging=False,
                      steps=[(10, "MIX"), (20, "HEAT"), (30, "PACK")])
    res = receive_material(client, "RM-000002", "40", 2)
    o = start_order(client, p["id"], "20", batches=2)
    pk = res["lots"][0]["packages"]
    data(weigh(client, o, pk[0]["qr_payload"], "10", batch_idx=0))
    data(weigh(client, o, pk[1]["qr_payload"], "10", batch_idx=1))
    wbs = wb_of(client, o)
    for w in wbs:
        data(client.post(f"{API}/weighing/batches/{w['id']}/complete", json={}, headers={**auth(client, "worker"), **idem()}))
    wbs = wb_of(client, o)
    wb_a = next(w for w in wbs if w["production_batch_id"] == o["batches"][0]["id"])
    wb_b = next(w for w in wbs if w["production_batch_id"] == o["batches"][1]["id"])
    qa = data(client.get(f"{API}/qr/{wb_a['qr_code_id']}", headers=auth(client, "worker")))["payload"]
    qb = data(client.get(f"{API}/qr/{wb_b['qr_code_id']}", headers=auth(client, "worker")))["payload"]
    w = auth(client, "worker")
    # 이전 공정 미완료 → 가열 시작 불가
    e2 = data(client.post(f"{API}/process-executions", json={"production_order_id": o["id"], "routing_step_sequence": 20,
                                                             "production_batch_id": o["batches"][0]["id"]}, headers=w))
    err(client.post(f"{API}/process-executions/{e2['id']}/start", headers=w), "PREVIOUS_STEP_INCOMPLETE", 422)
    # 혼합: A(배치1) + B(배치2) 병합 → 승인 필요
    e = data(client.post(f"{API}/process-executions", json={"production_order_id": o["id"], "routing_step_sequence": 10,
                                                            "production_batch_id": o["batches"][0]["id"]}, headers=w))
    data(client.post(f"{API}/process-executions/{e['id']}/start", headers=w))
    data(client.post(f"{API}/process-executions/{e['id']}/inputs", json={"raw": qa, "quantity": "10"}, headers={**w, **idem()}))
    err(client.post(f"{API}/process-executions/{e['id']}/inputs", json={"raw": qb, "quantity": "10"}, headers={**w, **idem()}), "BATCH_MISMATCH", 422)
    from app.core.db import SessionLocal
    from app.models import User
    from sqlalchemy import select

    with SessionLocal() as db:
        pm_id = str(db.execute(select(User.id).where(User.username == "pm")).scalar_one())
    data(client.post(f"{API}/process-executions/{e['id']}/inputs", json={"raw": qb, "quantity": "10", "merge_approved_by": pm_id},
                     headers={**w, **idem()}))
    # 소진된 WB 재투입 금지
    err(client.post(f"{API}/process-executions/{e['id']}/inputs", json={"raw": qa, "quantity": "1"}, headers={**w, **idem()}), "INPUT_NOT_USABLE", 422)
    # 필수 조건 누락 → 완료 불가
    err(client.post(f"{API}/process-executions/{e['id']}/complete", json={"outputs": [{"quantity": "19.8"}], "process_loss_quantity": "0.2"},
                    headers={**w, **idem()}), "REQUIRED_PARAMETER_MISSING", 422)
    data(client.post(f"{API}/process-executions/{e['id']}/parameters", json={"records": MIX_PARAMS}, headers=w))
    # 물질수지 불일치
    err(client.post(f"{API}/process-executions/{e['id']}/complete", json={"outputs": [{"quantity": "19"}]}, headers={**w, **idem()}),
        "MASS_BALANCE_MISMATCH", 422)
    c = data(client.post(f"{API}/process-executions/{e['id']}/complete", json={"outputs": [{"quantity": "19.8"}], "process_loss_quantity": "0.2"},
                         headers={**w, **idem()}))
    lot_c = c["outputs"][0]
    assert lot_c["lot_number"].startswith("WIP-") and c["execution"]["process_yield"] == "99"
    # C → D/E 분할 (가열: CCP, 온도 기록 필수)
    data(client.post(f"{API}/process-executions/{e2['id']}/start", headers=w))
    qc = data(client.get(f"{API}/qr/{lot_c['qr_code_id']}", headers=w))["payload"]
    assert qc != qa  # 서로 다른 QR
    data(client.post(f"{API}/process-executions/{e2['id']}/inputs", json={"raw": qc, "quantity": "19.8", "merge_approved_by": pm_id},
                     headers={**w, **idem()}))
    data(client.post(f"{API}/process-executions/{e2['id']}/parameters", json={"records": [
        {"parameter_code": "TEMPERATURE", "value_numeric": "90"}, {"parameter_code": "HOLD_TIME", "value_numeric": "20"}]}, headers=w))
    d = data(client.post(f"{API}/process-executions/{e2['id']}/complete", json={"outputs": [{"quantity": "9.9"}, {"quantity": "9.9"}]},
                         headers={**w, **idem()}))
    lot_d, lot_e = d["outputs"]
    # 양방향 연결
    q = auth(client, "qm")
    back = {n["lot_number"] for n in data(client.get(f"{API}/trace/backward/{lot_d['lot_number']}", headers=q))["nodes"]}
    assert {wb_a["lot_number"], wb_b["lot_number"], lot_c["lot_number"], res["lots"][0]["lot_number"]} <= back
    fwd = {n["lot_number"] for n in data(client.get(f"{API}/trace/forward/{wb_a['lot_number']}", headers=q))["nodes"]}
    assert {lot_c["lot_number"], lot_d["lot_number"], lot_e["lot_number"]} <= fwd
    g = data(client.get(f"{API}/trace/graph/{lot_c['lot_number']}", headers=q))
    types = {(x["event_type"]) for x in g["edges"]}
    assert "MERGE" in types and "PROCESSING" in types


def test_ccp_out_of_range_holds_process(client):
    p = setup_product(client, "CCP 제품", {"RM-000002": "100"}, packaging=False, steps=[(10, "HEAT"), (20, "PACK")])
    res = receive_material(client, "RM-000002", "10", 1)
    o = start_order(client, p["id"], "5")
    data(weigh(client, o, res["lots"][0]["packages"][0]["qr_payload"], "5"))
    wb = wb_of(client, o)[0]
    wb = data(client.post(f"{API}/weighing/batches/{wb['id']}/complete", json={}, headers={**auth(client, "worker"), **idem()}))
    w = auth(client, "worker")
    e = data(client.post(f"{API}/process-executions", json={"production_order_id": o["id"], "routing_step_sequence": 10,
                                                            "production_batch_id": o["batches"][0]["id"]}, headers=w))
    data(client.post(f"{API}/process-executions/{e['id']}/start", headers=w))
    r = data(client.post(f"{API}/process-executions/{e['id']}/parameters", json={"records": [
        {"parameter_code": "TEMPERATURE", "value_numeric": "70"}, {"parameter_code": "HOLD_TIME", "value_numeric": "20"}]}, headers=w))
    assert r["held"] is True and r["execution"]["status"] == "HOLD"
    err(client.post(f"{API}/process-executions/{e['id']}/release-hold", json={"reason": "x"}, headers=w), "FORBIDDEN", 403)
    data(client.post(f"{API}/process-executions/{e['id']}/release-hold", json={"reason": "재가열 확인"}, headers=auth(client, "pm")))
