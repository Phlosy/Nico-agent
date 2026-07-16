FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system nico && useradd --system --gid nico --home-dir /app nico

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY logging.json ./logging.json
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir .

USER nico
EXPOSE 8000

CMD ["uvicorn", "nico_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "logging.json"]
