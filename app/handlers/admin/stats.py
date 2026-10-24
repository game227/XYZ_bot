"""Admin: 📊 Statistika (TZ §17)."""

from datetime import datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminPermission, User
from app.keyboards import menus
from app.keyboards.commerce import CmStatsCb, btn, rows
from app.services import monitoring, stats, subscriptions
from app.services.access import AdminFilter, require_admin
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

router = Router(name="admin.stats")

PERM = AdminPermission.STATS


def _beat(age: int | None) -> str:
    return "🔴 javob yo‘q" if age is None or age > 120 else f"🟢 ishlayapti ({age} s oldin)"


def render_health(h: monitoring.HealthReport) -> str:
    return (
        "\n\n<b>🖥 Tizim holati</b>\n"
        f"Bot: {_beat(h.bot_beat)} · worker: {_beat(h.worker_beat)}\n"
        f"Oxirgi soat: {h.updates_1h} so‘rov, {h.errors_1h} xato, o‘rtacha {h.avg_ms_1h} ms\n"
        f"24 soat: {h.updates_24h} so‘rov, {h.errors_24h} xato, "
        f"sekin (≥{monitoring.SLOW_UPDATE_MS // 1000} s): {h.slow_24h}, o‘rtacha {h.avg_ms_24h} ms"
    )


def render(r: stats.StatsReport) -> str:
    top = (
        "\n".join(f"  {i}. {esc(truncate(title, 40))} — {n}" for i, (title, n) in enumerate(r.top_courses, 1))
        or "  —"
    )
    d = r.deliveries_7d
    broadcasts = (
        "\n".join(
            f"  #{i}: {status}, {sent}/{total} yuborildi, {failed} xato"
            for i, status, total, sent, failed in r.last_broadcasts
        )
        or "  —"
    )
    return (
        "📊 <b>Statistika</b>\n"
        f"<i>{fmt_dt(datetime.fromisoformat(r.generated_at))} holatiga</i>\n\n"
        "<b>👥 Foydalanuvchilar</b>\n"
        f"Jami: {r.users_total} · yangi (7 kun): {r.users_new_7d}\n"
        f"Faol: 7 kun — {r.users_active_7d}, 30 kun — {r.users_active_30d}\n"
        f"Botni bloklagan: {r.users_blocked_bot} · bloklangan: {r.users_blocked}\n"
        f"O‘qituvchilar: {r.teachers_total} (bloklangan: {r.teachers_blocked})\n\n"
        "<b>💳 Obunalar</b>\n"
        f"Faol: {r.subs_active} · muddati tugagan: {r.subs_expired}\n"
        f"Kutilayotgan so‘rovlar: {r.payments_pending}\n"
        f"30 kunda tasdiqlangan: {r.payments_approved_30d} · "
        f"{subscriptions.format_price(Decimal(r.revenue_30d))}\n\n"
        "<b>📚 Ta’lim</b>\n"
        f"Faol kurslar: {r.courses_active} · darslar: {r.lessons_active} (bepul: {r.lessons_free})\n"
        f"Dars ochilishlari (7 kun): {r.lesson_opens_7d}\n"
        f"O‘quvchilar (dars ochgan): {r.learners_total} · o‘rtacha {r.avg_lessons_per_learner} dars\n"
        f"Eng faol kurslar:\n{top}\n\n"
        "<b>🧪 Testlar</b>\n"
        f"Faol testlar: {r.quizzes_active} · yakunlangan urinishlar: {r.attempts_finished}"
        f" (30 kun: {r.attempts_30d})\n"
        f"O‘rtacha natija: {r.avg_percent}% · o‘tish darajasi: {r.pass_rate}%\n\n"
        "<b>📢 Bildirishnomalar (7 kun)</b>\n"
        f"Yuborildi: {d.get('sent', 0)} · xato: {d.get('failed', 0)} · bloklagan: {d.get('blocked', 0)}"
        f" · navbatda: {d.get('pending', 0)}\n"
        f"Oxirgi e’lonlar:\n{broadcasts}"
    )


@router.message(AdminFilter(PERM), F.text == menus.BTN_A_STATS)
async def stats_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    require_admin(user, PERM)
    await state.clear()
    report = await stats.get_report(session)
    text = render(report) + render_health(await monitoring.health_report())
    await message.answer(text, reply_markup=rows(btn("🔄 Yangilash", CmStatsCb(action="refresh"))))


@router.callback_query(CmStatsCb.filter(F.action == "refresh"))
async def stats_refresh(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    require_admin(user, PERM)
    report = await stats.get_report(session, fresh=True)
    await callback.answer("Yangilandi")
    if isinstance(callback.message, Message):
        text = render(report) + render_health(await monitoring.health_report())
        await callback.message.edit_text(
            text, reply_markup=rows(btn("🔄 Yangilash", CmStatsCb(action="refresh")))
        )
