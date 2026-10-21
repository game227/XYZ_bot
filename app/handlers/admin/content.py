"""Content editor: 📚 Fanlar va kurslar (admin) and the shared course-structure editor.

The `CtEd` callbacks are used by admins (CONTENT permission) and by teachers (🎬 Dars
materiallari, active session + `can_manage_content` on the course). Every handler
re-checks rights in the service layer (`lessons.ensure_can_*`), never trusting the callback.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import Base
from app.database.models import AdminPermission, Course, Lesson, Module, Topic, User
from app.keyboards import menus
from app.keyboards.content import CtEd, CtLes, CtQe, active_mark, btn, markup, show
from app.services import lessons, quizzes
from app.services.access import AdminFilter, has_permission
from app.services.errors import InvalidInput
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="admin.content")

KIND_NAMES = {"subject": "Fan", "course": "Kurs", "module": "Modul", "topic": "Mavzu", "lesson": "Dars"}
KIND_ICONS = {"subject": "📗", "course": "🎓", "module": "📦", "topic": "🗂", "lesson": "📘"}
CHILD = {"subject": "course", "course": "module", "module": "topic", "topic": "lesson"}
FIELDS = {"t": "title", "d": "description", "s": "short_description", "b": "body_text"}
FIELD_PROMPTS = {
    "t": "Yangi nomni yuboring:",
    "d": "Yangi tavsifni yuboring (bo‘shatish uchun «-» yuboring):",
    "s": "Qisqa tavsifni yuboring (1000 belgigacha, bo‘shatish uchun «-»):",
    "b": "Dars matnini yuboring (20 000 belgigacha, bo‘shatish uchun «-»):",
}


class CtEditText(StatesGroup):
    value = State()


# --------------------------------------------------------------------------- rendering


async def ensure_can_view(session: AsyncSession, user: User, kind: str, obj: Base) -> None:
    if kind == "subject":
        lessons.ensure_content_admin(user)
        return
    course_id = await lessons.course_id_of(session, kind, obj)
    assert course_id is not None
    await lessons.ensure_can_manage_course(session, user, course_id)


def _children_rows(kind: str, children: list) -> list[list[InlineKeyboardButton]]:
    child_kind = CHILD[kind]
    icon = KIND_ICONS[child_kind]
    rows = []
    for c in children:
        extra = " 🆓" if child_kind == "lesson" and c.is_free else ""
        rows.append(
            [
                btn(
                    f"{icon} {c.title}{extra}{active_mark(c.is_active)}",
                    CtEd(action="view", kind=child_kind, id=c.id),
                )
            ]
        )
    return rows


def _status_line(obj: Base) -> str:
    return "✅ faol" if obj.is_active else "🚫 nofaol (o‘quvchilarga ko‘rinmaydi)"  # type: ignore[attr-defined]


async def render_root(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    lessons.ensure_content_admin(user)
    subjects = await lessons.list_subjects(session, include_inactive=True)
    rows = [
        [btn(f"📗 {s.title}{active_mark(s.is_active)}", CtEd(action="view", kind="subject", id=s.id))]
        for s in subjects
    ]
    rows.append([btn("➕ Fan qo‘shish", CtEd(action="new", kind="root", field="subject"))])
    text = "📚 <b>Fanlar va kurslar</b>\n\nFanni tanlang yoki yangisini qo‘shing. 🚫 — nofaol."
    return text, markup(rows)


async def render_courses(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    """Course picker: all courses for content admins, assigned ones for teachers."""
    course_ids = await lessons.manageable_course_ids(session, user)
    if course_ids is not None and not course_ids:
        if user.role != "student":
            # Distinguish "no session" from "no courses": require_teacher raises the right message.
            from app.services import teacher_sessions

            await teacher_sessions.require_teacher(session, user)
        return "Sizga hali kurs biriktirilmagan.", None
    courses = await lessons.list_courses(session, include_inactive=True, course_ids=course_ids)
    if not courses:
        return "Hozircha kurslar yo‘q. Avval 📚 Fanlar va kurslar bo‘limida kurs yarating.", None
    rows = [
        [btn(f"🎓 {c.title}{active_mark(c.is_active)}", CtEd(action="view", kind="course", id=c.id))]
        for c in courses
    ]
    return "🎬 <b>Darslar va materiallar</b>\n\nKursni tanlang:", markup(rows)


async def render_node(
    session: AsyncSession, user: User, kind: str, obj_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    obj = await lessons.get_node(session, kind, obj_id)
    await ensure_can_view(session, user, kind, obj)
    is_admin = has_permission(user, AdminPermission.CONTENT)
    name, icon = KIND_NAMES[kind], KIND_ICONS[kind]
    lines = [f"{icon} <b>{name}: {esc(obj.title)}</b>", _status_line(obj)]  # type: ignore[attr-defined]
    rows: list[list[InlineKeyboardButton]] = []

    if kind == "lesson":
        assert isinstance(obj, Lesson)
        lines.append("🆓 bepul" if obj.is_free else "💳 pullik (obuna kerak)")
        lines.append(f"Chop etilgan: {fmt_dt(obj.published_at)}")
        if obj.short_description:
            lines.append(f"\n{esc(obj.short_description)}")
        lines.append(f"\nDars matni: {len(obj.body_text or '')} belgi")
        materials = await lessons.lesson_materials(session, obj.id, ready_only=False)
        n_quizzes = len(await quizzes.list_lesson_quizzes(session, obj.id, include_inactive=True))
        lines.append(f"Materiallar: {len(materials)} · Testlar: {n_quizzes}")
        rows += [
            [
                btn("✏️ Nom", CtEd(action="edit", kind=kind, id=obj.id, field="t")),
                btn("✏️ Qisqa tavsif", CtEd(action="edit", kind=kind, id=obj.id, field="s")),
            ],
            [btn("✏️ Dars matni", CtEd(action="edit", kind=kind, id=obj.id, field="b"))],
            [
                btn(f"📎 Materiallar ({len(materials)})", CtEd(action="media", kind=kind, id=obj.id)),
                btn(f"🧪 Testlar ({n_quizzes})", CtQe(action="lesson", id=obj.id)),
            ],
        ]
        if is_admin:
            rows.append(
                [
                    btn(
                        "💳 Pullik qilish" if obj.is_free else "🆓 Bepul qilish",
                        CtEd(action="free", kind=kind, id=obj.id),
                    )
                ]
            )
        if obj.is_active:
            toggle_text = "🚫 Yashirish"
        else:
            toggle_text = "📢 Chop etish" if obj.published_at is None else "✅ Faollashtirish"
        rows.append(
            [
                btn(toggle_text, CtEd(action="toggle", kind=kind, id=obj.id)),
                btn("👀 Ko‘rish", CtLes(action="open", lesson_id=obj.id)),
            ]
        )
    else:
        description = obj.description  # type: ignore[attr-defined]
        if description:
            lines.append(f"\n{esc(truncate(description, 1500))}")
        child_kind = CHILD[kind]
        children = {
            "subject": lambda: lessons.list_courses(session, obj.id, include_inactive=True),  # type: ignore[attr-defined]
            "course": lambda: lessons.list_modules(session, obj.id, include_inactive=True),  # type: ignore[attr-defined]
            "module": lambda: lessons.list_topics(session, obj.id, include_inactive=True),  # type: ignore[attr-defined]
            "topic": lambda: lessons.list_lessons(session, obj.id, include_inactive=True),  # type: ignore[attr-defined]
        }[kind]
        items = await children()
        lines.append(f"\n{KIND_NAMES[child_kind]}lar: {len(items)}")
        rows += _children_rows(kind, items)
        if child_kind != "course" or is_admin:
            rows.append(
                [
                    btn(
                        f"➕ {KIND_NAMES[child_kind]} qo‘shish",
                        CtEd(action="new", kind=kind, id=obj.id, field=child_kind),
                    )
                ]
            )
        if kind not in {"subject", "course"} or is_admin:
            rows.append(
                [
                    btn("✏️ Nom", CtEd(action="edit", kind=kind, id=obj.id, field="t")),
                    btn("✏️ Tavsif", CtEd(action="edit", kind=kind, id=obj.id, field="d")),
                ]
            )
            rows.append(
                [
                    btn(
                        "🚫 Nofaol qilish" if obj.is_active else "✅ Faollashtirish",
                        CtEd(action="toggle", kind=kind, id=obj.id),
                    )
                ]  # type: ignore[attr-defined]
            )

    if kind not in {"subject", "course"} or is_admin:
        rows.append(
            [
                btn("⬆️", CtEd(action="up", kind=kind, id=obj.id)),
                btn("⬇️", CtEd(action="down", kind=kind, id=obj.id)),
                btn("🗑 O‘chirish", CtEd(action="del", kind=kind, id=obj.id)),
            ]
        )
    rows.append([await back_button(session, user, kind, obj)])
    return "\n".join(lines), markup(rows)


async def back_button(session: AsyncSession, user: User, kind: str, obj: Base) -> InlineKeyboardButton:
    match kind:
        case "subject":
            return btn("⬅️ Fanlar", CtEd(action="root"))
        case "course":
            assert isinstance(obj, Course)
            if has_permission(user, AdminPermission.CONTENT):
                return btn("⬅️ Fan", CtEd(action="view", kind="subject", id=obj.subject_id))
            return btn("⬅️ Kurslar", CtEd(action="courses"))
        case "module":
            assert isinstance(obj, Module)
            return btn("⬅️ Kurs", CtEd(action="view", kind="course", id=obj.course_id))
        case "topic":
            assert isinstance(obj, Topic)
            return btn("⬅️ Modul", CtEd(action="view", kind="module", id=obj.module_id))
        case _:
            assert isinstance(obj, Lesson)
            return btn("⬅️ Mavzu", CtEd(action="view", kind="topic", id=obj.topic_id))


async def render_any(
    session: AsyncSession, user: User, kind: str, obj_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    if kind == "root":
        return await render_root(session, user)
    if kind == "courses":
        return await render_courses(session, user)
    return await render_node(session, user, kind, obj_id)


# --------------------------------------------------------------------------- entry + navigation


@router.message(AdminFilter(AdminPermission.CONTENT), F.text == menus.BTN_A_CONTENT)
async def content_entry(message: Message, session: AsyncSession, user: User) -> None:
    text, kb = await render_root(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CtEd.filter(F.action.in_({"root", "courses", "view"})))
async def navigate(callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User) -> None:
    kind = callback_data.action if callback_data.action != "view" else callback_data.kind
    text, kb = await render_any(session, user, kind, callback_data.id)
    await show(callback, text, kb)
    await callback.answer()


# --------------------------------------------------------------------------- simple actions


@router.callback_query(CtEd.filter(F.action.in_({"toggle", "free", "up", "down"})))
async def simple_action(
    callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User
) -> None:
    kind, obj_id = callback_data.kind, callback_data.id
    match callback_data.action:
        case "toggle":
            await lessons.toggle_active(session, user, kind, obj_id)
        case "free":
            if kind != "lesson":
                raise InvalidInput()
            lesson = await lessons.get_lesson(session, obj_id)
            await lessons.set_lesson_free(session, user, obj_id, not lesson.is_free)
        case "up" | "down":
            await lessons.move(session, user, kind, obj_id, -1 if callback_data.action == "up" else 1)
    text, kb = await render_node(session, user, kind, obj_id)
    await show(callback, text, kb)
    await callback.answer("Saqlandi")


@router.callback_query(CtEd.filter(F.action == "del"))
async def ask_delete(callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User) -> None:
    obj = await lessons.get_node(session, callback_data.kind, callback_data.id)
    await lessons.ensure_can_edit_node(session, user, callback_data.kind, obj)
    kb = markup(
        [
            [btn("🗑 Ha, o‘chirish", CtEd(action="delok", kind=callback_data.kind, id=callback_data.id))],
            [btn("⬅️ Bekor qilish", CtEd(action="view", kind=callback_data.kind, id=callback_data.id))],
        ]
    )
    name = KIND_NAMES.get(callback_data.kind, "")
    await show(callback, f"{name} «{esc(obj.title)}» o‘chirilsinmi? Bu amalni qaytarib bo‘lmaydi.", kb)  # type: ignore[attr-defined]
    await callback.answer()


@router.callback_query(CtEd.filter(F.action == "delok"))
async def do_delete(callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User) -> None:
    kind = callback_data.kind
    obj = await lessons.get_node(session, kind, callback_data.id)
    await lessons.ensure_can_edit_node(session, user, kind, obj)
    back = await back_button(session, user, kind, obj)
    await lessons.delete_node(session, user, kind, callback_data.id)
    await show(callback, "🗑 O‘chirildi.", markup([[back]]))
    await callback.answer()


# --------------------------------------------------------------------------- text input (create / edit)


@router.callback_query(CtEd.filter(F.action.in_({"new", "edit"})))
async def ask_text(
    callback: CallbackQuery, callback_data: CtEd, session: AsyncSession, user: User, state: FSMContext
) -> None:
    if callback_data.action == "new":
        child = callback_data.field
        if child in {"subject", "course"}:
            lessons.ensure_content_admin(user)
        if callback_data.kind != "root":
            parent = await lessons.get_node(session, callback_data.kind, callback_data.id)
            await ensure_can_view(session, user, callback_data.kind, parent)
        prompt = f"Yangi {KIND_NAMES[child].lower()} nomini yuboring:"
    else:
        if callback_data.field not in FIELDS:
            raise InvalidInput()
        obj = await lessons.get_node(session, callback_data.kind, callback_data.id)
        await lessons.ensure_can_edit_node(session, user, callback_data.kind, obj)
        prompt = FIELD_PROMPTS[callback_data.field]
    await state.set_state(CtEditText.value)
    await state.update_data(
        ct_op=callback_data.action,
        ct_kind=callback_data.kind,
        ct_id=callback_data.id,
        ct_field=callback_data.field,
    )
    await callback.answer()
    if callback.message is not None:
        await callback.bot.send_message(callback.from_user.id, f"{prompt}\n\nBekor qilish: /menu")  # type: ignore[union-attr]


@router.message(CtEditText.value, F.text)
async def receive_text(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    op, kind, obj_id, field = (
        data.get("ct_op"),
        data.get("ct_kind"),
        int(data.get("ct_id") or 0),
        data.get("ct_field"),
    )
    value = message.text or ""
    if op == "new":
        creators = {
            "subject": lambda: lessons.create_subject(session, user, title=value),
            "course": lambda: lessons.create_course(session, user, subject_id=obj_id, title=value),
            "module": lambda: lessons.create_module(session, user, course_id=obj_id, title=value),
            "topic": lambda: lessons.create_topic(session, user, module_id=obj_id, title=value),
            "lesson": lambda: lessons.create_lesson(session, user, topic_id=obj_id, title=value),
        }
        if field not in creators:
            await state.clear()
            return
        created = await creators[field]()
        kind, obj_id = str(field), created.id  # type: ignore[attr-defined]
        note = "✅ Yaratildi."
        if kind == "lesson":
            note += " Dars hozircha yashirin: matn va materiallarni qo‘shib, «📢 Chop etish»ni bosing."
    elif op == "edit" and field in FIELDS and kind:
        await lessons.update_field(session, user, kind, obj_id, FIELDS[field], value)
        note = "✅ Saqlandi."
    else:
        await state.clear()
        return
    await state.clear()
    text, kb = await render_node(session, user, str(kind), obj_id)
    await message.answer(f"{note}\n\n{text}", reply_markup=kb)


__all__ = ["CtEditText", "render_any", "render_courses", "render_node", "router"]
