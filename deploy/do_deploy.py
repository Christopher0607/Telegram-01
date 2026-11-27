#!/usr/bin/env python3
"""在 DigitalOcean 新建一台服务器跑机器人 —— 全程只调 API，不需要 SSH（给云端 Claude Code 用）。

  python deploy/do_deploy.py create --repo 用户名/仓库名 [--branch main] [--env-file deploy.env] [--bot-username xxx_bot] [--dry-run]
  python deploy/do_deploy.py status
  python deploy/do_deploy.py destroy --yes        删除服务器（服务器上的交易记录、Telegram 登录会一起删除）
  python deploy/do_deploy.py manual --repo 用户名/仓库名   生成 deploy/manual-user-data.sh：用户不用 Claude、
        自己在 DigitalOcean 网页上建服务器时贴进「Initialization scripts」的开机脚本（改了 cloud-init.sh 要重新生成）
  python deploy/do_deploy.py bundle --env-file deploy.env --pass-file /仓库外面/口令.txt   把机器人 Token、DeepSeek Key、
        Telegram API（和手机号）用口令加密成 deploy/secrets.enc（可以提交）；重开时用户只填口令。口令只告诉用户，绝不提交、不打印

变量来源：环境变量，或 --env-file 文件（KEY=VALUE，每行一个；文件里的值优先）
  必填：DIGITALOCEAN_TOKEN（要有 Write 权限） TG_API_ID TG_API_HASH TG_PHONE TG_BOT_TOKEN DEEPSEEK_API_KEY
  可选：GITHUB_READ_TOKEN（私有仓库必填：只读 Contents 权限）  TG_OWNER_ID  ANTHROPIC_API_KEY
        BITGET_API_KEY BITGET_API_SECRET BITGET_API_PASSPHRASE（一般之后在机器人里用 /bitget 设置）

服务器开机后会自动：装 Docker → 从 GitHub 拉代码 → 启动机器人 → 每 5 分钟检查 GitHub 有没有更新。
只用 Python 标准库，不需要 pip install。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.digitalocean.com/v2"
NAME = "tg-signal-trader"
HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRED = ["TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_BOT_TOKEN", "DEEPSEEK_API_KEY"]
OPTIONAL = ["TG_OWNER_ID", "ANTHROPIC_API_KEY", "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"]
NETWORK_HINT = ("如果是在 Claude Code 云端运行：请用户编辑当前环境，Network access 选 Custom，"
                "Allowed domains 加入 api.digitalocean.com，并勾选包含默认列表，保存后开一个新会话。")

# ---- 加密的密钥包（2026-11-26 用户要求：重开时只建服务器、登录 Telegram、设交易所 API，别的都存在仓库里）----
# 仓库是公开的，只能放加密后的；口令绝不提交、不打印（只在对话里告诉用户）。开机脚本用同样的参数解开（Ubuntu 自带 openssl）。
BUNDLE_KEYS = ["TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_BOT_TOKEN", "DEEPSEEK_API_KEY"]   # DigitalOcean / GitHub / 交易所的密钥绝不放
BUNDLE_PATH = os.path.join(HERE, "secrets.enc")
ENC_ARGS = "-aes-256-cbc -pbkdf2 -iter 1000000 -md sha256 -a"
PASS_CHARS = "abcdefghijkmnpqrstuvwxyz23456789"   # 32 个字符（去掉了容易看错的 l、o、0、1），24 位 = 120 bit
MIN_PASS = 20
# 没有加密包时用户在短开机脚本里自己 export 的值（2026-11-27 用户决定重开时再填；手机号不用填，登录时机器人会问）
FILL_KEYS = ["TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", "DEEPSEEK_API_KEY", "SETUP_PIN"]


def read_env_file(path: str) -> dict:
    vals = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def mask(v: str) -> str:
    return f"{v[:3]}***（{len(v)} 位）" if v else "（空）"


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            sys.exit("❌ DigitalOcean Token 无效或已过期（401）")
        if e.code == 403:
            sys.exit(f"❌ 被拒绝（403）：Token 没有 Write 权限，或网络被拦截。{NETWORK_HINT}\n   详情：{detail}")
        sys.exit(f"❌ DigitalOcean API 返回 {e.code}：{detail}")
    except urllib.error.URLError as e:
        sys.exit(f"❌ 连不上 api.digitalocean.com：{e.reason}\n   {NETWORK_HINT}")


def list_ours(token: str) -> list:
    return api("GET", f"/droplets?tag_name={NAME}&per_page=50", token).get("droplets") or []


def public_ip(d: dict) -> str | None:
    for n in (d.get("networks") or {}).get("v4") or []:
        if n.get("type") == "public":
            return n.get("ip_address")
    return None


def collect(args) -> tuple[str, dict, str]:
    filevals = read_env_file(args.env_file) if args.env_file else {}

    def val(k):
        return (filevals.get(k) or os.environ.get(k) or "").strip()

    token = val("DIGITALOCEAN_TOKEN")
    env = {k: val(k) for k in REQUIRED + OPTIONAL}
    return token, env, val("GITHUB_READ_TOKEN")


def env_errors(env: dict) -> list[str]:
    """各个值的格式（create 和 bundle 共用）。"""
    def g(k):
        return env.get(k) or ""
    errs = []
    if g("TG_API_ID") and not g("TG_API_ID").isdigit():
        errs.append("TG_API_ID 应该是纯数字")
    if g("TG_API_HASH") and not re.fullmatch(r"[0-9a-fA-F]{32}", g("TG_API_HASH")):
        errs.append("TG_API_HASH 应该是 32 位的 0-9、a-f")
    if g("TG_PHONE") and not re.fullmatch(r"\+\d{6,15}", g("TG_PHONE")):
        errs.append("TG_PHONE 要带国家码，例如 +8613800000000")
    if g("TG_BOT_TOKEN") and not re.fullmatch(r"\d+:[\w-]{20,}", g("TG_BOT_TOKEN")):
        errs.append("TG_BOT_TOKEN 格式不对（应类似 123456789:AAH...）")
    if g("TG_OWNER_ID") and not g("TG_OWNER_ID").isdigit():
        errs.append("TG_OWNER_ID 应该是纯数字")
    errs += [f"{k} 里不能有空格或换行" for k, v in env.items() if re.search(r"\s", v or "")]
    return errs


def validate(token: str, env: dict, repo: str, branch: str) -> list[str]:
    errs = [] if token else ["缺少 DIGITALOCEAN_TOKEN"]
    errs += [f"缺少 {k}" for k in REQUIRED if not env.get(k)]
    errs += env_errors(env)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        errs.append("--repo 格式应为 用户名/仓库名")
    if not re.fullmatch(r"[\w./-]+", branch or ""):
        errs.append("--branch 格式不对")
    return errs


def render_user_data(repo: str, branch: str, env: dict, gh_token: str, pin: str | None) -> str:
    url = f"https://x-access-token:{gh_token}@github.com/{repo}.git" if gh_token else f"https://github.com/{repo}.git"
    lines = [f"{k}={v}" for k, v in env.items() if v] + ([f"SETUP_PIN={pin}"] if pin else [])
    with open(os.path.join(HERE, "cloud-init.sh"), encoding="utf-8") as f:
        tpl = f.read()
    return tpl.replace("__BRANCH__", branch).replace("__REPO_URL__", url).replace("__ENV__", "\n".join(lines))


MANUAL_HEAD = """# 自己在 DigitalOcean 网页上建服务器时用（不用 Claude）：贴进建服务器页面的「Initialization scripts」。步骤见 README「自己重开」。
# 一般贴 README 里那段 4 行的短开机脚本（口令写在里面），它会下载这个文件来跑。直接整段贴这个文件也行：
#   有口令：只把下面 TGST_PASS 那一行的「填这里」换成口令。机器人 Token 这些密钥用口令加密存在仓库的 deploy/secrets.enc 里，开机时解开。
#   没有口令：把下面 6 行等号后面的「填这里」换成你自己的值（等号两边不要加空格），别的都不要动。
#   填了的值（或者短脚本里 export 的）比加密包里的优先：有口令、只换了某一个密钥，就只填那一个。
# 这个文件由 deploy/do_deploy.py manual 从 cloud-init.sh 生成。
[ -n "${TGST_PASS:-}" ] || TGST_PASS='填这里'
cat > /root/tgst.env <<'ENVEOF'
TG_API_ID=填这里
TG_API_HASH=填这里
TG_PHONE=填这里
TG_BOT_TOKEN=填这里
DEEPSEEK_API_KEY=填这里
SETUP_PIN=填这里
ENVEOF
for k in TG_API_ID TG_API_HASH TG_PHONE TG_BOT_TOKEN DEEPSEEK_API_KEY SETUP_PIN; do
  if [ -n "${!k:-}" ]; then echo "$k=${!k}" >> /root/tgst.env; fi
