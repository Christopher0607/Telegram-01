"""「当天赚够就收工」（daily_profit_stop_pct）：模拟盘所有单子按分钟一起推进，账户当天赚到开盘权益的这么多 % 就全部平仓、当天不再开。
（2026-10-29 改写 monitor_paper 时另外跟旧的逐单推进比过：没打开时 8 组随机行情的结果、通知一模一样）"""
import asyncio, copy, os, random, sys, tempfile
from datetime import datetime, timedelta, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import engine as E
import db as DBM
from config import Config


ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} FAILED {extra}"
    ok += 1; print("✅", name)

TZ = timezone(timedelta(hours=8))
T0 = int(datetime(2026, 10, 20, 0, 0, tzinfo=TZ).timestamp() * 1000)   # 北京时间 0 点
SIM = {"now": T0}
E.now_ms = lambda: SIM["now"]


class Quiet:
    def __init__(self): self.sent = []
    async def send(self, text, markup=None): self.sent.append(text); return True


class FakeEx:
    name, label, maker_capable, has_keys = "gate", "Gate", False, False
    def __init__(self, series): self.series = series
    async def ohlcv(self, symbol, since): return [c for c in self.series[symbol] if c[0] >= since]
    def fees(self, symbol): return (0.0002, 0.0005)


def walk(seed, start, minutes, p0, vol):
    rnd, p, out = random.Random(seed), p0, []
    for i in range(minutes):
        o = p
        p = max(p * (1 + rnd.gauss(0, vol)), p0 * 0.2)
        hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, vol / 2))), min(o, p) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append([start + i * 60000, o, hi, lo, p, 1.0])
    return out


def trade(i, sym, side, status, entry, sl, tps, created, risk=10.0, expires=None):
    qty = risk / abs(entry - sl)
    t = dict(mode="paper", channel="huhu", title="群", chat_id=-1, msg_id=i, symbol=sym, base=sym.split("/")[0], scale=1,
             side=side, status=status, entry_kind="limit" if status == "pending" else "market", entry_price=entry, qty=qty,
             remaining=qty if status == "open" else 0.0, sl=sl, soft_sl=sl,
             tps=[{"price": p, "frac": f, "qty": qty * f if status == "open" else None} for p, f in tps],
             leverage=10, risk_usdt=risk, realized=-qty * entry * 0.0005 if status == "open" else 0.0,
             created_at=created, opened_at=created if status == "open" else None, expires_at=expires, be_moved=0)
    return t


def scenario(seed, n=12):
    rnd = random.Random(seed)
    syms = {f"C{k}/USDT:USDT": walk(seed * 10 + k, T0, 3 * 1440, rnd.uniform(0.5, 50), rnd.uniform(0.001, 0.004)) for k in range(5)}
    ts = []
    for i in range(n):
        sym = rnd.choice(list(syms))
        at = T0 + rnd.randrange(0, 2 * 1440) * 60000
        px = next(c[4] for c in syms[sym] if c[0] >= at)
        side = rnd.choice(["long", "short"])
        d = px * rnd.uniform(0.005, 0.03)
        sgn = 1 if side == "long" else -1
        pending = rnd.random() < 0.3
        entry = px - sgn * d * 0.5 if pending else px
        tps = [(entry + sgn * d * k, f) for k, f in ((1, 0.5), (2, 0.3), (3, 0.2))]
        ts.append(trade(i, sym, side, "pending" if pending else "open", entry, entry - sgn * d, tps, at,
                        expires=at + 4 * 3600_000 if pending else None))
    return syms, ts


async def run(mod, syms, trades, steps, stop_pct=0.0):
    cfg = Config()
    cfg.risk["daily_profit_stop_pct"] = stop_pct
    cfg.risk["breakeven_after_tp1"] = True
    db = DBM.DB(":memory:")
    for t in trades:
        t = copy.deepcopy(t)
        t.pop("id", None)
        db.insert_trade(t)
    q = Quiet()
    eng = mod.Engine(cfg, db, FakeEx(syms), None, q)
    for now in steps:
        SIM["now"] = now
        await eng.monitor_paper()
    rows = [db._row(r) for r in db.conn.execute("SELECT * FROM trades ORDER BY id")]
    return rows, eng, q


