FROM python:3.12-slim AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.6.0 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src/ ./src/
COPY strategies/ ./strategies/

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/strategies /app/strategies
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src
# Non-root user
RUN useradd -m -r msai && chown -R msai:msai /app
USER msai
EXPOSE 8000
CMD ["uvicorn", "msai.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
