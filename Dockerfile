FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.6 /uv /bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

# SQLite lives on a mounted volume. On Railway: attach a volume at /data.
ENV PATH="/app/.venv/bin:$PATH" \
    DATABASE_PATH=/data/outreach.db \
    PYTHONUNBUFFERED=1
EXPOSE 8000

# One process only: the background sender runs inside it and SQLite is single-writer.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
