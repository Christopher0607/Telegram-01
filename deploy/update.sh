#!/usr/bin/env bash
# cron 每 5 分钟调用：GitHub 上的部署分支有新提交 → 拉取、重建、重启（config.yaml 的修改也靠它生效）
set -euo pipefail
exec 9>/tmp/tgst-update.lock
flock -n 9 || exit 0
cd /opt/tg-signal-trader
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$BRANCH"
if [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$BRANCH")" ]; then
  git reset -q --hard "origin/$BRANCH"
  git rev-parse --short HEAD > data/version.txt
  docker compose up -d --build --force-recreate
  echo "$(date '+%F %T') 已更新到 $(git rev-parse --short HEAD)"
fi
