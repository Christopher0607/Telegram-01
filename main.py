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
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events, utils
import ccxt.async_support as ccxt
from telethon.errors import (FloodWaitError, InviteHashExpiredError, InviteHashInvalidError, InviteRequestSentError,
                             PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
                             SessionPasswordNeededError)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import (ChannelParticipantAdmin, ChannelParticipantCreator, ChannelParticipantsAdmins, Chat,
                               ChatInviteAlready, ChatParticipantAdmin, ChatParticipantCreator, MessageMediaPhoto)

from config import ChannelCfg, Config, DATA_DIR, load_private_groups, save_private_group, save_secrets
from db import DB
from engine import MODE_CN, SIDE_CN, Engine, MsgCtx, fmt
from exchange import LABELS, Exchange
from notifier import KEYBOARD, Notifier
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


def invite_hash(text: str) -> str | None:
    """从私人邀请链接里取出邀请码：t.me/+xxxx、t.me/joinchat/xxxx、tg://join?invite=xxxx"""
    m = re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([\w-]+)|invite=([\w-]+)", text or "")
    return (m.group(1) or m.group(2)) if m else None


async def open_invite(client, h: str, join: bool):
    """邀请码 → 群/频道。监听账号还没进群、并且 join=True 时，用邀请链接加入。"""
    res = await client(CheckChatInviteRequest(h))
    if isinstance(res, ChatInviteAlready):
        return res.chat
    if not join:
        raise ValueError("监听账号还不在这个群里")
    return (await client(ImportChatInviteRequest(h))).chats[0]


async def find_private(client, info: dict | None, join: bool):
    """私人群：先按 /join 时记下的 id 找（群主换了邀请链接也不影响），找不到再用邀请链接。"""
    if not info:
        raise ValueError("还没设置邀请链接（给机器人发 /join 邀请链接）")
    try:
        return await client.get_entity(info["id"])
    except Exception:
        pass
    try:
        return await open_invite(client, info["hash"], join)
    except Exception as e:
        err = e
    await client.get_dialogs()  # 刷新本地记录，再按 id 找一次
    try:
        return await client.get_entity(info["id"])
    except Exception:
        raise err


def is_group(ent) -> bool:
    """群组：成员也能发言。频道只有管理员能发。"""
    return isinstance(ent, Chat) or bool(getattr(ent, "megagroup", False) or getattr(ent, "gigagroup", False))


ADMIN_TYPES = (ChannelParticipantAdmin, ChannelParticipantCreator, ChatParticipantAdmin, ChatParticipantCreator)
ADMIN_REFRESH_SEC = 6 * 3600


async def fetch_admins(client, ent) -> set[int]:
    """群主和管理员的 id（普通小群会返回全部成员，按身份筛出来）。"""
    ids = set()
    async for u in client.iter_participants(ent, filter=ChannelParticipantsAdmins()):
        if isinstance(getattr(u, "participant", None), ADMIN_TYPES):
            ids.add(u.id)
    return ids


async def from_admin(client, ch: ChannelCfg, msg) -> bool:
    """群组里只跟群主/管理员发的消息：普通成员随口一句「BTC 市價多」不能让程序下单。频道只有管理员能发，不用查。"""
    if not ch.is_group:
        return True
    sid = msg.sender_id
    if sid is None or sid == msg.chat_id:  # 匿名管理员（以群的身份发言）
        return True
    if ch.admin_ids is None or time.time() - ch.admins_at > ADMIN_REFRESH_SEC:
        try:
            ch.admin_ids, ch.admins_at = await fetch_admins(client, ch.entity), time.time()
        except Exception as e:  # 查不到：沿用上次的名单，10 分钟后再试
            log.warning("更新「%s」的管理员名单失败：%s", ch.title, e)
            ch.admin_ids, ch.admins_at = ch.admin_ids or set(), time.time() - ADMIN_REFRESH_SEC + 600
    return sid in ch.admin_ids


