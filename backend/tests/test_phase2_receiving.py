"""Phase 2: 기준정보, 입고, OCR, 검수, LOT/QR, 라벨, 재고/수불. 필수 테스트 001, 002, 009, 010, 011, 013, 019."""
from __future__ import annotations

import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import auth, idem
from tests.helpers import (
    API, data, err, inspection_pass, lot_quantity, material, package_balance, png_bytes, receive_material, supplier, warehouse,
)


def test_material_master_three_categories_and_no_stock(client):
    adm = auth(client, "admin")
    units = {u["code"]: u["id"] for u in data(client.get(f"{API}/units", headers=adm))}
    codes = {}
    for major, prefix in [("MAIN_MATERIAL", "RM"), ("SUB_MATERIAL", "PM"), ("OTHER_MATERIAL", "OM")]:
        m = data(client.post(f"{API}/materials", json={"material_name": f"테스트 {major}", "major_category": major,
                                                       "base_unit_id": units["kg" if major == "MAIN_MATERIAL" else "EA"]}, headers=adm))
        assert m["material_code"].startswith(prefix + "-")
        codes[major] = m
    assert codes["OTHER_MATERIAL"]["production_usable"] is False
    assert codes["OTHER_MATERIAL"]["expiry_tracking_required"] is False
    inv = data(client.get(f"{API}/inventory", params={"material_id": codes["MAIN_MATERIAL"]["id"]}, headers=adm))
    assert inv == []  # 마스터 등록은 재고를 증가시키지 않음
    # 권한: 작업자는 원료 등록 불가
    r = client.post(f"{API}/materials", json={"material_name": "x", "major_category": "MAIN_MATERIAL", "base_unit_id": units["kg"]},
                    headers=auth(client, "worker"))
    err(r, "FORBIDDEN", 403)
    # 감사로그(마스터 변경)
    data(client.patch(f"{API}/materials/{codes['MAIN_MATERIAL']['id']}", json={"safety_stock": "10", "reason": "기준 변경"}, headers=adm))


def test_002_receipt_100kg_5_packages(client):
    """002: 100kg(20kg×5) → LOT 1, 포장 QR 5, 총 100kg, 개별 20kg."""
    res = receive_material(client, "RM-000001", "100", 5)
    assert len(res["lots"]) == 1
    lot = res["lots"][0]
    assert lot["lot_number"].startswith("RM-") and lot["quantity"] == "100" and lot["quality_status"] == "PASSED"
    assert len(lot["packages"]) == 5
    assert len({p["qr_code_id"] for p in lot["packages"]}) == 5
    assert all(p["quantity"] == "20" for p in lot["packages"])
    assert all(p["package_number"].startswith("PKG-") for p in lot["packages"])
    assert lot_quantity(client, lot["lot_number"]) == "100"
    # QR payload 는 불변 식별자만
    import json

    pl = json.loads(lot["packages"][0]["qr_payload"])
    assert set(pl) == {"version", "type", "id", "lot"} and pl["type"] == "RAW_MATERIAL_PACKAGE"
    # 스캔 해석 → 포장 20kg, LOT 100kg
    d = data(client.post(f"{API}/qr/resolve", json={"raw": lot["packages"][0]["qr_payload"]}, headers=auth(client, "worker")))
    assert d["current_quantity"] == "20" and d["lot_quantity"] == "100" and d["usable"] is True
    # LOT 번호 직접 검색 (QR 훼손 대비)
    d2 = data(client.post(f"{API}/qr/resolve", json={"raw": lot["packages"][1]["package_number"]}, headers=auth(client, "worker")))
    assert d2["package_number"] == lot["packages"][1]["package_number"]
    # 수불 원장 RECEIPT 5건
    txs = data(client.get(f"{API}/inventory/transactions", params={"lot_number": lot["lot_number"]}, headers=auth(client, "admin")))
    assert txs["total"] == 5 and all(t["transaction_type"] == "RECEIPT" for t in txs["items"])
    # 라벨 PDF (4가지 규격 + 사용자 지정)
    for size in ("50x30", "60x40", "80x50", "100x70", "90x45"):
        r = client.get(f"{API}/qr/{lot['packages'][0]['qr_code_id']}/label", params={"size": size}, headers=auth(client, "wh"))
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
    z = client.get(f"{API}/qr/{lot['packages'][0]['qr_code_id']}/label", params={"format": "zpl"}, headers=auth(client, "wh"))
    assert z.status_code == 200 and "^XA" in z.text and "^BQN" in z.text


