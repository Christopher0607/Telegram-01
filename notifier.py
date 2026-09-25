"""通知与控制：通过你自己的 Telegram 机器人推送消息、接收命令。

也负责「手机上完成部署」的两件事：
- 绑定主人：没有 TG_OWNER_ID 时，谁先给机器人发 /start <SETUP_PIN>，谁就是主人（保存到 data/owner.json）
- 在机器人对话里完成监听账号登录（等待主人回复验证码）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger("notifier")

# 输入框下方常驻的按钮：点一下就等于发对应的命令
BUTTON_ROWS = [["📊 状态", "📈 战绩", "📜 最近交易"],
               ["🧠 AI识别", "⏸ 暂停开仓", "▶️ 恢复开仓"],
               ["🛑 全部平仓", "📖 帮助"]]
BUTTON_CMDS = {"📊 状态": "/status", "📈 战绩": "/stats", "📜 最近交易": "/trades", "🧠 AI识别": "/ai",
               "⏸ 暂停开仓": "/pause", "▶️ 恢复开仓": "/resume", "📖 帮助": "/help"}
CLOSEALL_BUTTON = "🛑 全部平仓"   # 这个按钮要再点一次「确认」才执行
CONFIRM_TTL = 300                 # 确认按钮 5 分钟内有效
KEYBOARD = {"keyboard": [[{"text": b} for b in row] for row in BUTTON_ROWS],
            "resize_keyboard": True, "is_persistent": True}
# 输入框左边「菜单」里的命令（/closeall、/gatetest 会真实下单，不放进菜单，免得误点）
MENU = [("status", "运行状态、权益、持仓"), ("stats", "各频道战绩"), ("trades", "最近 10 笔已平仓交易"),
        ("ai", "最近 10 条频道消息的 AI 识别结果"), ("pause", "暂停实盘开新仓"), ("resume", "恢复实盘开新仓"),
        ("ip", "服务器 IP"), ("help", "全部命令")]


def owner_file(data_dir: str) -> str:
    return os.path.join(data_dir, "owner.json")


def load_owner(data_dir: str) -> int:
    try:
        with open(owner_file(data_dir)) as f:
            return int(json.load(f).get("id") or 0)
    except Exception:
        return 0


class Notifier:
    def __init__(self, bot_token: str, owner_id: int, tg_client=None, data_dir: str = ""):
        self.token = bot_token
        self.owner_id = int(owner_id or 0)
        self.tg = tg_client
        self.data_dir = data_dir
        self.offset: int | None = None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=10.0))

    @property
    def api(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def close(self):
        await self.http.aclose()

    # ---------------- 发送 ----------------
    async def send(self, text: str, markup: dict | None = None) -> bool:
        """发给主人（markup = 附带的按钮）。通过机器人发送成功返回 True。"""
        text = text[:4000]
        log.info("通知: %s", text.replace("\n", " | ")[:300])
        if self.token and self.owner_id:
            try:
                payload = {"chat_id": self.owner_id, "text": text, "disable_web_page_preview": True}
                if markup:
                    payload["reply_markup"] = markup
                r = await self.http.post(f"{self.api}/sendMessage", json=payload)
                if r.status_code == 200:
                    return True
                log.warning("机器人发消息失败 %s：%s（先给机器人发一次 /start）", r.status_code, r.text[:200])
            except Exception as e:
                log.warning("机器人发消息异常：%s", e)
        if self.tg:
            try:
                if await self.tg.is_user_authorized():
                    await self.tg.send_message("me", text)
            except Exception as e:
                log.warning("发送到收藏夹失败：%s", e)
        return False

    async def call(self, method: str, payload: dict) -> dict | None:
        """调用机器人的其他接口（设置菜单、按钮回应、改消息）。失败只记日志。"""
        try:
            r = await self.http.post(f"{self.api}/{method}", json=payload)
            if r.status_code != 200:
                log.warning("机器人 %s 返回 %s：%s", method, r.status_code, r.text[:200])
            return r.json()
        except Exception as e:
            log.warning("机器人 %s 失败：%s", method, e)
            return None

    async def delete(self, msg: dict | None):
        """删除主人发来的消息（验证码、密码、API Key 用完就删）。"""
        if not msg or not self.token:
            return
        try:
            await self.http.post(f"{self.api}/deleteMessage", json={
                "chat_id": msg["chat"]["id"], "message_id": msg["message_id"]})
        except Exception as e:
            log.warning("删除消息失败：%s", e)

    # ---------------- 接收 ----------------
    async def _updates(self, timeout: int) -> list:
        params = {"timeout": timeout, "allowed_updates": '["message","callback_query"]'}
        if self.offset is not None:
            params["offset"] = self.offset
        r = await self.http.get(f"{self.api}/getUpdates", params=params)
        if r.status_code == 409:
            log.warning("这个机器人 token 正被别的程序使用（409），请给本程序单独建一个机器人")
            await asyncio.sleep(30)
            return []
        if r.status_code != 200:  # 401 = Token 失效（机器人被删或 Token 被重置）、429 = 太频繁：别连续狂发请求
            log.warning("机器人 getUpdates 返回 %s：%s（60 秒后重试）", r.status_code, r.text[:200])
            await asyncio.sleep(60)
            return []
        res = r.json().get("result") or []
        if res:
            self.offset = res[-1]["update_id"] + 1
        return res

    @staticmethod
    def _private_msg(u: dict) -> dict | None:
        m = u.get("message") or {}
        if (m.get("chat") or {}).get("type") != "private" or not m.get("text"):
            return None
        return m

    async def skip_backlog(self):
        """丢弃启动前积压的旧消息。"""
        if self.offset is not None:
            return
        try:
            r = await self.http.get(f"{self.api}/getUpdates", params={"offset": -1, "timeout": 0})
            res = r.json().get("result") or []
            self.offset = res[-1]["update_id"] + 1 if res else 0
        except Exception as e:
            log.warning("getUpdates 初始化失败：%s", e)

    async def next_owner_message(self, timeout_s: int) -> dict | None:
        """等主人发来下一条文字消息，最多等 timeout_s 秒。"""
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                for u in await self._updates(int(max(1, min(50, end - time.time())))):
                    m = self._private_msg(u)
                    if m and (m.get("from") or {}).get("id") == self.owner_id:
                        self.offset = u["update_id"] + 1  # 同一批里后面的消息留到下次读，不丢
                        return m
            except Exception as e:
                log.warning("等待消息异常：%s", e)
                await asyncio.sleep(5)
        return None

    async def discover_owner(self, pin: str) -> int:
        """首次部署：等用户给机器人发 /start <pin>，把他记为主人。
        不丢弃积压消息：服务器装好之前就发来的 /start <pin> 也要认（口令每次部署随机生成，旧消息里不会有）。"""
        log.info("等待主人绑定：请在 Telegram 给机器人发送 /start %s", pin)
        while not self.owner_id:
            try:
                for u in await self._updates(50):
                    m = self._private_msg(u)
                    if m and pin in m["text"]:
                        self.owner_id = int(m["from"]["id"])
                        if self.data_dir:
                            with open(owner_file(self.data_dir), "w") as f:
                                json.dump({"id": self.owner_id}, f)
                        await self.send("✅ 已把你绑定为这个机器人的主人。以后只有你能控制它。")
                        break
            except Exception as e:
                log.warning("等待绑定异常：%s", e)
                await asyncio.sleep(5)
        return self.owner_id

    async def on_button(self, cq: dict, handler):
        """主人点了消息下面的按钮（目前只有「确认全部平仓 / 取消」）。"""
        await self.call("answerCallbackQuery", {"callback_query_id": cq["id"]})
        m = cq.get("message") or {}
        where = {"chat_id": (m.get("chat") or {}).get("id"), "message_id": m.get("message_id")}
        data = cq.get("data") or ""
        if not data.startswith("closeall:"):
            await self.call("editMessageText", dict(where, text="已取消，什么都没做。"))
            return
        if time.time() - int(data.split(":")[1]) > CONFIRM_TTL:
            await self.call("editMessageText", dict(where, text=f"⌛ 这个确认按钮已经过期，什么都没做。要平仓请重新点「{CLOSEALL_BUTTON}」。"))
            return
        await self.call("editMessageText", dict(where, text="🛑 已确认，正在平掉所有实盘仓位……"))
        reply = await handler("/closeall", m)
        if reply:
            await self.send(reply)

    async def command_loop(self, handler):
        """长轮询机器人消息；只响应主人发来的 / 命令和按钮。handler(text, msg) -> 回复文字"""
        if not self.token or not self.owner_id:
            log.info("未配置机器人或主人，命令功能关闭")
            return
        await self.skip_backlog()
        await self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in MENU]})
        while True:
            try:
                for u in await self._updates(50):
                    cq = u.get("callback_query")
                    if cq:
                        if (cq.get("from") or {}).get("id") == self.owner_id:
                            await self.on_button(cq, handler)
                        continue
                    m = self._private_msg(u)
                    if not m or (m.get("from") or {}).get("id") != self.owner_id:
                        continue
                    text = m["text"].strip()
                    if text == CLOSEALL_BUTTON:
                        await self.send("⚠️ 确定要平掉本程序开的所有实盘仓位、撤销挂单，并暂停开新仓吗？\n（模拟盘不受影响）", {
                            "inline_keyboard": [[{"text": "✅ 确认全部平仓", "callback_data": f"closeall:{int(time.time())}"},
                                                 {"text": "取消", "callback_data": "cancel"}]]})
                        continue
                    text = BUTTON_CMDS.get(text, text)
                    if not text.startswith("/"):
                        if re.search(r"[0-9A-Za-z]{30,}", text):  # 像是忘了带命令直接发的 API 密钥：马上删掉
                            await self.delete(m)
                            await self.send("⚠️ 这条消息看起来是 API 密钥，我已经帮你删掉了，没有保存。\n"
                                            "设置交易所 API 要在前面加上命令，例如：/gate 你的Key 你的Secret（发 /help 查看命令）")
                        continue
                    try:
                        reply = await handler(text, m)
                    except Exception as e:
                        log.exception("命令处理出错")
                        reply = f"命令出错：{e}"
                    if reply:  # /start、/help 的回复带上按钮（万一按钮被收起来了，发 /help 就能找回）
                        cmd = text.split()[0].lower().split("@")[0]
                        await self.send(reply, KEYBOARD if cmd in ("/start", "/help") else None)
            except Exception as e:
                log.warning("命令轮询异常：%s", e)
                await asyncio.sleep(5)
