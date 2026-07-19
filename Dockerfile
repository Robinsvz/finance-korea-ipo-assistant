FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 ipo && useradd --uid 10001 --gid ipo --create-home ipo

WORKDIR /app

COPY requirements.runtime.lock ./
RUN pip install --upgrade pip && pip install -r requirements.runtime.lock

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY configs ./configs

RUN mkdir -p /app/data/raw && chown -R ipo:ipo /app

USER ipo

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-m", "app.cli", "healthcheck"]

CMD ["python", "-m", "app.cli", "run-scheduler"]
