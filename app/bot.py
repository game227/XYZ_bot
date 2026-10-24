from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand

from app.config import Settings
from app.database.session import get_sessionmaker
from app.handlers import collect_routers
from app.middlewares.db import DbSessionMiddleware
from app.middlewares.menu_reset import MenuResetMiddleware
from app.middlewares.throttling import ThrottlingMiddleware
from app.middlewares.user import UserMiddleware
from app.services.monitoring import MetricsMiddleware
from app.services.teacher_trail import TrailRequestMiddleware, TrailUpdateMiddleware

BOT_COMMANDS = [
    BotCommand(command="start", description="Botni boshlash"),
    BotCommand(command="menu", description="Asosiy menyu"),
    BotCommand(command="profile", description="Profil"),
    BotCommand(command="subscription", description="Obuna holati"),
    BotCommand(command="logout", description="O‘qituvchi sessiyasidan chiqish"),
    BotCommand(command="help", description="Yordam"),
]


def create_bot(settings: Settings) -> Bot:
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    bot.session.middleware(TrailRequestMiddleware())
    return bot


def create_storage(settings: Settings) -> BaseStorage:
    if settings.redis_url:
        return RedisStorage.from_url(settings.redis_url)
    return MemoryStorage()


def create_dispatcher(settings: Settings, storage: BaseStorage | None = None) -> Dispatcher:
    dp = Dispatcher(storage=storage or create_storage(settings))
    # Outer update middlewares run for every update, in registration order.
    dp.update.outer_middleware(MetricsMiddleware())
    dp.update.outer_middleware(TrailUpdateMiddleware())
    dp.update.outer_middleware(ThrottlingMiddleware())
    dp.update.outer_middleware(DbSessionMiddleware(get_sessionmaker()))
    dp.update.outer_middleware(UserMiddleware())
    dp.message.outer_middleware(MenuResetMiddleware())
    dp.include_routers(*collect_routers())
    return dp
