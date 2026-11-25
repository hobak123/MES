from __future__ import annotations

import uuid
from datetime import date

from tests.conftest import auth, idem
from tests.helpers import API, data, material


def units(client) -> dict[str, str]:
    return {u["code"]: u["id"] for u in data(client.get(f"{API}/units", headers=auth(client, "admin")))}


def processes(client) -> dict[str, dict]:
    return {p["code"]: p for p in data(client.get(f"{API}/processes", headers=auth(client, "admin")))}


def setup_product(client, name: str, ratios: dict[str, str], packaging: bool = True, steps: list[tuple[int, str]] | None = None,
                  tolerance: str = "1") -> dict:
    """제품 + 승인 BOM + 승인 라우팅 생성."""
    u = units(client)
    adm, pm = auth(client, "admin"), auth(client, "pm")
    p = data(client.post(f"{API}/products", json={
        "product_name": name, "content_quantity": "1", "content_unit_id": u["kg"], "base_unit_id": u["EA"],
        "standard_batch_size": "100", "shelf_life_days": 365, "standard_yield_pct": "98"}, headers=adm))
    items = [{"item_type": "FORMULA", "material_id": material(client, c)["id"], "quantity_basis": "RATIO", "ratio_percent": r,
              "unit_id": u["kg"], "tolerance_pct": tolerance, "weighing_group": "G1"} for c, r in ratios.items()]
    if packaging:
        items.append({"item_type": "PACKAGING", "material_id": material(client, "PM-000001")["id"], "quantity_basis": "PER_UNIT",
                      "standard_quantity": "1", "unit_id": u["EA"], "tolerance_pct": "0"})
    b = data(client.post(f"{API}/boms", json={"product_id": p["id"], "version": {"base_quantity": "100", "base_unit_id": u["kg"],
                                                                                 "base_fg_quantity": "98", "items": items}}, headers=pm))
    data(client.post(f"{API}/boms/{b['id']}/approve", json={"version_id": b["versions"][0]["id"]}, headers=adm))
    pr = processes(client)
    steps = steps or [(10, "MIX"), (20, "PACK")]
    r = data(client.post(f"{API}/routings", json={"product_id": p["id"], "steps": [
        {"process_definition_id": pr[code]["id"], "sequence": seq} for seq, code in steps]}, headers=pm))
    data(client.post(f"{API}/routings/{r['id']}/approve", json={"version_id": r["versions"][-1]["id"]}, headers=pm))
    return p


def start_order(client, product_id: str, planned: str, batches: int = 1, target: str | None = None) -> dict:
    pm = auth(client, "pm")
    body = {"production_date": str(date.today()), "product_id": product_id, "planned_quantity": planned, "batch_count": batches}
    if target:
        body["target_fg_quantity"] = target
    o = data(client.post(f"{API}/production-orders", json=body, headers={**pm, **idem()}))
    data(client.post(f"{API}/production-orders/{o['id']}/approve", json={}, headers=pm))
    return data(client.post(f"{API}/production-orders/{o['id']}/start", headers=pm))


def weigh(client, order: dict, payload: str, qty: str, batch_idx: int = 0, user: str = "worker", **extra):
    return client.post(f"{API}/weighing/records", json={
        "production_order_id": order["id"], "production_batch_id": order["batches"][batch_idx]["id"], "raw": payload,
        "actual_quantity": qty, **extra}, headers={**auth(client, user), **idem()})


def wb_of(client, order: dict) -> list[dict]:
    return data(client.get(f"{API}/weighing/batches", params={"production_order_id": order["id"]}, headers=auth(client, "pm")))


def run_step(client, order: dict, seq: int, inputs: list[tuple[str, str]], complete: dict, params: list[dict] | None = None,
             packaging: list[dict] | None = None, batch_idx: int | None = 0, user: str = "worker", **create_extra) -> dict:
    h = auth(client, user)
    body = {"production_order_id": order["id"], "routing_step_sequence": seq, **create_extra}
    if batch_idx is not None:
        body["production_batch_id"] = order["batches"][batch_idx]["id"]
    e = data(client.post(f"{API}/process-executions", json=body, headers=h))
    data(client.post(f"{API}/process-executions/{e['id']}/start", headers=h))
    for raw, q in inputs:
        data(client.post(f"{API}/process-executions/{e['id']}/inputs", json={"raw": raw, "quantity": q}, headers={**h, **idem()}))
    if params:
        data(client.post(f"{API}/process-executions/{e['id']}/parameters", json={"records": params}, headers=h))
    for pk in packaging or []:
        data(client.post(f"{API}/process-executions/{e['id']}/packaging", json=pk, headers={**h, **idem()}))
    return data(client.post(f"{API}/process-executions/{e['id']}/complete", json=complete, headers={**h, **idem()}))


MIX_PARAMS = [{"parameter_code": "MIX_TIME", "value_numeric": "15"}, {"parameter_code": "SPEED", "value_numeric": "120"}]


def new_id() -> str:
    return str(uuid.uuid4())
