"""Bitget USDT 永续合约接口（基于 ccxt，经典账户和统一账户 UTA 都支持）。

约定：
- 单向持仓模式（one-way），一个币种同一时间只有一个方向的仓位
- 开仓单自带交易所止损（preset stop loss），程序挂掉也有止损保护
- 交易所里不挂止盈限价单（会冻结仓位），止盈由 engine 盯盘后用「只减仓」市价单执行
"""
from __future__ import annotations

import asyncio
import logging
import time

import ccxt.async_support as ccxt
from ccxt.base.decimal_to_precision import TICK_SIZE

log = logging.getLogger("exchange")
PRODUCT = "USDT-FUTURES"


class Exchange:
    def __init__(self, cfg):
        self.cfg = cfg
        params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        self.has_keys = bool(cfg.bitget_key and cfg.bitget_secret and cfg.bitget_passphrase)
        if self.has_keys:
            params.update(apiKey=cfg.bitget_key, secret=cfg.bitget_secret, password=cfg.bitget_passphrase)
        self.ex = ccxt.bitget(params)
        self.uta = False
        self._tiers: dict = {}

    async def init(self):
        await self.ex.load_markets()
        if not self.has_keys:
            log.info("未配置 Bitget API Key：只能跑模拟盘（行情用公开接口）")
            return
        self.uta, _ = await self.ex.handle_uta_and_params({}, "init", False)
        log.info("Bitget 账户类型：%s", "统一账户 UTA" if self.uta else "经典账户")
        if not self.uta:
            try:
                await self.ex.set_position_mode(False, None, {"productType": PRODUCT})
            except Exception as e:  # 已经是单向模式、或者有持仓时改不了
                log.info("设置单向持仓模式：%s", str(e)[:150])

    async def close(self):
        await self.ex.close()

    # ---------------- 市场信息 ----------------
    def resolve(self, base: str) -> tuple[str | None, float]:
        """币种 → ccxt 合约代码。有些币在交易所是 1000 倍合约（如 1000PEPE），此时价格要 ×1000。"""
        base = (base or "").upper()
        for b, scale in ((base, 1.0), ("1000" + base, 1000.0)):
            sym = f"{b}/USDT:USDT"
            m = self.ex.markets.get(sym)
            if m and m.get("swap") and m.get("linear") and m.get("active") is not False:
                return sym, scale
        return None, 1.0

    def max_leverage(self, symbol: str) -> float | None:
        return ((self.ex.market(symbol).get("limits") or {}).get("leverage") or {}).get("max")

    async def leverage_tiers(self, symbol: str) -> list | None:
        """Bitget 的仓位档位：每档的最高杠杆和维持保证金率（公开接口，缓存 6 小时）。"""
        hit = self._tiers.get(symbol)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            tiers = await self.ex.fetch_market_leverage_tiers(symbol)
        except Exception as e:
            log.info("查询 %s 杠杆档位失败：%s（按保守的 2%% 维持保证金率计算）", symbol, str(e)[:120])
            tiers = None
        self._tiers[symbol] = (time.time(), tiers)
        return tiers

    def _contract_size(self, symbol: str) -> float:
        return float(self.ex.market(symbol).get("contractSize") or 1)

    def round_qty(self, symbol: str, qty: float) -> float:
        """数量（币本位）按交易所精度向下取整。"""
        cs = self._contract_size(symbol)
        try:
            return float(self.ex.amount_to_precision(symbol, qty / cs * (1 + 1e-9))) * cs
        except Exception:
            return 0.0

    def qty_step(self, symbol: str) -> float:
        """最小数量步长（币本位）。"""
        p = (self.ex.market(symbol).get("precision") or {}).get("amount")
        if p is None:
            return 1e-8
        step = float(p) if self.ex.precisionMode == TICK_SIZE else 10 ** (-int(p))
        return step * self._contract_size(symbol)

    def meets_min(self, symbol: str, qty: float, price: float) -> bool:
        m = self.ex.market(symbol)
        lim = m.get("limits") or {}
        amin = (lim.get("amount") or {}).get("min")
        cmin = (lim.get("cost") or {}).get("min") or 5.0
        cs = self._contract_size(symbol)
        if qty <= 0:
            return False
        if amin and qty / cs < amin:
            return False
        return qty * price >= cmin

    def _amt(self, symbol: str, qty: float) -> float:
        return qty / self._contract_size(symbol)

    # ---------------- 行情 ----------------
    async def ticker(self, symbol: str) -> dict:
        t = await self.ex.fetch_ticker(symbol)
        return {"last": t.get("last") or t.get("close"), "quoteVolume": t.get("quoteVolume")}

    async def last_price(self, symbol: str) -> float:
        return (await self.ticker(symbol))["last"]

    async def last_prices(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        if len(symbols) == 1:
            return {symbols[0]: await self.last_price(symbols[0])}
        ts = await self.ex.fetch_tickers(symbols)
        return {s: (ts.get(s) or {}).get("last") for s in symbols}

    async def ohlcv(self, symbol: str, since_ms: int) -> list:
        return await self.ex.fetch_ohlcv(symbol, "1m", since=since_ms, limit=200)

    async def candles(self, symbol: str, timeframe: str = "1h", limit: int = 40) -> list:
        return await self.ex.fetch_ohlcv(symbol, timeframe, limit=limit)

    # ---------------- 账户 ----------------
    async def balance(self) -> tuple[float, float]:
        b = await self.ex.fetch_balance({"type": "swap"})
        u = b.get("USDT") or {}
        return float(u.get("total") or 0), float(u.get("free") or 0)

    async def equity(self) -> float:
        return (await self.balance())[0]

    async def positions(self) -> dict:
        """{symbol: {side, size(币数量), entry, liq}}"""
        out = {}
        for p in await self.ex.fetch_positions(None, {"productType": PRODUCT}):
            c = float(p.get("contracts") or 0)
            if c <= 0:
                continue
            cs = float(p.get("contractSize") or 1)
            out[p["symbol"]] = {"side": p.get("side"), "size": c * cs,
                                "entry": float(p.get("entryPrice") or 0),
                                "liq": p.get("liquidationPrice")}
        return out

    async def wait_position(self, symbol: str, tries: int = 6) -> dict | None:
        for _ in range(tries):
            await asyncio.sleep(0.7)
            p = (await self.positions()).get(symbol)
            if p:
                return p
        return None

    # ---------------- 下单 ----------------
    def _params(self, extra: dict | None = None) -> dict:
        p = {} if self.uta else {"marginMode": self.cfg.margin_mode}
        if extra:
            p.update(extra)
        return p

    async def prepare(self, symbol: str, leverage: int, side: str):
        """设置保证金模式和杠杆。杠杆设置失败会抛异常（不知道实际杠杆就不开仓）。"""
        if not self.uta:
            try:
                await self.ex.set_margin_mode(self.cfg.margin_mode, symbol)
            except Exception as e:
                log.info("设置保证金模式 %s：%s", symbol, str(e)[:120])
        attempts = [{}]
        if not self.uta and self.cfg.margin_mode == "isolated":
            attempts.append({"holdSide": "long" if side == "long" else "short"})
        last = None
        for p in attempts:
            try:
                await self.ex.set_leverage(int(leverage), symbol, p)
                return
            except Exception as e:
                last = e
        raise RuntimeError(f"设置杠杆失败：{last}")

    async def open_market(self, symbol: str, side: str, qty: float, sl: float) -> str:
        o = await self.ex.create_order(symbol, "market", "buy" if side == "long" else "sell",
                                       self._amt(symbol, qty), None,
                                       self._params({"stopLoss": {"triggerPrice": sl}}))
        return str(o.get("id"))

    async def open_limit(self, symbol: str, side: str, qty: float, price: float, sl: float) -> str:
        o = await self.ex.create_order(symbol, "limit", "buy" if side == "long" else "sell",
                                       self._amt(symbol, qty), price,
                                       self._params({"stopLoss": {"triggerPrice": sl}}))
        return str(o.get("id"))

    async def reduce_market(self, symbol: str, side: str, qty: float):
        return await self.ex.create_order(symbol, "market", "sell" if side == "long" else "buy",
                                          self._amt(symbol, qty), None, self._params({"reduceOnly": True}))

    async def cancel(self, symbol: str, order_id: str | None):
        if not order_id:
            return
        try:
            await self.ex.cancel_order(order_id, symbol)
        except Exception as e:  # 已成交/已撤销
            log.info("撤单 %s %s：%s", symbol, order_id, str(e)[:120])

    async def order_status(self, symbol: str, order_id: str) -> dict:
        o = await self.ex.fetch_order(order_id, symbol)
        cs = self._contract_size(symbol)
        return {"status": o.get("status"), "filled": float(o.get("filled") or 0) * cs,
                "average": o.get("average")}

    async def closed_pnl(self, symbol: str, since_ms: int) -> tuple[float | None, float | None]:
        """最近一次平仓的净盈亏（含手续费）和平仓均价；查不到返回 (None, None)。"""
        try:
            hist = await self.ex.fetch_positions_history([symbol], since_ms, 20)
            if not hist:
                return None, None
            p = max(hist, key=lambda x: x.get("lastUpdateTimestamp") or x.get("timestamp") or 0)
            info = p.get("info") or {}
            net = info.get("netProfit")
            pnl = float(net) if net not in (None, "") else p.get("realizedPnl")
            return (float(pnl) if pnl is not None else None), p.get("lastPrice")
        except Exception as e:
            log.info("查询平仓盈亏失败：%s", str(e)[:150])
            return None, None
