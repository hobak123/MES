"""라벨 생성(PDF/ZPL/TSPL) 및 네트워크 프린터 전송 어댑터.

- PDF: ReportLab, 50x30 / 60x40 / 80x50 / 100x70 mm 및 사용자 지정, 한글 폰트.
- ZPL(Zebra) / TSPL(TSC) 텍스트 생성 → TCP 9100 RAW 전송. ※ 실제 프린터 모델별 검증 필요 (장비 연동).
- USB/Bluetooth 프린터는 클라이언트(브라우저 인쇄/모바일 SDK)에서 PDF/ZPL 을 전달하는 방식으로 확장.
"""
from __future__ import annotations

import io
import re
import socket
from typing import Any

from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.services.printing.fonts import korean_font
from app.services.qr.service import qr_png

PRESET_SIZES = {"50x30": (50, 30), "60x40": (60, 40), "80x50": (80, 50), "100x70": (100, 70)}


class PrinterError(Exception):
    pass


def parse_size(size: str | None) -> tuple[int, int]:
    if not size:
        return PRESET_SIZES["80x50"]
    if size in PRESET_SIZES:
        return PRESET_SIZES[size]
    m = re.fullmatch(r"(\d{2,3})x(\d{2,3})", size)
    if not m:
        raise ValueError("라벨 크기는 '가로x세로'(mm) 형식이어야 합니다. 예: 80x50")
    w, h = int(m[1]), int(m[2])
    if not (20 <= w <= 200 and 15 <= h <= 200):
        raise ValueError("라벨 크기 범위: 가로 20~200mm, 세로 15~200mm")
    return w, h


def _lines(d: dict[str, Any]) -> list[str]:
    rows = [
        d.get("company"),
        f"{d.get('name', '')} ({d.get('code', '')})",
        f"LOT: {d.get('lot_number', '')}" + (f" / 공급: {d['supplier_lot']}" if d.get("supplier_lot") else ""),
        f"포장ID: {d['package_number']}" if d.get("package_number") else None,
        f"수량: {d.get('quantity', '')} {d.get('unit', '')}" if d.get("quantity") else None,
        " / ".join(x for x in [f"제조 {d['manufactured_date']}" if d.get("manufactured_date") else None,
                               f"입고 {d['received_date']}" if d.get("received_date") else None] if x) or None,
        f"기한: {d['expiry_date']}" if d.get("expiry_date") else None,
        " / ".join(x for x in [d.get("storage_condition"), f"검수: {d['quality_status']}" if d.get("quality_status") else None] if x) or None,
    ]
    return [r for r in rows if r]


def label_pdf(labels: list[dict[str, Any]], size: str | None = None) -> bytes:
    w, h = parse_size(size)
    font = korean_font()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w * mm, h * mm))
    c.setTitle("QR Label")
    for d in labels:
        qr_side = min(h - 4, w * 0.42) * mm
        img = ImageReader(io.BytesIO(qr_png(d["payload"], box_size=6)))
        c.drawImage(img, w * mm - qr_side - 2 * mm, (h * mm - qr_side) / 2, qr_side, qr_side)
        text_w = w * mm - qr_side - 5 * mm
        lines = _lines(d)
        fs = max(5.0, min(9.0, (h - 4) / max(len(lines), 1) * 0.75 * 2.83))
        y = h * mm - 2 * mm - fs
        for i, line in enumerate(lines):
            c.setFont(font, fs + (1.5 if i == 1 else 0))
            # 잘림 방지: 폭을 넘으면 글자 크기 축소
            size_now = fs + (1.5 if i == 1 else 0)
            while c.stringWidth(line, font, size_now) > text_w and size_now > 4:
                size_now -= 0.5
            c.setFont(font, size_now)
            c.drawString(2 * mm, y, line)
            y -= size_now * 1.25
            if y < 1 * mm:
                break
        c.showPage()
    c.save()
    return buf.getvalue()


def _ascii(s: str) -> str:
    return s.replace("^", " ").replace("~", " ")


def label_zpl(d: dict[str, Any], size: str | None = None, dpi: int = 203) -> str:
    w, h = parse_size(size)
    dots = dpi / 25.4
    pw, ph = int(w * dots), int(h * dots)
    mag = max(2, min(8, int(min(ph, pw * 0.45) / 60)))
    out = ["^XA", "^CI28", f"^PW{pw}", f"^LL{ph}"]
    y = 10
    for line in _lines(d):
        out.append(f"^FO10,{y}^A0N,22,22^FD{_ascii(line)}^FS")
        y += 26
    out.append(f"^FO{int(pw - mag * 33 - 10)},10^BQN,2,{mag}^FDQA,{_ascii(d['payload'])}^FS")
    out.append("^XZ")
    return "\n".join(out)


def label_tspl(d: dict[str, Any], size: str | None = None) -> str:
    w, h = parse_size(size)
    out = [f"SIZE {w} mm,{h} mm", "GAP 2 mm,0", "CODEPAGE UTF-8", "CLS"]
    y = 10
    for line in _lines(d):
        out.append(f'TEXT 10,{y},"K.BF2",0,1,1,"{line.replace(chr(34), " ")}"')
        y += 28
    out.append(f'QRCODE {int(w * 8 * 0.55)},10,M,4,A,0,"{d["payload"].replace(chr(34), chr(92) + chr(34))}"')
    out.append("PRINT 1,1")
    return "\r\n".join(out) + "\r\n"


def send_raw(host: str, port: int, data: bytes, timeout: float = 5.0) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(data)
    except OSError as e:
        raise PrinterError(f"프린터 연결 실패({host}:{port}): {e}") from e
