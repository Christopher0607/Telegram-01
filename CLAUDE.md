# CLAUDE.md —— 给云端 Claude Code 的部署与运维手册

这个仓库是一个 Telegram 信号跟单机器人：监听 4 个喊单频道 → DeepSeek 解析 → 代码风控 → 交易所 USDT 永续模拟/实盘下单（`config.yaml` 的 `exchange.name`：gate、weex 或 bitget；2026-09-26 迁到 WEEX，2026-09-28 又换回 Gate，见日常操作「换交易所」） → 通过用户自己的 Telegram 机器人推送通知和每日日报。代码说明见 README.md，风控参数在 config.yaml。

**用户（俊宇）只用手机，不是程序员。** 用中文沟通，每步做完用一两句话告诉他结果。需要他操作时，给出手机上能照做的具体步骤。

## 你所在的环境和部署方式

- 你在 Claude Code 云端沙盒里，网络经过白名单代理：**不能 SSH 到任何服务器**。（2026-09 实测：Telegram 机器人 API、Gate / WEEX / Bitget 的公共行情、DeepSeek 能访问（WEEX 的完整接口文档：https://www.weex.com/api-doc/llms-full.txt），可以用 `getMe` 验证机器人 Token、用 `/models` 验证 DeepSeek Key；但 Telegram 账号登录只能在服务器上完成，交易所 API 绑定了服务器 IP，也只能在服务器上验证。）
- 所以采用「零 SSH」部署：
  1. `deploy/do_deploy.py` 调用 DigitalOcean API，新建一台服务器，并传入开机脚本 `deploy/cloud-init.sh`。
  2. 服务器开机后自己安装 Docker，从 GitHub 拉取本仓库，启动机器人。之后每 5 分钟检查一次 main 分支，有新提交就自动更新（`deploy/update.sh`）。
  3. Telegram 登录和交易所 API 由用户在他自己的机器人对话里完成，你不经手。
- **你对服务器的所有改动都通过「提交到 main 分支」来完成**，推送后 5 分钟内自动生效，机器人会发「🚀 已启动｜版本 xxx」。

## 铁律

