"""监听账号登录时发 /phone +新手机号：号码存进 data/secrets.env 的 TG_PHONE，重启后用新号码登录"""
import asyncio, os, sys, tempfile, types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
D = tempfile.mkdtemp(); os.environ["DATA_DIR"] = D
os.environ["TG_PHONE"] = "+19990001234"
import config as C
import main as M

ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} FAILED {extra}"
    ok += 1; print("✅", name)


class Restarted(Exception):
    pass


class Done(Exception):
    pass


def boom():
    raise Restarted()


M.restart_soon = boom


class FakeNotifier:
    def __init__(self, msgs): self.msgs, self.sent, self.deleted = list(msgs), [], []
    async def skip_backlog(self): pass
    async def send(self, text, markup=None): self.sent.append(text); return True
    async def delete(self, m): self.deleted.append(m["text"])
    async def next_owner_message(self, t):
        if not self.msgs:
            raise Done()
        return self.msgs.pop(0)


class FakeClient:
    def __init__(self): self.phones, self.signed = [], []
    async def send_code_request(self, phone):
        self.phones.append(phone)
        return types.SimpleNamespace(phone_code_hash="h")
    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        self.signed.append((phone, code))


def msg(t): return {"text": t, "chat": {"id": 1}, "message_id": 1}


async def run(msgs):
    cfg = C.Config()
    n, c = FakeNotifier(msgs), FakeClient()
    try:
        await M.bot_login(c, cfg, n)
        out = "logged_in"
    except Restarted:
        out = "restarted"
    except Done:
        out = "waiting"
    return out, n, c


def secrets():
    try:
        return open(C.SECRETS_PATH).read()
    except OSError:
        return ""


async def main():
    out, n, c = await run([msg("1 2 3 4 5")])
    check("照常：验证码用空格隔开发过来 → 登录", out == "logged_in" and c.signed == [("+19990001234", "12345")])
    check("登录提示里写着收不到验证码可以发 /phone", "/phone +手机号" in n.sent[0])

    out, n, c = await run([msg("/phone 8613800000000"), msg("/phone +60 12-345 6789")])
    check("号码没带 + → 提示用法，接着等", any("用法：/phone +手机号" in t for t in n.sent))
    check("带空格、横线也认 → 存进 secrets.env、重启", out == "restarted" and "TG_PHONE=+60123456789" in secrets())
    check("两条 /phone 消息都删掉（手机号不留在聊天里）", n.deleted == ["/phone 8613800000000", "/phone +60 12-345 6789"])
    check("确认消息里号码打码", any("+601****789" in t for t in n.sent) and not any("+60123456789" in t for t in n.sent))
    check("重启后 Config 读到新号码", C.Config().tg_phone == "+60123456789")
    check("换号的时候没把 /phone 当成验证码去登录", c.signed == [])

    out, n, c = await run([None, msg("/phone +8613800000000")])
    check("30 分钟没收到验证码以后再发 /phone：照样换号", out == "restarted" and "TG_PHONE=+8613800000000" in secrets())

    out, n, c = await run([msg("/status"), msg("9 8 7 6 5")])
    check("别的命令照旧不当验证码", out == "logged_in" and c.signed == [("+8613800000000", "98765")])
    print(f"\n{ok} passed")

asyncio.run(main())
