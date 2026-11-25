"""Idempotency-Key 처리.

- 키 선점은 업무 처리와 **같은 DB 트랜잭션**에서 수행된다. 업무가 실패/롤백되면 키도 롤백되어 재시도가 가능하다.
- 동시에 같은 키로 요청하면 UNIQUE 인덱스에서 두 번째 요청이 대기한 뒤, 첫 요청의 저장된 응답을 그대로 반환한다.
- 같은 키 + 다른 본문 → 409 IDEMPOTENCY_CONFLICT.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.errors import AppError, Conflict
from app.models import IdempotencyKey


@dataclass
class Idem:
    db: Session
    cu: CurrentUser
    key: str | None
    method: str
    path: str
    request_hash: str
    record_id: Any = None

    def begin(self) -> JSONResponse | None:
        if not self.key:
            return None
        if len(self.key) > 200:
            raise AppError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 가 너무 깁니다.")
        row = self.db.execute(
            text(
                """
                INSERT INTO idempotency_keys (id, organization_id, user_id, key, method, path, request_hash, created_at)
                VALUES (gen_random_uuid(), :org, :uid, :key, :m, :p, :h, :now)
                ON CONFLICT (organization_id, key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "org": self.cu.organization_id, "uid": self.cu.id, "key": self.key, "m": self.method,
                "p": self.path, "h": self.request_hash, "now": datetime.now(timezone.utc),
            },
        ).scalar_one_or_none()
        if row is not None:
            self.record_id = row
            return None
        existing = self.db.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.organization_id == self.cu.organization_id, IdempotencyKey.key == self.key
            )
        ).scalar_one()
        if existing.request_hash != self.request_hash or existing.path != self.path:
            raise Conflict(
                "IDEMPOTENCY_CONFLICT",
                "동일한 Idempotency-Key 로 다른 요청이 이미 처리되었습니다.",
                {"key": self.key},
            )
        body = dict(existing.response_body or {})
        body["idempotent_replay"] = True
        return JSONResponse(status_code=existing.response_status or 200, content=body)

    def store(self, response: dict[str, Any], status: int = 200) -> dict[str, Any]:
        if self.record_id is not None:
            rec = self.db.get(IdempotencyKey, self.record_id)
            rec.response_status = status
            rec.response_body = jsonable_encoder(response)
        return response


async def idempotency(
    request: Request,
    db: Session = Depends(get_db),
    cu: CurrentUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None),
) -> Idem:
    body = await request.body()
    try:
        normalized = json.dumps(json.loads(body), sort_keys=True, ensure_ascii=False).encode() if body else b""
    except (ValueError, UnicodeDecodeError):
        normalized = body
    h = hashlib.sha256(request.method.encode() + request.url.path.encode() + b"\n" + normalized).hexdigest()
    return Idem(db=db, cu=cu, key=idempotency_key, method=request.method, path=request.url.path, request_hash=h)
