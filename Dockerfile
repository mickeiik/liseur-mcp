FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache

EXPOSE 8000
CMD ["liseur-mcp"]
