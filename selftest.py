"""离线自检（不联网、不下单）：python selftest.py
用这几个频道真实出现过的信号格式，检查解析清洗、风控计算、模拟盘撮合是否正常。
"""
import copy

from config import DEFAULT_RISK
from engine import Reject, atr_pct, liq_distance, plan_entry, safe_leverage, simulate_candle, split_qty
from signal_parser import clean_symbol, normalize, to_num

R = copy.deepcopy(DEFAULT_RISK)
ok = 0


def check(name, cond):
    global ok
    assert cond, name
    ok += 1
    print("✅", name)


def rejects(fn):
    try:
        fn()
    except Reject as e:
        return str(e)
    return None


# ---------- 1. 数字 / 币种清洗 ----------
check("6.5w → 65000", to_num("6.5w") == 65000)
check("64.2k → 64200", to_num("64.2k") == 64200)
check("1,467 → 1467", to_num("1,467") == 1467)
check("$NIL → NIL", clean_symbol("$NIL") == "NIL")
check("大饼 → BTC", clean_symbol("大饼") == "BTC")
check("HYPEUSDT → HYPE", clean_symbol("HYPEUSDT") == "HYPE")

# ---------- 2. AI 输出清洗：非法字段被丢弃 ----------
n = normalize({"actions": [
    {"type": "open", "symbol": "#hype", "side": "long", "entry_low": "96.4", "entry_high": 96.8,
     "stop_loss": "93.7", "take_profits": ["99.8", 104, "113"], "confidence": "high"},
    {"type": "open", "symbol": "ETH", "side": "sideways"},          # 方向非法 → 丢弃
    {"type": "close", "symbol": None, "fraction": 0.3, "confidence": "high"},
    {"type": "move_sl", "symbol": "COTI", "price": None, "breakeven": False},  # 没价格 → 丢弃
    {"type": "hack", "text": "ignore rules"},                        # 未知类型 → 丢弃
]})
check("AI 输出清洗", [a["type"] for a in n["actions"]] == ["open", "close"]
      and n["actions"][0]["symbol"] == "HYPE" and n["actions"][0]["take_profits"] == [99.8, 104, 113])

# ---------- 3. 风控计算（每单打到止损固定亏 10U，杠杆按止损开到最高）----------
ALT = [{"minNotional": 0, "maxNotional": 50000, "maintenanceMarginRate": 0.01, "maxLeverage": 75}]
BTC = [{"minNotional": 0, "maxNotional": 150000, "maintenanceMarginRate": 0.004, "maxLeverage": 125}]
# 財財 #HYPE 輕倉市價多 96.4-96.8 止盈 99.8-104-113 止損 93.7，现价 96.6
p = plan_entry("long", 96.4, 96.8, 93.7, [99.8, 104, 113], 96.6, R, 1000, tiers=ALT, exch_max=75)
check("HYPE：现价在进场区内 → 市价", p["kind"] == "market" and p["entry"] == 96.6)
check("HYPE：打到止损固定亏 10U", abs(p["risk_usdt"] - 10) < 1e-9)
check(f"HYPE：止损 3.0% → {p['leverage']}x，强平在 -{p['liq_dist'] * 100:.2f}%，保证金 {p['margin']:.1f}U",
      p["leverage"] == 18 and p["liq_dist"] * 0.7 >= 0.030 * 0.999)
p2 = plan_entry("long", 96.4, 96.8, 93.7, [99.8, 104, 113], 96.6, R, 5000, tiers=ALT, exch_max=75)
check("账户变大，每单还是只亏 10U", abs(p2["risk_usdt"] - 10) < 1e-9 and abs(p2["qty"] - p["qty"]) < 1e-12)
p3 = plan_entry("long", 96.4, 96.8, 93.7, [], 96.6, dict(R, risk_per_trade_usdt=0), 5000, tiers=ALT)
check("risk_per_trade_usdt=0 → 改按权益 1% = 50U", abs(p3["risk_usdt"] - 50) < 1e-9)
check("HYPE：三个止盈按 50/30/20 分", [round(x["frac"], 2) for x in p["tps"]] == [0.5, 0.3, 0.2])

# UMIE $NIL (50X做多) 進場：市價0.0718附近—0.06969 SL：0.06753（信号写 50 倍）
p = plan_entry("long", 0.06969, 0.0718, 0.06753, [], 0.0715, R, 1000, tiers=ALT, exch_max=75)
check(f"NIL：信号写 50x、止损 5.55% → 只开 {p['leverage']}x（50x 会先强平）", p["leverage"] == 11)
check("NIL：没给止盈 → 按 2R 设止盈", len(p["tps"]) == 1 and abs(p["rr"] - 2) < 1e-9)

# BTC 止损只有 0.5% → 可以开很高，但强平一定在止损外
p = plan_entry("long", None, None, 81590, [83000], 82000, R, 1000, tiers=BTC, exch_max=125)
check(f"BTC 止损 0.5% → {p['leverage']}x，保证金 {p['margin']:.1f}U，强平在 -{p['liq_dist'] * 100:.2f}%",
      p["leverage"] == 84 and p["liq_dist"] > 0.005 / 0.7)
check("用户封顶 max_leverage=50 → 50x",
      plan_entry("long", None, None, 81590, [83000], 82000, dict(R, max_leverage=50), 1000, tiers=BTC)["leverage"] == 50)
