"""OCR 텍스트 → 구조화 필드. 문서에 없는 정보는 생성하지 않는다(None + missing)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.ocr.providers import OCRLine

KEY_FIELDS = ["supplier_name", "material_name", "supplier_lot", "manufactured_date", "expiry_date", "quantity", "unit"]

LABELS: dict[str, list[str]] = {
    "supplier_name": ["공급업체", "공급자", "공급처", "판매자", "판매처", "상호", "업체명", "supplier", "vendor", "seller"],
    "manufacturer_name": ["제조업체", "제조사", "제조원", "제조회사", "manufacturer", "maker"],
    "material_name": ["원료명", "제품명", "품명", "품목명", "품목", "product name", "product", "item name", "item", "material"],
    "supplier_lot": ["lot no", "lot number", "lot", "로트번호", "로트", "배치번호", "batch no", "batch"],
    "manufactured_date": ["제조일자", "제조일", "생산일자", "생산일", "mfg date", "mfg", "manufacturing date", "production date"],
    "expiry_date": ["소비기한", "유통기한", "사용기한", "품질유지기한", "exp date", "expiry date", "expiration date", "exp", "best before", "use by"],
    "quantity": ["입고수량", "수량", "중량", "총중량", "순중량", "내용량", "net weight", "net wt", "quantity", "qty", "weight"],
    "country_of_origin": ["원산지", "country of origin", "origin"],
    "coa_number": ["성적서 번호", "성적서번호", "시험성적서 번호", "coa no", "coa number", "certificate no", "report no", "문서번호"],
    "issued_date": ["발행일자", "발행일", "작성일자", "작성일", "시험일자", "issue date", "date of issue", "issued"],
}

UNIT_MAP = {
    "kg": "kg", "kgs": "kg", "킬로그램": "kg", "킬로": "kg", "g": "g", "gr": "g", "그램": "g", "t": "t", "톤": "t",
    "l": "L", "ℓ": "L", "리터": "L", "ml": "mL", "㎖": "mL", "밀리리터": "mL", "ea": "EA", "개": "EA", "pcs": "EA",
    "pc": "EA", "box": "BOX", "박스": "BOX", "roll": "ROLL", "롤": "ROLL", "㎏": "kg",
}

DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})\s*[-./년]\s*(?P<m>\d{1,2})\s*[-./월]\s*(?P<d>\d{1,2})\s*일?"),
    re.compile(r"(?<!\d)(?P<y>20\d{2})(?P<m>[01]\d)(?P<d>[0-3]\d)(?!\d)"),
    re.compile(r"(?<!\d)(?P<d>[0-3]?\d)[./-](?P<m>[01]?\d)[./-](?P<y>20\d{2})(?!\d)"),
]
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DATE_EN = re.compile(r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>20\d{2})", re.I)
QTY_RE = re.compile(r"(?P<n>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<u>kgs?|㎏|g|gr|t|l|ℓ|ml|㎖|ea|pcs?|box|roll|개|킬로그램|킬로|그램|톤|리터|밀리리터|박스|롤)", re.I)
JUDGE_RE = re.compile(r"(적합|부적합|pass|fail|conform|non-?conform|ok|ng)\s*$", re.I)


@dataclass
class FieldResult:
    name: str
    raw: str | None = None
    value: Any = None
    confidence: float = 0.0
    note: str | None = None

    @property
    def missing(self) -> bool:
        return self.value in (None, "")


@dataclass
class ParsedDocument:
    fields: dict[str, FieldResult]
    items: list[dict[str, FieldResult]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def normalize_date(s: str | None) -> date | None:
    if not s:
        return None
    for pat in DATE_PATTERNS:
        m = pat.search(s)
        if m:
            try:
                return date(int(m["y"]), int(m["m"]), int(m["d"]))
            except ValueError:
                continue
    m = DATE_EN.search(s)
    if m:
        try:
            return date(int(m["y"]), MONTHS[m["mon"].lower()[:3]], int(m["d"]))
        except ValueError:
            return None
    return None


def normalize_unit(s: str | None) -> str | None:
    if not s:
        return None
    return UNIT_MAP.get(s.strip().lower())


def parse_quantity(s: str | None) -> tuple[Decimal | None, str | None]:
    if not s:
        return None, None
    m = QTY_RE.search(s)
    if not m:
        return None, None
    try:
        n = Decimal(m["n"].replace(",", ""))
    except InvalidOperation:
        return None, None
    return n, normalize_unit(m["u"])


def _value_after_label(line: str, label: str) -> str | None:
    low = line.lower()
    idx = low.find(label)
    if idx < 0:
        return None
    rest = line[idx + len(label):]
    rest = re.sub(r"^[\s:：\-=|)\]]+", "", rest)
    rest = re.sub(r"^\(?[a-zA-Z .]*\)?\s*[:：]\s*", "", rest) if label.isascii() is False and rest.startswith("(") else rest
    return rest.strip() or None


def _match_label(line: str, labels: list[str]) -> tuple[str, str] | None:
    low = line.lower()
    for lb in sorted(labels, key=len, reverse=True):
        pos = low.find(lb)
        if pos < 0:
            continue
        # 영문 짧은 라벨은 단어 경계 확인 (예: 'lot' 이 'pilot' 에 매칭되지 않도록)
        if lb.isascii():
            before = low[pos - 1] if pos > 0 else " "
            after = low[pos + len(lb)] if pos + len(lb) < len(low) else " "
            if before.isalnum() or after.isalpha():
                continue
        v = _value_after_label(line, lb)
        if v:
            return lb, v
    return None


def parse(lines: list[OCRLine], document_type: str) -> ParsedDocument:
    fields: dict[str, FieldResult] = {}
    used_lines: set[int] = set()
    order = ["coa_number", "issued_date", "supplier_name", "manufacturer_name", "supplier_lot", "manufactured_date",
             "expiry_date", "quantity", "country_of_origin", "material_name"]
    for fname in order:
        for i, ln in enumerate(lines):
            if i in used_lines:
                continue
            m = _match_label(ln.text, LABELS[fname])
            if not m:
                continue
            label, raw = m
            fr = FieldResult(name=fname, raw=raw, confidence=float(ln.confidence))
            if fname in ("manufactured_date", "expiry_date", "issued_date"):
                d = normalize_date(raw)
                if d is None:
                    fr.confidence *= 0.5
                    fr.note = "날짜 형식을 인식하지 못했습니다."
                fr.value = d
                if fname == "expiry_date":
                    kind = "SELL_BY" if "유통" in label else ("CONSUME_BY" if ("소비" in label or label in ("use by",)) else None)
                    fields["expiry_type"] = FieldResult("expiry_type", raw=label, value=kind, confidence=fr.confidence if kind else 0.0)
            elif fname == "quantity":
                n, u = parse_quantity(raw)
                fr.value = n
                if u:
                    fields["unit"] = FieldResult("unit", raw=raw, value=u, confidence=float(ln.confidence))
                if n is None:
                    fr.confidence *= 0.5
            else:
                fr.value = raw.split("  ")[0].strip()[:200]
            fields[fname] = fr
            used_lines.add(i)
            break
    for k in KEY_FIELDS + ["manufacturer_name", "country_of_origin", "coa_number", "issued_date", "expiry_type"]:
        fields.setdefault(k, FieldResult(k))
    fields["document_type"] = FieldResult("document_type", value=document_type, confidence=1.0)

    doc = ParsedDocument(fields=fields)
    # 날짜 논리 검증
    mfg, exp = fields["manufactured_date"].value, fields["expiry_date"].value
    if mfg and exp and exp <= mfg:
        doc.warnings.append("소비/유통기한이 제조일자보다 빠르거나 같습니다.")
        fields["expiry_date"].confidence = min(fields["expiry_date"].confidence, 0.5)
    if exp and mfg is None and fields["manufactured_date"].raw is None:
        pass
    # COA 시험 항목
    if document_type == "COA":
        for ln in lines:
            jm = JUDGE_RE.search(ln.text)
            if jm and not ln.text.strip().startswith(("시험항목", "항목")):
                parts = ln.text[: jm.start()].split()
                if len(parts) >= 3:
                    doc.tests.append({"item": parts[0], "standard": " ".join(parts[1:-1]), "result": parts[-1],
                                      "judgement": "PASS" if jm.group(1).lower() in ("적합", "pass", "conform", "ok") else "FAIL",
                                      "confidence": ln.confidence})
    # 영수증/거래명세서 다품목 분리: "품명 수량단위 단가 금액" 형태
    if document_type in ("RECEIPT", "TRANSACTION_STATEMENT", "DELIVERY_NOTE"):
        item_re = re.compile(r"^(?P<name>[^\d:：]{2,40}?)\s+(?P<qty>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>kg|g|l|ml|ea|개|box|박스|포)\b(?:\s+(?P<price>[\d,]+))?(?:\s+(?P<amount>[\d,]+))?", re.I)
        for ln in lines:
            m = item_re.search(ln.text.strip())
            if not m:
                continue
            name = m["name"].strip()
            if any(lb in name.lower() for lb in ("합계", "total", "부가세", "vat", "수량", "품명")):
                continue
            c = float(ln.confidence)
            item = {
                "material_name": FieldResult("material_name", name, name, c),
                "quantity": FieldResult("quantity", m["qty"], Decimal(m["qty"].replace(",", "")), c),
                "unit": FieldResult("unit", m["unit"], normalize_unit(m["unit"]) or m["unit"], c if normalize_unit(m["unit"]) else c * 0.5),
            }
            if m["price"]:
                item["unit_price"] = FieldResult("unit_price", m["price"], Decimal(m["price"].replace(",", "")), c)
            if m["amount"]:
                item["amount"] = FieldResult("amount", m["amount"], Decimal(m["amount"].replace(",", "")), c)
            doc.items.append(item)
    return doc
