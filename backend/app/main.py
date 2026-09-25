"""SpeakQL API entry point.

Boots, validates its configuration, opens one connection pool per database
role, and mounts the routers. Everything it owns lives on `app.state` and is
reached through `app/deps.py` — no module imports a global engine or a global
session, which is what keeps `executor.py` unable to construct a connection.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
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
from auth import otp
from auth import routes as auth_routes
from core.llm_client import LLMClient
from core.schema_retriever import EmbeddingRetriever
from core.sql_generator import CodeT5Generator
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
        codes_per_address_hour=settings.rate_codes_per_address_hour,
        codes_per_ip_hour=settings.rate_codes_per_ip_hour,
    )

    # Model B, and Model A's index. Both optional: an untrained checkout
    # answers questions through Gemma and lexical retrieval, which is exactly
    # the off-the-shelf baseline the comparative study measures against.
    generator = CodeT5Generator(settings.generator_checkpoint)
    application.state.generator = generator if generator.available() else None
    retriever = EmbeddingRetriever(settings.retriever_checkpoint)
    application.state.retriever = retriever if retriever.available() else None
    log.info(
        "models: generator=%s retriever=%s",
        "codet5-small" if application.state.generator else "gemma (no checkpoint)",
        "minilm" if application.state.retriever else "lexical (no checkpoint)",
    )
    if application.state.generator is not None:
        threading.Thread(target=application.state.generator.warm, daemon=True,
                         name="speakql-warm-codet5").start()

    # The LLM is optional at boot. A backend that refused to start without a
    # 3 GB download would make the whole demo hostage to it; the explainer
    # falls back to its deterministic sentence and /health says so.
    client = LLMClient(mode=settings.llm_mode, model=settings.llm_model,
                       endpoint=settings.llm_endpoint)
    application.state.llm = client if client.health() else None
    if application.state.llm is not None:
        # In the background: loading 3 GB must not delay the port opening,
        # and /health is allowed to say "reachable" before it finishes.
        threading.Thread(target=client.warm, daemon=True,
                         name="speakql-warm-llm").start()
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
    # Headers are passed through: the first version dropped them, so every
    # 429 lost the Retry-After that tells a client when to come back.
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(otp.LockedOut)
async def locked_out(_request: Request, exc: otp.LockedOut) -> JSONResponse:
    # Without this handler a locked address fell through to the catch-all
    # below and got a 500 -- the right refusal, reported as our fault.
    wait = max(1, int((exc.until - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    return JSONResponse({"detail": str(exc)}, status_code=429,
                        headers={"Retry-After": str(wait)})


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

    # Asked NOW, not remembered from startup: the first version reported the
    # decision made when the process booted, so a model that had gone away an
    # hour ago was still described as reachable.
    client = getattr(request.app.state, "llm", None)
    llm_up = bool(client and client.health())

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
        "models": {
            "generator": (getattr(request.app.state, "generator", None)
                          and "codet5-small") or "none (Gemma writes every query)",
            "retriever": (getattr(request.app.state, "retriever", None)
                          and "minilm") or "lexical",
        },
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {"name": "SpeakQL", "docs": "/docs", "health": "/health"}
