# syntax=docker/dockerfile:1
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN uv sync --locked --no-dev

# The example config is the default; docker-compose mounts ./config.yaml over it.
COPY config.yaml ./
ENV RISING_CONFIG=/app/config.yaml
# Runs as root: on Linux, Docker creates the ./data bind mount as root, so another user could
# not write the database.
VOLUME ["/app/data"]
EXPOSE 8000
ENTRYPOINT ["rising"]
CMD ["--help"]
