#!/usr/bin/env bash
# TG Drive 一键部署（Ubuntu 22.04 / 24.04）
# 用法：
#   sudo bash deploy/bootstrap-ubuntu.sh --domain drive.example.com --email you@example.com \
#        --repo https://github.com/KubaZzZo/tgdrive.git
#   可选：--native（systemd 裸机，默认 docker）
set -euo pipefail

MODE="docker"; APP_DIR="/opt/tgdrive"; APP_USER="tgdrive"
DOMAIN=""; ACME_EMAIL=""; REPO_URL=""
log(){ printf '\033[1;32m[bootstrap]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker) MODE="docker"; shift ;;
    --native) MODE="native"; shift ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) ACME_EMAIL="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --dir) APP_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die "未知参数 $1" ;;
  esac
done
[[ $EUID -eq 0 ]] || die "请用 root 或 sudo 运行"

export DEBIAN_FRONTEND=noninteractive
log "安装基础依赖…"
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl git gnupg ufw fail2ban jq sqlite3
timedatectl set-timezone UTC || warn "时区设置失败"

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

mkdir -p "$APP_DIR"
if [[ -n "$REPO_URL" ]]; then
  if [[ -d "$APP_DIR/.git" ]]; then git -C "$APP_DIR" pull --ff-only || warn "git pull 失败"; else git clone "$REPO_URL" "$APP_DIR"; fi
else
  warn "未提供 --repo，请自行把代码放到 $APP_DIR"
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/tmp"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data" "$APP_DIR/tmp" 2>/dev/null || true

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "生成 .env（随机密钥）"
  COOKIE_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
  APP_PASSWORD="$(openssl rand -base64 18 | tr -d '\n')"
  cat > "$APP_DIR/.env" <<EOF
# ===== Telegram（务必用自己申请的 api_id/api_hash）=====
TG_MODE=user
TG_API_ID=
TG_API_HASH=
TG_CHAT_ID=
TG_SESSION=$APP_DIR/data/tgdrive.session
# 美国 VPS 一般不需要代理，留空
TG_PROXY=

# ===== 应用 =====
APP_NAME=TG Drive
APP_ENV=production
APP_PASSWORD=$APP_PASSWORD
APP_SECRET=$COOKIE_SECRET
COOKIE_SECURE=true
SESSION_MAX_AGE=1209600
LOGIN_MAX_ATTEMPTS=5
LOGIN_WINDOW_SECONDS=300
HOST=0.0.0.0
PORT=8787
LOG_LEVEL=INFO
LOG_JSON=true
DATA_DIR=$APP_DIR/data
TEMP_DIR=$APP_DIR/tmp
DB_PATH=$APP_DIR/data/tgdrive.sqlite3
FRONTEND_DIST=$APP_DIR/static/dist

# ===== 分片 =====
TG_SEGMENT_SIZE=1800000000
UPLOAD_CHUNK_SIZE=8388608
MAX_FILE_SIZE=0

# ===== Caddy =====
DOMAIN=${DOMAIN:-drive.example.com}
ACME_EMAIL=${ACME_EMAIL:-admin@example.com}
EOF
  chmod 600 "$APP_DIR/.env"; chown "$APP_USER:$APP_USER" "$APP_DIR/.env" || true
  warn "已生成 $APP_DIR/.env —— 记得填 TG_API_ID / TG_API_HASH / TG_CHAT_ID"
else
  log ".env 已存在，跳过"
fi

if [[ "$MODE" == "docker" ]]; then
  command -v docker >/dev/null 2>&1 || { log "安装 Docker…"; curl -fsSL https://get.docker.com | sh; }
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件"
  ( cd "$APP_DIR" && docker compose up -d --build )
else
  command -v python3.12 >/dev/null 2>&1 || {
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa && apt-get update -y
    apt-get install -y python3.12 python3.12-venv python3.12-dev
  }
  [[ -d "$APP_DIR/.venv" ]] || python3.12 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

  NODE_MAJOR="$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1 || true)"
  if [[ -z "$NODE_MAJOR" || "$NODE_MAJOR" -lt 22 ]]; then
    log "安装 Node.js 22（用于构建 Vue 前端）…"
    apt-get install -y ca-certificates curl gnupg
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
  fi
  log "构建前端…"
  ( cd "$APP_DIR/frontend" && npm ci --no-audit --no-fund && npm run build )

  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  install -m 0644 "$APP_DIR/deploy/tgdrive.service" /etc/systemd/system/tgdrive.service
  systemctl daemon-reload && systemctl enable --now tgdrive
  command -v caddy >/dev/null 2>&1 || {
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y && apt-get install -y caddy
  }
  sed -e 's|app:8787|127.0.0.1:8787|g' "$APP_DIR/Caddyfile" > /etc/caddy/Caddyfile
  systemctl enable --now caddy
fi

log "配置防火墙（22/80/443）"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 80/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
ufw allow 443/udp >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || warn "UFW 启用失败"
systemctl enable --now fail2ban >/dev/null 2>&1 || true

cat <<EOF

==================== 下一步 ====================
1) 填配置：nano $APP_DIR/.env  （TG_API_ID / TG_API_HASH / TG_CHAT_ID）
2) 生成 session：
$( [[ "$MODE" == native ]] && echo "   cd $APP_DIR && sudo -u $APP_USER .venv/bin/python scripts/login_qr.py" || echo "   cd $APP_DIR && docker compose run --rm app python scripts/login_qr.py" )
3) 重启：$( [[ "$MODE" == native ]] && echo "sudo systemctl restart tgdrive" || echo "docker compose up -d --force-recreate app" )
4) 验收：curl -fsS https://\$DOMAIN/api/meta ; curl -sI -r 0-1023 https://\$DOMAIN/api/files/<id>/download
⚠️ 8000/8787 端口绝不暴露公网；data 目录 + .env 必须加密异地备份。
===============================================
EOF