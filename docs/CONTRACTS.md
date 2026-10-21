# Team contract — XYZ Telegram bot

Read with `docs/TZ.md` (spec) and `docs/PLAN.md` (plan). Where this file and PLAN.md differ, **this file wins**.
Project root: `/home/neo/Desktop/TG_xYz/xyz-telegram-bot`. Python venv: `.venv` (Python 3.14, deps installed).

## 1. Roles and sessions

Roles are fixed; session names change when terminals restart. If you are a new session, find your
name in this table (or ask CORE).

| Role | Session (current) | Scope |
|---|---|---|
| **CORE** | `xyz-telegram-bot-setup` | Foundation, models + migrations, shared infra, `/start` `/menu` `/profile` `/help`, admin: users / roles / admin permissions / bot settings / audit log, integration, **all git commits**, Dockerfile / deploy / backup docs |
| **AUTH** | `tg-xyz-12` | Teacher accounts, login (Mini App), strict 24h sessions, `/logout`, admin: teachers + teacher sessions, teacher cabinet (my courses, students, results), teacher↔student chat (Mini App), security tests |
| **CONTENT** | `tg-xyz-a4` | Subject→Course→Module→Topic→Lesson CRUD (admin + assigned teachers), lesson materials, **2-free-lesson limit**, student catalog / lesson / video-lessons screens, quizzes (engine + management), results |
| **COMMERCE** | `tg-xyz-b5` | Plans, payment requests + approval, subscriptions, `/subscription`, media storage (S3/MinIO) + ffmpeg HLS + protected player + uploads, arq worker, notifications (auto + manual + student inbox), live sessions, statistics, backup scripts |

## 2. Working rules

- Edit **only files your role owns** (§3). For a change in someone else's file, message its owner.
- Models, migrations, `pyproject.toml`, config and other shared files are CORE's. To get a model or config
  change, message CORE with the exact spec (table, column, type, nullability, index); CORE adds it and
  regenerates the migration.
- Don't run `pip install` (shared venv) — ask CORE to add the dependency. Don't run git commands that
  change state (CORE commits). Don't edit `.env`.
- Lint/format only your files: `.venv/bin/ruff check <paths>` and `.venv/bin/ruff format <paths>` (line length 110).
- Tests: `.venv/bin/pytest -q tests/unit` works without a DB. Integration tests need `TEST_DATABASE_URL`
  (Docker is being installed by the user); write them now, they skip until the DB exists.
- When you finish a milestone, message CORE: files added, routers/endpoints, required model/config/deps
  changes, open questions. Keep messages short; put details in files.

## 3. File ownership

| Role | Owns |
|---|---|
| CORE | `app/config.py`, `app/bot.py`, `app/main.py`, `app/database/**`, `migrations/**`, `app/middlewares/**`, `app/handlers/__init__.py`, `app/handlers/common/**`, `app/handlers/admin/{users,settings,audit}.py`, `app/keyboards/menus.py`, `app/services/{access,audit,bot_settings,errors}.py`, `app/utils/**`, `app/web/{app,deps,telegram_auth}.py`, `tests/conftest.py`, `pyproject.toml`, `docker-compose.yml`, `Dockerfile`, `alembic.ini`, `.env.example`, `README.md`, `docs/**` |
| AUTH | `app/services/{auth,teacher_sessions,chat}.py`, `app/filters/teacher.py`, `app/handlers/teacher/**` except `materials.py` and `quizzes.py`, `app/handlers/admin/{teachers,teacher_sessions}.py`, `app/handlers/student/teacher_chat.py`, `app/web/routers/teacher.py`, `app/web/static/teacher/**`, `app/keyboards/auth*.py`, tests named `*auth*`, `*teacher*`, `*chat*` |
| CONTENT | `app/services/{lessons,quizzes}.py`, `app/handlers/student/{catalog,lessons,video_lessons,quizzes,results}.py`, `app/handlers/admin/{content,lessons,quizzes}.py`, `app/handlers/teacher/{materials,quizzes}.py`, `app/keyboards/content*.py`, tests named `*lesson*`, `*content*`, `*quiz*` |
| COMMERCE | `app/services/{subscriptions,media,storage,notifications,live,stats}.py`, `app/workers/**`, `app/handlers/student/{subscription,live,notifications}.py`, `app/handlers/admin/{plans,payments,notifications,live,stats}.py`, `app/web/routers/media.py`, `app/web/static/player/**`, `app/web/static/upload/**`, `app/keyboards/commerce*.py`, `scripts/backup*`, tests named `*subscription*`, `*payment*`, `*media*`, `*notification*`, `*live*`, `*stats*` |

