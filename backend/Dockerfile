# Build context is the REPO ROOT (not backend/) so the backend image can
# bake in user-supplied strategies from /strategies/ at the repo root.
# CI build command:
#   docker build -f backend/Dockerfile -t $REGISTRY/msai-backend:$SHA .
#
# Latent bug fixed 2026-05-09: the original Dockerfile had `COPY
# strategies/ ./strategies/` with build context `./backend`, which would
# have failed because strategies/ lives at the repo root. The bug never
# surfaced because dev uses Dockerfile.dev (no strategies COPY — they
# mount as a volume).
FROM python:3.12-slim AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.6.0 /uv /usr/local/bin/uv
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY backend/src/ ./src/
COPY strategies/ ./strategies/
COPY backend/alembic/ ./alembic/
COPY backend/alembic.ini ./

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/strategies /app/strategies
COPY --from=builder /app/alembic /app/alembic
COPY --from=builder /app/alembic.ini /app/alembic.ini
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src
# Non-root user
RUN useradd -m -r msai && chown -R msai:msai /app
USER msai
EXPOSE 8000
CMD ["uvicorn", "msai.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
