"""🧪 Testlar — taking quizzes inside Telegram (TZ §8.3).

Progress lives in `quiz_attempts`; the FSM only keeps the options ticked for the current
multiple-choice question and marks "waiting for a typed answer". Correct answers are never
part of a question payload: they are shown only after the attempt ends, per
`result_visibility`.
"""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AttemptStatus, QuestionType, QuizAccessMode, User
from app.keyboards import menus
from app.keyboards.content import PAGE_SIZE, CtLes, CtList, CtQz, btn, markup, pager_row, show
from app.services import lessons, quizzes
from app.services.subscriptions import has_active_subscription
from app.utils.text import esc, truncate
from app.utils.time import utcnow

router = Router(name="student.quizzes")

REVIEW_MESSAGE_MAX = 3800

STATUS_TEXT = {
    AttemptStatus.COMPLETED: "yakunlandi",
    AttemptStatus.EXPIRED: "vaqt tugadi",
    AttemptStatus.INTERRUPTED: "to‘xtatildi (kirish huquqi tugadi)",
    AttemptStatus.IN_PROGRESS: "davom etmoqda",
}


class CtQuizAnswer(StatesGroup):
    text = State()


def _selection_key(attempt_id: int, question_id: int) -> str:
    return f"ct_sel:{attempt_id}:{question_id}"