New files: use the names above or prefix with your area. If unsure, ask CORE.

## 4. Menu buttons → owner

Constants live in `app/keyboards/menus.py` (CORE). Match them with `F.text == menus.BTN_X`.

- Student: `BTN_COURSES`, `BTN_FREE_LESSONS`, `BTN_QUIZZES`, `BTN_VIDEO_LESSONS`, `BTN_MY_RESULTS` → CONTENT ·
  `BTN_SUBSCRIBE`, `BTN_LIVE`, `BTN_NOTIFICATIONS`, `/subscription` → COMMERCE ·
  `BTN_PROFILE`, `BTN_HELP`, `/start`, `/menu`, `/profile`, `/help` → CORE ·
  `BTN_TEACHER_ENTRY`, `BTN_TEACHER_MENU`, `/logout` → AUTH
- Teacher: `BTN_T_LOGIN`, `BTN_T_MY_COURSES`, `BTN_T_CHATS`, `BTN_T_STUDENTS`, `BTN_T_RESULTS`, `BTN_T_LOGOUT` → AUTH ·
  `BTN_T_MATERIALS`, `BTN_T_QUIZZES` → CONTENT
- Admin: `BTN_A_USERS`, `BTN_A_AUDIT`, `BTN_A_SETTINGS` → CORE · `BTN_A_TEACHERS`, `BTN_A_TEACHER_SESSIONS` → AUTH ·
  `BTN_A_CONTENT`, `BTN_A_LESSONS_MEDIA`, `BTN_A_QUIZZES` → CONTENT ·
  `BTN_A_PLANS`, `BTN_A_PAYMENT_REQUESTS`, `BTN_A_LIVE`, `BTN_A_NOTIFICATIONS`, `BTN_A_STATS` → COMMERCE

## 5. Code conventions

- **Routers**: each handler module defines a module-level `router = Router(name="<area>.<module>")`;
  `app/handlers/__init__.py` auto-includes every module under `app/handlers/**`. **No catch-all handlers**
  (bare `@router.message()` / `@router.callback_query()`) — only `common/fallback.py` has them.
- **Web**: each module in `app/web/routers/` defines `router = APIRouter(prefix=...)`, auto-included.
  Static files under `app/web/static/<area>/` (served at `/static/<area>/...`).
- **Handler kwargs** (from middlewares): `session: AsyncSession`, `user: User` (already registered,
  non-blocked), `state: FSMContext`, `bot: Bot`.
- **Transactions**: the middleware commits after the handler and rolls back if it raises. To persist
  something and still fail (e.g. a failed-login audit row), `await session.commit()` before raising.
  Never hold row locks across Telegram API calls — commit first.
- **Errors**: raise `app.services.errors.*` (`AccessDenied`, `NotFound`, `InvalidInput`, `Conflict`,
  `TeacherSessionRequired`, `SubscriptionRequired`) with an Uzbek `user_message`; the global handler
  replies with it. Any other exception → generic message + log (no technical details to users).
- **Callback data**: aiogram `CallbackData` classes; prefix starts with your area code — `co_` CORE,
  `au_` AUTH, `ct_` CONTENT, `cm_` COMMERCE (e.g. `prefix="ct_les"`). Packed data must be ≤ 64 bytes.
- **FSM**: `StatesGroup` names prefixed with the area (e.g. `CtLessonEdit`). Storage is Redis.
  Any reply-menu button (`menus.MENU_BUTTONS`) or any `/command` clears the current FSM state before
  filters run (`app/middlewares/menu_reset.py`), so free-text state handlers never receive menu texts.
  Don't keep anything important only in FSM — e.g. quiz progress lives in `quiz_attempts`.
- **Teacher role**: when AUTH binds a teacher account to a Telegram user it sets `users.role = teacher`
  (admins keep `admin`); unbinding/blocking sets it back to `student`. CONTENT's checks rely on this.
- **Permissions**: admin screens use `AdminFilter(AdminPermission.X)` on the entry handler **and**
  `require_admin(user, perm)` inside every callback/state handler that reads or mutates data.
  Teacher screens call AUTH's `require_teacher(...)` on **every** handler (never cache the session in FSM).
- **Texts**: Uzbek Latin with `‘` / `’` (o‘, g‘, ta’lim). Escape every user-supplied value with
  `app.utils.text.esc()` (parse mode is HTML).
- **Time**: `app.utils.time.utcnow()` (aware UTC) for logic; `fmt_dt()` / `fmt_date()` for display (Asia/Tashkent).
- **Money**: `Decimal`. **Logging**: `logging.getLogger(__name__)`; never log passwords, tokens, initData.
- **Audit** (TZ §16): `await audit.log_action(session, "area.event", actor=user, entity_type=..., entity_id=..., details={...})`
  for every important change. Action names are dotted: `teacher.created`, `teacher.login_failed`,
  `teacher_session.revoked`, `plan.updated`, `payment_request.approved`, `lesson.updated`, `quiz.updated`, ...

