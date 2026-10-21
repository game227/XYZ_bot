"""Pure quiz logic: scoring, partial credit, text normalization, ordering, visibility."""

import random
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.database.models import QuestionType, QuizAccessMode, QuizResultVisibility
from app.keyboards.content import CtEd, CtLes, CtQe, CtQz
from app.services import quizzes as q
from app.services.errors import InvalidInput

# --------------------------------------------------------------------------- choice scoring


def test_single_correct_and_wrong() -> None:
    assert q.score_choice(multiple=False, points=2, correct_ids=[5], selected_ids=[5]) == (
        True,
        Decimal("2.00"),
    )
    assert q.score_choice(multiple=False, points=2, correct_ids=[5], selected_ids=[6]) == (
        False,
        Decimal("0.00"),
    )


def test_multiple_requires_exact_set_without_partial_credit() -> None:
    assert q.score_choice(multiple=True, points=3, correct_ids=[1, 2], selected_ids=[2, 1])[0] is True
    assert q.score_choice(multiple=True, points=3, correct_ids=[1, 2], selected_ids=[1]) == (
        False,
        Decimal("0.00"),
    )
    assert q.score_choice(multiple=True, points=3, correct_ids=[1, 2], selected_ids=[1, 2, 3])[1] == Decimal(
        "0.00"
    )


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ([1, 2, 3], Decimal("4.00")),  # exact
        ([1, 2], Decimal("2.67")),  # 2/3 of 4
        ([1], Decimal("1.33")),
        ([1, 2, 9], Decimal("1.33")),  # one wrong cancels one right: (2-1)/3
        ([1, 8, 9], Decimal("0.00")),  # never negative
        ([8], Decimal("0.00")),
    ],
)
def test_partial_credit(selected: list[int], expected: Decimal) -> None:
    ok, points = q.score_choice(
        multiple=True, points=4, correct_ids=[1, 2, 3], selected_ids=selected, partial_credit=True
    )
    assert points == expected
    assert ok is (selected == [1, 2, 3])


def test_partial_credit_ignored_for_single() -> None:
    assert (
        q.score_choice(multiple=False, points=4, correct_ids=[1], selected_ids=[2], partial_credit=True)[1]
        == 0
    )


# --------------------------------------------------------------------------- text answers


@pytest.mark.parametrize(
    ("given", "accepted"),
    [
        ("Toshkent", ["toshkent"]),
        ("  TOSHKENT  ", ["Toshkent"]),
        ("O‘zbekiston", ["O'zbekiston"]),
        ("Oʻzbekiston", ["O‘zbekiston"]),
        ("o`zbekiston", ["O’zbekiston"]),
        ("ta’lim", ["ta'lim"]),
        ("Amir   Temur.", ["Amir Temur"]),
        ("ｆｕｌｌｗｉｄｔｈ", ["fullwidth"]),  # NFKC
    ],
)
def test_text_answer_normalization_accepts(given: str, accepted: list[str]) -> None:
    assert q.score_text(points=1, accepted=accepted, answer=given) == (True, Decimal("1.00"))


@pytest.mark.parametrize("given", ["", "   ", "Samarqand", "Toshkent shahri"])
def test_text_answer_rejects(given: str) -> None:
    assert q.score_text(points=1, accepted=["Toshkent"], answer=given) == (False, Decimal("0.00"))


# --------------------------------------------------------------------------- summary / attempts / visibility


def test_summarize() -> None:
    assert q.summarize(Decimal("6"), Decimal("10"), 60) == (Decimal("60.00"), True)
    assert q.summarize(Decimal("5.99"), Decimal("10"), 60) == (Decimal("59.90"), False)
    assert q.summarize(Decimal("1"), Decimal("3"), 30) == (Decimal("33.33"), True)
    assert q.summarize(Decimal("0"), Decimal("0"), 0) == (Decimal("0.00"), False)


def test_attempts_left() -> None:
    assert q.attempts_left(None, 10) is None
    assert q.attempts_left(3, 1) == 2
    assert q.attempts_left(3, 5) == 0


