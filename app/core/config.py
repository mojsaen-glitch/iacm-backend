from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
import json


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    APP_NAME: str = "IACM - Iraqi Airways Crew Management"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    SECRET_KEY: str
    API_V1_PREFIX: str = "/api/v1"
    # CORS allowlist (NOT "*"). Kept in code so it's set reliably without the
    # fragility of JSON-in-an-env-var. An ALLOWED_HOSTS env var (valid JSON
    # array) still overrides this if present.
    ALLOWED_HOSTS: List[str] = [
        "https://iacm-frontend.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
    ]

    # Supabase (required)
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # Database (optional — used by SQLAlchemy/Alembic only; primary access is via Supabase client)
    DATABASE_URL: Optional[str] = None
    DATABASE_URL_SYNC: Optional[str] = None

    # Shared secret for Vercel Cron → /documents/cron/* (Bearer token). Empty
    # disables the cron endpoint (returns 403). Set via env, never committed.
    CRON_SECRET: str = ""

    # Error tracking (Sentry) — inert when SENTRY_DSN is unset.
    SENTRY_DSN: str = ""
    SENTRY_ENV: str = "production"

    # JWT
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # File Storage
    UPLOAD_DIR: str = "uploads"
    MAX_FILE_SIZE_MB: int = 10
    ALLOWED_IMAGE_TYPES: List[str] = ["image/jpeg", "image/png", "image/webp"]
    ALLOWED_DOC_TYPES: List[str] = ["application/pdf", "image/jpeg", "image/png"]

    # Email
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = ""

    # Pagination
    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 100

    # FTL Limits (EASA defaults)
    MAX_MONTHLY_HOURS: float = 100.0
    MAX_YEARLY_HOURS: float = 900.0
    MIN_REST_HOURS: float = 10.0
    MAX_DUTY_HOURS: float = 14.0
    # Max same-station ground stop still treated as an intra-duty turnaround
    # (not inter-duty rest). Sectors of a same-day rotation connected by a stop
    # at/below this many hours form ONE duty; rest applies only after the last.
    MAX_TURNAROUND_HOURS: float = 3.0

    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v):
        # Accept a JSON array OR a comma-separated string, and NEVER raise — a
        # malformed value must not crash the whole app at startup. Falls back to
        # comma-splitting, then to an empty list.
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            try:
                parsed = json.loads(s)
                return parsed if isinstance(parsed, list) else [str(parsed)]
            except Exception:
                return [x.strip() for x in s.split(",") if x.strip()]
        return v


settings = Settings()