async def resolve_channels(client, cfg: Config, join: bool = True) -> dict:
    """config 里的频道/群 → Telegram 实体；没加入的公开频道自动加入（收实时消息必须先加入）。
    私人群用 /join 保存在服务器上的邀请链接。返回 {peer_id: ChannelCfg}"""
    out = {}
    saved = load_private_groups()
    for ch in cfg.channels:
        if ch.mode == "off":
            continue
        try:
            ent = await (find_private(client, saved.get(ch.username), join) if ch.private
                         else client.get_entity(ch.username))
        except Exception as e:
            log.error("找不到频道/群 %s：%s", ch.username, e)
            continue
        if not ch.private and join and getattr(ent, "left", False):
            try:
                await client(JoinChannelRequest(ent))
                log.info("已加入频道 %s", getattr(ent, "title", ch.username))
            except Exception as e:
                log.warning("加入频道 @%s 失败：%s", ch.username, e)
        ch.title = getattr(ent, "title", "") or ch.username
        ch.entity, ch.is_group = ent, is_group(ent)
        out[utils.get_peer_id(ent)] = ch
    return out


IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")


def image_mime(msg) -> str | None:
    """消息里有能交给 AI 看的图片就返回它的格式：照片，或者以文件形式发的图片（不含贴纸、动图、链接预览图）。"""
    if isinstance(msg.media, MessageMediaPhoto) and msg.media.photo:
        return "image/jpeg"
    f = msg.file
    if msg.document and not msg.sticker and not msg.gif and f and f.mime_type in IMAGE_MIMES and (f.size or 0) < 5_000_000:
        return f.mime_type
    return None


async def build_ctx(ch: ChannelCfg, msg, edited: bool, vision: bool = True) -> MsgCtx | None:
    text = (msg.raw_text or "").strip()
    image, mime = None, image_mime(msg) if vision else None
    if mime:
        try:
            image = await msg.download_media(file=bytes)
        except Exception as e:
            log.warning("下载图片失败（只按文字识别）：%s", e)
    if not text and not image:
        return None  # 视频、贴纸之类，识别不了
    reply_text = None
    if msg.reply_to_msg_id:
        try:
            r = await msg.get_reply_message()
            reply_text = (r.raw_text or "").strip() if r else None
        except Exception:
            pass
    # 没有修改时间的「修改」事件（比如只是表情回应变了）按原消息处理，处理过的会自动跳过
    edited = edited and msg.edit_date is not None
    return MsgCtx(channel=ch, title=ch.title or ch.username, chat_id=msg.chat_id, msg_id=msg.id,
                  date=msg.edit_date if edited else msg.date, text=text[:3000],
                  reply_to=msg.reply_to_msg_id, reply_text=reply_text, forwarded=msg.fwd_from is not None,
                  edited=edited, version=int(msg.edit_date.timestamp()) if edited else 0, posted=msg.date,
                  image=image, image_mime=mime or "image/jpeg")


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
        head = ("🖼 " if r.get("has_image") else "") + (r["text"] or "（只有图片）").replace("\n", " ")[:50]
        lines.append(f"\n{when} {(ch.title if ch and ch.title else r['channel'])}{'（修改后）' if r['edited'] else ''}：{head}")
        parsed = json.loads(r["parsed"]) if r["parsed"] else None
        if parsed and parsed.get("image"):
            lines.append(f"  🖼 图片：{parsed['image']}")
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


# 设置交易所 API 的命令 → (交易所, 依次要填的项 = 保存到 data/secrets.env 的变量名)
KEY_COMMANDS = {
    "/gate": ("gate", ["GATE_API_KEY", "GATE_API_SECRET"]),
    "/weex": ("weex", ["WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE"]),
    "/bitget": ("bitget", ["BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"]),
}
# 真实账户下单测试（开实盘/换交易所之前做一次）：命令 → 交易所
TEST_COMMANDS = {"/gatetest": "gate", "/weextest": "weex"}


