"""交易引擎：频道信号 → 风控检查 → 模拟/实盘执行 → 持仓监控 → 统计

几条硬规则（全部在代码里执行，不依赖 AI）：
1. 永远带止损下单：信号没给止损时，程序按该币的波动率（ATR）补一个，或按设置直接不跟
2. 仓位按「打到止损固定亏多少 U」来算，跟信号写的杠杆无关
3. 杠杆按止损自动开到最高：计入交易所的维持保证金率和手续费后，强平价一定在止损之外
4. 同一个币同一时间只做一单；同时持仓数有上限
5. 频道的移动止损只允许收紧，不允许放宽
6. 交易所里只挂止损单（开仓单自带），止盈由程序盯盘执行：
   Bitget 上挂着的「只减仓」止盈限价单会冻结仓位，可能导致止损/平仓失败，所以不挂
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from db import now_ms

log = logging.getLogger("engine")

CONF_RANK = {"low": 0, "medium": 1, "high": 2}
SIDE_CN = {"long": "多", "short": "空"}
MODE_CN = {"live": "实盘", "paper": "模拟"}

HELP = ("📖 命令\n"
        "/status  运行状态、权益、持仓\n"
        "/stats   各频道已平仓战绩（R 值）\n"
        "/pause   暂停实盘开新仓\n"
        "/resume  恢复实盘开新仓\n"
        "/closeall 立即平掉本程序开的所有实盘仓位、撤挂单，并暂停\n"
        "/ip      服务器 IP（Bitget API 白名单填这个）\n"
        "/bitget  KEY SECRET PASSPHRASE  设置 Bitget API（我会立刻删除你这条消息）")


class Reject(Exception):
    """信号不满足风控条件 → 跳过。"""


@dataclass
class MsgCtx:
    channel: object            # config.ChannelCfg
    title: str
    chat_id: int
    msg_id: int
    date: datetime             # 消息时间（UTC）
    text: str
    reply_to: int | None = None
    reply_text: str | None = None
    forwarded: bool = False
    edited: bool = False


# ====================================================================
# 纯计算函数（不联网，selftest.py 会测试它们）
# ====================================================================
def fmt(x) -> str:
    return "-" if x is None else f"{float(x):.8g}"


def normalize_split(split, n: int) -> list[float]:
    s = [float(x) for x in (split or []) if float(x) > 0][:n]
    while len(s) < n:
        s.append(s[-1] if s else 1.0)
    tot = sum(s) or 1.0
    return [x / tot for x in s]


def tighter(side: str, new: float, cur: float) -> bool:
    """new 这个止损是否比 cur 更收紧。"""
    return new > cur if side == "long" else new < cur


DEFAULT_MMR = 0.02    # 查不到交易所档位时，按偏保守的 2% 维持保证金率算
DEFAULT_LEV_CAP = 20  # 查不到交易所上限、且没设 max_leverage 时的杠杆上限


def pick_tier(tiers, notional: float):
    for t in tiers or []:
        mx = t.get("maxNotional")
        if mx is None or notional <= float(mx):
            return t
    return (tiers or [None])[-1]


def liq_distance(lev: float, mmr: float, fee: float) -> float:
    """逐仓强平价离开仓价的距离（小数）。取多空里偏保守的空单公式。"""
    m = mmr + fee
    return (1.0 / lev - m) / (1 + m)


def safe_leverage(d: float, notional: float, risk: dict, tiers=None, exch_max=None) -> tuple[int, float]:
    """止损距离 d（小数）下，保证「强平距离 × liq_safety ≥ 止损距离」的最高杠杆。返回 (杠杆, 维持保证金率)"""
    tier = pick_tier(tiers, notional)
    mmr = float(tier["maintenanceMarginRate"]) if tier and tier.get("maintenanceMarginRate") is not None else DEFAULT_MMR
    caps = [float(x) for x in ((tier or {}).get("maxLeverage"), exch_max) if x]
    if float(risk.get("max_leverage") or 0) > 0:
        caps.append(float(risk["max_leverage"]))
    cap = min(caps) if caps else DEFAULT_LEV_CAP
    m = mmr + float(risk["fee_rate"])
    best = 1.0 / ((d / float(risk["liq_safety"])) * (1 + m) + m)
    return int(max(1, min(cap, math.floor(best)))), mmr


def plan_entry(side, lo, hi, sl, tps, price, risk, equity, free=None, multiplier=1.0, sl_label="",
               tiers=None, exch_max=None) -> dict:
    """根据信号和现价算出：市价/限价、止损、止盈、杠杆、数量。不满足条件抛 Reject。"""
    long = side == "long"
    if lo is None and hi is not None:
        lo = hi
    if hi is None and lo is not None:
        hi = lo
    if lo is not None and lo > hi:
        lo, hi = hi, lo

    dev = float(risk["max_entry_deviation_pct"]) / 100
    if lo is not None:
        for v in (lo, hi):
            if abs(v - price) / price > dev:
                raise Reject(f"进场价 {fmt(v)} 与现价 {fmt(price)} 相差超过 {risk['max_entry_deviation_pct']}%，可能识别错误")

    # ---- 市价还是限价 ----
    chase = float(risk["chase_pct"]) / 100
    if lo is None:
        kind, entry = "market", price
    elif long:
        if price <= hi * (1 + chase):
            kind, entry = "market", price
        elif risk["allow_limit_orders"]:
            kind, entry = "limit", hi
        else:
            raise Reject(f"现价 {fmt(price)} 已高于进场区 {fmt(lo)}~{fmt(hi)}，不追")
    else:
        if price >= lo * (1 - chase):
            kind, entry = "market", price
        elif risk["allow_limit_orders"]:
            kind, entry = "limit", lo
        else:
            raise Reject(f"现价 {fmt(price)} 已低于进场区 {fmt(lo)}~{fmt(hi)}，不追")

    # ---- 止损 ----
    notes = []
    if sl is None:
        f = float(risk.get("fallback_sl_pct") or 0) / 100
        if str(risk.get("fallback_sl_mode", "off")).lower() == "off" or f <= 0:
            raise Reject("信号没有给止损（可在 config.yaml 把 fallback_sl_mode 设为 atr，让程序补止损）")
        sl = entry * (1 - f) if long else entry * (1 + f)
        notes.append(f"信号没给止损，程序{sl_label}补了 {f * 100:.2f}% 的止损")
    if (long and sl >= entry) or (not long and sl <= entry):
        raise Reject(f"止损 {fmt(sl)} 已经在进场价 {fmt(entry)} 的错误一侧（可能已被打掉）")
    dist = abs(entry - sl)
    if dist / entry > float(risk["max_sl_distance_pct"]) / 100:
        raise Reject(f"止损距离 {dist / entry * 100:.1f}% 过大，可能识别错误")

    # ---- 止盈 ----
    tps = sorted({t for t in tps if (t > entry if long else t < entry)}, reverse=not long)
    if not tps and float(risk["fallback_tp_r"] or 0) > 0:
        r = float(risk["fallback_tp_r"])
        tps = [entry + r * dist if long else entry - r * dist]
        notes.append(f"信号没给止盈，按 {r:g} 倍风险设止盈")
    fracs = normalize_split(risk["tp_split"], len(tps)) if tps else []
    rr = None
    if tps:
        rr = sum(f * abs(t - entry) for f, t in zip(fracs, tps)) / dist
        if rr < float(risk["min_rr"]):
            raise Reject(f"按现在的进场价算，盈亏比只有 {rr:.2f}（低于 {risk['min_rr']}）")

    # ---- 仓位：打到止损（含手续费）固定亏 risk_per_trade_usdt（或权益 %）----
    fee = float(risk["fee_rate"])
    per_unit_loss = dist + 2 * fee * entry
    fixed = float(risk.get("risk_per_trade_usdt") or 0)
    risk_usdt = (fixed if fixed > 0 else equity * float(risk["risk_per_trade_pct"]) / 100) * float(multiplier)
    if risk_usdt <= 0:
        raise Reject("每单风险金额为 0，检查 risk_per_trade_usdt / risk_per_trade_pct")
    qty = risk_usdt / per_unit_loss

    # ---- 杠杆：按止损开到最高，但强平价一定在止损之外 ----
    lev, mmr = safe_leverage(dist / entry, qty * entry, risk, tiers, exch_max)
    max_margin = equity * float(risk["max_margin_pct"]) / 100
    if free is not None:
        max_margin = min(max_margin, free * 0.9)
    capped = False
    if qty * entry / lev > max_margin:
        qty = max(max_margin, 0) * lev / entry
        capped = True
    if qty <= 0:
        raise Reject("可用保证金不足")
    return {"side": side, "kind": kind, "entry": entry, "sl": sl, "leverage": lev, "qty": qty,
            "tps": [{"price": t, "frac": f} for t, f in zip(tps, fracs)],
            "risk_usdt": qty * per_unit_loss, "rr": rr, "capped": capped, "notes": notes,
            "mmr": mmr, "liq_dist": liq_distance(lev, mmr, fee), "margin": qty * entry / lev}


def atr_pct(candles, period: int = 14) -> float | None:
    """已收盘 K 线的 ATR（平均真实波幅）占最新收盘价的比例。candles: [[ts, o, h, l, c, v], ...]"""
    if len(candles) < period + 1:
        return None
    trs = [max(float(c[2]) - float(c[3]), abs(float(c[2]) - float(p[4])), abs(float(c[3]) - float(p[4])))
           for p, c in zip(candles[:-1], candles[1:])]
    close = float(candles[-1][4])
    return (sum(trs[-period:]) / period) / close if close > 0 else None


def split_qty(total, fracs, prices, step, ok_fn) -> list[float]:
    """把仓位按比例拆给多个止盈位（按最小步长分配，余数优先给近的止盈位）；
    低于最小下单量的份额并入相邻份额。"""
    if not fracs or total <= 0 or step <= 0:
        return [0.0] * len(fracs)
    lots_total = int(total / step + 1e-9)
    raw = [lots_total * f for f in fracs]
    lots = [int(x + 1e-9) for x in raw]
    for i in sorted(range(len(fracs)), key=lambda i: (-(raw[i] - lots[i]), i))[: lots_total - sum(lots)]:
        lots[i] += 1
    qs = [round(n * step, 12) for n in lots]
    for i in range(len(qs) - 1, 0, -1):
        if qs[i] > 0 and not ok_fn(qs[i], prices[i]):
            qs[i - 1] = round(qs[i - 1] + qs[i], 12)
            qs[i] = 0.0
    if len(qs) > 1 and qs[0] > 0 and not ok_fn(qs[0], prices[0]):
        for j in range(1, len(qs)):
            if qs[j] > 0:
                qs[j] = round(qs[j] + qs[0], 12)
                qs[0] = 0.0
                break
    return qs


def sl_reason(t: dict, sl: float) -> str:
    if abs(sl - t["entry_price"]) <= abs(t["entry_price"]) * 1e-9:
        return "保本离场"
    return "移动止损离场" if tighter(t["side"], sl, t["sl"]) else "止损离场"


def exit_part(t: dict, price: float, q: float, fee_rate: float) -> float:
    """模拟盘：以 price 平掉 q 数量，记入已实现盈亏（含手续费）。"""
    q = min(q, t["remaining"])
    if q <= 0:
        return 0.0
    pnl = (price - t["entry_price"]) * q if t["side"] == "long" else (t["entry_price"] - price) * q
    t["realized"] = (t.get("realized") or 0.0) + pnl - q * price * fee_rate
    t["remaining"] = t["remaining"] - q
    if t["remaining"] <= t["qty"] * 1e-6:
        t["remaining"] = 0.0
    return q


def finish(t: dict, ts: int, reason: str):
    t["status"] = "closed"
    t["closed_at"] = ts
    t["exit_reason"] = reason
    t["remaining"] = 0.0
    t["pnl"] = t.get("realized") or 0.0
    t["r_mult"] = t["pnl"] / t["risk_usdt"] if t.get("risk_usdt") else None


def simulate_candle(t: dict, ts: int, high: float, low: float, fee_rate: float, be_after_tp1: bool) -> list[str]:
    """用一根 1 分钟 K 线推进一笔模拟单。同一根 K 线同时碰到止损和止盈时按止损算（偏保守）。"""
    ev: list[str] = []
    long = t["side"] == "long"
    if t["status"] == "pending":
        lp = t["entry_price"]
        if (long and low <= lp) or (not long and high >= lp):
            t["status"], t["opened_at"], t["remaining"] = "open", ts, t["qty"]
            t["realized"] = (t.get("realized") or 0.0) - t["qty"] * lp * fee_rate
            for tp in t["tps"]:
                tp["qty"] = t["qty"] * tp["frac"]
            ev.append(f"限价单成交 @{fmt(lp)}")
        return ev  # 成交那根 K 线不再判断止盈止损
    if t["status"] != "open":
        return ev

    sl = t["soft_sl"]
    if (long and low <= sl) or (not long and high >= sl):
        exit_part(t, sl, t["remaining"], fee_rate)
        reason = sl_reason(t, sl)
        finish(t, ts, reason)
        ev.append(f"{reason} @{fmt(sl)}")
        return ev

    tps = t["tps"]
    for i, tp in enumerate(tps):
        if tp.get("filled"):
            continue
        if not (high >= tp["price"] if long else low <= tp["price"]):
            break
        q = t["remaining"] if i == len(tps) - 1 else min(tp.get("qty") or 0.0, t["remaining"])
        exit_part(t, tp["price"], q, fee_rate)
        tp["filled"] = True
        ev.append(f"止盈{i + 1} 成交 @{fmt(tp['price'])}")
        if be_after_tp1 and not t.get("be_moved") and t["remaining"] > 0 \
                and tighter(t["side"], t["entry_price"], t["soft_sl"]):
            t["soft_sl"], t["be_moved"] = t["entry_price"], 1
            ev.append("止损移到开仓价（保本）")
        if t["remaining"] <= 0:
            break
    if t["status"] == "open" and t["remaining"] <= 0:
        finish(t, ts, "止盈离场")
    return ev


# ====================================================================
# 引擎
# ====================================================================
class Engine:
    def __init__(self, cfg, db, ex, parser, notifier):
        self.cfg, self.db, self.ex, self.parser, self.notifier = cfg, db, ex, parser, notifier
        self.lock = asyncio.Lock()
        self._err_ts: dict[str, float] = {}

    # ---------------- 小工具 ----------------
    async def notify(self, text: str):
        await self.notifier.send(text)

    async def notify_error(self, key: str, text: str, every: int = 600):
        now = time.time()
        if now - self._err_ts.get(key, 0) >= every:
            self._err_ts[key] = now
            await self.notify(text)

    def risk_of(self, t: dict) -> dict:
        ch = self.cfg.channel_by_username(t["channel"])
        return self.cfg.risk_for(ch) if ch else dict(self.cfg.risk)

    def paper_equity(self) -> float:
        return self.cfg.paper_equity + self.db.paper_realized()

    def paused(self) -> str | None:
        return self.db.kv_get("paused")

    def set_paused(self, reason: str | None):
        self.db.kv_set("paused", reason)

    @staticmethod
    def _conf_ok(conf: str, need: str) -> bool:
        return CONF_RANK.get(conf, 0) >= CONF_RANK.get(str(need).lower(), 2)

    # ---------------- 消息入口 ----------------
    async def handle_message(self, ctx: MsgCtx):
        ch = ctx.channel
        mode = self.cfg.mode_for(ch)
        if mode == "off":
            return
        risk = self.cfg.risk_for(ch)
        age = time.time() - ctx.date.timestamp()
        if age > float(risk["max_signal_age_sec"]):
            log.info("忽略旧消息 %s #%s（%.0f 秒前）", ctx.title, ctx.msg_id, age)
            return
        if self.db.message_seen(ctx.chat_id, ctx.msg_id, ctx.edited):
            return
        if ctx.edited and self.db.trade_for_message(ctx.chat_id, ctx.msg_id):
            self.db.save_message(ctx, None, outcome="edit_ignored")
            await self.notify(f"✏️ {ctx.title} 修改了一条已经跟过的信号，程序不跟随修改（防止事后改单）\n修改后：{ctx.text[:200]}")
            return

        try:
            parsed = await self.parser.parse(ctx)
        except Exception as e:
            log.exception("解析失败")
            self.db.save_message(ctx, None, outcome=f"parse_error: {e}")
            await self.notify_error("parse", f"⚠️ AI 解析失败（10 分钟内不重复提醒）：{str(e)[:200]}")
            return

        row = self.db.save_message(ctx, parsed)
        log.info("%s #%s → %s", ctx.title, ctx.msg_id, parsed)
        outcomes = []
        for act in parsed["actions"]:
            async with self.lock:
                try:
                    res = await self.dispatch(act, ctx, mode, risk, row)
                except Exception as e:
                    log.exception("执行出错")
                    res = f"error: {e}"
                    await self.notify(f"❌ 执行出错 {ctx.title} {act.get('symbol') or ''} {act['type']}：{str(e)[:300]}")
            outcomes.append(res)
        self.db.set_message(row, outcome=" | ".join(outcomes) or "no_action")

    async def dispatch(self, act, ctx, mode, risk, row) -> str:
        if act["type"] == "open":
            return await self.on_open(act, ctx, mode, risk, row)
        t = self.resolve_target(act, ctx)
        if not t:
            return f"{act['type']}: 没有对应的持仓"
        if not self._conf_ok(act["confidence"], risk["min_manage_confidence"]):
            return f"{act['type']}: 置信度低"
        self.db.set_message(row, trade_id=t["id"])
        if act["type"] == "close":
            if not risk["follow_close"]:
                return "close: 已关闭跟随"
            reason = "频道指令平仓" if act["fraction"] >= 0.95 else "频道指令减仓"
            return await self.on_close(t, act["fraction"], reason)
        if act["type"] == "move_sl":
            return await self.on_move_sl(t, act) if risk["follow_move_sl"] else "move_sl: 已关闭跟随"
        if act["type"] == "update_tp":
            return await self.on_update_tp(t, act, risk) if risk["follow_update_tp"] else "update_tp: 已关闭跟随"
        return "unknown"

    def resolve_target(self, act, ctx) -> dict | None:
        """跟进指令对应哪一笔交易：优先看「回复了哪条消息」，其次按同频道同币种匹配。"""
        sym = act.get("symbol")
        if ctx.reply_to:
            tid = self.db.trade_for_message(ctx.chat_id, ctx.reply_to)
            t = self.db.get_trade(tid) if tid else None
            if t:
                if t["status"] not in ("pending", "open") or (sym and sym != t["base"]):
                    return None
                return t
        if sym:
            cands = [t for t in self.db.active_trades() if t["chat_id"] == ctx.chat_id and t["base"] == sym]
            if len(cands) == 1:
                return cands[0]
        return None

    # ---------------- 开仓 ----------------
    async def on_open(self, act, ctx, mode, risk, row) -> str:
        head = f"[{MODE_CN[mode]}] {ctx.title}：{act.get('symbol') or '?'} {SIDE_CN.get(act['side'], '')}"
        try:
            plan = await self.make_plan(act, ctx, mode, risk)
        except Reject as r:
            if CONF_RANK.get(act["confidence"], 0) >= 1:  # 明显不是信号的就不打扰你了
                await self.notify(f"⏭ 跳过 {head}\n原因：{r}")
            return f"skip: {r}"
        t = await (self.open_live(plan, ctx) if mode == "live" else self.open_paper(plan, ctx))
        self.db.set_message(row, trade_id=t["id"])
        await self.notify(self.open_text(t, plan))
        return f"opened #{t['id']}"

    async def make_plan(self, act, ctx, mode, risk) -> dict:
        if not self._conf_ok(act["confidence"], risk["min_open_confidence"]):
            raise Reject(f"不是明确的开仓指令（AI 置信度 {act['confidence']}）")
        base = act.get("symbol")
        if not base:
            raise Reject("没识别出币种")
        if not act.get("stop_loss") and str(risk.get("fallback_sl_mode", "off")).lower() not in ("atr", "pct"):
            raise Reject("信号没有给止损（可在 config.yaml 把 fallback_sl_mode 设为 atr，让程序补止损）")
        allowed = [str(s).upper() for s in (risk["allowed_symbols"] or [])]
        if allowed and base not in allowed:
            raise Reject(f"{base} 不在白名单 allowed_symbols 里")
        if base in [str(s).upper() for s in (risk["blocked_symbols"] or [])]:
            raise Reject(f"{base} 在黑名单 blocked_symbols 里")
        symbol, scale = self.ex.resolve(base)
        if not symbol:
            raise Reject(f"Bitget 没有 {base} 的 USDT 永续合约")

        active = self.db.active_trades(mode)
        for t in active:
            if t["symbol"] == symbol:
                raise Reject(f"{base} 已有{MODE_CN[mode]}持仓/挂单 #{t['id']}（来自 {t['title']}）")
        if len(active) >= int(risk["max_open_positions"]):
            raise Reject(f"{MODE_CN[mode]}持仓+挂单已达上限 {risk['max_open_positions']} 单")

        free = None
        if mode == "live":
            p = self.paused()
            if p:
                raise Reject(f"实盘开新仓已暂停：{p}")
            if symbol in await self.ex.positions():
                raise Reject(f"交易所账户里已有 {base} 仓位（不是本程序开的），不叠加")
            equity, free = await self.ex.balance()
        else:
            equity = self.paper_equity()

        tk = await self.ex.ticker(symbol)
        price = tk["last"]
        if not price:
            raise Reject("拿不到最新价")
        vol = tk.get("quoteVolume")
        if vol is not None and vol < float(risk["min_24h_volume_usdt"]):
            raise Reject(f"{base} 24 小时成交额只有 {vol / 1e6:.2f}M USDT，流动性太差")

        def sc(v):
            return v * scale if v else None

        sl_source, sl_label = "signal", ""
        if not act.get("stop_loss"):
            pct, sl_label = await self.fallback_sl_pct(symbol, risk)
            risk = dict(risk, fallback_sl_pct=pct)
            sl_source = "fallback"
        tiers = await self.ex.leverage_tiers(symbol)
        plan = plan_entry(act["side"], sc(act.get("entry_low")), sc(act.get("entry_high")), sc(act.get("stop_loss")),
                          [v * scale for v in (act.get("take_profits") or [])], price, risk, equity, free,
                          ctx.channel.risk_multiplier, sl_label, tiers, self.ex.max_leverage(symbol))
        plan["sl_source"] = sl_source
        qty = self.ex.round_qty(symbol, plan["qty"])
        if not self.ex.meets_min(symbol, qty, plan["entry"]):
            raise Reject(f"按风控算出的仓位（约 {plan['qty'] * plan['entry']:.2f}U）低于交易所最小下单量")
        # 数量按交易所精度向下取整后，实际止损金额会略小于设定值，按真实数量重算
        plan["risk_usdt"] *= qty / plan["qty"]
        plan["margin"] = qty * plan["entry"] / plan["leverage"]
        plan.update(symbol=symbol, base=base, scale=scale, qty=qty, price=price)
        return plan

    async def fallback_sl_pct(self, symbol: str, risk: dict) -> tuple[float, str]:
        """信号没给止损时补多远的止损（%）。atr：1 小时 ATR × 倍数，限制在 min~max 之间。"""
        if str(risk["fallback_sl_mode"]).lower() == "pct":
            return float(risk["fallback_sl_pct"]), "按固定比例"
        candles = await self.ex.candles(symbol, "1h", 40)
        a = atr_pct(candles[:-1], 14)  # 最后一根还没收盘，不用
        if not a:
            raise Reject("这个币 K 线太少，算不出波动率止损")
        mult = float(risk["fallback_sl_atr_mult"])
        pct = min(max(a * mult * 100, float(risk["fallback_sl_min_pct"])), float(risk["fallback_sl_max_pct"]))
        return pct, f"按 1 小时 ATR×{mult:g}（{a * 100:.2f}%×{mult:g}）"

    def _new_trade(self, plan, ctx, mode) -> dict:
        now = now_ms()
        return {
            "mode": mode, "channel": ctx.channel.username, "title": ctx.title,
            "chat_id": ctx.chat_id, "msg_id": ctx.msg_id,
            "symbol": plan["symbol"], "base": plan["base"], "scale": plan["scale"], "side": plan["side"],
            "entry_kind": plan["kind"], "entry_price": plan["entry"], "qty": plan["qty"], "remaining": 0.0,
            "sl": plan["sl"], "soft_sl": plan["sl"],
            "tps": [{"price": x["price"], "frac": x["frac"], "qty": None, "order_id": None, "filled": False}
                    for x in plan["tps"]],
            "leverage": plan["leverage"], "risk_usdt": plan["risk_usdt"], "realized": 0.0,
            "created_at": now, "last_candle_ts": now // 60000 * 60000, "be_moved": 0,
            "sl_source": plan.get("sl_source", "signal"),
        }

    async def open_paper(self, plan, ctx) -> dict:
        t = self._new_trade(plan, ctx, "paper")
        risk = self.risk_of(t)
        if plan["kind"] == "market":
            t.update(status="open", opened_at=t["created_at"], remaining=plan["qty"],
                     realized=-plan["qty"] * plan["entry"] * float(risk["fee_rate"]))
            for tp in t["tps"]:
                tp["qty"] = plan["qty"] * tp["frac"]
        else:
            t.update(status="pending", expires_at=t["created_at"] + int(float(risk["limit_order_ttl_min"]) * 60000))
        t["id"] = self.db.insert_trade(t)
        return t

    async def open_live(self, plan, ctx) -> dict:
        t = self._new_trade(plan, ctx, "live")
        sym, side = plan["symbol"], plan["side"]
        await self.ex.prepare(sym, plan["leverage"], side)
        if plan["kind"] == "market":
            t["order_id"] = await self.ex.open_market(sym, side, plan["qty"], plan["sl"])
            pos = await self.ex.wait_position(sym)
            if pos and pos["side"] == side:
                t.update(entry_price=pos["entry"] or plan["price"], qty=pos["size"], remaining=pos["size"])
            else:
                t.update(remaining=plan["qty"])
                await self.notify(f"⚠️ {plan['base']} 市价单已提交但暂时没查到持仓，程序会在 30 秒内自动核对")
            t.update(status="open", opened_at=now_ms())
            self.assign_tp_qty(t)
            t["id"] = self.db.insert_trade(t)
            liq = float((pos or {}).get("liq") or 0)
            if liq > 0 and ((side == "long" and liq >= t["sl"]) or (side == "short" and liq <= t["sl"])):
                await self.notify(f"⚠️ #{t['id']} {plan['base']} 交易所显示强平价 {fmt(liq)} 在止损 {fmt(t['sl'])} 之前！"
                                  f"请马上在 App 里给这个仓位追加保证金，或发 /closeall")
        else:
            t["order_id"] = await self.ex.open_limit(sym, side, plan["qty"], plan["entry"], plan["sl"])
            ttl = float(self.risk_of(t)["limit_order_ttl_min"])
            t.update(status="pending", expires_at=now_ms() + int(ttl * 60000))
            t["id"] = self.db.insert_trade(t)
        return t

    def assign_tp_qty(self, t: dict):
        """把当前剩余仓位按比例分配给还没触发的止盈位（实盘按交易所最小步长/最小下单量取整）。"""
        items = [x for x in t["tps"] if not x.get("filled")]
        if not items:
            return
        fr = normalize_split([x["frac"] for x in items], len(items))
        if t["mode"] == "live":
            qs = split_qty(t["remaining"], fr, [x["price"] for x in items], self.ex.qty_step(t["symbol"]),
                           lambda q, p: self.ex.meets_min(t["symbol"], q, p))
        else:
            qs = [t["remaining"] * f for f in fr]
        for x, f, q in zip(items, fr, qs):
            x["frac"], x["qty"] = f, q

    # ---------------- 跟进指令 ----------------
    async def on_close(self, t, frac, reason) -> str:
        full = frac >= 0.95
        m = MODE_CN[t["mode"]]
        if t["status"] == "pending":
            if t["mode"] == "live":
                await self.ex.cancel(t["symbol"], t["order_id"])
            t.update(status="cancelled", closed_at=now_ms(), exit_reason=f"{reason}（挂单未成交，已撤）")
            self.db.save_trade(t)
            await self.notify(f"🚫 #{t['id']} [{m}] {t['base']} 限价单已撤销（{reason}）")
            return "pending cancelled"

        if t["mode"] == "paper":
            price = await self.ex.last_price(t["symbol"])
            exit_part(t, price, t["remaining"] if full else t["remaining"] * frac, float(self.risk_of(t)["fee_rate"]))
            if full or t["remaining"] <= 0:
                finish(t, now_ms(), reason)
                self.db.save_trade(t)
                await self.notify(self.close_text(t, price))
                return "closed"
            self.assign_tp_qty(t)
            self.db.save_trade(t)
            await self.notify(f"✂️ #{t['id']} [模拟] {t['base']} {reason} {frac * 100:.0f}% @{fmt(price)}，剩余 {fmt(t['remaining'])}")
            return "reduced"

        pos = (await self.ex.positions()).get(t["symbol"])
        if not pos or pos["side"] != t["side"]:
            await self.finalize_live(t, "已在交易所平仓")
            return "already closed"
        size = pos["size"]
        q = size if full else self.ex.round_qty(t["symbol"], size * frac)
        if not full and q >= size:
            full, q = True, size
        if not full and not self.ex.meets_min(t["symbol"], q, pos["entry"] or t["entry_price"]):
            await self.notify(f"⚠️ #{t['id']} {t['base']} 频道要求减仓 {frac * 100:.0f}%，但减仓量低于最小下单量，未执行")
            return "reduce too small"
        try:
            await self.ex.reduce_market(t["symbol"], t["side"], q)
        except Exception:
            pos = (await self.ex.positions()).get(t["symbol"])
            if not pos or pos["side"] != t["side"]:  # 同一时间被交易所止损平掉了
                await self.finalize_live(t, "已在交易所平仓")
                return "already closed"
            raise
        if full:
            await self.finalize_live(t, reason)
            return "closed"
        t["remaining"] = max(size - q, 0.0)
        self.assign_tp_qty(t)
        self.db.save_trade(t)
        price = await self.ex.last_price(t["symbol"])
        await self.notify(f"✂️ #{t['id']} [实盘] {t['base']} {reason} {frac * 100:.0f}% @≈{fmt(price)}，剩余 {fmt(t['remaining'])}")
        return "reduced"

    async def on_move_sl(self, t, act) -> str:
        new = t["entry_price"] if act["breakeven"] else act["price"] * (t.get("scale") or 1)
        label = f"保本价 {fmt(new)}" if act["breakeven"] else fmt(new)
        if not tighter(t["side"], new, t["soft_sl"]):
            await self.notify(f"ℹ️ #{t['id']} {t['base']} 频道把止损改到 {label}，没有比当前止损 {fmt(t['soft_sl'])} 更收紧，忽略（程序只收紧、不放宽止损）")
            return "move_sl: 不是收紧"
        if t["status"] == "open":
            price = await self.ex.last_price(t["symbol"])
            if (t["side"] == "long" and new >= price) or (t["side"] == "short" and new <= price):
                return await self.on_close(t, 1.0, f"新止损 {label} 已越过现价，直接离场")
        t["soft_sl"] = new
        self.db.save_trade(t)
        extra = "\n（交易所里的原止损保留兜底，新止损由程序盯盘执行）" if t["mode"] == "live" else ""
        await self.notify(f"🛡 #{t['id']} [{MODE_CN[t['mode']]}] {t['base']} 止损收紧到 {label}{extra}")
        return "move_sl: ok"

    async def on_update_tp(self, t, act, risk) -> str:
        sc = t.get("scale") or 1
        long = t["side"] == "long"
        ref = await self.ex.last_price(t["symbol"]) if t["status"] == "open" else t["entry_price"]
        new = sorted({p * sc for p in act["take_profits"] if (p * sc > ref if long else p * sc < ref)}, reverse=not long)
        if not new:
            await self.notify(f"ℹ️ #{t['id']} {t['base']} 频道给的新止盈位已经过了现价，忽略")
            return "update_tp: 无效"
        items = [{"price": p, "frac": f} for p, f in zip(new, normalize_split(risk["tp_split"], len(new)))]
        filled = [x for x in t["tps"] if x.get("filled")] if t["status"] == "open" else []
        t["tps"] = filled + [dict(x, qty=None, order_id=None, filled=False) for x in items]
        if t["status"] == "open":
            self.assign_tp_qty(t)
        self.db.save_trade(t)
        await self.notify(f"🎯 #{t['id']} [{MODE_CN[t['mode']]}] {t['base']} 止盈更新为 {' / '.join(fmt(p) for p in new)}")
        return "update_tp: ok"

    # ---------------- 实盘收尾 ----------------
    async def finalize_live(self, t, reason: str):
        await asyncio.sleep(1.5)  # 等交易所生成历史仓位记录
        pnl, exit_px = await self.ex.closed_pnl(t["symbol"], int(t.get("opened_at") or t["created_at"]) - 60000)
        t.update(status="closed", closed_at=now_ms(), exit_reason=reason, remaining=0.0, pnl=pnl,
                 r_mult=(pnl / t["risk_usdt"]) if (pnl is not None and t.get("risk_usdt")) else None)
        self.db.save_trade(t)
        await self.notify(self.close_text(t, exit_px))

    # ---------------- 监控循环 ----------------
    async def monitor_forever(self):
        tick = 0
        while True:
            try:
                async with self.lock:
                    await self.monitor_live(tick)
                    if tick % 12 == 0:
                        await self.monitor_paper()
                        await self.maybe_daily_report()
            except Exception as e:
                log.exception("监控出错")
                await self.notify_error("monitor", f"⚠️ 持仓监控出错（10 分钟内不重复提醒）：{str(e)[:200]}")
            tick += 1
            await asyncio.sleep(5)

    async def monitor_live(self, tick: int):
        trades = self.db.active_trades("live")
        if not trades:
            return
        # 每 5 秒：程序端止损（移动后的止损 + 交易所止损的兜底）和止盈
        opens = [t for t in trades if t["status"] == "open"]
        if opens:
            prices = await self.ex.last_prices(sorted({t["symbol"] for t in opens}))
            for t in opens:
                p = prices.get(t["symbol"])
                if not p:
                    continue
                if (t["side"] == "long" and p <= t["soft_sl"]) or (t["side"] == "short" and p >= t["soft_sl"]):
                    await self.on_close(t, 1.0, sl_reason(t, t["soft_sl"]))
                else:
                    await self.check_live_tps(t, p)
        # 每 30 秒：和交易所核对持仓/挂单
        if tick % 6:
            return
        pos = await self.ex.positions()
        for t in self.db.active_trades("live"):
            try:
                if t["status"] == "pending":
                    await self.sync_pending(t, pos)
                else:
                    await self.sync_open(t, pos)
            except Exception as e:
                log.exception("核对出错")
                await self.notify_error(f"sync{t['id']}", f"⚠️ 核对 #{t['id']} {t['base']} 出错：{str(e)[:150]}")

    async def check_live_tps(self, t, price: float):
        """实盘止盈：价格到达止盈位 → 只减仓市价单平掉这一档。"""
        long = t["side"] == "long"
        tps = t["tps"]
        for i, x in enumerate(tps):
            if x.get("filled"):
                continue
            if not (price >= x["price"] if long else price <= x["price"]):
                return
            q = x.get("qty") or 0.0
            if i == len(tps) - 1 or q >= t["remaining"] * 0.999:
                await self.on_close(t, 1.0, f"止盈{i + 1} 触发，全部平仓")
                return
            x["filled"] = True
            if q > 0:
                try:
                    await self.ex.reduce_market(t["symbol"], t["side"], q)
                except Exception:
                    x["filled"] = False
                    raise
                t["remaining"] = max(t["remaining"] - q, 0.0)
            msg = f"🎯 #{t['id']} [实盘] {t['base']} 止盈{i + 1} {fmt(x['price'])} 触发，平掉 {fmt(q)}，剩余 {fmt(t['remaining'])}"
            if self.risk_of(t)["breakeven_after_tp1"] and not t["be_moved"] \
                    and tighter(t["side"], t["entry_price"], t["soft_sl"]):
                t["soft_sl"], t["be_moved"] = t["entry_price"], 1
                msg += f"\n止损移到开仓价 {fmt(t['entry_price'])}（保本）"
            self.db.save_trade(t)
            await self.notify(msg)

    async def sync_open(self, t, pos):
        p = pos.get(t["symbol"])
        if not p or p["side"] != t["side"]:
            await self.finalize_live(t, "交易所止盈/止损成交")
            return
        if abs(p["size"] - t["remaining"]) > t["remaining"] * 0.001:
            if p["size"] < t["remaining"]:
                await self.notify(f"ℹ️ #{t['id']} {t['base']} 交易所仓位被减少到 {fmt(p['size'])}（可能是在 App 里手动操作），已同步")
            t["remaining"] = p["size"]
            self.assign_tp_qty(t)
            self.db.save_trade(t)

    async def sync_pending(self, t, pos):
        try:
            st = await self.ex.order_status(t["symbol"], t["order_id"])
        except Exception as e:
            log.warning("查询挂单 #%s 失败：%s", t["id"], e)
            st = {"status": "open"}  # 查不到就按「还在挂」处理，超时后照样撤单
        p = pos.get(t["symbol"])
        has_pos = bool(p and p["side"] == t["side"] and p["size"] > 0)
        if st["status"] == "open":
            if now_ms() <= (t.get("expires_at") or 0):
                return
            await self.ex.cancel(t["symbol"], t["order_id"])
            p = (await self.ex.positions()).get(t["symbol"])
            has_pos = bool(p and p["side"] == t["side"] and p["size"] > 0)
            if not has_pos:
                t.update(status="cancelled", closed_at=now_ms(), exit_reason="限价单超时未成交")
                self.db.save_trade(t)
                await self.notify(f"⌛ #{t['id']} [实盘] {t['base']} 限价单 {fmt(t['entry_price'])} 超时未成交，已撤单")
                return
        elif not has_pos:
            if st["status"] == "closed":  # 成交后马上被止损（极端行情）
                t.update(status="open", opened_at=now_ms())
                await self.finalize_live(t, "限价成交后已被止损/平仓")
            else:
                t.update(status="cancelled", closed_at=now_ms(), exit_reason=f"挂单状态 {st['status']}")
                self.db.save_trade(t)
                await self.notify(f"🚫 #{t['id']} [实盘] {t['base']} 限价单已失效（{st['status']}）")
            return
        # 已成交（全部或部分）
        t.update(status="open", opened_at=now_ms(), entry_price=p["entry"] or t["entry_price"],
                 qty=p["size"], remaining=p["size"])
        self.assign_tp_qty(t)
        self.db.save_trade(t)
        await self.notify(f"✅ #{t['id']} [实盘] {t['base']} 限价单成交 @{fmt(t['entry_price'])}，数量 {fmt(p['size'])}")

    async def monitor_paper(self):
        trades = self.db.active_trades("paper")
        now = now_ms()
        for t in trades:
            risk = self.risk_of(t)
            events: list[str] = []
            since = int(t.get("last_candle_ts") or t["created_at"]) + 60000
            if since + 60000 <= now:
                try:
                    candles = await self.ex.ohlcv(t["symbol"], since)
                except Exception as e:
                    log.warning("拉 K 线失败 %s：%s", t["symbol"], e)
                    continue
                for c in candles:
                    ts = int(c[0])
                    if ts < since or ts + 60000 > now:
                        continue
                    events += simulate_candle(t, ts, float(c[2]), float(c[3]), float(risk["fee_rate"]),
                                              bool(risk["breakeven_after_tp1"]))
                    t["last_candle_ts"] = ts
                    if t["status"] not in ("pending", "open"):
                        break
            if t["status"] == "pending" and now > (t.get("expires_at") or 0):
                t.update(status="cancelled", closed_at=now, exit_reason="限价单超时未成交")
                events.append("限价单超时未成交，已取消")
            self.db.save_trade(t)
            if not events:
                continue
            if t["status"] == "closed":
                await self.notify(self.close_text(t, None, events))
            else:
                await self.notify(f"📈 #{t['id']} [模拟] {t['base']} {SIDE_CN[t['side']]}：" + "；".join(events))

    async def maybe_daily_report(self):
        """每天 daily_report_hour 点推送一次日报（持仓 + 各频道战绩）。"""
        if self.cfg.report_hour < 0:
            return
        now = datetime.now(timezone.utc) + timedelta(hours=self.cfg.tz_offset)
        day = now.strftime("%Y-%m-%d")
        if now.hour < self.cfg.report_hour or self.db.kv_get("report_day") == day:
            return
        self.db.kv_set("report_day", day)
        await self.notify(f"🗓 日报 {day}\n" + await self.status_text() + "\n\n" + self.stats_text())

    # ---------------- 命令 ----------------
    async def handle_command(self, text: str) -> str:
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return await self.status_text()
        if cmd == "/stats":
            return self.stats_text()
        if cmd == "/pause":
            self.set_paused("手动暂停")
            return "⏸ 已暂停实盘开新仓。模拟盘继续记录，已有持仓照常管理。/resume 恢复"
        if cmd == "/resume":
            self.set_paused(None)
            return "▶️ 已恢复实盘开新仓"
        if cmd == "/closeall":
            async with self.lock:
                self.set_paused("手动 /closeall")
                n = 0
                for t in self.db.active_trades("live"):
                    try:
                        await self.on_close(t, 1.0, "手动 /closeall")
                        n += 1
                    except Exception as e:
                        await self.notify(f"❌ #{t['id']} {t['base']} 平仓失败：{e}，请立刻去 App 手动处理")
            return f"已处理 {n} 笔实盘持仓/挂单，并暂停开新仓（/resume 恢复）"
        return "未知命令，发 /help 查看"

    async def status_text(self) -> str:
        paused = self.paused()
        lines = [f"🤖 运行中｜实盘总开关：{'开' if self.cfg.live_trading else '关（全部模拟）'}｜实盘开新仓：{('⏸ ' + paused) if paused else '正常'}"]
        if self.ex.has_keys:
            try:
                eq, free = await self.ex.balance()
                lines.append(f"💰 实盘权益 {eq:.2f}U，可用 {free:.2f}U")
            except Exception as e:
                lines.append(f"💰 实盘账户查询失败：{str(e)[:100]}")
        lines.append(f"🧪 模拟权益 {self.paper_equity():.2f}U")
        lines.append("📡 " + "、".join(f"{c.title or c.username}={MODE_CN.get(self.cfg.mode_for(c), '关闭')}"
                                      for c in self.cfg.channels))
        act = self.db.active_trades()
        lines.append(f"📂 持仓/挂单 {len(act)} 笔：" if act else "📂 当前没有持仓/挂单")
        for t in act:
            st = f"持仓 {fmt(t['remaining'])}" if t["status"] == "open" else "挂单中"
            sl = fmt(t["soft_sl"]) + ("" if t["soft_sl"] == t["sl"] else f"（原 {fmt(t['sl'])}）")
            lines.append(f"#{t['id']} [{MODE_CN[t['mode']]}] {t['base']} {SIDE_CN[t['side']]} {st} @{fmt(t['entry_price'])} 止损 {sl}｜{t['title']}")
        return "\n".join(lines)

    def stats_text(self) -> str:
        rows = self.db.closed_trades()
        if not rows:
            return "📊 还没有已平仓的交易，模拟盘跑一段时间再看。"
        agg: dict = {}
        for t in rows:
            a = agg.setdefault((t["title"] or t["channel"], t["mode"]),
                               {"n": 0, "win": 0, "r": 0.0, "rn": 0, "pnl": 0.0, "fn": 0, "fr": 0.0})
            if t.get("sl_source") == "fallback":
                a["fn"] += 1
                a["fr"] += t.get("r_mult") or 0.0
            pnl = t.get("pnl") or 0.0
            a["n"] += 1
            a["pnl"] += pnl
            a["win"] += pnl > 0
            if t.get("r_mult") is not None:
                a["r"] += t["r_mult"]
                a["rn"] += 1
        lines = ["📊 已平仓战绩（R = 盈亏 ÷ 计划止损金额，已扣手续费）"]
        for (title, mode), a in sorted(agg.items(), key=lambda kv: -kv[1]["r"]):
            avg = a["r"] / a["rn"] if a["rn"] else 0.0
            lines.append(f"• {title} [{MODE_CN[mode]}]：{a['n']} 单，胜率 {a['win'] / a['n'] * 100:.0f}%，"
                         f"总 {a['r']:+.2f}R，平均 {avg:+.2f}R，{a['pnl']:+.2f}U"
                         + (f"\n   其中程序补止损的 {a['fn']} 单：总 {a['fr']:+.2f}R" if a["fn"] else ""))
        lines.append("单数够多（建议 ≥30）且总 R 为正的频道，才值得考虑切实盘。")
        return "\n".join(lines)

    # ---------------- 通知文案 ----------------
    def open_text(self, t, plan) -> str:
        dist = abs(t["entry_price"] - t["sl"]) / t["entry_price"] * 100
        if t["status"] == "pending":
            entry = f"限价挂单 {fmt(t['entry_price'])}（现价 {fmt(plan['price'])}，{int(float(self.risk_of(t)['limit_order_ttl_min']))} 分钟内有效）"
        else:
            entry = f"市价 {fmt(t['entry_price'])}"
        tps = " / ".join(fmt(x["price"]) for x in t["tps"]) or "无（等频道指令）"
        if t["mode"] == "live" and t["tps"]:
            tps += "（程序盯盘，到价分批市价平）"
        lines = [f"🟢 开仓 [{MODE_CN[t['mode']]}] #{t['id']} {t['base']} {SIDE_CN[t['side']]}",
                 f"来源：{t['title']}",
                 f"入场：{entry}",
                 f"数量：{fmt(t['qty'])}（≈{t['qty'] * t['entry_price']:.1f}U）｜杠杆 {t['leverage']}x｜保证金 ≈{t['qty'] * t['entry_price'] / t['leverage']:.1f}U",
                 f"止损：{fmt(t['sl'])}（-{dist:.2f}%）｜强平约在 -{plan['liq_dist'] * 100:.2f}%｜打到止损约亏 {t['risk_usdt']:.2f}U",
                 f"止盈：{tps}" + (f"｜盈亏比 {plan['rr']:.2f}" if plan.get("rr") else "")]
        lines += [f"注：{n}" for n in plan.get("notes") or []]
        if plan.get("capped"):
            lines.append("注：受单笔保证金上限限制，仓位已缩小")
        return "\n".join(lines)

    def close_text(self, t, exit_px=None, events=None) -> str:
        pnl, r = t.get("pnl"), t.get("r_mult")
        if pnl is None:
            res, icon = "盈亏请在交易所 App 查看", "⚪"
        else:
            res = f"{pnl:+.2f}U" + (f"（{r:+.2f}R）" if r is not None else "")
            icon = "✅" if pnl > 0 else "🔴"
        lines = [f"{icon} 平仓 [{MODE_CN[t['mode']]}] #{t['id']} {t['base']} {SIDE_CN[t['side']]}｜{t.get('exit_reason') or ''}"]
        if events:
            lines.append("；".join(events))
        if exit_px:
            lines.append(f"平仓均价 {fmt(exit_px)}")
        lines.append(f"结果：{res}｜来源：{t['title']}")
        return "\n".join(lines)
