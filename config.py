"""配置加载：config.yaml（策略/风控参数） + .env（密钥）"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
# 通过机器人 /gate、/bitget 命令设置的密钥保存在这里（优先级高于 .env）
SECRETS_PATH = os.path.join(DATA_DIR, "secrets.env")

# 所有风控参数的默认值（config.yaml 里写了就以 config.yaml 为准）
DEFAULT_RISK = {
    "risk_per_trade_usdt": 10.0,      # 每单打到止损固定亏多少 U（含手续费估算）；0 = 改用下面的百分比
    "risk_per_trade_pct": 1.0,        # risk_per_trade_usdt 为 0 时：每单亏权益的 %
    "max_leverage": 0,                # 0 = 不设上限，按止损自动开到最高（仍受交易所每个币的上限约束）
    "liq_safety": 0.7,                # 止损距离 ≤ 强平距离 × 0.7（已计入维持保证金率和手续费）
    "max_margin_pct": 25.0,           # 单笔保证金最多占权益 %
    "max_open_positions": 3,          # 同时最多几单（持仓+挂单）
    "fallback_sl_mode": "atr",        # 信号没给止损时：atr 按波动率补 / pct 固定百分比 / off 不跟
    "fallback_sl_atr_mult": 2.0,      # atr 模式：止损距离 = 1 小时 ATR(14) × 此倍数
    "fallback_sl_min_pct": 1.5,       # 补的止损离进场价最近 %
    "fallback_sl_max_pct": 8.0,       # 补的止损离进场价最远 %
    "fallback_sl_pct": 3.0,           # pct 模式的固定止损 %（任何模式下都不会不带止损下单）
    "fallback_tp_r": 2.0,             # 信号没给止盈时按 X 倍风险设止盈；0 = 不设
    "tp_split": [0.5, 0.3, 0.2],      # 多个止盈位的分批比例
    "breakeven_after_tp1": True,      # 第一止盈成交后止损移到开仓价
    "min_rr": 1.0,                    # 按实际进场价算的盈亏比低于此值不开
    "chase_pct": 0.5,                 # 价格偏离进场区 ≤ 此 % 仍可市价进
    "max_entry_deviation_pct": 15.0,  # 进场价与现价偏差超过此 % 视为识别错误
    "max_sl_distance_pct": 25.0,      # 止损距离超过此 % 视为识别错误
    "allow_limit_orders": True,       # 价格没到进场区时挂限价单等
    "limit_order_ttl_min": 240,       # 限价单多久不成交就撤
    "max_signal_age_sec": 300,        # 超过此秒数的旧消息不处理
    "min_24h_volume_usdt": 3_000_000, # 24h 成交额低于此值的币不做
    "allowed_symbols": [],            # 为空 = 全部允许
    "blocked_symbols": [],
    "min_open_confidence": "high",
    "min_manage_confidence": "medium",
    "follow_close": True,             # 跟随频道的平仓/减仓指令
    "follow_move_sl": True,           # 跟随频道的移动止损
    "follow_update_tp": True,         # 跟随频道的止盈更新
    "fee_rate": 0.0006,               # 单边手续费估算（算仓位和模拟盘盈亏用）
}


@dataclass
class ChannelCfg:
    username: str
    mode: str = "paper"               # paper 模拟 / live 实盘 / off 关闭
    risk_multiplier: float = 1.0
    overrides: dict = field(default_factory=dict)
    title: str = ""                   # 运行时自动填入频道名


def _saved_owner() -> int:
    """首次部署时通过 /start <PIN> 绑定的主人 id（data/owner.json）。"""
    try:
        import json
        with open(os.path.join(DATA_DIR, "owner.json")) as f:
            return int(json.load(f).get("id") or 0)
    except Exception:
        return 0


def save_secrets(values: dict):
    """把密钥写入 data/secrets.env（只有 root 可读）。已有的同名项会被覆盖。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    cur = {}
    if os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    cur[k] = v
    cur.update(values)
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in cur.items())
    os.chmod(SECRETS_PATH, 0o600)


def _clean_username(u: str) -> str:
    u = str(u).strip()
    for p in ("https://", "http://", "t.me/", "telegram.me/", "@"):
        if u.startswith(p):
            u = u[len(p):]
    for p in ("t.me/", "s/"):
        if u.startswith(p):
            u = u[len(p):]
    return u.strip("/")


class Config:
    def __init__(self, path: str | None = None):
        load_dotenv(os.path.join(BASE_DIR, ".env"))
        load_dotenv(SECRETS_PATH, override=True)
        path = path or os.environ.get("CONFIG_PATH") or os.path.join(BASE_DIR, "config.yaml")
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        self.live_trading = bool(raw.get("live_trading", False))
        ex = raw.get("exchange") or {}
        self.exchange_name = str(ex.get("name", "bitget")).lower()   # bitget 或 gate
        self.margin_mode = str(ex.get("margin_mode", "isolated")).lower()
        llm = raw.get("llm") or {}
        self.llm_provider = str(llm.get("provider", "deepseek")).lower()
        self.llm_model = str(llm.get("model", "deepseek-v4-flash"))
        self.llm_vision = bool(llm.get("vision", True))   # 频道消息里的图片也交给 AI 看
        paper = raw.get("paper") or {}
        self.paper_equity = float(paper.get("equity", 1000))
        self.tz_offset = float(raw.get("timezone_offset_hours", 8))
        self.report_hour = int(raw.get("daily_report_hour", 22))

        self.risk = copy.deepcopy(DEFAULT_RISK)
        self.risk.update(raw.get("risk") or {})

        self.channels: list[ChannelCfg] = []
        for c in raw.get("channels") or []:
            if isinstance(c, str):
                c = {"username": c}
            self.channels.append(ChannelCfg(
                username=_clean_username(c["username"]),
                mode=str(c.get("mode", "paper")).lower(),
                risk_multiplier=float(c.get("risk_multiplier", 1.0)),
                overrides=dict(c.get("overrides") or {}),
            ))

        # ---- 密钥（.env）----
        self.tg_api_id = int(os.getenv("TG_API_ID") or 0)
        self.tg_api_hash = os.getenv("TG_API_HASH", "")
        self.tg_phone = os.getenv("TG_PHONE", "")
        self.tg_bot_token = os.getenv("TG_BOT_TOKEN", "")
        self.tg_owner_id = int(os.getenv("TG_OWNER_ID") or 0) or _saved_owner()
        self.setup_pin = os.getenv("SETUP_PIN", "").strip()
        self.server_ip = os.getenv("SERVER_IP", "").strip()
        self.bitget_key = os.getenv("BITGET_API_KEY", "")
        self.bitget_secret = os.getenv("BITGET_API_SECRET", "")
        self.bitget_passphrase = os.getenv("BITGET_API_PASSPHRASE", "")
        self.gate_key = os.getenv("GATE_API_KEY", "")
        self.gate_secret = os.getenv("GATE_API_SECRET", "")
        self.deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    def risk_for(self, ch: ChannelCfg) -> dict:
        r = copy.deepcopy(self.risk)
        r.update(ch.overrides or {})
        return r

    def mode_for(self, ch: ChannelCfg) -> str:
        """实际生效的模式：只有总开关 live_trading 打开且频道设为 live 才真实下单。"""
        if ch.mode == "off":
            return "off"
        if ch.mode == "live" and self.live_trading:
            return "live"
        return "paper"

    def channel_by_username(self, username: str) -> ChannelCfg | None:
        for c in self.channels:
            if c.username.lower() == str(username).lower():
                return c
        return None
