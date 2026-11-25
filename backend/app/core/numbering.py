"""원자적 번호 발급: PREFIX-YYYYMMDD-000001.

INSERT ... ON CONFLICT DO UPDATE ... RETURNING 은 행 잠금을 수반하여
동시 요청에서도 중복 없이 증가한다. 번호 컬럼에는 별도 UNIQUE 제약이 있다.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

SEOUL = ZoneInfo(settings.TIMEZONE)


def today_seoul() -> date:
    return datetime.now(SEOUL).date()


def next_number(db: Session, org_id: uuid.UUID, prefix: str, on: date | None = None, width: int = 6) -> str:
    d = on or today_seoul()
    value = db.execute(
        text(
            """
            INSERT INTO number_sequences (organization_id, prefix, seq_date, last_value)
            VALUES (:org, :prefix, :d, 1)
            ON CONFLICT (organization_id, prefix, seq_date)
            DO UPDATE SET last_value = number_sequences.last_value + 1
            RETURNING last_value
            """
        ),
        {"org": org_id, "prefix": prefix, "d": d},
    ).scalar_one()
    return f"{prefix}-{d.strftime('%Y%m%d')}-{value:0{width}d}"
