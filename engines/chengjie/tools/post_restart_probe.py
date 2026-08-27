# -*- coding: utf-8 -*-
"""重启后装载验证探针（P1-3，2026-08-18）——只读，一条命令替代手写探针。

背景：`.py` 改动搭重启窗装载，「装没装好」此前靠各线手写一次性探针（本周期
已手写三轮）。本工具把探针制度化：

    python tools/post_restart_probe.py                      # 默认 zhiliao
    python tools/post_restart_probe.py --base http://127.0.0.1:18899 \
        --data-root D:/chengjie-instances/tongyi/data       # 其他实例

核心招法：**用 /openapi.json 验「路由真装载」**——flag 关的端点运行时 404，
与「路由根本没注册」的 404 无法区分；openapi 路径表登记的是注册事实，没有
这个歧义（welcome/agent-tasks 等 flag 门控端点注册无条件、门在 handler 内）。

exit：0=全过（SKIP 不算败）；1=有 FAIL；2=实例不可达/登录失败。
可挂 `restart_instance.ps1 -Probe`（报告性质，绝不影响重启结果）。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, List, Optional, Tuple

#: openapi 必须在场的路径（= 近期批次的 .py 装载证明；新批次往这里追加）
EXPECTED_PATHS = (
    "/api/agent-tasks",
    "/api/personas/{pid}/stock-readiness",
    "/api/personas/{pid}/speech-print",
    "/api/admin/crisis-referrals",
    "/api/setup/deploy-profile",
    "/api/workspace/boss-value",
)


def read_token(data_root: Path) -> str:
    try:
        import yaml
    except Exception:
        return ""
    for name in ("config.local.yaml", "config.yaml"):
        f = data_root / "config" / name
        if f.is_file():
            try:
                cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
            if tok:
                return tok
    return ""


class Probe:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))
        self.rows: List[Tuple[str, str, str]] = []

    def login(self) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(self.base + "/login", data=data,
                                     method="POST")
        try:
            with self._op.open(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            return False
        return code in (200, 303) and any(
            c.name == "session" for c in self._cj)

    def get(self, path: str, *, raw: bool = False):
        req = urllib.request.Request(
            self.base + path,
            headers={"Authorization": f"Bearer {self.token}"})
        try:
            with self._op.open(req, timeout=15) as r:
                body = r.read()
                return r.status, (body if raw else json.loads(
                    body.decode("utf-8")))
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:
            return -1, str(e)

    def note(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        print(f"[{status:4}] {name:46} {detail}")


def run(base: str, data_root: Path) -> int:
    token = read_token(data_root)
    p = Probe(base, token)

    code, _ = p.get("/login", raw=True)
    if code != 200:
        print(f"[FAIL] instance unreachable: {base}/login -> {code}")
        return 2
    p.note("instance /login", "PASS")
    if not token or not p.login():
        print("[FAIL] auth login failed (token missing/rejected)")
        return 2
    p.note("auth session", "PASS")

    # 1) 路由装载事实（openapi 路径表）
    code, spec = p.get("/openapi.json")
    if code == 200 and isinstance(spec, dict):
        paths = set((spec.get("paths") or {}).keys())
        missing = [x for x in EXPECTED_PATHS if x not in paths]
        p.note("openapi expected paths", "FAIL" if missing else "PASS",
               f"missing={missing}" if missing else f"{len(EXPECTED_PATHS)} ok")
    else:
        p.note("openapi expected paths", "SKIP", f"openapi -> {code}")

    # 2) 关键端点活体应答
    code, d = p.get("/api/admin/crisis-referrals")
    ok = code == 200 and isinstance(d, dict) and "switches" in d
    p.note("compliance crisis-referrals", "PASS" if ok else "FAIL",
           f"http={code}")

    code, d = p.get("/api/setup/deploy-profile")
    p.note("deploy-profile", "PASS" if code == 200 else "FAIL", f"http={code}")

    code, d = p.get("/api/workspace/boss-value")
    ok = code == 200 and isinstance(d, dict) and d.get("ok") is not False
    p.note("boss-value", "PASS" if ok else "FAIL", f"http={code}")

    code, d = p.get("/api/report/weekly")
    ok = code == 200 and isinstance(d, dict) and "value" in d
    p.note("weekly report value", "PASS" if ok else "FAIL", f"http={code}")

    code, d = p.get("/api/personas/profiles")
    pid = ""
    if code == 200 and isinstance(d, dict):
        profs = d.get("profiles") or {}
        if isinstance(profs, dict) and profs:
            pid = next(iter(profs.keys()))
    if pid:
        code, d = p.get(
            f"/api/personas/{urllib.parse.quote(pid)}/stock-readiness")
        ok = code == 200 and isinstance(d, dict) and "items" in d
        p.note("persona stock-readiness", "PASS" if ok else "FAIL",
               f"pid={pid} http={code}"
               + (f" {d.get('ready_count')}/{d.get('applicable_count')}"
                  if ok else ""))
    else:
        p.note("persona stock-readiness", "SKIP", "no personas")

    code, body = p.get("/static/workspace/agent-tasks-card.js", raw=True)
    p.note("agent-tasks card static", "PASS" if code == 200 else "FAIL",
           f"http={code}")

    fails = [r for r in p.rows if r[1] == "FAIL"]
    print(f"\nresult: {len([r for r in p.rows if r[1] == 'PASS'])} pass / "
          f"{len(fails)} fail / "
          f"{len([r for r in p.rows if r[1] == 'SKIP'])} skip")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="post-restart load verification")
    ap.add_argument("--base", default="http://127.0.0.1:18799")
    ap.add_argument("--data-root",
                    default=r"D:\chengjie-instances\zhiliao\data")
    args = ap.parse_args()
    return run(args.base, Path(args.data_root))


if __name__ == "__main__":
    sys.exit(main())
