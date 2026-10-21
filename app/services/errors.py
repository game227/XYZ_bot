class ServiceError(Exception):
    """Base for expected, user-presentable errors.

    `user_message` is shown to the user as-is (Uzbek, no technical details).
    The global error handler turns these into a reply; anything else becomes a
    generic "xatolik" message and is logged.
    """

    default_message = "Amalni bajarib bo‘lmadi."

    def __init__(self, user_message: str | None = None) -> None:
        self.user_message = user_message or self.default_message
        super().__init__(self.user_message)


class AccessDenied(ServiceError):
    default_message = "Bu bo‘limga kirish huquqingiz yo‘q."


class NotFound(ServiceError):
    default_message = "Ma’lumot topilmadi."


class InvalidInput(ServiceError):
    default_message = "Noto‘g‘ri ma’lumot kiritildi."


class Conflict(ServiceError):
    default_message = "Bu amal allaqachon bajarilgan."


class TeacherSessionRequired(AccessDenied):
    default_message = "Sessiya muddati tugagan yoki faol emas. Iltimos, qayta kiring."


class SubscriptionRequired(AccessDenied):
    default_message = "Bu material faqat faol obuna bilan ochiladi. 💳 Obuna olish bo‘limiga o‘ting."
