# Same Python minor version as development and CI (TZ: Python 3.12+).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv

# postgresql-client: pg_dump / pg_restore for scripts/backup*.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /srv

# The app runs from /srv via PYTHONPATH, so every entry point (python -m, arq) uses the same code.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install ".[monitoring]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

USER app
CMD ["python", "-m", "app.main"]
