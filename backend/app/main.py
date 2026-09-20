"""SpeakQL API entry point.

Boots, validates its configuration, opens one connection pool per database
role, and mounts the routers. Everything it owns lives on `app.state` and is
reached through `app/deps.py` — no module imports a global engine or a global
session, which is what keeps `executor.py` unable to construct a connection.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import (
    routes_ask, routes_connections, routes_datasets, routes_merges,
    routes_misc, routes_org,
)
from app.config import Settings, load_or_exit
from app.mailer import Mailer
from app.ratelimit import RateLimiter
from auth import routes as auth_routes
from core.llm_client import LLMClient
from db.engines import Engines
from db.session import SessionFactory

log = logging.getLogger("speakql")

settings: Settings = load_or_exit()


@asynccontextmanager
async def lifespan(application: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for warning in settings.warnings:
        log.warning(warning)

    engines = Engines(settings)

    application.state.settings = settings
    application.state.engines = engines
    application.state.sessions = SessionFactory(engines.meta)
    application.state.mailer = Mailer(env=settings.env)
    application.state.limiter = RateLimiter(
        ask_per_hour=settings.rate_ask_per_hour,
        upload_per_day=settings.rate_upload_per_day,
        export_per_hour=settings.rate_export_per_hour,
    )

    # The LLM is optional at boot. A backend that refused to start without a
    # 3 GB download would make the whole demo hostage to it; the explainer
    # falls back to its deterministic sentence and /health says so.
    client = LLMClient(mode=settings.llm_mode, model=settings.llm_model,
                       endpoint=settings.llm_endpoint)
    application.state.llm = client if client.health() else None
    if application.state.llm is None:
        log.warning(
            "%s is not reachable at %s. Questions will fail until it is; "
            "run `make up-local && make pull-model`.",
            settings.llm_model, settings.llm_endpoint,
        )

    log.info(
        "started env=%s llm=%s/%s threshold=%.2f free_email=%s",
        settings.env, settings.llm_mode, settings.llm_model,
        settings.confidence_threshold, settings.free_email_mode,
    )
    try:
        yield
    finally:
        engines.dispose()
        log.info("stopped")


app = FastAPI(
    title="SpeakQL",
    description=(
        "Natural-language analytics over a company's own warehouse. "
        "Every answer is a single validated read-only SELECT."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- errors ----
# One shape for every problem, so the interface has one place to render it.

@app.exception_handler(StarletteHTTPException)
async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request,
                           exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", ())[1:]) or "request"
    return JSONResponse(
        {"detail": f"{field}: {first.get('msg', 'is not valid')}"},
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
    # Never return the exception text: it carries schema names, file paths and
    # occasionally a DSN. Log it in full, return a sentence.
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse({"detail": "something went wrong on our side"},
                        status_code=500)


# ---------------------------------------------------------------- routes ----

app.include_router(auth_routes.router)
app.include_router(routes_ask.router_api)
app.include_router(routes_org.router_api)
app.include_router(routes_connections.router_api)
app.include_router(routes_datasets.router_api)
app.include_router(routes_merges.router_api)
app.include_router(routes_misc.router_api)


@app.get("/health", tags=["meta"], response_model=None)
def health(request: Request) -> JSONResponse:
    """Per-role database status rather than one boolean.

    "The API is up" and "the read path can reach the warehouse" are different
    facts, and the second is the one that breaks.
    """
    engines: Engines | None = getattr(request.app.state, "engines", None)
    databases = engines.check() if engines else {"meta": "not started"}
    llm_up = getattr(request.app.state, "llm", None) is not None

    healthy = all(v == "ok" for v in databases.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "version": app.version,
        "env": settings.env,
        "databases": databases,
        "llm": {
            "mode": settings.llm_mode,
            "model": settings.llm_model,
            "reachable": "yes" if llm_up else "no",
        },
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {"name": "SpeakQL", "docs": "/docs", "health": "/health"}