def key_parts(name: str, text: str) -> list[str]:
    """从 /gate、/weex、/bitget 消息里取出 API 各项（命令后面用空格或换行隔开）。
    Gate 的 Key、Secret 都是一长串十六进制字符：多出来的「Key:」之类的字、被截成两段的 Secret 也能认出来。"""
    parts = text.split()[1:]
    if name == "gate" and len(parts) != 2:
        tokens = re.findall(r"[0-9a-fA-F]{16,}", " ".join(parts))
        if len(tokens) >= 2:
            return [tokens[0], "".join(tokens[1:])]
    return parts


async def verify_keys(name: str, values: list[str]) -> tuple[bool, str]:
    """用这组 API 查一次合约账户余额，查得到才算有效。values = [key, secret(, passphrase)]"""
    params = {"apiKey": values[0], "secret": values[1], "enableRateLimit": True, "options": {"defaultType": "swap"}}
    if len(values) > 2:
        params["password"] = values[2]
    ex = getattr(ccxt, name)(params)
    try:
        b = await ex.fetch_balance({"type": "swap"})
        return True, f"合约账户权益 {float((b.get('USDT') or {}).get('total') or 0):.2f}U"
    except Exception as e:
        return False, str(e)[:200]
    finally:
        await ex.close()


async def exchange_selftest(ex: Exchange) -> tuple[str, bool]:
    """/gatetest、/weextest：用最小数量的 BTC（约 8U 仓位、逐仓 5 倍，手续费约 0.01U）在真实账户上实测下单流程：
    开仓 → 挂止损单、查到、撤掉 → 挂一张马上满足条件的止损单，确认它真的把整个仓位平掉 → 查到这次的盈亏。
    结束时一定清理干净。返回 (结果文字, 是否每一步都通过)。"""
    s = "BTC/USDT:USDT"
    qty = ex.min_qty(s)
    lines = [f"🧪 {ex.label} 实盘接口测试（{qty:g} BTC）"]
    if (await ex.positions()).get(s):
        return "账户里已经有 BTC 仓位，为了不干扰它，测试没有进行。", False
    since, sids, ok = int(time.time() * 1000) - 60000, [], False
    try:
        await ex.prepare(s, 5, "long")
        lines.append("✅ 设置逐仓 5 倍杠杆")
        await ex.open_market(s, "long", qty, 0)
        p = await ex.wait_position(s)
        if not p:
            lines.append("❌ 市价开仓后没查到持仓")
            return "\n".join(lines), False
        lines.append(f"✅ 市价开多 {p['size']:g} BTC @{p['entry']:g}")
        sids.append(await ex.place_sl(s, "long", p["entry"] * 0.95))
        st = await ex.sl_info(s, sids[-1])
        placed = st["open"] and st["covers"]
        lines.append(f"{'✅' if placed else '❌'} 挂止损单（-5%）：状态 {st['status']}，触发价 {st['trigger']}，{st['detail']}")
        await ex.cancel_sl(s, sids[-1])
        await asyncio.sleep(1)
        st = await ex.sl_info(s, sids[-1])
        cancelled = not st["open"]
        lines.append(f"{'✅' if cancelled else '❌'} 撤销止损单：{st['status']}")
        px = await ex.last_price(s)
        try:  # 触发价高于现价的多单止损 = 条件已经满足，应该马上触发
            sids.append(await ex.place_sl(s, "long", px * 1.002))
            wait = 15
        except Exception as e:  # 交易所不接受已满足条件的止损，就挂一张贴着现价的，等价格自然波动触发
            lines.append(f"ℹ️ 交易所不接受已越过现价的止损（{str(e)[:80]}），改挂一张贴着现价的等它触发")
            sids.append(await ex.place_sl(s, "long", px * 0.9997))
            wait = 120
        closed = False
        for _ in range(wait):
            await asyncio.sleep(1)
            if not (await ex.positions()).get(s):
                closed = True
                break
        lines.append("✅ 止损单触发后，整个仓位被平掉" if closed else f"⚠️ {wait} 秒内止损没有触发（价格没碰到），这一项没测出结果")
        ok = placed and cancelled and closed
    except Exception as e:
        lines.append(f"❌ 出错：{str(e)[:200]}")
    finally:  # 不管上面成不成功：平掉测试仓位、撤掉测试挂的止损单
        pos = (await ex.positions()).get(s)
        if pos:
            await ex.reduce_market(s, pos["side"], pos["size"])
            lines.append("（收尾：已市价平掉测试仓位）")
        for sid in sids:
            await ex.cancel_sl(s, sid)
    await asyncio.sleep(2)
    pnl, _ = await ex.closed_pnl(s, since)
    lines.append(f"测试花费（含手续费）：{pnl:+.4f}U" if pnl is not None else "测试盈亏：暂时查询不到")
    return "\n".join(lines), ok


