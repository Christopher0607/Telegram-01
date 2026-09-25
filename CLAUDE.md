# CLAUDE.md —— 给云端 Claude Code 的部署与运维手册

这个仓库是一个 Telegram 信号跟单机器人：监听 4 个喊单频道 → DeepSeek 解析 → 代码风控 → Bitget USDT 永续模拟/实盘下单 → 通过用户自己的 Telegram 机器人推送通知和每日日报。代码说明见 README.md，风控参数在 config.yaml。

**用户（俊宇）只用手机，不是程序员。** 用中文沟通，每步做完用一两句话告诉他结果。需要他操作时，给出手机上能照做的具体步骤。

## 你所在的环境和部署方式

- 你在 Claude Code 云端沙盒里，网络经过白名单代理：**不能 SSH 到任何服务器**。（2026-09 实测：Telegram 机器人 API、Bitget 公共行情、DeepSeek 能访问，可以用 `getMe` 验证机器人 Token、用 `/models` 验证 DeepSeek Key；但 Telegram 账号登录只能在服务器上完成，Bitget API 绑定了服务器 IP，也只能在服务器上验证。）
- 所以采用「零 SSH」部署：
  1. `deploy/do_deploy.py` 调用 DigitalOcean API，新建一台服务器，并传入开机脚本 `deploy/cloud-init.sh`。
  2. 服务器开机后自己安装 Docker，从 GitHub 拉取本仓库，启动机器人。之后每 5 分钟检查一次 main 分支，有新提交就自动更新（`deploy/update.sh`）。
  3. Telegram 登录和 Bitget API 由用户在他自己的机器人对话里完成，你不经手。
- **你对服务器的所有改动都通过「提交到 main 分支」来完成**，推送后 5 分钟内自动生效，机器人会发「🚀 已启动｜版本 xxx」。

## 铁律

1. **不显示、不提交任何密钥。** 不要打印 Token、Key、密码。`.env`、`deploy.env` 已在 .gitignore 里；每次提交前看一眼 `git status`，确认没有密钥文件被提交。
2. **默认只跑模拟盘。** 只有用户在当前对话里明确说开实盘，并且你提醒过「实盘会真实下单，每单打到止损约亏 10U」之后，才能改 `live_trading: true`，并把指定频道改成 `mode: live`。
3. **不擅自改风控参数**（config.yaml 的 risk 段），用户明确要求才改。
4. **只动本项目的服务器**（标签 `tg-signal-trader`），绝不碰用户的其他 DigitalOcean 服务器（例如博悦 V9）。新建服务器前先告诉用户费用（约 6 美元/月）并得到同意；除非用户明确要求，否则不删除服务器。
5. Bitget API Key 只能有「合约交易」权限，不能有提现权限。

## 首次部署

### 第 0 步：把代码放进仓库
- 如果仓库里只有 `tg-signal-trader.zip`：运行 `unzip -o tg-signal-trader.zip && rm tg-signal-trader.zip`（文件直接解压到仓库根目录）。
- 验证代码：`pip install -r requirements.txt && python selftest.py`，应显示「全部 N 项通过」。
- 提交到 main：先试 `git push origin HEAD:main`。如果只能推当前工作分支，就推工作分支并创建 PR，请用户在 GitHub App 里点 Merge，合并后再继续。服务器只跟踪 main 分支。

### 第 1 步：检查能否访问 DigitalOcean API
`curl -s -o /dev/null -w "%{http_code}\n" https://api.digitalocean.com/v2/account -H "Authorization: Bearer $DIGITALOCEAN_TOKEN"`
- 返回 200：正常。
- 返回 000 或 403（代理拦截）：请用户改环境设置。在 Claude Code 里点输入框上方显示环境名的云朵图标 → 编辑当前环境 → Network access 选 **Custom** → Allowed domains 填 `api.digitalocean.com` → 勾选「Also include default list of common package managers」→ 保存 → **开一个新会话**（设置只对新会话生效）。手机 App 里找不到这个入口时，用手机浏览器打开 claude.ai/code 操作。

### 第 2 步：准备变量
推荐让用户把变量填在同一个环境设置的「Environment variables」框里（格式 `KEY=VALUE`，每行一个），这样密钥不会出现在对话里。需要这些：

| 变量 | 用户去哪拿 |
|---|---|
| DIGITALOCEAN_TOKEN | DigitalOcean 网页 → API → Generate New Token，勾选 **Write** |
| TG_API_ID / TG_API_HASH | 手机浏览器打开 my.telegram.org → 用 Telegram 验证码登录 → API development tools |
| TG_PHONE | 监听频道用的 Telegram 账号手机号，带国家码，如 +8613800000000 |
| TG_BOT_TOKEN | Telegram 找 @BotFather → /newbot（新建一个，不要复用博悦 V9 的），同时记下机器人用户名。**用户名和显示名不要带 Telegram、GenDan / 跟单 之类的字样**（首次部署时这样的机器人两次在建好十几分钟内被 Telegram 自动删除）。拿到 Token 先用 `getMe` 验证，建服务器前再验证一次 |
| DEEPSEEK_API_KEY | 用户已有 |
| GITHUB_READ_TOKEN | **仓库是私有的才需要**：GitHub → Settings → Developer settings → Fine-grained tokens → 只选这个仓库 → Contents: Read-only，有效期选最长。仓库公开则不需要 |

