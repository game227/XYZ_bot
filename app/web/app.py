"""Web service (Mini App + protected video player): uvicorn app.web.app:app

Routers are auto-discovered: every module in app/web/routers/ exposing a module-level
`router: fastapi.APIRouter` is included. Static files: app/web/static/<area>/...
"""

import importlib
import logging
import pkgutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database.session import get_engine
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _collect_routers() -> list[APIRouter]:
    import app.web.routers as package

    routers = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.services import storage

    try:
        # Uploads go straight to S3; on a fresh store the bucket may not exist yet.
        await storage.ensure_bucket()
    except Exception:  # noqa: BLE001 - storage being down must not stop the web service
        logger.warning("could not ensure the media bucket at startup", exc_info=True)
    yield
    await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings)
    is_prod = settings.environment == "prod"
    application = FastAPI(
        title="XYZ Bot Web",
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    STATIC_DIR.mkdir(exist_ok=True)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in _collect_routers():
        application.include_router(router)
    return application


app = create_app()
