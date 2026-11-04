"""频道后补止损时按它加减仓（resize_on_fill）：打到频道的止损正好亏开仓时定的每单风险"""
import asyncio, os, sys, tempfile, types
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

FEE = 0.0005


class Quiet:
    def __init__(self): self.sent = []
    async def send(self, text, markup=None): self.sent.append(text); return True


class FakeEx:
    name, label, maker_capable, has_keys, attached_sl = "gate", "Gate", False, False, False
    def __init__(self, px): self.px = px
    async def last_price(self, symbol): return self.px
    def fees(self, symbol): return (0.0002, FEE)


def setup(px, status="open", side="long", resize=True, margin_pct=None):
    cfg = Config()
    cfg.risk.update(resize_on_fill=resize, stop_loss_r=1.0)
    if margin_pct is not None:
        cfg.risk["max_margin_pct"] = margin_pct
    db = DBM.DB(":memory:")
    eng = E.Engine(cfg, db, FakeEx(px), None, Quiet())
    g = 1 if side == "long" else -1
    e, sl = 1.0, 1.0 - g * 0.08                       # 程序补的止损 8%
    per = abs(e - sl) + 2 * FEE * e
    qty = 100 / per                                    # 每单风险 100U
    t = dict(mode="paper", channel="huhu", title="群", chat_id=-1, msg_id=1, symbol="X/USDT:USDT", base="X", scale=1, side=side,
             status=status, entry_kind="market" if status == "open" else "limit", entry_price=e, qty=qty,
             remaining=qty if status == "open" else 0.0, sl=sl, soft_sl=sl, sl_source="fallback", tp_source="fallback",
             tps=[{"price": e + g * 0.16, "frac": 1.0, "qty": qty if status == "open" else None}], leverage=10,
             risk_usdt=100.0, realized=-qty * e * FEE if status == "open" else 0.0, created_at=0, opened_at=0, be_moved=0)
    t["id"] = db.insert_trade(t)
    return eng, t, cfg.risk_for(cfg.channel_by_username("huhu"))


def loss_at(t, stop):
    """现在打到 stop 一共亏多少（已实现的不算，从现在的仓位算，含平仓手续费）"""
    return t["remaining"] * (abs(t["entry_price"] - stop) + FEE * stop)


CTX = types.SimpleNamespace(title="群")


async def main():
    # 1. 频道的止损更近（4%）：按现价加仓，打到止损正好亏 100U 左右
    eng, t, risk = setup(px=1.005)
    q0 = t["remaining"]
    res = await eng.fill_in(t, {"stop_loss": 0.96, "take_profits": [1.04, 1.08]}, CTX, risk)
    check(f"止损更近 → 加仓（{q0:.0f} → {t['remaining']:.0f}），开仓均价在原价和现价之间",
          t["remaining"] > q0 * 1.8 and 1.0 < t["entry_price"] < 1.005 and t["soft_sl"] == t["sl"] == 0.96, res)
    check(f"打到频道的止损约亏 {t['risk_usdt']:.1f}U（每单风险 100U）", 99 < t["risk_usdt"] < 101)
    check("止盈换成频道的，数量按新的仓位分", [x["price"] for x in t["tps"]] == [1.04, 1.08]
          and abs(sum(x["qty"] for x in t["tps"]) - t["remaining"]) < 1e-6)
    check("加仓的手续费记进已实现盈亏", t["realized"] < -q0 * FEE)
    check("通知里写了加仓", any("加仓" in s and "每单风险 100.00U" in s for s in eng.notifier.sent), eng.notifier.sent)
    eng.db.save_trade(t)
    check("止损来源改成频道的（再补发不会再调）", eng.db.get_trade(t["id"])["sl_source"] == "signal")

    # 2. 频道的止损更远（12%）：按现价减仓，止损放宽到频道的
    eng, t, risk = setup(px=0.99)
    q0, r0 = t["remaining"], t["realized"]
    await eng.fill_in(t, {"stop_loss": 0.88}, CTX, risk)
    check(f"止损更远 → 减仓（{q0:.0f} → {t['remaining']:.0f}），止损放宽到 0.88", t["remaining"] < q0 * 0.7 and t["soft_sl"] == 0.88)
    check(f"打到频道的止损约亏 {t['risk_usdt']:.1f}U", 99 < t["risk_usdt"] < 101)
    check("减掉的那部分按现价平（亏了一点，记进已实现）", t["realized"] < r0)

    # 3. 空单一样
    eng, t, risk = setup(px=0.995, side="short")
    q0 = t["remaining"]
    await eng.fill_in(t, {"stop_loss": 1.04}, CTX, risk)
    check("空单：止损更近 → 加仓", t["remaining"] > q0 * 1.8 and 0.995 < t["entry_price"] < 1.0 and 99 < t["risk_usdt"] < 101)

    # 4. 限价单还没成交：直接改数量
    eng, t, risk = setup(px=1.02, status="pending")
    q0 = t["qty"]
    await eng.fill_in(t, {"stop_loss": 0.96}, CTX, risk)
    check(f"限价单没成交：数量 {q0:.0f} → {t['qty']:.0f}，止损换成频道的", t["qty"] > q0 * 1.8 and t["sl"] == 0.96
          and 99 < t["risk_usdt"] < 101 and t["remaining"] == 0)

    # 5. 保证金不够：少加，通知里说加不满
    eng, t, risk = setup(px=1.005, margin_pct=15)
    await eng.fill_in(t, {"stop_loss": 0.96}, CTX, risk)
    cap = eng.paper_equity() * 0.15 * 10 / 1.005
    check(f"保证金上限：仓位最多 {cap:.0f}，打到止损亏 {t['risk_usdt']:.1f}U（不满 100U）",
          t["remaining"] <= cap + 1e-6 and t["risk_usdt"] < 99 and any("加不满" in s for s in eng.notifier.sent))

    # 6. 现价已经越过频道的止损：直接离场
    eng, t, risk = setup(px=0.955)
    await eng.fill_in(t, {"stop_loss": 0.96}, CTX, risk)
    check("现价已经越过频道补发的止损：直接平仓", eng.db.get_trade(t["id"])["status"] == "closed")

    # 7. 没打开：跟以前一样只收紧止损，1R 变小
    eng, t, risk = setup(px=1.005, resize=False)
    q0 = t["remaining"]
    await eng.fill_in(t, {"stop_loss": 0.96}, CTX, risk)
    check(f"没打开：仓位不变、止损收紧，打到止损只亏 {t['risk_usdt']:.1f}U", t["remaining"] == q0 and t["soft_sl"] == 0.96
          and 45 < t["risk_usdt"] < 55)
    eng, t, risk = setup(px=0.99, resize=False)
    await eng.fill_in(t, {"stop_loss": 0.88}, CTX, risk)
    check("没打开：频道的止损更远就不放宽", t["soft_sl"] == 0.92)

    # 8. 第一止盈已经成交过的不调（已经落袋一部分，按剩下的调容易算乱）
    eng, t, risk = setup(px=1.005)
    t["tps"] = [{"price": 1.004, "frac": 0.5, "qty": t["qty"] / 2, "filled": True}, {"price": 1.16, "frac": 0.5, "qty": t["qty"] / 2}]
    t["remaining"] = t["qty"] / 2
    await eng.fill_in(t, {"stop_loss": 0.96}, CTX, risk)
    check("止盈成交过一部分的：不加减仓，照旧只收紧止损", t["remaining"] == t["qty"] / 2 and t["soft_sl"] == 0.96)
    print(f"\n{ok} passed")

asyncio.run(main())
