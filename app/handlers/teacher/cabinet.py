"""Teacher cabinet in the bot (TZ §7.2, §7.4, §13.2). OWNER: AUTH.

Every handler calls `require_teacher` (via `_ctx`) before touching any data: the session is
read from the DB on every update, so expiry / admin revocation / logout take effect on the
very next button press: after 24h (or logout / admin revoke) nothing works until the teacher
logs in again. Login happens in the chat; the password messages are deleted at once.
"""

import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AttemptStatus,
    Course,
    CourseEnrollment,
    Lesson,
    LessonAccess,
    Quiz,
    QuizAttempt,
    TeacherCourseAssignment,
    User,
)
from app.keyboards import menus
from app.keyboards.auth import AuCabinetCb, pager_row
from app.services import auth, teacher_sessions
from app.services.errors import ServiceError, TeacherSessionRequired
from app.services.teacher_sessions import TeacherContext
from app.utils.text import esc, truncate
from app.utils.time import fmt_date, fmt_dt

router = Router(name="teacher.cabinet")

PAGE = 10
FINISHED_STATUSES = (AttemptStatus.COMPLETED, AttemptStatus.EXPIRED, AttemptStatus.INTERRUPTED)

# Temporary password between "first login" and "new password" steps: process memory only,
# never Redis/DB, dropped after PENDING_TTL seconds.
PENDING_TTL = 300
_pending_first_login: dict[int, tuple[str, str, float]] = {}


class AuLogin(StatesGroup):
    login = State()
    password = State()
    new_password = State()


class AuPassword(StatesGroup):
    current = State()
    new = State()


# Current password between the two steps of a voluntary change: process memory only.
_pending_change: dict[int, tuple[str, float]] = {}


async def _relogin(event: Message | CallbackQuery, text: str | None = None) -> None:
    text = text or TeacherSessionRequired.default_message
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    if message is not None:
        await message.answer(
            f"🔐 {text}\n\nKirish uchun «{menus.BTN_T_LOGIN}» tugmasini bosing.",
            reply_markup=menus.teacher_menu(logged_in=False),
        )


async def _ctx(event: Message | CallbackQuery, session: AsyncSession, user: User) -> TeacherContext | None:
    """Valid teacher context, or None after telling the user to log in again."""
    try:
        return await teacher_sessions.require_teacher(session, user)
    except TeacherSessionRequired as exc:
        await _relogin(event, exc.user_message)
        return None


# --------------------------------------------------------------------------- entry / login / logout


