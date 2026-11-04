#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo 'Run with sudo bash deploy/install-traffic.sh'; exit 1; }
command -v python3 >/dev/null || { echo 'Install python3 first'; exit 1; }
id tgdrive >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin tgdrive
install -d -o tgdrive -g tgdrive -m 755 "$APP_DIR/data/traffic"
sed "s|/opt/tgdrive|$APP_DIR|g" "$APP_DIR/deploy/tgdrive-traffic.service" > /etc/systemd/system/tgdrive-traffic.service
systemctl daemon-reload
systemctl enable --now tgdrive-traffic
echo 'Host collector enabled. Check: systemctl status tgdrive-traffic'
