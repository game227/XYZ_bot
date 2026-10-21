"""Admin: runtime bot settings (payment instructions, support contact, help text)."""

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, User
from app.keyboards import menus
from app.services import bot_settings
from app.services.access import AdminFilter, require_admin
from app.services.errors import InvalidInput, NotFound
from app.utils.text import esc, truncate

router = Router(name="admin.settings")

MAX_VALUE_LENGTH = 3500


class CoSettingEdit(StatesGroup):
    value = State()


class CoSettingCb(CallbackData, prefix="co_set"):
    action: str  # edit
    key: str


def _kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, label in bot_settings.LABELS.items():
        kb.button(text=f"✏️ {label}", callback_data=CoSettingCb(action="edit", key=key))
    kb.adjust(1)
    return kb.as_markup()


async def _overview(session: AsyncSession) -> str:
    lines = ["<b>⚙️ Bot sozlamalari</b>"]
    for key, label in bot_settings.LABELS.items():
        value = await bot_settings.get_setting(session, key)
        shown = esc(truncate(str(value), 300)) if value else "<i>bo‘sh</i>"
        lines.append(f"\n<b>{label}</b>:\n{shown}")
    return "\n".join(lines)


@router.message(F.text == menus.BTN_A_SETTINGS, AdminFilter(AdminPermission.SETTINGS))
async def settings_menu(message: Message, user: User, session: AsyncSession, state: FSMContext) -> None:
    require_admin(user, AdminPermission.SETTINGS)
    await state.clear()
    await message.answer(await _overview(session), reply_markup=_kb())


@router.callback_query(CoSettingCb.filter(F.action == "edit"))
async def edit_start(
    callback: CallbackQuery, callback_data: CoSettingCb, user: User, state: FSMContext
) -> None:
    require_admin(user, AdminPermission.SETTINGS)
    if callback_data.key not in bot_settings.LABELS:
        raise NotFound()
    await state.set_state(CoSettingEdit.value)
    await state.update_data(key=callback_data.key)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"«{bot_settings.LABELS[callback_data.key]}» uchun yangi matnni yuboring.\n"
            "Bo‘shatish uchun «-» yuboring. Bekor qilish: /menu"
        )
    await callback.answer()


@router.message(CoSettingEdit.value, F.text)
async def edit_save(message: Message, user: User, session: AsyncSession, state: FSMContext) -> None:
    require_admin(user, AdminPermission.SETTINGS)
    key = (await state.get_data()).get("key")
    if key not in bot_settings.LABELS:
        await state.clear()
        raise NotFound()
    value = (message.text or "").strip()
    if len(value) > MAX_VALUE_LENGTH:
        raise InvalidInput(f"Matn juda uzun (maksimal {MAX_VALUE_LENGTH} belgi).")
    await bot_settings.set_setting(session, key, "" if value == "-" else value, actor=user)
    await state.clear()
    await message.answer("✅ Saqlandi.")
    await message.answer(await _overview(session), reply_markup=_kb())
