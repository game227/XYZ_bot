"""🧪 Testlar (admin) and the shared quiz editor (TZ §8.1, §8.2).

`CtQe` callbacks are used by admins (QUIZZES permission) and teachers (active session +
`can_manage_quizzes` on the course); the service re-checks rights on every call.

Question input format (one message per step):
- question text;
- options, one per line, correct ones prefixed with «+» (TEXT questions: every line is an
  accepted answer).
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AdminPermission,
    QuestionType,
    QuizAccessMode,
    QuizResultVisibility,
    User,
)
from app.keyboards import menus
from app.keyboards.content import CtEd, CtQe, active_mark, btn, markup, show
from app.services import lessons, quizzes
from app.services.access import AdminFilter
from app.services.errors import InvalidInput
from app.utils.text import esc, truncate

router = Router(name="admin.quizzes")

ACCESS_NAMES = {
    QuizAccessMode.INHERIT: "darsga bog‘liq",
    QuizAccessMode.FREE: "hammaga bepul",
    QuizAccessMode.PAID: "faqat obunachilar",
}
VISIBILITY_NAMES = {
    QuizResultVisibility.HIDDEN: "natija yashirin",
    QuizResultVisibility.SCORE_ONLY: "faqat ball",
    QuizResultVisibility.SCORE_AND_ANSWERS: "ball + to‘g‘ri javoblar",
    QuizResultVisibility.ANSWERS_AFTER_LAST_ATTEMPT: "javoblar oxirgi urinishdan keyin",
}
TYPE_NAMES = {
    QuestionType.SINGLE: "bitta to‘g‘ri javob",
    QuestionType.MULTIPLE: "bir nechta to‘g‘ri javob",
    QuestionType.TEXT: "matnli javob",
}
SET_PROMPTS = {
    "title": "Test nomini yuboring:",
    "description": "Test tavsifini yuboring (bo‘shatish uchun «-»):",
    "pass_percent": "O‘tish chegarasini foizda yuboring (0–100):",
    "time_limit": "Vaqt chegarasini daqiqada yuboring (0 — cheklovsiz):",
    "max_attempts": "Ruxsat etilgan urinishlar sonini yuboring (0 — cheklovsiz):",
}
FLAG_NAMES = {
    "shuffle_questions": "Savollarni aralashtirish",
    "shuffle_options": "Variantlarni aralashtirish",
    "partial_credit": "Qisman ball (ko‘p javobli)",
}


class CtQuizEdit(StatesGroup):
    new_title = State()
    field = State()
    question_text = State()
    question_options = State()


def _on(value: bool) -> str:
    return "✅" if value else "▫️"


# --------------------------------------------------------------------------- rendering


async def render_courses(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    course_ids = await lessons.manageable_course_ids(session, user, quizzes=True)
    if course_ids is not None and not course_ids:
        if user.role != "student":
            from app.services import teacher_sessions

            await teacher_sessions.require_teacher(session, user)
        return "Sizga testlarni boshqarish uchun kurs biriktirilmagan.", None
    courses = await lessons.list_courses(session, include_inactive=True, course_ids=course_ids)
    if not courses:
        return "Hozircha kurslar yo‘q.", None
    rows = [[btn(f"🎓 {c.title}{active_mark(c.is_active)}", CtQe(action="course", id=c.id))] for c in courses]
    return "🧪 <b>Testlarni boshqarish</b>\n\nKursni tanlang:", markup(rows)


async def render_course(
    session: AsyncSession, user: User, course_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    course = await lessons.get_course(session, course_id)
    await lessons.ensure_can_manage_course(session, user, course.id, quizzes=True)
    items = await lessons.list_course_lessons(session, course.id)
    rows = [
        [btn(f"📘 {lsn.title}{active_mark(lsn.is_active)}", CtQe(action="lesson", id=lsn.id))]
        for lsn in items
    ]
    rows.append([btn("⬅️ Kurslar", CtQe(action="courses"))])
    text = f"🎓 <b>{esc(course.title)}</b>\n\n" + (
        "Darsni tanlang:" if items else "Bu kursda hali darslar yo‘q."
    )
    return text, markup(rows)


async def render_lesson(
    session: AsyncSession, user: User, lesson_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    lesson = await lessons.get_lesson(session, lesson_id)
    await lessons.ensure_can_manage_course(session, user, lesson.course_id, quizzes=True)
    items = await quizzes.list_lesson_quizzes(session, lesson.id, include_inactive=True)
    rows = [[btn(f"🧪 {q.title}{active_mark(q.is_active)}", CtQe(action="view", id=q.id))] for q in items]
    rows.append([btn("➕ Test qo‘shish", CtQe(action="new", id=lesson.id))])
    rows.append(
        [
            btn("⬅️ Darslar", CtQe(action="course", id=lesson.course_id)),
            btn("📘 Dars sozlamalari", CtEd(action="view", kind="lesson", id=lesson.id)),
        ]
    )
    text = f"📘 <b>{esc(lesson.title)}</b> — testlar\n\n🚫 — nofaol (o‘quvchilarga ko‘rinmaydi)."
    return text, markup(rows)


async def render_quiz(session: AsyncSession, user: User, quiz_id: int) -> tuple[str, InlineKeyboardMarkup]:
    quiz, lesson = await quizzes.get_managed_quiz(session, user, quiz_id)
    questions = await quizzes.list_questions(session, quiz.id)
    max_score = sum(q.points for q in questions)
    lines = [
        f"🧪 <b>{esc(quiz.title)}</b>{active_mark(quiz.is_active)}",
        f"Dars: {esc(lesson.title)}",
        "Holat: " + ("✅ faol" if quiz.is_active else "🚫 nofaol"),
    ]
    if quiz.description:
        lines.append(f"\n{esc(truncate(quiz.description, 1000))}\n")
    lines += [
        f"Savollar: {len(questions)} · maksimal ball: {max_score}",
        f"O‘tish chegarasi: {quiz.pass_percent}%",
        "Vaqt: " + (f"{quiz.time_limit_seconds // 60} daqiqa" if quiz.time_limit_seconds else "cheklanmagan"),
        "Urinishlar: " + (str(quiz.max_attempts) if quiz.max_attempts else "cheklanmagan"),
        f"Kirish: {ACCESS_NAMES[QuizAccessMode(quiz.access_mode)]}",
        f"Natija: {VISIBILITY_NAMES[QuizResultVisibility(quiz.result_visibility)]}",
    ]
    rows: list[list[InlineKeyboardButton]] = [
        [btn(f"❓ Savollar ({len(questions)})", CtQe(action="qlist", id=quiz.id))],
        [
            btn("✏️ Nom", CtQe(action="set", id=quiz.id, arg="title")),
            btn("✏️ Tavsif", CtQe(action="set", id=quiz.id, arg="description")),
        ],
        [
            btn("🎯 O‘tish %", CtQe(action="set", id=quiz.id, arg="pass_percent")),
            btn("⏱ Vaqt", CtQe(action="set", id=quiz.id, arg="time_limit")),
            btn("🔁 Urinishlar", CtQe(action="set", id=quiz.id, arg="max_attempts")),
        ],
    ]
    for flag, name in FLAG_NAMES.items():
        rows.append([btn(f"{_on(getattr(quiz, flag))} {name}", CtQe(action="flag", id=quiz.id, arg=flag))])
    settings_row = [btn("📊 Natija ko‘rinishi", CtQe(action="cycle", id=quiz.id, arg="result_visibility"))]
    if quizzes.can_set_access_mode(user):
        settings_row.insert(0, btn("🔐 Kirish turi", CtQe(action="cycle", id=quiz.id, arg="access_mode")))
    rows.append(settings_row)
    rows.append(
        [
            btn(
                "🚫 Nofaol qilish" if quiz.is_active else "✅ Faollashtirish",
                CtQe(action="flag", id=quiz.id, arg="is_active"),
            ),
            btn("🗑 O‘chirish", CtQe(action="del", id=quiz.id)),
        ]
    )
    rows.append([btn("⬅️ Dars testlari", CtQe(action="lesson", id=lesson.id))])
    return "\n".join(lines), markup(rows)


async def render_questions(
    session: AsyncSession, user: User, quiz_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    quiz, _ = await quizzes.get_managed_quiz(session, user, quiz_id)
    questions = await quizzes.list_questions(session, quiz.id)
    rows = [
        [btn(f"{n}. {truncate(q.text, 50)}", CtQe(action="qview", id=q.id))]
        for n, q in enumerate(questions, start=1)
    ]
    rows.append([btn("➕ Savol qo‘shish", CtQe(action="qadd", id=quiz.id))])
    rows.append([btn("⬅️ Test", CtQe(action="view", id=quiz.id))])
    text = f"❓ <b>{esc(quiz.title)}</b> — savollar: {len(questions)}"
    return text, markup(rows)


async def render_question(
    session: AsyncSession, user: User, question_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    question, quiz = await quizzes.get_managed_question(session, user, question_id)
    options = (await quizzes.options_for(session, [question.id]))[question.id]
    lines = [
        f"❓ <b>Savol</b> ({TYPE_NAMES[QuestionType(question.type)]}, {question.points} ball)",
        "",
        esc(truncate(question.text, 2000)),
        "",
    ]
    title = (
        "Qabul qilinadigan javoblar:" if question.type == QuestionType.TEXT else "Variantlar (✔️ — to‘g‘ri):"
    )
    lines.append(title)
    for opt in options:
        lines.append(f"{'✔️' if opt.is_correct else '▫️'} {esc(opt.text)}")
    rows = [
        [btn("🗑 O‘chirish", CtQe(action="qdel", id=question.id))],
        [btn("⬅️ Savollar", CtQe(action="qlist", id=quiz.id))],
    ]
    return "\n".join(lines), markup(rows)


async def render(
    session: AsyncSession, user: User, action: str, obj_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    match action:
        case "courses":
            return await render_courses(session, user)
        case "course":
            return await render_course(session, user, obj_id)
        case "lesson":
            return await render_lesson(session, user, obj_id)
        case "view":
            return await render_quiz(session, user, obj_id)
        case "qlist":
            return await render_questions(session, user, obj_id)
        case "qview":
            return await render_question(session, user, obj_id)
    raise InvalidInput()


# --------------------------------------------------------------------------- entry + navigation


@router.message(AdminFilter(AdminPermission.QUIZZES), F.text == menus.BTN_A_QUIZZES)
async def quizzes_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render_courses(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtQe.filter(F.action.in_({"courses", "course", "lesson", "view", "qlist", "qview"})))
async def navigate(callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User) -> None:
    text, kb = await render(session, user, callback_data.action, callback_data.id)
    await show(callback, text, kb)
    await callback.answer()


# --------------------------------------------------------------------------- settings


@router.callback_query(CtQe.filter(F.action == "flag"))
async def toggle_flag(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User
) -> None:
    await quizzes.toggle_quiz_flag(session, user, callback_data.id, callback_data.arg)
    text, kb = await render_quiz(session, user, callback_data.id)
    await show(callback, text, kb)
    await callback.answer("Saqlandi")


@router.callback_query(CtQe.filter(F.action == "cycle"))
async def cycle_option(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User
) -> None:
    await quizzes.cycle_quiz_option(session, user, callback_data.id, callback_data.arg)
    text, kb = await render_quiz(session, user, callback_data.id)
    await show(callback, text, kb)
    await callback.answer("Saqlandi")


async def _prompt(callback: CallbackQuery, text: str) -> None:
    await callback.answer()
    await callback.bot.send_message(callback.from_user.id, f"{text}\n\nBekor qilish: /menu")  # type: ignore[union-attr]


@router.callback_query(CtQe.filter(F.action == "set"))
async def ask_setting(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User, state: FSMContext
) -> None:
    if callback_data.arg not in SET_PROMPTS:
        raise InvalidInput()
    await quizzes.get_managed_quiz(session, user, callback_data.id)
    await state.set_state(CtQuizEdit.field)
    await state.update_data(ct_quiz=callback_data.id, ct_field=callback_data.arg)
    await _prompt(callback, SET_PROMPTS[callback_data.arg])


@router.message(CtQuizEdit.field, F.text)
async def receive_setting(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    quiz_id = int(data.get("ct_quiz") or 0)
    await quizzes.update_quiz_field(session, user, quiz_id, str(data.get("ct_field")), message.text or "")
    await state.clear()
    text, kb = await render_quiz(session, user, quiz_id)
    await message.answer(f"✅ Saqlandi.\n\n{text}", reply_markup=kb)


@router.callback_query(CtQe.filter(F.action == "new"))
async def ask_new_quiz(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User, state: FSMContext
) -> None:
    lesson = await lessons.get_lesson(session, callback_data.id)
    await lessons.ensure_can_manage_course(session, user, lesson.course_id, quizzes=True)
    await state.set_state(CtQuizEdit.new_title)
    await state.update_data(ct_lesson=lesson.id)
    await _prompt(callback, "Yangi test nomini yuboring:")


@router.message(CtQuizEdit.new_title, F.text)
async def receive_new_quiz(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    lesson_id = int((await state.get_data()).get("ct_lesson") or 0)
    quiz = await quizzes.create_quiz(session, user, lesson_id=lesson_id, title=message.text or "")
    await state.clear()
    text, kb = await render_quiz(session, user, quiz.id)
    await message.answer(
        f"✅ Test yaratildi. Savollar qo‘shing va «✅ Faollashtirish»ni bosing.\n\n{text}", reply_markup=kb
    )


# --------------------------------------------------------------------------- questions


@router.callback_query(CtQe.filter(F.action == "qadd"))
async def choose_type(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User
) -> None:
    quiz, _ = await quizzes.get_managed_quiz(session, user, callback_data.id)
    rows = [
        [btn(f"➕ {name}", CtQe(action="qtype", id=quiz.id, arg=t.value))] for t, name in TYPE_NAMES.items()
    ]
    rows.append([btn("⬅️ Savollar", CtQe(action="qlist", id=quiz.id))])
    await show(callback, "Savol turini tanlang:", markup(rows))
    await callback.answer()


@router.callback_query(CtQe.filter(F.action == "qtype"))
async def ask_question_text(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User, state: FSMContext
) -> None:
    qtype = QuestionType(callback_data.arg)
    await quizzes.get_managed_quiz(session, user, callback_data.id)
    await state.set_state(CtQuizEdit.question_text)
    await state.update_data(ct_quiz=callback_data.id, ct_qtype=qtype.value)
    await _prompt(callback, f"Savol matnini yuboring ({TYPE_NAMES[qtype]}):")


@router.message(CtQuizEdit.question_text, F.text)
async def receive_question_text(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    text = (message.text or "").strip()
    if not text or len(text) > quizzes.QUESTION_TEXT_MAX:
        raise InvalidInput(f"Savol matni 1–{quizzes.QUESTION_TEXT_MAX} belgi bo‘lishi kerak.")
    data = await state.get_data()
    await quizzes.get_managed_quiz(session, user, int(data.get("ct_quiz") or 0))
    await state.update_data(ct_qtext=text)
    await state.set_state(CtQuizEdit.question_options)
    if data.get("ct_qtype") == QuestionType.TEXT.value:
        hint = (
            "Qabul qilinadigan to‘g‘ri javoblarni yuboring, har birini yangi qatordan.\n"
            "Katta-kichik harf va apostrof turlari (o‘ / o' / oʻ) farqlanmaydi."
        )
    else:
        hint = (
            "Variantlarni yuboring, har birini yangi qatordan. To‘g‘ri variant(lar) oldiga «+» qo‘ying.\n"
            "Masalan:\n+Toshkent\nSamarqand\nBuxoro"
        )
    await message.answer(f"{esc(hint)}\n\nBekor qilish: /menu")


@router.message(CtQuizEdit.question_options, F.text)
async def receive_question_options(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    data = await state.get_data()
    quiz_id = int(data.get("ct_quiz") or 0)
    qtype = QuestionType(str(data.get("ct_qtype")))
    options = quizzes.parse_options_block(message.text or "", qtype)
    await quizzes.add_question(
        session, user, quiz_id, qtype=qtype, text=str(data.get("ct_qtext") or ""), options=options
    )
    await state.clear()
    text, kb = await render_questions(session, user, quiz_id)
    kb.inline_keyboard.insert(
        0, [btn("➕ Yana savol qo‘shish", CtQe(action="qtype", id=quiz_id, arg=qtype.value))]
    )
    await message.answer(f"✅ Savol qo‘shildi.\n\n{text}", reply_markup=kb)


# --------------------------------------------------------------------------- deletion


@router.callback_query(CtQe.filter(F.action.in_({"qdel", "del"})))
async def ask_delete(callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User) -> None:
    if callback_data.action == "qdel":
        question, quiz = await quizzes.get_managed_question(session, user, callback_data.id)
        text = f"Savol o‘chirilsinmi?\n\n{esc(truncate(question.text, 500))}"
        yes, no = CtQe(action="qdelok", id=question.id), CtQe(action="qview", id=question.id)
    else:
        quiz, _ = await quizzes.get_managed_quiz(session, user, callback_data.id)
        text = f"«{esc(quiz.title)}» testi o‘chirilsinmi?"
        yes, no = CtQe(action="delok", id=quiz.id), CtQe(action="view", id=quiz.id)
    await show(callback, text, markup([[btn("🗑 Ha, o‘chirish", yes)], [btn("⬅️ Bekor qilish", no)]]))
    await callback.answer()


@router.callback_query(CtQe.filter(F.action == "qdelok"))
async def delete_question(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User
) -> None:
    quiz = await quizzes.delete_question(session, user, callback_data.id)
    text, kb = await render_questions(session, user, quiz.id)
    await show(callback, f"🗑 Savol o‘chirildi.\n\n{text}", kb)
    await callback.answer()


@router.callback_query(CtQe.filter(F.action == "delok"))
async def delete_quiz(
    callback: CallbackQuery, callback_data: CtQe, session: AsyncSession, user: User
) -> None:
    lesson_id = await quizzes.delete_quiz(session, user, callback_data.id)
    text, kb = await render_lesson(session, user, lesson_id)
    await show(callback, f"🗑 Test o‘chirildi.\n\n{text}", kb)
    await callback.answer()
