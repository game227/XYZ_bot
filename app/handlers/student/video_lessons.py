"""▶️ Video darslar — lessons that have a video material, across the catalog."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers.student.lessons import lesson_label, lesson_marks
from app.keyboards import menus
from app.keyboards.content import PAGE_SIZE, CtLes, CtList, btn, markup, pager_row, show
from app.services import lessons

router = Router(name="student.video_lessons")


async def render(session: AsyncSession, user: User, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    items, total = await lessons.list_video_lessons(session, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    if not items:
        return "▶️ Hozircha video darslar yo‘q.", None
    subscriber, opened = await lesson_marks(session, user)
    rows = [
        [btn(lesson_label(lsn, subscriber, opened), CtLes(action="open", lesson_id=lsn.id))] for lsn in items
    ]
    rows.append(pager_row(page, total, CtList, kind="video"))
    text = (
        "▶️ <b>Video darslar</b>\n\n"
        "Videolar himoyalangan player orqali ko‘rsatiladi.\n🆓 bepul · 🔒 obuna kerak · ✅/📖 ochiq"
    )
    return text, markup(rows)


@router.message(F.text == menus.BTN_VIDEO_LESSONS)
async def video_lessons_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render(session, user, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtList.filter(F.kind == "video"))
async def video_lessons_page(
    callback: CallbackQuery, callback_data: CtList, session: AsyncSession, user: User
) -> None:
    text, kb = await render(session, user, max(0, callback_data.page))
    await show(callback, text, kb)
    await callback.answer()
