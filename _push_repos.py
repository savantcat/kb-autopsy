#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 kb-autopsy 建仓并推送到 Gitee / GitHub（幂等）。

凭证：运行时从 ~/AppData/Local/hermes/.env 读 GITEE_TOKEN / GITHUB_TOKEN，
      不落命令行、不打印、不进 shell 历史。

用法：
  python _push_repos.py            # 两个都推
  python _push_repos.py gitee
  python _push_repos.py github
"""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", ".env")

REPO_NAME = "kb-autopsy"
DESC = ("Knowledge-base autopsy MCP server: gives a knowledge base a 0-100 "
        "zombie index + five pathology metrics. Zero dependencies, read-only.")
HOME = "https://savantcat.cn/"
TOPICS = ["mcp", "mcp-server", "knowledge-base", "rag", "audit", "healthcheck",
          "llm", "ai", "python", "china"]


def load_env():
    out = {}
    if not os.path.exists(ENV):
        return out
    for line in open(ENV, encoding="utf-8", errors="ignore"):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if m:
            v = m.group(2).strip().strip('"').strip("'")
            if v:
                out[m.group(1)] = v
    return out


def api(url, token, method="GET", payload=None, form=False, query=False):
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
        else:
            data = json.dumps(payload).encode()
    if query and token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={urllib.parse.quote(token)}"
    req = urllib.request.Request(url, data=data, method=method)
    if form:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        req.add_header("Content-Type", "application/json")
    if not query:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "savantcat-agent")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            b = r.read().decode()
            return r.status, (json.loads(b) if b else {})
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, {"raw": b[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


def git(args, extra_header=None):
    cmd = ["git"] + (["-c", "http.extraheader=" + extra_header] if extra_header else []) + args
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr).strip()


def ensure_repo_and_push(kind, token, login_default=None):
    """kind: 'github' | 'gitee'"""
    print(f"\n=== {kind.title()} ===")
    gq = (kind == "gitee")
    if kind == "github":
        who, repos_api, home = "https://api.github.com/user", "https://api.github.com/user/repos", "https://github.com"
        host_api = "https://api.github.com/repos"
    else:
        who, repos_api, home = "https://gitee.com/api/v5/user", "https://gitee.com/api/v5/user/repos", "https://gitee.com"
        host_api = "https://gitee.com/api/v5/repos"

    st, me = api(who, token, query=gq)
    if st != 200:
        print(f"  [X] token 无效 (HTTP {st}) {str(me)[:200]}")
        return False
    login = me.get("login")
    print(f"  [OK] 身份: {login}")

    st, _ = api(f"{host_api}/{login}/{REPO_NAME}", token, query=gq)
    if st == 404:
        body = {"name": REPO_NAME, "description": DESC[:120], "homepage": HOME,
                "private": "false", "has_issues": "true", "auto_init": "false"}
        if kind == "gitee":
            body["public"] = "true"        # Gitee: private:false 不生效，必须显式 public
        st, r = api(repos_api, token, "POST", body, form=gq, query=gq)
        print("  " + ("[OK] 已创建仓库" if st in (200, 201) else f"[X] 建仓失败 HTTP {st}: {str(r)[:250]}"))
        if st not in (200, 201):
            return False
    else:
        print("  [--] 仓库已存在，直接推送")

    url = f"{home}/{login}/{REPO_NAME}.git"
    auth = "Authorization: Basic " + base64.b64encode(f"{login}:{token}".encode()).decode()
    remote = "origin" if kind == "github" else "gitee"
    git(["remote", "remove", remote])
    git(["remote", "add", remote, url])
    rc, out = git(["push", "-u", remote, "main"], extra_header=auth)
    print("  " + ("[OK] 推送成功" if rc == 0 else "[X] 推送失败"))
    if out:
        print("      " + out.replace("\n", "\n      ")[:800])
    if rc == 0 and kind == "github":
        st, _ = api(f"{host_api}/{login}/{REPO_NAME}/topics", token, "PUT", {"names": TOPICS})
        print(f"  {'[OK] topics 已设置' if st in (200, 201) else f'[--] topics HTTP {st}'} ({len(TOPICS)} 个)")
    if rc == 0:
        print(f"  -> {url[:-4]}")
    return rc == 0


def main():
    env = load_env()
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    # 0) git init + commit
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        print("[1] git init")
        print("   ", git(["init", "-q"])[1] or "ok")
        git(["branch", "-m", "main"])
    git(["add", "-A"])
    rc, out = git(["status", "--porcelain"])
    if out:
        git(["-c", "user.name=savantcat", "-c", "user.email=savantcat@users.noreply.gitee.com",
             "commit", "-q", "-m", "kb-autopsy: 知识库尸检 MCP（僵尸指数 + 五项病理指标）"])
        print("[2] 已提交:", len(out.splitlines()), "个文件")
    else:
        print("[2] 无待提交改动")

    results = {}
    if target in ("all", "github"):
        t = env.get("GITHUB_TOKEN")
        print("\n=== GitHub ===" if not t else "")
        results["GitHub"] = ensure_repo_and_push("github", t) if t else (print("  [--] .env 无 GITHUB_TOKEN，跳过") or False)
    if target in ("all", "gitee"):
        t = env.get("GITEE_TOKEN")
        results["Gitee"] = ensure_repo_and_push("gitee", t) if t else (print("=== Gitee ===\n  [--] .env 无 GITEE_TOKEN，跳过") or False)

    print("\n=== 结果 ===")
    for k, v in results.items():
        print(f"  {k}: {'成功' if v else '失败/跳过'}")
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