def restart_soon():
    """3 秒后退出，Docker 自动重启并读取新设置。"""
    asyncio.get_running_loop().call_later(3, os._exit, 0)


async def join_command(text: str, cfg: Config, client, restart) -> str:
    """/join 邀请链接：跟一个私人群/频道（会员群）。链接只保存在服务器上：GitHub 仓库是公开的，不能写进 config.yaml。"""
    h = invite_hash(text)
    if not h:
        return "用法：/join 邀请链接（t.me/+ 开头的私人群链接）。公开频道请告诉 Claude Code 加到 config.yaml。"
    slots = [c for c in cfg.channels if c.private]
    if not slots:
        return "config.yaml 里还没有私人群的位置，请告诉 Claude Code 加一个。"
    words = text.split()[1:]
    slot = next((c for c in slots if c.username in words), None) or (slots[0] if len(slots) == 1 else None)
    if not slot:
        return f"有好几个私人群位置，请写明是哪个：/join 名字 邀请链接（名字：{'、'.join(c.username for c in slots)}）"
    try:
        ent = await open_invite(client, h, join=True)
    except InviteRequestSentError:
        return "📨 这个群要管理员批准才能进，已经用监听账号提交了入群申请。批准以后再发一次 /join 邀请链接。"
    except (InviteHashExpiredError, InviteHashInvalidError):
        return "❌ 这个邀请链接已经失效，请向群主要一个新的。"
    except FloodWaitError as e:
        return f"⏳ Telegram 要求等 {e.seconds // 60 + 1} 分钟再试。"
    except Exception as e:
        return f"❌ 进群失败：{str(e)[:200]}"
    title = getattr(ent, "title", "") or slot.username
    save_private_group(slot.username, {"hash": h, "id": utils.get_peer_id(ent), "title": title})
    restart()
    kind = "群组，只跟群主/管理员发的消息" if is_group(ent) else "频道"
    return (f"✅ 已连上「{title}」（{kind}）。邀请链接只保存在服务器上。\n"
            f"3 秒后自动重启开始监听，现在是{MODE_CN.get(cfg.mode_for(slot), '关闭')}。")


