"""📈 Mening natijalarim — the student's own finished quiz attempts (TZ §8.3, §8.4).

Results stay after a subscription ends. Only the user's own attempts are ever listed.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import QuizResultVisibility, User
from app.handlers.student.quizzes import fmt_score
from app.keyboards import menus
from app.keyboards.content import PAGE_SIZE, CtList, CtQz, btn, markup, pager_row, show
from app.services import quizzes
from app.utils.text import esc
from app.utils.time import fmt_dt

router = Router(name="student.results")


async def render(session: AsyncSession, user: User, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    await quizzes.expire_overdue_attempts(session, user.id)
    total = await quizzes.count_user_results(session, user.id)
    rows_data = await quizzes.results_for_user(session, user.id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    if not rows_data:
        return "📈 Siz hali test ishlamagansiz. 🧪 Testlar bo‘limidan boshlang.", None
    lines = ["📈 <b>Mening natijalarim</b>\n"]
    buttons = []
    for n, row in enumerate(rows_data, start=page * PAGE_SIZE + 1):
        a = row.attempt
        hidden = row.quiz_visibility == QuizResultVisibility.HIDDEN
        result = "natija yashirin" if hidden else f"{fmt_score(a.percent)}% {'✅' if a.passed else '❌'}"
        lines.append(
            f"{n}. <b>{esc(row.quiz_title)}</b> ({esc(row.lesson_title)}) — {result}, {fmt_dt(a.finished_at)}"
        )
        buttons.append([btn(f"{n}. {row.quiz_title}", CtQz(action="summary", attempt_id=a.id))])
    buttons.append(pager_row(page, total, CtList, kind="results"))
    return "\n".join(lines), markup(buttons)


@router.message(F.text == menus.BTN_MY_RESULTS)
async def results_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render(session, user, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtList.filter(F.kind == "results"))
async def results_page(
    callback: CallbackQuery, callback_data: CtList, session: AsyncSession, user: User
) -> None:
    text, kb = await render(session, user, max(0, callback_data.page))
    await show(callback, text, kb)
    await callback.answer()


@router.callback_query(CtQz.filter(F.action == "summary"))
async def result_summary(
    callback: CallbackQuery, callback_data: CtQz, session: AsyncSession, user: User
) -> None:
    from app.handlers.student.quizzes import render_summary

    text, kb = await render_summary(session, user, callback_data.attempt_id)
    kb.inline_keyboard.append([btn("⬅️ Natijalar", CtList(kind="results"))])
    await show(callback, text, kb)
    await callback.answer()
