FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY alembic ./alembic
COPY alembic.ini ./
COPY fixtures ./fixtures
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
# SHA коміту для паспорта прогону (у образі немає .git): docker compose build --build-arg GIT_SHA=$(git rev-parse HEAD)
ARG GIT_SHA=""
ENV FUZZHELM_GIT_SHA=$GIT_SHA
