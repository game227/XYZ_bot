"""Router auto-discovery.

Every module under app/handlers/** that defines a module-level `router: aiogram.Router`
is included automatically, in sorted module-path order. `app.handlers.common.fallback`
is always included last so catch-all handlers never shadow feature handlers.
No other module may register catch-all handlers (a bare @router.message() with no filters).
"""

import importlib
import logging
import pkgutil

from aiogram import Router

logger = logging.getLogger(__name__)

FALLBACK_MODULE = "app.handlers.common.fallback"


def _iter_modules() -> list[str]:
    import app.handlers as package

    names = [
        info.name
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}.")
        if not info.ispkg
    ]
    return sorted(n for n in names if n != FALLBACK_MODULE) + [FALLBACK_MODULE]


def collect_routers() -> list[Router]:
    routers: list[Router] = []
    for name in _iter_modules():
        module = importlib.import_module(name)
        router = getattr(module, "router", None)
        if isinstance(router, Router):
            routers.append(router)
            logger.debug("router included: %s", name)
    return routers
