"""FastAPI application factory: request guard, API routers, static UI (guard, error envelope, PWA and SPA come from
Hoard Link)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import APP_ID, SERVICE, __version__
from .api import ROUTERS
from .config import Config
from .errors import BairdError
from .hoard_link import family
from .hoard_link.agentkit import format_issues, issues_of
from .hoard_link.guard import install_guard
from .hoard_link.service import health_router, install_error_handlers, install_pwa, install_spa
from .services import Services

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config: Config | None = None, services: Services | None = None) -> FastAPI:
    config = config or (services.config if services else Config.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or Services(config)
        app.state.services = svc
        svc.start()
        logging.getLogger("baird").info("Baird's Hoard %s - data in %s", __version__, config.data_dir)
        try:
            yield
        finally:
            svc.stop()

    app = FastAPI(title="Baird's Hoard", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config = config
    family.configure(APP_ID, str(config.data_dir), token_file=str(config.token_path))
    install_guard(app, port_getter=lambda: config.port, allowed_env="BAIRD_ALLOWED_HOSTS", allowed_hosts=config.allowed_hosts)
    install_error_handlers(app)

    @app.exception_handler(BairdError)
    async def baird_error(_: Request, exc: BairdError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(ValidationError)
    async def model_error(_: Request, exc: ValidationError):
        return JSONResponse({"error": format_issues(exc), "code": "invalid_arguments", "issues": issues_of(exc)}, status_code=400)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse({"error": str(exc), "code": "invalid"}, status_code=400)

    app.include_router(health_router(SERVICE, __version__, extra=lambda: _health(app)))
    for router in ROUTERS:
        app.include_router(router)
    install_pwa(app, name="Baird's Hoard", short_name="Baird", theme="#141a1c", background="#141a1c", cache="bairds-hoard-assets",
                lang="es", static_dir=STATIC_DIR, version=__version__)
    install_spa(app, STATIC_DIR)
    return app


def _health(app: FastAPI) -> dict:
    svc = getattr(app.state, "services", None)
    return svc.health() if svc else {}
