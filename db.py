"""SQLite 存储：频道消息、交易记录、少量状态"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER, msg_id INTEGER, edited INTEGER DEFAULT 0,
  channel TEXT, ts INTEGER, reply_to INTEGER, text TEXT,
  parsed TEXT, outcome TEXT, trade_id INTEGER,
  UNIQUE(chat_id, msg_id, edited)
);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT, channel TEXT, title TEXT, chat_id INTEGER, msg_id INTEGER,
  symbol TEXT, base TEXT, scale REAL DEFAULT 1, side TEXT, status TEXT,
  entry_kind TEXT, entry_price REAL, qty REAL, remaining REAL,
  sl REAL, soft_sl REAL, tps TEXT, leverage INTEGER, risk_usdt REAL,
  realized REAL DEFAULT 0, order_id TEXT,
  created_at INTEGER, opened_at INTEGER, closed_at INTEGER, expires_at INTEGER,
  last_candle_ts INTEGER, be_moved INTEGER DEFAULT 0,
  exit_reason TEXT, pnl REAL, r_mult REAL, sl_source TEXT
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_msg ON trades(chat_id, msg_id);
"""

TRADE_FIELDS = {
    "mode", "channel", "title", "chat_id", "msg_id", "symbol", "base", "scale", "side", "status",
    "entry_kind", "entry_price", "qty", "remaining", "sl", "soft_sl", "tps", "leverage", "risk_usdt",
    "realized", "order_id", "created_at", "opened_at", "closed_at", "expires_at",
    "last_candle_ts", "be_moved", "exit_reason", "pnl", "r_mult", "sl_source",
}
ACTIVE = ("pending", "open")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "sl_source" not in cols:  # 旧版数据库升级
            self.conn.execute("ALTER TABLE trades ADD COLUMN sl_source TEXT")
        self.conn.commit()

    # ---------------- messages ----------------
    def message_seen(self, chat_id: int, msg_id: int, edited: bool) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=? AND msg_id=? AND edited=?",
            (chat_id, msg_id, int(edited))).fetchone()
        return r is not None

    def save_message(self, ctx, parsed: dict | None, outcome: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages(chat_id,msg_id,edited,channel,ts,reply_to,text,parsed,outcome)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (ctx.chat_id, ctx.msg_id, int(ctx.edited), ctx.channel.username, int(ctx.date.timestamp()),
             ctx.reply_to, ctx.text, json.dumps(parsed, ensure_ascii=False) if parsed else None, outcome))
        self.conn.commit()
        return cur.lastrowid

    def set_message(self, row_id: int, outcome: str | None = None, trade_id: int | None = None):
        if outcome is not None:
            self.conn.execute("UPDATE messages SET outcome=? WHERE id=?", (outcome, row_id))
        if trade_id is not None:
            self.conn.execute("UPDATE messages SET trade_id=? WHERE id=?", (trade_id, row_id))
        self.conn.commit()

    def trade_for_message(self, chat_id: int, msg_id: int) -> int | None:
        """找到某条频道消息关联的交易（原始信号，或者曾经处理过的跟进消息）。"""
        r = self.conn.execute(
            "SELECT id FROM trades WHERE chat_id=? AND msg_id=? ORDER BY id DESC LIMIT 1",
            (chat_id, msg_id)).fetchone()
        if r:
            return r["id"]
        r = self.conn.execute(
            "SELECT trade_id FROM messages WHERE chat_id=? AND msg_id=? AND trade_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (chat_id, msg_id)).fetchone()
        return r["trade_id"] if r else None

    # ---------------- trades ----------------
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["tps"] = json.loads(d["tps"]) if d.get("tps") else []
        return d

    def insert_trade(self, t: dict) -> int:
        data = {k: v for k, v in t.items() if k in TRADE_FIELDS}
        if "tps" in data:
            data["tps"] = json.dumps(data["tps"], ensure_ascii=False)
        cols = ",".join(data)
        cur = self.conn.execute(
            f"INSERT INTO trades({cols}) VALUES({','.join('?' * len(data))})", tuple(data.values()))
        self.conn.commit()
        return cur.lastrowid

    def update_trade(self, tid: int, **fields):
        data = {k: v for k, v in fields.items() if k in TRADE_FIELDS}
        if not data:
            return
        if "tps" in data:
            data["tps"] = json.dumps(data["tps"], ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in data)
        self.conn.execute(f"UPDATE trades SET {sets} WHERE id=?", (*data.values(), tid))
        self.conn.commit()

    def save_trade(self, t: dict):
        self.update_trade(t["id"], **{k: v for k, v in t.items() if k != "id"})

    def get_trade(self, tid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        return self._row(r) if r else None

    def active_trades(self, mode: str | None = None) -> list[dict]:
        q = "SELECT * FROM trades WHERE status IN ('pending','open')"
        args: tuple = ()
        if mode:
            q += " AND mode=?"
            args = (mode,)
        return [self._row(r) for r in self.conn.execute(q + " ORDER BY id", args)]

    def closed_trades(self) -> list[dict]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY id")]

    def paper_realized(self) -> float:
        r = self.conn.execute(
            "SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE mode='paper' AND status='closed'").fetchone()
        return float(r["s"] or 0)

    # ---------------- kv ----------------
    def kv_get(self, k: str, default=None):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, k: str, v):
        self.conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        self.conn.commit()


def now_ms() -> int:
    return int(time.time() * 1000)
