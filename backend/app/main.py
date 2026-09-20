"""SpeakQL API entry point.

Phase 1 of the build plan: the application boots, validates its configuration,
opens one engine per database role, and answers /health honestly. The pipeline
routes arrive in later phases and are listed in README.md.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, load_or_exit
from db.engines import Engines

log = logging.getLogger("speakql")

settings: Settings = load_or_exit()
engines: Engines | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global engines
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for warning in settings.warnings:
        log.warning(warning)

    engines = Engines(settings)
    log.info(
        "started env=%s llm=%s/%s threshold=%.2f free_email=%s",
        settings.env, settings.llm_mode, settings.llm_model,
        settings.confidence_threshold, settings.free_email_mode,
    )
    try:
        yield
    finally:
        if engines is not None:
            engines.dispose()
        log.info("stopped")


app = FastAPI(
    title="SpeakQL",
    description="Natural-language analytics over a company's own warehouse.",
    version="0.1.0",
    lifespan=lifespan,
)

# The frontend is a separate origin in development (Vite on 5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
def health() -> JSONResponse:
    """Liveness plus the state of every database role.

    Reports per-role status rather than one boolean, because "the API is up"
    and "the read path can reach the warehouse" are different facts and the
    second one is the one that breaks.
    """
    databases = engines.check() if engines else {"meta": "not started"}
    healthy = all(v == "ok" for v in databases.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "version": app.version,
        "env": settings.env,
        "databases": databases,
        "llm": {"mode": settings.llm_mode, "model": settings.llm_model},
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {
        "name": "SpeakQL",
        "docs": "/docs",
        "health": "/health",
    }
