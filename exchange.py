"""交易所 USDT 永续合约接口（基于 ccxt）：Bitget（经典账户和统一账户 UTA）、Gate 或 WEEX，由 config.yaml 的 exchange.name 决定。

约定：
- 一个币种同一时间只有一个方向的仓位（Bitget/Gate 用单向持仓模式；WEEX 多空分开记，程序不会同时开两边）
- 交易所里一定有止损，程序挂掉也有保护：
    Bitget：开仓单自带止损（preset stop loss）
    Gate / WEEX：持仓出现后另挂一张「平掉整个仓位」的止损触发单（place_sl），
          平仓时由 engine 撤掉（cancel_sl）
- 交易所里不挂止盈限价单（会冻结仓位），止盈由 engine 盯盘后用「只减仓」市价单执行
- 数量一律用币本位（BTC 个数）；Gate 按张下单，这里负责换算
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import ccxt.async_support as ccxt
from ccxt.base.decimal_to_precision import TICK_SIZE

log = logging.getLogger("exchange")
PRODUCT = "USDT-FUTURES"          # Bitget 的 USDT 永续
LABELS = {"bitget": "Bitget", "gate": "Gate", "weex": "WEEX"}
GATE_MAX_LEVERAGE = 100           # ccxt 给 Gate 设杠杆最多只接受 100 倍
WEEX_API = "https://api-contract.weex.com/capi/v3"
WEEX_OPEN_SL = ("NEW", "PENDING", "UNTRIGGERED")   # WEEX 止损单还挂着（没触发、没撤销）的状态


class Exchange:
    def __init__(self, cfg, name: str | None = None):
        """name 不填就用 config.yaml 的 exchange.name（/weextest 这类测试可以临时指定别的交易所）。"""
        self.cfg = cfg
        self.name = name or getattr(cfg, "exchange_name", "bitget")
        if self.name not in LABELS:
            raise ValueError(f"config.yaml 里 exchange.name 只能是 {' / '.join(LABELS)}，现在是 {self.name}")
        self.label = LABELS[self.name]
        params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        if self.name == "gate":
            self.has_keys = bool(cfg.gate_key and cfg.gate_secret)
            if self.has_keys:
                params.update(apiKey=cfg.gate_key, secret=cfg.gate_secret)
            self.ex = ccxt.gate(params)
        elif self.name == "weex":
            self.has_keys = bool(cfg.weex_key and cfg.weex_secret and cfg.weex_passphrase)
            if self.has_keys:
                params.update(apiKey=cfg.weex_key, secret=cfg.weex_secret, password=cfg.weex_passphrase)
            self.ex = ccxt.weex(params)
        else:
            self.has_keys = bool(cfg.bitget_key and cfg.bitget_secret and cfg.bitget_passphrase)
            if self.has_keys:
                params.update(apiKey=cfg.bitget_key, secret=cfg.bitget_secret, password=cfg.bitget_passphrase)
            self.ex = ccxt.bitget(params)
        # Bitget 的止损跟着开仓单一起下；Gate、WEEX 要在持仓出现后单独挂止损触发单
        self.attached_sl = self.name == "bitget"
        self.uta = False
        self._tiers: dict = {}

    async def init(self):
        await self.ex.load_markets()
        if not self.has_keys:
            log.info("未配置 %s API Key：只能跑模拟盘（行情用公开接口）", self.label)
            return
        if self.name == "weex":  # WEEX 的保证金模式、持仓合并方式按币设置，开仓前在 prepare 里设
            return
        if self.name == "gate":
            try:
                await self.ex.set_position_mode(False, None, {"settle": "usdt"})
            except Exception as e:  # 已经是单向模式、或者有持仓/挂单时改不了
                log.info("设置单向持仓模式：%s", str(e)[:150])
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
        """币种 → ccxt 合约代码。有些币在交易所是 1000 倍合约（如 1000PEPE），此时价格要 ×1000；
        反过来频道写 1000PEPE、交易所只有 PEPE 时，价格要 ÷1000。"""
        base = (base or "").upper()
        cands = [(base, 1.0), ("1000" + base, 1000.0)]
        if base.startswith("1000") and len(base) > 4:
            cands.append((base[4:], 0.001))
        for b, scale in cands:
            sym = f"{b}/USDT:USDT"
            m = self.ex.markets.get(sym)
            if m and m.get("swap") and m.get("linear") and m.get("active") is not False:
                return sym, scale
        return None, 1.0

    def has_symbol(self, symbol: str) -> bool:
        return symbol in (self.ex.markets or {})

    def min_qty(self, symbol: str) -> float:
        """最小下单数量（币本位）。"""
        amin = ((self.ex.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0
        return max(self.qty_step(symbol), float(amin) * self._contract_size(symbol))

    def max_leverage(self, symbol: str) -> float | None:
        mx = ((self.ex.market(symbol).get("limits") or {}).get("leverage") or {}).get("max")
        if self.name == "gate":
            return min(float(mx), GATE_MAX_LEVERAGE) if mx else GATE_MAX_LEVERAGE
        return mx

    async def leverage_tiers(self, symbol: str) -> list | None:
        """交易所的仓位档位：每档的最高杠杆和维持保证金率（公开接口，缓存 6 小时）。"""
        hit = self._tiers.get(symbol)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            tiers = await (self._weex_tiers(symbol) if self.name == "weex" else self.ex.fetch_market_leverage_tiers(symbol))
        except Exception as e:
            log.info("查询 %s 杠杆档位失败：%s（按保守的 2%% 维持保证金率计算）", symbol, str(e)[:120])
            tiers = None
        self._tiers[symbol] = (time.time(), tiers)
        return tiers

    async def _weex_tiers(self, symbol: str) -> list:
        """WEEX 的风险档位（ccxt 没有封装，直接查公开接口）。"""
        r = await self.ex.fetch(f"{WEEX_API}/market/riskLimits?symbol={self.ex.market(symbol)['id']}")
        return [{"minNotional": float(b["notionalFloor"]), "maxNotional": float(b["notionalCap"]),
                 "maintenanceMarginRate": float(b["maintMarginRatio"]), "maxLeverage": float(b["initialLeverage"])}
                for b in sorted(r[0]["brackets"], key=lambda b: int(b["bracket"]))]

    def _contract_size(self, symbol: str) -> float:
        """ccxt 下单数量的单位：Gate 是「张」，要乘合约面值换成币；WEEX、Bitget 本来就是币的个数
        （WEEX 的 contractSize 只是数量步长，不能拿来换算，否则会差上万倍）。"""
        if self.name == "weex":
            return 1.0
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
        if self.name == "weex":  # ccxt 把这个接口按 200 的额度算，调一次后面所有请求都要等 4 秒，所以直接查
            t = (await self.ex.fetch(f"{WEEX_API}/market/ticker/24hr?symbol={self.ex.market(symbol)['id']}"))[0]
            return {"last": float(t["lastPrice"]), "quoteVolume": float(t["quoteVolume"])}
        t = await self.ex.fetch_ticker(symbol)
        return {"last": t.get("last") or t.get("close"), "quoteVolume": t.get("quoteVolume")}

    async def last_price(self, symbol: str) -> float:
        if self.name == "weex":  # 最新成交价 = 当前这根 1 分钟 K 线的收盘价（额度只算 1）
            return float((await self.ex.fetch_ohlcv(symbol, "1m", limit=1))[-1][4])
        return (await self.ticker(symbol))["last"]

    async def last_prices(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        if len(symbols) == 1 or self.name == "weex":
            return {s: await self.last_price(s) for s in symbols}
        ts = await self.ex.fetch_tickers(symbols)
        return {s: (ts.get(s) or {}).get("last") for s in symbols}

    async def ohlcv(self, symbol: str, since_ms: int) -> list:
        if self.name == "weex":
            # WEEX 的普通 K 线接口不管 since，只给最近 200 根；落后 3 小时以上改用历史接口，从 since 开始一次补 99 根
            if since_ms < self.ex.milliseconds() - 180 * 60000:
                return await self.ex.fetch_ohlcv(symbol, "1m", since_ms, 99, {"historical": True})
            return await self.ex.fetch_ohlcv(symbol, "1m", None, 200)
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
        params = {"settle": "usdt"} if self.name == "gate" else {} if self.name == "weex" else {"productType": PRODUCT}
        out = {}
        for p in await self.ex.fetch_positions(None, params):
            c = float(p.get("contracts") or 0)
            if c <= 0:
                continue
            # 不能用 p["contractSize"]：ccxt 会把 WEEX 的数量步长填进去，乘上以后仓位小了 10 倍、上万倍
            cs = self._contract_size(p["symbol"])
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
        p = {} if (self.uta or self.name in ("gate", "weex")) else {"marginMode": self.cfg.margin_mode}
        if extra:
            p.update(extra)
        return p

    async def prepare(self, symbol: str, leverage: int, side: str):
        """设置保证金模式和杠杆。杠杆设置失败会抛异常（不知道实际杠杆就不开仓）。"""
        if self.name == "weex":
            # 逐仓/全仓 + 多空各自合并成一个仓位（COMBINED）；已经是这个模式、或者有持仓时会报错，不影响
            try:
                await self.ex.set_margin_mode(self.cfg.margin_mode, symbol, {"separatedType": "COMBINED"})
            except Exception as e:
                log.info("设置保证金模式 %s：%s", symbol, str(e)[:120])
            try:
                await self.ex.set_leverage(int(leverage), symbol, {"marginMode": self.cfg.margin_mode})
                return
            except Exception as e:
                raise RuntimeError(f"设置杠杆失败：{e}")
        if self.name == "gate":
            # Gate 没有单独的「保证金模式」开关：杠杆填数字 = 逐仓；杠杆 0 + cross_leverage_limit = 全仓
            lev = int(min(leverage, GATE_MAX_LEVERAGE))
            p = {"cross_leverage_limit": lev} if self.cfg.margin_mode == "cross" else {}
            try:
                await self.ex.set_leverage(lev, symbol, p)
                return
            except Exception as e:
                raise RuntimeError(f"设置杠杆失败：{e}")
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

    def _sl_params(self, sl: float) -> dict:
        """Bitget：开仓单上直接带止损。Gate 不支持，止损由 place_sl 单独挂。"""
        return self._params({"stopLoss": {"triggerPrice": sl}}) if self.attached_sl else self._params()

    async def open_market(self, symbol: str, side: str, qty: float, sl: float) -> str:
        o = await self.ex.create_order(symbol, "market", "buy" if side == "long" else "sell",
                                       self._amt(symbol, qty), None, self._sl_params(sl))
        return str(o.get("id"))

    async def open_limit(self, symbol: str, side: str, qty: float, price: float, sl: float) -> str:
        o = await self.ex.create_order(symbol, "limit", "buy" if side == "long" else "sell",
                                       self._amt(symbol, qty), price, self._sl_params(sl))
        return str(o.get("id"))

    async def place_sl(self, symbol: str, side: str, sl: float) -> str:
        """Gate / WEEX：给现有持仓挂止损触发单。价格（最新成交价）触及 sl 时，以市价平掉这个币的整个仓位，
        所以程序之后分批止盈、减仓都不用改它。返回触发单 id。"""
        m = self.ex.market(symbol)
        long = side == "long"
        if self.name == "weex":
            # 不填 quantity = 给整个仓位设止损；不填 executePrice = 触发后市价平
            r = await self.ex.contractPrivatePostCapiV3PlaceTpSlOrder({
                "symbol": m["id"], "clientAlgoId": f"tgst-sl-{uuid.uuid4().hex[:16]}", "planType": "STOP_LOSS",
                "triggerPrice": self.ex.price_to_precision(symbol, sl), "positionSide": "LONG" if long else "SHORT",
                "triggerPriceType": "CONTRACT_PRICE"})
            res = (r[0] if isinstance(r, list) and r else r) or {}
            if not res.get("success") or not res.get("orderId"):
                raise RuntimeError(f"WEEX 没接受止损单：{res.get('errorCode')} {res.get('errorMessage') or str(r)[:150]}")
            return str(res["orderId"])
        r = await self.ex.privateFuturesPostSettlePriceOrders({
            "settle": "usdt",
            "initial": {"contract": m["id"], "size": 0, "price": "0", "tif": "ioc", "close": True, "text": "t-tgst-sl"},
            "trigger": {"strategy_type": 0, "price_type": 0, "price": self.ex.price_to_precision(symbol, sl),
                        "rule": 2 if long else 1},   # 1: 价格 ≥ 触发价（空单止损）；2: 价格 ≤ 触发价（多单止损）
            "order_type": "close-long-position" if long else "close-short-position",
        })
        oid = r.get("id") if isinstance(r, dict) else None
        if not oid:
            raise RuntimeError(f"交易所没有返回止损单 id：{str(r)[:150]}")
        return str(oid)

    async def cancel_sl(self, symbol: str, sl_order_id: str | None):
        """撤掉程序挂的止损触发单（已经触发或已撤销的会报错，忽略即可）。Bitget 的止损跟着仓位走，不需要撤。"""
        if not sl_order_id or self.attached_sl:
            return
        try:
            if self.name == "weex":
                await self.ex.contractPrivateDeleteCapiV3AlgoOrder({"orderId": sl_order_id})
            else:
                await self.ex.privateFuturesDeleteSettlePriceOrdersOrderId({"settle": "usdt", "order_id": sl_order_id})
        except Exception as e:
            log.info("撤止损单 %s %s：%s", symbol, sl_order_id, str(e)[:120])

    async def sl_info(self, symbol: str, sl_order_id: str) -> dict:
        """止损触发单现在的状态（实盘接口测试用）：{"open": 还挂着吗, "status", "trigger": 触发价, "detail": 说明}"""
        if self.name == "weex":
            orders = await self.ex.contractPrivateGetCapiV3OpenAlgoOrders({"symbol": self.ex.market(symbol)["id"]})
            o = next((x for x in orders or [] if str(x.get("algoId")) == str(sl_order_id)), None)
            if not o:
                return {"open": False, "status": "已不在挂单列表（已触发或已撤销）", "trigger": None, "detail": ""}
            st = str(o.get("algoStatus"))
            return {"open": st in WEEX_OPEN_SL, "status": st, "trigger": o.get("triggerPrice"),
                    "detail": f"{o.get('orderType')}，平掉整个仓位：{'是' if o.get('closePosition') else '否'}"}
        o = await self.ex.privateFuturesGetSettlePriceOrdersOrderId({"settle": "usdt", "order_id": sl_order_id})
        trig = o.get("trigger") or {}
        return {"open": o.get("status") == "open", "status": o.get("status"), "trigger": trig.get("price"),
                "detail": f"规则 {trig.get('rule')}（2 = 价格≤触发价，1 = 价格≥触发价）"}

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
        st = o.get("status")
        if self.name == "weex" and st in ("pending", "canceling"):  # 等待生效 / 正在撤：都还算挂着
            st = "open"
        return {"status": st, "filled": float(o.get("filled") or 0) * cs, "average": o.get("average")}

    async def closed_pnl(self, symbol: str, since_ms: int) -> tuple[float | None, float | None]:
        """最近一次平仓的净盈亏（含手续费）和平仓均价；查不到返回 (None, None)。"""
        if self.name == "weex":
            return await self._weex_closed_pnl(symbol, since_ms)
        try:
            hist = await self.ex.fetch_positions_history([symbol], since_ms, 20)
            if not hist:
                return None, None
            p = max(hist, key=lambda x: x.get("lastUpdateTimestamp") or x.get("timestamp") or 0)
            info = p.get("info") or {}
            if self.name == "gate":
                # pnl = 仓位盈亏 + 资金费 + 手续费；平多的均价在 short_price，平空的在 long_price
                pnl = info.get("pnl")
                px = info.get("short_price") if info.get("side") == "long" else info.get("long_price")
                return (float(pnl) if pnl not in (None, "") else None), (float(px) if px not in (None, "", "0") else None)
            net = info.get("netProfit")
            pnl = float(net) if net not in (None, "") else p.get("realizedPnl")
            return (float(pnl) if pnl is not None else None), p.get("lastPrice")
        except Exception as e:
            log.info("查询平仓盈亏失败：%s", str(e)[:150])
            return None, None

    async def _weex_closed_pnl(self, symbol: str, since_ms: int) -> tuple[float | None, float | None]:
        """WEEX 没有「历史仓位」接口：把开仓以来这个币的成交记录加起来，净盈亏 = 已实现盈亏 - 手续费。
        （这个接口一次最多查 7 天，持仓超过 7 天的单子会漏掉开仓那笔的手续费，影响很小）"""
        try:
            now = int(time.time() * 1000)
            rows = await self.ex.contractPrivateGetCapiV3UserTrades({
                "symbol": self.ex.market(symbol)["id"], "startTime": max(int(since_ms), now - 7 * 86400000 + 60000),
                "endTime": now, "limit": 100})
            if not rows:
                return None, None
            pnl = sum(float(r.get("realizedPnl") or 0) - float(r.get("commission") or 0) for r in rows)
            # 平仓成交：平多是卖（SELL + LONG），平空是买（BUY + SHORT）
            closes = [r for r in rows if (r.get("side") == "SELL") == (r.get("positionSide") == "LONG")]
            qty = sum(float(r.get("qty") or 0) for r in closes)
            px = sum(float(r.get("price") or 0) * float(r.get("qty") or 0) for r in closes) / qty if qty else None
            return pnl, px
        except Exception as e:
            log.info("查询平仓盈亏失败：%s", str(e)[:150])
            return None, None
