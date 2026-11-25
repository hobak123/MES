"""OCR Provider Adapter.

모든 Provider 는 원본 바이트를 받아 OCRRawResult(text, lines[{text, confidence}]) 를 반환한다.
- google  : Google Cloud Vision REST (GOOGLE_VISION_API_KEY 필요)   ※ 외부 전송
- clova   : NAVER CLOVA OCR General (CLOVA_OCR_URL, CLOVA_OCR_SECRET) ※ 외부 전송
- paddle  : 로컬 PaddleOCR (pip install paddleocr paddlepaddle 필요)
- pdftext : 텍스트 레이어가 있는 PDF (pypdf) — 키 불필요, 실제 추출
- mock    : 개발/테스트 전용 고정 샘플. 운영 환경에서는 기동 자체가 거부된다.
"""
from __future__ import annotations

import base64
import io
import time
import uuid
from dataclasses import dataclass, field

import httpx

from app.core.config import settings


class OCRProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class OCRLine:
    text: str
    confidence: float


@dataclass
class OCRRawResult:
    text: str
    lines: list[OCRLine] = field(default_factory=list)
    provider: str = ""


def preprocess_image(data: bytes, mime: str) -> bytes:
    """EXIF 회전 보정, 그레이스케일, 최대 2500px 정규화 (원본은 별도 보관)."""
    if not mime.startswith("image/"):
        return data
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("L")
    m = max(img.size)
    if m > 2500:
        r = 2500 / m
        img = img.resize((int(img.width * r), int(img.height * r)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class OCRProvider:
    name = "base"
    external = False

    def extract(self, data: bytes, mime: str) -> OCRRawResult:  # pragma: no cover - interface
        raise NotImplementedError


class PdfTextProvider(OCRProvider):
    name = "pdftext"

    def extract(self, data: bytes, mime: str) -> OCRRawResult:
        if mime != "application/pdf":
            raise OCRProviderError("PDF 텍스트 추출기는 PDF 파일만 처리합니다. 이미지에는 OCR Provider 가 필요합니다.")
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        if not text.strip():
            raise OCRProviderError("PDF 에 텍스트 레이어가 없습니다(스캔 이미지). 이미지 OCR Provider 를 설정하세요.")
        lines = [OCRLine(t.strip(), 0.99) for t in text.splitlines() if t.strip()]
        return OCRRawResult(text=text, lines=lines, provider=self.name)


class GoogleVisionProvider(OCRProvider):
    name = "google"
    external = True

    def extract(self, data: bytes, mime: str) -> OCRRawResult:
        key = settings.GOOGLE_VISION_API_KEY
        if not key:
            raise OCRProviderError("GOOGLE_VISION_API_KEY 가 설정되지 않았습니다.")
        content = base64.b64encode(data).decode()
        if mime == "application/pdf":
            url = f"https://vision.googleapis.com/v1/files:annotate?key={key}"
            body = {"requests": [{"inputConfig": {"content": content, "mimeType": "application/pdf"},
                                  "features": [{"type": "DOCUMENT_TEXT_DETECTION"}], "pages": [1, 2, 3, 4, 5]}]}
        else:
            url = f"https://vision.googleapis.com/v1/images:annotate?key={key}"
            body = {"requests": [{"image": {"content": content}, "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                                  "imageContext": {"languageHints": ["ko", "en"]}}]}
        try:
            r = httpx.post(url, json=body, timeout=60)
        except httpx.HTTPError as e:
            raise OCRProviderError(f"Google Vision 연결 실패: {e}", retryable=True)
        if r.status_code >= 500 or r.status_code == 429:
            raise OCRProviderError(f"Google Vision 일시 오류 HTTP {r.status_code}", retryable=True)
        if r.status_code != 200:
            raise OCRProviderError(f"Google Vision 오류 HTTP {r.status_code}: {r.text[:200]}")
        resp = r.json()["responses"][0]
        annotations = [x for x in resp.get("responses", [resp])]
        texts, lines = [], []
        for a in annotations:
            fta = a.get("fullTextAnnotation") or {}
            if fta.get("text"):
                texts.append(fta["text"])
            for page in fta.get("pages", []):
                for block in page.get("blocks", []):
                    for para in block.get("paragraphs", []):
                        words = ["".join(s.get("text", "") for s in w.get("symbols", [])) for w in para.get("words", [])]
                        lines.append(OCRLine(" ".join(words), float(para.get("confidence", 0.0))))
        text = "\n".join(texts)
        if not lines:
            lines = [OCRLine(t, 0.8) for t in text.splitlines() if t.strip()]
        return OCRRawResult(text=text, lines=lines, provider=self.name)


class ClovaOCRProvider(OCRProvider):
    name = "clova"
    external = True

    def extract(self, data: bytes, mime: str) -> OCRRawResult:
        if not settings.CLOVA_OCR_URL or not settings.CLOVA_OCR_SECRET:
            raise OCRProviderError("CLOVA_OCR_URL / CLOVA_OCR_SECRET 이 설정되지 않았습니다.")
        fmt = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}.get(mime, "png")
        body = {"version": "V2", "requestId": str(uuid.uuid4()), "timestamp": int(time.time() * 1000), "lang": "ko",
                "images": [{"format": fmt, "name": "doc", "data": base64.b64encode(data).decode()}]}
        try:
            r = httpx.post(settings.CLOVA_OCR_URL, json=body, headers={"X-OCR-SECRET": settings.CLOVA_OCR_SECRET}, timeout=60)
        except httpx.HTTPError as e:
            raise OCRProviderError(f"CLOVA OCR 연결 실패: {e}", retryable=True)
        if r.status_code >= 500 or r.status_code == 429:
            raise OCRProviderError(f"CLOVA OCR 일시 오류 HTTP {r.status_code}", retryable=True)
        if r.status_code != 200:
            raise OCRProviderError(f"CLOVA OCR 오류 HTTP {r.status_code}: {r.text[:200]}")
        lines: list[OCRLine] = []
        cur, confs = [], []
        for img in r.json().get("images", []):
            for f in img.get("fields", []):
                cur.append(f.get("inferText", ""))
                confs.append(float(f.get("inferConfidence", 0)))
                if f.get("lineBreak"):
                    lines.append(OCRLine(" ".join(cur), min(confs) if confs else 0))
                    cur, confs = [], []
        if cur:
            lines.append(OCRLine(" ".join(cur), min(confs) if confs else 0))
        return OCRRawResult(text="\n".join(line.text for line in lines), lines=lines, provider=self.name)


class PaddleOCRProvider(OCRProvider):
    name = "paddle"
    _engine = None

    def extract(self, data: bytes, mime: str) -> OCRRawResult:
        if mime == "application/pdf":
            raise OCRProviderError("PaddleOCR Provider 는 이미지 파일만 처리합니다.")
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except ImportError:
            raise OCRProviderError("paddleocr 패키지가 설치되지 않았습니다. (pip install paddleocr paddlepaddle)")
        import numpy as np  # type: ignore
        from PIL import Image

        if PaddleOCRProvider._engine is None:
            PaddleOCRProvider._engine = PaddleOCR(lang="korean", use_angle_cls=True)
        arr = np.array(Image.open(io.BytesIO(data)).convert("RGB"))
        res = PaddleOCRProvider._engine.ocr(arr)
        lines = [OCRLine(item[1][0], float(item[1][1])) for page in res or [] for item in page or []]
        return OCRRawResult(text="\n".join(line.text for line in lines), lines=lines, provider=self.name)


MOCK_TEXT = """시험성적서 (Certificate of Analysis)
성적서 번호: COA-2026-1120
발행일: 2026.11.21
공급업체: ABC 원료
제조업체: ABC 제조
제품명: 혼합분말
LOT No: ABC261125
원산지: 대한민국
제조일자: 2026-11-20
소비기한: 2027-11-19
수량: 100 kg
시험항목 기준 결과 판정
수분 5% 이하 3.2% 적합
대장균군 음성 음성 적합
"""


class MockOCRProvider(OCRProvider):
    """개발/테스트 전용. 실제 문서를 읽지 않고 고정 샘플을 반환한다 (결과에 provider=mock 표시)."""

    name = "mock"

    def extract(self, data: bytes, mime: str) -> OCRRawResult:
        if settings.is_production:
            raise OCRProviderError("운영 환경에서는 Mock OCR 을 사용할 수 없습니다.")
        if mime == "application/pdf":
            try:
                return PdfTextProvider().extract(data, mime)
            except OCRProviderError:
                pass
        conf = {"수분": 0.82}
        lines = [OCRLine(t, conf.get(t.split()[0], 0.96) if t.strip() else 0.96) for t in MOCK_TEXT.splitlines() if t.strip()]
        return OCRRawResult(text=MOCK_TEXT, lines=lines, provider=self.name)


PROVIDERS: dict[str, type[OCRProvider]] = {
    "google": GoogleVisionProvider, "clova": ClovaOCRProvider, "paddle": PaddleOCRProvider,
    "pdftext": PdfTextProvider, "mock": MockOCRProvider,
}


def get_provider(name: str | None = None) -> OCRProvider:
    n = (name or settings.OCR_PROVIDER).lower()
    if n not in PROVIDERS:
        raise OCRProviderError(f"알 수 없는 OCR Provider: {n}")
    if n == "mock" and settings.is_production:
        raise OCRProviderError("운영 환경에서는 Mock OCR 을 사용할 수 없습니다.")
    p = PROVIDERS[n]()
    if p.external and not settings.OCR_EXTERNAL_ALLOWED:
        raise OCRProviderError("외부 OCR 문서 전송이 허용되지 않았습니다 (OCR_EXTERNAL_ALLOWED=false). 관리자 정책을 확인하세요.")
    return p