## 6. Shared APIs

### Provided by CORE (exists now)
- `app.config.get_settings()` — see `app/config.py` for all fields (media, broadcast, live reminder settings included).
- `app.database.session`: `get_sessionmaker()`, `session_scope()` (for web/worker/scripts code).
- `app.services.access`: `is_superadmin(user)`, `is_admin(user)`, `has_permission(user, perm)`,
  `require_admin(user, perm=None)`, `await admin_telegram_ids(session, perm=None)`, filters `AdminFilter(perm)`, `RoleFilter(*roles)`.
  Permissions (`AdminPermission`): `users`, `admins`, `teachers`, `teacher_sessions`, `content`, `quizzes`,
  `plans`, `payments`, `notifications`, `live`, `stats`, `audit`, `settings`. Superadmins (`SUPERADMIN_IDS`) have all.
- `app.services.audit.log_action(...)` (above). `app.services.bot_settings.get_setting(session, key)` /
  `set_setting(...)`; keys `PAYMENT_INSTRUCTIONS`, `SUPPORT_CONTACT`, `HELP_TEXT`.
- `app.keyboards.menus` (buttons + `student_menu()`, `teacher_menu(logged_in=)`, `admin_menu()`),
  `app.handlers.common.start.main_menu_for(user)`.
- Web: `app.web.deps.get_db` (session dependency), `app.web.telegram_auth.verify_init_data(init_data)`,
  dependencies `webapp_init_data` (header `X-Telegram-Init-Data`) and `webapp_user` (→ `User`).
- Tests (`tests/conftest.py`): `session` (rolled back after each test), `committing_sessionmaker`
  (real commits, tables truncated after — for concurrency tests), `make_user(role=..., **fields)`.

### Provided by AUTH
- `app.services.teacher_sessions`:
  - `await get_active_session(session, telegram_id) -> TeacherSession | None` (valid iff not revoked and `now < expires_at`; never extends).
  - `await require_teacher(session, user) -> TeacherContext` — raises `TeacherSessionRequired`. `TeacherContext` has `.teacher: TeacherAccount`, `.session: TeacherSession`.
  - `await teacher_course_ids(session, teacher_id, right=None) -> set[int]` — assigned course ids; `right` is a
    `TeacherCourseAssignment` flag name (`can_manage_content`, `can_manage_quizzes`, `can_view_students`, `can_view_results`, `can_chat`).
- `app.services.chat.write_to_teacher_button(course_id) -> InlineKeyboardButton` — student entry to the chat (CONTENT may place it on course screens).

### Provided by CONTENT
- `app.services.lessons`:
  - `await can_view_lesson(session, user, lesson) -> bool` — no side effects. True for admins (CONTENT perm), teachers with an
    active session assigned to the course, active subscribers, and users who already opened this (free) lesson.
  - `await open_lesson(session, user, lesson) -> LessonOpenResult` — records the open, consumes a free slot when needed (row lock), raises `SubscriptionRequired` when the limit is used up.
  - `await free_lessons_left(session, user) -> int`
  - `await attach_media(session, lesson_id, media_id, *, actor) -> LessonMedia`
- `app.services.quizzes`: result queries for AUTH's teacher "Natijalar" screen (`results_for_course(...)`, `results_for_user(...)`).

### Provided by COMMERCE
- `app.services.subscriptions`: `await get_active_subscription(session, user_id)`, `await has_active_subscription(session, user_id)` (exist now; keep signatures).
- `app.services.media`:
  - `player_button(media, *, lesson_id, text="▶️ Videoni ko‘rish") -> InlineKeyboardButton` (WebApp button to the protected player).
  - `upload_button(*, lesson_id=None) -> InlineKeyboardButton` (WebApp upload page for admins/teachers; large files, resumable, checksum).
  - `await store_telegram_file(bot, session, *, file_id, kind, filename, mime_type, size, actor) -> MediaFile` (small non-video files sent to the bot).
  - `await send_protected_document(bot, chat_id, media)` (non-video materials, `protect_content=True`).
- `app.services.notifications`: `await notify_user(session, user_id, kind, body, *, dedupe_key=None)`,
  `await notify_new_lesson(session, lesson_id)`, `await enqueue_broadcast(notification_id)`.
- `app.workers.queue.get_arq_pool()`; worker entry `arq app.workers.main.WorkerSettings`.

