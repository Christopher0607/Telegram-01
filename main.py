"""入口

  python main.py login              交互式登录监听用的 Telegram 账号（手动在终端输入验证码）
  python main.py login send         非交互登录第 1 步：发送验证码到 Telegram（给 Claude Code 用）
  python main.py login code 12345   非交互登录第 2 步：提交验证码（开了二步验证：设环境变量 TG_2FA_PASSWORD）
  python main.py login password     二步验证：读取环境变量 TG_2FA_PASSWORD 完成登录
  python main.py check              检查交易所 / AI / Telegram / 通知机器人是否配置正确
  python main.py replay 30          拿每个频道最近 30 条消息测试 AI 识别效果（不下单）
  python main.py stats              打印当前持仓和各频道战绩（机器人运行时也可以用）
  python main.py run                正式运行（docker compose up 默认就是这个）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events, utils
import ccxt.async_support as ccxt
from telethon.errors import (FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,
                             PhoneCodeInvalidError, SessionPasswordNeededError)
from telethon.tl.functions.channels import JoinChannelRequest

from config import ChannelCfg, Config, DATA_DIR, save_secrets
from db import DB
from engine import MODE_CN, SIDE_CN, Engine, MsgCtx, fmt
from exchange import Exchange
from notifier import Notifier
from signal_parser import SignalParser

log = logging.getLogger("main")


def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    f = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(f)
    fh = RotatingFileHandler(os.path.join(DATA_DIR, "bot.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(f)
    root.addHandler(sh)
    root.addHandler(fh)
    for noisy in ("telethon", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_client(cfg: Config) -> TelegramClient:
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        sys.exit("❌ .env 里缺少 TG_API_ID / TG_API_HASH（在 my.telegram.org 申请）")
    os.makedirs(DATA_DIR, exist_ok=True)
    return TelegramClient(os.path.join(DATA_DIR, "telegram"), cfg.tg_api_id, cfg.tg_api_hash)


async def resolve_channels(client, cfg: Config, join: bool = True) -> dict:
    """username → 频道实体；没加入的频道自动加入（收实时消息必须先加入）。返回 {peer_id: ChannelCfg}"""
    out = {}
    for ch in cfg.channels:
        if ch.mode == "off":
            continue
        try:
            ent = await client.get_entity(ch.username)
        except Exception as e:
            log.error("找不到频道 @%s：%s", ch.username, e)
            continue
        if join and getattr(ent, "left", False):
            try:
                await client(JoinChannelRequest(ent))
                log.info("已加入频道 %s", getattr(ent, "title", ch.username))
            except Exception as e:
                log.warning("加入频道 @%s 失败：%s", ch.username, e)
        ch.title = getattr(ent, "title", "") or ch.username
        out[utils.get_peer_id(ent)] = ch
    return out


async def build_ctx(ch: ChannelCfg, msg, edited: bool) -> MsgCtx | None:
    text = (msg.raw_text or "").strip()
    if not text:
        return None  # 纯图片/视频没有文字，识别不了
    reply_text = None
    if msg.reply_to_msg_id:
        try:
            r = await msg.get_reply_message()
            reply_text = (r.raw_text or "").strip() if r else None
        except Exception:
            pass
    date = msg.edit_date if (edited and msg.edit_date) else msg.date
    return MsgCtx(channel=ch, title=ch.title or ch.username, chat_id=msg.chat_id, msg_id=msg.id, date=date,
                  text=text[:3000], reply_to=msg.reply_to_msg_id, reply_text=reply_text,
                  forwarded=msg.fwd_from is not None, edited=edited)


def describe(a: dict) -> str:
    t = a["type"]
    who = a.get("symbol") or "(按回复关联)"
    if t == "open":
        if a.get("entry_low") or a.get("entry_high"):
            zone = f"{'限价' if a['entry_type'] == 'limit' else '市价'} {fmt(a.get('entry_low'))}~{fmt(a.get('entry_high'))}"
        else:
            zone = "市价"
        tps = "/".join(fmt(x) for x in a["take_profits"]) or "-"
        return f"开仓 {a['symbol']} {SIDE_CN[a['side']]}｜{zone}｜止损 {fmt(a.get('stop_loss'))}｜止盈 {tps}｜{a['confidence']}"
    if t == "close":
        return f"平仓 {who} {a['fraction'] * 100:.0f}%｜{a['confidence']}"
    if t == "move_sl":
        return f"移动止损 {who} → {'保本' if a['breakeven'] else fmt(a['price'])}｜{a['confidence']}"
    if t == "update_tp":
        return f"更新止盈 {who} → {'/'.join(fmt(x) for x in a['take_profits'])}｜{a['confidence']}"
    return str(a)


OUTCOME_CN = {"no_action": "无操作", "edit_ignored": "频道修改了已跟过的信号，不跟随", "closed": "已平仓",
              "reduced": "已减仓", "pending cancelled": "已撤销挂单", "already closed": "交易所里已经平仓",
              "reduce too small": "减仓量太小，未执行", "move_sl: ok": "已收紧止损",
              "move_sl: 不是收紧": "新止损没有更紧，忽略", "update_tp: ok": "已更新止盈",
              "update_tp: 无效": "新止盈已经过了现价，忽略"}


def outcome_cn(outcome: str | None) -> str:
    out = []
    for p in (outcome or "").split(" | "):
        if p in OUTCOME_CN:
            out.append(OUTCOME_CN[p])
        elif p.startswith("opened #"):
            out.append("已开仓 " + p[len("opened "):])
        elif p.startswith("skip: "):
            out.append("跳过：" + p[len("skip: "):])
        elif p.startswith("parse_error: "):
            out.append("AI 解析失败：" + p[len("parse_error: "):][:80])
        elif p.startswith("error: "):
            out.append("执行出错：" + p[len("error: "):][:80])
        elif p:
            out.append(p.replace("close:", "平仓：").replace("move_sl:", "移动止损：").replace("update_tp:", "更新止盈："))
    return "；".join(out) or "-"


def ai_text(db: DB, cfg: Config, n: int) -> str:
    """/ai：最近 n 条频道消息的原文开头、AI 识别结果、程序最后怎么处理的（新的在上面）。"""
    rows = db.recent_messages(n)
    if not rows:
        return "🧠 还没有收到频道消息。"
    tz = timezone(timedelta(hours=cfg.tz_offset))
    lines = [f"🧠 最近 {len(rows)} 条频道消息的 AI 识别结果（新的在上面）"]
    for r in rows:
        ch = cfg.channel_by_username(r["channel"])
        when = datetime.fromtimestamp(r["ts"], tz).strftime("%m-%d %H:%M")
        head = (r["text"] or "").replace("\n", " ")[:50]
        lines.append(f"\n{when} {(ch.title if ch and ch.title else r['channel'])}{'（修改后）' if r['edited'] else ''}：{head}")
        parsed = json.loads(r["parsed"]) if r["parsed"] else None
        if parsed and parsed.get("actions"):
            lines += [f"  🤖 {describe(a)}" for a in parsed["actions"]]
        elif parsed:
            lines.append(f"  🤖 无指令（{parsed.get('note') or '-'}）")
        lines.append(f"  结果：{outcome_cn(r['outcome'])}")
    return "\n".join(lines)


# ============================== 在机器人对话里登录 ==============================
async def bot_login(client, cfg: Config, notifier: Notifier):
    """监听账号没登录时，通过你自己的机器人要验证码完成登录（手机上就能完成，不需要 SSH）。"""
    phone = cfg.tg_phone
    if not phone:
        sys.exit("❌ .env 里没有 TG_PHONE")
    masked = phone[:4] + "****" + phone[-3:]
    await notifier.skip_backlog()
    while True:
        try:
            sent = await client.send_code_request(phone)
        except FloodWaitError as e:
            await notifier.send(f"⏳ Telegram 要求等待约 {e.seconds // 60 + 1} 分钟才能再发验证码，到时我会自动重试。")
            await asyncio.sleep(e.seconds + 5)
            continue
        except Exception as e:  # API ID/Hash、手机号填错或网络问题：告诉主人，而不是崩溃后反复重启、一声不吭
            log.exception("发送登录验证码失败")
            await notifier.send(f"❌ 给 {masked} 发送登录验证码失败：{str(e)[:200]}\n"
                                f"请把这条消息发给 Claude Code。10 分钟后我会自动重试。")
            await asyncio.sleep(600)
            continue
        await notifier.send(f"🔐 需要登录监听频道用的 Telegram 账号 {masked}。\n"
                            f"验证码已发到这个账号里官方的「Telegram」对话。\n"
                            f"请把验证码的数字用空格隔开发给我，例如：1 2 3 4 5\n"
                            f"（原样直接发，Telegram 会认为验证码泄露而作废）")
        code = ""
        while not code:
            m = await notifier.next_owner_message(1800)
            if not m:
                break
            if m["text"].strip().startswith("/"):
                continue  # 比如又点了一次绑定链接（/start 口令）：口令不是验证码
            await notifier.delete(m)
            code = re.sub(r"\D", "", m["text"])
            if not code:
                await notifier.send("没看到数字。请把验证码的数字用空格隔开发给我，例如：1 2 3 4 5")
        if not code:
            await notifier.send("⌛ 30 分钟没收到验证码。随便发一条消息给我，我就重新发送验证码。")
            while not await notifier.next_owner_message(3600):
                pass
            continue
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            return
        except SessionPasswordNeededError:
            pass
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            await notifier.send("❌ 验证码不对或已过期，我重新发一个。")
            continue
        while True:  # 二步验证
            await notifier.send("🔑 这个账号开了二步验证，请发送二步验证密码（收到后我会立刻删除这条消息）。")
            pm = await notifier.next_owner_message(1800)
            if not pm:
                continue
            await notifier.delete(pm)
            try:
                await client.sign_in(password=pm["text"].strip())
                return
            except PasswordHashInvalidError:
                await notifier.send("❌ 二步验证密码不对，请再发一次。")


async def verify_bitget(key: str, secret: str, passphrase: str) -> tuple[bool, str]:
    ex = ccxt.bitget({"apiKey": key, "secret": secret, "password": passphrase, "enableRateLimit": True,
                      "options": {"defaultType": "swap"}})
    try:
        b = await ex.fetch_balance({"type": "swap"})
        return True, f"合约账户权益 {float((b.get('USDT') or {}).get('total') or 0):.2f}U"
    except Exception as e:
        return False, str(e)[:200]
    finally:
        await ex.close()


async def admin_command(text: str, msg: dict, cfg: Config, notifier: Notifier, engine: Engine,
                        restart=lambda: asyncio.get_running_loop().call_later(3, os._exit, 0)) -> str | None:
    """机器人命令入口：/ip、/bitget 在这里处理，其余交给 engine。"""
    cmd = text.split()[0].lower().split("@")[0]
    if cmd == "/ai":
        arg = text.split()[1] if len(text.split()) > 1 else ""
        return ai_text(engine.db, cfg, max(1, min(int(arg) if arg.isdigit() else 10, 20)))
    if cmd == "/ip":
        return (f"🌐 服务器 IP：{cfg.server_ip or '未知（请在 DigitalOcean 后台查看）'}\n"
                f"在 Bitget 创建 API 时，IP 白名单填这个。")
    if cmd == "/bitget":
        await notifier.delete(msg)
        parts = text.split()
        if len(parts) != 4:
            return "用法：/bitget API_KEY SECRET PASSPHRASE（三项之间用空格隔开）。你刚才那条消息我已经删除。"
        ok, info = await verify_bitget(*parts[1:])
        if not ok:
            return (f"❌ 这组 Bitget API 验证失败：{info}\n请检查：IP 白名单是否填了 {cfg.server_ip or '服务器 IP'}、"
                    f"是否勾了合约交易、passphrase 是否正确。你的消息已删除，没有保存。")
        save_secrets({"BITGET_API_KEY": parts[1], "BITGET_API_SECRET": parts[2], "BITGET_API_PASSPHRASE": parts[3]})
        restart()  # 3 秒后退出，Docker 自动重启并读取新密钥
        return (f"✅ Bitget API 验证通过（{info}），已保存，3 秒后自动重启生效。你的消息已删除。\n"
                f"现在仍然是模拟盘；要开实盘，请告诉 Claude Code。")
    return await engine.handle_command(text)


# ============================== 命令 ==============================
async def cmd_login(cfg: Config, args: list[str]):
    client = make_client(cfg)
    state_path = os.path.join(DATA_DIR, "login_state.json")
    sub = args[0] if args else ""
    try:
        if not sub:  # 交互式
            await client.start(phone=cfg.tg_phone or (lambda: input("手机号（带国家码，如 +8613800000000）：")))
        else:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"✅ 已经登录：{me.first_name}（id={me.id}），不需要再登录")
                return
            if sub == "send":
                if not cfg.tg_phone:
                    sys.exit("❌ .env 里没有 TG_PHONE")
                sent = await client.send_code_request(cfg.tg_phone)
                with open(state_path, "w") as f:
                    json.dump({"phone": cfg.tg_phone, "hash": sent.phone_code_hash}, f)
                print("✅ 验证码已发送（在 Telegram App 的官方「Telegram」对话里），拿到后运行：python main.py login code 验证码")
                return
            if sub == "code":
                if len(args) < 2 or not os.path.exists(state_path):
                    sys.exit("❌ 用法：python main.py login code 12345（先运行 login send）")
                with open(state_path) as f:
                    st = json.load(f)
                try:
                    await client.sign_in(st["phone"], args[1].strip(), phone_code_hash=st["hash"])
                except SessionPasswordNeededError:
                    if not os.getenv("TG_2FA_PASSWORD"):
                        print("🔐 这个账号开了二步验证，请设置环境变量 TG_2FA_PASSWORD 后运行：python main.py login password")
                        sys.exit(2)
                    await client.sign_in(password=os.getenv("TG_2FA_PASSWORD"))
            elif sub == "password":
                if not os.getenv("TG_2FA_PASSWORD"):
                    sys.exit("❌ 没有设置环境变量 TG_2FA_PASSWORD")
                await client.sign_in(password=os.getenv("TG_2FA_PASSWORD"))
            else:
                sys.exit("❌ 未知参数，可用：login / login send / login code 12345 / login password")
        me = await client.get_me()
        if os.path.exists(state_path):
            os.remove(state_path)
        print(f"✅ 登录成功：{me.first_name}（id={me.id}），会话已保存在 data/ 目录")
    finally:
        await client.disconnect()


async def cmd_stats(cfg: Config):
    """不依赖 Telegram，直接读数据库打印持仓和战绩（机器人运行中也能用）。"""
    class _Quiet:
        async def send(self, text):
            return False

    ex = Exchange(cfg)
    await ex.init()
    try:
        eng = Engine(cfg, DB(os.path.join(DATA_DIR, "trader.db")), ex, None, _Quiet())
        print(await eng.status_text())
        print()
        print(eng.stats_text())
    finally:
        await ex.close()


async def cmd_check(cfg: Config):
    print("\n== 1. 交易所 ==")
    ex = Exchange(cfg)
    try:
        await ex.init()
        print(f"✅ 行情接口正常（{len(ex.ex.markets)} 个市场）")
        for b in ("BTC", "HYPE", "NIL"):
            s, scale = ex.resolve(b)
            print(f"   {b} → {s or '没有这个合约'}" + (f"（价格×{scale:g}）" if scale != 1 else ""))
        if ex.has_keys:
            eq, free = await ex.balance()
            print(f"✅ API Key 正常（{'统一账户 UTA' if ex.uta else '经典账户'}）：权益 {eq:.2f}U，可用 {free:.2f}U")
            pos = await ex.positions()
            print(f"   当前持仓：{', '.join(pos) or '无'}")
            if ex.uta and cfg.margin_mode == "isolated":
                print("   ⚠️ 统一账户程序无法切换逐仓，请在 Bitget App 里把合约设为逐仓")
        else:
            print("ℹ️ 没填 Bitget API Key：只能跑模拟盘")
    except Exception as e:
        print(f"❌ 交易所出错：{e}")
    finally:
        await ex.close()

    print("\n== 2. AI 解析 ==")
    parser = SignalParser(cfg)
    try:
        sample = "#HYPE 輕倉市價多\n96.4-96.8\n✅止盈：99.8-104-113\n❌止損：93.7"
        res = await parser.parse(MsgCtx(ChannelCfg("test"), "测试", 0, 0, datetime.now(timezone.utc), sample))
        print(f"✅ {cfg.llm_provider}/{cfg.llm_model} 正常：", "；".join(describe(a) for a in res["actions"]) or "（无动作）")
    except Exception as e:
        print(f"❌ AI 出错：{e}")
    finally:
        await parser.close()

    print("\n== 3. Telegram ==")
    client = make_client(cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("❌ 还没登录，先运行：python main.py login")
            return
        me = await client.get_me()
        print(f"✅ 监听账号：{me.first_name}（id={me.id}）")
        chans = await resolve_channels(client, cfg, join=False)
        for ch in cfg.channels:
            ok = any(c is ch for c in chans.values())
            print(f"   {'✅' if ok else '❌'} @{ch.username} {ch.title}｜模式：{cfg.mode_for(ch)}")
        print("\n== 4. 通知机器人 ==")
        if not cfg.tg_bot_token:
            print("ℹ️ 没填 TG_BOT_TOKEN：通知会发到监听账号的「收藏夹」，命令功能不可用")
            return
        n = Notifier(cfg.tg_bot_token, cfg.tg_owner_id or me.id)
        ok = await n.send("✅ 测试消息：通知通道正常")
        await n.close()
        print("✅ 机器人已给你发测试消息" if ok else "❌ 机器人发不出消息：先在 Telegram 里给你的机器人发一次 /start，再重试")
    finally:
        await client.disconnect()


async def cmd_replay(cfg: Config, n: int):
    client = make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("❌ 还没登录，先运行：python main.py login")
    ex = Exchange(cfg)
    await ex.init()
    parser = SignalParser(cfg)
    try:
        chans = await resolve_channels(client, cfg, join=False)
        for ch in chans.values():
            msgs = [m async for m in client.iter_messages(ch.username, limit=n)]
            print(f"\n===== {ch.title}（@{ch.username}）最近 {len(msgs)} 条 =====")
            n_open = n_ok = 0
            for m in reversed(msgs):
                ctx = await build_ctx(ch, m, False)
                if not ctx:
                    continue
                res = await parser.parse(ctx)
                head = ctx.text.replace("\n", " ")[:60]
                print(f"\n[{m.date.astimezone().strftime('%m-%d %H:%M')}] {head}")
                if not res["actions"]:
                    print(f"   → 无指令（{res['note']}）")
                for a in res["actions"]:
                    flag = ""
                    if a["type"] == "open":
                        n_open += 1
                        problems = []
                        if a["confidence"] != "high":
                            problems.append("不够明确")
                        sl_note = ""
                        if not a.get("stop_loss"):
                            if str(cfg.risk_for(ch)["fallback_sl_mode"]).lower() in ("atr", "pct"):
                                sl_note = "（没给止损，程序会补）"
                            else:
                                problems.append("没止损")
                        if a.get("symbol") and not ex.resolve(a["symbol"])[0]:
                            problems.append("Bitget 无此合约")
                        n_ok += not problems
                        flag = f"  ✓会跟{sl_note}" if not problems else f"  ✗会跳过：{'、'.join(problems)}"
                    print(f"   → {describe(a)}{flag}")
            print(f"\n小结：{n_open} 个开仓信号，其中 {n_ok} 个符合跟单条件（还要再过价格/盈亏比等实时检查）")
    finally:
        await parser.close()
        await ex.close()
        await client.disconnect()


async def init_exchange(ex: Exchange, notifier: Notifier):
    """加载 Bitget 市场信息。失败就在机器人里提醒一次，之后每分钟重试（不让程序崩溃后反复重启、你却收不到任何消息）。"""
    warned = False
    while True:
        try:
            await ex.init()
            if warned:
                await notifier.send("✅ 已连上 Bitget")
            return
        except Exception as e:
            log.exception("连接 Bitget 失败")
            if not warned:
                warned = True
                await notifier.send(f"⚠️ 连不上 Bitget：{str(e)[:200]}\n我会每分钟自动重试。"
                                    f"如果一直收不到「✅ 已连上 Bitget」，请把这条消息发给 Claude Code。")
            await asyncio.sleep(60)


async def cmd_run(cfg: Config):
    db = DB(os.path.join(DATA_DIR, "trader.db"))
    ex = Exchange(cfg)
    if cfg.live_trading and not ex.has_keys:
        log.error("live_trading 已打开但没填 Bitget API Key → 本次全部按模拟盘运行")
        cfg.live_trading = False
    client = make_client(cfg)
    await client.connect()
    notifier = Notifier(cfg.tg_bot_token, cfg.tg_owner_id, client, DATA_DIR)
    authorized = await client.is_user_authorized()
    if not notifier.owner_id:
        if authorized and not cfg.setup_pin:
            notifier.owner_id = (await client.get_me()).id
        elif cfg.tg_bot_token and cfg.setup_pin:
            await notifier.discover_owner(cfg.setup_pin)   # 等你给机器人发 /start <PIN>
        else:
            sys.exit("❌ 需要设置 TG_OWNER_ID 或 SETUP_PIN（配合 TG_BOT_TOKEN），或者先运行 python main.py login")
    if not authorized:
        if not cfg.tg_bot_token:
            sys.exit("❌ Telegram 还没登录，先运行：python main.py login")
        await bot_login(client, cfg, notifier)
    me = await client.get_me()
    await init_exchange(ex, notifier)  # 放在绑定和登录之后：连不上 Bitget 时也能通过机器人告诉你
    parser = SignalParser(cfg)
    engine = Engine(cfg, db, ex, parser, notifier)
    chans = await resolve_channels(client, cfg, join=True)
    if not chans:
        sys.exit("❌ 没有可监听的频道，检查 config.yaml")

    # 每个频道一个队列：同一频道的消息严格按顺序处理，不同频道互不阻塞
    queues: dict[int, asyncio.Queue] = {}
    tasks: list[asyncio.Task] = []

    async def worker(q: asyncio.Queue):
        while True:
            ch, msg, edited = await q.get()
            try:
                ctx = await build_ctx(ch, msg, edited)
                if ctx:
                    await engine.handle_message(ctx)
            except Exception:
                log.exception("处理消息出错")

    def enqueue(event, edited: bool):
        ch = chans.get(event.chat_id)
        if not ch:
            return
        if event.chat_id not in queues:
            queues[event.chat_id] = asyncio.Queue()
            tasks.append(asyncio.create_task(worker(queues[event.chat_id])))
        queues[event.chat_id].put_nowait((ch, event.message, edited))

    async def on_new(event):
        enqueue(event, False)

    async def on_edit(event):
        enqueue(event, True)

    client.add_event_handler(on_new, events.NewMessage(chats=list(chans)))
    client.add_event_handler(on_edit, events.MessageEdited(chats=list(chans)))
    async def on_command(text: str, msg: dict) -> str | None:
        return await admin_command(text, msg, cfg, notifier, engine)

    tasks.append(asyncio.create_task(engine.monitor_forever()))
    tasks.append(asyncio.create_task(notifier.command_loop(on_command)))

    modes = "\n".join(f"• {c.title}：{MODE_CN[cfg.mode_for(c)]}" for c in chans.values())
    try:
        with open(os.path.join(DATA_DIR, "version.txt")) as f:
            version = f"｜版本 {f.read().strip()}"
    except OSError:
        version = ""
    await notifier.send(f"🚀 信号跟单已启动（监听账号 {me.first_name}{version}）\n"
                        f"实盘总开关：{'开' if cfg.live_trading else '关，全部模拟'}"
                        f"｜Bitget API：{'已设置' if ex.has_keys else '未设置'}\n{modes}\n发 /help 查看命令")
    log.info("开始监听 %d 个频道", len(chans))
    try:
        await client.run_until_disconnected()
    finally:
        for t in tasks:
            t.cancel()
        await ex.close()
        await parser.close()
        await notifier.close()


def main():
    setup_logging()
    cfg = Config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "login":
        asyncio.run(cmd_login(cfg, sys.argv[2:]))
    elif cmd == "stats":
        asyncio.run(cmd_stats(cfg))
    elif cmd == "check":
        asyncio.run(cmd_check(cfg))
    elif cmd == "replay":
        asyncio.run(cmd_replay(cfg, int(sys.argv[2]) if len(sys.argv) > 2 else 30))
    elif cmd == "run":
        asyncio.run(cmd_run(cfg))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