KEYS = ("status", "closed_at", "opened_at", "realized", "pnl", "r_mult", "exit_reason", "remaining", "soft_sl", "be_moved",
        "last_candle_ts")


def same(a, b):
    for x, y in zip(a, b):
        for k in KEYS:
            if x[k] != y[k] and not (isinstance(x[k], float) and isinstance(y[k], float) and abs(x[k] - y[k]) < 1e-9):
                return f"#{x['id']} {k}: {x[k]} != {y[k]}"
        if [tp.get("filled") for tp in x["tps"]] != [tp.get("filled") for tp in y["tps"]]:
            return f"#{x['id']} tps"
    return ""


async def main():
    # 2. 两单都在涨：当天账户赚到开盘权益 1%（1000U → 10U）就全部平掉，挂单撤掉，当天不再开
    up = [[T0 + i * 60000, 1 + i * 0.0001, 1 + i * 0.0001 + 0.00005, 1 + i * 0.0001 - 0.00005, 1 + (i + 1) * 0.0001, 1]
          for i in range(3 * 1440)]
    syms = {"UP/USDT:USDT": up, "UP2/USDT:USDT": [list(c) for c in up]}
    tr = [trade(1, "UP/USDT:USDT", "long", "open", 1.0, 0.95, [(2.0, 1.0)], T0, risk=10),
          trade(2, "UP2/USDT:USDT", "long", "open", 1.0, 0.95, [(2.0, 1.0)], T0, risk=10),
          trade(3, "UP/USDT:USDT", "long", "pending", 0.5, 0.45, [(2.0, 1.0)], T0, risk=10, expires=T0 + 86400_000)]
    rows, eng, q = await run(E, syms, tr, [T0 + 600 * 60000], stop_pct=1.0)
    r1, r2, r3 = rows
    gain = sum(r["pnl"] for r in rows if r["status"] == "closed")
    check(f"两单一起赚到开盘权益的 1% 就全部平仓（合计 {gain:+.2f}U）", r1["status"] == r2["status"] == "closed"
          and r1["closed_at"] == r2["closed_at"] and 10 <= gain < 10.5 and "当天赚到总权益 1%，全部平仓收工" == r1["exit_reason"],
          (r1["status"], r1["exit_reason"], gain))
    check("还没成交的限价单撤掉", r3["status"] == "cancelled" and "撤掉挂单" in r3["exit_reason"])
    check("发了收工通知", any("全部平仓，今天不再开新的模拟仓" in s for s in q.sent), q.sent)
    check("当天不再开新的模拟仓（daily_block）", "已经赚到总权益 1%" in (eng.daily_block("paper") or ""))
    check("实盘不受影响", eng.daily_block("live") is None)
    SIM["now"] = T0 + 86400_000 + 60000
    check("第二天 0 点恢复", eng.daily_block("paper") is None)

    # 3. 每一单自己没到 1%，加起来才到：照样收工（看的是整个账户）
    tr = [trade(1, "UP/USDT:USDT", "long", "open", 1.0, 0.95, [(2.0, 1.0)], T0, risk=4),
          trade(2, "UP2/USDT:USDT", "long", "open", 1.0, 0.95, [(2.0, 1.0)], T0, risk=4)]
    rows, eng, q = await run(E, syms, tr, [T0 + 1000 * 60000], stop_pct=1.0)
    check("单子各自赚得不够、合起来到了：照样全部平仓", all(r["status"] == "closed" for r in rows)
          and abs(sum(r["pnl"] for r in rows) - 10) < 0.5)

    # 4. 前一天已经赚的不算今天的；开盘权益 = 本金 + 以前平掉的
    tr = [trade(1, "UP/USDT:USDT", "long", "open", 1.0, 0.95, [(1.05, 1.0)], T0, risk=100)]
    rows, eng, q = await run(E, syms, tr, [T0 + 1440 * 60000 - 60000], stop_pct=50.0)
    first = rows[0]
    check(f"第一天止盈赚了 {first['pnl']:+.0f}U，没到 50%：不收工", first["exit_reason"] == "止盈离场"
          and eng.db.kv_get("profit_stop:paper") is None)
    print(f"\n{ok} passed")

asyncio.run(main())
