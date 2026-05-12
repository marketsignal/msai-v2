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
# Non-root user, and pre-create the named-volume mountpoints with the
# right ownership.
#
# Why ``mkdir -p /app/data /app/reports`` BEFORE the recursive ``chown``:
# when Docker initializes a named volume at first mount, it copies the
# IMAGE's existing mountpoint (including its ownership) into the new
# volume. Without these directories present in the image, Docker creates
# the volume root as ``root:root``, and every write from the ``msai``
# (uid 999) container raises ``PermissionError``. This was the bug
# surfaced on 2026-05-12 when the first prod data-path exercise tried
# to write Parquet partitions to ``/app/data/parquet`` and crashed.
#
# Fresh-VM provisions get both directories writable from day one without
# operator intervention; the existing prod VM gets the heal via the
# ``deploy-on-vm.sh`` volume-heal sentinel step (also in this PR) since
# named-volume ownership inheritance is strictly first-mount only.
RUN useradd -m -r msai && \
    mkdir -p /app/data /app/reports && \
    chown -R msai:msai /app
USER msai
EXPOSE 8000
CMD ["uvicorn", "msai.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
