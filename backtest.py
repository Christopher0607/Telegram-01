"""回测：一个频道/群最近 N 天的喊单，按现在的规则（同一套 AI 识别、风控、分批止盈）跟单会是什么成绩（/backtest 命令）。

分两步：
1. 主程序里（有 Telegram 登录和 AI）：拉历史消息（群组只要群主/管理员发的）→ AI 识别（和实盘同一套提示词，图片也看）
   → 存成 data/backtest/<频道>.json
2. 另起一个进程：python backtest.py simulate 任务.json 结果.json
   用交易所的历史 K 线和一个假时钟驱动 engine 的模拟盘代码，把消息按时间一条条喂进去。
   放在单独的进程里，是因为假时钟要替换 engine 模块里的 time，绝不能影响正在跑的实盘。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import itertools
import json
import logging
import os
import re
import sqlite3
import sys
import time
import types
import zlib
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from config import BASE_DIR, DATA_DIR, Config, entry_rule, exit_rules, follows_reduce, rules_code, rules_line

log = logging.getLogger("backtest")
MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
GATE_1M_KEEP = 9_600 * MIN   # Gate 只给最近 1 万根 1 分钟 K 线（约 6.9 天）；留点余量，更早的换别的交易所的行情
BT_DIR = os.path.join(DATA_DIR, "backtest")
SIDE_CN = {"long": "多", "short": "空"}
_running: set[str] = set()
CACHE_KEEP_DAYS = 200   # 缓存（AI 识别结果、历史 K 线）只留最近 200 天的：回测最多 90 天，留点余量
PARSE_WORKERS = 8       # 同时识别几条消息
PREFETCH_H = 3          # 模拟前先把每个开仓信号之后 3 小时的 1 分钟 K 线一起下载（同时下几个，比边模拟边下载快；更久的模拟时成批下）
MEM_BLOCKS = 3000       # 内存里最多留多少段 K 线（每段 99～200 根），多了的从缓存文件里读，省内存
PROGRESS_EVERY = 300    # 模拟时最多每 5 分钟发一次进度（模拟进程往标准输出写「PROGRESS 说明」）


def cache_db() -> sqlite3.Connection:
    """回测缓存（data/backtest/cache.db）：同一条消息的 AI 识别结果、已经过去的历史 K 线都不会变，
    记下来下次直接用：再回测同一个频道（换天数、换规则、几个频道一起）就不用重新识别、重新下载。"""
    os.makedirs(BT_DIR, exist_ok=True)
    c = sqlite3.connect(os.path.join(BT_DIR, "cache.db"), timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("CREATE TABLE IF NOT EXISTS parsed(k TEXT PRIMARY KEY, v TEXT, ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS klines(k TEXT PRIMARY KEY, v BLOB, ts INTEGER)")
    return c


def prune_cache(c: sqlite3.Connection):
    cut = int(time.time()) - CACHE_KEEP_DAYS * 86400
    c.execute("DELETE FROM parsed WHERE ts < ?", (cut,))
    c.execute("DELETE FROM klines WHERE ts < ?", (cut,))
    c.commit()


def parser_version(cfg, parser) -> str:
    """AI 识别结果对应哪一版：提示词/识别代码（parser 所在的文件）、模型、看不看图，任何一样变了，旧的识别结果都不用。"""
    try:
        src = inspect.getsource(sys.modules[type(parser).__module__])
    except (OSError, TypeError, KeyError):
        src = type(parser).__qualname__
    llm = f"{getattr(cfg, 'llm_provider', '')}|{getattr(cfg, 'llm_model', '')}|{getattr(cfg, 'llm_vision', '')}"
    return hashlib.sha1(f"{src}|{llm}".encode()).hexdigest()[:16]


def msg_key(ver: str, ch, m) -> str:
    """一条消息（这个版本：改过就不一样）的缓存键。"""
    media = getattr(m, "photo", None) or getattr(m, "document", None)
    edit = getattr(m, "edit_date", None)
    raw = f"{ver}|{ch.username}|{getattr(m, 'chat_id', '')}|{m.id}|{int(edit.timestamp()) if edit else 0}|" \
          f"{getattr(media, 'id', '')}|{getattr(m, 'reply_to_msg_id', None)}|{m.raw_text or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()


# ====================================================================
# 第 1 步：拉消息 + AI 识别（在主程序里跑）
# ====================================================================
async def collect(client, ch, days: int, keep, build_ctx, parser, vision: bool, progress=None, cfg=None) -> list[dict]:
    """最近 days 天、keep(msg) 通过的消息（群里普通成员的发言不要），逐条 AI 识别。按时间顺序返回。"""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    total, kept = 0, []
    async for m in client.iter_messages(ch.entity, offset_date=start, reverse=True):   # 边拉边筛，大群也不占内存
        total += 1
        if await keep(m):
            kept.append(m)
    db = cache_db()
    try:
        ver = parser_version(cfg, parser)
        keys = {m.id: msg_key(ver, ch, m) for m in kept}
        hit = {}
        for mid, k in keys.items():
            r = db.execute("SELECT v FROM parsed WHERE k=?", (k,)).fetchone()
            if r:
                hit[mid] = json.loads(r[0])
        if progress:
            await progress(f"📥 拉到 {total} 条消息，其中要识别的 {len(kept)} 条"
                           + (f"（{len(hit)} 条以前识别过，直接用）" if hit else "")
                           + ("，AI 正在逐条识别……" if len(hit) < len(kept) else "，不用再识别"))
        sem = asyncio.Semaphore(PARSE_WORKERS)
        new = []

        async def one(m):
            if m.id in hit:
                return hit[m.id]
            async with sem:
                try:
                    ctx = await build_ctx(ch, m, False, vision)
                except Exception as e:
                    log.warning("回测：读取消息 %s 失败：%s", m.id, e)
                    return None
                if not ctx:
                    return None
                try:
                    parsed = await parser.parse(ctx)
                except Exception as e:
                    parsed = {"error": str(e)[:200]}
                ev = {"id": m.id, "ts": m.date.timestamp(), "text": ctx.text, "reply_to": ctx.reply_to,
                      "reply_text": ctx.reply_text, "forwarded": ctx.forwarded, "has_image": bool(ctx.image),
                      "parsed": parsed}
                if "error" not in parsed:   # 识别失败的下次重新识别
                    new.append((keys[m.id], json.dumps(ev, ensure_ascii=False), int(ev["ts"])))
                return ev

        out = [e for e in await asyncio.gather(*(one(m) for m in kept)) if e]
        if new:
            db.executemany("INSERT OR REPLACE INTO parsed(k, v, ts) VALUES(?,?,?)", new)
            db.commit()
        return out
    finally:
        db.close()


async def send_long(send, text: str, limit: int = 3800, markup: dict | None = None) -> None:
    """Telegram 一条消息最多 4096 个字，长的报告按行拆成几条发（不然后面会被截掉）。markup：按钮，放在最后一条下面。"""
    part = ""
    for line in text.split("\n"):
        if part and len(part) + 1 + len(line) > limit:
            await send(part)
            part = ""
        part = f"{part}\n{line}" if part else line
    if part:
        await (send(part, markup) if markup else send(part))


async def run(client, cfg, ch, days: int, keep, build_ctx, parser_cls, send, equity: float | None = None) -> None:
    """一个频道的回测，结果用 send(文字) 发给主人。keep(msg)：这条消息要不要（群里只要群主/管理员的）。
    equity：现在的实盘权益（每单风险是固定金额时用它算收益率；查不到是 None）。"""
    await run_many(client, cfg, [ch], days, lambda c: keep, build_ctx, parser_cls, send, equity)


async def run_many(client, cfg, chs: list, days: int, keep_for, build_ctx, parser_cls, send,
                   equity: float | None = None) -> None:
    """几个频道当成一个账户一起回测（只有一个就是普通回测）：消息按时间混在一起喂给同一个模拟账户，
    共用「同时最多几单」「每天最多亏几单」，同一个币已经有单就不再开——跟实盘一样。
    keep_for(频道) → 这个频道的 keep(msg)。同一组频道同一时间只跑一个。"""
    key = "+".join(c.username for c in chs)
    name = "、".join(c.title or c.username for c in chs)
    if key in _running:
        await send(f"「{name}」的回测已经在跑了，请等它完成。")
        return
    _running.add(key)
    multi = len(chs) > 1
    try:
        end = int(time.time()) // 60 * 60 - 120
        parser = parser_cls(cfg)
        events = []
        try:
            for ch in chs:
                title = ch.title or ch.username

                async def progress(t, title=title):   # 几个频道一起时，进度前面写上是哪个频道
                    await send(t.replace("📥 ", f"📥 「{title}」", 1) if multi else t)
                evs = await collect(client, ch, days, keep_for(ch), build_ctx, parser, cfg.llm_vision, progress, cfg)
                for e in evs:
                    e["ch"] = ch.username
                events += evs
        finally:
            await parser.close()
        events.sort(key=lambda e: e["ts"])
        n_err = sum(1 for e in events if "error" in e["parsed"])
        await send(f"🧠 识别完了 {len(events)} 条（{n_err} 条识别失败），正在用历史行情一单单模拟"
                   + ("（几个频道当成一个账户一起跑，" if multi else "（")
                   + f"还会把「🧭 跟单规则」的 {len(grid_rules())} 种组合都跑一遍、挑出最好的，要多等一会儿）……")
        os.makedirs(BT_DIR, exist_ok=True)
        base = key if not multi else "combo-" + key
        job_path = os.path.join(BT_DIR, f"{base}.json")
        out_path = os.path.join(BT_DIR, f"{base}.result.json")
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump({"channel": chs[0].username, "title": name, "start": end - days * 86400, "end": end,
                       "channels": [{"username": c.username, "title": c.title or c.username} for c in chs],
                       "days": days, "equity": equity, "events": events}, f, ensure_ascii=False)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, os.path.abspath(__file__), "simulate", job_path, out_path, cwd=BASE_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        # 48 种规则组合都要跑一遍：时限比以前放宽一倍（正常远用不到，只防卡死）
        limit = 7200 * (2 if multi else 1) * (2 if days > 30 else 1)
        sent_at = time.time()

        async def watch():   # 模拟进程报的进度：最多每 5 分钟转发一次，免得等很久没消息
            nonlocal sent_at
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line.startswith("PROGRESS ") and time.time() - sent_at >= PROGRESS_EVERY:
                    sent_at = time.time()
                    await send(f"⏳ 「{name}」回测还在跑：{line[len('PROGRESS '):]}")
        try:
            _, err = await asyncio.wait_for(asyncio.gather(watch(), proc.stderr.read()), limit)
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"模拟超过 {limit // 3600} 小时还没完成")
        if proc.returncode != 0:
            tail = (err or b"").decode(errors="replace").strip().splitlines()
            raise RuntimeError(tail[-1][:300] if tail else f"模拟进程退出码 {proc.returncode}")
        with open(out_path, encoding="utf-8") as f:
            res = json.load(f)
        await send_long(send, report_text(res), markup=report_markup(res))
    except Exception as e:
        log.exception("回测失败")
        await send(f"❌ 「{name}」回测失败：{str(e)[:300]}\n请把这条消息发给 Claude Code。")
    finally:
        _running.discard(key)


# ====================================================================
# 第 2 步：用历史行情模拟（单独的进程）
# ====================================================================
def fast_history(ex) -> None:
    """WEEX 的历史 K 线：ccxt 按 25 的额度算（实际只算 5），调快到每 0.15 秒一个，还远低于每 10 秒 500 的上限。
    放行速度要连 throttler 一起改：ccxt 只在建立交易所对象时按 rateLimit 算一次（以前只改 rateLimit 没用，一直是 0.5 秒一个）。"""
    ex.rateLimit = 6
    ex.throttler.config["refillRate"] = 1 / 6


def history_exchange(cfg, sim: dict):
    """交易所接口的「历史版」：市场信息、杠杆档位用真实接口；价格、K 线取假时钟当时的历史数据（不偷看未来）。"""
    from exchange import Exchange

    class HistEx(Exchange):
        def __init__(self):
            super().__init__(cfg)
            if self.name == "weex":
                fast_history(self.ex)
            self._blocks: OrderedDict = OrderedDict()   # 最近用过的几段 K 线（内存里），更早用过的在缓存文件里
            self._db = cache_db()
            self._sem = asyncio.Semaphore(4)
            self._old = None            # 更早的 1 分钟 K 线从哪个交易所拿（见 _source）
            self._old_lock = asyncio.Lock()
            self.old_used = ""          # 用过的话记下交易所名字，报告里说明

        async def _source(self, symbol: str, tf: str, start: int):
            """这段 K 线从哪个交易所拿。Gate 只保留最近 1 万根 K 线（1 分钟的约 7 天），更早的 1 分钟 K 线改用 WEEX 的
            公开行情（同一个币两边价格一般只差 0.1% 左右）。WEEX 没有这个合约就返回 None（这段没有行情）。"""
            if self.name != "gate" or tf != "1m" or start >= self._now_real() - GATE_1M_KEEP:
                return self
            async with self._old_lock:
                if self._old is None:
                    self._old = Exchange(cfg, "weex")
                    fast_history(self._old.ex)
                    await self._old.init()
            if symbol not in (self._old.ex.markets or {}):
                return None
            self.old_used = self._old.label
            return self._old

        async def close(self):
            await super().close()
            if self._old:
                await self._old.close()
            self._db.close()

        def _now(self) -> int:
            return int(sim["now"] * 1000)

        def _window(self, tf: str) -> int:
            return (99 if self.name == "weex" else 200) * (MIN if tf == "1m" else HOUR)

        def _remember(self, key, rows: list) -> list:
            self._blocks[key] = rows
            if len(self._blocks) > MEM_BLOCKS:
                self._blocks.popitem(last=False)
            return rows

        async def _block(self, symbol: str, tf: str, start: int) -> list:
            key = (symbol, tf, start)
            if key in self._blocks:
                self._blocks.move_to_end(key)
                return self._blocks[key]
            w = self._window(tf)
            until = min(start + w - 1, self._now_real())   # WEEX 的结束时间不能是将来
            if until < start:
                return []
            src = await self._source(symbol, tf, start)
            if src is None:
                return []
            dkey = f"{src.name}|{symbol}|{tf}|{start}"
            r = self._db.execute("SELECT v FROM klines WHERE k=?", (dkey,)).fetchone()
            if r:
                return self._remember(key, json.loads(zlib.decompress(r[0])))
            async with self._sem:
                for i in range(4):
                    try:
                        if src.name == "weex":  # 普通 K 线接口不管 since，要用历史接口（一次最多 99 根，从 since 开始）
                            rows, s, step = [], start, 99 * (MIN if tf == "1m" else HOUR)
                            while s <= until:
                                rows += await src.ex.fetch_ohlcv(symbol, tf, s, 99, {"historical": True,
                                                                                      "until": min(s + step - 1, until)}) or []
                                s += step
                        else:
                            rows = await src.ex.fetch_ohlcv(symbol, tf, since=start, limit=200)
                        break
                    except Exception:
                        if i == 3:
                            raise
                        await asyncio.sleep(2 * (i + 1))
            rows = [r for r in rows or [] if start <= r[0] < start + w]
            if start + w <= self._now_real():   # 已经完全过去的时间段才存进缓存文件（以后的回测还能用）
                self._db.execute("INSERT OR REPLACE INTO klines(k, v, ts) VALUES(?,?,?)",
                                 (dkey, zlib.compress(json.dumps(rows).encode()), start // 1000))
                self._db.commit()
            # 还没走完的这一段只在这次回测里用：模拟最多到任务结束的时间，那之前的 K 线已经全了
            return self._remember(key, rows)

        @staticmethod
        def _now_real() -> int:
            return int(time.time() * 1000) - 2 * MIN

        async def bars(self, symbol: str, tf: str, a: int, b: int) -> list:
            """[a, b) 之间开盘的 K 线"""
            if b <= a:
                return []
            w = self._window(tf)
            starts = range(a // w * w, b, w)
            blocks = await asyncio.gather(*(self._block(symbol, tf, s) for s in starts))
            return [r for blk in blocks for r in blk if a <= r[0] < b]

        async def price_at(self, symbol: str, t: int) -> float | None:
            m = t // MIN * MIN
            cs = await self.bars(symbol, "1m", m - 10 * MIN, m + MIN)
            if not cs:
                return None
            c = cs[-1]
            return float(c[1]) if c[0] == m else float(c[4])   # 这一分钟的开盘价

        async def ticker(self, symbol: str) -> dict:
            now = self._now()
            hs = await self.bars(symbol, "1h", now - DAY, now - HOUR)
            cs = self._contract_size(symbol)   # Gate 的 K 线成交量是「张」，要乘合约面值换成币
            vol = sum(float(c[5]) * cs * float(c[4]) for c in hs) * 24 / max(len(hs), 1) if hs else None
            return {"last": await self.price_at(symbol, now), "quoteVolume": vol}

        async def last_price(self, symbol: str) -> float | None:
            return await self.price_at(symbol, self._now())

        async def quote(self, symbol: str):
            return None   # 没有历史盘口：回测不查买卖价差

        async def last_prices(self, symbols: list) -> dict:
            return {s: await self.last_price(s) for s in symbols}

        async def candles(self, symbol: str, timeframe: str = "1h", limit: int = 40) -> list:
            now = self._now()
            return (await self.bars(symbol, "1h", now - (limit + 1) * HOUR, now + 1))[-limit:]

        async def ohlcv(self, symbol: str, since_ms: int) -> list:
            return await self.bars(symbol, "1m", since_ms, self._now() // MIN * MIN)

    return HistEx()


async def prefetch(ex, events: list) -> None:
    """模拟前先把要用的 K 线一起下载好（同时下 4 段；边模拟边下载一次只下一段，慢好几倍）：
    每个开仓信号之后 PREFETCH_H 小时的 1 分钟 K 线、之前 42 小时的 1 小时 K 线（算波动率止损、24 小时成交额用）。
    缓存文件里已经有的不用再下。更久的单子模拟时用到再下。"""
    want = set()
    for e in events:
        if "error" in e["parsed"]:
            continue
        for a in e["parsed"].get("actions") or []:
            if a.get("type") != "open" or not a.get("symbol"):
                continue
            sym = ex.resolve(a["symbol"])[0]
            if not sym:
                continue
            t = int(e["ts"] * 1000)
            for tf, lo, hi in (("1m", t - 10 * MIN, t + PREFETCH_H * HOUR), ("1h", t - 42 * HOUR, t)):
                w = ex._window(tf)
                want.update((sym, tf, s) for s in range(lo // w * w, hi, w))
    await asyncio.gather(*(ex._block(*k) for k in sorted(want)), return_exceptions=True)


HEAD_VARIANTS = (   # 报告里单独拿出来跟现在的规则逐项对比的几种做法（跟现在一样的自动跳过）
    ("A", {"profit_post_close_frac": 0.5, "stop_loss_r": 1.0}),
    ("B", {"profit_post_close_frac": 1.0, "stop_loss_r": 0.5}),
    ("A+B", {"profit_post_close_frac": 0.5, "stop_loss_r": 0.5}),
    ("C", {"breakeven_after_tp1": False}),   # 第一止盈后不推保本（现在已经不推的话，下面那个反过来对比）
    ("C", {"breakeven_after_tp1": True}),
    ("D", {"follow_reduce": True}),   # 跟频道的减仓指令（现在已经跟的话，下面那个反过来对比不跟）
    ("D", {"follow_reduce": False}),
    ("原来", {"profit_post_close_frac": 1.0, "stop_loss_r": 1.0}),
)


def rules_key(r: dict) -> tuple:
    """这套规则实际上是哪一种做法：跟现在一样、或者跟前面跑过的一样的对比就不用再跑。"""
    me, cop = bool(r.get("market_entry")), bool(r.get("close_on_profit_post"))
    frac, cut = float(r.get("profit_post_close_frac") or 1.0), float(r.get("stop_loss_r") or 1.0)
    return (me, None if me else bool(r.get("allow_limit_orders")), cop,
            (1.0 if frac >= 0.95 else round(frac, 4)) if cop else None, 1.0 if cut >= 0.999 else round(cut, 4),
            bool(r.get("breakeven_after_tp1", True)), follows_reduce(r), bool(r.get("follow_close", True)))
VARIANTS = (   # 回测时同一段行情拿来对比的几种完整做法（进场怎么进、晒盈利平不平）；跟现在一样的自动跳过
    ("进场一律市价、晒盈利就清仓、跟频道减仓（09-29～10-09 用的）",
     {"market_entry": True, "close_on_profit_post": True, "profit_post_close_frac": 1.0, "follow_reduce": True}),
    ("挂限价等回调、晒盈利就清仓",
     {"market_entry": False, "allow_limit_orders": True, "close_on_profit_post": True, "profit_post_close_frac": 1.0}),
    ("进场一律市价、晒盈利不平仓", {"market_entry": True, "close_on_profit_post": False}),
    ("挂限价等回调、晒盈利不平仓、跟频道减仓（09-29 以前、10-09～10-13 的做法）",
     {"market_entry": False, "allow_limit_orders": True, "close_on_profit_post": False, "follow_reduce": True}),
)
VARIANT_TAGS = ("E", "F", "G", "H")   # 报告里给上面几种做法的代号（「采用 F」按钮用），接在 HEAD_VARIANTS 的 A～D 后面
RULE_GRID = (   # 「⭐ 推荐」在这几项的所有组合里挑（2×3×2×2×2 = 48 种，跟「🧭 跟单规则」面板上能选的一样：main.RULE_ROWS）
    ({"market_entry": False, "allow_limit_orders": True}, {"market_entry": True}),
    ({"close_on_profit_post": False}, {"close_on_profit_post": True, "profit_post_close_frac": 1.0},
     {"close_on_profit_post": True, "profit_post_close_frac": 0.5}),
    ({"stop_loss_r": 1.0}, {"stop_loss_r": 0.5}),
    ({"breakeven_after_tp1": True}, {"breakeven_after_tp1": False}),
    ({"follow_reduce": True}, {"follow_reduce": False}),
)
REC_MIN_TRADES = 10    # 平仓不到 10 单的组合不推荐（太少，看不出好坏）
REC_MIN_GAIN_R = 2.0   # 比现在多赚不到 2R（也不到现在总 R 的 10%）：差别太小，多半是碰巧，不建议换
# 几十种组合里挑最好的，总有几种是碰巧赚得多；所以建议换的那一种还要前半段、后半段都比现在好（不是靠某几天）


def grid_rules() -> list[dict]:
    """RULE_GRID 的每一种组合 → 要改的设置。"""
    out = []
    for combo in itertools.product(*RULE_GRID):
        ov = {}
        for o in combo:
            ov.update(o)
        out.append(ov)
    return out


def recommend(rows: list, now: dict) -> dict | None:
    """「⭐ 推荐」：跑过的所有组合按收益率排（每单是固定 U、算不出收益率时按总 R）；回撤比现在深太多（最多深一半，
    至少允许深 2R）、平仓不到 10 单的不参加。建议换的（pick）：排得比现在靠前、多赚至少 REC_MIN_GAIN_R（或现在总 R 的 10%）、
    前半段和后半段都比现在好的里面最靠前的那一种；没有就是继续用现在的。
    rows 里要有 now（现在的规则），每一种要有 h1（前半段的总 R）。返回 {best（第 1 名）, pick（建议换的，没有是 None）, now,
    top（前 3）, rank（现在排第几）, n（一共跑了几种）, out（不参加排名的有几种）, gain（第 1 名多几 R）, need（至少要多几 R）}；
    现在的规则平仓不到 10 单（单子太少，挑不出来）返回 None。"""
    def score(r):
        return (r["ret"] if r.get("ret") is not None else r["total"], r["total"])
    if now["n"] < REC_MIN_TRADES:
        return None
    floor = min(now["dd"] * 1.5, now["dd"] - 2.0)
    ok = sorted((r for r in rows if r["n"] >= REC_MIN_TRADES and r["dd"] >= floor), key=score, reverse=True)
    need = max(REC_MIN_GAIN_R, 0.1 * abs(now["total"]))
    pick = next((r for r in ok if r is not now and score(r) > score(now) and r["total"] - now["total"] >= need
                 and steady(r, now)), None)
    return {"best": ok[0], "pick": pick, "now": now, "top": ok[:3], "rank": ok.index(now) + 1, "n": len(rows),
            "out": len(rows) - len(ok), "gain": ok[0]["total"] - now["total"], "need": need}


def steady(r: dict, now: dict) -> bool:
    """前半段、后半段都比现在赚得多（不是靠某几天碰巧）。"""
    return r["h1"] > now["h1"] and r["total"] - r["h1"] > now["total"] - now["h1"]


def closed_stats(rs: list) -> dict:
    """已平仓单子（按平仓顺序）的 R → 单数、胜率 %、总 R、平均 R、最大回撤 R、最长连亏、赚的单子平均 R、亏的单子平均 R。"""
    cum = peak = dd = 0.0
    streak = worst = 0
    for r in rs:
        cum += r
        peak, dd = max(peak, cum), min(dd, cum - peak)
        streak = streak + 1 if r < 0 else 0
        worst = max(worst, streak)
    n = len(rs)
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r < 0]
    return {"n": n, "win": len(wins) / n * 100 if n else 0.0, "total": sum(rs),
            "avg": sum(rs) / n if n else 0.0, "dd": dd, "streak": worst,
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0}


def money(rs: list, pct: float | None, fixed: float | None, equity: float | None, mults: list | None = None) -> dict:
    """按每单风险把一串 R（按平仓顺序）换成收益：
    每单亏总权益的 pct% → 复利算收益率；每单固定亏 fixed U → 按 U 算，知道现在的权益 equity 再换成收益率。
    mults：每一单的风险倍数（几个频道一起回测时，各频道的 risk_multiplier）；不给就都是 1。
    返回 {"ret": 收益率, "ret_dd": 最大回撤（比例，负数）, "pnl_u": 赚了多少 U, "dd_u": 最大回撤 U}，算不出来的不给。"""
    mults = mults or [1.0] * len(rs)
    if pct:
        eq = pk = 1.0
        mdd = 0.0
        for r, k in zip(rs, mults):
            eq *= 1 + pct * k / 100 * r
            pk = max(pk, eq)
            mdd = min(mdd, eq / pk - 1)
        return {"ret": eq - 1, "ret_dd": mdd}
    if not fixed:
        return {}
    cum = pk = dd_u = mdd = 0.0
    for r, k in zip(rs, mults):
        cum += r * fixed * k
        pk = max(pk, cum)
        dd_u = min(dd_u, cum - pk)
        if equity:
            mdd = min(mdd, (cum - pk) / (equity + pk))
    out = {"pnl_u": cum, "dd_u": dd_u}
    if equity:
        out.update(ret=cum / equity, ret_dd=mdd)
    return out


COPY_WINDOW_H = 24   # 别的频道 24 小时内发过同一个币、同一方向、点位几乎一样的信号，算「照抄」


def copy_check(events: list, chans: list, trades: list) -> list:
    """几个频道一起回测时，看有没有频道照抄别的频道（晚一点发同一个信号）：同一个币、同一方向、
    进场价或止损差不到 1%（或者有一个止盈差不到 0.5%），比别的频道晚发、24 小时以内。
    返回每一对（后发的 ← 先发的）：照抄几个、晚多久，照抄的单子在回测里开了几单、合计多少 R。"""
    from engine import coin_key
    title = {c["username"]: c["title"] for c in chans}
    first = chans[0]["username"]
    sig = []
    for e in events:
        for a in (e["parsed"].get("actions") or []):
            if a.get("type") == "open" and a.get("symbol") and a.get("side"):
                ent = [x for x in (a.get("entry_low"), a.get("entry_high")) if x]
                sig.append(dict(ch=e.get("ch") or first, id=e["id"], ts=e["ts"], coin=coin_key(a["symbol"]), side=a["side"],
                                entry=sum(ent) / len(ent) if ent else None, sl=a.get("stop_loss"),
                                tps=[x for x in (a.get("take_profits") or []) if x]))
    sig.sort(key=lambda s: s["ts"])

    def near(a, b, tol):
        return bool(a and b) and abs(a - b) / abs(b) <= tol

    def same(s, p):
        return (near(s["entry"], p["entry"], 0.01) or near(s["sl"], p["sl"], 0.01)
                or any(near(x, y, 0.005) for x in s["tps"] for y in p["tps"]))

    tmap = {(t.get("channel"), t.get("msg_id")): t for t in trades}
    pairs: dict = {}
    for i, s in enumerate(sig):
        src = [p for p in sig[:i] if p["ch"] != s["ch"] and p["coin"] == s["coin"] and p["side"] == s["side"]
               and s["ts"] - p["ts"] <= COPY_WINDOW_H * 3600 and same(s, p)]
        if not src:
            continue
        p = src[-1]
        d = pairs.setdefault((s["ch"], p["ch"]), {"delays": [], "opened": 0, "r": 0.0})
        d["delays"].append((s["ts"] - p["ts"]) / 60)
        t = tmap.get((s["ch"], s["id"]))
        if t and t.get("opened_at"):
            d["opened"] += 1
            d["r"] += t.get("r_mult") or 0.0
    out = []
    for (later, earlier), d in pairs.items():
        ds = sorted(d["delays"])
        out.append(dict(later=title.get(later, later), earlier=title.get(earlier, earlier), n=len(ds),
                        of=sum(1 for s in sig if s["ch"] == later), dmin=ds[0], dmed=ds[len(ds) // 2], dmax=ds[-1],
                        opened=d["opened"], r=d["r"]))
    return sorted(out, key=lambda x: -x["n"])


def skip_reason(outcome: str) -> str:
    """把跳过原因归成几类，方便统计。"""
    r = outcome[len("skip: "):]
    for keys, label in ((("已有",), "同一个币已经有单"), (("上限",), "同时持仓数到上限"),
                        (("永续合约",), "交易所没有这个币"), (("成交额",), "成交额太小"),
                        (("相差超过", "识别错误", "过大"), "价格对不上（可能识别错）"),
                        (("盈亏比",), "盈亏比不够"), (("错误一侧", "打掉"), "价格已经过了止损"),
                        (("不追",), "价格跑远了不追"), (("置信度", "不是明确"), "不是明确的开仓指令"),
                        (("没有给止损",), "没给止损"), (("最小下单量",), "仓位低于最小下单量")):
        if any(k in r for k in keys):
            return label
    return re.sub(r"[\d.]+", "", r)[:20]


async def simulate(job_path: str, out_path: str):
    import engine as E
    from db import DB

    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    sim = {"now": float(job["start"])}
    E.time = types.SimpleNamespace(time=lambda: sim["now"])   # 只在这个进程里替换 engine 的时钟
    E.now_ms = lambda: int(sim["now"] * 1000)

    cfg = Config()
    live_now = sorted(c.username for c in cfg.channels if cfg.mode_for(c) == "live")   # 现在跑实盘的频道（机器人里切过的也算）
    cfg.live_trading = False   # 全部按模拟盘跑
    cfg.paper_enabled = True   # 主人停了模拟盘也照样回测（回测本来就是模拟）
    for k in ("gate_key", "gate_secret", "weex_key", "weex_secret", "weex_passphrase",
              "bitget_key", "bitget_secret", "bitget_passphrase"):
        setattr(cfg, k, "")    # 回测进程不拿交易所密钥：只用公开行情，绝不可能真实下单
    # 几个频道一起回测：每个频道一个假的 chat_id（跟进指令只找同一个频道的单子），消息 id 按频道区分
    chans = job.get("channels") or [{"username": job["channel"], "title": job["title"]}]
    chmap = {}
    for i, c in enumerate(chans):
        cc = cfg.channel_by_username(c["username"])
        cc.title = c["title"]
        chmap[c["username"]] = (cc, -1 - i)
    first = chans[0]["username"]
    ch = chmap[first][0]
    multi = len(chans) > 1
    ex = history_exchange(cfg, sim)
    await ex.init()
    prune_cache(ex._db)
    t0 = time.time()
    await prefetch(ex, job["events"])
    log.info("回测：预先下载行情用了 %.0f 秒", time.time() - t0)
    parsed = {(e.get("ch") or first, str(e["id"])): e["parsed"] for e in job["events"]}

    class Parser:
        async def parse(self, ctx):
            r = parsed.get((ctx.channel.username, str(ctx.msg_id)))
            if r is None or "error" in r:
                raise RuntimeError((r or {}).get("error", "没有识别结果"))
            return r

    class Quiet:
        async def send(self, text, markup=None):
            return True

    base_risk = dict(cfg.risk)
    risk0 = cfg.risk_for(ch)
    unit = 1.0 if multi else ch.risk_multiplier   # 几个频道时每一单按自己频道的倍数算（mult_of）
    fixed = float(risk0.get("risk_per_trade_usdt") or 0) * unit or None
    pct = None if fixed else float(risk0["risk_per_trade_pct"]) * unit
    equity = job.get("equity")

    def mult_of(t: dict) -> float:
        return chmap[t["channel"]][0].risk_multiplier if multi and t.get("channel") in chmap else 1.0

    async def one_pass(overrides: dict) -> tuple[list, list, bool]:
        """按一套规则（在现在的风控上改 overrides 这几项）把消息从头喂一遍。返回（所有单子，每条消息的处理结果，是否挂单）。"""
        sim["now"] = float(job["start"])
        cfg.risk = dict(base_risk, **overrides)
        db = DB(":memory:")   # 放内存里：盯盘每分钟都要存一遍单子，写硬盘每次要等 1 毫秒多，几十种组合跑下来慢很多
        eng = E.Engine(cfg, db, ex, Parser(), Quiet())

        async def advance(t: float):
            # 一次推进到下一条消息：每一单按自己的 K 线逐根撮合，限价单过了有效期以后的 K 线不算（engine.monitor_paper），
            # 跟一分钟一分钟推进结果一样，快很多
            if sim["now"] < t:
                sim["now"] = t
                await eng.monitor_paper()

        try:
            for e in sorted(job["events"], key=lambda e: (e["ts"], e["id"])):
                await advance(e["ts"])
                cc, chat_id = chmap[e.get("ch") or first]
                ctx = E.MsgCtx(cc, cc.title, chat_id, e["id"], datetime.fromtimestamp(e["ts"], timezone.utc), e["text"],
                               reply_to=e["reply_to"], reply_text=e["reply_text"], forwarded=e["forwarded"])
                await eng.handle_message(ctx)
            await advance(job["end"])
            trades = [db._row(r) for r in db.conn.execute("SELECT * FROM trades ORDER BY id")]
            for t in trades:   # 还没平仓的：按结束时的价格算浮动盈亏
                if t["status"] == "open":
                    px = await ex.last_price(t["symbol"]) or t["entry_price"]
                    sign = 1 if t["side"] == "long" else -1
                    pnl = (t["realized"] or 0) + sign * (px - t["entry_price"]) * t["remaining"] \
                        - t["remaining"] * px * float(cfg.risk_for(ch)["fee_rate"])
                    t["unrealized_r"] = pnl / t["risk_usdt"] if t["risk_usdt"] else None
            outcomes = [r["outcome"] or "" for r in db.conn.execute("SELECT outcome FROM messages")]
            return trades, outcomes, eng.maker_on("paper")
        finally:
            db.conn.close()

    def stats_of(tr: list, rules: dict) -> dict:
        """一套规则跑出来的单子 → 成绩（对比、推荐都用这个）。"""
        done = sorted((t for t in tr if t["status"] == "closed" and t.get("r_mult") is not None), key=lambda t: t["closed_at"])
        rs = [t["r_mult"] for t in done]
        mid = (job["start"] + job["end"]) / 2 * 1000
        return dict(code=rules_code(rules), rules=rules_line(rules), opened=sum(1 for t in tr if t.get("opened_at")),
                    h1=sum(t["r_mult"] for t in done if t["closed_at"] < mid),
                    **closed_stats(rs), **money(rs, pct, fixed, equity, [mult_of(t) for t in done]),
                    per_channel=[dict(title=c["title"], n=sum(1 for t in done if t.get("channel") == c["username"]),
                                      total=sum(t["r_mult"] for t in done if t.get("channel") == c["username"]))
                                 for c in (chans if multi else [])])

    try:
        trades, outcomes, maker = await one_pass({})
        compare = []
        now_rules = cfg.risk_for(ch)
        tried = {rules_key(now_rules): stats_of(trades, now_rules)}   # 跑过的每一种做法的成绩：同一种做法只跑一次

        todo = {rules_key({**now_rules, **ov}) for ov in [{}] + [v[1] for v in HEAD_VARIANTS + VARIANTS] + grid_rules()}

        async def result(ov: dict) -> dict:
            rules = {**now_rules, **ov}
            key = rules_key(rules)
            if key not in tried:
                tr, _, _ = await one_pass(ov)
                tried[key] = stats_of(tr, rules)
                print(f"PROGRESS {len(todo)} 种规则组合跑完了 {len(tried)} 种", flush=True)
            return tried[key]

        seen = {rules_key(now_rules)}
        # 同一段行情、同一批识别结果，换几条规则再跑：看这几条规则到底帮了多少（效果跟现在或者前面一样的不重复跑）
        for head, tag, (label, ov) in ([(True, v[0], v) for v in HEAD_VARIANTS]
                                       + [(False, VARIANT_TAGS[i], v) for i, v in enumerate(VARIANTS)]):
            key = rules_key({**now_rules, **ov})
            if key in seen:
                continue
            seen.add(key)
            if head:
                label = f"{label}：{exit_rules({**now_rules, **ov})}"
            compare.append(dict(await result(ov), label=label, tag=tag, head=head))
        # ⭐ 推荐：「🧭 跟单规则」能选的所有组合都跑一遍（上面跑过的不重跑），挑最好的
        for ov in grid_rules():
            await result(ov)
        best = recommend(list(tried.values()), tried[rules_key(now_rules)])
        cfg.risk = base_risk
    finally:
        await ex.close()

    risk = cfg.risk_for(ch)
    pfrac = float(risk.get("profit_post_close_frac") or 1.0)
    closed = sorted((t for t in trades if t["status"] == "closed" and t.get("r_mult") is not None), key=lambda t: t["closed_at"])
    skips: dict = {}
    for o in outcomes:
        for part in o.split(" | "):
            if part.startswith("skip: "):
                k = skip_reason(part)
                skips[k] = skips.get(k, 0) + 1
    errors = [p[len("error: "):] for o in outcomes for p in o.split(" | ") if p.startswith("error: ")]
    copies = copy_check(job["events"], chans, trades) if multi else []
    per_channel = []
    for c in chans if multi else []:   # 几个频道一起时：每个频道各自的成绩
        mine = [t for t in trades if t.get("channel") == c["username"]]
        per_channel.append(dict(title=c["title"], opened=sum(1 for t in mine if t.get("opened_at")),
                                **closed_stats([t["r_mult"] for t in closed if t.get("channel") == c["username"]])))
    out = {
        "title": job["title"], "start": job["start"], "end": job["end"], "days": job["days"], "risk_pct": pct,
        "risk_usdt": fixed, "equity": equity, "multi": multi, "per_channel": per_channel, "copies": copies,
        "channels": [c["title"] for c in chans],
        "limits": [int(risk.get("max_open_positions") or 0), int(risk.get("max_daily_losses") or 0)],
        "maker": maker, "errors": len(errors), "error_sample": errors[0][:80] if errors else "",
        "old_source": ex.old_used, "compare": compare, "best": best,
        "live_match": sorted(c["username"] for c in chans) == live_now,   # 回测的正好是现在跑实盘的全部频道
        "rules_now": [entry_rule(risk),
                      ("晒盈利不平仓" if not risk.get("close_on_profit_post") else "晒盈利就清仓" if pfrac >= 0.95
                       else "晒盈利先减仓推保本")] + ([f"亏到 {float(risk['stop_loss_r']):g}R 就止损"]
                                                if float(risk.get("stop_loss_r") or 1.0) < 0.999 else [])
                     + ([] if follows_reduce(risk) else ["频道减仓不跟"]),
        "profit_frac": pfrac if risk.get("close_on_profit_post") else None,
        "exit_rules": exit_rules(risk),
        "messages": len(job["events"]),
        "signals": sum(1 for e in job["events"] if any(a.get("type") == "open" for a in (e["parsed"].get("actions") or []))),
        "parse_errors": sum(1 for e in job["events"] if "error" in e["parsed"]),
        "opened": sum(1 for t in trades if t.get("opened_at")),
        "cancelled": sum(1 for t in trades if t["status"] == "cancelled" and not t.get("opened_at")),
        "skips": sorted(skips.items(), key=lambda kv: -kv[1]),
        "filled": sum(1 for o in outcomes for p in o.split(" | ") if p.startswith("fill: 换成")),
        "closed": [{"base": t["base"], "side": t["side"], "r": t["r_mult"], "reason": t.get("exit_reason") or "",
                    "opened_at": t.get("opened_at") or t["created_at"], "closed_at": t["closed_at"],
                    "fallback": t.get("sl_source") == "fallback", "mult": mult_of(t),
                    "ch": chmap[t["channel"]][0].title if multi and t.get("channel") in chmap else ""} for t in closed],
        "open": [{"base": t["base"], "side": t["side"], "r": t.get("unrealized_r"), "opened_at": t.get("opened_at") or t["created_at"]}
                 for t in trades if t["status"] == "open"],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)


# ====================================================================
# 结果文字
# ====================================================================
def report_text(res: dict, tz_hours: float = 8) -> str:
    tz = timezone(timedelta(hours=tz_hours))
    day = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%m-%d")
    rs = [t["r"] for t in res["closed"]]
    mults = [t.get("mult", 1.0) for t in res["closed"]]
    if res.get("multi"):
        mp, ml = (res.get("limits") or [0, 0])[:2]
        lines = [f"📊 组合回测：{' + '.join(res['channels'])} 最近 {res['days']} 天（{day(res['start'])} ~ {day(res['end'])}）",
                 f"几个频道当成一个账户一起跑：共用「同时最多 {mp} 单」「每天最多亏 {ml or '不限'} 单」，同一个币已经有单就不再开"
                 f"（跟实盘一样）；1R = 一单打到止损亏的钱"]
    else:
        lines = [f"📊 回测：{res['title']} 最近 {res['days']} 天（{day(res['start'])} ~ {day(res['end'])}）",
                 "按你现在的规则：同一套 AI 识别、止损、分批止盈、同时最多几单都一样；1R = 一单打到止损亏的钱"]
    lines.append(f"• 要识别的消息 {res['messages']} 条，AI 认出开仓信号 {res['signals']} 个"
                 + (f"（{res['parse_errors']} 条识别失败）" if res["parse_errors"] else ""))
    skipped = sum(n for _, n in res["skips"])
    lines.append(f"• 实际开单 {res['opened']} 笔" + (f"，跳过 {skipped} 个：" + "、".join(f"{k} {n}" for k, n in res["skips"][:5])
                                                   if skipped else "")
                 + (f"；另有 {res['cancelled']} 张限价单没成交" if res["cancelled"] else ""))
    if res.get("filled"):
        lines.append(f"• 其中 {res['filled']} 单频道先发简短的、过一会儿才补止损止盈：已换成频道的止损止盈")
    if res.get("errors"):
        lines.append(f"• 另有 {res['errors']} 个信号没法模拟（{res.get('error_sample') or '出错'}）")
    if not rs:
        lines.append("• 这段时间没有已平仓的单子，没法统计成绩。")
    else:
        s = closed_stats(rs)
        lines.append(f"• 已平仓 {s['n']} 单：胜率 {s['win']:.0f}%，总 {s['total']:+.1f}R，平均每单 {s['avg']:+.2f}R")
        lines.append(f"• 最大回撤 {s['dd']:.1f}R，最长连亏 {s['streak']} 单；最好一单 {max(rs):+.1f}R，最差一单 {min(rs):+.1f}R")
        fb = [t["r"] for t in res["closed"] if t["fallback"]]
        if fb:
            lines.append(f"• 其中信号没给止损、程序补止损的 {len(fb)} 单：总 {sum(fb):+.1f}R")
        m = money(rs, res.get("risk_pct"), res.get("risk_usdt"), res.get("equity"), mults)
        if res.get("risk_pct"):
            lines.append(f"💰 收益率：按每单亏总权益 {res['risk_pct']:g}% 算（复利），这段时间 {m['ret'] * 100:+.0f}%，"
                         f"中途最多回撤 {m['ret_dd'] * 100:.0f}%")
        elif res.get("risk_usdt"):
            u = res["risk_usdt"]
            if m.get("ret") is not None:
                lines.append(f"💰 收益率：按每单固定亏 {u:g}U、现在权益 {res['equity']:.0f}U 算，这段时间 {m['ret'] * 100:+.0f}%"
                             f"（{m['pnl_u']:+.0f}U），中途最多回撤 {m['ret_dd'] * 100:.0f}%（{m['dd_u']:.0f}U）")
            else:
                lines.append(f"💰 按每单固定亏 {u:g}U 算：这段时间 {m['pnl_u']:+.0f}U，中途最多回撤 {m['dd_u']:.0f}U"
                             f"（查不到实盘权益，没法换算成百分比）")
    if res["open"]:
        ur = [t["r"] for t in res["open"] if t["r"] is not None]
        lines.append(f"• 还没平仓 {len(res['open'])} 单，按现价浮动 {sum(ur):+.1f}R（没算进上面的成绩）")
    if res.get("per_channel"):
        lines.append("\n各频道（在这个组合里实际开到的单子）：")
        for c in res["per_channel"]:
            lines.append(f"• {c['title']}：开 {c['opened']} 单｜平仓 {c['n']} 单｜胜率 {c['win']:.0f}%｜{c['total']:+.1f}R")
    if res.get("multi"):
        lines.append(f"\n🔍 有没有频道照抄别的频道（同一个币、同一方向、点位几乎一样，比别的频道晚发，{COPY_WINDOW_H} 小时内）：")
        mins = lambda m: "不到 1 分钟" if m < 1 else f"{m:.0f} 分钟" if m < 120 else f"{m / 60:.1f} 小时"
        real = [c for c in res.get("copies") or [] if c["n"] >= 3 and c["n"] >= 0.1 * c["of"]]
        for c in real:
            left = c["n"] - c["opened"]
            late = mins(c["dmin"]) if c["dmin"] == c["dmax"] else f"{mins(c['dmin'])}～{mins(c['dmax'])}（中间数 {mins(c['dmed'])}）"
            lines.append(f"• {c['later']} ← {c['earlier']}：{c['later']}的 {c['of']} 个信号里 {c['n']} 个是照抄的，晚 {late}；"
                         + (f"回测里开了 {c['opened']} 单（原单已经平了才开），合计 {c['r']:+.1f}R" if c["opened"] else "回测里一单都没开")
                         + (f"，另外 {left} 个没开（多半是同一个币已经有单）" if left and c["opened"] else ""))
        few = [c for c in res.get("copies") or [] if c not in real]
        if few:
            lines.append("• 偶尔撞车（太少，多半只是碰巧）：" + "；".join(f"{c['later']} ← {c['earlier']} {c['n']} 个" for c in few))
        if not real:
            lines.append("• 没发现照抄的频道")
    if res.get("compare"):
        now = {**closed_stats(rs), **money(rs, res.get("risk_pct"), res.get("risk_usdt"), res.get("equity"), mults)}
        has_ret = now.get("ret") is not None

        def row(st: dict) -> str:
            ret = f"｜收益 {st['ret'] * 100:+.0f}%" if st.get("ret") is not None else ""
            return f"{st['total']:+.1f}R｜{st['avg']:+.2f}R｜{st['win']:.0f}%｜{st['dd']:.1f}R{ret}（平仓 {st['n']} 单）"

        def detail(st: dict) -> list:
            ret = (f"｜收益 {st['ret'] * 100:+.0f}%（回撤 {st['ret_dd'] * 100:.0f}%）" if st.get("ret") is not None
                   else f"｜{st['pnl_u']:+.0f}U" if st.get("pnl_u") is not None else "")
            return [f"   平仓 {st['n']} 单｜胜率 {st['win']:.0f}%｜总 {st['total']:+.1f}R{ret}",
                    f"   赚的单子平均 {st.get('avg_win', 0):+.2f}R｜亏的单子平均 {st.get('avg_loss', 0):+.2f}R｜"
                    f"最大回撤 {st['dd']:.1f}R｜最长连亏 {st['streak']} 单"]
        head = [c for c in res["compare"] if c.get("head")]
        if head:
            lines.append("\n🆚 现在的规则 vs 新方案（同一段行情、同一批信号，只差「怎么平仓、止损怎么放」）：")
            lines.append(f"• 现在：{res.get('exit_rules') or '晒盈利全部平掉｜打到信号止损才平｜第一止盈后推保本｜跟频道减仓'}")
            lines += detail(now)
            for c in head:
                lines.append(f"• {c['label']}")
                lines += detail(c)
                if c.get("per_channel") and res.get("per_channel"):
                    was = {x["title"]: x["total"] for x in res["per_channel"]}
                    lines.append("   各频道：" + "；".join(f"{x['title']} {was.get(x['title'], 0):+.1f}R → {x['total']:+.1f}R"
                                                      for x in c["per_channel"]))
        rest = [c for c in res["compare"] if not c.get("head")]
        if rest:
            lines.append(f"\n🔁 其他规则对比（总 R｜平均每单｜胜率｜最大回撤{'｜收益率' if has_ret else ''}）：")
            lines.append(f"• 现在的规则（{'、'.join(res.get('rules_now') or [])}）：{row(now)}")
            for c in rest:
                lines.append(f"• {c['tag'] + ' ' if c.get('tag') else ''}{c['label']}：{row(c)}")
    if res["closed"]:
        lines.append("\n最近的单子：")
        for t in res["closed"][-12:]:
            icon = "✅" if t["r"] > 0 else "🔴"
            src = f"{t['ch'][:6]}｜" if t.get("ch") else ""
            lines.append(f"{icon} {datetime.fromtimestamp(t['opened_at'] / 1000, tz).strftime('%m-%d %H:%M')} "
                         f"{src}{t['base']} {SIDE_CN.get(t['side'], '')} {t['r']:+.2f}R｜{t['reason']}")
    if res.get("best"):
        lines += recommend_lines(res)
    fee = ("开仓、止盈按挂单手续费（假设挂单都能成交），止损按市价手续费" if res.get("maker")
           else "手续费按市价算")
    old = (f"Gate 只留最近约 7 天的 1 分钟 K 线，更早的用 {res['old_source']} 的行情（同一个币两边价格一般只差 0.1% 左右）；"
           if res.get("old_source") else "")
    lines.append("\n说明：用 1 分钟 K 线模拟，进场按信号那一分钟的价格，同一根 K 线碰到止损和止盈按止损算；"
                 f"{old}{fee}；没算滑点和资金费。历史表现不代表以后。")
    if star_pick(res):
        lines.append("\n👇 想换成推荐的：点「⭐ 采用推荐」；想换成上面别的：点「采用 X」（都会先问你一次），"
                     "或者点「🧭 跟单规则」自己一项项改。")
    elif report_markup(res):
        lines.append("\n👇 想换成上面哪一种：点「采用 X」（会先问你一次），或者点「🧭 跟单规则」自己一项项改。")
    return "\n".join(lines)


def star_pick(res: dict) -> dict | None:
    """「⭐ 采用推荐」按钮换成的那一种：只在有建议换的、而且回测的正好是现在跑实盘的全部频道时给
    （规则是所有频道一起用的，只看一个频道挑出来的不能直接拿去改全部）。"""
    rec = res.get("best")
    return rec["pick"] if rec and rec.get("pick") and res.get("live_match") else None


def recommend_lines(res: dict) -> list[str]:
    """报告最后的「⭐ 推荐」：所有组合里的前 3 名、现在的排第几、建议换不换。"""
    rec = res["best"]
    best, now, pick = rec["best"], rec["now"], rec.get("pick")

    def stat(st: dict) -> str:
        ret = f"｜收益 {st['ret'] * 100:+.0f}%" if st.get("ret") is not None else ""
        return f"{st['total']:+.1f}R｜胜率 {st['win']:.0f}%｜最大回撤 {st['dd']:.1f}R{ret}（平仓 {st['n']} 单）"
    lines = [f"\n⭐ 推荐：「🧭 跟单规则」能选的 {rec['n']} 种组合各跑一遍，挑收益最高、回撤不比现在深太多的"]
    for i, st in enumerate(rec["top"]):
        mark = "（现在的）" if st["code"] == now["code"] else "（推荐）" if pick and st["code"] == pick["code"] else ""
        lines.append(f"{i + 1}. {st['rules']}{mark}")
        lines.append(f"   {stat(st)}")
    rank = "" if rec["rank"] == 1 else (f"现在的规则排第 {rec['rank']}（共 {rec['n']} 种"
                                         + (f"，回撤太深、单子太少的 {rec['out']} 种不算）。" if rec["out"] else "）。"))
    if best["code"] == now["code"]:
        lines.append("👉 现在的规则就是第 1 名，继续用就行。")
    elif not pick:
        why = (f"只比现在多 {rec['gain']:+.1f}R，差别太小" if rec["gain"] < rec["need"]
               else f"虽然多 {rec['gain']:+.1f}R，但{'前半段' if best['h1'] <= now['h1'] else '后半段'}不如现在，靠的是某几天")
        lines.append(f"👉 第 1 名{why}，多半是碰巧：继续用现在的就行。{rank}")
    else:
        top = next((i + 1 for i, st in enumerate(rec["top"]) if st["code"] == pick["code"]), None)
        name = f"第 {top} 名" if top else f"「{pick['rules']}」"
        gain = (f"比现在多 {pick['total'] - now['total']:+.1f}R，前半段、后半段都比现在好，"
                f"最大回撤 {now['dd']:.1f}R → {pick['dd']:.1f}R")
        if res.get("live_match"):
            lines.append(f"👉 建议换成{name}：{gain}。{rank}点下面「⭐ 采用推荐」（会先问你一次）。"
                         f"{rec['n']} 种里挑最好的难免有碰巧的成分，选 90 天回测更可靠。")
        else:
            lines.append(f"👉 对这{'几个频道' if res.get('multi') else '个频道'}来说{name}更好：{gain}。{rank}")
    if not res.get("live_match"):
        lines.append(f"（跟单规则是所有频道一起用的，这份只看了「{'、'.join(res.get('channels') or [res.get('title', '')])}」。"
                     "要换规则，最好发 /backtest 点「🔴 只选实盘频道」、选 90 天一起回测，看那份的推荐）")
    return lines


def report_markup(res: dict) -> dict | None:
    """回测报告下面的按钮：「⭐ 采用推荐」（值得换的时候）+ 对比过的每一种做法一个「采用」（点了先问一次，确认了才改「🧭 跟单规则」）。"""
    picks = [c for c in res.get("compare") or [] if c.get("code") and c.get("tag")]
    star = star_pick(res)
    if not picks and not star:
        return None
    btns = [{"text": f"采用 {c['tag']}", "callback_data": f"rlpick:{c['code']}:{c['tag']}"} for c in picks]
    rows = ([[{"text": "⭐ 采用推荐", "callback_data": f"rlpick:{star['code']}:⭐推荐"}]] if star else []) \
        + [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([{"text": "🧭 跟单规则（自己一项项改）", "callback_data": "rlpick:panel:-"}])
    return {"inline_keyboard": rows}


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "simulate":
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(simulate(sys.argv[2], sys.argv[3]))
    else:
        print("用法：python backtest.py simulate 任务.json 结果.json（一般由机器人的 /backtest 命令调用）")
