"""Quiz engine against PostgreSQL (TZ §8, acceptance 13, 14, 19). Skipped without TEST_DATABASE_URL."""

from datetime import timedelta
from decimal import Decimal

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.database.models import AttemptStatus, QuizAccessMode
from app.handlers.student import quizzes as quiz_screens
from app.services import lessons, quizzes
from app.services.errors import AccessDenied, Conflict, SubscriptionRequired
from app.utils.time import utcnow
from tests.integration.content_factory import make_lessons, make_quiz, make_student, make_tree, subscribe

pytestmark = pytest.mark.integration


async def _option(session, question_id: int, *, correct: bool) -> int:
    opts = (await quizzes.options_for(session, [question_id]))[question_id]
    return next(o.id for o in opts if o.is_correct is correct)


async def _answer(session, user, attempt, *, correct: bool):
    cq = await quizzes.current_question(session, attempt)
    assert cq is not None
    option_id = await _option(session, cq.question.id, correct=correct)
    return await quizzes.submit_answer(session, user, attempt.id, cq.question.id, option_ids=[option_id])


async def test_full_attempt_is_scored_and_saved(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=1)
    quiz, _ = await make_quiz(session, free[0], questions=3, pass_percent=60)
    user = await make_student(session)
    await lessons.open_lesson(session, user, free[0])  # free lesson quizzes need no subscription

    attempt = await quizzes.start_or_resume(session, user, quiz.id)
    assert attempt.attempt_no == 1 and attempt.max_score == Decimal("3")
    await _answer(session, user, attempt, correct=True)
    await _answer(session, user, attempt, correct=True)
    outcome = await _answer(session, user, attempt, correct=False)

    assert outcome.finished and attempt.status == AttemptStatus.COMPLETED
    assert (attempt.score, attempt.percent, attempt.passed) == (Decimal("2.00"), Decimal("66.67"), True)
    rows = await quizzes.results_for_user(session, user.id)
    assert [r.attempt.id for r in rows] == [attempt.id]


async def test_free_lesson_quiz_needs_the_lesson_opened(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=1)
    quiz, _ = await make_quiz(session, free[0])
    user = await make_student(session)
    with pytest.raises(AccessDenied):
        await quizzes.start_or_resume(session, user, quiz.id)


async def test_paid_quiz_requires_subscription(session):
    topic = await make_tree(session)
    _, paid = await make_lessons(session, topic, paid=1)
    quiz, _ = await make_quiz(session, paid[0])
    user = await make_student(session)
    with pytest.raises(SubscriptionRequired):
        await quizzes.start_or_resume(session, user, quiz.id)
    await subscribe(session, user)
    assert (await quizzes.start_or_resume(session, user, quiz.id)).status == AttemptStatus.IN_PROGRESS


async def test_explicit_paid_mode_on_free_lesson(session):
    topic = await make_tree(session)
    free, _ = await make_lessons(session, topic, free=1)
    quiz, _ = await make_quiz(session, free[0], access_mode=QuizAccessMode.PAID)
    user = await make_student(session)
    await lessons.open_lesson(session, user, free[0])
    with pytest.raises(SubscriptionRequired):
        await quizzes.start_or_resume(session, user, quiz.id)


async def test_access_loss_mid_quiz_interrupts_and_keeps_answers(session):
    topic = await make_tree(session)
    _, paid = await make_lessons(session, topic, paid=1)
    quiz, _ = await make_quiz(session, paid[0], questions=3)
    user = await make_student(session)
    sub = await subscribe(session, user)

    attempt = await quizzes.start_or_resume(session, user, quiz.id)
    await _answer(session, user, attempt, correct=True)
    sub.ends_at = utcnow() - timedelta(seconds=1)  # subscription ended
    await session.flush()

    outcome = await _answer(session, user, attempt, correct=True)
    assert outcome.finished and not outcome.accepted
    assert attempt.status == AttemptStatus.INTERRUPTED
    assert attempt.score == Decimal("1.00")  # the answer given before access loss counts
    with pytest.raises(SubscriptionRequired):
        await quizzes.start_or_resume(session, user, quiz.id)
    assert len(await quizzes.results_for_user(session, user.id)) == 1  # history is kept


async def test_resume_returns_same_attempt_and_double_tap_is_ignored(session):
    topic = await make_tree(session)
    _, paid = await make_lessons(session, topic, paid=1)
    quiz, _ = await make_quiz(session, paid[0], questions=2)
    user = await make_student(session)
    await subscribe(session, user)

    attempt = await quizzes.start_or_resume(session, user, quiz.id)
    assert (await quizzes.start_or_resume(session, user, quiz.id)).id == attempt.id

    cq = await quizzes.current_question(session, attempt)
    assert cq is not None
    option_id = await _option(session, cq.question.id, correct=True)
    first = await quizzes.submit_answer(session, user, attempt.id, cq.question.id, option_ids=[option_id])
    again = await quizzes.submit_answer(session, user, attempt.id, cq.question.id, option_ids=[option_id])
    assert first.accepted and not again.accepted and attempt.current_index == 1


async def test_max_attempts_enforced(session):
    topic = await make_tree(session)
    _, paid = await make_lessons(session, topic, paid=1)
    quiz, _ = await make_quiz(session, paid[0], questions=1, max_attempts=2)
    user = await make_student(session)
    await subscribe(session, user)
    for n in (1, 2):
        attempt = await quizzes.start_or_resume(session, user, quiz.id)
        assert attempt.attempt_no == n
        await _answer(session, user, attempt, correct=False)
    with pytest.raises(Conflict):
        await quizzes.start_or_resume(session, user, quiz.id)


async def test_time_limit_expires_attempt(session):
    topic = await make_tree(session)
    _, paid = await make_lessons(session, topic, paid=1)
    quiz, _ = await make_quiz(session, paid[0], questions=2, time_limit_seconds=60)
    user = await make_student(session)
    await subscribe(session, user)
    attempt = await quizzes.start_or_resume(session, user, quiz.id)
    await _answer(session, user, attempt, correct=True)
    attempt.deadline_at = utcnow() - timedelta(seconds=1)
    await session.flush()
    outcome = await _answer(session, user, attempt, correct=True)
    assert outcome.finished and attempt.status == AttemptStatus.EXPIRED
    assert attempt.score == Decimal("1.00")


async def test_rendered_question_does_not_reveal_correct_option(session):
    """The question payload (text + keyboard) is identical whichever option is correct."""
    topic = await make_tree(session)
    _, paid = await make_lessons(session, topic, paid=1)
    quiz, questions = await make_quiz(session, paid[0], questions=1)
    user = await make_student(session)
    await subscribe(session, user)
    attempt = await quizzes.start_or_resume(session, user, quiz.id)
    state = FSMContext(
        MemoryStorage(), StorageKey(bot_id=1, chat_id=user.telegram_id, user_id=user.telegram_id)
    )

    def _payload(text, kb) -> str:
        return text + repr([[b.text for b in row] for row in kb.inline_keyboard])

    before = _payload(*(await quiz_screens.render_question(session, state, attempt.id, user))[:2])
    for opt in (await quizzes.options_for(session, [questions[0].id]))[questions[0].id]:
        opt.is_correct = not opt.is_correct  # flip which option is correct
    await session.flush()
    after = _payload(*(await quiz_screens.render_question(session, state, attempt.id, user))[:2])
    assert before == after
    assert "✔" not in before and "to‘g‘ri javob" not in before.lower()
