#!/usr/bin/env python3
"""在 DigitalOcean 新建一台服务器跑机器人 —— 全程只调 API，不需要 SSH（给云端 Claude Code 用）。

  python deploy/do_deploy.py create --repo 用户名/仓库名 [--branch main] [--env-file deploy.env] [--bot-username xxx_bot] [--dry-run]
  python deploy/do_deploy.py status
  python deploy/do_deploy.py destroy --yes        删除服务器（服务器上的交易记录、Telegram 登录会一起删除）

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


def validate(token: str, env: dict, repo: str, branch: str) -> list[str]:
    errs = [] if token else ["缺少 DIGITALOCEAN_TOKEN"]
    errs += [f"缺少 {k}" for k in REQUIRED if not env.get(k)]
    if env["TG_API_ID"] and not env["TG_API_ID"].isdigit():
        errs.append("TG_API_ID 应该是纯数字")
    if env["TG_PHONE"] and not re.fullmatch(r"\+\d{6,15}", env["TG_PHONE"]):
        errs.append("TG_PHONE 要带国家码，例如 +8613800000000")
    if env["TG_BOT_TOKEN"] and not re.fullmatch(r"\d+:[\w-]{20,}", env["TG_BOT_TOKEN"]):
        errs.append("TG_BOT_TOKEN 格式不对（应类似 123456789:AAH...）")
    if env["TG_OWNER_ID"] and not env["TG_OWNER_ID"].isdigit():
        errs.append("TG_OWNER_ID 应该是纯数字")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        errs.append("--repo 格式应为 用户名/仓库名")
    if not re.fullmatch(r"[\w./-]+", branch or ""):
        errs.append("--branch 格式不对")
    errs += [f"{k} 里不能有换行" for k, v in env.items() if "\n" in v or "\r" in v]
    return errs


def render_user_data(repo: str, branch: str, env: dict, gh_token: str, pin: str | None) -> str:
    url = f"https://x-access-token:{gh_token}@github.com/{repo}.git" if gh_token else f"https://github.com/{repo}.git"
    lines = [f"{k}={v}" for k, v in env.items() if v] + ([f"SETUP_PIN={pin}"] if pin else [])
    with open(os.path.join(HERE, "cloud-init.sh"), encoding="utf-8") as f:
        tpl = f.read()
    return tpl.replace("__BRANCH__", branch).replace("__REPO_URL__", url).replace("__ENV__", "\n".join(lines))


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
    args = p.parse_args()
    {"create": cmd_create, "status": cmd_status, "destroy": cmd_destroy}[args.cmd](args)


if __name__ == "__main__":
    main()
