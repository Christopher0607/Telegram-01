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
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone

from config import BASE_DIR, DATA_DIR, Config

log = logging.getLogger("backtest")
MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
GATE_1M_KEEP = 9_600 * MIN   # Gate 只给最近 1 万根 1 分钟 K 线（约 6.9 天）；留点余量，更早的换别的交易所的行情
BT_DIR = os.path.join(DATA_DIR, "backtest")
SIDE_CN = {"long": "多", "short": "空"}
_running: set[str] = set()


# ====================================================================
# 第 1 步：拉消息 + AI 识别（在主程序里跑）
# ====================================================================
async def collect(client, ch, days: int, keep, build_ctx, parser, vision: bool, progress=None) -> list[dict]:
    """最近 days 天、keep(msg) 通过的消息（群里普通成员的发言不要），逐条 AI 识别。按时间顺序返回。"""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    total, kept = 0, []
    async for m in client.iter_messages(ch.entity, offset_date=start, reverse=True):   # 边拉边筛，大群也不占内存
        total += 1
        if await keep(m):
            kept.append(m)
    if progress:
        await progress(f"📥 拉到 {total} 条消息，其中要识别的 {len(kept)} 条，AI 正在逐条识别……")
    sem = asyncio.Semaphore(4)

    async def one(m):
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
            return {"id": m.id, "ts": m.date.timestamp(), "text": ctx.text, "reply_to": ctx.reply_to,
                    "reply_text": ctx.reply_text, "forwarded": ctx.forwarded, "has_image": bool(ctx.image),
                    "parsed": parsed}

    return [e for e in await asyncio.gather(*(one(m) for m in kept)) if e]


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
                evs = await collect(client, ch, days, keep_for(ch), build_ctx, parser, cfg.llm_vision, progress)
                for e in evs:
                    e["ch"] = ch.username
                events += evs
        finally:
            await parser.close()
        events.sort(key=lambda e: e["ts"])
        n_err = sum(1 for e in events if "error" in e["parsed"])
        await send(f"🧠 识别完了 {len(events)} 条（{n_err} 条识别失败），正在用历史行情一单单模拟"
                   + ("（几个频道当成一个账户一起跑，" if multi else "（") + "还会换成新方案和以前的规则各跑一遍做对比）……")
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
        limit = 3600 * (2 if multi else 1)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), limit)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"模拟超过 {limit // 3600} 小时还没完成")
        if proc.returncode != 0:
            tail = (err or b"").decode(errors="replace").strip().splitlines()
            raise RuntimeError(tail[-1][:300] if tail else f"模拟进程退出码 {proc.returncode}")
        with open(out_path, encoding="utf-8") as f:
            await send(report_text(json.load(f)))
    except Exception as e:
        log.exception("回测失败")
        await send(f"❌ 「{name}」回测失败：{str(e)[:300]}\n请把这条消息发给 Claude Code。")
    finally:
        _running.discard(key)


# ====================================================================
# 第 2 步：用历史行情模拟（单独的进程）
# ====================================================================
def history_exchange(cfg, sim: dict):
    """交易所接口的「历史版」：市场信息、杠杆档位用真实接口；价格、K 线取假时钟当时的历史数据（不偷看未来）。"""
    from exchange import Exchange

    class HistEx(Exchange):
        def __init__(self):
            super().__init__(cfg)
            if self.name == "weex":
                self.ex.rateLimit = 6   # ccxt 按 25 的额度算历史 K 线（实际只算 5），调快一点，还远低于每 10 秒 500 的上限
            self._blocks: dict = {}
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
                    self._old.ex.rateLimit = 6
                    await self._old.init()
            if symbol not in (self._old.ex.markets or {}):
                return None
            self.old_used = self._old.label
            return self._old

        async def close(self):
            await super().close()
            if self._old:
                await self._old.close()

        def _now(self) -> int:
            return int(sim["now"] * 1000)

        def _window(self, tf: str) -> int:
            return (99 if self.name == "weex" else 200) * (MIN if tf == "1m" else HOUR)

        async def _block(self, symbol: str, tf: str, start: int) -> list:
            key = (symbol, tf, start)
            if key in self._blocks:
                return self._blocks[key]
            w = self._window(tf)
            until = min(start + w - 1, self._now_real())   # WEEX 的结束时间不能是将来
            if until < start:
                return []
            src = await self._source(symbol, tf, start)
            if src is None:
                return []
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
            if start + w <= self._now_real():   # 已经完全过去的时间段才缓存
                self._blocks[key] = rows
            return rows

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


