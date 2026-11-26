"""重开新服务器（2026-11-26）：/start 口令 绑定主人后删掉这条消息、长口令不写进日志；私人群还没连上就主动列出群让主人点（每个位置只问一次）"""
import asyncio, json, logging, os, sys, tempfile, types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import config as C
import db as DBM
import main as M
import notifier as N

ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} FAILED {extra}"
    ok += 1; print("✅", name)

PIN = "abcd-efgh-ijkm-npqr-stuv-wxyz"


class Resp:
    def __init__(self, data): self.data, self.status_code, self.text = data, 200, json.dumps(data)
    def json(self): return self.data


class FakeHttp:
    def __init__(self, batches): self.batches, self.posts = list(batches), []
    async def get(self, url, params=None):
        return Resp({"ok": True, "result": self.batches.pop(0) if self.batches else []})
    async def post(self, url, json=None):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return Resp({"ok": True})


def upd(i, uid, text):
    return {"update_id": i, "message": {"message_id": i, "chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": text}}


class Logs(logging.Handler):
    def __init__(self): super().__init__(); self.msgs = []
    def emit(self, r): self.msgs.append(r.getMessage())


class Quiet:
    def __init__(self): self.sent = []
    async def send(self, text, markup=None): self.sent.append((text, markup)); return True


class FakeClient:
    def __init__(self, dialogs): self.dialogs = dialogs
    async def iter_dialogs(self, limit=None):
        for d in self.dialogs:
            yield d


async def main():
    # 1. /start 口令 绑定主人
    d = tempfile.mkdtemp()
    n = N.Notifier("TOKEN", 0, None, d, "")
    n.http = FakeHttp([[upd(1, 333, "你好"), upd(2, 222, f"/start {PIN}")]])
    h = Logs(); N.log.addHandler(h); N.log.setLevel(logging.INFO)
    await n.discover_owner(PIN)
    check("发了 /start 口令的人成了主人", n.owner_id == 222 and json.load(open(os.path.join(d, "owner.json")))["id"] == 222)
    check("口令那条消息删掉", ("deleteMessage", {"chat_id": 222, "message_id": 2}) in n.http.posts)
    check("日志里没有口令", h.msgs and not any(PIN in m for m in h.msgs), h.msgs)
    n2 = N.Notifier("TOKEN", 0, None, tempfile.mkdtemp(), "")
    n2.http = FakeHttp([[upd(3, 444, "/start 482913")]])
    h.msgs.clear()
    await n2.discover_owner("482913")
    check("短的 PIN（Claude 建服务器时给的）照旧写进日志", n2.owner_id == 444 and any("482913" in m for m in h.msgs))

    # 2. 私人群还没连上：列出监听账号的群
    cfg = C.Config()
    for c in cfg.channels:
        c.entity = None
    private = [c.username for c in cfg.channels if c.private and c.mode != "off"]
    dialogs = [types.SimpleNamespace(is_group=True, is_channel=False, id=-100111, title="测试群甲"),
               types.SimpleNamespace(is_group=False, is_channel=True, id=-100222, title="测试频道乙"),
               types.SimpleNamespace(is_group=False, is_channel=False, id=555, title="私聊")]
    db, q = DBM.DB(":memory:"), Quiet()
    await M.prompt_unjoined(cfg, FakeClient(dialogs), {}, db, q)
    asked = [t.split("「", 1)[1].split("」", 1)[0] for t, _ in q.sent]
    check(f"没连上的私人群（{'、'.join(private)}）各问一次，公开频道、关掉的不问", asked == private and "huhu" in private, asked)
    t, mk = q.sent[[i for i, x in enumerate(asked) if x == "huhu"][0]]
    cbs = [b["callback_data"] for row in mk["inline_keyboard"] for b in row]
    check("列出群和频道（不列私聊），点一下就是「/join huhu」的选择", cbs == ["jn:huhu:-100111", "jn:huhu:-100222"]
          and "连上以后是实盘" in t, (cbs, t))
    await M.prompt_unjoined(cfg, FakeClient(dialogs), {}, db, q)
    check("重启以后不再问（每个位置只问一次）", len(q.sent) == len(private))
    huhu = next(c for c in cfg.channels if c.username == "huhu")
    db2, q2 = DBM.DB(":memory:"), Quiet()
    await M.prompt_unjoined(cfg, FakeClient(dialogs), {-100111: huhu}, db2, q2)
    check("已经连上的不问", all("「huhu」" not in t for t, _ in q2.sent) and len(q2.sent) == len(private) - 1)
    print(f"\n{ok} passed")

asyncio.run(main())