@router.message(F.text.in_({menus.BTN_TEACHER_ENTRY, menus.BTN_TEACHER_MENU, menus.BTN_T_LOGIN}))
async def cabinet_entry(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    ctx = await teacher_sessions.get_active_context(session, user.telegram_id)
    if ctx is not None and ctx.teacher.user_id == user.id:
        await message.answer(
            f"👨‍🏫 <b>{esc(ctx.teacher.full_name)}</b>, kabinetga xush kelibsiz.\n"
            f"Sessiya <b>{fmt_dt(ctx.session.expires_at)}</b> gacha amal qiladi.",
            reply_markup=menus.teacher_menu(logged_in=True),
        )
        return
    await state.set_state(AuLogin.login)
    await message.answer(
        "🔐 O‘qituvchi kabinetiga kirish.\n\nLoginingizni yuboring (bekor qilish: /menu).",
        reply_markup=menus.teacher_menu(logged_in=False),
    )


async def _delete_secret(message: Message) -> None:
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - e.g. already deleted
        pass


@router.message(AuLogin.login, F.text)
async def login_step(message: Message, state: FSMContext) -> None:
    await state.update_data(au_login=(message.text or "").strip()[:64])
    await state.set_state(AuLogin.password)
    await message.answer("Parolni yuboring. Xavfsizlik uchun parol xabari darhol o‘chiriladi.")


async def _finish_login(message: Message, state: FSMContext, ctx: TeacherContext) -> None:
    await state.clear()
    await message.answer(
        f"✅ Xush kelibsiz, <b>{esc(ctx.teacher.full_name)}</b>!\n"
        f"Sessiya <b>{fmt_dt(ctx.session.expires_at)}</b> gacha amal qiladi. Shundan so‘ng "
        "login va parol qayta so‘raladi.",
        reply_markup=menus.teacher_menu(logged_in=True),
    )


@router.message(AuLogin.password, F.text)
async def password_step(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    password = message.text or ""
    await _delete_secret(message)
    login = (await state.get_data()).get("au_login", "")
    try:
        result = await auth.login(session, user=user, login=login, password=password)
    except auth.PasswordChangeRequired:
        _pending_first_login[user.telegram_id] = (login, password, time.monotonic() + PENDING_TTL)
        await state.set_state(AuLogin.new_password)
        await message.answer(
            "Birinchi kirish: vaqtinchalik parolni almashtiring.\n"
            f"Yangi parolni yuboring (kamida {auth.PASSWORD_MIN_LENGTH} belgi, harf va raqam)."
        )
        return
    except ServiceError as exc:
        await state.set_state(AuLogin.login)
        await message.answer(f"❌ {exc.user_message}\n\nLoginni qaytadan yuboring yoki /menu.")
        return
    await _finish_login(message, state, result.context)


@router.message(AuLogin.new_password, F.text)
async def new_password_step(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    new_password = message.text or ""
    await _delete_secret(message)
    pending = _pending_first_login.pop(user.telegram_id, None)
    if pending is None or pending[2] < time.monotonic():
        await state.set_state(AuLogin.login)
        await message.answer("Vaqt tugadi. Loginni qaytadan yuboring.")
        return
    login, password, _ = pending
    try:
        result = await auth.login(
            session, user=user, login=login, password=password, new_password=new_password
        )
    except ServiceError as exc:
        if not isinstance(exc, auth.LoginFailed):
            _pending_first_login[user.telegram_id] = pending
            await message.answer(f"❌ {exc.user_message}\n\nBoshqa yangi parol yuboring.")
            return
        await state.set_state(AuLogin.login)
        await message.answer(f"❌ {exc.user_message}\n\nLoginni qaytadan yuboring.")
        return
    await _finish_login(message, state, result.context)


@router.message(Command("logout"))
@router.message(F.text == menus.BTN_T_LOGOUT)
async def cmd_logout(message: Message, session: AsyncSession, user: User) -> None:
    if await teacher_sessions.logout(session, user):
        await message.answer(
            "🔒 Sessiya yakunlandi. Kabinetga qayta kirish uchun login va parol kerak bo‘ladi.",
            reply_markup=menus.teacher_menu(logged_in=False),
        )
    else:
        await message.answer("Faol o‘qituvchi sessiyasi yo‘q.")


@router.message(F.text == menus.BTN_T_PASSWORD)
async def password_change_start(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    if await _ctx(message, session, user) is None:
        return
    await state.set_state(AuPassword.current)
    await message.answer("🔑 Joriy parolingizni yuboring (xabar darhol o‘chiriladi). Bekor qilish: /menu")


@router.message(AuPassword.current, F.text)
async def password_change_current(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    current = message.text or ""
    await _delete_secret(message)
    if await _ctx(message, session, user) is None:
        await state.clear()
        return
    _pending_change[user.telegram_id] = (current, time.monotonic() + PENDING_TTL)
    await state.set_state(AuPassword.new)
    await message.answer(
        f"Yangi parolni yuboring (kamida {auth.PASSWORD_MIN_LENGTH} belgi, harf va raqam). "
        "Xabar darhol o‘chiriladi."
    )


@router.message(AuPassword.new, F.text)
async def password_change_new(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    new_password = message.text or ""
    await _delete_secret(message)
    pending = _pending_change.pop(user.telegram_id, None)
    ctx = await _ctx(message, session, user)
    if ctx is None:
        await state.clear()
        return
    if pending is None or pending[1] < time.monotonic():
        await state.clear()
        await message.answer("Vaqt tugadi. «🔑 Parolni almashtirish» ni qaytadan bosing.")
        return
    try:
        result = await auth.change_password(
            session, ctx, user, current_password=pending[0], new_password=new_password
        )
    except ServiceError as exc:
        await state.clear()
        await message.answer(f"❌ {exc.user_message}\n\n«🔑 Parolni almashtirish» ni qaytadan bosing.")
        return
    await state.clear()
    await message.answer(
        "✅ Parol almashtirildi. Yangi sessiya "
        f"<b>{fmt_dt(result.context.session.expires_at)}</b> gacha amal qiladi.",
        reply_markup=menus.teacher_menu(logged_in=True),
    )


# --------------------------------------------------------------------------- my courses


async def _courses(session: AsyncSession, ctx: TeacherContext) -> list[tuple[Course, dict[str, bool]]]:
    rows = await session.execute(
        select(Course, TeacherCourseAssignment)
        .join(TeacherCourseAssignment, TeacherCourseAssignment.course_id == Course.id)
        .where(TeacherCourseAssignment.teacher_id == ctx.teacher.id)
        .order_by(Course.title)
    )
    return [
        (
            course,
            {r: bool(getattr(assignment, r)) for r in sorted(teacher_sessions.COURSE_RIGHTS)},
        )
        for course, assignment in rows.all()
    ]


RIGHT_LABELS = {
    "can_manage_content": "materiallar",
    "can_manage_quizzes": "testlar",
    "can_view_students": "o‘quvchilar",
    "can_view_results": "natijalar",
    "can_chat": "chat",
}


@router.message(F.text == menus.BTN_T_MY_COURSES)
async def my_courses(message: Message, session: AsyncSession, user: User) -> None:
    ctx = await _ctx(message, session, user)
    if ctx is None:
        return
    courses = await _courses(session, ctx)
    if not courses:
        await message.answer("Sizga hali kurs biriktirilmagan. Administratorga murojaat qiling.")
        return
    lines = ["<b>📚 Mening kurslarim</b>", ""]
    for course, rights in courses:
        allowed = ", ".join(RIGHT_LABELS[r] for r, v in rights.items() if v) or "—"
        status = "" if course.is_active else " (nofaol)"
        lines.append(f"• <b>{esc(course.title)}</b>{status}\n   Ruxsatlar: {allowed}")
    await message.answer("\n".join(lines))


# --------------------------------------------------------------------------- students


def _course_picker(courses: list[Course], action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=truncate(c.title, 60),
                    callback_data=AuCabinetCb(action=action, course_id=c.id).pack(),
                )
            ]
            for c in courses
        ]
    )


async def _courses_with_right(session: AsyncSession, ctx: TeacherContext, right: str) -> list[Course]:
    ids = await teacher_sessions.teacher_course_ids(session, ctx.teacher.id, right)
    if not ids:
        return []
    return list(
        (await session.scalars(select(Course).where(Course.id.in_(ids)).order_by(Course.title))).all()
    )


@router.message(F.text == menus.BTN_T_STUDENTS)
async def students_entry(message: Message, session: AsyncSession, user: User) -> None:
    ctx = await _ctx(message, session, user)
    if ctx is None:
        return
    courses = await _courses_with_right(session, ctx, "can_view_students")
    if not courses:
        await message.answer("O‘quvchilarni ko‘rish uchun ruxsat berilgan kurs yo‘q.")
        return
    await message.answer("👨‍🎓 Kursni tanlang:", reply_markup=_course_picker(courses, "students"))


@router.callback_query(AuCabinetCb.filter(F.action == "students"))
async def students_list(
    callback: CallbackQuery, callback_data: AuCabinetCb, session: AsyncSession, user: User
) -> None:
    ctx = await _ctx(callback, session, user)
    if ctx is None:
        return
    await teacher_sessions.require_course_right(session, ctx, callback_data.course_id, "can_view_students")
    page = max(callback_data.page, 0)
    course = await session.get(Course, callback_data.course_id)
    total = await session.scalar(
        select(func.count(CourseEnrollment.id)).where(CourseEnrollment.course_id == callback_data.course_id)
    )
    rows = (
        await session.execute(
            select(User, CourseEnrollment.created_at)
            .join(CourseEnrollment, CourseEnrollment.user_id == User.id)
            .where(CourseEnrollment.course_id == callback_data.course_id)
            .order_by(CourseEnrollment.created_at.desc())
            .limit(PAGE + 1)
            .offset(page * PAGE)
        )
    ).all()
    has_next = len(rows) > PAGE
    rows = rows[:PAGE]
    buttons = [
        [
            InlineKeyboardButton(
                text=truncate(f"{student.full_name} · {fmt_date(enrolled_at)}", 60),
                callback_data=AuCabinetCb(
                    action="student", course_id=callback_data.course_id, user_id=student.id
                ).pack(),
            )
        ]
        for student, enrolled_at in rows
    ]
    nav = pager_row(
        page,
        has_next,
        lambda p: AuCabinetCb(action="students", course_id=callback_data.course_id, page=p).pack(),
    )
    if nav:
        buttons.append(nav)
    text = f"👨‍🎓 <b>{esc(course.title if course else '')}</b> — o‘quvchilar: {total or 0}"
    if not rows:
        text += "\n\nHozircha o‘quvchilar yo‘q."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(AuCabinetCb.filter(F.action == "student"))
async def student_detail(
    callback: CallbackQuery, callback_data: AuCabinetCb, session: AsyncSession, user: User
) -> None:
    ctx = await _ctx(callback, session, user)
    if ctx is None:
        return
    course_id = callback_data.course_id
    await teacher_sessions.require_course_right(session, ctx, course_id, "can_view_students")
    row = (
        await session.execute(
            select(User, CourseEnrollment.created_at)
            .join(CourseEnrollment, CourseEnrollment.user_id == User.id)
            .where(CourseEnrollment.course_id == course_id, User.id == callback_data.user_id)
        )
    ).first()
    if row is None:
        await callback.answer("O‘quvchi topilmadi.", show_alert=True)
        return
    student, enrolled_at = row
    opened = await session.scalar(
        select(func.count(LessonAccess.id))
        .join(Lesson, Lesson.id == LessonAccess.lesson_id)
        .where(LessonAccess.user_id == student.id, Lesson.course_id == course_id)
    )
    total_lessons = await session.scalar(
        select(func.count(Lesson.id)).where(Lesson.course_id == course_id, Lesson.is_active.is_(True))
    )
    username = f"@{esc(student.username)}" if student.username else "—"
    lines = [
        f"👤 <b>{esc(student.full_name)}</b>",
        f"Username: {username}",
        f"Kursga qo‘shilgan: {fmt_date(enrolled_at)}",
        f"Ochilgan darslar: {opened or 0} / {total_lessons or 0}",
    ]
    if course_id in await teacher_sessions.teacher_course_ids(session, ctx.teacher.id, "can_view_results"):
        attempts = (
            await session.execute(
                select(Quiz.title, QuizAttempt.percent, QuizAttempt.passed, QuizAttempt.finished_at)
                .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
                .join(Lesson, Lesson.id == Quiz.lesson_id)
                .where(
                    QuizAttempt.user_id == student.id,
                    Lesson.course_id == course_id,
                    QuizAttempt.status.in_(FINISHED_STATUSES),
                )
                .order_by(QuizAttempt.finished_at.desc())
                .limit(10)
            )
        ).all()
        lines.append("")
        lines.append("<b>🧪 So‘nggi test natijalari</b>")
        if not attempts:
            lines.append("—")
        for title, percent, passed, finished_at in attempts:
            mark = "✅" if passed else "❌"
            lines.append(f"{mark} {esc(truncate(title, 40))}: {percent or 0}% · {fmt_date(finished_at)}")
    back = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Ro‘yxatga",
                    callback_data=AuCabinetCb(action="students", course_id=course_id).pack(),
                )
            ]
        ]
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=back)
    await callback.answer()


