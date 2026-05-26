from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    raw_dir: Path
    public_dir: Path
    signing_private_key_path: Path
    signing_public_key_path: Path
    live_enabled: bool


def load_settings() -> Settings:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL") or _legacy_database_url()
    return Settings(
        database_url=database_url,
        raw_dir=Path(os.getenv("PRICE_BOT_RAW_DIR", "data/raw")),
        public_dir=Path(os.getenv("PRICE_BOT_PUBLIC_DIR", "public/v1")),
        signing_private_key_path=Path(
            os.getenv("PRICE_BOT_SIGNING_PRIVATE_KEY_PATH", ".secrets/ed25519.key")
        ),
        signing_public_key_path=Path(
            os.getenv("PRICE_BOT_SIGNING_PUBLIC_KEY_PATH", ".secrets/ed25519.pub")
        ),
        live_enabled=os.getenv("PRICE_BOT_LIVE", "0").lower() in {"1", "true", "yes"},
    )


def require_database_url(settings: Settings | None = None) -> str:
    current = settings or load_settings()
    if not current.database_url:
        raise RuntimeError("DATABASE_URL is required. Add it to .env or the environment.")
    return current.database_url


def _legacy_database_url() -> str | None:
    host = os.getenv("DB_HOST")
    name = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASS")
    if not (host and name and user):
        return None
    port = os.getenv("DB_PORT", "5432")
    auth = quote_plus(user)
    if password:
        auth += f":{quote_plus(password)}"
    return f"postgresql+psycopg://{auth}@{host}:{port}/{quote_plus(name)}"
