from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from fastapi import Query
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFound


def ser_value(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, Decimal):
        # 정규화된 문자열 (불필요한 0 제거) — 클라이언트는 문자열 숫자로 처리
        s = format(v.normalize(), "f")
        return s if s != "-0" else "0"
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, list):
        return [ser_value(x) for x in v]
    if isinstance(v, dict):
        return {k: ser_value(x) for k, x in v.items()}
    return v


def ser(obj: Any, exclude: Sequence[str] = (), **extra: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    out = {c.key: ser_value(getattr(obj, c.key)) for c in obj.__table__.columns if c.key not in exclude}
    out.update({k: ser_value(v) for k, v in extra.items()})
    return out


class PageParams:
    def __init__(
        self,
        page: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=500),
        sort: str | None = Query(None, description="정렬 필드, '-' 접두사는 내림차순"),
        q: str | None = Query(None, description="검색어"),
    ):
        self.page, self.size, self.sort, self.q = page, size, sort, q


def paginate(db: Session, stmt: Select, params: PageParams, model: Any, search_cols: Sequence[Any] = (),
             default_sort: Any = None) -> tuple[list[Any], int]:
    if params.q and search_cols:
        like = f"%{params.q.strip()}%"
        stmt = stmt.where(or_(*[c.ilike(like) for c in search_cols]))
    total = db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
    if params.sort:
        desc = params.sort.startswith("-")
        name = params.sort.lstrip("-")
        col = getattr(model, name, None)
        if col is not None and hasattr(col, "asc"):
            stmt = stmt.order_by(col.desc() if desc else col.asc())
    elif default_sort is not None:
        stmt = stmt.order_by(default_sort)
    rows = db.execute(stmt.offset((params.page - 1) * params.size).limit(params.size)).unique().scalars().all()
    return list(rows), total


def get_owned(db: Session, model: Any, id_: uuid.UUID, org_id: uuid.UUID, what: str = "대상", facility_check=None) -> Any:
    obj = db.get(model, id_)
    if obj is None or getattr(obj, "organization_id", org_id) != org_id:
        raise NotFound(what)
    if facility_check is not None and hasattr(obj, "facility_id"):
        facility_check(obj.facility_id)
    return obj
