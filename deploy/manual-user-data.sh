#!/bin/bash
# 自己在 DigitalOcean 网页上建服务器时用（不用 Claude）：贴进建服务器页面的「Initialization scripts」。步骤见 README「自己重开」。
# 一般贴 README 里那段 4 行的短开机脚本（口令写在里面），它会下载这个文件来跑。直接整段贴这个文件也行：
#   有口令：只把下面 TGST_PASS 那一行的「填这里」换成口令。机器人 Token 这些密钥用口令加密存在仓库的 deploy/secrets.enc 里，开机时解开。
#   没有口令：把下面 6 行等号后面的「填这里」换成你自己的值（等号两边不要加空格），别的都不要动。
#   填了的值（或者短脚本里 export 的）比加密包里的优先：有口令、只换了某一个密钥，就只填那一个。
# 这个文件由 deploy/do_deploy.py manual 从 cloud-init.sh 生成。
[ -n "${TGST_PASS:-}" ] || TGST_PASS='填这里'
cat > /root/tgst.env <<'ENVEOF'
TG_API_ID=填这里
TG_API_HASH=填这里
TG_PHONE=填这里
TG_BOT_TOKEN=填这里
DEEPSEEK_API_KEY=填这里
SETUP_PIN=填这里
ENVEOF
for k in TG_API_ID TG_API_HASH TG_PHONE TG_BOT_TOKEN DEEPSEEK_API_KEY SETUP_PIN; do
  if [ -n "${!k:-}" ]; then echo "$k=${!k}" >> /root/tgst.env; fi
done
chmod 600 /root/tgst.env
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
  git clone -q -b 'main' 'https://github.com/Christopher0607/Telegram-01.git' "$APP" && break
  echo "clone 失败，15 秒后重试（$i）"; sleep 15
done
cd "$APP"

echo "== 写入 .env"
# 有口令：解开仓库里加密的密钥（deploy/secrets.enc），绑定主人的口令（SETUP_PIN）也用它；最上面填了的值优先
: > /root/tgst.dec
if [ -n "$TGST_PASS" ] && [ "$TGST_PASS" != '填这里' ]; then
  if [ -f deploy/secrets.enc ] && TGST_PASS="$TGST_PASS" openssl enc -d -aes-256-cbc -pbkdf2 -iter 1000000 -md sha256 -a -in deploy/secrets.enc -pass env:TGST_PASS > /root/tgst.dec 2>/dev/null \
      && grep -q '^TG_BOT_TOKEN=' /root/tgst.dec; then
    echo "SETUP_PIN=$TGST_PASS" >> /root/tgst.dec
    echo "口令对了：用 deploy/secrets.enc 里加密的密钥"
  else
    : > /root/tgst.dec
    echo "❌ 口令不对（或者仓库里没有 deploy/secrets.enc）：只用最上面填的值"
  fi
fi
cp /root/tgst.dec .env
while IFS= read -r line; do
  case "$line" in *=填这里|"") continue ;; esac
  grep -v "^${line%%=*}=" .env > .env.tmp || true
  mv .env.tmp .env
  echo "$line" >> .env
done < /root/tgst.env
rm -f /root/tgst.env /root/tgst.dec
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
