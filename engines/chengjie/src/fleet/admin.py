"""操作端 CLI（老板本机 → 主控 → 各节点）：发注册码 / 看节点 / 下任务 / 批量升级。标准库，无第三方依赖。

    set CHATX_FLEET_CONTROLLER=https://bd2026.cc/fleet
    set CHATX_FLEET_ADMIN_TOKEN=<web_admin.auth_token>
    python -m src.fleet.admin new-code --label 机房A-01 --group 机房A [--ttl-min 60]
    python -m src.fleet.admin nodes [--group 机房A]
    python -m src.fleet.admin task <node_id> ping [--payload '{"echo":1}'] [--target '{...}'] [--ttl 900]
    python -m src.fleet.admin tasks [--node <id>] [--status queued]
    python -m src.fleet.admin upgrade --manifest https://bd2026.cc/downloads/fleet/manifest.json [--group 机房A | --node <id>]

主控 API 需要 ``web_admin.auth_token``（Bearer）。URL 口径与 Agent 一致：``<controller>/api/fleet/...``
（公网 https://bd2026.cc/fleet/api/fleet/... 由 nginx 剥掉 /fleet 前缀，见 deploy/fleet/nginx-fleet.conf）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

ENV_CONTROLLER = "CHATX_FLEET_CONTROLLER"
ENV_TOKEN = "CHATX_FLEET_ADMIN_TOKEN"
TIMEOUT = 20


def api_base(url: str) -> str:
    return url.strip().rstrip("/")


class Admin:
    def __init__(self, controller: str, token: str, *, http=None) -> None:
        self.base = api_base(controller)
        self.token = token
        self.http = http or self._http

    def _http(self, method: str, url: str, body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "User-Agent": "chatx-fleet-admin"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise SystemExit(f"HTTP {e.code} {url}: {raw[:300]}")

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.http(method, self.base + path, body)

    def new_code(self, *, label: str = "", group: str = "", ttl_min: int = 0) -> Dict[str, Any]:
        body: Dict[str, Any] = {"label": label, "group_name": group}
        if ttl_min:
            body["ttl_min"] = ttl_min
        return self.call("POST", "/api/fleet/enroll-codes", body)

    def nodes(self, *, group: str = "", include_revoked: bool = False) -> List[Dict[str, Any]]:
        q = urllib.parse.urlencode({"group": group, "include_revoked": "true" if include_revoked else "false"})
        res = self.call("GET", f"/api/fleet/nodes?{q}")
        return list(res.get("nodes") or res.get("items") or [])

    def task(self, node_id: str, kind: str, *, payload: Optional[Dict[str, Any]] = None,
             target: Optional[Dict[str, Any]] = None, ttl_sec: int = 0) -> Dict[str, Any]:
        body: Dict[str, Any] = {"kind": kind, "payload": payload or {}, "target": target or {}}
        if ttl_sec:
            body["ttl_sec"] = ttl_sec
        return self.call("POST", f"/api/fleet/nodes/{node_id}/tasks", body)

    def tasks(self, *, node_id: str = "", status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        q = urllib.parse.urlencode({"node_id": node_id, "status": status, "limit": limit})
        res = self.call("GET", f"/api/fleet/tasks?{q}")
        return list(res.get("tasks") or res.get("items") or [])

    def upgrade(self, manifest: Dict[str, Any], *, node_ids: List[str]) -> List[Dict[str, Any]]:
        payload = {k: manifest.get(k) for k in ("url", "sha256", "version") if manifest.get(k)}
        if not payload.get("url") or not payload.get("sha256"):
            raise SystemExit("manifest 缺 url / sha256")
        return [self.task(n, "upgrade", payload=payload, ttl_sec=3600) for n in node_ids]


def load_manifest(src: str) -> Dict[str, Any]:
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    with open(src, "r", encoding="utf-8") as f:
        return json.load(f)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="chatx-fleet-admin", description="智控主控操作端")
    ap.add_argument("--controller", default=os.environ.get(ENV_CONTROLLER, ""), help="如 https://bd2026.cc/fleet")
    ap.add_argument("--token", default=os.environ.get(ENV_TOKEN, ""), help="主控 web_admin.auth_token")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("new-code", help="发一张一次性注册码")
    c.add_argument("--label", default="")
    c.add_argument("--group", default="")
    c.add_argument("--ttl-min", type=int, default=0)

    n = sub.add_parser("nodes")
    n.add_argument("--group", default="")
    n.add_argument("--all", action="store_true", help="含已吊销")

    t = sub.add_parser("task", help="给节点下一个任务")
    t.add_argument("node_id")
    t.add_argument("kind")
    t.add_argument("--payload", default="{}")
    t.add_argument("--target", default="{}")
    t.add_argument("--ttl", type=int, default=0)

    ts = sub.add_parser("tasks")
    ts.add_argument("--node", default="")
    ts.add_argument("--status", default="")
    ts.add_argument("--limit", type=int, default=100)

    u = sub.add_parser("upgrade", help="按 manifest.json 给节点下发 upgrade")
    u.add_argument("--manifest", required=True, help="URL 或本地路径（build_agent.py 产出）")
    u.add_argument("--group", default="")
    u.add_argument("--node", action="append", default=[])
    u.add_argument("--yes", action="store_true", help="不询问直接下发")

    args = ap.parse_args(argv)
    if not args.controller or not args.token:
        print(f"需要 --controller/--token（或环境变量 {ENV_CONTROLLER} / {ENV_TOKEN}）", file=sys.stderr)
        return 2
    adm = Admin(args.controller, args.token)

    if args.cmd == "new-code":
        res = adm.new_code(label=args.label, group=args.group, ttl_min=args.ttl_min)
        _print(res)
        code = res.get("code") or (res.get("enroll_code") or {}).get("code")
        if code:
            print(f"\n机房电脑安装命令：\n  Install-ChatXAgent.ps1 -Controller {args.controller} -Code {code}", file=sys.stderr)
        return 0
    if args.cmd == "nodes":
        rows = adm.nodes(group=args.group, include_revoked=args.all)
        for r in rows:
            print(f"{r.get('node_id','')[:14]:14} {str(r.get('status','')):8} {str(r.get('group_name') or '-'):10} "
                  f"{str(r.get('label') or r.get('host_name') or ''):24} agent={r.get('agent_version','')} app={r.get('app_version','')}")
        print(f"共 {len(rows)} 台", file=sys.stderr)
        return 0
    if args.cmd == "task":
        _print(adm.task(args.node_id, args.kind, payload=json.loads(args.payload), target=json.loads(args.target),
                        ttl_sec=args.ttl))
        return 0
    if args.cmd == "tasks":
        for r in adm.tasks(node_id=args.node, status=args.status, limit=args.limit):
            print(f"{str(r.get('task_id',''))[:14]:14} {str(r.get('node_id',''))[:14]:14} {str(r.get('kind','')):16} "
                  f"{str(r.get('status','')):9} {str(r.get('detail') or '')[:60]}")
        return 0
    if args.cmd == "upgrade":
        mf = load_manifest(args.manifest)
        if args.node:
            ids = list(args.node)
        else:
            ids = [str(r.get("node_id")) for r in adm.nodes(group=args.group)
                   if r.get("status") == "online" and str(r.get("agent_version") or "") != str(mf.get("version") or "")]
        if not ids:
            print("没有可升级的在线节点（离线或已是该版本）", file=sys.stderr)
            return 1
        print(f"将向 {len(ids)} 台节点下发 upgrade → {mf.get('version')} ({mf.get('url')})", file=sys.stderr)
        if not args.yes and (input("确认? [y/N] ").strip().lower() != "y"):
            return 1
        _print(adm.upgrade(mf, node_ids=ids))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
