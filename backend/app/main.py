from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.context import get_request_id, set_request_context
from app.core.errors import AppError

log = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="Food ERP·MES API",
    version="1.0.0",
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
    docs_url=f"{settings.API_PREFIX}/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Content-Disposition"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or str(uuid.uuid4())
    try:
        uuid.UUID(rid)
    except ValueError:
        rid = str(uuid.uuid4())
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)
    set_request_context(rid, ip, request.headers.get("user-agent"))
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _err(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "error_code": code, "message": message, "details": details or {}, "request_id": get_request_id()},
    )


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError):
    return _err(exc.status_code, exc.error_code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    errs = []
    for e in exc.errors():
        errs.append({"field": ".".join(str(x) for x in e.get("loc", []) if x != "body"), "message": e.get("msg"), "type": e.get("type")})
    return _err(422, "VALIDATION_ERROR", "입력값을 확인해 주세요.", {"errors": errs})


@app.exception_handler(StarletteHTTPException)
async def http_handler(_: Request, exc: StarletteHTTPException):
    msg = {404: "요청한 경로를 찾을 수 없습니다.", 405: "허용되지 않는 메서드입니다."}.get(exc.status_code, str(exc.detail))
    return _err(exc.status_code, "HTTP_ERROR", msg)


@app.exception_handler(IntegrityError)
async def integrity_handler(_: Request, exc: IntegrityError):
    text = str(exc.orig) if exc.orig else str(exc)
    log.warning("integrity error: %s", text)
    if "qty_nonneg" in text or "after_nonneg" in text or "reserved_le_qty" in text:
        return _err(422, "INSUFFICIENT_STOCK", "사용 가능한 재고가 부족합니다.")
    if "unique" in text.lower() or "duplicate" in text.lower():
        return _err(409, "DUPLICATE", "이미 존재하는 데이터입니다.")
    if "foreign key" in text.lower():
        return _err(422, "INVALID_REFERENCE", "참조 대상이 올바르지 않습니다.")
    return _err(422, "INTEGRITY_ERROR", "데이터 무결성 제약을 위반했습니다.")


@app.exception_handler(DBAPIError)
async def db_handler(_: Request, exc: DBAPIError):
    text = str(exc.orig) if exc.orig else str(exc)
    if "immutable" in text:
        return _err(409, "IMMUTABLE_RECORD", "변경할 수 없는 기록입니다.")
    log.exception("database error")
    return _err(500, "DATABASE_ERROR", "데이터베이스 처리 중 오류가 발생했습니다.")


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error")
    return _err(500, "INTERNAL_ERROR", "서버 내부 오류가 발생했습니다. 관리자에게 문의하세요.")


@app.get("/health")
def health():
    from sqlalchemy import text

    from app.core.db import engine

    with engine.connect() as c:
        c.execute(text("SELECT 1"))
    return {"status": "ok", "env": settings.APP_ENV}


from app.api.v1.router import api_router  # noqa: E402

app.include_router(api_router, prefix=settings.API_PREFIX)
