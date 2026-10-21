"""Admin: teacher accounts (TZ §7.1, §3.1, §22.4). OWNER: AUTH.

Create (temporary password, must be changed on first login), block / unblock, reset password,
reset Telegram binding, course assignments with per-course rights. Entry requires the
`teachers` permission; every callback / state handler re-checks it with `require_admin`.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, Course, TeacherAccount, TeacherCourseAssignment, User
from app.keyboards import menus
from app.keyboards.auth import AuAssignCb, AuTeacherCb, pager_row
from app.services import auth, teacher_sessions
from app.services.access import AdminFilter, require_admin
from app.services.errors import InvalidInput
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

logger = logging.getLogger(__name__)

router = Router(name="admin.teachers")

PERM = AdminPermission.TEACHERS
PAGE = 10

RIGHT_LABELS = {
    "can_manage_content": "Materiallarni boshqarish",
    "can_manage_quizzes": "Testlarni boshqarish",
    "can_view_students": "O‘quvchilarni ko‘rish",
    "can_view_results": "Natijalarni ko‘rish",
    "can_chat": "Chat",
}


class AuTeacherCreate(StatesGroup):
    full_name = State()
    login = State()
    telegram_id = State()


CANCEL_ROW = [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=AuTeacherCb(action="cancel").pack())]


# --------------------------------------------------------------------------- list / view


async def _list_markup(session: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await session.scalar(select(func.count(TeacherAccount.id))) or 0
    teachers = (
        await session.scalars(
            select(TeacherAccount).order_by(TeacherAccount.full_name).limit(PAGE + 1).offset(page * PAGE)
        )
    ).all()
    has_next = len(teachers) > PAGE
    buttons = [
        [InlineKeyboardButton(text="➕ Yangi o‘qituvchi", callback_data=AuTeacherCb(action="new").pack())]
    ]
    for t in teachers[:PAGE]:
        mark = "⛔️" if t.is_blocked else ("🟢" if t.user_id else "⚪️")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=truncate(f"{mark} {t.full_name} ({t.login})", 60),
                    callback_data=AuTeacherCb(action="view", teacher_id=t.id, page=page).pack(),
                )
            ]
        )
    nav = pager_row(page, has_next, lambda p: AuTeacherCb(action="list", page=p).pack())
    if nav:
        buttons.append(nav)
    text = (
        f"<b>👨‍🏫 O‘qituvchilar</b> ({total})\n\n"
        "🟢 Telegramga biriktirilgan · ⚪️ hali kirmagan · ⛔️ bloklangan"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(AdminFilter(PERM), F.text == menus.BTN_A_TEACHERS)
async def teachers_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.clear()
    text, markup = await _list_markup(session, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(AuTeacherCb.filter(F.action == "list"))
async def teachers_list(
    callback: CallbackQuery, callback_data: AuTeacherCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    text, markup = await _list_markup(session, max(callback_data.page, 0))
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


async def _view(session: AsyncSession, teacher_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    account = await auth.get_teacher(session, teacher_id)
    bound = await session.get(User, account.user_id) if account.user_id else None
    active = None
    if bound is not None:
        ctx = await teacher_sessions.get_active_context(session, bound.telegram_id)
        active = ctx.session if ctx and ctx.teacher.id == account.id else None
    courses = (
        await session.scalars(
            select(Course.title)
            .join(TeacherCourseAssignment, TeacherCourseAssignment.course_id == Course.id)
            .where(TeacherCourseAssignment.teacher_id == account.id)
            .order_by(Course.title)
        )
    ).all()
    binding = f"{esc(bound.full_name)} (<code>{bound.telegram_id}</code>)" if bound else "hali kirmagan"
    lines = [
        f"<b>👨‍🏫 {esc(account.full_name)}</b>",
        f"Login: <code>{esc(account.login)}</code>",
        f"Holat: {'⛔️ bloklangan' if account.is_blocked else '✅ faol'}",
        f"Telegram: {binding}",
        f"Vaqtinchalik parol: {'ha (almashtirilmagan)' if account.must_change_password else 'yo‘q'}",
        f"Oxirgi kirish: {fmt_dt(account.last_login_at)}",
        f"Faol sessiya: {('⏳ ' + fmt_dt(active.expires_at) + ' gacha') if active else 'yo‘q'}",
        f"Kurslar: {', '.join(esc(c) for c in courses) if courses else '—'}",
    ]
    tid = account.id
    block_btn = (
        InlineKeyboardButton(
            text="✅ Blokdan chiqarish", callback_data=AuTeacherCb(action="unblock", teacher_id=tid).pack()
        )
        if account.is_blocked
        else InlineKeyboardButton(
            text="⛔️ Bloklash", callback_data=AuTeacherCb(action="block", teacher_id=tid).pack()
        )
    )
    buttons = [
        [
            InlineKeyboardButton(
                text="📚 Kurslar va ruxsatlar",
                callback_data=AuTeacherCb(action="courses", teacher_id=tid).pack(),
            )
        ],
        [block_btn],
        [
            InlineKeyboardButton(
                text="🔑 Parolni tiklash", callback_data=AuTeacherCb(action="reset_pw", teacher_id=tid).pack()
            )
        ],
    ]
    if account.user_id:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🔗 Telegram bog‘lanishini bekor qilish",
                    callback_data=AuTeacherCb(action="reset_bind", teacher_id=tid).pack(),
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Ro‘yxat", callback_data=AuTeacherCb(action="list", page=page).pack())]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(AuTeacherCb.filter(F.action == "view"))
async def teacher_view(
    callback: CallbackQuery, callback_data: AuTeacherCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    text, markup = await _view(session, callback_data.teacher_id, callback_data.page)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


# --------------------------------------------------------------------------- actions with confirmation

CONFIRM_TEXT = {
    "block": "O‘qituvchini bloklaysizmi? Faol sessiyasi darhol yakunlanadi.",
    "unblock": "O‘qituvchini blokdan chiqarasizmi?",
    "reset_pw": "Yangi vaqtinchalik parol yaratilsinmi? Joriy sessiyalar yakunlanadi.",
    "reset_bind": (
        "Telegram bog‘lanishi bekor qilinsinmi? Keyingi muvaffaqiyatli kirish yangi akkauntni biriktiradi."
    ),
}


async def _notify(bot: Bot, telegram_id: int | None, text: str) -> None:
    if telegram_id is None:
        return
    try:
        await bot.send_message(telegram_id, text, reply_markup=menus.teacher_menu(logged_in=False))
    except Exception:  # noqa: BLE001 - notification is best effort
        logger.info("teacher notice failed", exc_info=True)


@router.callback_query(AuTeacherCb.filter(F.action.in_(set(CONFIRM_TEXT))))
async def teacher_action(
    callback: CallbackQuery, callback_data: AuTeacherCb, session: AsyncSession, user: User, bot: Bot
) -> None:
    require_admin(user, PERM)
    tid = callback_data.teacher_id
    action = callback_data.action
    if not callback_data.confirm:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Ha",
                        callback_data=AuTeacherCb(action=action, teacher_id=tid, confirm=1).pack(),
                    ),
                    InlineKeyboardButton(
                        text="❌ Yo‘q", callback_data=AuTeacherCb(action="view", teacher_id=tid).pack()
                    ),
                ]
            ]
        )
        await callback.message.edit_text(CONFIRM_TEXT[action], reply_markup=markup)
        await callback.answer()
        return

    account = await auth.get_teacher(session, tid)
    old_user_id = account.user_id
    notice: str | None = None
    temp_password: str | None = None
    if action == "block":
        await auth.set_blocked(session, actor=user, teacher_id=tid, blocked=True)
        notice = "⛔️ O‘qituvchi hisobingiz administrator tomonidan bloklandi. Sessiya yakunlandi."
    elif action == "unblock":
        await auth.set_blocked(session, actor=user, teacher_id=tid, blocked=False)
    elif action == "reset_pw":
        _, temp_password = await auth.reset_password(session, actor=user, teacher_id=tid)
        notice = "🔑 Parolingiz administrator tomonidan tiklandi. Sessiya yakunlandi."
    elif action == "reset_bind":
        await auth.reset_binding(session, actor=user, teacher_id=tid)
        notice = "🔗 O‘qituvchi hisobingiz bu Telegram akkauntdan ajratildi. Sessiya yakunlandi."
    old_tg = (
        await session.scalar(select(User.telegram_id).where(User.id == old_user_id)) if old_user_id else None
    )
    await session.commit()

    if temp_password is not None:
        await callback.message.answer(
            "🔑 Yangi vaqtinchalik parol:\n"
            f"<code>{esc(temp_password)}</code>\n\n"
            "Uni o‘qituvchiga xavfsiz yo‘l bilan yetkazing va bu xabarni o‘chiring. "
            "Birinchi kirishda parol almashtiriladi.",
            protect_content=True,
        )
    if notice:
        await _notify(bot, old_tg, notice)
    text, markup = await _view(session, tid)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Bajarildi")


# --------------------------------------------------------------------------- create


@router.callback_query(AuTeacherCb.filter(F.action == "new"))
async def create_start(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.set_state(AuTeacherCreate.full_name)
    await callback.message.answer(
        "Yangi o‘qituvchining F.I.Sh.ni kiriting:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[CANCEL_ROW]),
    )
    await callback.answer()


@router.callback_query(AuTeacherCb.filter(F.action == "cancel"))
async def create_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Bekor qilindi.")
    await callback.answer()


@router.message(AuTeacherCreate.full_name, F.text)
async def create_full_name(message: Message, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    full_name = " ".join((message.text or "").split())
    if not 2 <= len(full_name) <= 255:
        await message.answer("F.I.Sh. 2–255 belgidan iborat bo‘lishi kerak. Qayta kiriting:")
        return
    await state.update_data(au_full_name=full_name)
    await state.set_state(AuTeacherCreate.login)
    await message.answer(
        "Login kiriting (3–32 belgi: kichik lotin harflari, raqamlar, “_” va “.”):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[CANCEL_ROW]),
    )


@router.message(AuTeacherCreate.login, F.text)
async def create_login(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    try:
        login = auth.validate_login(message.text or "")
    except InvalidInput as exc:
        await message.answer(f"{exc.user_message}\nQayta kiriting:")
        return
    if await session.scalar(select(TeacherAccount.id).where(TeacherAccount.login == login)) is not None:
        await message.answer("Bu login band. Boshqa login kiriting:")
        return
    await state.update_data(au_login=login)
    await state.set_state(AuTeacherCreate.telegram_id)
    await message.answer(
        "O‘qituvchining Telegram ID raqamini kiriting (u botga /start bosgan bo‘lishi kerak) "
        "yoki «-» yuboring — u holda hisob birinchi muvaffaqiyatli kirishda biriktiriladi:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[CANCEL_ROW]),
    )


@router.message(AuTeacherCreate.telegram_id, F.text)
async def create_finish(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    raw = (message.text or "").strip()
    telegram_id: int | None = None
    if raw not in {"-", "—"}:
        if not raw.isdigit() or len(raw) > 15:
            await message.answer(
                "Telegram ID faqat raqamlardan iborat bo‘ladi. Qayta kiriting yoki «-» yuboring:"
            )
            return
        telegram_id = int(raw)
    data = await state.get_data()
    account, temp_password = await auth.create_teacher(
        session,
        actor=user,
        full_name=data.get("au_full_name", ""),
        login=data.get("au_login", ""),
        telegram_id=telegram_id,
    )
    await state.clear()
    await session.commit()
    await message.answer(
        f"✅ O‘qituvchi yaratildi: <b>{esc(account.full_name)}</b>\n"
        f"Login: <code>{esc(account.login)}</code>\n"
        f"Vaqtinchalik parol: <code>{esc(temp_password)}</code>\n\n"
        "Ma’lumotlarni o‘qituvchiga xavfsiz yo‘l bilan yetkazing va bu xabarni o‘chiring. "
        f"O‘qituvchi «{menus.BTN_TEACHER_ENTRY}» → «{menus.BTN_T_LOGIN}» orqali kiradi "
        "va birinchi kirishda parolni almashtiradi.",
        protect_content=True,
    )
    text, markup = await _view(session, account.id)
    await message.answer(text, reply_markup=markup)


# --------------------------------------------------------------------------- course assignments


async def _courses_markup(
    session: AsyncSession, teacher_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    account = await auth.get_teacher(session, teacher_id)
    assigned = await teacher_sessions.teacher_course_ids(session, teacher_id)
    courses = (
        await session.scalars(select(Course).order_by(Course.title).limit(PAGE + 1).offset(page * PAGE))
    ).all()
    has_next = len(courses) > PAGE
    buttons = []
    for c in courses[:PAGE]:
        row = [
            InlineKeyboardButton(
                text=truncate(("✅ " if c.id in assigned else "▫️ ") + c.title, 50),
                callback_data=AuAssignCb(
                    action="toggle", teacher_id=teacher_id, course_id=c.id, page=page
                ).pack(),
            )
        ]
        if c.id in assigned:
            row.append(
                InlineKeyboardButton(
                    text="⚙️",
                    callback_data=AuAssignCb(
                        action="rights", teacher_id=teacher_id, course_id=c.id, page=page
                    ).pack(),
                )
            )
        buttons.append(row)
    nav = pager_row(
        page, has_next, lambda p: AuTeacherCb(action="courses", teacher_id=teacher_id, page=p).pack()
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ O‘qituvchi", callback_data=AuTeacherCb(action="view", teacher_id=teacher_id).pack()
            )
        ]
    )
    text = (
        f"<b>📚 {esc(account.full_name)}</b> — kurslar\n\n"
        "Kursni bosib biriktiring yoki ajrating. ⚙️ — kurs bo‘yicha ruxsatlar."
    )
    if not courses:
        text += "\n\nHali kurslar yaratilmagan."
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(AuTeacherCb.filter(F.action == "courses"))
async def courses_view(
    callback: CallbackQuery, callback_data: AuTeacherCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    text, markup = await _courses_markup(session, callback_data.teacher_id, max(callback_data.page, 0))
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(AuAssignCb.filter(F.action == "toggle"))
async def course_toggle(
    callback: CallbackQuery, callback_data: AuAssignCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    assigned = await teacher_sessions.teacher_course_ids(session, callback_data.teacher_id)
    if callback_data.course_id in assigned:
        await auth.unassign_course(
            session, actor=user, teacher_id=callback_data.teacher_id, course_id=callback_data.course_id
        )
    else:
        await auth.assign_course(
            session, actor=user, teacher_id=callback_data.teacher_id, course_id=callback_data.course_id
        )
    await session.flush()
    text, markup = await _courses_markup(session, callback_data.teacher_id, callback_data.page)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Saqlandi")


async def _rights_markup(session: AsyncSession, data: AuAssignCb) -> tuple[str, InlineKeyboardMarkup]:
    assignment = await session.scalar(
        select(TeacherCourseAssignment).where(
            TeacherCourseAssignment.teacher_id == data.teacher_id,
            TeacherCourseAssignment.course_id == data.course_id,
        )
    )
    course = await session.get(Course, data.course_id)
    if assignment is None or course is None:
        raise InvalidInput("Biriktirish topilmadi.")
    buttons = [
        [
            InlineKeyboardButton(
                text=("✅ " if getattr(assignment, right) else "▫️ ") + label,
                callback_data=AuAssignCb(
                    action="right",
                    teacher_id=data.teacher_id,
                    course_id=data.course_id,
                    right=right,
                    page=data.page,
                ).pack(),
            )
        ]
        for right, label in RIGHT_LABELS.items()
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Kurslar",
                callback_data=AuTeacherCb(
                    action="courses", teacher_id=data.teacher_id, page=data.page
                ).pack(),
            )
        ]
    )
    return f"⚙️ <b>{esc(course.title)}</b> — ruxsatlar", InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(AuAssignCb.filter(F.action == "rights"))
async def rights_view(
    callback: CallbackQuery, callback_data: AuAssignCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    text, markup = await _rights_markup(session, callback_data)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(AuAssignCb.filter(F.action == "right"))
async def right_toggle(
    callback: CallbackQuery, callback_data: AuAssignCb, session: AsyncSession, user: User
) -> None:
    require_admin(user, PERM)
    await auth.toggle_right(
        session,
        actor=user,
        teacher_id=callback_data.teacher_id,
        course_id=callback_data.course_id,
        right=callback_data.right,
    )
    await session.flush()
    text, markup = await _rights_markup(session, callback_data)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Saqlandi")