HEAD_VARIANTS = (   # 报告里单独拿出来跟现在的规则逐项对比的方案（跟现在一样的那个自动跳过）
    ("新方案：晒盈利先平一半、剩下推保本", {"profit_post_close_frac": 0.5}),
    ("晒盈利就全部平掉", {"profit_post_close_frac": 1.0}),
)
VARIANTS = (   # 回测时同一段行情拿来对比的规则（在现在的规则上改这几项）
    ("只关掉「晒盈利就清仓」", {"close_on_profit_post": False}),
    ("只关掉「进场一律市价」（挂限价等回调）", {"market_entry": False, "allow_limit_orders": True}),
    ("以前的做法：进场挂限价等回调、晒盈利不清仓", {"market_entry": False, "allow_limit_orders": True, "close_on_profit_post": False}),
)


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
        tmp = tempfile.mkdtemp()
        db = DB(os.path.join(tmp, "bt.db"))
        eng = E.Engine(cfg, db, ex, Parser(), Quiet())

        async def advance(t: float):
            # 有限价挂单时一分钟一分钟推进（挂单超时要按时撤）；只有持仓时一次推进到位（逐根 K 线撮合，结果一样）
            while sim["now"] < t:
                pending = any(x["status"] == "pending" for x in db.active_trades("paper"))
                sim["now"] = min(t, sim["now"] + 60) if pending else t
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
            shutil.rmtree(tmp, ignore_errors=True)

    try:
        trades, outcomes, maker = await one_pass({})
        compare = []
        now_rules = cfg.risk_for(ch)
        if now_rules.get("market_entry") or now_rules.get("close_on_profit_post"):
            # 同一段行情、同一批识别结果，换几条规则再跑：看这几条规则到底帮了多少
            for head, (label, ov) in [(True, v) for v in HEAD_VARIANTS] + [(False, v) for v in VARIANTS]:
                if all(now_rules.get(k) == v for k, v in ov.items()):
                    continue
                if set(ov) == {"profit_post_close_frac"} and not now_rules.get("close_on_profit_post"):
                    continue   # 晒盈利本来就不平仓：平一半还是全平都一样
                tr, _, _ = await one_pass(ov)
                done = sorted((t for t in tr if t["status"] == "closed" and t.get("r_mult") is not None), key=lambda t: t["closed_at"])
                rs = [t["r_mult"] for t in done]
                compare.append(dict(label=label, head=head, opened=sum(1 for t in tr if t.get("opened_at")),
                                    **closed_stats(rs), **money(rs, pct, fixed, equity, [mult_of(t) for t in done]),
                                    per_channel=[dict(title=c["title"], n=sum(1 for t in done if t.get("channel") == c["username"]),
                                                      total=sum(t["r_mult"] for t in done if t.get("channel") == c["username"]))
                                                 for c in (chans if multi and head else [])]))
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
    per_channel = []
    for c in chans if multi else []:   # 几个频道一起时：每个频道各自的成绩
        mine = [t for t in trades if t.get("channel") == c["username"]]
        per_channel.append(dict(title=c["title"], opened=sum(1 for t in mine if t.get("opened_at")),
                                **closed_stats([t["r_mult"] for t in closed if t.get("channel") == c["username"]])))
    out = {
        "title": job["title"], "start": job["start"], "end": job["end"], "days": job["days"], "risk_pct": pct,
        "risk_usdt": fixed, "equity": equity, "multi": multi, "per_channel": per_channel,
        "channels": [c["title"] for c in chans],
        "limits": [int(risk.get("max_open_positions") or 0), int(risk.get("max_daily_losses") or 0)],
        "maker": maker, "errors": len(errors), "error_sample": errors[0][:80] if errors else "",
        "old_source": ex.old_used, "compare": compare,
        "rules_now": [label for key, label in (("market_entry", "进场一律市价"),
                                               ("close_on_profit_post", "晒盈利就清仓" if pfrac >= 0.95 else "晒盈利先减仓推保本"))
                      if risk.get(key)],
        "profit_frac": pfrac if risk.get("close_on_profit_post") else None,
        "messages": len(job["events"]),
        "signals": sum(1 for e in job["events"] if any(a.get("type") == "open" for a in (e["parsed"].get("actions") or []))),
        "parse_errors": sum(1 for e in job["events"] if "error" in e["parsed"]),
        "opened": sum(1 for t in trades if t.get("opened_at")),
        "cancelled": sum(1 for t in trades if t["status"] == "cancelled" and not t.get("opened_at")),
        "skips": sorted(skips.items(), key=lambda kv: -kv[1]),
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
            pf = res.get("profit_frac") or 1.0
            lines.append("\n🆚 现在的规则 vs 新方案（同一段行情、同一批信号，只差「频道晒盈利时怎么平」）：")
            lines.append(f"• 现在：{'晒盈利就全部平掉' if pf >= 0.95 else '晒盈利先平一半、剩下推保本'}")
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
                lines.append(f"• {c['label']}：{row(c)}")
    if res["closed"]:
        lines.append("\n最近的单子：")
        for t in res["closed"][-12:]:
            icon = "✅" if t["r"] > 0 else "🔴"
            src = f"{t['ch'][:6]}｜" if t.get("ch") else ""
            lines.append(f"{icon} {datetime.fromtimestamp(t['opened_at'] / 1000, tz).strftime('%m-%d %H:%M')} "
                         f"{src}{t['base']} {SIDE_CN.get(t['side'], '')} {t['r']:+.2f}R｜{t['reason']}")
    fee = ("开仓、止盈按挂单手续费（假设挂单都能成交），止损按市价手续费" if res.get("maker")
           else "手续费按市价算")
    old = (f"Gate 只留最近约 7 天的 1 分钟 K 线，更早的用 {res['old_source']} 的行情（同一个币两边价格一般只差 0.1% 左右）；"
           if res.get("old_source") else "")
    lines.append("\n说明：用 1 分钟 K 线模拟，进场按信号那一分钟的价格，同一根 K 线碰到止损和止盈按止损算；"
                 f"{old}{fee}；没算滑点和资金费。历史表现不代表以后。")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "simulate":
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(simulate(sys.argv[2], sys.argv[3]))
    else:
        print("用法：python backtest.py simulate 任务.json 结果.json（一般由机器人的 /backtest 命令调用）")
