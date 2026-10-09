FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend

WORKDIR /build
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

ENV PATH="/app/.venv/bin:$PATH"

COPY app/ ./app/
COPY scripts/init_config.py ./scripts/init_config.py
COPY main.py config.example.yaml ./
COPY --from=frontend /build/dist ./frontend/dist

RUN mkdir -p /app/data /app/config/data

EXPOSE 8000

CMD ["python", "-m", "app.server", "--host", "0.0.0.0", "--port", "8000"]
