"""Teacher cabinet in the bot (TZ §7.2, §7.4, §13.2). OWNER: AUTH.

Every handler calls `require_teacher` (via `_ctx`) before touching any data: the session is
read from the DB on every update, so expiry / admin revocation / logout take effect on the
very next button press. Login itself happens only in the Mini App (no password in chat).
"""

from aiogram import F, Router
from aiogram.filters import Command
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
from app.keyboards.auth import AuCabinetCb, chats_keyboard, login_keyboard, pager_row
from app.services import chat, teacher_sessions
from app.services.errors import TeacherSessionRequired
from app.services.teacher_sessions import TeacherContext
from app.utils.text import esc, truncate
from app.utils.time import fmt_date, fmt_dt

router = Router(name="teacher.cabinet")

PAGE = 10
FINISHED_STATUSES = (AttemptStatus.COMPLETED, AttemptStatus.EXPIRED, AttemptStatus.INTERRUPTED)
NO_HTTPS_HINT = (
    "\n\n⚠️ Kirish oynasi faqat HTTPS manzil bilan ishlaydi (WEB_BASE_URL). Administratorga murojaat qiling."
)


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
async def cabinet_entry(message: Message, session: AsyncSession, user: User) -> None:
    ctx = await teacher_sessions.get_active_context(session, user.telegram_id)
    if ctx is not None and ctx.teacher.user_id == user.id:
        await message.answer(
            f"👨‍🏫 <b>{esc(ctx.teacher.full_name)}</b>, kabinetga xush kelibsiz.\n"
            f"Sessiya <b>{fmt_dt(ctx.session.expires_at)}</b> gacha amal qiladi.",
            reply_markup=menus.teacher_menu(logged_in=True),
        )
        return
    await message.answer("👨‍🏫 O‘qituvchi kabineti", reply_markup=menus.teacher_menu(logged_in=False))
    text = (
        "🔐 Kabinetga kirish uchun quyidagi tugma orqali kirish oynasini oching va login hamda "
        "parolingizni kiriting.\n\nXavfsizlik uchun parol bot chatiga yozilmaydi. "
        "Sessiya 24 soat amal qiladi; muddat tugagach qayta kirish kerak bo‘ladi."
    )
    keyboard = login_keyboard()
    if keyboard is None:
        text += NO_HTTPS_HINT
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("logout"))
@router.message(F.text == menus.BTN_T_LOGOUT)
async def cmd_logout(message: Message, session: AsyncSession, user: User) -> None:
    if await teacher_sessions.logout(session, user):
        await message.answer(
            "🔒 Sessiya yakunlandi. Kabinet, chatlar va o‘quvchilar ma’lumotlari yopildi.",
            reply_markup=menus.teacher_menu(logged_in=False),
        )
    else:
        await message.answer("Faol o‘qituvchi sessiyasi yo‘q.")


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


# --------------------------------------------------------------------------- chats


@router.message(F.text == menus.BTN_T_CHATS)
async def chats_entry(message: Message, session: AsyncSession, user: User) -> None:
    ctx = await _ctx(message, session, user)
    if ctx is None:
        return
    unread = await chat.unread_threads_count(session, ctx)
    text = (
        "💬 Yozishmalar faqat kabinet oynasida ko‘rsatiladi va sessiya tugashi bilan yopiladi."
        f"\nO‘qilmagan suhbatlar: <b>{unread}</b>"
    )
    keyboard = chats_keyboard()
    if keyboard is None:
        text += NO_HTTPS_HINT
    await message.answer(text, reply_markup=keyboard)


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
