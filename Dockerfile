# syntax=docker/dockerfile:1
# TG Drive 运行镜像：FastAPI + Telethon，非 root，自带健康检查
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    HOST=0.0.0.0 \
    PORT=8787 \
    DATA_DIR=/data \
    TEMP_DIR=/tmp/upload \
    DB_PATH=/data/tgdrive.sqlite3 \
    TG_SESSION=/data/tgdrive.session

# curl 用于 HEALTHCHECK；tini 做 PID 1 正确处理信号；ca-certificates 供 MTProto TLS
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app/ ./app/
COPY --chown=app:app static/ ./static/
COPY --chown=app:app scripts/ ./scripts/

# 必须先建目录并 chown，否则匿名卷会是 root 属主，非 root 用户写不进 SQLite
RUN mkdir -p /data /tmp/upload && chown -R app:app /data /tmp/upload

VOLUME ["/data"]
USER app
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/api/meta || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers", "--forwarded-allow-ips", "*"]