"""파일 저장소 (S3 호환 / 로컬). 파일은 비공개이며 인증된 API 로만 다운로드한다."""
from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import BusinessRule
from app.models import FileAttachment

ALLOWED = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "application/pdf": [b"%PDF-"],
    "image/webp": [b"RIFF"],
}
EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".pdf": "application/pdf", ".webp": "image/webp"}


def sniff_mime(data: bytes) -> str | None:
    for mime, sigs in ALLOWED.items():
        if any(data.startswith(s) for s in sigs):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return None


def validate_upload(filename: str, data: bytes, allowed: set[str] | None = None) -> str:
    if not data:
        raise BusinessRule("EMPTY_FILE", "빈 파일입니다.")
    if len(data) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise BusinessRule("FILE_TOO_LARGE", f"파일 크기는 {settings.MAX_UPLOAD_MB}MB 이하여야 합니다.")
    ext = Path(filename or "").suffix.lower()
    claimed = EXT_MIME.get(ext)
    actual = sniff_mime(data)
    if not claimed or not actual or claimed != actual:
        raise BusinessRule("INVALID_FILE_TYPE", "허용되지 않는 파일 형식이거나 확장자와 실제 형식이 다릅니다. (JPG/PNG/PDF/WEBP)")
    if allowed and actual not in allowed:
        raise BusinessRule("INVALID_FILE_TYPE", "허용되지 않는 파일 형식입니다.")
    if actual == "application/pdf" and (b"/JavaScript" in data or b"/JS " in data or b"/Launch" in data):
        raise BusinessRule("UNSAFE_FILE", "실행 스크립트가 포함된 PDF 는 업로드할 수 없습니다.")
    return actual


class _Backend:
    def put(self, key: str, data: bytes, mime: str) -> None: ...
    def get(self, key: str) -> bytes: ...


class LocalBackend(_Backend):
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise BusinessRule("INVALID_KEY", "잘못된 파일 경로입니다.")
        return p

    def put(self, key: str, data: bytes, mime: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class S3Backend(_Backend):
    def __init__(self) -> None:
        import boto3

        self.client = boto3.client(
            "s3", endpoint_url=settings.S3_ENDPOINT_URL, aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY, region_name=settings.S3_REGION,
        )
        self.bucket = settings.S3_BUCKET
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes, mime: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime, ServerSideEncryption="AES256")

    def get(self, key: str) -> bytes:
        buf = io.BytesIO()
        self.client.download_fileobj(self.bucket, key, buf)
        return buf.getvalue()


_backend: _Backend | None = None


def backend() -> _Backend:
    global _backend
    if _backend is None:
        _backend = S3Backend() if settings.STORAGE_BACKEND == "s3" else LocalBackend(settings.STORAGE_LOCAL_DIR)
    return _backend


def save_file(db: Session, *, org_id: uuid.UUID, facility_id: uuid.UUID | None, user_id: uuid.UUID | None,
              filename: str, data: bytes, mime: str, entity_type: str | None = None,
              entity_id: uuid.UUID | None = None) -> FileAttachment:
    fid = uuid.uuid4()
    ext = Path(filename or "").suffix.lower() or ".bin"
    key = f"{org_id}/{datetime.utcnow():%Y/%m}/{fid}{ext}"
    backend().put(key, data, mime)
    fa = FileAttachment(
        id=fid, organization_id=org_id, facility_id=facility_id, original_name=(filename or "file")[:300],
        storage_key=key, mime_type=mime, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        entity_type=entity_type, entity_id=entity_id, created_by=user_id,
    )
    db.add(fa)
    db.flush()
    return fa


def read_file(fa: FileAttachment) -> bytes:
    return backend().get(fa.storage_key)
