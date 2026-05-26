from __future__ import annotations

from sqlalchemy import create_engine

from price_bot.config import load_settings, require_database_url
from price_bot.db.models import Base


def init_db() -> None:
    settings = load_settings()
    engine = create_engine(require_database_url(settings), future=True)
    Base.metadata.create_all(engine)