done
chmod 600 /root/tgst.env
"""
# 替换 cloud-init.sh 里写 .env 的那一段：有口令就解开加密包，再用上面填的值盖过去（同一个名字只留最后一个）
ENV_MERGE = """# 有口令：解开仓库里加密的密钥（deploy/secrets.enc），绑定主人的口令（SETUP_PIN）也用它；最上面填了的值优先
: > /root/tgst.dec
if [ -n "$TGST_PASS" ] && [ "$TGST_PASS" != '填这里' ]; then
  if [ -f deploy/secrets.enc ] && TGST_PASS="$TGST_PASS" openssl enc -d __ENC_ARGS__ -in deploy/secrets.enc -pass env:TGST_PASS > /root/tgst.dec 2>/dev/null \\
      && grep -q '^TG_BOT_TOKEN=' /root/tgst.dec; then
    echo "SETUP_PIN=$TGST_PASS" >> /root/tgst.dec
    echo "口令对了：用 deploy/secrets.enc 里加密的密钥"
  else
    : > /root/tgst.dec
    echo "❌ 口令不对（或者仓库里没有 deploy/secrets.enc）：只用最上面填的值"
  fi
fi
cp /root/tgst.dec .env
while IFS= read -r line; do
  case "$line" in *=填这里|"") continue ;; esac
  grep -v "^${line%%=*}=" .env > .env.tmp || true
  mv .env.tmp .env
  echo "$line" >> .env
