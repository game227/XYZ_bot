"""Teacher Mini App: login form, password change, chat (TZ §7). OWNER: AUTH.

Every /teacher/api endpoint except login/resume requires BOTH:
- valid Telegram initData (`X-Telegram-Init-Data`, HMAC-checked by CORE's `webapp_user`), and
- `Authorization: Bearer <token>` of a valid session bound to that same Telegram user.
The session is re-read from the database on every request (no caching, no extension).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import ChatSenderRole, Course, TeacherChatMessage, User
from app.keyboards import menus
from app.services import auth, chat, teacher_sessions
from app.services.errors import (
    AccessDenied,
    Conflict,
    InvalidInput,
    NotFound,
    ServiceError,
    TeacherSessionRequired,
)
from app.utils.time import fmt_dt
from app.web.deps import get_db
from app.web.telegram_auth import webapp_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/teacher", tags=["teacher"])

PAGE_FILE = Path(__file__).resolve().parent.parent / "static" / "teacher" / "index.html"

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PAGE_HEADERS = {
    **NO_STORE,
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# --------------------------------------------------------------------------- errors


class ApiError:
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def _api_error(exc: ServiceError) -> ApiError:
    if isinstance(exc, auth.LoginLocked):
        return ApiError(429, "locked", exc.user_message, {"Retry-After": str(exc.retry_after)})
    if isinstance(exc, auth.LoginUnavailable):
        return ApiError(503, "unavailable", exc.user_message)
    if isinstance(exc, auth.PasswordChangeRequired):
        return ApiError(409, "password_change_required", exc.user_message)
    if isinstance(exc, auth.LoginFailed):
        return ApiError(401, "bad_credentials", exc.user_message)
    if isinstance(exc, TeacherSessionRequired):
        return ApiError(401, "session", exc.user_message)
    if isinstance(exc, AccessDenied):
        return ApiError(403, "forbidden", exc.user_message)
    if isinstance(exc, NotFound):
        return ApiError(404, "not_found", exc.user_message)
    if isinstance(exc, InvalidInput):
        return ApiError(422, "invalid", exc.user_message)
    if isinstance(exc, Conflict):
        return ApiError(409, "conflict", exc.user_message)
    return ApiError(400, "error", exc.user_message)


def _error_response(err: ApiError) -> JSONResponse:
    # Same shape as FastAPI's HTTPException body: {"detail": {...}}.
    return JSONResponse(
        {"detail": {"code": err.code, "message": err.message}},
        status_code=err.status,
        headers={**NO_STORE, **err.headers},
    )


def _ok(payload: dict) -> JSONResponse:
    return JSONResponse(payload, headers=NO_STORE)


# --------------------------------------------------------------------------- dependencies


@dataclass(slots=True)
class TeacherAuth:
    user: User
    ctx: teacher_sessions.TeacherContext


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def teacher_auth(
    user: Annotated[User, Depends(webapp_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> TeacherAuth:
    try:
        ctx = await teacher_sessions.require_teacher_token(session, _bearer(authorization), user)
    except TeacherSessionRequired as exc:
        err = _api_error(exc)
        raise HTTPException(err.status, {"code": err.code, "message": err.message}, headers=NO_STORE) from exc
    return TeacherAuth(user=user, ctx=ctx)


# --------------------------------------------------------------------------- bot side effects


def _bot() -> Bot:
    """Short-lived Bot client for side effects of web requests (closed by `async with`)."""
    return Bot(
        token=get_settings().bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


async def _send_bot(chat_id: int, text: str, reply_markup=None) -> None:  # noqa: ANN001
    try:
        async with _bot() as bot:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception:  # noqa: BLE001 - notification failure must not fail the request
        logger.warning("bot message from web failed", exc_info=True)


def _session_payload(ctx: teacher_sessions.TeacherContext, token: str) -> dict:
    return {
        "token": token,
        "expires_at": ctx.session.expires_at.isoformat(),
        "expires_at_local": fmt_dt(ctx.session.expires_at),
        "teacher": {"full_name": ctx.teacher.full_name, "login": ctx.teacher.login},
    }


# --------------------------------------------------------------------------- page


@router.get("/", include_in_schema=False)
async def page() -> FileResponse:
    return FileResponse(PAGE_FILE, media_type="text/html", headers=PAGE_HEADERS)


# --------------------------------------------------------------------------- auth endpoints


class LoginIn(BaseModel):
    login: str = Field(max_length=64)
    password: str = Field(max_length=auth.PASSWORD_MAX_LENGTH)
    new_password: str | None = Field(default=None, max_length=auth.PASSWORD_MAX_LENGTH)


@router.post("/api/login")
async def login(
    body: LoginIn,
    user: Annotated[User, Depends(webapp_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    try:
        result = await auth.login(
            session, user=user, login=body.login, password=body.password, new_password=body.new_password
        )
    except ServiceError as exc:
        return _error_response(_api_error(exc))
    await _send_bot(
        user.telegram_id,
        "✅ O‘qituvchi kabinetiga kirdingiz.\n"
        f"Sessiya <b>{fmt_dt(result.context.session.expires_at)}</b> gacha amal qiladi (24 soat).\n"
        "Chiqish: /logout",
        menus.teacher_menu(logged_in=True),
    )
    return _ok(_session_payload(result.context, result.token))


@router.post("/api/resume")
async def resume(
    user: Annotated[User, Depends(webapp_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Mini App reopened: if the Telegram user already has a valid session, issue a new bearer
    token for it (old token stops working, expiry unchanged). Otherwise 401 -> login form."""
    try:
        ctx = await teacher_sessions.require_teacher(session, user)
    except TeacherSessionRequired as exc:
        return _error_response(_api_error(exc))
    token = await teacher_sessions.rotate_token(session, ctx)
    await session.commit()
    return _ok(_session_payload(ctx, token))