# --------------------------------------------------------------------------- results


@router.message(F.text == menus.BTN_T_RESULTS)
async def results_entry(message: Message, session: AsyncSession, user: User) -> None:
    ctx = await _ctx(message, session, user)
    if ctx is None:
        return
    courses = await _courses_with_right(session, ctx, "can_view_results")
    if not courses:
        await message.answer("Natijalarni ko‘rish uchun ruxsat berilgan kurs yo‘q.")
        return
    await message.answer("📊 Kursni tanlang:", reply_markup=_course_picker(courses, "results"))


@router.callback_query(AuCabinetCb.filter(F.action == "results"))
async def results_list(
    callback: CallbackQuery, callback_data: AuCabinetCb, session: AsyncSession, user: User
) -> None:
    ctx = await _ctx(callback, session, user)
    if ctx is None:
        return
    course_id = callback_data.course_id
    await teacher_sessions.require_course_right(session, ctx, course_id, "can_view_results")
    page = max(callback_data.page, 0)
    course = await session.get(Course, course_id)
    base = (
        select(QuizAttempt, Quiz.title, User)
        .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
        .join(Lesson, Lesson.id == Quiz.lesson_id)
        .join(User, User.id == QuizAttempt.user_id)
        .where(Lesson.course_id == course_id, QuizAttempt.status.in_(FINISHED_STATUSES))
    )
    stats = (
        await session.execute(
            select(
                func.count(QuizAttempt.id),
                func.avg(QuizAttempt.percent),
                func.count(QuizAttempt.id).filter(QuizAttempt.passed.is_(True)),
            )
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Lesson, Lesson.id == Quiz.lesson_id)
            .where(Lesson.course_id == course_id, QuizAttempt.status.in_(FINISHED_STATUSES))
        )
    ).one()
    rows = (
        await session.execute(
            base.order_by(QuizAttempt.finished_at.desc()).limit(PAGE + 1).offset(page * PAGE)
        )
    ).all()
    has_next = len(rows) > PAGE
    total, avg, passed = stats
    lines = [
        f"📊 <b>{esc(course.title if course else '')}</b> — test natijalari",
        f"Urinishlar: {total or 0} · o‘rtacha: {round(float(avg or 0), 1)}% · o‘tgan: {passed or 0}",
        "",
    ]
    for attempt, quiz_title, student in rows[:PAGE]:
        mark = "✅" if attempt.passed else "❌"
        lines.append(
            f"{mark} {esc(truncate(student.full_name, 30))} — {esc(truncate(quiz_title, 30))}: "
            f"{attempt.percent or 0}% · {fmt_date(attempt.finished_at)}"
        )
    if not rows:
        lines.append("Hozircha natijalar yo‘q.")
    nav = pager_row(
        page, has_next, lambda p: AuCabinetCb(action="results", course_id=course_id, page=p).pack()
    )
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    )
    await callback.answer()
