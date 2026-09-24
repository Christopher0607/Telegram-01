#!/bin/bash
# DigitalOcean 新服务器首次开机时自动执行（deploy/do_deploy.py 会把占位符填好后作为 user_data 传入）
# 日志：/var/log/tgst-setup.log
set -euo pipefail
exec > /var/log/tgst-setup.log 2>&1
export DEBIAN_FRONTEND=noninteractive
echo "== $(date '+%F %T') 开始初始化"

# 1GB 小机器：加 1G swap，防止构建镜像时内存不够
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update -y
apt-get install -y git curl ca-certificates cron
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh

APP=/opt/tg-signal-trader
echo "== 拉取代码"
for i in 1 2 3 4 5; do
  git clone -q -b '__BRANCH__' '__REPO_URL__' "$APP" && break
  echo "clone 失败，15 秒后重试（$i）"; sleep 15
done
cd "$APP"

echo "== 写入 .env"
cat > .env <<'ENVEOF'
__ENV__
ENVEOF
echo "SERVER_IP=$(curl -s http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || true)" >> .env
chmod 600 .env
mkdir -p data
git rev-parse --short HEAD > data/version.txt

echo "== 构建并启动"
docker compose up -d --build

echo "== 安装自动更新（每 5 分钟检查一次 GitHub）"
chmod +x deploy/update.sh
echo '*/5 * * * * root /opt/tg-signal-trader/deploy/update.sh >> /var/log/tgst-update.log 2>&1' > /etc/cron.d/tgst-update
chmod 644 /etc/cron.d/tgst-update
echo "== $(date '+%F %T') 初始化完成"