1. **不显示、不提交任何密钥。** 不要打印 Token、Key、密码。`.env`、`deploy.env` 已在 .gitignore 里；每次提交前看一眼 `git status`，确认没有密钥文件被提交。
2. **默认只跑模拟盘。** 只有用户在当前对话里明确说开实盘，并且你提醒过「实盘会真实下单，每单打到止损约亏 X」（X 以机器人里的设置为准，用户点「📊 状态」能看到「🎯 每单风险」；config.yaml 的默认值 2026-09-27 起是总权益的 5%）之后，才能改 `live_trading: true`，并把指定频道改成 `mode: live`。
3. **不擅自改风控参数**（config.yaml 的 risk 段），用户明确要求才改。
4. **只动本项目的服务器**（标签 `tg-signal-trader`），绝不碰用户的其他 DigitalOcean 服务器（例如博悦 V9）。新建服务器前先告诉用户费用（约 6 美元/月）并得到同意；除非用户明确要求，否则不删除服务器。
5. 交易所（Gate / WEEX）API Key 只能有「合约」交易权限，不能有提现权限。
6. **这个 GitHub 仓库是公开的**：私人群/会员群的邀请链接、账户金额等个人信息不要写进任何提交的文件。私人群在 config.yaml 里只写 `- private: 名字`，链接由用户发给机器人（`/join 邀请链接`），只保存在服务器的 data/ 里。

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
| 改参数（杠杆上限、止盈方案等） | 改 config.yaml → 提交到 main → 告诉用户 5 分钟内生效。止盈方案（`tp_plans`）只影响之后新开的单子，已经开着的单子按开仓时的比例走完。2026-09-28 用户要求：3 个止盈 50% / 30% / 清仓，2 个止盈 50% / 清仓。2026-09-29 用户要求：进场一律市价（`market_entry: true`、`allow_limit_orders: false`；价格越过信号全部止盈位、追高后盈亏比 < 1 的照样不开），频道一晒盈利/浮盈/注意仓位就清仓止盈（`close_on_profit_post: true`：AI 输出 `profit` 动作 → `engine.dispatch` 清仓；没写币种时这个频道只有一单就是它；复盘汇总不算）。用户认为这些喊单群靠会费、跟交易所对赌吃跟单者的亏损（这也是转回 Gate 的原因），以后遇到有实力的交易员才正常跟单 |
| 改每单风险 / 同时最多几单 / 每天最多亏几单 | **用户可以自己在机器人里改**：「💰 风控设置」按钮（= `/risk`），或发 `/risk 3%`（每单亏总权益的比例）、`/risk 5u`（固定金额）、`/maxpos 10`（同时最多几单，持仓 + 挂单）、`/maxloss 6`（每天最多亏几单，0 = 不限）；调大要再确认，每单风险超过总权益 20% 的不改。存在服务器 `data/risk.json`（你看不到）：config.yaml 的 `risk_per_trade_pct` / `risk_per_trade_usdt` / `max_open_positions` / `max_daily_losses` 只是默认值，每一项都是谁最后改算谁的（改了 config.yaml 的某一项，机器人里调的那一项就作废）。所以用户让你改时，优先请他在机器人里点；你改 config.yaml 也行（仍属铁律 3：用户明确要求才改），但先问清他机器人里现在是多少。每天最多亏：亏了超过 0.1R 的才算亏损单（保本出场不算），到了当天不再开新仓，第二天 0 点（`timezone_offset_hours`，北京时间）恢复，实盘/模拟分开算（`engine.daily_block`）。2026-09-27 用户要求：每单风险 10% → 5%，同时持仓 3 → 10，每天最多亏 6 单（都改的 config.yaml） |
| 看战绩 | 你连不上服务器。请用户点机器人下方的「📈 战绩」（= `/stats`）和「📜 最近交易」（= `/trades`），把结果贴给你，你帮他分析；每晚 22:00 也会自动推送日报。这些报告和「📊 状态」都是实盘、模拟分开列的（2026-09-27 用户要求） |
| 停止 / 恢复模拟盘 | **用户可以自己在机器人里点**：「⚙️ 实盘/模拟」里的「🧪 停止模拟盘 / 恢复模拟盘」（= `/paper off`、`/paper on`）。停止后模拟频道的消息直接不处理（不识别、不下模拟单、不提醒，也不花 AI 额度），实盘频道照常；还开着的模拟单作废（`engine.void_paper`，不计入战绩），有的话先要确认。存在服务器 `data/paper.json`：config.yaml 的 `paper.enabled` 是默认值，谁最后改算谁的。回测（`/backtest`）不受影响 |
| 问某个频道/群最近的成绩（尤其是刚跟、还没有模拟记录的私人群） | 你读不到私人群，也连不上服务器。请用户点输入框左边「菜单」里的 `/backtest`（或发 `/backtest 名字 天数`，默认 30 天、最多 60 天）→ 点要看的频道 → 等 5～15 分钟，机器人会发「📊 回测」报告，请他截图给你分析。回测在服务器上单独的进程里跑（`backtest.py`），不带交易所密钥、不影响实盘；成交额门槛、止盈方案、每单风险都按 config.yaml 现在的设置，所以改风控前后都可以用它对比。报告里「跳过：成交额太小」多，说明这些币在当前交易所 24 小时成交额低于 `min_24h_volume_usdt`。报告最后的「🔁 换规则对比」是同一批信号按 `backtest.VARIANTS`（关掉 `close_on_profit_post` / 关掉 `market_entry` / 两条都关）各跑一遍（同一进程、共用 K 线缓存），用户问「要不要换回以前的规则」就看这一段。收益率（`backtest.money`）：每单风险是权益 % 按复利算；是固定 U（用户在机器人里设过 `/risk 5u` 这类）就用回测开始时查到的实盘权益换算（`main.start_backtest` 查好传进任务），对比的每一行也带收益率（2026-09-29 用户要求：以前固定 U 时报告里没有收益率）。**组合回测**（2026-09-29 用户要求「把几个频道串在一起回测，更贴切真实情况」）：`/backtest` 面板多选（`main.backtest_panel`，按钮数据 `bts:勾选位:天数` / `btgo:…`，位的顺序是 `main.bt_channels`），或 `/backtest 名字 名字 天数`（`main.pick_channels`）→ `backtest.run_many`：每个频道各自拉消息、AI 识别，按时间混在一起喂给同一个模拟账户（每个频道一个假 chat_id，识别结果按「频道 + 消息 id」对应），共用同时最多几单 / 每天最多亏几单 / 同一个币不重复开；报告有「各频道」一栏，最近的单子带频道名。**Gate 只给最近 1 万根 K 线（1 分钟的约 7 天）**：2026-09-29 以前换回 Gate 后回测只算到最近 7 天（更早的信号拿 K 线报错、被悄悄丢掉），已改成更早的 1 分钟 K 线用 WEEX 公开行情（`HistEx._source`），出错的信号会在报告里列出「另有 N 个信号没法模拟」；Gate 的 K 线成交量是张数，算成交额要乘合约面值 |
| 问单子有没有止损、止盈（2026-09-28 用户问过） | 你连不上交易所（API 绑了服务器 IP）。请用户点「🔍 核对止损止盈」（= `/check`，菜单里也有），截图给你：机器人直接去交易所逐单核对止损单（在不在、数量够不够、价格有没有在 App 里改过）和每一档止盈（挂在交易所 / 已完成 / 程序盯盘市价平），还会列出手动单在交易所有没有止损止盈条件单（只支持 WEEX，`Exchange.open_tpsl`）。交易所里的止损单或止盈挂单不见了，`engine.audit_live` 会补挂：/check 当场一次，监控每 5 分钟一次（`tick % 60`）。止损先挂新的再撤旧的（查错了也不会变成两张），同一单最多补 3 次（`engine.sl_fixes`，重启清零），之后只提醒一次。交易所里的止损是开仓时的原止损；保本/频道移动止损只改程序盯盘的 `soft_sl`，交易所那张留着兜底 |
| 某条消息没跟 / 识别错 | 请用户点「🧠 AI识别」（= `/ai`）截图给你：能看到原文、图片内容、AI 识别结果和跳过原因 |
| 设置交易所 API（Gate） | 请用户：给机器人发 `/ip` 拿到服务器 IP → 在 Gate 建子账户 API（只给「合约」读写权限、不给提现、IP 白名单填这个 IP），把钱划进子账户的 USDT 永续合约账户 → 给机器人发 `/gate KEY SECRET`。机器人会先验证，再保存，并删除这条消息。第一次开实盘前请用户发 `/gatetest`（1 张 BTC 合约实测开仓、挂止损、止损触发平仓，花费约 0.01U），把结果截图给你确认。Gate 开仓单不能带止损，程序是开仓后另挂「平掉整个仓位」的止损触发单（`exchange.place_sl`），挂不上就立刻平仓 |
| 设置交易所 API（WEEX） | 同上，在 WEEX「API 管理」创建（只勾合约交易、不勾提现、IP 白名单填 `/ip` 的 IP、自己设一个 Passphrase），钱划进 USDT 合约账户 → 给机器人发 `/weex KEY SECRET PASSPHRASE`。然后发 `/weextest`（最小数量 BTC 实测开仓、挂止损、止损触发平仓），**不用先切交易所也能测**。WEEX 是开仓后另挂止损单（`placeTpSlOrder`），**按仓位数量挂**（单号记成 `q:单号:数量`），仓位数量变了（分批止盈、限价单陆续成交、手动减仓）由 `engine.fit_sl` 先挂新的再撤旧的。`/weextest` 还测挂单（maker）：POST_ONLY 开仓单能挂能撤、会直接成交的被拒绝；持仓时挂只减仓的止盈限价单，再让止损触发，确认止损照样平掉整个仓位、止盈单没成交。全部通过记为 `selftest_maker_ok:weex`，之后开仓先挂单、止盈挂在交易所；没通过（或测出 ❌）就继续市价开仓、程序盯盘止盈，**不影响实盘**。实测（2026-09-26）：`quantity` 不填报 -1141；填 "0" 会挂出一张数量 0、不平整个仓位的空止损单（触发了也平不掉），**绝不能填 0**；触发价越过现价会报 -1140 |
| 某个频道切实盘/模拟 | **用户可以自己在机器人里切**：「⚙️ 实盘/模拟」按钮（= `/mode`）。切到实盘时机器人会按铁律 2 提醒「实盘会真实下单，每单打到止损约亏…」并要求再点确认；切回模拟立即生效。结果存在服务器 `data/modes.json`（你看不到）：config.yaml 的 `mode` 只是默认值，谁最后改算谁的（改了 config.yaml 里某个频道的 mode，机器人对它的切换就作废）。所以用户让你切某个频道时，优先请他直接在机器人里点；你改 config.yaml 也行，但先问清他机器人里现在是什么状态 |
| 某个频道开实盘（改 config.yaml 的做法） | 先确认交易所 API 已设置（启动消息里会显示「XX API：已设置」）、对应的 `/gatetest` 或 `/weextest` 通过；按铁律 2 提醒后，改 config.yaml 并提交到 main |
| 换交易所（例如 Gate → WEEX） | **代码里有保险**：`/gatetest`、`/weextest` 每一步都通过才记为通过（数据库 `selftest_ok:交易所`），没通过的交易所不会真实下单（`engine.live_block`，启动消息和「📊 状态」会显示「⏸ 还没通过 /weextest」），测试出现 ❌ 会取消通过记录。2026-09-26 已切到 WEEX。① 用户设置新交易所 API、跑 `/weextest` 截图给你确认 ② 请用户点「📊 状态」确认旧交易所没有 [实盘] 持仓/挂单（有的话等它平掉，或点「🛑 全部平仓」）③ 改 config.yaml 的 `exchange.name` 提交到 main。启动时程序会检查：旧交易所还有没平完的实盘单，只要服务器上有旧交易所的 API，就接着用旧交易所管到平仓（`main.attach_old_exchanges` → `engine.others`；每一单用 `engine.xt(t)` 找自己的交易所，盯盘按交易所分组 `monitor_on`，旧交易所出错只提醒、不耽误新交易所），新单在新交易所开；没有旧交易所 API 的才提醒用户去旧交易所 App 处理并不再管理；新交易所没有的模拟单币种会作废。**2026-09-28 换回 Gate**（用户要求：WEEX 空闲资金转去 Gate，WEEX 上开着的单子继续在 WEEX 管到平仓，新单在 Gate 开；`/gatetest` 当天实测全部通过）。手续费：程序按交易所公布的每个币的费率算（WEEX 市价 0.08%、挂单大多 0.02%），`fee_rate` 只是交易所没公布时的后备。**2026-09-27 用户决定留在 WEEX（会员群要求用 WEEX），以后要换 Bitget / Gate 他会主动说**；当时对比过：两边同一时刻价差中位数 0.09%、90% 的币在 0.3% 以内，Bitget 币更多（如 GRAM）、市价费 0.06%。换 Bitget 前要先补：Bitget 的真实下单测试（像 `/weextest`）和挂单开仓/止盈（`Exchange.maker_capable` 现在只有 WEEX）。同一天用户又说在认真考虑换 Bitget 或 Gate（手续费：市价 WEEX 0.08% / Bitget 0.06% / Gate 0.05%，挂单都是 0.02%；USDT 永续数量 WEEX 576 / Bitget 805 / Gate 1014）。Gate 以前 `/gatetest` 实测通过过，换回 Gate 最省事 |
| 挂单（maker）开仓 / 止盈（2026-09-27 用户要求「全部采用挂单」省手续费，之后来回改过几次；2026-09-28 用户以为挂单没成交，查下来是 **WEEX 价格根本没碰到止盈价**（喊单的人用 Bitget 的价格，差 0.1% 左右），又改回挂单。现在 `maker_orders: true`） | config.yaml 的 `exchange.maker_orders: true` 才启用（false = 全部市价；关掉后 30 秒内，已经开着的单子在交易所里的止盈挂单会被撤掉（`sync_open` 里 `drop`），之后价格一碰到止盈价就由程序市价平；再打开时会按剩下的数量补挂）。止盈挂单：价格到了止盈价 10 秒还没成交完（`TP_TOUCH_WAIT_MS`，排队没轮到），或者冲过止盈价 0.3%，撤掉改市价。只在通过 `/weextest` 的挂单测试后生效（「📊 状态」里有一行「📝 下单方式」写着现在是挂单还是市价、为什么）。**止损永远是市价**（挂单止损在急跌时可能成交不了）。开仓：按买一/卖一挂只做 maker 的单子（`engine.open_maker_entry` / `sync_maker`），盘口变了跟着改价，最多等 `maker_entry_wait_sec`（30 秒），没成交的部分改市价；价格跑出进场区 0.5%（`chase_pct`）以外就不追，已成交的部分照常管理。止盈：开仓后每一档挂在交易所（`engine.place_tp_orders`），5 秒核对成交（`sync_tp_orders`），到价 10 秒或冲过 0.3% 还没成交完就撤掉改市价；频道减仓/改止盈/平仓、手动改仓位都会先撤掉止盈挂单再按新数量重挂。回测按「挂单都能成交」算手续费，偏乐观 |
| 跟一个私人群/会员群 | config.yaml 加 `- private: 名字`（先 `mode: paper`）→ 提交到 main → 请用户给机器人发 `/join 邀请链接`（直接发链接也行）。监听账号不在群里会自动用链接加入；要审批的群会提示已申请。群组只跟群主/管理员（含匿名管理员）的消息，频道全跟。启动消息里写「❌ 没连上」就是还没 /join 或链接失效 |
| 改代码 | 修改 → `python selftest.py` 通过 → 提交到 main |
| 服务器状态 | `python deploy/do_deploy.py status` |
| 紧急停止 | 请用户给机器人发 `/closeall`：平掉本程序开的实盘仓位、撤销挂单，并暂停开新仓 |
| 重建服务器（仅用户明确要求） | 先说明后果：交易记录清空、要重新登录 Telegram、IP 变了要更新交易所 API 白名单。然后 `destroy --yes`，再 `create` |

