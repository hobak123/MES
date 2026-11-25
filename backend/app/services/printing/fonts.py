"""ReportLab 한글 폰트 등록. PDF_FONT_PATH(TTF, 임베딩) 우선, 없으면 내장 CID 폰트(HYGothic-Medium)."""
from __future__ import annotations

import os

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

from app.core.config import settings

_FONT: str | None = None
CANDIDATES = [
    "C:/Windows/Fonts/malgun.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
]


def korean_font() -> str:
    global _FONT
    if _FONT:
        return _FONT
    paths = [settings.PDF_FONT_PATH] if settings.PDF_FONT_PATH else []
    for p in paths + CANDIDATES:
        if p and os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont("KR", p))
                _FONT = "KR"
                return _FONT
            except Exception:  # noqa: BLE001
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    _FONT = "HYGothic-Medium"
    return _FONT
