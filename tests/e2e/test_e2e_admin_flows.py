"""E2E: admin content editor, paid lesson + payment approval, menu reset, forged callbacks.

Skipped without TEST_DATABASE_URL. No real Telegram calls (see tests/e2e/conftest.py).
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database.models import (
    Lesson,
    Notification,
    PaymentRequest,
    PaymentRequestStatus,
    QuizAttempt,
    Subject,
    Subscription,
    SubscriptionPlan,
)
from app.handlers.common.errors import GENERIC_ERROR
from app.keyboards import menus
from app.keyboards.commerce import CmPayCb
from app.keyboards.content import CtEd, CtLes, CtQe, CtQz
from tests.e2e.test_e2e_content_flows import LESSON_BODY, seed, walk_to_topic

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- 4. admin builds a course


async def test_superadmin_builds_and_publishes_a_lesson(harness, superadmin, committing_sessionmaker):
    replies = await superadmin.send("/start")
    keyboard = superadmin.reply_keyboard(replies)
    assert menus.BTN_A_CONTENT in keyboard and menus.BTN_A_USERS in keyboard

    await superadmin.send(menus.BTN_A_CONTENT)
    for button, name in (
        ("➕ Fan qo‘shish", "Fizika"),
        ("➕ Kurs qo‘shish", "Mexanika"),
        ("➕ Modul qo‘shish", "Kinematika"),
        ("➕ Mavzu qo‘shish", "Tezlik"),
        ("➕ Dars qo‘shish", "Tekis harakat"),
    ):
        replies = await superadmin.click(button)
        assert "nomini yuboring" in superadmin.texts(replies)
        replies = await superadmin.send(name)
        assert "✅ Yaratildi." in superadmin.texts(replies) and name in superadmin.texts(replies)

    assert "nofaol" in superadmin.texts(replies)  # a new lesson starts hidden
    await superadmin.click("✏️ Dars matni")
    replies = await superadmin.send("Tezlik = masofa / vaqt.")
    assert "✅ Saqlandi." in superadmin.texts(replies)

    replies = await superadmin.click("📢 Chop etish")
    assert "✅ faol" in superadmin.texts(replies)

    async with committing_sessionmaker() as s:
        lesson = await s.scalar(select(Lesson).where(Lesson.title == "Tekis harakat"))
        assert lesson is not None and lesson.is_active and lesson.published_at is not None
        assert lesson.body_text == "Tezlik = masofa / vaqt."
        notice = await s.scalar(
            select(Notification).where(Notification.dedupe_key == f"new_lesson:{lesson.id}")
        )
        assert notice is not None  # COMMERCE announces the new lesson to the course audience

    # A student now finds it in the catalog (paid by default → 🔒).
    student = harness.client()
    await student.send("/start")
    await student.send(menus.BTN_COURSES)
    for step in ("Fizika", "Mexanika", "Kinematika", "Tezlik"):
        await student.click(step)
    assert student.has_button("🔒 Tekis harakat")


# --------------------------------------------------------------------------- 5. paid lesson + payment


async def test_paid_lesson_payment_request_and_idempotent_approval(
    harness, superadmin, committing_sessionmaker
):
    _, _, paid, _ = await seed(committing_sessionmaker, free=0, paid=1)
    async with committing_sessionmaker() as s:
        s.add(SubscriptionPlan(name="1 oylik", price=Decimal("150000"), duration_days=30))
        await s.commit()
    await superadmin.send("/start")
    student = harness.client(first_name="Ali")
    await student.send("/start")

    await walk_to_topic(student)
    replies = await student.click("Pullik dars 1")
    assert LESSON_BODY not in student.texts(replies)
    assert "obuna" in student.last_alert().lower()

    await student.send(menus.BTN_SUBSCRIBE)
    replies = await student.click("1 oylik")
    assert str(student.tg_id) in student.texts(replies)  # TZ §6.2.4: the user's Telegram ID is shown
    await student.click("🧾 Chek yuborish")
    replies = await student.send_photo(caption="karta 1234")
    assert "qabul qilindi" in student.texts(replies)
    admin_cards = [r for r in replies if r.chat_id == superadmin.tg_id and r.method == "SendPhoto"]
    assert admin_cards, "admins with the payments permission are notified with the proof photo"

    _, approve = superadmin.button("✅ Tasdiqlash")
    assert approve.callback_data
    replies = await superadmin.click("✅ Tasdiqlash")
    assert "tasdiqlandi" in superadmin.texts(replies)

    # Second tap on the same (stale) button creates nothing.
    await superadmin.press(approve.callback_data)
    async with committing_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(Subscription)) == 1
        request = await s.scalar(select(PaymentRequest))
        assert request is not None and request.status == PaymentRequestStatus.APPROVED

    # The paid lesson opens now.
    await walk_to_topic(student)
    replies = await student.click("Pullik dars 1")
    assert LESSON_BODY in student.texts(replies)


# --------------------------------------------------------------------------- 6. menu button cancels FSM input


async def test_menu_button_cancels_pending_input(harness, superadmin, committing_sessionmaker):
    await superadmin.send("/start")
    await superadmin.send(menus.BTN_A_CONTENT)
    replies = await superadmin.click("➕ Fan qo‘shish")
    assert "nomini yuboring" in superadmin.texts(replies)

    replies = await superadmin.send(menus.BTN_A_CONTENT)  # a menu button instead of the name
    assert "Fanni tanlang" in superadmin.texts(replies)
    replies = await superadmin.send("Kimyo")  # input mode is over: this is not a subject name
    assert "Yaratildi" not in superadmin.texts(replies)

    replies = await superadmin.click("➕ Fan qo‘shish")
    await superadmin.send("/menu")  # commands cancel too
    await superadmin.send("Biologiya")
    async with committing_sessionmaker() as s:
        titles = set(await s.scalars(select(Subject.title)))
    assert not titles & {menus.BTN_A_CONTENT, "Kimyo", "/menu", "Biologiya"}


# --------------------------------------------------------------------------- 7. stale / foreign callbacks


async def test_forged_and_stale_callbacks_leak_nothing(harness, committing_sessionmaker):
    _, free, paid, quiz = await seed(committing_sessionmaker, free=1, paid=1, quiz_on_free=True)
    assert quiz is not None
    owner = harness.client()
    await owner.send("/start")
    await walk_to_topic(owner)
    await owner.click("Bepul dars 1")
    await owner.click("✅ Ochish")
    await owner.click("🧪 Nazorat testi")
    await owner.click("Boshlash")
    await owner.click("⏹ Testni yakunlash")
    async with committing_sessionmaker() as s:
        attempt_id = await s.scalar(select(QuizAttempt.id))
    assert attempt_id is not None

    intruder = harness.client()
    await intruder.send("/start")
    forged = [
        CtEd(action="view", kind="lesson", id=paid[0].id).pack(),  # admin editor
        CtEd(action="root").pack(),
        CtEd(action="toggle", kind="lesson", id=paid[0].id).pack(),
        CtQe(action="view", id=quiz.id).pack(),  # quiz editor (would show correct answers)
        CtQe(action="qlist", id=quiz.id).pack(),
        CtLes(action="mat", lesson_id=paid[0].id, media_id=1).pack(),
        CtLes(action="open", lesson_id=paid[0].id).pack(),
        CtLes(action="open", lesson_id=999_999).pack(),
        CtQz(action="summary", attempt_id=attempt_id).pack(),  # someone else's result
        CtQz(action="result", attempt_id=attempt_id).pack(),
        CmPayCb(action="approve", request_id=1).pack(),
        "ct_les:open:not-a-number",
        "totally:unknown:callback",
    ]
    for data in forged:
        replies = await intruder.press(data)
        text = intruder.texts(replies)
        assert LESSON_BODY not in text, data
        assert "to‘g‘ri" not in text and "Ball:" not in text, data
        assert intruder.last_alert(), f"{data}: the tap must be answered (alert or toast)"
        assert GENERIC_ERROR not in intruder.last_alert(), f"{data}: crashed instead of refusing"

    async with committing_sessionmaker() as s:
        lesson = await s.get(Lesson, paid[0].id)
        assert lesson is not None and lesson.is_active  # the forged toggle changed nothing

    # Stale button: the owner's lesson list after the lesson was hidden by an admin.
    async with committing_sessionmaker() as s:
        hidden = await s.get(Lesson, free[0].id)
        assert hidden is not None
        hidden.is_active = False
        await s.commit()
    await owner.click("Mavzuga qaytish")
    replies = await owner.click("Bepul dars 1")
    assert LESSON_BODY not in owner.texts(replies)
    assert "mavjud emas" in owner.last_alert()
