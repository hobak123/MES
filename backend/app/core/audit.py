from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.context import get_client_ip, get_request_id, get_user_agent
from app.models import AuditLog

SENSITIVE = {"password", "password_hash", "token", "refresh_token", "access_token", "token_hash", "secret", "api_key"}


def _clean(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: ("***" if k.lower() in SENSITIVE else _clean(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (uuid.UUID, Decimal)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def snapshot(obj: Any, fields: list[str] | None = None) -> dict[str, Any]:
    if obj is None:
        return {}
    cols = fields or [c.key for c in obj.__table__.columns]
    return _clean({c: getattr(obj, c, None) for c in cols})


def audit(
    db: Session,
    cu: Any,
    action: str,
    target_type: str | None = None,
    target_id: Any = None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
    organization_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
) -> None:
    db.add(
        AuditLog(
            organization_id=organization_id or (cu.organization_id if cu else None),
            facility_id=getattr(cu, "facility_id", None),
            user_id=user_id or (cu.id if cu else None),
            username=username or (cu.user.username if cu else None),
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            before_data=_clean(before) if before else None,
            after_data=_clean(after) if after else None,
            reason=reason,
            request_id=get_request_id(),
            ip_address=get_client_ip(),
            user_agent=(get_user_agent() or "")[:300] or None,
            created_at=datetime.now(timezone.utc),
        )
    )
