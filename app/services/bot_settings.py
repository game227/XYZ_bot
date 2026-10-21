"""Runtime bot settings stored in `bot_settings` (editable by admins with SETTINGS perm)."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BotSetting, User
from app.services.audit import log_action

# Known keys and their defaults.
PAYMENT_INSTRUCTIONS = "payment_instructions"
SUPPORT_CONTACT = "support_contact"
HELP_TEXT = "help_text"

DEFAULTS: dict[str, Any] = {
    PAYMENT_INSTRUCTIONS: (
        "To‘lovni amalga oshirgach, chek rasmini shu yerga yuboring. "
        "Administrator tekshirib, obunangizni faollashtiradi."
    ),
    SUPPORT_CONTACT: "",
    HELP_TEXT: (
        "XYZ ta’lim boti.\n\n"
        "📚 Fanlar va kurslar — katalog\n"
        "🎓 Bepul darslar — obunasiz 2 ta dars\n"
        "💳 Obuna olish — tariflar va to‘lov\n"
        "📈 Mening natijalarim — test natijalari\n\n"
        "Buyruqlar: /start, /menu, /profile, /subscription, /help"
    ),
}

# Human-readable names for the admin settings screen.
LABELS: dict[str, str] = {
    PAYMENT_INSTRUCTIONS: "To‘lov yo‘riqnomasi",
    SUPPORT_CONTACT: "Yordam uchun aloqa",
    HELP_TEXT: "Yordam matni",
}


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    value = await session.scalar(select(BotSetting.value).where(BotSetting.key == key))
    if value is not None:
        return value
    return DEFAULTS.get(key, default) if default is None else default


async def set_setting(session: AsyncSession, key: str, value: Any, *, actor: User | None) -> None:
    stmt = insert(BotSetting).values(key=key, value=value, updated_by_user_id=actor.id if actor else None)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BotSetting.key],
        set_={
            "value": stmt.excluded.value,
            "updated_by_user_id": stmt.excluded.updated_by_user_id,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)
    await log_action(session, "bot_setting.updated", actor=actor, entity_type="bot_setting", entity_id=key)