- 用户如果直接在对话里发了这些值：写进 `deploy.env`（已被 gitignore），不要在回复里复述。
- 预检（不会创建任何东西，输出已打码）：`python deploy/do_deploy.py create --repo 用户名/仓库名 --dry-run [--env-file deploy.env]`

### 第 3 步：创建服务器
告诉用户：「将在新加坡新建一台 1GB 服务器，约 6 美元/月，不会动你原来的服务器。」得到同意后运行：
`python deploy/do_deploy.py create --repo 用户名/仓库名 --bot-username 机器人用户名 [--env-file deploy.env]`
把输出里的 IP、绑定口令（PIN）和下一步告诉用户。

### 第 4 步：用户在 Telegram 里完成绑定和登录（约 5–10 分钟后）
1. 打开输出里的链接 `t.me/机器人?start=PIN` 点「开始」，或者给机器人发送 `/start PIN`。
2. 机器人会说验证码已发送。用户到官方「Telegram」对话里看验证码，**把数字用空格隔开**发给机器人（如 `1 2 3 4 5`）。
3. 如果账号开了二步验证，按提示发送密码（机器人收到后立即删除）。
4. 看到「🚀 信号跟单已启动」就完成了。请用户发 `/status` 给机器人，并把结果告诉你确认。

## 之后的日常操作

| 用户说 | 你做 |
|---|---|
| 改参数（止损金额、杠杆上限等） | 改 config.yaml → 提交到 main → 告诉用户 5 分钟内生效 |
| 看战绩 | 你连不上服务器。请用户给机器人发 `/stats` 并把结果贴给你，你帮他分析；每晚 22:00 也会自动推送日报 |
| 设置 Bitget | 请用户：给机器人发 `/ip` 拿到服务器 IP → 在 Bitget 建子账户 API（只勾合约交易、不勾提现、IP 白名单填这个 IP）→ 给机器人发 `/bitget KEY SECRET PASSPHRASE`。机器人会先验证，再保存，并删除这条消息 |
| 某个频道开实盘 | 先确认 Bitget 已设置（启动消息里会显示「Bitget API：已设置」）；按铁律 2 提醒后，改 config.yaml 并提交到 main |
| 改代码 | 修改 → `python selftest.py` 通过 → 提交到 main |
| 服务器状态 | `python deploy/do_deploy.py status` |
| 紧急停止 | 请用户给机器人发 `/closeall`：平掉本程序开的实盘仓位、撤销挂单，并暂停开新仓 |
| 重建服务器（仅用户明确要求） | 先说明后果：交易记录清空、要重新登录 Telegram、IP 变了要更新 Bitget 白名单。然后 `destroy --yes`，再 `create` |

判断能否切实盘：模拟盘至少 30 单、总 R 为正。「其中程序补止损的」部分如果是负的，建议给该频道设 `fallback_sl_mode: off`。

## 故障排查

- **创建 15 分钟后机器人仍无反应**：最常见的原因是私有仓库没给 GITHUB_READ_TOKEN、代码不在 main 分支，或者 TG_BOT_TOKEN 填错。请用户用手机浏览器打开 cloud.digitalocean.com → Droplets → tg-signal-trader → Access → Launch Droplet Console，运行 `tail -50 /var/log/tgst-setup.log; docker logs --tail 30 tg-signal-trader`，把结果截图给你。首次构建失败时，自动更新任务每 5 分钟会重试一次（`/var/log/tgst-update.log`）。
- **机器人发「❌ 发送登录验证码失败」**：TG_API_ID / TG_API_HASH / TG_PHONE 填错（服务器上的变量是创建时写入的，要改只能 `destroy --yes` 后用正确的值重新 `create`；这时还没有交易记录，没有损失）。
- **机器人发「⚠️ 连不上 Bitget」**：服务器所在地区可能访问不了 Bitget。先看错误内容；确认是地区限制的话，征得用户同意后换 `--region`（如 `sgp1` 换 `fra1`）重建。
- **机器人突然完全没反应**：先用 `getMe` 检查 Token。返回 401 说明机器人被删或 Token 被重置。Token 是建服务器时写入的，只能让用户新建机器人（名字按第 2 步的要求），征得同意后 `destroy --yes` 再 `create`；已登录的话，登录状态和交易记录会一起清空。
- **改动没生效**：确认已合并进 main；在控制台运行 `tail /var/log/tgst-update.log` 查看。**首次部署时的真实原因**：建服务器没配 SSH 密钥，DigitalOcean 把 root 密码（发到用户邮箱）设成「首次登录必须修改」，改之前 cron 会被 PAM 拒绝执行 root 任务，自动更新一次都不会跑，重启也没用。现在的开机脚本改用 systemd 定时器 `tgst-update.timer`，不受影响；2026-09-25 之前建的服务器，需要用户打开控制台，用邮件里的密码登录并设一次新密码。控制台提示 `Current password` 就是这个原因。
- **/bitget 验证失败**：检查 IP 白名单、合约交易权限、passphrase。
- **DeepSeek 报 401/402**：Key 错误或余额不足；模型名应为 `deepseek-v4-flash`。
