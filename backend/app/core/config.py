from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")

    APP_ENV: str = "development"  # development | test | production
    APP_NAME: str = "Food ERP-MES"
    COMPANY_NAME: str = "식품제조(주)"
    API_PREFIX: str = "/api/v1"
    TIMEZONE: str = "Asia/Seoul"

    DATABASE_URL: str = "postgresql+psycopg://foodmes:foodmes@127.0.0.1:55432/foodmes"
    DB_POOL_SIZE: int = 10

    JWT_SECRET: str = Field(default="dev-only-change-me-please-32bytes-minimum!!")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 14
    LOGIN_MAX_FAILURES: int = 5
    LOGIN_LOCK_MINUTES: int = 15

    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8081"

    # Storage
    STORAGE_BACKEND: str = "local"  # local | s3
    STORAGE_LOCAL_DIR: str = "./storage"
    S3_ENDPOINT_URL: str | None = None
    S3_ACCESS_KEY: str | None = None
    S3_SECRET_KEY: str | None = None
    S3_BUCKET: str = "food-mes"
    S3_REGION: str = "us-east-1"
    MAX_UPLOAD_MB: int = 20

    # Async
    CELERY_ENABLED: bool = False
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # OCR
    OCR_PROVIDER: str = "mock"  # google | clova | paddle | pdftext | mock
    OCR_REVIEW_THRESHOLD: Decimal = Decimal("0.90")
    OCR_EXTERNAL_ALLOWED: bool = False  # 외부 OCR 문서 전송 정책
    GOOGLE_VISION_API_KEY: str | None = None
    CLOVA_OCR_URL: str | None = None
    CLOVA_OCR_SECRET: str | None = None

    # PDF fonts
    PDF_FONT_PATH: str | None = None  # 예: C:/Windows/Fonts/malgun.ttf, /usr/share/fonts/truetype/nanum/NanumGothic.ttf

    # 업무 설정
    EXPIRY_ALERT_DAYS: str = "90,60,30,7"
    LONG_UNUSED_DAYS: int = 180
    MASS_BALANCE_TOLERANCE_PCT: Decimal = Decimal("2.0")

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def expiry_alert_days(self) -> list[int]:
        return sorted({int(x) for x in self.EXPIRY_ALERT_DAYS.split(",") if x.strip()}, reverse=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_production:
        if s.JWT_SECRET.startswith("dev-only") or len(s.JWT_SECRET) < 32:
            raise RuntimeError("운영 환경에서는 JWT_SECRET을 32자 이상 안전한 값으로 설정해야 합니다.")
        if s.OCR_PROVIDER == "mock":
            raise RuntimeError("운영 환경에서 Mock OCR Provider는 사용할 수 없습니다.")
    return s


settings = get_settings()
