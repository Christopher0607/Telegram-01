"""加密的密钥包（2026-11-26 用户要求重开时只建服务器、登录 Telegram、设交易所 API）：
开机脚本用口令解开 deploy/secrets.enc；填了的值（或者短脚本里 export 的）优先；口令不对就只用填的值；没口令跟以前一样填 6 个值"""
import argparse, importlib.util, os, re, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("do_deploy", os.path.join(ROOT, "deploy", "do_deploy.py"))
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} FAILED {extra}"
    ok += 1; print("✅", name)

FAKE = {"TG_API_ID": "1234567", "TG_API_HASH": "0123456789abcdef0123456789abcdef", "TG_PHONE": "+60123456789",
        "TG_BOT_TOKEN": "123456789:AAH" + "x" * 32, "DEEPSEEK_API_KEY": "sk-" + "f" * 32}
REPO = "Christopher0607/Telegram-01"
PW = D.new_pass()
BUNDLE = D.make_bundle(FAKE, PW)
MANUAL = D.render_manual(REPO, "main")
HEAD = MANUAL.split("chmod 600 /root/tgst.env\n", 1)[0] + "chmod 600 /root/tgst.env\n"
MERGE = D.ENV_MERGE.replace("__ENC_ARGS__", D.ENC_ARGS)


def lines(text):
    return sorted(l for l in text.splitlines() if l)


def boot(exported=None, fill=None, pw_line=None, bundle=BUNDLE):
    """跑开机脚本里写 .env 的两段（最上面填的值 + 解开加密包），/root 换成临时目录。返回 (.env, 输出, /root 里剩下的文件)"""
    tmp = tempfile.mkdtemp()
    root, app = os.path.join(tmp, "root"), os.path.join(tmp, "app")
    os.makedirs(root); os.makedirs(os.path.join(app, "deploy"))
    if bundle is not None:
        with open(os.path.join(app, "deploy", "secrets.enc"), "wb") as f:
            f.write(bundle)
    h = HEAD
    for k, v in (fill or {}).items():
        h = h.replace(f"{k}=填这里", f"{k}={v}")
    if pw_line:
        h = h.replace("TGST_PASS='填这里'", f"TGST_PASS='{pw_line}'")
    script = ("#!/bin/bash\n" + h + "set -euo pipefail\ncd '" + app + "'\n" + MERGE).replace("/root/", root + "/")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], **(exported or {})})
    assert r.returncode == 0, r.stderr
    with open(os.path.join(app, ".env")) as f:
        return f.read(), r.stdout, os.listdir(root)


want = lines("".join(f"{k}={v}\n" for k, v in FAKE.items()) + f"SETUP_PIN={PW}\n")

# 1. 生成的文件
with open(os.path.join(ROOT, "deploy", "manual-user-data.sh"), encoding="utf-8") as f:
    check("仓库里的 manual-user-data.sh 是最新生成的（改了 cloud-init.sh / do_deploy.py 要重新跑 manual）", f.read() == MANUAL)
check("开机脚本里解开加密包用的参数跟加密时一样", MERGE in MANUAL and D.ENC_ARGS in MANUAL)
check("整个开机脚本语法没问题", subprocess.run(["bash", "-n", "-c", MANUAL]).returncode == 0)
short = D.short_boot(REPO, "main", PW)
check("短开机脚本：带口令、下载 main 分支的 manual-user-data.sh、语法没问题",
      f"export TGST_PASS='{PW}'" in short and f"https://raw.githubusercontent.com/{REPO}/main/deploy/manual-user-data.sh" in short
      and subprocess.run(["bash", "-n", "-c", short]).returncode == 0)
check(f"口令 24 位（6 组 × 4，去掉了 l、o、0、1），约 120 bit：{PW[:4]}-…",
      re.fullmatch(r"([a-km-np-z2-9]{4}-){5}[a-km-np-z2-9]{4}", PW) is not None)

# 2. 加密包本身
check("加密包里看不到任何一个原文", not any(v.encode() in BUNDLE for v in FAKE.values()) and PW.encode() not in BUNDLE)
check("同样的内容每次加密结果都不一样（随机盐）", D.make_bundle(FAKE, PW) != BUNDLE)
try:
    D.run_openssl("-d", BUNDLE, D.new_pass())
    bad = False
except ValueError:
    bad = True
check("口令不对解不开", bad)

