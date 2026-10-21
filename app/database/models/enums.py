from enum import StrEnum

import sqlalchemy as sa


def str_enum(enum_cls: type[StrEnum], length: int = 32) -> sa.Enum:
    """Stored as VARCHAR + CHECK (no native PG enum), so adding values is a cheap migration."""
    return sa.Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class UserRole(StrEnum):
    ADMIN = "admin"
    TEACHER = "teacher"
    STUDENT = "student"


class UserStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class AdminPermission(StrEnum):
    """Granular admin rights. Superadmins (SUPERADMIN_IDS) implicitly have all of them."""

    USERS = "users"  # view/block users, change roles
    ADMINS = "admins"  # grant/revoke admin permissions
    TEACHERS = "teachers"  # create/block teacher accounts, course assignments
    TEACHER_SESSIONS = "teacher_sessions"  # view/revoke teacher sessions
    CONTENT = "content"  # subjects/courses/modules/topics/lessons/media
    QUIZZES = "quizzes"
    PLANS = "plans"  # subscription plans
    PAYMENTS = "payments"  # review payment requests, manage subscriptions
    NOTIFICATIONS = "notifications"
    LIVE = "live"
    STATS = "stats"
    AUDIT = "audit"
    SETTINGS = "settings"


class TeacherSessionEndReason(StrEnum):
    LOGOUT = "logout"
    ADMIN_REVOKED = "admin_revoked"
    PASSWORD_CHANGED = "password_changed"
    REPLACED = "replaced"  # new login replaced the old session
    ACCOUNT_BLOCKED = "account_blocked"


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"
    IMAGE = "image"
    DOCUMENT = "document"


class MediaStatus(StrEnum):
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class QuizAccessMode(StrEnum):
    INHERIT = "inherit"  # same as the lesson (default)
    FREE = "free"
    PAID = "paid"


class QuizResultVisibility(StrEnum):
    HIDDEN = "hidden"
    SCORE_ONLY = "score_only"
    SCORE_AND_ANSWERS = "score_and_answers"
    ANSWERS_AFTER_LAST_ATTEMPT = "answers_after_last_attempt"


class QuestionType(StrEnum):
    SINGLE = "single"
    MULTIPLE = "multiple"
    TEXT = "text"


class AttemptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    EXPIRED = "expired"  # time limit ran out
    INTERRUPTED = "interrupted"  # access lost mid-quiz; answers so far are scored


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"  # still check ends_at: active + ends_at <= now means expired
    CANCELLED = "cancelled"


class PaymentRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LiveSessionStatus(StrEnum):
    SCHEDULED = "scheduled"
    STARTED = "started"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class LiveLinkVisibility(StrEnum):
    PUBLIC = "public"
    SUBSCRIBERS = "subscribers"
    REGISTERED = "registered"


class NotificationAudience(StrEnum):
    ALL = "all"
    ROLE = "role"
    COURSE = "course"
    USERS = "users"
    SINGLE = "single"


class NotificationStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENDING = "sending"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"  # user blocked the bot


class ChatSenderRole(StrEnum):
    TEACHER = "teacher"
    STUDENT = "student"
