# Same Python minor version as development and CI (TZ: Python 3.12+).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv

# ffmpeg: HLS transcoding in the worker. curl: container health checks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /srv

# Dependencies first (better layer caching); the app itself runs from /srv via PYTHONPATH,
# so static files and every entry point (python -m, uvicorn, arq) use the same code.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

USER app
CMD ["python", "-m", "app.main"]
