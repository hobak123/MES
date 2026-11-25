from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.db import get_db
from app.core.deps import CurrentUser, require
from app.core.responses import ok
from app.services.qr.resolver import search_nodes
from app.services.traceability import report, service as trace

router = APIRouter(prefix="/trace", tags=["추적"])


@router.get("/forward/{lot_id}")
def forward(lot_id: str, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("trace:read"))):
    node = report.resolve_node(db, cu.organization_id, lot_id)
    return ok(report.forward_report(db, cu.organization_id, node))


@router.get("/backward/{lot_id}")
def backward(lot_id: str, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("trace:read"))):
    node = report.resolve_node(db, cu.organization_id, lot_id)
    return ok(report.backward_report(db, cu.organization_id, node))


@router.get("/graph/{lot_id}")
def graph(lot_id: str, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("trace:read"))):
    node = report.resolve_node(db, cu.organization_id, lot_id)
    g = trace.graph(db, cu.organization_id, node.id)
    from app.models import TraceNode
    import uuid

    for n in g["nodes"]:
        n["detail"] = report.node_detail(db, db.get(TraceNode, uuid.UUID(n["id"])))
    return ok(g)


class SearchIn(BaseModel):
    q: str = Field(min_length=1, max_length=100)
    node_type: str | None = None


@router.post("/search")
def search(body: SearchIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("trace:read"))):
    nodes = search_nodes(db, cu.organization_id, body.q, body.node_type)
    return ok([{"node_id": str(n.id), "node_type": n.node_type, "entity_id": str(n.entity_id), "lot_number": n.lot_number,
                "label": n.label} for n in nodes])
