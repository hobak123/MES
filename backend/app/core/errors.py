from __future__ import annotations

from typing import Any


class AppError(Exception):
    """업무 오류. error_code 는 클라이언트가 분기에 사용하는 안정적 코드."""

    status_code = 400

    def __init__(self, error_code: str, message: str, status_code: int | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}


class NotFound(AppError):
    def __init__(self, what: str = "대상", details: dict[str, Any] | None = None):
        super().__init__("NOT_FOUND", f"{what}을(를) 찾을 수 없습니다.", 404, details)


class Forbidden(AppError):
    def __init__(self, message: str = "권한이 없습니다.", error_code: str = "FORBIDDEN"):
        super().__init__(error_code, message, 403)


class Unauthorized(AppError):
    def __init__(self, message: str = "인증이 필요합니다.", error_code: str = "UNAUTHORIZED"):
        super().__init__(error_code, message, 401)


class Conflict(AppError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(error_code, message, 409, details)


class InvalidState(AppError):
    def __init__(self, current: str, action: str, details: dict[str, Any] | None = None):
        super().__init__(
            "INVALID_STATE_TRANSITION",
            f"현재 상태({current})에서는 '{action}' 처리를 할 수 없습니다.",
            409,
            {"current_status": current, "action": action, **(details or {})},
        )


class BusinessRule(AppError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(error_code, message, 422, details)
