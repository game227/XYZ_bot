"""E2E: student catalog, free-lesson limit and quizzes through the real Dispatcher.

Skipped without TEST_DATABASE_URL. No real Telegram calls (see tests/e2e/conftest.py).
"""

import pytest
from sqlalchemy import func, select

from app.database.models import LessonAccess, QuizAttempt, QuizResultVisibility
from app.keyboards import menus
from tests.integration.content_factory import make_lessons, make_quiz, make_tree

pytestmark = pytest.mark.integration

LESSON_BODY = "Bu darsning maxfiy matni: kvadrat tenglama formulasi."


async def seed(sessionmaker, *, free: int = 3, paid: int = 1, quiz_on_free: bool = False, **quiz_settings):
    async with sessionmaker() as s:
        topic = await make_tree(s)
        free_lessons, paid_lessons = await make_lessons(s, topic, free=free, paid=paid)
        for lesson in (*free_lessons, *paid_lessons):
            lesson.body_text = f"{LESSON_BODY} ({lesson.title})"
        quiz = None
        if quiz_on_free:
            quiz, _ = await make_quiz(s, free_lessons[0], questions=3, **quiz_settings)
        await s.commit()
        return topic, free_lessons, paid_lessons, quiz


async def walk_to_topic(student) -> None:
    replies = await student.send(menus.BTN_COURSES)
    assert "Fanlar va kurslar" in student.texts(replies)
    await student.click("Matematika")
    await student.click("Algebra")
    await student.click("1-modul")
    replies = await student.click("Tenglamalar")
    assert "Darsni tanlang" in student.texts(replies)


# --------------------------------------------------------------------------- 1. start + catalog


async def test_new_student_start_and_catalog_navigation(harness, committing_sessionmaker):
    await seed(committing_sessionmaker)
    student = harness.client()

    replies = await student.send("/start")
    assert "xush kelibsiz" in student.texts(replies)
    keyboard = student.reply_keyboard(replies)
    assert menus.BTN_COURSES in keyboard and menus.BTN_A_USERS not in keyboard

    await walk_to_topic(student)
    assert student.has_button("🆓 Bepul dars 1")
    assert student.has_button("🔒 Pullik dars 1")
    # Navigation edits the same message instead of flooding the chat.
    edits = [s for s in harness.session.sent if s.chat_id == student.tg_id and s.method == "EditMessageText"]
    assert len(edits) >= 4


# --------------------------------------------------------------------------- 2. free lesson limit


async def test_free_lesson_confirm_limit_and_reopen(harness, committing_sessionmaker):
    await seed(committing_sessionmaker, free=3)
    student = harness.client()
    await student.send("/start")
    await walk_to_topic(student)

    # First free lesson: a confirmation step, nothing consumed yet.
    replies = await student.click("Bepul dars 1")
    assert "limitingizdan 1 tasi ishlatiladi" in student.texts(replies)
    assert LESSON_BODY not in student.texts(replies)
    replies = await student.click("✅ Ochish")
    text = student.texts(replies)
    assert "Bepul dars 1" in text and LESSON_BODY in text and "Qolgan bepul darslar: <b>1</b>" in text

    await student.click("Mavzuga qaytish")
    await student.click("Bepul dars 2")
    replies = await student.click("✅ Ochish")
    assert "Qolgan bepul darslar: <b>0</b>" in student.texts(replies)

    # Third distinct free lesson: refused with an offer to subscribe; no content leaks.
    await student.click("Mavzuga qaytish")
    replies = await student.click("Bepul dars 3")
    assert LESSON_BODY not in student.texts(replies)
    assert "2 ta bepul darsdan foydalanib bo‘ldingiz" in student.last_alert()
    assert "💳 Obuna olish" in student.last_alert()

    # Reopening an already opened lesson still works, without a confirmation.
    replies = await student.click("Bepul dars 1")
    text = student.texts(replies)
    assert LESSON_BODY in text and "limitingizdan" not in text

    async with committing_sessionmaker() as s:
        consumed = await s.scalar(select(func.count()).where(LessonAccess.consumed_free_slot))
    assert consumed == 2

    # 🎓 Bepul darslar shows the remaining allowance.
    replies = await student.send(menus.BTN_FREE_LESSONS)
    assert "Qolgan: <b>0</b>" in student.texts(replies)


# --------------------------------------------------------------------------- 3. quiz on a free lesson


async def test_quiz_on_free_lesson_scored_and_answers_hidden_until_end(harness, committing_sessionmaker):
    _, free, _, quiz = await seed(
        committing_sessionmaker,
        free=1,
        quiz_on_free=True,
        result_visibility=QuizResultVisibility.SCORE_AND_ANSWERS,
    )
    student = harness.client()
    await student.send("/start")
    await walk_to_topic(student)
    await student.click("Bepul dars 1")
    await student.click("✅ Ochish")

    replies = await student.click("🧪 Nazorat testi")
    assert "Savollar: 3" in student.texts(replies)
    during: list[str] = []
    replies = await student.click("Boshlash")
    during.append(student.texts(replies))
    # Options are listed as "1) to‘g‘ri / 2) xato" (fixed order, no shuffle); answer 1, 1, 2.
    for choice in ("1", "1", "2"):
        sent, _ = student.button("⏹ Testni yakunlash")
        assert "Savol" in sent.text
        replies = await student.click_exact(choice)
        during.append(student.texts(replies))

    final = during.pop()
    assert "Ball: <b>2</b> / 3" in final and "66.67%" in final and "✅ O‘tdingiz" in final
    joined = "\n".join(during)
    assert "To‘g‘ri javob" not in joined and "✔" not in joined and "Ball:" not in joined

    # Review (allowed by result_visibility) only after the end.
    replies = await student.click("📋 Javoblarni ko‘rish")
    review = student.texts(replies)
    assert "To‘g‘ri javob: to‘g‘ri" in review and review.count("✅") == 2

    # 📈 Mening natijalarim lists the attempt.
    replies = await student.send(menus.BTN_MY_RESULTS)
    assert "Nazorat testi" in student.texts(replies) and "66.67%" in student.texts(replies)
    async with committing_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(QuizAttempt)) == 1