判断能否切实盘：模拟盘至少 30 单、总 R 为正。「其中程序补止损的」部分如果是负的，建议给该频道设 `fallback_sl_mode: off`。

## 故障排查

- **创建 15 分钟后机器人仍无反应**：最常见的原因是私有仓库没给 GITHUB_READ_TOKEN、代码不在 main 分支，或者 TG_BOT_TOKEN 填错。请用户用手机浏览器打开 cloud.digitalocean.com → Droplets → tg-signal-trader → Access → Launch Droplet Console，运行 `tail -50 /var/log/tgst-setup.log; docker logs --tail 30 tg-signal-trader`，把结果截图给你。首次构建失败时，自动更新任务每 5 分钟会重试一次（`/var/log/tgst-update.log`）。
- **机器人发「❌ 发送登录验证码失败」**：TG_API_ID / TG_API_HASH / TG_PHONE 填错（服务器上的变量是创建时写入的，要改只能 `destroy --yes` 后用正确的值重新 `create`；这时还没有交易记录，没有损失）。
- **机器人发「⚠️ 连不上 Gate / WEEX」**：服务器所在地区可能访问不了交易所。先看错误内容；确认是地区限制的话，征得用户同意后换 `--region`（如 `sgp1` 换 `fra1`）重建。
- **机器人突然完全没反应**：先用 `getMe` 检查 Token。返回 401 说明机器人被删或 Token 被重置。Token 是建服务器时写入的，只能让用户新建机器人（名字按第 2 步的要求），征得同意后 `destroy --yes` 再 `create`；已登录的话，登录状态和交易记录会一起清空。
- **改动没生效**：确认已合并进 main；在控制台运行 `tail /var/log/tgst-update.log` 查看。**首次部署时的真实原因**：建服务器没配 SSH 密钥，DigitalOcean 把 root 密码（发到用户邮箱）设成「首次登录必须修改」，改之前 cron 会被 PAM 拒绝执行 root 任务，自动更新一次都不会跑，重启也没用。现在的开机脚本改用 systemd 定时器 `tgst-update.timer`，不受影响；2026-09-25 之前建的服务器，需要用户打开控制台，用邮件里的密码登录并设一次新密码。控制台提示 `Current password` 就是这个原因。
- **/gate（或 /bitget）验证失败**：检查 IP 白名单、合约交易权限（Bitget 还要检查 passphrase）。
- **DeepSeek 报 401/402**：Key 错误或余额不足；模型名应为 `deepseek-v4-flash`。
