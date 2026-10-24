# XYZ Telegram Education Bot

To‘liq Telegram ichida ishlaydigan mustaqil ta’lim boti: kurslar, bepul (2 ta) va pullik darslar,
himoyalangan video va hujjatlar, interaktiv testlar, obunalar, o‘qituvchi kabineti (24 soatlik sessiya),
o‘qituvchi–o‘quvchi chati, bildirishnomalar, jonli darslar va admin panel. Veb sayt yoki Mini App yo‘q.

Spec: [`docs/TZ.md`](docs/TZ.md) · Ishga tushirish: [`docs/DEPLOY.md`](docs/DEPLOY.md) ·
Zaxira: [`docs/BACKUP.md`](docs/BACKUP.md) · Yuklama testi: [`docs/LOAD_TEST.md`](docs/LOAD_TEST.md) ·
Jamoa kelishuvi: [`docs/CONTRACTS.md`](docs/CONTRACTS.md)

## Stack

Python 3.12+, aiogram 3, SQLAlchemy 2 (async) + asyncpg, Alembic, PostgreSQL, Redis (FSM, rate limit,
arq navbati). Dars fayllari Telegram serverlarida (`file_id`), `protect_content=True` bilan yuboriladi.

| Xizmat | Buyruq | Vazifa |
|---|---|---|
| bot | `python -m app.main` | Telegram long polling |
| worker | `arq app.workers.main.WorkerSettings` | eslatmalar, ommaviy xabarlar |

## TZ'dan kelishilgan chetlanishlar

Buyurtmachi qarori (21.10.2026, 24.10.2026 da tasdiqlangan): **faqat bot**, veb sayt, Mini App va domen yo‘q.

| TZ bandi | TZ talabi | Amalda |
|---|---|---|
| §9.1, §9.3, §22.20–21 | Video himoyalangan HLS player orqali, vaqtinchalik token va watermark bilan | Video Telegram fayli sifatida `protect_content=True` bilan yuboriladi: Telegram ilovalarida forward va saqlash bloklanadi, lekin norasmiy klientlar yoki ekran yozuvidan himoya yo‘q |
| §2, §9.2 | S3-compatible storage, checksum, resumable upload | Fayllar Telegram serverlarida, bazada faqat `file_id` va metama’lumot |
| §19 | Media zaxirasi | Asl fayllarni alohida (masalan, bulutli diskda) saqlash buyurtmachi zimmasida: Telegram faylni o‘chirsa, uni qayta yuklash kerak |
| §7.5 | Yozishmalar bot ichidagi tizimda | Yozishmalar botda; sessiya tugagach bot o‘qituvchiga ko‘rsatgan xabarlarni chatdan o‘chiradi (`services/teacher_trail.py`) |

Himoyalangan player kerak bo‘lsa, olib tashlangan veb qism git tarixida bor (`ce664fa` dan oldingi commit).

## Ishga tushirish (Docker)

```bash
cp .env.example .env    # BOT_TOKEN, SUPERADMIN_IDS
docker compose --profile app up -d --build
```

## Ishlab chiqish

```bash
docker compose up -d postgres redis
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/python -m app.main
.venv/bin/arq app.workers.main.WorkerSettings
```

## Testlar

```bash
.venv/bin/pytest -q tests/unit
docker compose exec postgres createdb -U xyz xyz_test
TEST_DATABASE_URL=postgresql+asyncpg://xyz:xyz@localhost:5432/xyz_test .venv/bin/pytest -q --ignore=tests/load
```
