#!/usr/bin/env bash
# cron 每 5 分钟调用：GitHub 上的部署分支有新提交 → 拉取、重建、重启（config.yaml 的修改也靠它生效）
# 上一次构建没成功（包括首次开机时）也会在这里自动重试
set -euo pipefail
exec 9>/tmp/tgst-update.lock
flock -n 9 || exit 0
cd /opt/tg-signal-trader
BRANCH=$(git rev-parse --abbrev-ref HEAD)
# 加超时：网络卡住时 git / docker 可能一直不退出，一直占着锁，以后的更新就全被跳过
timeout 120 git fetch -q origin "$BRANCH"
TARGET=$(git rev-parse "origin/$BRANCH")
# data/deployed.txt = 最近一次成功启动的提交（构建失败就不会更新它，下次继续重试）
if [ "$(cat data/deployed.txt 2>/dev/null)" != "$TARGET" ]; then
  git reset -q --hard "$TARGET"
  git rev-parse --short HEAD > data/version.txt
  timeout 1500 docker compose up -d --build --force-recreate
  echo "$TARGET" > data/deployed.txt
  echo "$(date '+%F %T') 已更新到 $(git rev-parse --short HEAD)"
fi
