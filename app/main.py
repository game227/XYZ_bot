"""Bot entry point (long polling): python -m app.main"""

import asyncio
import logging

from app.bot import BOT_COMMANDS, create_bot, create_dispatcher
from app.config import get_settings
from app.database.session import get_engine
from app.services import monitoring
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    monitoring.init_sentry(settings, "bot")
    bot = create_bot(settings)
    dp = create_dispatcher(settings)
    heartbeat = asyncio.create_task(monitoring.heartbeat_loop("bot"))
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info("Bot started (long polling)")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        heartbeat.cancel()
        await bot.session.close()
        await dp.storage.close()
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
