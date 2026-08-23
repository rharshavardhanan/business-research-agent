# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv gives us a reproducible install straight from the committed lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so a code change does not invalidate the layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# SQLite and the workbooks live here. Mount a persistent volume at this path,
# or every lead is lost on redeploy — the store is the source of truth.
ENV DATA_DIR=/data
RUN mkdir -p /data

ENV PORT=8000
EXPOSE 8000

# Exec form wrapping a shell, so $PORT is still expanded but `exec` hands PID 1
# to uvicorn. Shell-form CMD leaves the shell as PID 1 and swallows SIGTERM, so a
# platform's rolling restart would kill the server mid-write — and this app is
# writing SQLite and .xlsx files that are the source of truth.
CMD ["sh", "-c", "exec uv run --frozen --no-dev uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
