from __future__ import annotations

import io
import uuid
from datetime import date, timedelta
from typing import Any

from tests.conftest import auth, idem

API = "/api/v1"


def png_bytes(text: str = "doc") -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).text((10, 10), f"{text} {uuid.uuid4()}", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def data(r) -> Any:
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True, body
    return body["data"]


def err(r, code: str, status: int | None = None) -> dict:
    body = r.json()
    assert body["success"] is False, body
    assert body["error_code"] == code, body
    if status:
        assert r.status_code == status, r.text
    return body


def material(client, code: str) -> dict:
    items = data(client.get(f"{API}/materials", params={"q": code}, headers=auth(client, "admin")))["items"]
    return next(m for m in items if m["material_code"] == code)


def product(client, code: str = "FP-000001") -> dict:
    items = data(client.get(f"{API}/products", params={"q": code}, headers=auth(client, "admin")))["items"]
    return next(p for p in items if p["product_code"] == code)


def supplier(client, code: str = "SUP-001") -> dict:
    items = data(client.get(f"{API}/suppliers", params={"q": code}, headers=auth(client, "admin")))["items"]
    return next(s for s in items if s["code"] == code)


def customer(client, code: str = "CUS-001") -> dict:
    items = data(client.get(f"{API}/customers", params={"q": code}, headers=auth(client, "admin")))["items"]
    return next(s for s in items if s["code"] == code)


def warehouse(client, code: str) -> dict:
    items = data(client.get(f"{API}/warehouses", params={"q": code, "size": 100}, headers=auth(client, "admin")))["items"]
    return next(w for w in items if w["code"] == code)


def receive_material(client, material_code: str, qty: str, packages: int = 1, *, expiry_days: int = 365,
                     approve: bool = True, supplier_lot: str | None = None, manufactured: date | None = None) -> dict:
    """입고 등록 → (COA) → 검수 → 승인 → 확정. 확정 결과(lots/packages) 반환."""
    m = material(client, material_code)
    wh, qm = auth(client, "wh"), auth(client, "qm")
    item = {"material_id": m["id"], "quantity": qty, "package_count": packages,
            "supplier_lot_number": supplier_lot or f"SL-{uuid.uuid4().hex[:6]}"}
    if m["expiry_tracking_required"]:
        item.update({"manufactured_date": str(manufactured or date.today() - timedelta(days=5)),
                     "expiry_date": str(date.today() + timedelta(days=expiry_days)), "expiry_type": "CONSUME_BY"})
    r = data(client.post(f"{API}/receipts", json={"supplier_id": supplier(client)["id"], "items": [item]}, headers={**wh, **idem()}))
    rid = r["id"]
    if m["coa_required"]:
        data(client.post(f"{API}/receipts/{rid}/documents", files={"files": ("coa.png", png_bytes("COA"), "image/png")},
                         data={"document_type": "COA", "run_ocr": "false"}, headers=wh))
    if m["inspection_required"]:
        data(client.post(f"{API}/receipts/{rid}/submit-inspection", headers=wh))
        data(client.post(f"{API}/receipts/{rid}/inspection", json=inspection_pass(), headers=qm))
        if approve:
            data(client.post(f"{API}/receipts/{rid}/approve", json={}, headers=qm))
    return data(client.post(f"{API}/receipts/{rid}/post", headers={**wh, **idem()}))


RECEIPT_ITEMS = ["APPEARANCE", "PACKAGE_DAMAGE", "CONTAMINATION", "FOREIGN_MATTER", "ODOR", "TEMPERATURE", "TRANSPORT", "EXPIRY",
                 "LABELING", "ORIGIN", "LOT", "QUANTITY"]


def inspection_pass(temp: str = "20") -> dict:
    res = [{"item_code": c, "result": "PASS"} for c in RECEIPT_ITEMS]
    for r in res:
        if r["item_code"] == "TEMPERATURE":
            r["measured_value"] = temp
    return {"source": "FIELD", "overall_result": "PASS", "results": res}


def package_balance(client, package_number: str) -> dict:
    rows = data(client.get(f"{API}/inventory/packages", params={"q": package_number, "include_zero": True},
                           headers=auth(client, "admin")))["items"]
    return next(r for r in rows if r["package_number"] == package_number)


def lot_quantity(client, lot_number: str) -> str:
    rows = data(client.get(f"{API}/inventory/packages", params={"lot_number": lot_number, "include_zero": True, "size": 500},
                           headers=auth(client, "admin")))["items"]
    from decimal import Decimal

    from app.repositories.common import ser_value

    return ser_value(sum((Decimal(r["quantity"]) for r in rows), Decimal(0)))
