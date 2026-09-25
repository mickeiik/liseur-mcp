FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache

# Nothing here writes at runtime (PYTHONDONTWRITEBYTECODE above), so the
# process does not need to own the tree it reads.
RUN useradd --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["liseur-mcp"]