@router.post("/api/logout")
async def logout(
    ta: Annotated[TeacherAuth, Depends(teacher_auth)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    await teacher_sessions.logout(session, ta.user)
    await session.commit()
    await _send_bot(
        ta.user.telegram_id,
        "🔒 O‘qituvchi sessiyasi yakunlandi.",
        menus.teacher_menu(logged_in=False),
    )
    return _ok({"ok": True})


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=auth.PASSWORD_MAX_LENGTH)
    new_password: str = Field(max_length=auth.PASSWORD_MAX_LENGTH)


@router.post("/api/password")
async def change_password(
    body: PasswordIn,
    ta: Annotated[TeacherAuth, Depends(teacher_auth)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    try:
        result = await auth.change_password(
            session, ta.ctx, ta.user, current_password=body.current_password, new_password=body.new_password
        )
    except ServiceError as exc:
        return _error_response(_api_error(exc))
    return _ok(_session_payload(result.context, result.token))


@router.get("/api/me")
async def me(
    ta: Annotated[TeacherAuth, Depends(teacher_auth)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    course_ids = await teacher_sessions.teacher_course_ids(session, ta.ctx.teacher.id)
    courses = []
    if course_ids:
        rows = await session.execute(
            select(Course.id, Course.title).where(Course.id.in_(course_ids)).order_by(Course.title)
        )
        courses = [{"id": cid, "title": title} for cid, title in rows.all()]
    return _ok(
        {
            "teacher": {"full_name": ta.ctx.teacher.full_name, "login": ta.ctx.teacher.login},
            "expires_at": ta.ctx.session.expires_at.isoformat(),
            "expires_at_local": fmt_dt(ta.ctx.session.expires_at),
            "courses": courses,
            "unread_threads": await chat.unread_threads_count(session, ta.ctx),
        }
    )


# --------------------------------------------------------------------------- chat endpoints


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _message_out(m: TeacherChatMessage) -> dict:
    return {
        "id": m.id,
        "from": "teacher" if m.sender_role == ChatSenderRole.TEACHER else "student",
        "body": m.body,
        "created_at": _iso(m.created_at),
        "created_at_local": fmt_dt(m.created_at),
        "read": m.read_at is not None,
    }


@router.get("/api/threads")
async def threads(
    ta: Annotated[TeacherAuth, Depends(teacher_auth)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    items = await chat.teacher_threads(session, ta.ctx)
    return _ok(
        {
            "threads": [
                {
                    "id": t.id,
                    "student_name": t.student_name,
                    "student_username": t.student_username,
                    "course_title": t.course_title,
                    "last_message_at": _iso(t.last_message_at),
                    "last_message_at_local": fmt_dt(t.last_message_at),
                    "unread": t.unread,
                }
                for t in items
            ]
        }
    )


@router.get("/api/threads/{thread_id}/messages")
async def messages(
    thread_id: int,
    ta: Annotated[TeacherAuth, Depends(teacher_auth)],
    session: Annotated[AsyncSession, Depends(get_db)],
    after_id: int = 0,
    before_id: int | None = None,
) -> JSONResponse:
    try:
        thread = await chat.teacher_thread(session, ta.ctx, thread_id)
    except ServiceError as exc:
        return _error_response(_api_error(exc))
    items = await chat.thread_messages(session, ta.ctx, thread, after_id=after_id, before_id=before_id)
    student = await session.get(User, thread.student_user_id)
    course_title = await session.scalar(select(Course.title).where(Course.id == thread.course_id))
    return _ok(
        {
            "thread": {
                "id": thread.id,
                "student_name": student.full_name if student else "—",
                "student_username": student.username if student else None,
                "course_title": course_title,
                "is_closed": thread.is_closed,
            },
            "messages": [_message_out(m) for m in items],
        }
    )


class MessageIn(BaseModel):
    body: str = Field(max_length=chat.MAX_MESSAGE_LENGTH * 2)


@router.post("/api/threads/{thread_id}/messages")
async def send_message(
    thread_id: int,
    body: MessageIn,
    ta: Annotated[TeacherAuth, Depends(teacher_auth)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    try:
        thread = await chat.teacher_thread(session, ta.ctx, thread_id)
        msg, student = await chat.teacher_send(session, ta.ctx, ta.user, thread, body.body)
    except ServiceError as exc:
        return _error_response(_api_error(exc))
    course_title = await session.scalar(select(Course.title).where(Course.id == thread.course_id))
    teacher_name = ta.ctx.teacher.full_name
    await session.commit()
    delivered = False
    if student is not None:
        async with _bot() as bot:
            delivered = await chat.deliver_to_student(
                bot,
                student,
                teacher_name=teacher_name,
                course_title=course_title,
                thread_id=thread.id,
                body=msg.body,
            )
    return _ok({"message": _message_out(msg), "delivered": delivered})
