"""arq connection shared by the bot, web and worker processes. OWNER: COMMERCE."""

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.config import get_settings

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