done < /root/tgst.env
rm -f /root/tgst.env /root/tgst.dec
"""


def render_manual(repo: str, branch: str) -> str:
    """用户自己在网页上建服务器用的开机脚本：跟 render_user_data 一样，只是变量放在最上面让用户自己填，
    或者只填口令、解开仓库里加密的密钥（deploy/secrets.enc）。"""
    with open(os.path.join(HERE, "cloud-init.sh"), encoding="utf-8") as f:
        tpl = f.read()
    block = "cat > .env <<'ENVEOF'\n__ENV__\nENVEOF\n"
    if block not in tpl:
        sys.exit("❌ cloud-init.sh 里找不到写 .env 的那一段，manual 要跟着改")
    shebang, body = tpl.split("\n", 1)
    body = body.replace(block, ENV_MERGE.replace("__ENC_ARGS__", ENC_ARGS))
    body = body.replace("__BRANCH__", branch).replace("__REPO_URL__", f"https://github.com/{repo}.git")
    return shebang + "\n" + MANUAL_HEAD + body


def short_boot(repo: str, branch: str, pw: str = "你的口令", values: dict | None = None) -> str:
    """贴进 DigitalOcean「Initialization scripts」的短开机脚本（用户存在备忘录里）：下载 manual-user-data.sh 来跑。
    有加密包：只 export 口令；values 给了就 export 这些值（README 里让用户自己填的那段：FILL_KEYS 全是「填这里」）。"""
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/deploy/manual-user-data.sh"
    head = "".join(f"export {k}='{v}'\n" for k, v in values.items()) if values is not None else f"export TGST_PASS='{pw}'\n"
    return ("#!/bin/bash\n" + head
            + f"for i in 1 2 3 4 5 6; do curl -fsSL {url} -o /root/tgst-boot.sh && break; sleep 20; done\n"
            "bash /root/tgst-boot.sh\n")


def fill_boot(repo: str, branch: str) -> str:
    """README「自己重开」里让用户把「填这里」换成自己的值的那段短开机脚本。"""
    return short_boot(repo, branch, values={k: "填这里" for k in FILL_KEYS})


def cmd_manual(args):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo or ""):
        sys.exit("❌ --repo 格式应为 用户名/仓库名")
    out = os.path.join(HERE, "manual-user-data.sh")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_manual(args.repo, args.branch))
    print(f"✅ 已生成 {out}（仓库要是公开的，服务器才拉得到代码）\n贴进 DigitalOcean 的短脚本（README 里那段）：\n"
          + fill_boot(args.repo, args.branch) + "\n有加密包、口令时：\n" + short_boot(args.repo, args.branch))


def new_pass() -> str:
    return "-".join("".join(secrets.choice(PASS_CHARS) for _ in range(4)) for _ in range(6))


def run_openssl(mode: str, data: bytes, pw: str) -> bytes:
    """mode：-e 加密 / -d 解密，参数跟开机脚本里的一样。口令经环境变量传给 openssl，不出现在命令行里。"""
    r = subprocess.run(["openssl", "enc", mode, *ENC_ARGS.split(), "-pass", "env:TGST_PASS"],
                       input=data, capture_output=True, env={**os.environ, "TGST_PASS": pw})
    if r.returncode != 0:
        raise ValueError(r.stderr.decode("utf-8", "replace").strip()[:200] or "openssl 出错")
    return r.stdout


def make_bundle(env: dict, pw: str) -> bytes:
    """把 BUNDLE_KEYS 里有值的几项加密（KEY=VALUE 每行一个），并确认解得回来。"""
    plain = "".join(f"{k}={env[k]}\n" for k in BUNDLE_KEYS if env.get(k)).encode()
    enc = run_openssl("-e", plain, pw)
    if run_openssl("-d", enc, pw) != plain:
        raise ValueError("加密以后解不回原文")
    return enc


def cmd_bundle(args):
    root = os.path.dirname(HERE)
    pass_file = os.path.abspath(args.pass_file)
    if os.path.commonpath([pass_file, root]) == root:
        sys.exit("❌ 口令文件不能放在仓库里（会被提交）：放到仓库外面，例如 Claude 的 scratchpad")
    filevals = read_env_file(args.env_file) if args.env_file else {}
    env = {k: (filevals.get(k) or os.environ.get(k) or "").strip() for k in BUNDLE_KEYS}
    env["TG_PHONE"] = re.sub(r"[\s\-()]", "", env["TG_PHONE"])
    errs = [f"缺少 {k}" for k in BUNDLE_KEYS if k != "TG_PHONE" and not env[k]] + env_errors(env)
    if errs:
        sys.exit("❌ " + "；".join(errs))
    if os.path.exists(pass_file):
        with open(pass_file, encoding="utf-8") as f:
            pw = f.read().strip()
    else:
        pw = new_pass()
        with os.fdopen(os.open(pass_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(pw + "\n")
    if len(pw) < MIN_PASS or re.search(r"[\s'\"\\$`]", pw):
        sys.exit(f"❌ 口令至少 {MIN_PASS} 位、不能有空格和引号：加密包放在公开仓库里，口令短了能被硬猜出来")
    with open(args.out, "wb") as f:
        f.write(make_bundle(env, pw))
    keys = [k for k in BUNDLE_KEYS if env[k]]
    print(f"✅ 已加密写入 {args.out}：{'、'.join(keys)}" + ("" if env["TG_PHONE"] else "（没有手机号：登录时机器人会问）"))
    print(f"   口令在 {pass_file}（不要提交、不要打印）")


def cmd_create(args):
    token, env, gh = collect(args)
    errs = validate(token, env, args.repo, args.branch)
    if errs:
        sys.exit("❌ " + "；".join(errs))
    pin = None if env["TG_OWNER_ID"] else str(secrets.randbelow(900000) + 100000)
    ud = render_user_data(args.repo, args.branch, env, gh, pin)
    if len(ud.encode()) > 60000:
        sys.exit("❌ user_data 超过 DigitalOcean 的 64KB 限制")
    body = {"name": NAME, "region": args.region, "size": args.size, "image": args.image,
            "user_data": ud, "tags": [NAME], "monitoring": True}

    print("变量检查（只显示前 3 位）：")
    for k in REQUIRED + OPTIONAL:
        print(f"  {k}: {mask(env[k])}")
    print(f"  GITHUB_READ_TOKEN: {mask(gh)}")
    if not gh:
        print("  ⚠️ 没有 GITHUB_READ_TOKEN：仓库必须是公开的，否则服务器拉不到代码")

    if args.dry_run:
        shown = ud
        for v in sorted([v for v in list(env.values()) + [gh, pin or ""] if v], key=len, reverse=True):
            shown = shown.replace(v, mask(v))
        print(f"\n[dry-run] 将创建：{NAME}｜{args.region}｜{args.size}｜{args.image}｜仓库 {args.repo}@{args.branch}")
        print("[dry-run] user_data（密钥已打码）：\n" + shown)
        return

    existing = list_ours(token)
    if existing and not args.force:
        for d in existing:
            print(f"已存在：{d['name']}（id {d['id']}）IP {public_ip(d)} 状态 {d['status']}")
        sys.exit("⚠️ 已经有一台本项目的服务器，没有重复创建。确实要再建一台请加 --force")

    d = api("POST", "/droplets", token, body)["droplet"]
    print(f"\n⏳ 已提交创建：{NAME}（id {d['id']}），等待分配 IP……")
    ip = None
    for _ in range(60):
        time.sleep(5)
        d = api("GET", f"/droplets/{d['id']}", token)["droplet"]
        ip = public_ip(d)
        if d.get("status") == "active" and ip:
            break
    print(f"✅ 服务器已开机：IP {ip}｜{args.region}｜{args.size}（约 6 美元/月）")
    print("⏳ 它正在自动安装 Docker、拉取代码、启动机器人，大约需要 5~10 分钟。")
    if pin:
        link = f"https://t.me/{args.bot_username.lstrip('@')}?start={pin}" if args.bot_username else ""
        print(f"👉 请用户在 Telegram 里给自己的通知机器人发送：/start {pin}" + (f"\n   或者直接点开：{link}" if link else ""))
        print("   （装好之前发也可以，机器人启动后会读到）绑定成功后，按机器人提示发送登录验证码（数字之间加空格）。")
    else:
        print("👉 装好后机器人会主动在 Telegram 给用户发消息，按提示发送登录验证码（数字之间加空格）。")
    print(f"🔑 以后在 Bitget 创建 API 时，IP 白名单填：{ip}")


def cmd_status(args):
    token, _, _ = collect(args)
    if not token:
        sys.exit("❌ 缺少 DIGITALOCEAN_TOKEN")
    ds = list_ours(token)
    if not ds:
        print("还没有本项目的服务器")
    for d in ds:
        print(f"{d['name']}（id {d['id']}）IP {public_ip(d)}｜状态 {d['status']}｜{d['region']['slug']}｜"
              f"{d['size_slug']}｜创建于 {d['created_at']}")


def cmd_destroy(args):
    if not args.yes:
        sys.exit("❌ 删除服务器会丢失交易记录和 Telegram 登录，确认要删请加 --yes")
    token, _, _ = collect(args)
    for d in list_ours(token):
        api("DELETE", f"/droplets/{d['id']}", token)
        print(f"🗑 已删除 {d['name']}（id {d['id']}，IP {public_ip(d)}）")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", help="KEY=VALUE 格式的变量文件，例如 deploy.env（已在 .gitignore 里）")
    p = argparse.ArgumentParser(description="DigitalOcean 部署（无需 SSH）")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", parents=[common])
    c.add_argument("--repo", required=True, help="GitHub 仓库，格式 用户名/仓库名")
    c.add_argument("--branch", default="main")
    c.add_argument("--region", default="sgp1")
    c.add_argument("--size", default="s-1vcpu-1gb")
    c.add_argument("--image", default="ubuntu-24-04-x64")
    c.add_argument("--bot-username", default="", help="通知机器人的用户名，用来生成一键绑定链接")
    c.add_argument("--dry-run", action="store_true", help="只检查和预览，不真正创建")
    c.add_argument("--force", action="store_true", help="已有服务器时仍然再建一台")
    sub.add_parser("status", parents=[common])
    d = sub.add_parser("destroy", parents=[common])
    d.add_argument("--yes", action="store_true")
    m = sub.add_parser("manual")
    m.add_argument("--repo", required=True, help="GitHub 仓库，格式 用户名/仓库名（要是公开的）")
    m.add_argument("--branch", default="main")
    b = sub.add_parser("bundle", parents=[common])
    b.add_argument("--pass-file", required=True, help="口令文件（没有就新生成一个写进去）：放在仓库外面，不要提交")
    b.add_argument("--out", default=BUNDLE_PATH)
    args = p.parse_args()
    {"create": cmd_create, "status": cmd_status, "destroy": cmd_destroy, "manual": cmd_manual,
     "bundle": cmd_bundle}[args.cmd](args)


if __name__ == "__main__":
    main()