## 7. Decisions already made (do not re-open without CORE)

**Free lessons (CONTENT)**
- Every open writes or updates a `lesson_accesses` row and upserts `course_enrollments`.
- `consumed_free_slot = true` only when a non-subscriber opens a free lesson through the allowance. The limit counts only these rows.
- Under concurrency: first `SELECT ... FROM users WHERE id=:id FOR UPDATE`, then count, then insert, all in one transaction.
- Reopening a lesson never consumes a slot again.
- When the subscription expires, results remain but paid lessons close.

**Quizzes (CONTENT)**
- Access follows the lesson (`quizzes.access_mode`: `inherit` / `free` / `paid`) and is re-checked on every answer.
- If access is lost mid-quiz, the attempt becomes `interrupted` and the answers given so far are scored and saved.
- One attempt `in_progress` per user and quiz (partial unique index). `attempt_no` is counted per user and quiz.
- Correct answers are never sent before the quiz ends. `result_visibility` controls what is shown after.
- For TEXT questions, the accepted answers are the `answer_options` rows with `is_correct=true`.

**Payments (COMMERCE)**
- Payment proof is sent inside the bot: `proof_kind` (`photo` / `document` / `text`), `proof_file_id`, `user_comment`.
- At most one pending request per user (partial unique index).
- Approval: `UPDATE ... SET status='approved' WHERE id=:id AND status='pending' RETURNING` creates exactly one subscription; `subscriptions.payment_request_id` is UNIQUE.
- Renewals stack: `starts_at = max(now, current ends_at)`.
- Admins with the `payments` permission are notified of each new request (`admin_telegram_ids`).
- Payment instructions come from `bot_settings.payment_instructions`.

**Teacher auth (AUTH)**
- The password is entered only in the Mini App login form (TZ §7.6). After login, the Mini App holds the session token in memory and sends it as `Authorization: Bearer`. The server checks:
  - initData HMAC;
  - token hash match;
  - not revoked and not expired;
  - `session.telegram_id` equals the initData user;
  - the teacher account is not blocked.
- Bot handlers find the active session by `telegram_id` on every call.
- `teacher_accounts.user_id` is bound on the first login; a login from any other Telegram account is rejected (an admin can reset the binding).
- One active session per teacher: a new login ends older sessions with `replaced`. Expiry is `created_at + 24h` and is never extended.
- Rate limit: 5 failed attempts in 15 minutes (per login and per telegram_id) locks login for 15 minutes; this lives in Redis.
- Every success and failure goes to the audit log without the password or token.
- Chat UI lives in the Mini App. The bot only sends a text-free "yangi xabar bor" notice, so nothing is left in the teacher's Telegram history after the session ends.

**Media (COMMERCE)**
- Storage: private S3 bucket.
- Video: ffmpeg → HLS with AES-128; the key is served only by an authenticated endpoint.
- Player (Mini App) opening:
  1. Verify initData.
  2. Check lesson access via `can_view_lesson`. Teachers additionally need an active session.
  3. Issue a 10-minute token, which the player refreshes.
- Segments are proxied through FastAPI (dev) or served via 60-second presigned redirects (prod).
- A watermark shows the user's ID.
- Protected video is never sent as a Telegram file.

**Notifications (COMMERCE)**
- `dedupe_key` makes automatic notifications idempotent.
- 3-day and 1-day reminders re-check the subscription right before sending, so stale reminders are not sent.
- One `notification_deliveries` row per recipient. Send rate ≤ `broadcast_rate_per_second`; `RetryAfter` is honoured.
- `TelegramForbiddenError` sets `users.bot_blocked_at` and marks the delivery `blocked`.
- Users with `user_settings.notifications_enabled = false` are skipped for non-critical manual broadcasts.

**Live sessions (COMMERCE)**
- `link_visibility` is `public`, `subscribers` or `registered`.
- Reminders are sent at `live_reminder_minutes` before start.

## 8. Model naming notes

Models are in `app/database/models/` — read them before writing queries. Names that differ from what you might expect:
- `position` (not `sort_order`); `lesson_media.media_id`; `lessons.course_id` (denormalized, keep in sync with the topic's course).
- `subscriptions.ends_at` (not `expires_at`); `payment_requests.reviewed_by_user_id`, `decision_comment`.
- `teacher_chat_messages.body`; `notifications.body`, `audience_type`, `audience` (JSONB), `kind` (free string owned by COMMERCE).
- `live_sessions.requirements`, `link_visibility`.
- `media_files.hls_prefix`, `hls_aes_key`, `checksum_sha256`.
- `users.permissions` (array of `AdminPermission` values), `users.bot_blocked_at`, `users.last_seen_at`.
