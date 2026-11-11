# JARVIS API image — `docker compose up` builds and runs it next to Postgres.
#
# Serves the HTTP API only; the web app runs from web/ with `npm run dev`.
# The container migrates the schema (`jarvis init`) and then serves on
# 0.0.0.0:8000. Configuration comes from JARVIS_* environment variables;
# no .env file is copied into the image (see .dockerignore).

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    JARVIS_HOST=0.0.0.0 \
    JARVIS_PORT=8000

WORKDIR /app

# Dependencies first, so source edits don't invalidate this layer.
# The dev extra's path dependency lives under tests/, so its project file
# must be present for uv to read the lockfile.
COPY pyproject.toml uv.lock README.md ./
COPY tests/fixtures/strategies tests/fixtures/strategies
RUN uv sync --frozen --no-install-project

COPY src ./src
COPY alembic.ini ./
RUN uv sync --frozen

RUN useradd --system --uid 10001 jarvis && chown -R jarvis /app
USER jarvis

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

CMD ["sh", "-c", "jarvis init && exec jarvis serve"]
