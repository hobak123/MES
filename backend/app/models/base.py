from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, MetaData, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

QTY = Numeric(20, 6)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {
        Decimal: QTY,
        datetime: DateTime(timezone=True),
        date: Date,
        uuid.UUID: UUID(as_uuid=True),
        dict[str, Any]: JSONB().with_variant(JSON(), "sqlite"),
        list[Any]: JSONB().with_variant(JSON(), "sqlite"),
    }


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now())

    @declared_attr
    def created_by(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(ForeignKey("users.id", use_alter=True), nullable=True)

    @declared_attr
    def updated_by(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(ForeignKey("users.id", use_alter=True), nullable=True)


class OrgMixin:
    @declared_attr
    def organization_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(ForeignKey("organizations.id"), index=True)


class FacilityMixin:
    @declared_attr
    def facility_id(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(ForeignKey("facilities.id"), index=True, nullable=True)


class VersionMixin:
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


def code_col(length: int = 50, **kw: Any) -> Mapped[str]:
    return mapped_column(String(length), **kw)