@pytest.mark.parametrize(
    ("visibility", "left", "expected"),
    [
        (QuizResultVisibility.HIDDEN, 2, (False, False)),
        (QuizResultVisibility.SCORE_ONLY, 0, (True, False)),
        (QuizResultVisibility.SCORE_AND_ANSWERS, 2, (True, True)),
        (QuizResultVisibility.ANSWERS_AFTER_LAST_ATTEMPT, 1, (True, False)),
        (QuizResultVisibility.ANSWERS_AFTER_LAST_ATTEMPT, None, (True, False)),
        (QuizResultVisibility.ANSWERS_AFTER_LAST_ATTEMPT, 0, (True, True)),
    ],
)
def test_answer_visibility(
    visibility: QuizResultVisibility, left: int | None, expected: tuple[bool, bool]
) -> None:
    assert q.answer_visibility(visibility, left) == expected


def test_effective_mode() -> None:
    free_lesson, paid_lesson = SimpleNamespace(is_free=True), SimpleNamespace(is_free=False)
    inherit = SimpleNamespace(access_mode=QuizAccessMode.INHERIT)
    assert q.effective_mode(inherit, free_lesson) == QuizAccessMode.FREE  # type: ignore[arg-type]
    assert q.effective_mode(inherit, paid_lesson) == QuizAccessMode.PAID  # type: ignore[arg-type]
    paid = SimpleNamespace(access_mode=QuizAccessMode.PAID)
    assert q.effective_mode(paid, free_lesson) == QuizAccessMode.PAID  # type: ignore[arg-type]


# --------------------------------------------------------------------------- ordering


def test_build_order_keeps_order_without_shuffle() -> None:
    order, opts = q.build_order(
        [3, 1, 2], {3: [30, 31], 1: [10], 2: []}, shuffle_questions=False, shuffle_options=False
    )
    assert order == [3, 1, 2]
    assert opts == {"3": [30, 31], "1": [10], "2": []}


def test_build_order_shuffle_is_a_permutation() -> None:
    qids = list(range(1, 21))
    options = {qid: [qid * 10 + i for i in range(5)] for qid in qids}
    order, opts = q.build_order(
        qids, options, shuffle_questions=True, shuffle_options=True, rng=random.Random(7)
    )
    assert sorted(order) == qids and order != qids
    assert all(sorted(opts[str(qid)]) == options[qid] for qid in qids)
    assert any(opts[str(qid)] != options[qid] for qid in qids)


# --------------------------------------------------------------------------- admin input parsing


def test_parse_options_single() -> None:
    items = q.parse_options_block("+Toshkent\nSamarqand\n\n  Buxoro  ", QuestionType.SINGLE)
    assert items == [("Toshkent", True), ("Samarqand", False), ("Buxoro", False)]


def test_parse_options_text_lines_are_all_accepted() -> None:
    assert q.parse_options_block("Toshkent\nTashkent", QuestionType.TEXT) == [
        ("Toshkent", True),
        ("Tashkent", True),
    ]


@pytest.mark.parametrize(
    ("raw", "qtype"),
    [
        ("A\nB", QuestionType.SINGLE),  # no correct
        ("+A\n+B", QuestionType.SINGLE),  # two correct for single
        ("+A", QuestionType.MULTIPLE),  # one option only
        ("+A\na", QuestionType.SINGLE),  # duplicates after normalization
        ("A\nB", QuestionType.MULTIPLE),  # no correct
        ("", QuestionType.TEXT),
        ("+" + "x" * 300 + "\nB", QuestionType.SINGLE),  # choice too long
        ("\n".join(f"+{i}" if i == 0 else str(i) for i in range(11)), QuestionType.SINGLE),  # too many
    ],
)
def test_parse_options_rejects(raw: str, qtype: QuestionType) -> None:
    with pytest.raises(InvalidInput):
        q.parse_options_block(raw, qtype)


# --------------------------------------------------------------------------- callback data


def test_callback_data_fits_telegram_limit() -> None:
    """aiogram refuses to pack > 64 bytes; ids up to 12 digits must always fit."""
    big = 999_999_999_999
    samples = [
        CtQz(action="toggle", attempt_id=big, question_id=big, option_id=big),
        CtQz(action="submit", attempt_id=big, question_id=big),
        CtEd(action="detach", kind="lesson", id=big, field="t", extra=big),
        CtQe(action="cycle", id=big, arg="result_visibility"),
        CtLes(action="confirm", lesson_id=big, media_id=big),
    ]
    for data in samples:
        assert len(data.pack().encode()) <= 64
