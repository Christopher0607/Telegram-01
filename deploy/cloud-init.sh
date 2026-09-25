#!/bin/bash
# DigitalOcean 新服务器首次开机时自动执行（deploy/do_deploy.py 会把占位符填好后作为 user_data 传入）
# 日志：/var/log/tgst-setup.log
set -euo pipefail
exec > /var/log/tgst-setup.log 2>&1
export DEBIAN_FRONTEND=noninteractive
echo "== $(date '+%F %T') 开始初始化"

# 联网的步骤偶尔会失败（新机器开机时系统自动更新占着 apt、网络抖动）：每 20 秒重试，最多 10 次
retry() {
  for i in 1 2 3 4 5 6 7 8 9 10; do
    "$@" && return 0
    echo "失败，20 秒后重试（$i）：$*"; sleep 20
  done
  return 1
}
# apt 被系统自动更新占用时排队等待（最多 10 分钟），而不是直接报错退出
echo 'DPkg::Lock::Timeout "600";' > /etc/apt/apt.conf.d/99tgst-lock-timeout

# 1GB 小机器：加 1G swap，防止构建镜像时内存不够
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

retry apt-get update -y
retry apt-get install -y git curl ca-certificates
if ! command -v docker >/dev/null 2>&1; then
  retry curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  retry sh /tmp/get-docker.sh
fi

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
if retry docker compose up -d --build; then
  git rev-parse HEAD > data/deployed.txt   # 记下成功启动的版本；没有这个文件 → update.sh 会自动重新构建
else
  echo "首次构建失败：下面安装的自动更新任务会每 5 分钟重试一次"
fi

echo "== 安装自动更新（每 5 分钟检查一次 GitHub）"
chmod +x deploy/update.sh
# 用 systemd 定时器，不用 cron：没配 SSH 密钥时 DigitalOcean 会把 root 密码设成「首次登录必须修改」，
# 改之前 cron 会被 PAM 拒绝执行 root 的任务（PAM ERROR: Authentication token is no longer valid），自动更新就一直不跑
cat > /etc/systemd/system/tgst-update.service <<'UNIT'
[Unit]
Description=tg-signal-trader: pull GitHub main and redeploy when it changed
[Service]
Type=oneshot
Environment=HOME=/root
ExecStart=/opt/tg-signal-trader/deploy/update.sh
StandardOutput=append:/var/log/tgst-update.log
StandardError=append:/var/log/tgst-update.log
UNIT
cat > /etc/systemd/system/tgst-update.timer <<'UNIT'
[Unit]
Description=tg-signal-trader: check GitHub every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now tgst-update.timer
echo "== $(date '+%F %T') 初始化完成"
