# XYZ Telegram Education Bot — 4 sessiya bo'yicha ish rejasi

## Kontekst
Bo'sh papka `/home/neo/Desktop/TG_xYz` ichida TZ v1.0 bo'yicha mustaqil Telegram ta'lim boti quriladi: aiogram 3, async SQLAlchemy 2, PostgreSQL, Alembic.
4 ta Claude sessiyasi parallel ishlaydi. **tg-xyz-d6** koordinator, qolganlari: **tg-xyz-ae**, **tg-xyz-e4**, **tg-xyz-cd**.

Foydalanuvchi qarorlari:
- Muhit Docker orqali: postgres, redis va minio `docker-compose` ichida ko'tariladi.
- Veb qism: FastAPI servisi. U video player sahifasini, HLS uchun imzolangan URL'larni va o'qituvchi uchun Telegram Mini App'ni (login va chat) beradi.
- Media: S3/MinIO. Video ffmpeg bilan HLS'ga o'giriladi, qisqa muddatli tokenlar va watermark ishlatiladi. DRM yo'q.
- Ko'lam: barcha bosqichlar parallel bajariladi. Yuklama testi, backup va VPS deploy oxirida qilinadi.

## Texnik qarorlar
- Loyiha ildizi: `xyz-telegram-bot/` (TZ §20 tuzilmasi va `web/` papkasi). `pyproject.toml` ishlatiladi; lokalda Python 3.14, venv `.venv`.
- Kutubxonalar: aiogram 3, sqlalchemy[asyncio], asyncpg, alembic, pydantic-settings, argon2-cffi, redis, arq (worker), fastapi, uvicorn, aioboto3, pytest, pytest-asyncio.
- Barcha vaqtlar UTC `timestamptz` da saqlanadi. Foydalanuvchiga Asia/Tashkent vaqtida ko'rsatiladi.
- Birinchi superadmin `.env` ichidagi `SUPERADMIN_IDS` orqali belgilanadi. Qolgan adminlarni bot ichidan u tayinlaydi.
- Ruxsatlar tekshiruvi:
  - `services/access.py` bitta markaziy joy: `can_open_lesson`, `require_active_subscription`, `require_teacher_session` (har so'rovda DB'dan o'qiladi, muddati uzaytirilmaydi), `require_admin(perm)`.
  - Har bir handler va web endpoint bular orqali o'tadi.
- Bepul dars limiti:
  - `lesson_accesses` jadvalida `UNIQUE(user_id, lesson_id)`.
  - Yangi dars ochilganda `SELECT ... FOR UPDATE` bilan `users` qatori qulflanadi, tranzaksiya ichida `count < 2` tekshiriladi.
- Ikki marta tasdiqlashdan himoya:
  - `UPDATE payment_requests SET status='approved' WHERE id=? AND status='pending' RETURNING`.
  - Qo'shimcha `subscriptions.payment_request_id UNIQUE`.
- To'lov dalili:
  - O'quvchi chekni (rasm yoki izoh) to'g'ridan-to'g'ri bot ichida yuboradi va u `payment_requests` ga biriktiriladi.
  - Admin uni "Obuna so'rovlari" bo'limida ko'radi, alohida shaxsiy yozishma kerak emas.
- O'qituvchi sessiyasi:
  - Token `secrets.token_urlsafe(32)`, DB'da faqat sha256 hash saqlanadi.
  - `expires_at = created_at + 24h` qat'iy, `revoked_at` alohida maydon.
  - Sessiya teacher va telegram_id'ga bog'lanadi.
- O'qituvchi chatlari (TZ §7.5):
  - Yozishmalar Telegram xabari sifatida yuborilmaydi, Mini App ichida ko'rsatiladi.
  - Bot o'qituvchiga faqat matnsiz "yangi xabar bor" bildirishnomasini yuboradi.
  - Shunda sessiya tugagach, yozishmalar o'qituvchining Telegram tarixida qolmaydi.
- HTTPS: Mini App va player tugmasi faqat HTTPS URL bilan ishlaydi. Dev'da `cloudflared` tunnel, prod'da domen va TLS (Caddy) ishlatiladi.

## Fayllar egaligi (konfliktsiz parallel ishlash uchun)
Qoidalar:
- Har sessiya faqat o'z fayllarini o'zgartiradi.
- Umumiy fayllarni (`database/models/*`, `migrations/`, `config.py`, `bot.py`, `main.py`, `web/app.py`, `pyproject.toml`, `docker-compose.yml`) faqat d6 o'zgartiradi. Boshqalar model yoki config o'zgarishini d6'ga xabar orqali so'raydi.
- `git init` qilinadi, **faqat d6 commit qiladi** (integratsiya nuqtalarida). Shunda git index'da to'qnashuv bo'lmaydi.

### tg-xyz-d6 (men): asos, integratsiya, admin yadrosi
1. 0-bosqich, eng birinchi:
   - Skelet, `config.py`, `docker-compose.yml` (postgres, redis, minio), `.env.example`, `.gitignore`.
   - **TZ §12 dagi barcha jadvallar uchun SQLAlchemy modellari**, Alembic init va birinchi migratsiya.
   - `database/session.py`, `bot.py`/`main.py`, router ro'yxatdan o'tkazish.
   - Middlewares: DB session, user upsert, throttling.
   - `web/app.py` (FastAPI skeleti), `web/telegram_auth.py` (Mini App `initData` HMAC tekshiruvi, umumiy).
   - `services/access.py` va `services/audit.py`.
2. `/start`, `/menu`, `/help`, `/profile`, rolga qarab menyu klaviaturalari (`keyboards/`).
3. Admin: foydalanuvchilar va rollar, admin vakolat darajalari, bot sozlamalari, audit jurnalini ko'rish.
4. Oxirida: routerlarni ulash, code review, umumiy integratsion testlar, Dockerfile, deploy va backup hujjati.

### tg-xyz-ae: o'qituvchi, sessiya va chat (xavfsizlik bloki)
- `services/auth.py`, `services/teacher_sessions.py`: Argon2id hash, login rate limit (Redis), vaqtinchalik parolni almashtirish, `/logout`, admin tomonidan bekor qilish.
- `web/routers/teacher.py` va Mini App sahifalari: login formasi, parolni almashtirish, chat UI. Har so'rovda sessiya tekshiriladi.
- Admin bo'limlari: o'qituvchi yaratish/bloklash, kursga biriktirish, faol sessiyalar ro'yxati va ularni bekor qilish.
- `handlers/teacher/*`: kabinet, mening kurslarim, o'quvchilar va natijalar (ruxsat doirasida).
- Chat servisi: `teacher_chat_threads` va `teacher_chat_messages`. O'quvchi tomonidan yozish bot orqali bo'ladi.
- `tests/security/`: sessiya 24 soatda tugashi, faollik muddatni uzaytirmasligi, bekor qilish, chat va o'quvchi ma'lumotlarining bloklanishi.

### tg-xyz-e4: ta'lim kontenti va testlar
- `services/lessons.py`: Fan → Kurs → Modul → Mavzu → Dars bo'yicha CRUD (admin va biriktirilgan o'qituvchi uchun), materiallar, bepul deb belgilash.
- **2 ta bepul dars limiti** (yuqoridagi qulflash orqali), qayta ochish limitni kamaytirmaydi.
- O'quvchi handlerlari: katalog, bepul darslar, dars sahifasi. "Videoni ko'rish" tugmasi cd'ning `media.build_player_url()` funksiyasidan foydalanadi.
- `services/quizzes.py`: savol turlari (bitta yoki ko'p to'g'ri javob, matnli), vaqt limiti, shuffle, urinishlar soni, FSM orqali test ishlash. To'g'ri javoblar test oxirigacha yuborilmaydi. Natijalar, "Mening natijalarim".
- Test yaratish va tahrirlash: admin va o'qituvchi uchun.
- Unit va integratsion testlar: limit, shu jumladan parallel so'rovlar testi; quiz scoring.

### tg-xyz-cd: obuna, media, worker, bildirishnomalar, jonli darslar
- `services/subscriptions.py`:
  - Tariflar CRUD.
  - O'quvchi oqimi: tarif tanlash, Telegram ID ko'rsatish, to'lov dalilini yuklash.
  - Admin "Obuna so'rovlari": tasdiqlash idempotent, rad etishda sabab.
  - `/subscription` holati; muddat tugaganda kirish yopiladi.
- `services/media.py` va `web/routers/media.py`:
  - S3/MinIO'ga yuklash: hajm, format va checksum tekshiruvi.
  - Worker'da ffmpeg orqali HLS'ga o'girish.
  - Player sahifasi (hls.js): qisqa muddatli imzolangan playlist/segment tokenlari, user ID watermark, eskirgan token 403 qaytaradi.
  - O'qituvchi uchun media kirishi uning sessiyasiga bog'lanadi.
- `workers/` (arq + Redis):
  - Muddatga 3 va 1 kun qolganda eslatmalar, eskirgan eslatma yuborilmaydi (yuborishdan oldin qayta tekshiriladi).
  - Tugash xabari.
  - Ommaviy xabarlar navbati: rate limit, `TelegramForbiddenError`/`RetryAfter` qayta ishlanadi, `notification_deliveries` qayd qilinadi.
- Admin bildirishnomalari: hammaga, rolga, kursga, tanlangan foydalanuvchilarga. O'quvchi uchun bildirishnomalar bo'limi.
- Jonli darslar: CRUD, ro'yxatdan o'tish, maxfiy havola faqat ruxsatli foydalanuvchiga ko'rinadi, yaqinlashganda eslatma.
- Statistika: indeksli agregat so'rovlar.

## Ish tartibi
1. Foydalanuvchi parallel ravishda Docker o'rnatadi:
   ```
   sudo pacman -S docker docker-compose && sudo systemctl enable --now docker && sudo usermod -aG docker $USER
   ```
   Keyin BotFather'dan test bot tokenini oladi.
2. d6 vazifalarni ae, e4 va cd'ga SendMessage orqali yuboradi. Xabarda yuqoridagi egalik bo'limi, texnik qarorlar va interfeyslar bo'ladi.
3. d6 0-bosqichni bajaradi (modellar va migratsiya), so'ng "foundation ready" deb xabar beradi. Shu orada boshqalar modellarga bog'liq bo'lmagan qismlarni yozadi:
   - ae: hash va token util, Mini App HTML.
   - e4: quiz scoring logikasi.
   - cd: S3 klient, ffmpeg HLS pipeline, worker skeleti.
4. Har sessiya modulini tugatgach testlarni o'tkazadi va d6'ga xabar beradi. d6 routerlarni ulaydi, umumiy test qiladi va commit qiladi.

## Tekshirish
- `docker compose up -d` → `alembic upgrade head` → `pytest` (unit, integration, security).
- Bot long polling'da ishga tushiriladi, web `cloudflared` tunnel orqali ochiladi. `.env` ichiga `BOT_TOKEN` va `SUPERADMIN_IDS` yoziladi.
- Qo'lda tekshiriladi, qabul mezonlari §22 (1–23):
  - 2 ta bepul dars limiti, qayta ochish limitni kamaytirmasligi.
  - O'qituvchi login; sessiya muddatini sun'iy o'tkazib, chat va ma'lumotlar yopilishi; admin sessiyani bekor qilishi; `/logout`.
  - To'lovni ikki marta tasdiqlashda ikkinchi obuna yaratilmasligi.
  - Video faqat player orqali ochilishi, eskirgan token 403 qaytarishi.
- Oxirida: yuklama testi (locust), backup va restore sinovi.
