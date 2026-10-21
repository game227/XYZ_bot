"""Main reply-keyboard menus (TZ §13). Handlers match buttons with F.text == BTN_*."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Student (TZ §13.1)
BTN_COURSES = "📚 Fanlar va kurslar"
BTN_FREE_LESSONS = "🎓 Bepul darslar"
BTN_QUIZZES = "🧪 Testlar"
BTN_VIDEO_LESSONS = "▶️ Video darslar"
BTN_SUBSCRIBE = "💳 Obuna olish"
BTN_LIVE = "📅 Jonli darslar"
BTN_MY_RESULTS = "📈 Mening natijalarim"
BTN_PROFILE = "👤 Profilim"
BTN_NOTIFICATIONS = "🔔 Bildirishnomalar"
BTN_HELP = "❓ Yordam"
BTN_TEACHER_ENTRY = "👨‍🏫 O‘qituvchi kabineti"  # opens teacher login / cabinet

# Teacher (TZ §13.2)
BTN_T_LOGIN = "🔐 O‘qituvchi kabinetiga kirish"
BTN_T_MY_COURSES = "📚 Mening kurslarim"
BTN_T_MATERIALS = "🎬 Dars materiallari"
BTN_T_QUIZZES = "🧪 Testlarni boshqarish"
BTN_T_CHATS = "💬 Chatlar"
BTN_T_STUDENTS = "👨‍🎓 O‘quvchilar"
BTN_T_RESULTS = "📊 Natijalar"
BTN_T_PASSWORD = "🔑 Parolni almashtirish"
BTN_T_LOGOUT = "🔒 Chiqish"

# Admin (TZ §13.3)
BTN_A_USERS = "👥 Foydalanuvchilar"
BTN_A_ADMINS = "👑 Adminlar"
BTN_A_TEACHERS = "👨‍🏫 O‘qituvchilar"
BTN_A_TEACHER_SESSIONS = "🔐 O‘qituvchi sessiyalari"
BTN_A_CONTENT = "📚 Fanlar va kurslar (admin)"
BTN_A_LESSONS_MEDIA = "🎬 Darslar va media"
BTN_A_QUIZZES = "🧪 Testlar (admin)"
BTN_A_PLANS = "💳 Tariflar va obunalar"
BTN_A_PAYMENT_REQUESTS = "🧾 Obuna so‘rovlari"
BTN_A_LIVE = "📅 Jonli darslar (admin)"
BTN_A_NOTIFICATIONS = "📢 Bildirishnomalar"
BTN_A_STATS = "📊 Statistika"
BTN_A_AUDIT = "🧾 Audit jurnali"
BTN_A_SETTINGS = "⚙️ Bot sozlamalari"

# Navigation
BTN_STUDENT_MENU = "⬅️ Asosiy menyu"
BTN_ADMIN_MENU = "🛠 Admin"
BTN_TEACHER_MENU = "👨‍🏫 O‘qituvchi menyusi"


def _kb(rows: list[list[str]], placeholder: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )


def student_menu(
    *, show_admin_switch: bool = False, show_teacher_switch: bool = False
) -> ReplyKeyboardMarkup:
    rows = [
        [BTN_COURSES, BTN_FREE_LESSONS],
        [BTN_QUIZZES, BTN_VIDEO_LESSONS],
        [BTN_SUBSCRIBE, BTN_LIVE],
        [BTN_MY_RESULTS, BTN_PROFILE],
        [BTN_NOTIFICATIONS, BTN_HELP],
    ]
    switches = []
    if show_teacher_switch:
        switches.append(BTN_TEACHER_MENU)
    else:
        switches.append(BTN_TEACHER_ENTRY)
    if show_admin_switch:
        switches.append(BTN_ADMIN_MENU)
    rows.append(switches)
    return _kb(rows, "Bo‘limni tanlang")


def teacher_menu(*, logged_in: bool) -> ReplyKeyboardMarkup:
    if not logged_in:
        return _kb([[BTN_T_LOGIN], [BTN_STUDENT_MENU]], "Kabinetga kiring")
    return _kb(
        [
            [BTN_T_MY_COURSES, BTN_T_MATERIALS],
            [BTN_T_QUIZZES, BTN_T_CHATS],
            [BTN_T_STUDENTS, BTN_T_RESULTS],
            [BTN_T_PASSWORD, BTN_T_LOGOUT],
            [BTN_STUDENT_MENU],
        ],
        "O‘qituvchi kabineti",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return _kb(
        [
            [BTN_A_USERS, BTN_A_ADMINS],
            [BTN_A_TEACHERS],
            [BTN_A_TEACHER_SESSIONS, BTN_A_CONTENT],
            [BTN_A_LESSONS_MEDIA, BTN_A_QUIZZES],
            [BTN_A_PLANS, BTN_A_PAYMENT_REQUESTS],
            [BTN_A_LIVE, BTN_A_NOTIFICATIONS],
            [BTN_A_STATS, BTN_A_AUDIT],
            [BTN_A_SETTINGS],
            [BTN_TEACHER_ENTRY, BTN_STUDENT_MENU],
        ],
        "Admin panel",
    )


# Every reply-keyboard button text. Pressing one always cancels the current FSM input
# (see app/middlewares/menu_reset.py), so state handlers never receive menu texts.
MENU_BUTTONS: frozenset[str] = frozenset(
    value for name, value in dict(globals()).items() if name.startswith("BTN_") and isinstance(value, str)
)
