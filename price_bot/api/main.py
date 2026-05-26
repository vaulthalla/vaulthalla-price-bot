from __future__ import annotations

from fastapi import FastAPI

from price_bot.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="vaulthalla-price-bot", version="0.1.0")
    app.include_router(router)
    return app
