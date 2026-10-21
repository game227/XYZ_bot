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
