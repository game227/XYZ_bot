import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database.models import AdminPermission, TeacherAccount, User, UserRole
from app.handlers.common.start import main_menu_for
from app.keyboards import menus
from app.services import admins
from app.services.errors import AccessDenied, Conflict, InvalidInput

pytestmark = pytest.mark.integration

SUPERADMIN_TG = 7564700491


@pytest.fixture
def superadmin_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "superadmin_ids", [SUPERADMIN_TG])


async def test_add_admin_by_telegram_id_before_first_start(session, make_user, superadmin_ids) -> None:
    root = await make_user(UserRole.ADMIN, telegram_id=SUPERADMIN_TG)
    new_admin, created = await admins.add_admin(session, root, 555_000_111)
    assert created and new_admin.role == UserRole.ADMIN
    # The superadmin holds every permission, so the new admin gets all of them (incl. adding admins).
    assert set(new_admin.permissions) == {p.value for p in AdminPermission}
    stored = await session.scalar(select(User).where(User.telegram_id == 555_000_111))
    assert stored is not None and stored.role == UserRole.ADMIN


async def test_new_admin_gets_only_the_adders_permissions(session, make_user, superadmin_ids) -> None:
    limited = await make_user(UserRole.ADMIN, permissions=["admins", "content"])
    target = await make_user()
    added, created = await admins.add_admin(session, limited, target.telegram_id)
    assert not created
    assert set(added.permissions) == {"admins", "content"}


async def test_admin_without_admins_permission_cannot_add(session, make_user, superadmin_ids) -> None:
    content_admin = await make_user(UserRole.ADMIN, permissions=["content"])
    with pytest.raises(AccessDenied):
        await admins.add_admin(session, content_admin, 555_000_222)


async def test_add_rejects_duplicates_superadmin_and_bad_ids(session, make_user, superadmin_ids) -> None:
    root = await make_user(UserRole.ADMIN, telegram_id=SUPERADMIN_TG)
    other = await make_user(UserRole.ADMIN, permissions=[])
    with pytest.raises(Conflict):
        await admins.add_admin(session, root, other.telegram_id)
    with pytest.raises(Conflict):
        await admins.add_admin(session, root, SUPERADMIN_TG)
    for bad in ["abc", "12", "", "-5", "1234567890123456"]:
        with pytest.raises(InvalidInput):
            admins.parse_telegram_id(bad)
    assert admins.parse_telegram_id(" 123456789 ") == 123456789


async def test_remove_admin_rules(session, make_user, superadmin_ids) -> None:
    root = await make_user(UserRole.ADMIN, telegram_id=SUPERADMIN_TG)
    plain = await make_user(UserRole.ADMIN, permissions=["admins"])
    teacher_admin = await make_user(UserRole.ADMIN, permissions=[])
    session.add(TeacherAccount(login="t_admin", password_hash="x", full_name="T", user_id=teacher_admin.id))
    await session.flush()

    removed = await admins.remove_admin(session, root, plain.id)
    assert removed.role == UserRole.STUDENT and removed.permissions == []
    # A bound teacher account keeps its teacher role after losing admin rights.
    assert (await admins.remove_admin(session, root, teacher_admin.id)).role == UserRole.TEACHER

    with pytest.raises(AccessDenied):
        await admins.remove_admin(session, root, root.id)  # superadmin can't be removed
    another = await make_user(UserRole.ADMIN, permissions=["admins"])
    with pytest.raises(AccessDenied):
        await admins.remove_admin(session, another, another.id)  # not yourself


async def test_admins_use_regular_menu_with_admin_button(session, make_user, superadmin_ids) -> None:
    admin = await make_user(UserRole.ADMIN, permissions=["content"])
    student = await make_user()

    def texts(markup) -> set[str]:
        return {b.text for row in markup.keyboard for b in row}

    admin_buttons = texts(main_menu_for(admin))
    assert menus.BTN_ADMIN_MENU in admin_buttons and menus.BTN_COURSES in admin_buttons
    assert menus.BTN_A_USERS not in admin_buttons  # admin panel opens only via «🛠 Admin»
    assert menus.BTN_ADMIN_MENU not in texts(main_menu_for(student))
    # Admins can also open the teacher cabinet, from the main menu and from the admin panel.
    assert menus.BTN_TEACHER_ENTRY in admin_buttons
    assert menus.BTN_TEACHER_ENTRY in texts(menus.admin_menu())


async def test_one_account_can_be_student_teacher_and_admin(session, make_user, monkeypatch, superadmin_ids):
    from app.handlers.common.start import role_labels
    from app.services import auth, lessons
    from app.services.errors import TeacherSessionRequired
    from tests.teacher_helpers import (
        TEACHER_PASSWORD,
        assign,
        make_course,
        make_limiter,
        make_teacher,
        use_cheap_hasher,
    )

    use_cheap_hasher(monkeypatch)
    limiter, _ = make_limiter()
    admin = await make_user(UserRole.ADMIN, permissions=["admins"])  # no content permission
    account = await make_teacher(session, login="admin_teacher")
    course = await make_course(session)
    await assign(session, account, course, can_manage_content=True)

    assert await role_labels(session, admin) == ["O‘quvchi", "Administrator"]
    with pytest.raises(TeacherSessionRequired):
        await lessons.ensure_can_manage_course(session, admin, course.id)

    await auth.login(session, user=admin, login="admin_teacher", password=TEACHER_PASSWORD, limiter=limiter)
    await session.refresh(admin)
    assert admin.role == UserRole.ADMIN  # logging in as a teacher keeps admin rights
    await lessons.ensure_can_manage_course(session, admin, course.id)  # teacher rights now apply
    assert await role_labels(session, admin) == ["O‘quvchi", "O‘qituvchi", "Administrator"]
    assert menus.BTN_ADMIN_MENU in {b.text for row in main_menu_for(admin).keyboard for b in row}
