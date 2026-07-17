from __future__ import annotations
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import get_settings
from app.db import Base, engine
from app.routers import tools, webhooks


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Clinic Voice Receptionist API",
        version="1.0.0",
        description="Backend tool-calling API for Bolna voice receptionist + Cliniko PMS write-back",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(tools.router)
    app.include_router(webhooks.router)

    @app.get("/")
    async def root():
        return {
            "service": "clinic-voice-receptionist",
            "timezone": settings.timezone,
            "docs": "/docs",
            "tools": "/tools/clinic_directory",
        }

    return app


app = create_app()
