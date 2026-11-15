# syntax=docker/dockerfile:1
# ---- 前端构建阶段 ----
FROM node:22-alpine AS frontend

WORKDIR /src

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci --no-audit --no-fund

COPY frontend/ ./frontend/
COPY VERSION ./
RUN cd frontend && npm run build

# ---- Python 运行阶段 ----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=8787 \
    DATA_DIR=/data \
    TEMP_DIR=/tmp/upload \
    DB_PATH=/data/tgdrive.sqlite3 \
    TG_SESSION=/data/tgdrive.session \
    FRONTEND_DIST=/app/static/dist

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini ca-certificates tzdata ffmpeg amule-daemon amule-utils \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app/ ./app/
COPY --chown=app:app VERSION ./
COPY --chown=app:app scripts/ ./scripts/
COPY --from=frontend --chown=app:app /src/static/dist/ ./static/dist/

RUN mkdir -p /data /tmp/upload && chown -R app:app /data /tmp/upload

VOLUME ["/data"]
USER app
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/api/health/live || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers", "--forwarded-allow-ips", "*"]
