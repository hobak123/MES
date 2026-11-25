"""LOT 추적 그래프: trace_nodes + trace_events(inputs/outputs).

- 이벤트는 불변. 취소는 reversal_of_id 를 가진 REVERSAL 이벤트로 추가하고, 탐색 시 역처리된 이벤트는 제외한다.
- 정/역방향 탐색은 PostgreSQL Recursive CTE (경로 배열로 순환 차단, 최대 깊이 제한).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import BusinessRule, NotFound
from app.models import TraceEvent, TraceEventInput, TraceEventOutput, TraceNode

MAX_DEPTH = 60

_ACTIVE_EVENT = "NOT EXISTS (SELECT 1 FROM trace_events r WHERE r.reversal_of_id = e.id)"


@dataclass
class NodeRef:
    node_id: uuid.UUID
    quantity: Decimal | None = None
    unit_id: uuid.UUID | None = None


def create_node(db: Session, org_id: uuid.UUID, facility_id: uuid.UUID | None, node_type: str, entity_id: uuid.UUID,
                lot_number: str, label: str | None = None) -> TraceNode:
    existing = db.execute(select(TraceNode).where(TraceNode.node_type == node_type, TraceNode.entity_id == entity_id)).scalar_one_or_none()
    if existing:
        return existing
    node = TraceNode(
        id=uuid.uuid4(), organization_id=org_id, facility_id=facility_id, node_type=node_type, entity_id=entity_id,
        lot_number=lot_number, label=label, created_at=datetime.now(timezone.utc),
    )
    db.add(node)
    db.flush()
    return node


def get_node(db: Session, org_id: uuid.UUID, node_id: uuid.UUID) -> TraceNode:
    n = db.get(TraceNode, node_id)
    if not n or n.organization_id != org_id:
        raise NotFound("추적 노드")
    return n


def find_node_by_lot(db: Session, org_id: uuid.UUID, lot_number: str) -> TraceNode | None:
    return db.execute(
        select(TraceNode).where(TraceNode.organization_id == org_id, TraceNode.lot_number == lot_number)
        .order_by(TraceNode.created_at)
    ).scalars().first()


def _ancestors(db: Session, node_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not node_ids:
        return set()
    rows = db.execute(
        text(
            f"""
            WITH RECURSIVE b(node_id, depth) AS (
                SELECT unnest(CAST(:ids AS uuid[])), 0
                UNION
                SELECT i.trace_node_id, b.depth + 1
                FROM b
                JOIN trace_event_outputs o ON o.trace_node_id = b.node_id
                JOIN trace_events e ON e.id = o.trace_event_id AND {_ACTIVE_EVENT}
                JOIN trace_event_inputs i ON i.trace_event_id = e.id
                WHERE b.depth < {MAX_DEPTH}
            )
            SELECT DISTINCT node_id FROM b
            """
        ),
        {"ids": [str(x) for x in node_ids]},
    ).scalars()
    return set(rows)


def record_event(
    db: Session, *, org_id: uuid.UUID, facility_id: uuid.UUID | None, user_id: uuid.UUID, event_type: str,
    inputs: list[NodeRef], outputs: list[NodeRef], production_order_id: uuid.UUID | None = None,
    production_batch_id: uuid.UUID | None = None, reference_type: str | None = None,
    reference_id: uuid.UUID | None = None, note: str | None = None, reversal_of_id: uuid.UUID | None = None,
) -> TraceEvent:
    in_ids = [r.node_id for r in inputs]
    out_ids = [r.node_id for r in outputs]
    if set(in_ids) & set(out_ids):
        raise BusinessRule("TRACE_CYCLE", "입력과 출력에 동일한 LOT 가 포함될 수 없습니다.")
    if out_ids and in_ids and event_type != "REVERSAL":
        anc = _ancestors(db, in_ids)
        if anc & set(out_ids):
            raise BusinessRule("TRACE_CYCLE", "추적 관계에 순환이 발생합니다. 잘못된 연결입니다.")
    ev = TraceEvent(
        id=uuid.uuid4(), organization_id=org_id, facility_id=facility_id, event_type=event_type,
        production_order_id=production_order_id, production_batch_id=production_batch_id,
        reference_type=reference_type, reference_id=reference_id, performed_by=user_id,
        performed_at=datetime.now(timezone.utc), note=note, reversal_of_id=reversal_of_id,
    )
    db.add(ev)
    db.flush()
    for r in inputs:
        db.add(TraceEventInput(trace_event_id=ev.id, trace_node_id=r.node_id, quantity=r.quantity, unit_id=r.unit_id))
    for r in outputs:
        db.add(TraceEventOutput(trace_event_id=ev.id, trace_node_id=r.node_id, quantity=r.quantity, unit_id=r.unit_id))
    db.flush()
    return ev


def reverse_event(db: Session, *, org_id: uuid.UUID, facility_id: uuid.UUID | None, user_id: uuid.UUID,
                  event_id: uuid.UUID, note: str) -> TraceEvent:
    orig = db.get(TraceEvent, event_id)
    if not orig or orig.organization_id != org_id:
        raise NotFound("추적 이벤트")
    return record_event(
        db, org_id=org_id, facility_id=facility_id, user_id=user_id, event_type="REVERSAL", inputs=[], outputs=[],
        production_order_id=orig.production_order_id, production_batch_id=orig.production_batch_id,
        reference_type=orig.reference_type, reference_id=orig.reference_id, note=note, reversal_of_id=orig.id,
    )


def _walk(db: Session, org_id: uuid.UUID, start: uuid.UUID, direction: str) -> list[dict[str, Any]]:
    if direction == "forward":
        join = """
            JOIN trace_event_inputs a ON a.trace_node_id = w.node_id
            JOIN trace_events e ON e.id = a.trace_event_id AND {active}
            JOIN trace_event_outputs b ON b.trace_event_id = e.id
        """
    else:
        join = """
            JOIN trace_event_outputs a ON a.trace_node_id = w.node_id
            JOIN trace_events e ON e.id = a.trace_event_id AND {active}
            JOIN trace_event_inputs b ON b.trace_event_id = e.id
        """
    sql = f"""
        WITH RECURSIVE w(node_id, depth, path, from_node, event_id, event_type, in_qty, out_qty) AS (
            SELECT CAST(:start AS uuid), 0, ARRAY[CAST(:start AS uuid)], NULL::uuid, NULL::uuid, NULL::varchar,
                   NULL::numeric, NULL::numeric
            UNION ALL
            SELECT b.trace_node_id, w.depth + 1, w.path || b.trace_node_id, w.node_id, e.id, e.event_type,
                   a.quantity, b.quantity
            FROM w
            {join.format(active=_ACTIVE_EVENT)}
            WHERE NOT (b.trace_node_id = ANY(w.path)) AND w.depth < {MAX_DEPTH} AND e.organization_id = :org
        )
        SELECT w.node_id, w.depth, w.from_node, w.event_id, w.event_type, w.in_qty, w.out_qty,
               n.node_type, n.entity_id, n.lot_number, n.label, ev.performed_at, ev.production_order_id
        FROM w JOIN trace_nodes n ON n.id = w.node_id
        LEFT JOIN trace_events ev ON ev.id = w.event_id
        ORDER BY w.depth, n.lot_number
    """
    rows = db.execute(text(sql), {"start": str(start), "org": org_id}).mappings().all()
    return [dict(r) for r in rows]


def forward(db: Session, org_id: uuid.UUID, node_id: uuid.UUID) -> list[dict[str, Any]]:
    return _walk(db, org_id, node_id, "forward")


def backward(db: Session, org_id: uuid.UUID, node_id: uuid.UUID) -> list[dict[str, Any]]:
    return _walk(db, org_id, node_id, "backward")


def graph(db: Session, org_id: uuid.UUID, node_id: uuid.UUID) -> dict[str, Any]:
    """시작 노드 기준 상류(역방향)+하류(정방향) 그래프. 노드/엣지 중복 제거."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for direction in ("backward", "forward"):
        for r in _walk(db, org_id, node_id, direction):
            nid = str(r["node_id"])
            nodes.setdefault(nid, {
                "id": nid, "node_type": r["node_type"], "entity_id": str(r["entity_id"]),
                "lot_number": r["lot_number"], "label": r["label"], "is_root": r["depth"] == 0,
            })
            if r["from_node"] is None:
                continue
            src, dst = (str(r["from_node"]), nid) if direction == "forward" else (nid, str(r["from_node"]))
            # 역방향의 in_qty/out_qty 는 (출력측, 입력측) 이므로 교환
            in_qty, out_qty = (r["in_qty"], r["out_qty"]) if direction == "forward" else (r["out_qty"], r["in_qty"])
            k = (src, dst, str(r["event_id"]))
            edges.setdefault(k, {
                "source": src, "target": dst, "event_id": str(r["event_id"]), "event_type": r["event_type"],
                "input_quantity": str(in_qty) if in_qty is not None else None,
                "output_quantity": str(out_qty) if out_qty is not None else None,
                "performed_at": r["performed_at"].isoformat() if r["performed_at"] else None,
            })
    return {"root": str(node_id), "nodes": list(nodes.values()), "edges": list(edges.values())}