def test_001_ocr_extract_review_correct_and_original_kept(client):
    """001: COA 촬영 → 추출/자동 입력/누락 표시/수정/원본 저장."""
    wh = auth(client, "wh")
    m = material(client, "RM-000001")
    r = data(client.post(f"{API}/receipts", json={"supplier_id": None, "items": [
        {"material_id": m["id"], "quantity": "100", "package_count": 5}]}, headers={**wh, **idem()}))
    img = png_bytes("COA original")
    up = data(client.post(f"{API}/receipts/{r['id']}/documents", files={"files": ("coa.png", img, "image/png")},
                          data={"document_type": "COA"}, headers=wh))
    job_id = up[0]["ocr_job_id"]
    assert job_id
    res = data(client.get(f"{API}/ocr/jobs/{job_id}/result", headers=wh))
    st = res["structured"]
    assert res["job"]["status"] == "REVIEW_REQUIRED"
    assert st["supplier_name"]["value"] == "ABC 원료" and st["supplier_name"]["matched_entity_id"]  # 마스터 매칭
    assert st["supplier_lot"]["value"] == "ABC261125"
    assert st["manufactured_date"]["value"] == "2026-11-20" and st["expiry_date"]["value"] == "2027-11-19"
    assert st["quantity"]["value"] == "100" and st["unit"]["value"] == "kg"
    assert st["expiry_type"]["value"] == "CONSUME_BY"
    assert st["review_required"] is True  # 고신뢰도도 검토 필수
    assert st["tests"] and st["tests"][0]["judgement"] == "PASS"
    assert res["provider_notice"]  # Mock 결과 명시
    # 원본과 결과 동시 표시: 원본 파일 그대로 다운로드
    f = client.get(res["document"]["file_url"].replace("/api/v1", API), headers=wh)
    assert f.status_code == 200 and f.content == img
    # 수동 수정 후 확정 → 입고 품목 반영
    data(client.post(f"{API}/ocr/jobs/{job_id}/confirm", json={"fields": {"supplier_lot": "ABC261125-A"}}, headers=wh))
    det = data(client.get(f"{API}/receipts/{r['id']}", headers=wh))
    assert det["items"][0]["supplier_lot_number"] == "ABC261125-A"
    assert det["items"][0]["expiry_date"] == "2027-11-19"
    assert det["supplier_id"] == st["supplier_name"]["matched_entity_id"]
    assert det["coa_number"] == "COA-2026-1120"
    assert det["status"] == "DRAFT"
    assert any(i["source"] == "COA" for i in det["inspections"])  # COA 시험결과 분리 저장
    # 중복 문서 감지
    up2 = data(client.post(f"{API}/receipts/{r['id']}/documents", files={"files": ("coa.png", img, "image/png")},
                           data={"document_type": "COA"}, headers=wh))
    assert up2[0]["duplicate_of"] and up2[0]["ocr_job_id"] is None


def test_ocr_missing_fields_and_failure_manual(client):
    from app.services.ocr.parser import parse
    from app.services.ocr.providers import OCRLine

    doc = parse([OCRLine("제품명: 정제당", 0.97), OCRLine("제조일자: 2026년 11월 3일", 0.95), OCRLine("유통기한 2027.05.02", 0.7)], "MATERIAL_LABEL")
    assert doc.fields["material_name"].value == "정제당"
    assert str(doc.fields["manufactured_date"].value) == "2026-11-03"
    assert doc.fields["expiry_type"].value == "SELL_BY"
    assert doc.fields["supplier_lot"].missing and doc.fields["quantity"].missing  # 임의 생성 금지
    multi = parse([OCRLine("품명 수량 단가 금액", 0.9), OCRLine("정제당 B 20 kg 1,500 30,000", 0.93),
                   OCRLine("식물성유 C 15 kg 3,000 45,000", 0.91), OCRLine("합계 75,000", 0.9)], "RECEIPT")
    assert len(multi.items) == 2 and multi.items[1]["quantity"].value == Decimal("15")


