"""Interactive quizzes (TZ §8): engine, scoring, results and management.

OWNER: CONTENT.

Engine rules:
- Progress lives in `quiz_attempts` (question order, option order, current index); FSM only
  holds transient UI state (e.g. the options ticked for a multiple-choice question).
- Access follows the lesson (`access_mode`) and is re-checked on every answer. When access
  is lost mid-quiz the attempt becomes `interrupted`; answers given so far are scored and saved.
- At most one `in_progress` attempt per user and quiz (partial unique index). Starting is
  serialized with a users-row lock so `attempt_no` / `max_attempts` stay consistent.
- Correct answers are never sent before the attempt ends; `result_visibility` decides what
  is shown afterwards.
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AnswerOption,
    AttemptAnswer,
    AttemptStatus,
    Course,
    Lesson,
    QuestionType,
    Quiz,
    QuizAccessMode,
    QuizAttempt,
    QuizResultVisibility,
    User,
)
from app.database.models import Question as QuestionModel
from app.services import audit, lessons
from app.services.errors import AccessDenied, Conflict, InvalidInput, NotFound, SubscriptionRequired
from app.services.subscriptions import has_active_subscription
from app.utils.time import utcnow

TEXT_ANSWER_MAX = 500
QUESTION_TEXT_MAX = 3000
OPTION_TEXT_MAX = 1000
MAX_OPTIONS = 10  # keeps the inline keyboard usable
TWO_PLACES = Decimal("0.01")

_rng = random.SystemRandom()

# --------------------------------------------------------------------------- pure helpers

# Uzbek texts mix several apostrophe-like characters (o‘, o', oʻ, o`); treat them as one.
_APOSTROPHES = str.maketrans({c: "'" for c in "‘’ʻʼ`´′ʹ"})
_SPACES = re.compile(r"\s+")


def normalize_text_answer(value: str | None) -> str:
    """Canonical form for comparing typed answers: NFKC, casefold, unified apostrophes,
    collapsed whitespace, trailing punctuation ignored."""
    value = unicodedata.normalize("NFKC", value or "").translate(_APOSTROPHES).casefold()
    value = _SPACES.sub(" ", value).strip()
    return value.rstrip(".!?;,").strip()


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def score_choice(
    *,
    multiple: bool,
    points: int | Decimal,
    correct_ids: Iterable[int],
    selected_ids: Iterable[int],
    partial_credit: bool = False,
) -> tuple[bool, Decimal]:
    """Score a single/multiple-choice answer.

    Exact match → full points. With `partial_credit` (multiple only) each right pick earns
    points/len(correct) and each wrong pick cancels one right pick (never below zero).
    """
    correct, selected = set(correct_ids), set(selected_ids)
    full = Decimal(points)
    if selected == correct and correct:
        return True, _quantize(full)
    if multiple and partial_credit and correct:
        right = len(selected & correct)
        wrong = len(selected - correct)
        share = Decimal(max(0, right - wrong)) / Decimal(len(correct))
        return False, _quantize(full * share)
    return False, Decimal("0.00")


def score_text(*, points: int | Decimal, accepted: Iterable[str], answer: str | None) -> tuple[bool, Decimal]:
    given = normalize_text_answer(answer)
    ok = bool(given) and given in {normalize_text_answer(a) for a in accepted}
    return ok, _quantize(Decimal(points)) if ok else Decimal("0.00")


def summarize(score: Decimal, max_score: Decimal, pass_percent: int) -> tuple[Decimal, bool]:
    """(percent, passed). An empty quiz (max 0) counts as 0 % / not passed."""
    if max_score <= 0:
        return Decimal("0.00"), False
    percent = _quantize(score * 100 / max_score)
    return percent, percent >= Decimal(pass_percent)


def attempts_left(max_attempts: int | None, used: int) -> int | None:
    """None = unlimited."""
    return None if max_attempts is None else max(0, max_attempts - used)


def answer_visibility(visibility: QuizResultVisibility, left: int | None) -> tuple[bool, bool]:
    """(show_score, show_correct_answers) after an attempt has ended."""
    match visibility:
        case QuizResultVisibility.HIDDEN:
            return False, False
        case QuizResultVisibility.SCORE_ONLY:
            return True, False
        case QuizResultVisibility.SCORE_AND_ANSWERS:
            return True, True
        case QuizResultVisibility.ANSWERS_AFTER_LAST_ATTEMPT:
            return True, left == 0
    return True, False


def effective_mode(quiz: Quiz, lesson: Lesson) -> QuizAccessMode:
    if quiz.access_mode == QuizAccessMode.INHERIT:
        return QuizAccessMode.FREE if lesson.is_free else QuizAccessMode.PAID
    return QuizAccessMode(quiz.access_mode)


def build_order(
    question_ids: Sequence[int],
    options_by_question: dict[int, list[int]],
    *,
    shuffle_questions: bool,
    shuffle_options: bool,
    rng: random.Random | None = None,
) -> tuple[list[int], dict[str, list[int]]]:
    rng = rng or _rng
    q_order = list(question_ids)
    if shuffle_questions:
        rng.shuffle(q_order)
    o_order: dict[str, list[int]] = {}
    for qid in q_order:
        opts = list(options_by_question.get(qid, []))
        if shuffle_options:
            rng.shuffle(opts)
        o_order[str(qid)] = opts
    return q_order, o_order


def parse_options_block(raw: str, qtype: QuestionType) -> list[tuple[str, bool]]:
    """Admin input → options. One option per line; a leading '+' marks a correct one.

    For TEXT questions every line is an accepted answer.
    """
    items: list[tuple[str, bool]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        correct = qtype == QuestionType.TEXT
        if line.startswith("+"):
            correct, line = True, line[1:].strip()
        elif line.startswith("-"):
            line = line[1:].strip()
        if not line:
            continue
        if len(line) > OPTION_TEXT_MAX:
            raise InvalidInput(f"Variant {OPTION_TEXT_MAX} belgidan oshmasligi kerak.")
        items.append((line, correct))
    validate_options(qtype, items)
    return items


def validate_options(qtype: QuestionType, items: Sequence[tuple[str, bool]]) -> None:
    n_correct = sum(1 for _, ok in items if ok)
    if qtype == QuestionType.TEXT:
        if n_correct < 1:
            raise InvalidInput("Kamida bitta to‘g‘ri javob kiriting.")
        return
    if not 2 <= len(items) <= MAX_OPTIONS:
        raise InvalidInput(f"Variantlar soni 2 dan {MAX_OPTIONS} tagacha bo‘lishi kerak.")
    if len({normalize_text_answer(t) for t, _ in items}) != len(items):
        raise InvalidInput("Variantlar takrorlanmasligi kerak.")
    if qtype == QuestionType.SINGLE and n_correct != 1:
        raise InvalidInput("Bitta to‘g‘ri javobli savolda aynan bitta variant ‘+’ bilan belgilanadi.")
    if qtype == QuestionType.MULTIPLE and n_correct < 1:
        raise InvalidInput("Kamida bitta to‘g‘ri variantni ‘+’ bilan belgilang.")


# --------------------------------------------------------------------------- lookups


async def get_quiz(session: AsyncSession, quiz_id: int) -> Quiz:
    quiz = await session.get(Quiz, quiz_id)
    if quiz is None:
        raise NotFound("Test topilmadi.")
    return quiz


async def list_lesson_quizzes(
    session: AsyncSession, lesson_id: int, *, include_inactive: bool = False
) -> list[Quiz]:
    stmt = select(Quiz).where(Quiz.lesson_id == lesson_id).order_by(Quiz.position, Quiz.id)
    if not include_inactive:
        stmt = stmt.where(Quiz.is_active.is_(True))
    return list(await session.scalars(stmt))


async def list_questions(session: AsyncSession, quiz_id: int) -> list[QuestionModel]:
    stmt = (
        select(QuestionModel)
        .where(QuestionModel.quiz_id == quiz_id)
        .order_by(QuestionModel.position, QuestionModel.id)
    )
    return list(await session.scalars(stmt))


async def options_for(session: AsyncSession, question_ids: Sequence[int]) -> dict[int, list[AnswerOption]]:
    result: dict[int, list[AnswerOption]] = {qid: [] for qid in question_ids}
    if not question_ids:
        return result
    stmt = (
        select(AnswerOption)
        .where(AnswerOption.question_id.in_(question_ids))
        .order_by(AnswerOption.position, AnswerOption.id)
    )
    for opt in await session.scalars(stmt):
        result[opt.question_id].append(opt)
    return result


async def quiz_max_score(session: AsyncSession, quiz_id: int) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(QuestionModel.points), 0)).where(QuestionModel.quiz_id == quiz_id)
    )
    return Decimal(total or 0)


async def attempts_used(session: AsyncSession, user_id: int, quiz_id: int) -> int:
    stmt = select(func.count()).where(QuizAttempt.user_id == user_id, QuizAttempt.quiz_id == quiz_id)
    return await session.scalar(stmt) or 0


# --------------------------------------------------------------------------- access


async def can_take_quiz(session: AsyncSession, user: User, quiz: Quiz, lesson: Lesson | None = None) -> bool:
    """Server-side check before starting and on every answer."""
    lesson = lesson or await lessons.get_lesson(session, quiz.lesson_id)
    if await lessons.can_preview_course(session, user, lesson.course_id):
        return True
    if not quiz.is_active or not await lessons.is_lesson_public(session, lesson):
        return False
    if await has_active_subscription(session, user.id):
        return True
    if effective_mode(quiz, lesson) == QuizAccessMode.PAID:
        return False
    if quiz.access_mode == QuizAccessMode.FREE and not lesson.is_free:
        # Explicitly free quiz on a paid lesson: open to everyone.
        return True
    # Free lesson: the user must have opened it through their free allowance.
    return await lessons.can_view_lesson(session, user, lesson)


async def ensure_can_take(session: AsyncSession, user: User, quiz: Quiz, lesson: Lesson) -> None:
    if await can_take_quiz(session, user, quiz, lesson):
        return
    if not quiz.is_active or not await lessons.is_lesson_public(session, lesson):
        raise NotFound("Test hozircha mavjud emas.")
    if effective_mode(quiz, lesson) == QuizAccessMode.PAID:
        raise SubscriptionRequired()
    raise AccessDenied("Avval shu bepul darsni oching — test dars ichida joylashgan.")


# --------------------------------------------------------------------------- engine


@dataclass(slots=True)
class CurrentQuestion:
    attempt: QuizAttempt
    question: QuestionModel
    options: list[AnswerOption]  # in display order (never reveals is_correct to the UI layer)
    index: int  # 0-based
    total: int


@dataclass(slots=True)
class AnswerOutcome:
    attempt: QuizAttempt
    accepted: bool  # False when the tap was stale/duplicate
    finished: bool
    message: str | None = None


@dataclass(slots=True)
class ReviewItem:
    question: QuestionModel
    options: list[AnswerOption]
    answer: AttemptAnswer | None


@dataclass(slots=True)
class AttemptSummary:
    attempt: QuizAttempt
    quiz: Quiz
    show_score: bool
    show_answers: bool
    attempts_left: int | None
    review: list[ReviewItem] = field(default_factory=list)


async def _lock_user(session: AsyncSession, user_id: int) -> None:
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())


async def _in_progress(session: AsyncSession, user_id: int, quiz_id: int) -> QuizAttempt | None:
    stmt = select(QuizAttempt).where(
        QuizAttempt.user_id == user_id,
        QuizAttempt.quiz_id == quiz_id,
        QuizAttempt.status == AttemptStatus.IN_PROGRESS,
    )
    return await session.scalar(stmt)


def is_overdue(attempt: QuizAttempt, now: datetime | None = None) -> bool:
    return attempt.deadline_at is not None and (now or utcnow()) >= attempt.deadline_at


async def start_or_resume(session: AsyncSession, user: User, quiz_id: int) -> QuizAttempt:
    """Resume the user's in-progress attempt or start a new one (checks access + attempts)."""
    quiz = await get_quiz(session, quiz_id)
    lesson = await lessons.get_lesson(session, quiz.lesson_id)
    await ensure_can_take(session, user, quiz, lesson)
    await _lock_user(session, user.id)

    current = await _in_progress(session, user.id, quiz.id)
    if current is not None:
        if not is_overdue(current):
            return current
        await finish_attempt(session, current, AttemptStatus.EXPIRED)

    used = await attempts_used(session, user.id, quiz.id)
    if attempts_left(quiz.max_attempts, used) == 0:
        raise Conflict("Bu test uchun urinishlar soni tugagan.")

    questions = await list_questions(session, quiz.id)
    if not questions:
        raise NotFound("Bu testda hali savollar yo‘q.")
    opts = await options_for(session, [q.id for q in questions])
    q_order, o_order = build_order(
        [q.id for q in questions],
        {qid: [o.id for o in items] for qid, items in opts.items()},
        shuffle_questions=quiz.shuffle_questions,
        shuffle_options=quiz.shuffle_options,
    )
    now = utcnow()
    attempt = QuizAttempt(
        quiz_id=quiz.id,
        user_id=user.id,
        attempt_no=used + 1,
        status=AttemptStatus.IN_PROGRESS,
        started_at=now,
        deadline_at=now + timedelta(seconds=quiz.time_limit_seconds) if quiz.time_limit_seconds else None,
        question_order=q_order,
        option_order=o_order,
        current_index=0,
        max_score=Decimal(sum(q.points for q in questions)),
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def get_user_attempt(
    session: AsyncSession, user: User, attempt_id: int, *, lock: bool = False
) -> QuizAttempt:
    stmt = select(QuizAttempt).where(QuizAttempt.id == attempt_id, QuizAttempt.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    attempt = await session.scalar(stmt)
    if attempt is None:
        raise NotFound("Urinish topilmadi.")
    return attempt


async def current_question(session: AsyncSession, attempt: QuizAttempt) -> CurrentQuestion | None:
    order: list[int] = list(attempt.question_order or [])
    if attempt.status != AttemptStatus.IN_PROGRESS or attempt.current_index >= len(order):
        return None
    qid = order[attempt.current_index]
    question = await session.get(QuestionModel, qid)
    if question is None:  # deleted while the attempt was running: skip it
        attempt.current_index += 1
        await session.flush()
        return await current_question(session, attempt)
    by_id = {o.id: o for o in (await options_for(session, [qid]))[qid]}
    ordered = [by_id[i] for i in attempt.option_order.get(str(qid), []) if i in by_id]
    ordered += [o for i, o in by_id.items() if o not in ordered]  # options added later
    return CurrentQuestion(attempt, question, ordered, attempt.current_index, len(order))


async def submit_answer(
    session: AsyncSession,
    user: User,
    attempt_id: int,
    question_id: int,
    *,
    option_ids: Sequence[int] | None = None,
    text: str | None = None,
) -> AnswerOutcome:
    attempt = await get_user_attempt(session, user, attempt_id, lock=True)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return AnswerOutcome(attempt, accepted=False, finished=True, message="Bu urinish yakunlangan.")

    if is_overdue(attempt):
        await finish_attempt(session, attempt, AttemptStatus.EXPIRED)
        return AnswerOutcome(attempt, False, True, "⏰ Vaqt tugadi. Berilgan javoblar hisoblandi.")

    quiz = await get_quiz(session, attempt.quiz_id)
    lesson = await lessons.get_lesson(session, quiz.lesson_id)
    if not await can_take_quiz(session, user, quiz, lesson):
        await finish_attempt(session, attempt, AttemptStatus.INTERRUPTED)
        return AnswerOutcome(
            attempt, False, True, "Testga kirish huquqi tugadi. Shu paytgacha berilgan javoblar saqlandi."
        )

    cq = await current_question(session, attempt)
    if cq is None:
        await finish_attempt(session, attempt, AttemptStatus.COMPLETED)
        return AnswerOutcome(attempt, False, True)
    if cq.question.id != question_id:
        return AnswerOutcome(attempt, False, False, "Bu savolga javob allaqachon qabul qilingan.")

    question = cq.question
    if question.type == QuestionType.TEXT:
        answer_text = (text or "").strip()
        if not answer_text:
            raise InvalidInput("Javob matnini yuboring.")
        if len(answer_text) > TEXT_ANSWER_MAX:
            raise InvalidInput(f"Javob {TEXT_ANSWER_MAX} belgidan oshmasligi kerak.")
        accepted = [o.text for o in cq.options if o.is_correct]
        is_correct, points = score_text(points=question.points, accepted=accepted, answer=answer_text)
        row = AttemptAnswer(
            attempt_id=attempt.id,
            question_id=question.id,
            text_answer=answer_text,
            is_correct=is_correct,
            points_awarded=points,
        )
    else:
        selected = sorted(set(option_ids or []))
        valid_ids = {o.id for o in cq.options}
        if not selected or not set(selected) <= valid_ids:
            raise InvalidInput("Javob variantini tanlang.")
        if question.type == QuestionType.SINGLE and len(selected) != 1:
            raise InvalidInput("Faqat bitta variantni tanlang.")
        is_correct, points = score_choice(
            multiple=question.type == QuestionType.MULTIPLE,
            points=question.points,
            correct_ids=[o.id for o in cq.options if o.is_correct],
            selected_ids=selected,
            partial_credit=quiz.partial_credit,
        )
        row = AttemptAnswer(
            attempt_id=attempt.id,
            question_id=question.id,
            selected_option_ids=selected,
            is_correct=is_correct,
            points_awarded=points,
        )

    session.add(row)
    attempt.current_index += 1
    await session.flush()
    if attempt.current_index >= len(attempt.question_order or []):
        await finish_attempt(session, attempt, AttemptStatus.COMPLETED)
        return AnswerOutcome(attempt, True, True)
    return AnswerOutcome(attempt, True, False)


async def finish_attempt(session: AsyncSession, attempt: QuizAttempt, status: AttemptStatus) -> QuizAttempt:
    """Score the answers given so far and close the attempt (unanswered questions = 0)."""
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return attempt
    quiz = await get_quiz(session, attempt.quiz_id)
    total = await session.scalar(
        select(func.coalesce(func.sum(AttemptAnswer.points_awarded), 0)).where(
            AttemptAnswer.attempt_id == attempt.id
        )
    )
    score = _quantize(Decimal(total or 0))
    percent, passed = summarize(score, Decimal(attempt.max_score), quiz.pass_percent)
    attempt.status = status
    attempt.finished_at = utcnow()
    attempt.score = score
    attempt.percent = percent
    attempt.passed = passed
    await session.flush()
    return attempt


async def finish_early(session: AsyncSession, user: User, attempt_id: int) -> QuizAttempt:
    """The student ends the quiz before answering everything."""
    attempt = await get_user_attempt(session, user, attempt_id, lock=True)
    status = AttemptStatus.EXPIRED if is_overdue(attempt) else AttemptStatus.COMPLETED
    return await finish_attempt(session, attempt, status)


async def expire_overdue_attempts(session: AsyncSession, user_id: int) -> int:
    """Close the user's timed-out attempts (lazy cleanup before showing results)."""
    stmt = (
        select(QuizAttempt)
        .where(
            QuizAttempt.user_id == user_id,
            QuizAttempt.status == AttemptStatus.IN_PROGRESS,
            QuizAttempt.deadline_at.is_not(None),
            QuizAttempt.deadline_at <= utcnow(),
        )
        .with_for_update(skip_locked=True)
    )
    attempts = list(await session.scalars(stmt))
    for attempt in attempts:
        await finish_attempt(session, attempt, AttemptStatus.EXPIRED)
    return len(attempts)


async def attempt_summary(session: AsyncSession, user: User, attempt_id: int) -> AttemptSummary:
    attempt = await get_user_attempt(session, user, attempt_id)
    if attempt.status == AttemptStatus.IN_PROGRESS:
        raise Conflict("Test hali yakunlanmagan.")
    quiz = await get_quiz(session, attempt.quiz_id)
    left = attempts_left(quiz.max_attempts, await attempts_used(session, user.id, quiz.id))
    show_score, show_answers = answer_visibility(QuizResultVisibility(quiz.result_visibility), left)
    summary = AttemptSummary(attempt, quiz, show_score, show_answers, left)
    if show_answers:
        summary.review = await attempt_review(session, attempt)
    return summary


async def attempt_review(session: AsyncSession, attempt: QuizAttempt) -> list[ReviewItem]:
    order: list[int] = list(attempt.question_order or [])
    questions = {
        q.id: q
        for q in await session.scalars(select(QuestionModel).where(QuestionModel.id.in_(order or [-1])))
    }
    opts = await options_for(session, list(questions))
    answers = {
        a.question_id: a
        for a in await session.scalars(select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt.id))
    }
    return [
        ReviewItem(questions[qid], opts.get(qid, []), answers.get(qid)) for qid in order if qid in questions
    ]


# --------------------------------------------------------------------------- results


@dataclass(slots=True)
class ResultRow:
    attempt: QuizAttempt
    quiz_title: str
    lesson_title: str
    course_title: str
    user: User | None = None


def _results_stmt():
    return (
        select(QuizAttempt, Quiz.title, Lesson.title, Course.title)
        .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
        .join(Lesson, Lesson.id == Quiz.lesson_id)
        .join(Course, Course.id == Lesson.course_id)
        .where(QuizAttempt.status != AttemptStatus.IN_PROGRESS)
        .order_by(QuizAttempt.finished_at.desc(), QuizAttempt.id.desc())
    )


async def results_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    course_ids: set[int] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[ResultRow]:
    """Finished attempts of one user, newest first. `course_ids` restricts (teacher scope)."""
    stmt = _results_stmt().where(QuizAttempt.user_id == user_id)
    if course_ids is not None:
        stmt = stmt.where(Lesson.course_id.in_(course_ids or {-1}))
    rows = await session.execute(stmt.limit(limit).offset(offset))
    return [ResultRow(a, qt, lt, ct) for a, qt, lt, ct in rows.all()]


async def results_for_course(
    session: AsyncSession,
    course_id: int,
    *,
    quiz_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[ResultRow]:
    """Finished attempts in a course with their students, newest first.

    The caller checks the viewer's rights (AUTH: `can_view_results` on this course).
    """
    stmt = _results_stmt().add_columns(User).join(User, User.id == QuizAttempt.user_id)
    stmt = stmt.where(Lesson.course_id == course_id)
    if quiz_id is not None:
        stmt = stmt.where(QuizAttempt.quiz_id == quiz_id)
    rows = await session.execute(stmt.limit(limit).offset(offset))
    return [ResultRow(a, qt, lt, ct, u) for a, qt, lt, ct, u in rows.all()]


async def count_user_results(session: AsyncSession, user_id: int) -> int:
    stmt = select(func.count()).where(
        QuizAttempt.user_id == user_id, QuizAttempt.status != AttemptStatus.IN_PROGRESS
    )
    return await session.scalar(stmt) or 0


async def student_quizzes(
    session: AsyncSession, user: User, *, limit: int = 10, offset: int = 0
) -> tuple[list[tuple[Quiz, Lesson]], int]:
    """Active quizzes of publicly visible lessons, catalog order."""
    base = (
        lessons._public_lessons()
        .add_columns(Quiz)
        .join(Quiz, Quiz.lesson_id == Lesson.id)
        .where(Quiz.is_active.is_(True))
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(Course.position, Course.id, Lesson.position, Lesson.id, Quiz.position, Quiz.id)
    rows = await session.execute(stmt.limit(limit).offset(offset))
    return [(quiz, lesson) for lesson, quiz in rows.all()], total


# --------------------------------------------------------------------------- management


async def _ensure_manage_quiz_lesson(session: AsyncSession, actor: User, lesson_id: int) -> Lesson:
    lesson = await lessons.get_lesson(session, lesson_id)
    await lessons.ensure_can_manage_course(session, actor, lesson.course_id, quizzes=True)
    return lesson


async def get_managed_quiz(session: AsyncSession, actor: User, quiz_id: int) -> tuple[Quiz, Lesson]:
    quiz = await get_quiz(session, quiz_id)
    lesson = await _ensure_manage_quiz_lesson(session, actor, quiz.lesson_id)
    return quiz, lesson


async def _log(session: AsyncSession, action: str, actor: User, quiz: Quiz, **details: object) -> None:
    await audit.log_action(
        session, action, actor=actor, entity_type="quiz", entity_id=quiz.id, details=details or None
    )


async def create_quiz(session: AsyncSession, actor: User, *, lesson_id: int, title: str) -> Quiz:
    await _ensure_manage_quiz_lesson(session, actor, lesson_id)
    position = (
        await session.scalar(
            select(func.coalesce(func.max(Quiz.position), 0)).where(Quiz.lesson_id == lesson_id)
        )
        or 0
    ) + 10
    # Inactive until it has questions and the author activates it.
    quiz = Quiz(
        lesson_id=lesson_id,
        title=lessons.clean_title(title),
        position=position,
        is_active=False,
        created_by_user_id=actor.id,
    )
    session.add(quiz)
    await session.flush()
    await _log(session, "quiz.created", actor, quiz, lesson_id=lesson_id, title=quiz.title)
    return quiz


def _parse_int(raw: str, *, lo: int, hi: int, what: str) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        raise InvalidInput(f"{what}: butun son kiriting.") from None
    if not lo <= value <= hi:
        raise InvalidInput(f"{what}: {lo} dan {hi} gacha bo‘lishi kerak.")
    return value


async def update_quiz_field(
    session: AsyncSession, actor: User, quiz_id: int, field_name: str, raw: str
) -> Quiz:
    quiz, _ = await get_managed_quiz(session, actor, quiz_id)
    match field_name:
        case "title":
            quiz.title = lessons.clean_title(raw)
        case "description":
            quiz.description = lessons.clean_text(raw, lessons.BODY_MAX)
        case "pass_percent":
            quiz.pass_percent = _parse_int(raw, lo=0, hi=100, what="O‘tish chegarasi (%)")
        case "time_limit":
            minutes = _parse_int(raw, lo=0, hi=600, what="Vaqt chegarasi (daqiqa)")
            quiz.time_limit_seconds = minutes * 60 or None
        case "max_attempts":
            value = _parse_int(raw, lo=0, hi=100, what="Urinishlar soni")
            quiz.max_attempts = value or None
        case _:
            raise InvalidInput()
    await session.flush()
    await _log(session, "quiz.updated", actor, quiz, field=field_name)
    return quiz


_TOGGLES = {"shuffle_questions", "shuffle_options", "partial_credit"}


async def toggle_quiz_flag(session: AsyncSession, actor: User, quiz_id: int, flag: str) -> Quiz:
    quiz, _ = await get_managed_quiz(session, actor, quiz_id)
    if flag == "is_active":
        if not quiz.is_active and not await list_questions(session, quiz.id):
            raise InvalidInput("Faollashtirishdan oldin kamida bitta savol qo‘shing.")
        quiz.is_active = not quiz.is_active
    elif flag in _TOGGLES:
        setattr(quiz, flag, not getattr(quiz, flag))
    else:
        raise InvalidInput()
    await session.flush()
    await _log(session, "quiz.updated", actor, quiz, **{flag: getattr(quiz, flag)})
    return quiz


def _cycle(values: Sequence[str], current: str) -> str:
    idx = values.index(current) if current in values else -1
    return values[(idx + 1) % len(values)]


async def cycle_quiz_option(session: AsyncSession, actor: User, quiz_id: int, option: str) -> Quiz:
    quiz, _ = await get_managed_quiz(session, actor, quiz_id)
    if option == "access_mode":
        quiz.access_mode = QuizAccessMode(_cycle([m.value for m in QuizAccessMode], quiz.access_mode))
        value: str = quiz.access_mode
    elif option == "result_visibility":
        quiz.result_visibility = QuizResultVisibility(
            _cycle([v.value for v in QuizResultVisibility], quiz.result_visibility)
        )
        value = quiz.result_visibility
    else:
        raise InvalidInput()
    await session.flush()
    await _log(session, "quiz.updated", actor, quiz, **{option: value})
    return quiz


async def add_question(
    session: AsyncSession,
    actor: User,
    quiz_id: int,
    *,
    qtype: QuestionType,
    text: str,
    options: Sequence[tuple[str, bool]],
    points: int = 1,
    explanation: str | None = None,
) -> QuestionModel:
    quiz, _ = await get_managed_quiz(session, actor, quiz_id)
    text = (text or "").strip()
    if not text:
        raise InvalidInput("Savol matni bo‘sh bo‘lmasligi kerak.")
    if len(text) > QUESTION_TEXT_MAX:
        raise InvalidInput(f"Savol {QUESTION_TEXT_MAX} belgidan oshmasligi kerak.")
    if not 1 <= points <= 100:
        raise InvalidInput("Ball 1 dan 100 gacha bo‘lishi kerak.")
    validate_options(qtype, options)
    position = (
        await session.scalar(
            select(func.coalesce(func.max(QuestionModel.position), 0)).where(QuestionModel.quiz_id == quiz.id)
        )
        or 0
    ) + 10
    question = QuestionModel(
        quiz_id=quiz.id,
        type=qtype,
        text=text,
        position=position,
        points=points,
        explanation=lessons.clean_text(explanation, QUESTION_TEXT_MAX),
    )
    session.add(question)
    await session.flush()
    for i, (opt_text, correct) in enumerate(options, start=1):
        session.add(AnswerOption(question_id=question.id, text=opt_text, is_correct=correct, position=i * 10))
    await session.flush()
    await _log(session, "quiz.question_added", actor, quiz, question_id=question.id, type=str(qtype))
    return question


async def get_managed_question(
    session: AsyncSession, actor: User, question_id: int
) -> tuple[QuestionModel, Quiz]:
    question = await session.get(QuestionModel, question_id)
    if question is None:
        raise NotFound("Savol topilmadi.")
    quiz, _ = await get_managed_quiz(session, actor, question.quiz_id)
    return question, quiz


async def delete_question(session: AsyncSession, actor: User, question_id: int) -> Quiz:
    question, quiz = await get_managed_question(session, actor, question_id)
    answered = await session.scalar(
        select(AttemptAnswer.id).where(AttemptAnswer.question_id == question_id).limit(1)
    )
    if answered is not None:
        raise Conflict(
            "Bu savolga o‘quvchilar javob bergan. Natijalar saqlanishi uchun uni o‘chirib bo‘lmaydi."
        )
    await session.delete(question)
    await session.flush()
    if quiz.is_active and not await list_questions(session, quiz.id):
        quiz.is_active = False
    await _log(session, "quiz.question_deleted", actor, quiz, question_id=question_id)
    return quiz


async def delete_quiz(session: AsyncSession, actor: User, quiz_id: int) -> int:
    """Delete a quiz without attempts; returns its lesson id."""
    quiz, lesson = await get_managed_quiz(session, actor, quiz_id)
    if await session.scalar(select(QuizAttempt.id).where(QuizAttempt.quiz_id == quiz_id).limit(1)):
        raise Conflict("Bu testni o‘quvchilar ishlagan. O‘chirish o‘rniga uni nofaol qiling.")
    await _log(session, "quiz.deleted", actor, quiz, title=quiz.title)
    await session.delete(quiz)
    await session.flush()
    return lesson.id