# 3. 开机时
env, out, left = boot(exported={"TGST_PASS": PW})
check("短开机脚本（export 口令）：.env = 加密包里的 5 个值 + 绑定口令就是这个口令", lines(env) == want, env)
check("说了口令对了，/root 下的临时文件都删了", "口令对了" in out and left == [], (out, left))
env, out, _ = boot(pw_line=PW)
check("整段贴 manual-user-data.sh、只把 TGST_PASS 那行换成口令：一样", lines(env) == want, env)
env, _, _ = boot(exported={"TGST_PASS": PW, "DEEPSEEK_API_KEY": "sk-new"})
check("口令 + 短脚本里 export 了新的 DeepSeek Key：用新的，只有一行",
      env.count("DEEPSEEK_API_KEY=") == 1 and "DEEPSEEK_API_KEY=sk-new\n" in env and f"TG_BOT_TOKEN={FAKE['TG_BOT_TOKEN']}\n" in env)
env, _, _ = boot(pw_line=PW, fill={"TG_PHONE": "+8613800000000"})
check("口令 + 最上面填了手机号：用填的", env.count("TG_PHONE=") == 1 and "TG_PHONE=+8613800000000\n" in env)
env, out, left = boot(exported={"TGST_PASS": D.new_pass()}, fill={"TG_PHONE": "+8613800000000"})
check("口令不对：提示、只用填了的值", "❌ 口令不对" in out and lines(env) == ["TG_PHONE=+8613800000000"] and left == [], (out, env))
six = dict(FAKE, SETUP_PIN="482913")
env, out, _ = boot(fill=six)
check("没有口令、6 个值都填了：跟以前一样", lines(env) == lines("".join(f"{k}={v}\n" for k, v in six.items()))
      and "口令" not in out, env)
env, out, _ = boot(exported={"TGST_PASS": PW}, bundle=None, fill={"SETUP_PIN": "482913"})
check("有口令但仓库里没有加密包：提示、只用填了的值", "❌" in out and lines(env) == ["SETUP_PIN=482913"])
env, _, _ = boot()
check("什么都没填：.env 是空的（机器人会说缺少设置，不会拿「填这里」当值）", env == "")

# 4. 生成加密包的命令
tmp = tempfile.mkdtemp()
ef = os.path.join(tmp, "deploy.env")
with open(ef, "w") as f:
    f.write("".join(f"{k}={v}\n" for k, v in FAKE.items()).replace("+60123456789", "+60 12-345 6789")
            + "DIGITALOCEAN_TOKEN=dop_v1_secret\nGATE_API_KEY=gatekey\nGITHUB_READ_TOKEN=ghp_x\n")


def bundle_cmd(pass_file, out, env_file=ef):
    try:
        D.cmd_bundle(argparse.Namespace(env_file=env_file, pass_file=pass_file, out=out))
        return "ok"
    except SystemExit as e:
        return str(e)


pf, out = os.path.join(tmp, "pass.txt"), os.path.join(tmp, "secrets.enc")
check("口令文件放在仓库里：不干", "不能放在仓库里" in bundle_cmd(os.path.join(ROOT, "pass.txt"), out)
      and not os.path.exists(os.path.join(ROOT, "pass.txt")))
check("生成新口令（文件只有自己能读）、写出加密包", bundle_cmd(pf, out) == "ok" and oct(os.stat(pf).st_mode & 0o777) == "0o600")
pw = open(pf).read().strip()
with open(out, "rb") as f:
    plain = D.run_openssl("-d", f.read(), pw).decode()
check("加密包里只有这 5 项（DigitalOcean、交易所、GitHub 的密钥绝不放），手机号去掉了空格和横线",
      lines(plain) == lines("".join(f"{k}={v}\n" for k, v in FAKE.items())), plain.replace(pw, "…"))
check("口令文件已经有了：接着用同一个口令", bundle_cmd(pf, out) == "ok" and open(pf).read().strip() == pw)
weak = os.path.join(tmp, "weak.txt")
with open(weak, "w") as f:
    f.write("junyu123\n")
check("口令太短：不干（加密包在公开仓库里，短口令能被硬猜）", "至少 20 位" in bundle_cmd(weak, os.path.join(tmp, "x.enc")))
nophone = os.path.join(tmp, "nophone.env")
with open(nophone, "w") as f:
    f.write("".join(f"{k}={v}\n" for k, v in FAKE.items() if k != "TG_PHONE"))
check("不给手机号也行（登录时机器人会问）", bundle_cmd(pf, os.path.join(tmp, "np.enc"), nophone) == "ok")
bad = os.path.join(tmp, "bad.env")
with open(bad, "w") as f:
    f.write("TG_API_ID=12ab\nTG_API_HASH=xyz\nTG_BOT_TOKEN=nope\n")
r = bundle_cmd(pf, os.path.join(tmp, "b.enc"), bad)
check("填错的、缺的都说出来", all(w in r for w in ("TG_API_ID 应该是纯数字", "TG_API_HASH", "TG_BOT_TOKEN 格式不对", "缺少 DEEPSEEK_API_KEY")), r)
print(f"\n{ok} passed")