def test_receipt_quality_rules(client):
    wh, qm = auth(client, "wh"), auth(client, "qm")
    m = material(client, "RM-000004")  # 냉장 향료 0~10℃
    r = data(client.post(f"{API}/receipts", json={"items": [{"material_id": m["id"], "quantity": "10", "supplier_lot_number": "X1",
                                                             "expiry_date": str(date.today() + timedelta(days=100)),
                                                             "expiry_type": "CONSUME_BY"}]}, headers={**wh, **idem()}))
    rid = r["id"]
    # 검수 전 확정 불가 (검수 필요 원료)
    err(client.post(f"{API}/receipts/{rid}/post", headers={**wh, **idem()}), "INSPECTION_REQUIRED", 422)
    data(client.post(f"{API}/receipts/{rid}/submit-inspection", headers=wh))
    # 온도 초과 → 합격 처리 불가
    err(client.post(f"{API}/receipts/{rid}/inspection", json=inspection_pass("15"), headers=qm), "OUT_OF_RANGE", 422)
    # 보류 판정 → 격리재고로 확정
    body = inspection_pass("15")
    body["overall_result"] = "HOLD"
    d = data(client.post(f"{API}/receipts/{rid}/inspection", json=body, headers=qm))
    assert d["status"] == "HOLD" and d["warnings"]
    posted = data(client.post(f"{API}/receipts/{rid}/post", headers={**wh, **idem()}))
    assert posted["lots"][0]["quality_status"] == "HOLD"
    pkg = posted["lots"][0]["packages"][0]
    res = data(client.post(f"{API}/qr/resolve", json={"raw": pkg["qr_payload"]}, headers=wh))
    assert res["usable"] is False
    # 승인은 검수 합격 후에만
    err(client.post(f"{API}/receipts/{rid}/approve", json={}, headers=qm), "INSPECTION_NOT_PASSED", 422)
    # 재검수 FAIL → 부적합, 정상 가용 전환 금지
    fail = inspection_pass("15")
    fail.update({"overall_result": "FAIL", "nonconformance_reason": "온도 이탈", "disposition": "RETURN"})
    data(client.post(f"{API}/receipts/{rid}/inspection", json=fail, headers=qm))
    res = data(client.post(f"{API}/qr/resolve", json={"raw": pkg["qr_payload"]}, headers=wh))
    assert res["quality_status"] == "REJECTED"
    data(client.post(f"{API}/receipts/{rid}/inspection", json=inspection_pass("5"), headers=qm))
    data(client.post(f"{API}/receipts/{rid}/approve", json={}, headers=qm))
    res = data(client.post(f"{API}/qr/resolve", json={"raw": pkg["qr_payload"]}, headers=wh))
    assert res["quality_status"] == "REJECTED"  # 부적합은 가용 전환 불가


def test_coa_required_for_approval(client):
    wh, qm = auth(client, "wh"), auth(client, "qm")
    m = material(client, "RM-000001")
    r = data(client.post(f"{API}/receipts", json={"items": [{"material_id": m["id"], "quantity": "5", "supplier_lot_number": "C1",
                                                             "expiry_date": str(date.today() + timedelta(days=100)),
                                                             "expiry_type": "CONSUME_BY"}]}, headers={**wh, **idem()}))
    data(client.post(f"{API}/receipts/{r['id']}/submit-inspection", headers=wh))
    data(client.post(f"{API}/receipts/{r['id']}/inspection", json=inspection_pass(), headers=qm))
    err(client.post(f"{API}/receipts/{r['id']}/approve", json={}, headers=qm), "COA_REQUIRED", 422)


def test_expired_material_cannot_be_posted_and_expiry_required(client):
    wh = auth(client, "wh")
    m = material(client, "RM-000002")
    r = data(client.post(f"{API}/receipts", json={"items": [{"material_id": m["id"], "quantity": "5", "supplier_lot_number": "E1"}]},
                         headers={**wh, **idem()}))
    err(client.post(f"{API}/receipts/{r['id']}/submit-inspection", headers=wh), "EXPIRY_REQUIRED", 422)
    # 청소도구 등 기한 해당 없음 → 검수 불필요 품목은 바로 확정
    om = material(client, "OM-000001")
    r2 = data(client.post(f"{API}/receipts", json={"items": [{"material_id": om["id"], "quantity": "3", "package_count": 3}]},
                          headers={**wh, **idem()}))
    p = data(client.post(f"{API}/receipts/{r2['id']}/post", headers={**wh, **idem()}))
    assert p["lots"][0]["lot_number"].startswith("OM-") and p["lots"][0]["quality_status"] == "PASSED"