async def admin_command(text: str, msg: dict, cfg: Config, notifier: Notifier, engine: Engine,
                        client=None, restart=restart_soon) -> str | None:
    """机器人命令入口：/ai、/ip、/join、/gate、/weex、/bitget、/gatetest、/weextest 在这里处理，其余交给 engine。"""
    cmd = text.split()[0].lower().split("@")[0]
    if cmd == "/join":
        return await join_command(text, cfg, client, restart)
    if cmd == "/ai":
        arg = text.split()[1] if len(text.split()) > 1 else ""
        return ai_text(engine.db, cfg, max(1, min(int(arg) if arg.isdigit() else 10, 20)))
    if cmd == "/ip":
        return (f"🌐 服务器 IP：{cfg.server_ip or '未知（请在 DigitalOcean 后台查看）'}\n"
                f"在交易所创建 API 时，IP 白名单填这个。")
    if cmd in TEST_COMMANDS:
        # 测试的交易所不是现在用的那个也能测（换交易所之前先测）：临时连一个
        name = TEST_COMMANDS[cmd]
        temp = engine.ex.name != name
        ex = Exchange(cfg, name) if temp else engine.ex
        try:
            if not ex.has_keys:
                return f"还没设置 {LABELS[name]} API：先发 /{name} 设置，再发 {cmd}。"
            await notifier.send("🧪 开始测试，大约 30 秒～2 分钟……")
            if temp:  # 另一个交易所：和程序正在用的账户互不影响，不用暂停盯盘
                await ex.init()
                text, passed = await exchange_selftest(ex)
            else:
                async with engine.lock:
                    text, passed = await exchange_selftest(ex)
            label = LABELS[name]
            if passed:  # 记下来：这个交易所的下单流程验证过了，可以真实下单
                engine.db.kv_set(f"selftest_ok:{name}", int(time.time()))
                return text + (f"\n✅ 全部通过：{label} 可以实盘下单了" if not temp else f"\n✅ 全部通过：换成 {label} 后就能实盘下单")
            if "❌" in text:  # 真出错了（不是价格没碰到这种）：在重新测通过之前不在这个交易所真实下单
                engine.db.kv_set(f"selftest_ok:{name}", None)
            return text + f"\n这次没有全部通过，{label} 暂时不会真实下单。请把这条消息截图发给 Claude Code。"
        finally:
            if temp:
                await ex.close()
    if cmd in KEY_COMMANDS:
        await notifier.delete(msg)
        name, env_names = KEY_COMMANDS[cmd]
        label = LABELS[name]
        parts = key_parts(name, text)
        if len(parts) != len(env_names):
            usage = " ".join(["API_KEY", "SECRET", "PASSPHRASE"][:len(env_names)])
            return f"用法：{cmd} {usage}（各项之间用空格隔开）。你刚才那条消息我已经删除。"
        ok, info = await verify_keys(name, parts)
        if not ok:
            return (f"❌ 这组 {label} API 验证失败：{info}\n请检查：IP 白名单是否填了 {cfg.server_ip or '服务器 IP'}、"
                    f"是否勾了合约交易{'、passphrase 是否正确' if name == 'bitget' else ''}。你的消息已删除，没有保存。")
        save_secrets(dict(zip(env_names, parts)))
        restart()  # 3 秒后退出，Docker 自动重启并读取新密钥
        test = next((c for c, n in TEST_COMMANDS.items() if n == name), None)
        note = "" if name == engine.ex.name else (
            f"\n现在用的交易所还是 {engine.ex.label}。" + (f"重启后发 {test} 在 {label} 真实账户上测试下单和止损，"
                                                            f"把结果截图发给 Claude Code，再切换交易所。" if test else ""))
        return (f"✅ {label} API 验证通过（{info}），已保存，3 秒后自动重启生效。你的消息已删除。"
                + ("" if cfg.live_trading else "\n现在仍然是模拟盘；要开实盘，请告诉 Claude Code。") + note)
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
        print(f"✅ {ex.label} 行情接口正常（{len(ex.ex.markets)} 个市场）")
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
            print(f"ℹ️ 没填 {ex.label} API Key：只能跑模拟盘")
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
            print(f"   {'✅' if ok else '❌'} {ch.username if ch.private else '@' + ch.username} {ch.title}｜模式：{cfg.mode_for(ch)}")
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
            msgs = [m async for m in client.iter_messages(ch.entity, limit=n)]
            print(f"\n===== {ch.title}（{ch.username}）最近 {len(msgs)} 条 =====")
            n_open = n_ok = 0
            for m in reversed(msgs):
                if not await from_admin(client, ch, m):
                    continue
                ctx = await build_ctx(ch, m, False, cfg.llm_vision)
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
                            problems.append(f"{ex.label} 无此合约")
                        n_ok += not problems
                        flag = f"  ✓会跟{sl_note}" if not problems else f"  ✗会跳过：{'、'.join(problems)}"
                    print(f"   → {describe(a)}{flag}")
            print(f"\n小结：{n_open} 个开仓信号，其中 {n_ok} 个符合跟单条件（还要再过价格/盈亏比等实时检查）")
    finally:
        await parser.close()
        await ex.close()
        await client.disconnect()


