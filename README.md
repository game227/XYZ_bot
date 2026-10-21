# XYZ Telegram Education Bot

Mustaqil Telegram ta’lim boti: kurslar, bepul/pullik darslar, interaktiv testlar, obunalar,
himoyalangan video player, o‘qituvchi kabineti (24 soatlik sessiya) va admin panel.

Spec: [`docs/TZ.md`](docs/TZ.md) · Plan: [`docs/PLAN.md`](docs/PLAN.md) · Team contract: [`docs/CONTRACTS.md`](docs/CONTRACTS.md)

## Stack

Python 3.12+, aiogram 3, SQLAlchemy 2 (async) + asyncpg, Alembic, PostgreSQL, Redis (FSM, rate limits, arq queue),
FastAPI (Mini App + video player), S3-compatible storage (MinIO in dev), ffmpeg (HLS).

## Services

| Service | Command | Purpose |
|---|---|---|
| bot | `python -m app.main` | Telegram long polling |
| web | `uvicorn app.web.app:app --port 8080` | Mini App pages, protected video player |
| worker | `arq app.workers.main.WorkerSettings` | Reminders, broadcasts, video transcoding |

## Local development

```bash
# 1. Infra (PostgreSQL, Redis, MinIO)
docker compose up -d postgres redis minio

# 2. Python env
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 3. Config
cp .env.example .env   # set BOT_TOKEN, SUPERADMIN_IDS, SECRET_KEY, WEB_BASE_URL

# 4. Database
.venv/bin/alembic upgrade head

# 5. Run
.venv/bin/python -m app.main
.venv/bin/uvicorn app.web.app:app --port 8080
.venv/bin/arq app.workers.main.WorkerSettings
```

Telegram Mini App and player buttons require a public HTTPS URL. In development expose the web service with
`cloudflared tunnel --url http://localhost:8080` and put the URL into `WEB_BASE_URL`.

## Tests

```bash
.venv/bin/pytest -q tests/unit                     # no external services
docker compose exec postgres createdb -U xyz xyz_test
TEST_DATABASE_URL=postgresql+asyncpg://xyz:xyz@localhost:5432/xyz_test .venv/bin/pytest -q
```

## Production (VPS)

```bash
docker compose --profile app up -d --build   # postgres, redis, minio, migrate, bot, web, worker
```

Put a TLS reverse proxy (e.g. Caddy) in front of `web`. Never commit `.env`.
