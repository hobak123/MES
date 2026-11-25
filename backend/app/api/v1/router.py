from fastapi import APIRouter

from app.api.v1 import auth, inventory, master, mes, ocr, production, qr, receipts, system, trace

api_router = APIRouter()
for m in (auth, master, receipts, ocr, qr, inventory, production, mes, trace, system):
    api_router.include_router(m.router)