def sources_text(cfg: Config, chans: dict) -> str:
    """启动消息里的频道列表：每个频道/群是实盘还是模拟；没连上的也列出来，免得以为在跟。"""
    lines = [f"• {c.title}{'（群组：只跟群主/管理员）' if c.is_group else ''}：{MODE_CN[cfg.mode_for(c)]}"
             for c in chans.values()]
    for c in cfg.channels:
        if c.mode != "off" and not any(c is x for x in chans.values()):
            lines.append(f"• {c.username}：❌ 没连上" + ("（给我发 /join 邀请链接）" if c.private else "（请告诉 Claude Code）"))
    return "\n".join(lines)


async def init_exchange(ex: Exchange, notifier: Notifier):
    """加载交易所市场信息。失败就在机器人里提醒一次，之后每分钟重试（不让程序崩溃后反复重启、你却收不到任何消息）。"""
    warned = False
    while True:
        try:
            await ex.init()
            if warned:
                await notifier.send(f"✅ 已连上 {ex.label}")
            return
        except Exception as e:
            log.exception("连接 %s 失败", ex.label)
            if not warned:
                warned = True
                await notifier.send(f"⚠️ 连不上 {ex.label}：{str(e)[:200]}\n我会每分钟自动重试。"
                                    f"如果一直收不到「✅ 已连上 {ex.label}」，请把这条消息发给 Claude Code。")
            await asyncio.sleep(60)


async def cmd_run(cfg: Config):
    db = DB(os.path.join(DATA_DIR, "trader.db"))
    ex = Exchange(cfg)
    if cfg.live_trading and not ex.has_keys:
        log.error("live_trading 已打开但没填 %s API Key → 本次全部按模拟盘运行", ex.label)
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
    await init_exchange(ex, notifier)  # 放在绑定和登录之后：连不上交易所时也能通过机器人告诉你
    parser = SignalParser(cfg)
    engine = Engine(cfg, db, ex, parser, notifier)
    await engine.adopt_exchange()  # 换了交易所：旧交易所的实盘单提醒主人处理
    chans = await resolve_channels(client, cfg, join=True)
    if not chans:
        sys.exit("❌ 没有可监听的频道，检查 config.yaml")
    for ch in chans.values():
        if ch.is_group:  # 群组：先拿到管理员名单，拿不到要让主人知道（否则群里的喊单会全部被忽略）
            try:
                ch.admin_ids, ch.admins_at = await fetch_admins(client, ch.entity), time.time()
            except Exception as e:
                ch.admin_ids, ch.admins_at = set(), time.time() - ADMIN_REFRESH_SEC + 600
                await notifier.send(f"⚠️ 查不到「{ch.title}」的群主/管理员名单：{str(e)[:150]}\n"
                                    f"为了安全，这个群里只跟匿名管理员的消息，10 分钟后自动重试。请把这条消息发给 Claude Code。")

    # 每个频道一个队列：同一频道的消息严格按顺序处理，不同频道互不阻塞
    queues: dict[int, asyncio.Queue] = {}
    tasks: list[asyncio.Task] = []

    async def worker(q: asyncio.Queue):
        while True:
            ch, msg, edited = await q.get()
            try:
                if not await from_admin(client, ch, msg):
                    continue  # 群里普通成员的发言
                ctx = await build_ctx(ch, msg, edited, cfg.llm_vision)
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
        return await admin_command(text, msg, cfg, notifier, engine, client)

    tasks.append(asyncio.create_task(engine.monitor_forever()))
    tasks.append(asyncio.create_task(notifier.command_loop(on_command)))

    modes = sources_text(cfg, chans)
    block = engine.live_block() if cfg.live_trading else None
    if block:
        modes += f"\n⚠️ 实盘暂不下单：{block}"
    try:
        with open(os.path.join(DATA_DIR, "version.txt")) as f:
            engine.version = f.read().strip()
    except OSError:
        pass
    version = f"｜版本 {engine.version}" if engine.version else ""
    await notifier.send(f"🚀 信号跟单已启动（监听账号 {me.first_name}{version}）\n"
                        f"实盘总开关：{'开' if cfg.live_trading else '关，全部模拟'}"
                        f"｜{ex.label} API：{'已设置' if ex.has_keys else '未设置'}\n{modes}\n"
                        f"常用功能点输入框下方的按钮，全部命令发 /help", KEYBOARD)
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