def _fmt_seconds(seconds: int) -> str:
    seconds = max(0, seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def fmt_score(value: object) -> str:
    text = f"{value}"
    return text[:-3] if text.endswith(".00") else text


# --------------------------------------------------------------------------- list + card


async def render_list(
    session: AsyncSession, user: User, page: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    items, total = await quizzes.student_quizzes(session, user, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    if not items:
        return "🧪 Hozircha testlar yo‘q.", None
    subscriber = await has_active_subscription(session, user.id)
    opened = await lessons.opened_free_lesson_ids(session, user.id)
    rows: list[list[InlineKeyboardButton]] = []
    for quiz, lesson in items:
        mode = quizzes.effective_mode(quiz, lesson)
        if subscriber or lesson.id in opened or quiz.access_mode == QuizAccessMode.FREE:
            mark = "🧪"
        elif mode == QuizAccessMode.FREE:
            mark = "🆓"
        else:
            mark = "🔒"
        rows.append([btn(f"{mark} {quiz.title} · {lesson.title}", CtQz(action="card", quiz_id=quiz.id))])
    rows.append(pager_row(page, total, CtList, kind="quizzes"))
    return "🧪 <b>Testlar</b>\n\nTestni tanlang (🆓 bepul dars testi · 🔒 obuna kerak):", markup(rows)


@router.message(F.text == menus.BTN_QUIZZES)
async def quizzes_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render_list(session, user, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtList.filter(F.kind == "quizzes"))
async def quizzes_page(
    callback: CallbackQuery, callback_data: CtList, session: AsyncSession, user: User
) -> None:
    text, kb = await render_list(session, user, max(0, callback_data.page))
    await show(callback, text, kb)
    await callback.answer()


@router.callback_query(CtQz.filter(F.action == "card"))
async def quiz_card(callback: CallbackQuery, callback_data: CtQz, session: AsyncSession, user: User) -> None:
    quiz = await quizzes.get_quiz(session, callback_data.quiz_id)
    lesson = await lessons.get_lesson(session, quiz.lesson_id)
    staff = await lessons.can_preview_course(session, user, lesson.course_id)
    if not staff and not (quiz.is_active and await lessons.is_lesson_public(session, lesson)):
        await callback.answer("Test hozircha mavjud emas.", show_alert=True)
        return
    n_questions = len(await quizzes.list_questions(session, quiz.id))
    used = await quizzes.attempts_used(session, user.id, quiz.id)
    left = quizzes.attempts_left(quiz.max_attempts, used)
    lines = [f"🧪 <b>{esc(quiz.title)}</b>", f"Dars: {esc(lesson.title)}"]
    if quiz.description:
        lines.append(f"\n{esc(quiz.description)}\n")
    lines.append(f"Savollar: {n_questions}")
    lines.append(f"O‘tish chegarasi: {quiz.pass_percent}%")
    if quiz.time_limit_seconds:
        lines.append(f"Vaqt: {quiz.time_limit_seconds // 60} daqiqa")
    lines.append(
        f"Urinishlar: {used}" + (f" / {quiz.max_attempts}" if quiz.max_attempts else " (cheklanmagan)")
    )

    rows: list[list[InlineKeyboardButton]] = []
    can_take = await quizzes.can_take_quiz(session, user, quiz, lesson)
    if not can_take:
        if quizzes.effective_mode(quiz, lesson) == QuizAccessMode.PAID:
            lines.append("\n🔒 Bu test uchun faol obuna kerak.")
        else:
            lines.append("\n🆓 Testni ishlash uchun avval shu bepul darsni oching.")
        rows.append([btn("📘 Darsni ochish", CtLes(action="open", lesson_id=lesson.id))])
    elif left == 0:
        lines.append("\nUrinishlar soni tugagan.")
    else:
        rows.append([btn("▶️ Boshlash / davom ettirish", CtQz(action="start", quiz_id=quiz.id))])
    rows.append([btn("⬅️ Testlar", CtList(kind="quizzes"))])
    await show(callback, "\n".join(lines), markup(rows))
    await callback.answer()


# --------------------------------------------------------------------------- question flow


async def render_question(
    session: AsyncSession, state: FSMContext, attempt_id: int, user: User
) -> tuple[str, InlineKeyboardMarkup | None, bool]:
    """(text, keyboard, finished). Never includes which options are correct."""
    attempt = await quizzes.get_user_attempt(session, user, attempt_id)
    if attempt.status == AttemptStatus.IN_PROGRESS and quizzes.is_overdue(attempt):
        await quizzes.finish_attempt(session, attempt, AttemptStatus.EXPIRED)
    cq = await quizzes.current_question(session, attempt)
    if cq is None:
        if attempt.status == AttemptStatus.IN_PROGRESS:
            await quizzes.finish_attempt(session, attempt, AttemptStatus.COMPLETED)
        await state.set_state(None)
        text, kb = await render_summary(session, user, attempt.id)
        return text, kb, True

    quiz = await quizzes.get_quiz(session, attempt.quiz_id)
    head = f"🧪 <b>{esc(quiz.title)}</b>\nSavol {cq.index + 1}/{cq.total}"
    if attempt.deadline_at:
        left = int((attempt.deadline_at - utcnow()).total_seconds())
        head += f" · ⏱ {_fmt_seconds(left)}"
    q = cq.question
    body = f"{head}\n\n{esc(truncate(q.text, 2000))}"
    rows: list[list[InlineKeyboardButton]] = []
    data_key = _selection_key(attempt.id, q.id)

    if q.type == QuestionType.TEXT:
        body += "\n\n✍️ Javobingizni xabar qilib yozib yuboring."
        await state.set_state(CtQuizAnswer.text)
        await state.update_data(ct_attempt=attempt.id, ct_question=q.id)
    else:
        await state.set_state(None)
        selected = set((await state.get_data()).get(data_key, []))
        body += "\n"
        for n, opt in enumerate(cq.options, start=1):
            body += f"\n<b>{n})</b> {esc(truncate(opt.text, quizzes.CHOICE_TEXT_MAX))}"
        if q.type == QuestionType.SINGLE:
            buttons = [
                btn(str(n), CtQz(action="pick", attempt_id=attempt.id, question_id=q.id, option_id=opt.id))
                for n, opt in enumerate(cq.options, start=1)
            ]
        else:
            body += "\n\nBir nechta to‘g‘ri javob bo‘lishi mumkin. Belgilab, «Javobni yuborish»ni bosing."
            buttons = [
                btn(
                    f"{'☑️' if opt.id in selected else '⬜'} {n}",
                    CtQz(action="toggle", attempt_id=attempt.id, question_id=q.id, option_id=opt.id),
                )
                for n, opt in enumerate(cq.options, start=1)
            ]
        rows += [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        if q.type == QuestionType.MULTIPLE:
            rows.append(
                [btn("✅ Javobni yuborish", CtQz(action="submit", attempt_id=attempt.id, question_id=q.id))]
            )
    rows.append([btn("⏹ Testni yakunlash", CtQz(action="finish", attempt_id=attempt.id))])
    return body, markup(rows), False


@router.callback_query(CtQz.filter(F.action == "start"))
async def start_quiz(
    callback: CallbackQuery, callback_data: CtQz, session: AsyncSession, user: User, state: FSMContext
) -> None:
    attempt = await quizzes.start_or_resume(session, user, callback_data.quiz_id)
    await session.commit()  # release the users-row lock
    text, kb, _ = await render_question(session, state, attempt.id, user)
    await show(callback, text, kb)
    await callback.answer()


async def _after_answer(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    outcome: quizzes.AnswerOutcome,
) -> None:
    await session.commit()
    if outcome.message and not outcome.accepted:
        await callback.answer(outcome.message, show_alert=outcome.finished)
    else:
        await callback.answer()
    text, kb, _ = await render_question(session, state, outcome.attempt.id, user)
    await show(callback, text, kb)


@router.callback_query(CtQz.filter(F.action == "pick"))
async def pick_option(
    callback: CallbackQuery, callback_data: CtQz, session: AsyncSession, user: User, state: FSMContext
) -> None:
    outcome = await quizzes.submit_answer(
        session,
        user,
        callback_data.attempt_id,
        callback_data.question_id,
        option_ids=[callback_data.option_id],
    )
    await _after_answer(callback, session, user, state, outcome)


@router.callback_query(CtQz.filter(F.action == "toggle"))
async def toggle_option(
    callback: CallbackQuery, callback_data: CtQz, session: AsyncSession, user: User, state: FSMContext
) -> None:
    key = _selection_key(callback_data.attempt_id, callback_data.question_id)
    selected = set((await state.get_data()).get(key, []))
    selected ^= {callback_data.option_id}
    await state.update_data({key: sorted(selected)})
    text, kb, _ = await render_question(session, state, callback_data.attempt_id, user)
    await show(callback, text, kb)
    await callback.answer()


@router.callback_query(CtQz.filter(F.action == "submit"))
async def submit_multiple(
    callback: CallbackQuery, callback_data: CtQz, session: AsyncSession, user: User, state: FSMContext
) -> None:
    key = _selection_key(callback_data.attempt_id, callback_data.question_id)
    data = await state.get_data()
    selected = list(data.get(key, []))
    if not selected:
        await callback.answer("Kamida bitta variantni belgilang.", show_alert=True)
        return
    outcome = await quizzes.submit_answer(
        session, user, callback_data.attempt_id, callback_data.question_id, option_ids=selected
    )
    if outcome.accepted:
        data.pop(key, None)
        await state.set_data(data)
    await _after_answer(callback, session, user, state, outcome)


@router.message(CtQuizAnswer.text, F.text)
async def text_answer(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    attempt_id, question_id = data.get("ct_attempt"), data.get("ct_question")
    if not attempt_id or not question_id:
        await state.set_state(None)
        return
    outcome = await quizzes.submit_answer(session, user, int(attempt_id), int(question_id), text=message.text)
    await session.commit()
    if outcome.message and not outcome.accepted:
        await message.answer(esc(outcome.message))
    text, kb, _ = await render_question(session, state, outcome.attempt.id, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtQz.filter(F.action == "finish"))
async def finish_quiz(
    callback: CallbackQuery, callback_data: CtQz, session: AsyncSession, user: User, state: FSMContext
) -> None:
    attempt = await quizzes.finish_early(session, user, callback_data.attempt_id)
    await session.commit()
    await state.set_state(None)
    text, kb = await render_summary(session, user, attempt.id)
    await show(callback, text, kb)
    await callback.answer()


# --------------------------------------------------------------------------- summary


async def render_summary(
    session: AsyncSession, user: User, attempt_id: int, *, with_review: bool = True
) -> tuple[str, InlineKeyboardMarkup]:
    s = await quizzes.attempt_summary(session, user, attempt_id)
    a = s.attempt
    lines = [
        f"🧪 <b>{esc(s.quiz.title)}</b>",
        f"Urinish №{a.attempt_no}: {STATUS_TEXT.get(a.status, a.status)}",
    ]
    if s.show_score:
        verdict = "✅ O‘tdingiz" if a.passed else "❌ O‘tmadingiz"
        lines.append(f"Ball: <b>{fmt_score(a.score)}</b> / {fmt_score(a.max_score)}")
        lines.append(f"Natija: <b>{fmt_score(a.percent)}%</b> — {verdict} (chegara {s.quiz.pass_percent}%)")
    else:
        lines.append("Natijangiz saqlandi. Ballar administrator qaroriga ko‘ra ko‘rsatilmaydi.")
    if s.attempts_left is not None:
        lines.append(f"Qolgan urinishlar: {s.attempts_left}")
    rows = []
    if s.show_answers and with_review:
        rows.append([btn("📋 Javoblarni ko‘rish", CtQz(action="result", attempt_id=a.id))])
    rows.append([btn("🧪 Testga qaytish", CtQz(action="card", quiz_id=s.quiz.id))])
    return "\n".join(lines), markup(rows)


def render_review(items: list[quizzes.ReviewItem]) -> list[str]:
    """Per-question review (only called when result_visibility allows answers)."""
    blocks: list[str] = []
    for n, item in enumerate(items, start=1):
        q, ans = item.question, item.answer
        ok = ans is not None and ans.is_correct
        block = f"{'✅' if ok else '❌'} <b>{n}.</b> {esc(truncate(q.text, 1200))}\n"
        if q.type == QuestionType.TEXT:
            given = esc(truncate(ans.text_answer, 300)) if ans and ans.text_answer else "—"
            correct = ", ".join(esc(o.text) for o in item.options if o.is_correct)
        else:
            chosen = set(ans.selected_option_ids or []) if ans else set()
            given = ", ".join(esc(o.text) for o in item.options if o.id in chosen) or "—"
            correct = ", ".join(esc(o.text) for o in item.options if o.is_correct)
        block += f"Sizning javobingiz: {given}\nTo‘g‘ri javob: {correct}"
        if ans is not None and ans.points_awarded is not None:
            block += f"\nBall: {fmt_score(ans.points_awarded)} / {q.points}"
        if q.explanation:
            block += f"\n<i>{esc(truncate(q.explanation, 800))}</i>"
        blocks.append(block)
    return blocks


@router.callback_query(CtQz.filter(F.action == "result"))
async def show_review(
    callback: CallbackQuery, callback_data: CtQz, bot: Bot, session: AsyncSession, user: User
) -> None:
    s = await quizzes.attempt_summary(session, user, callback_data.attempt_id)
    if not s.show_answers:
        await callback.answer("Javoblar ko‘rsatilmaydi.", show_alert=True)
        return
    await callback.answer()
    # Blocks are escaped HTML: group whole blocks per message, never split inside one.
    message, blocks = "", render_review(s.review) or ["Savollar topilmadi."]
    for block in blocks:
        if message and len(message) + len(block) > REVIEW_MESSAGE_MAX:
            await bot.send_message(callback.from_user.id, message)
            message = ""
        message = f"{message}\n\n{block}" if message else block
    await bot.send_message(callback.from_user.id, message)
