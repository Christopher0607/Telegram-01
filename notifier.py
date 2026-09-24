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
import time

import httpx

log = logging.getLogger("notifier")


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
    async def send(self, text: str) -> bool:
        """发给主人。通过机器人发送成功返回 True。"""
        text = text[:4000]
        log.info("通知: %s", text.replace("\n", " | ")[:300])
        if self.token and self.owner_id:
            try:
                r = await self.http.post(f"{self.api}/sendMessage", json={
                    "chat_id": self.owner_id, "text": text, "disable_web_page_preview": True})
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
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if self.offset is not None:
            params["offset"] = self.offset
        r = await self.http.get(f"{self.api}/getUpdates", params=params)
        if r.status_code == 409:
            log.warning("这个机器人 token 正被别的程序使用（409），请给本程序单独建一个机器人")
            await asyncio.sleep(30)
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
                        return m
            except Exception as e:
                log.warning("等待消息异常：%s", e)
                await asyncio.sleep(5)
        return None

    async def discover_owner(self, pin: str) -> int:
        """首次部署：等用户给机器人发 /start <pin>，把他记为主人。"""
        await self.skip_backlog()
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

    async def command_loop(self, handler):
        """长轮询机器人消息；只响应主人发来的 / 命令。handler(text, msg) -> 回复文字"""
        if not self.token or not self.owner_id:
            log.info("未配置机器人或主人，命令功能关闭")
            return
        await self.skip_backlog()
        while True:
            try:
                for u in await self._updates(50):
                    m = self._private_msg(u)
                    if not m or (m.get("from") or {}).get("id") != self.owner_id:
                        continue
                    text = m["text"].strip()
                    if not text.startswith("/"):
                        continue
                    try:
                        reply = await handler(text, m)
                    except Exception as e:
                        log.exception("命令处理出错")
                        reply = f"命令出错：{e}"
                    if reply:
                        await self.send(reply)
            except Exception as e:
                log.warning("命令轮询异常：%s", e)
                await asyncio.sleep(5)
