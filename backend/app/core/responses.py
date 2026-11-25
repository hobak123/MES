from __future__ import annotations

import math
from typing import Any

from app.core.context import get_request_id


def ok(data: Any = None, message: str = "정상 처리되었습니다.") -> dict[str, Any]:
    return {"success": True, "data": data, "message": message, "request_id": get_request_id()}


def paged(items: list[Any], total: int, page: int, size: int, message: str = "정상 처리되었습니다.") -> dict[str, Any]:
    return ok(
        {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "pages": math.ceil(total / size) if size else 0,
        },
        message,
    )
