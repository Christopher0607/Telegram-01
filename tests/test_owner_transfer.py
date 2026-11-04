"""换主人口令：别的账号发 /start 口令 → 变成主人（存 owner.json、secrets.env），口令只能用一次，原来的主人收到提醒，然后重启"""
import asyncio, hashlib, json, os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
D = tempfile.mkdtemp(); os.environ["DATA_DIR"] = D
import config as C
import notifier as N

ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} FAILED {extra}"
    ok += 1; print("✅", name)

CODE = "TestCode9xYzAbCdEfGh"
H = hashlib.sha256(CODE.encode()).hexdigest()


class Resp:
    def __init__(self, data, code=200): self.data, self.status_code, self.text = data, code, json.dumps(data)
    def json(self): return self.data


class FakeHttp:
    def __init__(self, batches): self.batches, self.posts, self.gets = list(batches), [], []
    async def get(self, url, params=None):
        self.gets.append(params or {})
        if (params or {}).get("offset") == -1:
            return Resp({"ok": True, "result": []})
        return Resp({"ok": True, "result": self.batches.pop(0) if self.batches else []})
    async def post(self, url, json=None):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return Resp({"ok": True})
    async def aclose(self): pass


def upd(i, uid, text):
    return {"update_id": i, "message": {"message_id": i, "chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": text}}


def make(batches, d):
    n = N.Notifier("TOKEN", 111, None, d, H)
    n.http = FakeHttp(batches)
    n.restarts = 0
    def on(): n.restarts += 1
    n.on_owner_change = on
    return n


async def main():
    # 1. 新账号发 /start 口令
    d = tempfile.mkdtemp()
    n = make([[upd(1, 222, f"/start {CODE}"), upd(2, 222, "/status")]], d)
    got = await n._updates(0)
    sends = [(m, p) for m, p in n.http.posts if m == "sendMessage"]
    check("新账号发 /start 口令 → 变成主人", n.owner_id == 222 and json.load(open(os.path.join(d, "owner.json")))["id"] == 222)
    check("口令那条消息不再往后传（后面的 /status 照常）", [u["update_id"] for u in got] == [2])
    check("口令消息被删掉", ("deleteMessage", {"chat_id": 222, "message_id": 1}) in n.http.posts)
    check("新主人收到确认、原来的主人收到提醒", sends[0][1]["chat_id"] == 222 and "已把这个账号绑定为机器人的主人" in sends[0][1]["text"]
          and sends[1][1]["chat_id"] == 111 and "换成另一个 Telegram 账号" in sends[1][1]["text"])
    check("然后重启", n.restarts == 1)
    check("口令记成用过（只记 SHA-256，不记口令）", json.load(open(os.path.join(d, "owner_transfer.json")))["used"] == [H]
          and CODE not in open(os.path.join(d, "owner_transfer.json")).read())
    sec = open(C.SECRETS_PATH).read()
    check("data/secrets.env 里的 TG_OWNER_ID 也改了（比服务器原来的环境变量优先）", "TG_OWNER_ID=222" in sec)
    os.environ.pop("TG_OWNER_ID", None)
    check("重启后 Config 读出来的主人是新账号", C.Config().tg_owner_id == 222)

    # 2. 用过的口令再发：不认
    n2 = make([[upd(3, 333, f"/start {CODE}")]], d)
    n2.owner_id = 222
    got = await n2._updates(0)
    check("同一个口令再用一次：不认（主人不变）", n2.owner_id == 222 and n2.restarts == 0 and len(got) == 1)

    # 3. 错的口令、别的文字
    d3 = tempfile.mkdtemp()
    n3 = make([[upd(4, 333, "/start WRONGCODE123"), upd(5, 333, f"hello {CODE}"), upd(6, 333, f"/status {CODE}")]], d3)
    await n3._updates(0)
    check("口令不对、口令夹在别的话里、跟在别的命令后面：都不认", n3.owner_id == 111 and n3.restarts == 0
          and not os.path.exists(os.path.join(d3, "owner_transfer.json")))

    # 4. 只发口令、/owner 口令 也行
    for text in (CODE, f"/owner {CODE}", f"/start@MyBot {CODE}"):
        d4 = tempfile.mkdtemp()
        n4 = make([[upd(7, 444, text)]], d4)
        await n4._updates(0)
        check(f"「{text.replace(CODE, '口令')}」也认", n4.owner_id == 444 and n4.restarts == 1)

    # 5. 现在的主人自己发口令：不算换主人（当普通 /start 处理），口令也不作废
    d5 = tempfile.mkdtemp()
    n5 = make([[upd(8, 111, f"/start {CODE}")]], d5)
    got = await n5._updates(0)
    check("主人自己发口令：什么都不改，口令不作废", n5.owner_id == 111 and len(got) == 1 and n5.restarts == 0
          and not os.path.exists(os.path.join(d5, "owner_transfer.json")))

    # 6. 机器人重启时积压的消息里有口令（skip_backlog）
    d6 = tempfile.mkdtemp()
    n6 = make([[upd(9, 555, "随便说点什么"), upd(10, 555, f"/start {CODE}")]], d6)
    await n6.skip_backlog()
    check("重启前后发来的口令（积压的消息里）也认", n6.owner_id == 555 and n6.restarts == 1)

    # 7. 没设口令：什么都不认
    n7 = N.Notifier("TOKEN", 111, None, tempfile.mkdtemp(), "")
    n7.http = FakeHttp([[upd(11, 666, f"/start {CODE}")]])
    await n7._updates(0)
    check("config.yaml 没写口令：不认", n7.owner_id == 111)

    print(f"\n{ok} passed")

asyncio.run(main())