def test_010_idempotent_post_and_transfer(client):
    """010: 동일 키 요청 2회 → 1회 기록/차감."""
    wh = auth(client, "wh")
    om = material(client, "OM-000001")
    r = data(client.post(f"{API}/receipts", json={"items": [{"material_id": om["id"], "quantity": "4", "package_count": 2}]},
                         headers={**wh, **idem()}))
    key = idem()
    a = client.post(f"{API}/receipts/{r['id']}/post", headers={**wh, **key})
    b = client.post(f"{API}/receipts/{r['id']}/post", headers={**wh, **key})
    assert a.status_code == 200 and b.status_code == 200
    assert b.json().get("idempotent_replay") is True
    assert a.json()["data"]["lots"][0]["lot_number"] == b.json()["data"]["lots"][0]["lot_number"]
    lot_no = a.json()["data"]["lots"][0]["lot_number"]
    assert lot_quantity(client, lot_no) == "4"
    # 동일 키 + 다른 본문 → 409
    err(client.post(f"{API}/receipts", json={"items": []}, headers={**wh, **key}), "IDEMPOTENCY_CONFLICT", 409)
    # 이동도 1회만
    bal_id = a.json()["data"]["lots"][0]["packages"][0]["balance_id"]
    to = warehouse(client, "WH-AMB")
    k2 = idem()
    body = {"balance_id": bal_id, "quantity": "1", "to_warehouse_id": to["id"], "reason": "위치 이동"}
    data(client.post(f"{API}/inventory/transfer", json=body, headers={**wh, **k2}))
    data(client.post(f"{API}/inventory/transfer", json=body, headers={**wh, **k2}))
    txs = data(client.get(f"{API}/inventory/transactions", params={"lot_number": lot_no, "transaction_type": "TRANSFER_OUT"},
                          headers=wh))
    assert txs["total"] == 1
    assert lot_quantity(client, lot_no) == "4"


def test_009_insufficient_stock_rejected(client):
    """009: 5kg 재고에 7kg 요청 → 거부, 5kg 유지."""
    res = receive_material(client, "RM-000002", "5", 1)
    pkg = res["lots"][0]["packages"][0]
    from app.core.db import SessionLocal
    from app.core.errors import BusinessRule
    from app.models import User
    from app.services.inventory import service as inv
    from sqlalchemy import select
    import pytest

    with SessionLocal() as db:
        u = db.execute(select(User).where(User.username == "pm")).scalar_one()
        ctx = inv.Ctx(u.organization_id, u.default_facility_id, u.id)
        with pytest.raises(BusinessRule) as e:
            inv.issue(db, ctx, uuid.UUID(pkg["balance_id"]), Decimal("7"), "PRODUCTION_ISSUE")
        assert e.value.error_code == "INSUFFICIENT_STOCK"
        db.rollback()
    # API 경로: 조정 요청도 거부
    err(client.post(f"{API}/inventory/adjustment", json={"balance_id": pkg["balance_id"], "direction": "OUT", "quantity": "7",
                                                         "reason": "test"}, headers=auth(client, "wh")), "INSUFFICIENT_STOCK", 422)
    assert package_balance(client, pkg["package_number"])["quantity"] == "5"


def test_011_concurrent_issue(client):
    """011: 10kg 재고에 동시 7kg 요청 2건 → 한 건 성공, 3kg 잔량."""
    res = receive_material(client, "RM-000002", "10", 1)
    pkg = res["lots"][0]["packages"][0]
    from app.core.db import SessionLocal
    from app.core.errors import BusinessRule
    from app.models import User
    from app.services.inventory import service as inv
    from sqlalchemy import select

    with SessionLocal() as db:
        u = db.execute(select(User).where(User.username == "pm")).scalar_one()
        org, fac, uid = u.organization_id, u.default_facility_id, u.id
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def run():
        with SessionLocal() as db:
            barrier.wait()
            try:
                inv.issue(db, inv.Ctx(org, fac, uid), uuid.UUID(pkg["balance_id"]), Decimal("7"), "PRODUCTION_ISSUE")
                db.commit()
                outcomes.append("ok")
            except BusinessRule as e:
                db.rollback()
                outcomes.append(e.error_code)

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outcomes) == ["INSUFFICIENT_STOCK", "ok"]
    assert package_balance(client, pkg["package_number"])["quantity"] == "3"


def test_013_mobile_receipt_visible_on_pc_same_lot(client):
    """013: 모바일(창고관리자) 입고 후 PC(조회자) 조회 → 동일 LOT/잔량, 중복 없음."""
    res = receive_material(client, "RM-000003", "30", 2)
    lot = res["lots"][0]
    pc = auth(client, "viewer")
    lots = data(client.get(f"{API}/inventory/lots", params={"q": lot["lot_number"]}, headers=pc))["items"]
    assert len(lots) == 1 and lots[0]["quantity"] == "30" and lots[0]["package_count"] == 2


