from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.config import get_settings
from app.core.tracing import setup_tracing, shutdown_tracing
from app.database import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_tracing(settings)
    await init_db()
    try:
        yield
    finally:
        shutdown_tracing()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_production()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.is_dev else None,
        redoc_url="/redoc" if settings.is_dev else None,
        openapi_url="/openapi.json" if settings.is_dev else None,
    )

    origins = list(settings.cors_origins or [])
    # Dev-friendly: allow Expo Go / LAN origins without fighting credentials rules.
    allow_all = settings.is_dev or "*" in origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else origins,
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/")
    async def root() -> dict:
        return {
            "app": settings.app_name,
            "docs": "/docs" if settings.is_dev else None,
            "api": settings.api_prefix,
        }

    return app


app = create_app()
