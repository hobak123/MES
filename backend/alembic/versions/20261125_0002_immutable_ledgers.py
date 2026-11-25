"""immutable ledger triggers

Revision ID: 0002_immutable
Revises: f86a73813168
"""
from alembic import op

revision = "0002_immutable"
down_revision = "f86a73813168"
branch_labels = None
depends_on = None

TABLES = ["inventory_transactions", "audit_logs", "trace_events", "trace_event_inputs", "trace_event_outputs", "qr_print_logs"]


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'immutable record: % on % is not allowed', TG_OP, TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for t in TABLES:
        op.execute(f"CREATE TRIGGER trg_{t}_immutable BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION forbid_mutation();")
    # 거래성 테이블 물리 삭제 금지
    for t in ["material_receipts", "material_lots", "material_packages", "production_orders", "weighing_records",
              "process_executions", "finished_goods_lots", "shipment_headers", "shipment_items", "inventory_balances"]:
        op.execute(f"CREATE TRIGGER trg_{t}_nodelete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION forbid_mutation();")


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_immutable ON {t};")
    for t in ["material_receipts", "material_lots", "material_packages", "production_orders", "weighing_records",
              "process_executions", "finished_goods_lots", "shipment_headers", "shipment_items", "inventory_balances"]:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_nodelete ON {t};")
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation();")