def test_019_print_failure_keeps_receipt_and_same_qr_reprint(client):
    """019: 입고 성공 후 인쇄 실패 → 입고 유지, 동일 QR 재출력."""
    from app.core.db import SessionLocal
    from app.models import Facility, Organization, PrinterSetting
    from sqlalchemy import select

    with SessionLocal() as db:
        org = db.execute(select(Organization)).scalars().first()
        f1 = db.execute(select(Facility).where(Facility.code == "F1")).scalar_one()
        p = PrinterSetting(organization_id=org.id, facility_id=f1.id, name="고장 프린터", printer_type="NETWORK_ZPL",
                           host="127.0.0.1", port=1, is_default=False)
        db.add(p)
        db.commit()
        pid = str(p.id)
    res = receive_material(client, "RM-000002", "8", 1)
    qr_id = res["lots"][0]["packages"][0]["qr_code_id"]
    wh = auth(client, "wh")
    out = data(client.post(f"{API}/qr/{qr_id}/print", json={"printer_id": pid}, headers=wh))
    assert out["status"] == "FAILED" and out["pdf_url"]
    assert lot_quantity(client, res["lots"][0]["lot_number"]) == "8"  # 입고 유지
    err(client.post(f"{API}/qr/{qr_id}/reprint", json={}, headers=wh), "REASON_REQUIRED", 422)
    rp = data(client.post(f"{API}/qr/{qr_id}/reprint", json={"reason": "프린터 오류 재출력"}, headers=wh))
    assert rp["qr_code_id"] == qr_id
    logs = data(client.get(f"{API}/qr/{qr_id}/print-logs", headers=wh))
    assert [l["print_type"] for l in logs] == ["PRINT", "REPRINT"] and logs[1]["reason"] == "프린터 오류 재출력"


def test_adjustment_approval_and_stock_count(client):
    res = receive_material(client, "RM-000003", "12", 1)
    pkg = res["lots"][0]["packages"][0]
    wh, adm, mgr = auth(client, "wh"), auth(client, "admin"), auth(client, "manager")
    adj = data(client.post(f"{API}/inventory/adjustment", json={"balance_id": pkg["balance_id"], "direction": "OUT", "quantity": "2",
                                                                "reason": "파손"}, headers=wh))
    assert package_balance(client, pkg["package_number"])["quantity"] == "12"  # 승인 전 미반영
    err(client.post(f"{API}/inventory/adjustments/{adj['id']}/approve", headers={**wh, **idem()}), "FORBIDDEN", 403)
    data(client.post(f"{API}/inventory/adjustments/{adj['id']}/approve", headers={**mgr, **idem()}))
    assert package_balance(client, pkg["package_number"])["quantity"] == "10"
    sc = data(client.post(f"{API}/inventory/stock-count", json={"lines": [{"balance_id": pkg["balance_id"], "counted_quantity": "9.5"}]},
                          headers=wh))
    assert sc["status"] == "ADJUSTMENT_REQUESTED" and sc["lines"][0]["difference"] == "-0.5"
    v = data(client.post(f"{API}/inventory/verify-ledger", headers=adm))
    assert v["consistent"] is True


def test_receipt_reverse_unused(client):
    res = receive_material(client, "RM-000003", "6", 2)
    rid = res["receipt_id"]
    qm = auth(client, "qm")
    data(client.post(f"{API}/receipts/{rid}/reverse", json={"reason": "오입고"}, headers={**qm, **idem()}))
    assert lot_quantity(client, res["lots"][0]["lot_number"]) == "0"
    txs = data(client.get(f"{API}/inventory/transactions", params={"lot_number": res["lots"][0]["lot_number"],
                                                                   "transaction_type": "REVERSAL"}, headers=qm))
    assert txs["total"] == 2  # 원본 유지 + 역거래 추가


def test_expiring_and_low_stock(client):
    receive_material(client, "RM-000002", "3", 1, expiry_days=20)
    adm = auth(client, "admin")
    ex = data(client.get(f"{API}/inventory/expiring", params={"days": 30}, headers=adm))
    assert any(r["alert_level"] == "D-30" for r in ex)
    low = data(client.get(f"{API}/inventory/low-stock", headers=adm))
    assert isinstance(low, list)
