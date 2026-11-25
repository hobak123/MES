"""SQLAlchemy 메타데이터 → docs/database-erd.md (Mermaid ERD) 생성. backend 디렉터리에서 실행."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.models import Base  # noqa: E402

GROUPS = {
    "조직/권한": ["organizations", "facilities", "users", "roles", "permissions", "user_roles", "role_permissions", "user_facilities", "user_sessions"],
    "기준정보": ["materials", "material_categories", "suppliers", "customers", "products", "units", "unit_conversions", "warehouses", "storage_locations", "equipments"],
    "입고/OCR/품질": ["material_receipts", "material_receipt_items", "material_lots", "material_packages", "receipt_documents", "document_links", "ocr_jobs", "ocr_extracted_fields", "inspection_templates", "inspection_template_items", "quality_inspections", "quality_inspection_results"],
    "재고": ["inventory_balances", "inventory_transactions", "inventory_reservations", "stock_transfers", "stock_counts", "stock_adjustments"],
    "BOM": ["bom_headers", "bom_versions", "bom_items"],
    "생산/MES": ["production_orders", "production_order_items", "production_batches", "production_routings", "routing_versions", "routing_steps", "process_definitions", "weighing_batches", "weighing_records", "wip_lots", "process_executions", "process_inputs", "process_outputs", "process_parameter_records", "packaging_consumptions"],
    "완제품/출고": ["finished_goods_lots", "finished_goods_packages", "shipment_headers", "shipment_items", "shipment_returns"],
    "추적/회수": ["trace_nodes", "trace_events", "trace_event_inputs", "trace_event_outputs", "recall_cases", "recall_case_lots", "recall_case_shipments"],
    "공통": ["qr_codes", "qr_print_logs", "printer_settings", "file_attachments", "approval_requests", "notifications", "report_export_logs", "audit_logs", "idempotency_keys", "number_sequences", "system_settings"],
}
SKIP_FK = {"created_by", "updated_by", "organization_id", "facility_id"}


def coltype(c):
    t = c.type.__class__.__name__.lower()
    return {"uuid": "uuid", "numeric": "numeric", "datetime": "timestamptz", "jsonb": "jsonb", "json": "jsonb", "varchar": "varchar", "string": "varchar"}.get(t, t)


def block(tables):
    out = ["```mermaid", "erDiagram"]
    md = Base.metadata.tables
    rels = []
    for t in tables:
        tbl = md[t]
        out.append(f"  {t} {{")
        for c in tbl.columns:
            key = " PK" if c.primary_key else (" FK" if c.foreign_keys else "")
            uq = ""
            out.append(f"    {coltype(c)} {c.name}{key}{uq}")
        out.append("  }")
        for c in tbl.columns:
            if c.name in SKIP_FK:
                continue
            for fk in c.foreign_keys:
                ref = fk.column.table.name
                if ref in tables and ref != t:
                    rels.append(f'  {ref} ||--o{{ {t} : "{c.name}"')
    out += sorted(set(rels))
    out.append("```")
    return "\n".join(out)


def main():
    md = Base.metadata.tables
    lines = ["# 데이터베이스 ERD", "", "> 이 문서는 `infrastructure/scripts/gen_erd.py` 가 SQLAlchemy 모델(`backend/app/models`)에서 자동 생성한다. 모델 변경 후 재생성할 것.", "",
             "## 공통 규칙", "",
             "- PK: UUID (`gen_random_uuid()`/uuid4). 조직 데이터는 `organization_id`, 공장 데이터는 `facility_id` 보유.",
             "- 감사 컬럼: `created_at/created_by/updated_at/updated_by`. 수량 `NUMERIC(20,6)`, 시각 `TIMESTAMPTZ`(UTC 저장, Asia/Seoul 표시), 제조/기한 `DATE`.",
             "- 거래 테이블은 물리 삭제 금지(DB 트리거), 마스터는 `is_active` 비활성화.",
             "- 불변 테이블(UPDATE/DELETE 트리거 차단): inventory_transactions, audit_logs, trace_events, trace_event_inputs, trace_event_outputs, qr_print_logs.",
             "- 음수 재고 금지: `inventory_balances.quantity >= 0`, `reserved_quantity <= quantity`, `inventory_transactions.balance_after >= 0` CHECK.",
             "- 유일성: 조직별 품목코드/LOT/포장번호/거래번호, `(qr_type, entity_id)`, `(organization_id, idempotency_key)`, 재고 키 `(lot_node_id, package_node_id, warehouse_id, storage_location_id) NULLS NOT DISTINCT`.",
             "- 공통 FK(`organization_id`, `facility_id`, `created_by`, `updated_by`)는 다이어그램 가독성을 위해 관계선을 생략한다.", "",
             f"총 테이블 수: **{len(md)}**", ""]
    covered = set()
    for g, ts in GROUPS.items():
        lines += [f"## {g}", "", block(ts), ""]
        covered |= set(ts)
    missing = set(md) - covered
    assert not missing, missing
    lines += ["## 영역 간 핵심 관계", "", "```mermaid", "erDiagram",
              "  material_receipt_items ||--o| material_lots : \"receipt_item_id\"",
              "  material_lots ||--o{ material_packages : \"material_lot_id\"",
              "  material_packages ||--o{ weighing_records : \"material_package_id\"",
              "  weighing_batches ||--o{ weighing_records : \"weighing_batch_id\"",
              "  production_orders ||--o{ weighing_batches : \"production_order_id\"",
              "  process_executions ||--o{ wip_lots : \"process_execution_id\"",
              "  production_orders ||--o{ finished_goods_lots : \"production_order_id\"",
              "  finished_goods_lots ||--o{ shipment_items : \"fg_lot_id\"",
              "  trace_nodes ||--o{ inventory_balances : \"lot_node_id\"",
              "  inventory_balances ||--o{ inventory_transactions : \"balance_id\"",
              "  trace_nodes ||--o{ trace_event_inputs : \"trace_node_id\"",
              "  trace_nodes ||--o{ trace_event_outputs : \"trace_node_id\"",
              "  qr_codes ||--o{ qr_print_logs : \"qr_code_id\"",
              "```", ""]
    Path(__file__).resolve().parents[2].joinpath("docs", "database-erd.md").write_text("\n".join(lines), encoding="utf-8")
    print("written", len(md), "tables")


main()
