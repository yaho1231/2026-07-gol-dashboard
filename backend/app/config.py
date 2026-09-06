import os
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def resolve_sqlite_url(url: str) -> str:
    if not url.startswith("sqlite:///"):
        return url
    db_path = url.replace("sqlite:///", "", 1)
    if os.path.isabs(db_path):
        return url
    return f"sqlite:///{os.path.join(PROJECT_ROOT, os.path.normpath(db_path))}"


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./inhouse.db"
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "qwen3:8b"
    OLLAMA_TIMEOUT_SECONDS: float = 120.0
    SYNC_INTERVAL_MINUTES: int = 45
    TIER_REFRESH_HOURS: int = 24
    API_KEY: str = ""
    DASHBOARD_PORT: int = 8501
    # 공개 대시보드 주소(.env PUBLIC_URL). 공유 카드 푸터 등 사용자에게 보여줄
    # "사이트 주소"는 전부 여기서 파생시킨다 — 도메인이 바뀌어도 .env 한 줄만 고치면 된다.
    PUBLIC_URL: str = ""
    # PUBLIC_URL 이 비었을 때 카드 푸터 등에 표기할 대체 호스트.
    PUBLIC_HOST_FALLBACK: str = "localhost"
    # 대시보드 외의 출처에서 이 API를 브라우저로 호출해야 할 때만 채운다(콤마 구분).
    CORS_ALLOW_ORIGINS: str = ""

    model_config = SettingsConfigDict(
        env_file=os.path.join(PROJECT_ROOT, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("DATABASE_URL", mode="after")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        return resolve_sqlite_url(value)

settings = Settings()

DEFAULT_PUBLIC_HOST = settings.PUBLIC_HOST_FALLBACK


def public_host() -> str:
    """카드 푸터 등에 표기할 사이트 주소(스킴·경로 뺀 호스트).

    PUBLIC_URL 이 비어 있으면(PUBLIC_MODE=none 등) PUBLIC_HOST_FALLBACK 을 쓴다.
    """
    url = (settings.PUBLIC_URL or "").strip()
    if not url:
        return DEFAULT_PUBLIC_HOST
    host = url.split("://", 1)[-1].strip("/")
    return host.split("/", 1)[0] or DEFAULT_PUBLIC_HOST
