"""재고 원장 서비스 — 모든 재고 수량 변경의 유일한 진입점.

규칙
- 잔량(inventory_balances) 변경과 원장(inventory_transactions) 기록은 호출자의 DB 트랜잭션 안에서 함께 수행된다.
- 잔량 행은 SELECT ... FOR UPDATE 로 잠근다 (동시 차감 시 한 건만 성공).
- 음수 잔량은 서비스 검증 + DB CHECK 제약으로 이중 차단한다.
- 원장은 불변이며 정정은 REVERSAL(역거래) 또는 조정으로만 한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import and_, select, text, update
from sqlalchemy.orm import Session

from app.core.errors import BusinessRule, NotFound
from app.core.numbering import next_number, today_seoul
from app.models import InventoryBalance, InventoryReservation, InventoryTransaction, Warehouse

ZERO = Decimal("0")
AVAILABLE_QUALITY = ("PASSED", "RELEASED")
IN_TYPES = {
    "OPENING", "RECEIPT", "PRODUCTION_RETURN", "ADJUSTMENT_IN", "TRANSFER_IN", "WIP_RECEIPT", "FG_RECEIPT", "FG_RETURN",
}
OUT_TYPES = {
    "PRODUCTION_ISSUE", "SUPPLIER_RETURN", "DISPOSAL", "ADJUSTMENT_OUT", "TRANSFER_OUT", "SAMPLE_USE",
    "GENERAL_USE", "WIP_ISSUE", "FG_SHIPMENT",
}


@dataclass
class StockKey:
    item_type: str
    lot_node_id: uuid.UUID
    package_node_id: uuid.UUID | None
    warehouse_id: uuid.UUID
    storage_location_id: uuid.UUID | None
    unit_id: uuid.UUID
    lot_number: str
    quality_status: str
    material_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    package_number: str | None = None
    expiry_date: date | None = None
    material_lot_id: uuid.UUID | None = None
    package_id: uuid.UUID | None = None


@dataclass
class MovementResult:
    transaction: InventoryTransaction
    balance: InventoryBalance


@dataclass
class Ctx:
    """원장 기록에 필요한 공통 문맥."""

    organization_id: uuid.UUID
    facility_id: uuid.UUID | None
    user_id: uuid.UUID
    group_id: uuid.UUID = field(default_factory=uuid.uuid4)
    reference_type: str | None = None
    reference_id: uuid.UUID | None = None
    reason: str | None = None


def q(v: Any) -> Decimal:
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return d.quantize(Decimal("0.000001"))


def lock_balance(db: Session, balance_id: uuid.UUID, org_id: uuid.UUID) -> InventoryBalance:
    bal = db.execute(
        select(InventoryBalance)
        .where(InventoryBalance.id == balance_id, InventoryBalance.organization_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not bal:
        raise NotFound("재고")
    return bal


def lock_balances(db: Session, ids: Iterable[uuid.UUID], org_id: uuid.UUID) -> dict[uuid.UUID, InventoryBalance]:
    """교착 방지를 위해 id 정렬 순서로 잠근다."""
    ordered = sorted(set(ids), key=str)
    out: dict[uuid.UUID, InventoryBalance] = {}
    for bid in ordered:
        out[bid] = lock_balance(db, bid, org_id)
    return out


def _find_balance_for_update(db: Session, ctx: Ctx, key: StockKey) -> InventoryBalance | None:
    cond = [
        InventoryBalance.lot_node_id == key.lot_node_id,
        InventoryBalance.warehouse_id == key.warehouse_id,
        InventoryBalance.package_node_id.is_(None) if key.package_node_id is None else InventoryBalance.package_node_id == key.package_node_id,
        InventoryBalance.storage_location_id.is_(None) if key.storage_location_id is None else InventoryBalance.storage_location_id == key.storage_location_id,
        InventoryBalance.organization_id == ctx.organization_id,
    ]
    return db.execute(
        select(InventoryBalance).where(and_(*cond)).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _get_or_create_balance(db: Session, ctx: Ctx, key: StockKey) -> InventoryBalance:
    bal = _find_balance_for_update(db, ctx, key)
    if bal:
        return bal
    db.execute(
        text(
            """
            INSERT INTO inventory_balances (id, organization_id, facility_id, item_type, material_id, product_id,
                lot_node_id, package_node_id, warehouse_id, storage_location_id, quantity, reserved_quantity, unit_id,
                lot_number, package_number, quality_status, expiry_date, version)
            VALUES (gen_random_uuid(), :org, :fac, :it, :mid, :pid, :lot, :pkg, :wh, :loc, 0, 0, :unit,
                :lotno, :pkgno, :qs, :exp, 1)
            ON CONFLICT ON CONSTRAINT uq_inventory_balances_stock_key DO NOTHING
            """
        ),
        {
            "org": ctx.organization_id, "fac": ctx.facility_id, "it": key.item_type, "mid": key.material_id,
            "pid": key.product_id, "lot": key.lot_node_id, "pkg": key.package_node_id, "wh": key.warehouse_id,
            "loc": key.storage_location_id, "unit": key.unit_id, "lotno": key.lot_number, "pkgno": key.package_number,
            "qs": key.quality_status, "exp": key.expiry_date,
        },
    )
    bal = _find_balance_for_update(db, ctx, key)
    assert bal is not None
    return bal


def _write_tx(
    db: Session, ctx: Ctx, bal: InventoryBalance, tx_type: str, direction: str, qty: Decimal,
    before: Decimal, after: Decimal, material_lot_id: uuid.UUID | None, package_id: uuid.UUID | None,
    idempotency_key: str | None, reversal_of_id: uuid.UUID | None, reason: str | None,
) -> InventoryTransaction:
    now = datetime.now(timezone.utc)
    tx = InventoryTransaction(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        facility_id=bal.facility_id or ctx.facility_id,
        transaction_number=next_number(db, ctx.organization_id, "TX", today_seoul(), width=7),
        transaction_type=tx_type,
        item_type=bal.item_type,
        material_id=bal.material_id,
        product_id=bal.product_id,
        balance_id=bal.id,
        trace_node_id=bal.package_node_id or bal.lot_node_id,
        lot_node_id=bal.lot_node_id,
        material_lot_id=material_lot_id,
        package_id=package_id,
        warehouse_id=bal.warehouse_id,
        storage_location_id=bal.storage_location_id,
        quality_status=bal.quality_status,
        quantity=qty,
        base_unit_id=bal.unit_id,
        direction=direction,
        balance_before=before,
        balance_after=after,
        reference_type=ctx.reference_type,
        reference_id=ctx.reference_id,
        transaction_group_id=ctx.group_id,
        reversal_of_id=reversal_of_id,
        idempotency_key=idempotency_key,
        performed_by=ctx.user_id,
        performed_at=now,
        reason=reason or ctx.reason,
        created_at=now,
    )
    db.add(tx)
    bal.last_movement_at = now
    bal.version = (bal.version or 0) + 1
    db.flush()
    return tx


def receive(
    db: Session, ctx: Ctx, key: StockKey, qty: Any, tx_type: str = "RECEIPT",
    idempotency_key: str | None = None, reason: str | None = None,
) -> MovementResult:
    if tx_type not in IN_TYPES:
        raise ValueError(f"{tx_type} is not an IN transaction type")
    qty = q(qty)
    if qty <= ZERO:
        raise BusinessRule("INVALID_QUANTITY", "수량은 0보다 커야 합니다.")
    bal = _get_or_create_balance(db, ctx, key)
    if bal.unit_id != key.unit_id:
        raise BusinessRule("UNIT_MISMATCH", "재고 단위가 일치하지 않습니다.")
    before = q(bal.quantity)
    after = before + qty
    bal.quantity = after
    if bal.quality_status != key.quality_status and before == ZERO:
        bal.quality_status = key.quality_status
    tx = _write_tx(db, ctx, bal, tx_type, "IN", qty, before, after, key.material_lot_id, key.package_id,
                   idempotency_key, None, reason)
    return MovementResult(tx, bal)


def issue(
    db: Session, ctx: Ctx, balance_id: uuid.UUID, qty: Any, tx_type: str,
    *, allow_reserved_for_order: uuid.UUID | None = None, require_available_quality: bool = False,
    material_lot_id: uuid.UUID | None = None, package_id: uuid.UUID | None = None,
    idempotency_key: str | None = None, reason: str | None = None, ignore_reservations: bool = False,
) -> MovementResult:
    if tx_type not in OUT_TYPES:
        raise ValueError(f"{tx_type} is not an OUT transaction type")
    qty = q(qty)
    if qty <= ZERO:
        raise BusinessRule("INVALID_QUANTITY", "수량은 0보다 커야 합니다.")
    bal = lock_balance(db, balance_id, ctx.organization_id)
    if require_available_quality and bal.quality_status not in AVAILABLE_QUALITY:
        raise BusinessRule("QUALITY_NOT_RELEASED", "품질 승인되지 않은 재고는 사용할 수 없습니다.",
                           {"quality_status": bal.quality_status})
    before = q(bal.quantity)
    reserved = q(bal.reserved_quantity)
    own_reserved = ZERO
    if allow_reserved_for_order is not None and reserved > ZERO:
        own_reserved = _active_reserved_for_order(db, bal.id, allow_reserved_for_order)
    usable = before if ignore_reservations else before - reserved + own_reserved
    if qty > usable:
        raise BusinessRule(
            "INSUFFICIENT_STOCK",
            "사용 가능한 재고가 부족합니다.",
            {"requested": str(qty), "available": str(max(usable, ZERO)), "on_hand": str(before), "lot_number": bal.lot_number,
             "package_number": bal.package_number},
        )
    after = before - qty
    # 예약 소진: 이 지시의 예약분에서 먼저 차감
    if own_reserved > ZERO:
        _consume_reservation(db, bal, allow_reserved_for_order, min(qty, own_reserved))
    if ignore_reservations and bal.reserved_quantity > after:
        _shrink_reservations(db, bal, bal.reserved_quantity - after)
    bal.quantity = after
    tx = _write_tx(db, ctx, bal, tx_type, "OUT", qty, before, after, material_lot_id, package_id,
                   idempotency_key, None, reason)
    return MovementResult(tx, bal)


def transfer(
    db: Session, ctx: Ctx, balance_id: uuid.UUID, qty: Any, to_warehouse_id: uuid.UUID,
    to_location_id: uuid.UUID | None, *, new_quality_status: str | None = None, reason: str | None = None,
    ignore_reservations: bool = False,
) -> tuple[MovementResult, MovementResult]:
    src = lock_balance(db, balance_id, ctx.organization_id)
    if src.warehouse_id == to_warehouse_id and src.storage_location_id == to_location_id:
        raise BusinessRule("SAME_LOCATION", "출발 위치와 도착 위치가 같습니다.")
    out = issue(db, ctx, balance_id, qty, "TRANSFER_OUT", reason=reason, ignore_reservations=ignore_reservations)
    key = StockKey(
        item_type=src.item_type, lot_node_id=src.lot_node_id, package_node_id=src.package_node_id,
        warehouse_id=to_warehouse_id, storage_location_id=to_location_id, unit_id=src.unit_id,
        lot_number=src.lot_number, package_number=src.package_number,
        quality_status=new_quality_status or src.quality_status, material_id=src.material_id,
        product_id=src.product_id, expiry_date=src.expiry_date,
    )
    inn = receive(db, ctx, key, qty, "TRANSFER_IN", reason=reason)
    return out, inn


def reverse_transaction(db: Session, ctx: Ctx, tx_id: uuid.UUID, reason: str) -> MovementResult:
    """원 거래와 반대 방향의 REVERSAL 거래를 추가한다 (원본 불변)."""
    orig = db.get(InventoryTransaction, tx_id)
    if not orig or orig.organization_id != ctx.organization_id:
        raise NotFound("원장 거래")
    if orig.transaction_type == "REVERSAL":
        raise BusinessRule("CANNOT_REVERSE_REVERSAL", "역거래는 다시 역처리할 수 없습니다.")
    exists = db.execute(select(InventoryTransaction.id).where(InventoryTransaction.reversal_of_id == orig.id)).first()
    if exists:
        raise BusinessRule("ALREADY_REVERSED", "이미 역처리된 거래입니다.")
    bal = lock_balance(db, orig.balance_id, ctx.organization_id)
    before = q(bal.quantity)
    qty = q(orig.quantity)
    if orig.direction == "IN":
        if qty > before - q(bal.reserved_quantity):
            raise BusinessRule("INSUFFICIENT_STOCK", "이미 사용된 수량이 있어 역처리할 수 없습니다.",
                               {"requested": str(qty), "available": str(before - q(bal.reserved_quantity))})
        after, direction = before - qty, "OUT"
    else:
        after, direction = before + qty, "IN"
    bal.quantity = after
    tx = _write_tx(db, ctx, bal, "REVERSAL", direction, qty, before, after, orig.material_lot_id, orig.package_id,
                   None, orig.id, reason)
    return MovementResult(tx, bal)


# ---------------------------------------------------------------- reservations

def _active_reserved_for_order(db: Session, balance_id: uuid.UUID, order_id: uuid.UUID) -> Decimal:
    rows = db.execute(
        select(InventoryReservation).where(
            InventoryReservation.balance_id == balance_id,
            InventoryReservation.production_order_id == order_id,
            InventoryReservation.status == "ACTIVE",
        )
    ).scalars()
    return sum((q(r.quantity) - q(r.consumed_quantity) for r in rows), ZERO)


def _consume_reservation(db: Session, bal: InventoryBalance, order_id: uuid.UUID, qty: Decimal) -> None:
    remaining = qty
    rows = db.execute(
        select(InventoryReservation).where(
            InventoryReservation.balance_id == bal.id,
            InventoryReservation.production_order_id == order_id,
            InventoryReservation.status == "ACTIVE",
        ).with_for_update()
    ).scalars().all()
    for r in rows:
        if remaining <= ZERO:
            break
        open_qty = q(r.quantity) - q(r.consumed_quantity)
        take = min(open_qty, remaining)
        r.consumed_quantity = q(r.consumed_quantity) + take
        if q(r.consumed_quantity) >= q(r.quantity):
            r.status = "CONSUMED"
        remaining -= take
    bal.reserved_quantity = max(q(bal.reserved_quantity) - (qty - remaining), ZERO)


def _shrink_reservations(db: Session, bal: InventoryBalance, excess: Decimal) -> None:
    rows = db.execute(
        select(InventoryReservation).where(InventoryReservation.balance_id == bal.id, InventoryReservation.status == "ACTIVE")
        .order_by(InventoryReservation.created_at.desc()).with_for_update()
    ).scalars().all()
    reduced = ZERO
    for r in rows:
        if excess <= ZERO:
            break
        open_qty = q(r.quantity) - q(r.consumed_quantity)
        take = min(open_qty, excess)
        r.quantity = q(r.quantity) - take
        if q(r.quantity) <= q(r.consumed_quantity):
            r.status = "RELEASED"
        excess -= take
        reduced += take
    bal.reserved_quantity = max(q(bal.reserved_quantity) - reduced, ZERO)


def reserve(db: Session, ctx: Ctx, balance_id: uuid.UUID, qty: Any, *, order_id: uuid.UUID | None = None,
            order_item_id: uuid.UUID | None = None, reason: str | None = None) -> InventoryReservation:
    qty = q(qty)
    bal = lock_balance(db, balance_id, ctx.organization_id)
    if bal.quality_status not in AVAILABLE_QUALITY:
        raise BusinessRule("QUALITY_NOT_RELEASED", "품질 승인되지 않은 재고는 예약할 수 없습니다.")
    avail = q(bal.quantity) - q(bal.reserved_quantity)
    if qty <= ZERO or qty > avail:
        raise BusinessRule("INSUFFICIENT_STOCK", "예약 가능한 재고가 부족합니다.", {"requested": str(qty), "available": str(avail)})
    bal.reserved_quantity = q(bal.reserved_quantity) + qty
    r = InventoryReservation(
        organization_id=ctx.organization_id, facility_id=bal.facility_id, balance_id=bal.id,
        production_order_id=order_id, production_order_item_id=order_item_id, quantity=qty,
        reason=reason, created_by=ctx.user_id,
    )
    db.add(r)
    db.flush()
    return r


def release_reservations(db: Session, ctx: Ctx, order_id: uuid.UUID) -> Decimal:
    rows = db.execute(
        select(InventoryReservation).where(
            InventoryReservation.production_order_id == order_id, InventoryReservation.status == "ACTIVE"
        ).order_by(InventoryReservation.balance_id)
    ).scalars().all()
    total = ZERO
    for r in rows:
        bal = lock_balance(db, r.balance_id, ctx.organization_id)
        open_qty = q(r.quantity) - q(r.consumed_quantity)
        bal.reserved_quantity = max(q(bal.reserved_quantity) - open_qty, ZERO)
        r.status = "RELEASED"
        total += open_qty
    return total


# ---------------------------------------------------------------- lot status sync

def set_lot_quality(db: Session, org_id: uuid.UUID, lot_node_id: uuid.UUID, status: str,
                    expiry_date: date | None | object = ...) -> int:
    values: dict[str, Any] = {"quality_status": status}
    if expiry_date is not ...:
        values["expiry_date"] = expiry_date
    res = db.execute(
        update(InventoryBalance)
        .where(InventoryBalance.lot_node_id == lot_node_id, InventoryBalance.organization_id == org_id)
        .values(**values)
    )
    return res.rowcount or 0


# ---------------------------------------------------------------- queries

def quarantine_warehouse_ids(db: Session, org_id: uuid.UUID) -> list[uuid.UUID]:
    return list(db.execute(select(Warehouse.id).where(Warehouse.organization_id == org_id, Warehouse.warehouse_type == "QUARANTINE")).scalars())


def available_balances(db: Session, org_id: uuid.UUID, facility_id: uuid.UUID | None, *, material_id: uuid.UUID | None = None,
                       product_id: uuid.UUID | None = None, on: date | None = None, fefo: bool = True) -> list[InventoryBalance]:
    """사용 가능 재고(합격, 기한 내, 격리창고 제외, 잔량>예약) — FEFO(기한 빠른 순) 또는 FIFO(LOT 번호 순)."""
    on = on or today_seoul()
    qw = quarantine_warehouse_ids(db, org_id)
    stmt = select(InventoryBalance).where(
        InventoryBalance.organization_id == org_id,
        InventoryBalance.quality_status.in_(AVAILABLE_QUALITY),
        InventoryBalance.quantity > InventoryBalance.reserved_quantity,
        (InventoryBalance.expiry_date.is_(None)) | (InventoryBalance.expiry_date >= on),
    )
    if facility_id:
        stmt = stmt.where(InventoryBalance.facility_id == facility_id)
    if qw:
        stmt = stmt.where(InventoryBalance.warehouse_id.not_in(qw))
    if material_id:
        stmt = stmt.where(InventoryBalance.material_id == material_id)
    if product_id:
        stmt = stmt.where(InventoryBalance.product_id == product_id)
    order = [InventoryBalance.expiry_date.asc().nulls_last()] if fefo else []
    order += [InventoryBalance.lot_number.asc(), InventoryBalance.package_number.asc().nulls_last()]
    return list(db.execute(stmt.order_by(*order)).scalars())


def available_quantity(db: Session, org_id: uuid.UUID, facility_id: uuid.UUID | None, *, material_id: uuid.UUID | None = None,
                       product_id: uuid.UUID | None = None) -> Decimal:
    return sum((q(b.quantity) - q(b.reserved_quantity) for b in available_balances(
        db, org_id, facility_id, material_id=material_id, product_id=product_id)), ZERO)


def lot_total(db: Session, org_id: uuid.UUID, lot_node_id: uuid.UUID) -> Decimal:
    rows = db.execute(select(InventoryBalance.quantity).where(
        InventoryBalance.organization_id == org_id, InventoryBalance.lot_node_id == lot_node_id)).scalars()
    return sum((q(x) for x in rows), ZERO)