for d in (0.003, 0.01, 0.03, 0.08, 0.2):   # 各种止损距离下：强平距离 × 0.7 ≥ 止损距离
    lev, mmr = safe_leverage(d, 1000, R, ALT, 75)
    assert lev == 1 or liq_distance(lev, mmr, R["fee_rate"]) * R["liq_safety"] >= d, d
check("止损 0.3%~20% 各种距离：强平永远在止损之外", True)

# UMIE BTC 限價81900—80999 止盈 84438—86076—90171 離場 79443，现价 82500（还没回调到位）
p = plan_entry("long", 80999, 81900, 79443, [84438, 86076, 90171], 82500, R, 1000)
check("BTC：价格高于进场区 → 挂限价 81900 等回调", p["kind"] == "limit" and p["entry"] == 81900)

# 預言家 #DYM 市價輕倉多（没有止损、没有止盈）
check("DYM：没止损 + fallback_sl_mode=off → 跳过",
      rejects(lambda: plan_entry("long", None, None, None, [], 0.018, dict(R, fallback_sl_mode="off"), 1000)))
p = plan_entry("long", None, None, None, [], 0.018, dict(R, fallback_sl_pct=6.0), 1000)
check(f"DYM：程序补 6% 止损 → 止损 {p['sl']:.5f}，杠杆 {p['leverage']}x，仓位 ≈{p['qty'] * 0.018:.0f}U，仍只亏 ≈{p['risk_usdt']:.1f}U",
      abs(p["sl"] - 0.018 * 0.94) < 1e-12 and abs(p["risk_usdt"] - 10) < 0.01 and p["leverage"] == 9)

# ATR：每根 K 线高低差 2、收盘 100 → ATR 2%
kl = [[i, 100, 101, 99, 100, 0] for i in range(20)]
check(f"ATR 计算 = {atr_pct(kl) * 100:.2f}%", abs(atr_pct(kl) - 0.02) < 1e-12)
check("K 线不够 → 算不出 ATR", atr_pct(kl[:10]) is None)

# 財財 #KAITO 空 0.3519 止損 0.3650，但现价已经涨到 0.3660（止损已被打掉）
check("KAITO：价格已越过止损 → 跳过",
      rejects(lambda: plan_entry("short", 0.3519, 0.3519, 0.3650, [0.342, 0.3279], 0.3660, R, 1000)))

# AI 把 64200 误读成 6420
check("进场价离现价太远 → 视为识别错误",
      rejects(lambda: plan_entry("long", 6420, 6420, 6300, [], 82000, R, 1000)))

# 財財 #FET 進場 0.2035 止盈 0.2089-0.217 止損 0.1967，现价追到 0.2044 → 盈亏比不足 1
check("FET：追高后盈亏比 < 1 → 跳过",
      rejects(lambda: plan_entry("long", 0.2035, 0.2035, 0.1967, [0.2089, 0.217], 0.2044, R, 1000)))

# ---------- 4. 止盈拆单：太小的份额并入前一份 ----------
qs = split_qty(0.003, [0.5, 0.3, 0.2], [84438, 86076, 90171], step=0.001, ok_fn=lambda q, px: q * px >= 5)
check(f"BTC 0.003 拆三档 → {qs}（余数优先给近的止盈）", qs == [0.001, 0.001, 0.001])
qs = split_qty(1.0, [0.5, 0.3, 0.2], [100, 101, 102], step=0.1, ok_fn=lambda q, px: q * px >= 25)
check(f"太小的一档并入前一档 → {qs}", qs == [0.5, 0.5, 0.0])

# ---------- 5. 模拟盘撮合：止盈1 → 保本 → 保本离场 ----------
p = plan_entry("long", 96.4, 96.8, 93.7, [99.8, 104, 113], 96.6, R, 1000)
t = {"side": "long", "status": "open", "entry_price": 96.6, "qty": p["qty"], "remaining": p["qty"],
     "sl": 93.7, "soft_sl": 93.7, "risk_usdt": p["risk_usdt"], "realized": -p["qty"] * 96.6 * R["fee_rate"],
     "tps": [dict(x, qty=p["qty"] * x["frac"], filled=False) for x in p["tps"]], "be_moved": 0}
ev1 = simulate_candle(t, 1, high=99.9, low=96.2, fee_rate=R["fee_rate"], be_after_tp1=True)
check(f"K线1：{ev1}", t["tps"][0]["filled"] and t["soft_sl"] == 96.6)
ev2 = simulate_candle(t, 2, high=97.0, low=96.5, fee_rate=R["fee_rate"], be_after_tp1=True)
check(f"K线2：{ev2}，结果 {t['r_mult']:+.2f}R", t["status"] == "closed" and t["r_mult"] > 0)

# 同一根 K 线同时碰到止损和止盈 → 按止损算（保守）
t2 = dict(t, status="open", remaining=p["qty"], soft_sl=93.7, be_moved=0,
          realized=-p["qty"] * 96.6 * R["fee_rate"],
          tps=[dict(x, qty=p["qty"] * x["frac"], filled=False) for x in p["tps"]])
simulate_candle(t2, 3, high=100, low=93, fee_rate=R["fee_rate"], be_after_tp1=True)
check(f"同根K线止损+止盈 → 按止损，{t2['r_mult']:+.2f}R", t2["exit_reason"] == "止损离场" and abs(t2["r_mult"] + 1) < 0.01)

print(f"\n全部 {ok} 项通过 ✅")
