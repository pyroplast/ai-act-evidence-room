# app image: FastAPI + Jinja/htmx UI. No browser here; Chromium lives only in the worker.
FROM python:3.11-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /build
COPY pyproject.toml constraints.txt README.md LICENSE ./
COPY src ./src
RUN pip install -c constraints.txt .

FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH \
    ARTIFACT_STORE=/data/store
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY sql ./sql
RUN useradd --uid 10001 --create-home evidence \
 && mkdir -p /data/store && chown evidence:evidence /data/store
USER evidence
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=10 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
# Migrations run here, before the server starts; the worker waits for this container
# to be healthy (docs/decisions.md, D-003).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn evidence_room.app:app --host 0.0.0.0 --port 8000 --proxy-headers"]
