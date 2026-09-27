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


async def run(client, cfg, ch, days: int, keep, build_ctx, parser_cls, send) -> None:
    """整个回测流程，结果用 send(文字) 发给主人。同一个频道同一时间只跑一个。"""
    if ch.username in _running:
        await send(f"「{ch.title or ch.username}」的回测已经在跑了，请等它完成。")
        return
    _running.add(ch.username)
    name = ch.title or ch.username
    try:
        end = int(time.time()) // 60 * 60 - 120
        parser = parser_cls(cfg)
        try:
            events = await collect(client, ch, days, keep, build_ctx, parser, cfg.llm_vision, send)
        finally:
            await parser.close()
        n_err = sum(1 for e in events if "error" in e["parsed"])
        await send(f"🧠 识别完了 {len(events)} 条（{n_err} 条识别失败），正在用历史行情一单单模拟……")
        os.makedirs(BT_DIR, exist_ok=True)
        job_path = os.path.join(BT_DIR, f"{ch.username}.json")
        out_path = os.path.join(BT_DIR, f"{ch.username}.result.json")
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump({"channel": ch.username, "title": name, "start": end - days * 86400, "end": end,
                       "days": days, "events": events}, f, ensure_ascii=False)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, os.path.abspath(__file__), "simulate", job_path, out_path, cwd=BASE_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 3600)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("模拟超过 1 小时还没完成")
        if proc.returncode != 0:
            tail = (err or b"").decode(errors="replace").strip().splitlines()
            raise RuntimeError(tail[-1][:300] if tail else f"模拟进程退出码 {proc.returncode}")
        with open(out_path, encoding="utf-8") as f:
            await send(report_text(json.load(f)))
    except Exception as e:
        log.exception("回测失败")
        await send(f"❌ 「{name}」回测失败：{str(e)[:300]}\n请把这条消息发给 Claude Code。")
    finally:
        _running.discard(ch.username)


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
            async with self._sem:
                for i in range(4):
                    try:
                        if self.name == "weex":  # 普通 K 线接口不管 since，要用历史接口（一次最多 99 根，从 since 开始）
                            rows = await self.ex.fetch_ohlcv(symbol, tf, start, 99, {"historical": True, "until": until})
                        else:
                            rows = await self.ex.fetch_ohlcv(symbol, tf, since=start, limit=200)
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
            vol = sum(float(c[5]) * float(c[4]) for c in hs) * 24 / max(len(hs), 1) if hs else None
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
    for k in ("gate_key", "gate_secret", "weex_key", "weex_secret", "weex_passphrase",
              "bitget_key", "bitget_secret", "bitget_passphrase"):
        setattr(cfg, k, "")    # 回测进程不拿交易所密钥：只用公开行情，绝不可能真实下单
    ch = cfg.channel_by_username(job["channel"])
    ch.title = job["title"]
    ex = history_exchange(cfg, sim)
    await ex.init()
    parsed = {str(e["id"]): e["parsed"] for e in job["events"]}

    class Parser:
        async def parse(self, ctx):
            r = parsed.get(str(ctx.msg_id))
            if r is None or "error" in r:
                raise RuntimeError((r or {}).get("error", "没有识别结果"))
            return r

    class Quiet:
        async def send(self, text, markup=None):
            return True

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
            ctx = E.MsgCtx(ch, ch.title, -1, e["id"], datetime.fromtimestamp(e["ts"], timezone.utc), e["text"],
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
    finally:
        await ex.close()
        db.conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    risk = cfg.risk_for(ch)
    pct = float(risk["risk_per_trade_pct"]) * ch.risk_multiplier if not float(risk.get("risk_per_trade_usdt") or 0) else None
    closed = sorted((t for t in trades if t["status"] == "closed" and t.get("r_mult") is not None), key=lambda t: t["closed_at"])
    skips: dict = {}
    for o in outcomes:
        for part in o.split(" | "):
            if part.startswith("skip: "):
                k = skip_reason(part)
                skips[k] = skips.get(k, 0) + 1
    out = {
        "title": job["title"], "start": job["start"], "end": job["end"], "days": job["days"], "risk_pct": pct,
        "maker": eng.maker_on("paper"),
        "messages": len(job["events"]),
        "signals": sum(1 for e in job["events"] if any(a.get("type") == "open" for a in (e["parsed"].get("actions") or []))),
        "parse_errors": sum(1 for e in job["events"] if "error" in e["parsed"]),
        "opened": sum(1 for t in trades if t.get("opened_at")),
        "cancelled": sum(1 for t in trades if t["status"] == "cancelled" and not t.get("opened_at")),
        "skips": sorted(skips.items(), key=lambda kv: -kv[1]),
        "closed": [{"base": t["base"], "side": t["side"], "r": t["r_mult"], "reason": t.get("exit_reason") or "",
                    "opened_at": t.get("opened_at") or t["created_at"], "closed_at": t["closed_at"],
                    "fallback": t.get("sl_source") == "fallback"} for t in closed],
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
    lines = [f"📊 回测：{res['title']} 最近 {res['days']} 天（{day(res['start'])} ~ {day(res['end'])}）",
             "按你现在的规则：同一套 AI 识别、止损、分批止盈、同时最多几单都一样；1R = 一单打到止损亏的钱"]
    lines.append(f"• 要识别的消息 {res['messages']} 条，AI 认出开仓信号 {res['signals']} 个"
                 + (f"（{res['parse_errors']} 条识别失败）" if res["parse_errors"] else ""))
    skipped = sum(n for _, n in res["skips"])
    lines.append(f"• 实际开单 {res['opened']} 笔" + (f"，跳过 {skipped} 个：" + "、".join(f"{k} {n}" for k, n in res["skips"][:5])
                                                   if skipped else "")
                 + (f"；另有 {res['cancelled']} 张限价单没成交" if res["cancelled"] else ""))
    if not rs:
        lines.append("• 这段时间没有已平仓的单子，没法统计成绩。")
    else:
        wins = sum(1 for r in rs if r > 0)
        cum = peak = dd = 0.0
        streak = worst = 0
        for r in rs:
            cum += r
            peak, dd = max(peak, cum), min(dd, cum - peak)
            streak = streak + 1 if r < 0 else 0
            worst = max(worst, streak)
        lines.append(f"• 已平仓 {len(rs)} 单：胜率 {wins / len(rs) * 100:.0f}%，总 {sum(rs):+.1f}R，平均每单 {sum(rs) / len(rs):+.2f}R")
        lines.append(f"• 最大回撤 {dd:.1f}R，最长连亏 {worst} 单；最好一单 {max(rs):+.1f}R，最差一单 {min(rs):+.1f}R")
        fb = [t["r"] for t in res["closed"] if t["fallback"]]
        if fb:
            lines.append(f"• 其中信号没给止损、程序补止损的 {len(fb)} 单：总 {sum(fb):+.1f}R")
        if res["risk_pct"]:
            eq = pk = 1.0
            mdd = 0.0
            for r in rs:   # 按平仓顺序复利：每单亏/赚 权益 × 风险% × R
                eq *= 1 + res["risk_pct"] / 100 * r
                pk = max(pk, eq)
                mdd = min(mdd, eq / pk - 1)
            lines.append(f"💰 按每单亏总权益 {res['risk_pct']:g}% 算（复利）：这段时间 {(eq - 1) * 100:+.0f}%，"
                         f"中途最多回撤 {mdd * 100:.0f}%")
    if res["open"]:
        ur = [t["r"] for t in res["open"] if t["r"] is not None]
        lines.append(f"• 还没平仓 {len(res['open'])} 单，按现价浮动 {sum(ur):+.1f}R（没算进上面的成绩）")
    if res["closed"]:
        lines.append("\n最近的单子：")
        for t in res["closed"][-12:]:
            icon = "✅" if t["r"] > 0 else "🔴"
            lines.append(f"{icon} {datetime.fromtimestamp(t['opened_at'] / 1000, tz).strftime('%m-%d %H:%M')} "
                         f"{t['base']} {SIDE_CN.get(t['side'], '')} {t['r']:+.2f}R｜{t['reason']}")
    fee = ("开仓、止盈按挂单手续费（假设挂单都能成交），止损按市价手续费" if res.get("maker")
           else "手续费按市价算")
    lines.append("\n说明：用 1 分钟 K 线模拟，进场按信号那一分钟的价格，同一根 K 线碰到止损和止盈按止损算；"
                 f"{fee}；没算滑点和资金费。历史表现不代表以后。")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "simulate":
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(simulate(sys.argv[2], sys.argv[3]))
    else:
        print("用法：python backtest.py simulate 任务.json 结果.json（一般由机器人的 /backtest 命令调用）")
