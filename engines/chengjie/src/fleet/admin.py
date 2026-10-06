"""操作端 CLI（老板本机 → 主控 → 各节点）：发注册码 / 看节点 / 下任务 / 批量升级。标准库，无第三方依赖。

    set CHATX_FLEET_CONTROLLER=https://bd2026.cc/fleet
    set CHATX_FLEET_ADMIN_TOKEN=<web_admin.auth_token>
    python -m src.fleet.admin new-code --label 机房A-01 --group 机房A [--ttl-min 60]
    python -m src.fleet.admin nodes [--group 机房A]
    python -m src.fleet.admin task <node_id> ping [--payload '{"echo":1}'] [--target '{...}'] [--ttl 900]
    python -m src.fleet.admin tasks [--node <id>] [--status queued]
    python -m src.fleet.admin upgrade --manifest https://bd2026.cc/downloads/fleet/manifest.json [--group 机房A | --node <id>]
    python -m src.fleet.admin upgrade --manifest https://bd2026.cc/downloads/fleet/manifest-0.3.8.json --node <id> --setup --manage-adb-server --yes
    python -m src.fleet.admin enable-phone-adb --node <id> --yes
    python -m src.fleet.admin enable-phone-flows --node <id> --yes

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

    def pending(self) -> List[Dict[str, Any]]:
        res = self.call("GET", "/api/fleet/pending")
        return list(res.get("pending") or [])

    def approve(self, request_id: str, *, label: str = "", group: str = "",
                confirm_rotate: bool = False) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if label:
            body["label"] = label
        if group:
            body["group_name"] = group
        if confirm_rotate:
            body["confirm_rotate"] = True
        return self.call("POST", f"/api/fleet/pending/{request_id}/approve", body)

    def reject(self, request_id: str) -> Dict[str, Any]:
        return self.call("POST", f"/api/fleet/pending/{request_id}/reject", {})

    def room_key(self, *, label: str = "", group: str = "", max_uses: int = 50, ttl_hours: int = 168) -> Dict[str, Any]:
        return self.call("POST", "/api/fleet/room-keys", {
            "label": label, "group_name": group, "max_uses": max_uses, "ttl_hours": ttl_hours})

    def revoke_room_key(self, key_id: str) -> Dict[str, Any]:
        return self.call("POST", f"/api/fleet/room-keys/{key_id}/revoke", {})

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

    def upgrade(self, manifest: Dict[str, Any], *, node_ids: List[str], setup: bool = False,
                manage_adb_server: bool = False) -> List[Dict[str, Any]]:
        if setup:
            payload = {k: manifest.get(k) for k in ("setup_url", "setup_sha256", "version") if manifest.get(k)}
            if not payload.get("setup_url") or not payload.get("setup_sha256"):
                raise SystemExit("manifest 缺 setup_url / setup_sha256")
            if manage_adb_server:
                payload["manage_adb_server"] = True
            return [self.task(n, "upgrade", payload=payload, ttl_sec=3600) for n in node_ids]
        payload = {k: manifest.get(k) for k in ("url", "sha256", "version") if manifest.get(k)}
        if not payload.get("url") or not payload.get("sha256"):
            raise SystemExit("manifest 缺 url / sha256")
        return [self.task(n, "upgrade", payload=payload, ttl_sec=3600) for n in node_ids]

    def enable_phone_adb(self, node_id: str) -> Dict[str, Any]:
        return self.task(node_id, "enable_phone_adb", payload={}, ttl_sec=600)

    def enable_phone_flows(self, node_id: str) -> Dict[str, Any]:
        return self.task(node_id, "push_config", payload={"patch": {"phone_flows_enabled": True}}, ttl_sec=600)


def load_manifest(src: str) -> Dict[str, Any]:
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8-sig"))
    with open(src, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="chatx-fleet-admin", description="智拓群控 主控操作端")
    ap.add_argument("--controller", default=os.environ.get(ENV_CONTROLLER, ""), help="如 https://bd2026.cc/fleet")
    ap.add_argument("--token", default=os.environ.get(ENV_TOKEN, ""), help="主控 web_admin.auth_token")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("new-code", help="发一张一次性注册码（12 位 Crockford，默认 15 分钟，失败限速）")
    c.add_argument("--label", default="")
    c.add_argument("--group", default="")
    c.add_argument("--ttl-min", type=int, default=0)

    sub.add_parser("pending", help="列出待批准的电脑")
    a = sub.add_parser("approve", help="批准一台待批准电脑")
    a.add_argument("request_id")
    a.add_argument("--label", default="")
    a.add_argument("--group", default="", help="空则落入 pending-default；不采用安装请求里自报的分组")
    a.add_argument("--confirm-rotate", action="store_true",
                   help="machine_id 已有节点时必加：approving will rotate key of n_xxx")
    rj = sub.add_parser("reject", help="拒绝一台待批准电脑")
    rj.add_argument("request_id")
    rk = sub.add_parser("room-key", help="签发机房安装链接（自动进组，用完或到期失效）")
    rk.add_argument("--label", default="")
    rk.add_argument("--group", default="", help="必填。空分组会匹配所有未分组节点，因此拒绝签发")
    rk.add_argument("--max-uses", type=int, default=50)
    rk.add_argument("--ttl-hours", type=int, default=168)
    rr = sub.add_parser("revoke-room", help="吊销一张机房密钥")
    rr.add_argument("key_id")

    n = sub.add_parser("nodes")
    n.add_argument("--group", default="")
    n.add_argument("--all", action="store_true", help="含已吊销")

    t = sub.add_parser(
        "task",
        help="queue a task. Social payloads (post/like/comment/follow/warmup/dm/watch) "
             "accept dry_run or predict_only: JSON true returns planned taps/text and does not run adb input",
    )
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
    u.add_argument("--setup", action="store_true",
                   help="下发安装包（setup_url + setup_sha256），不换单独的 exe")
    u.add_argument("--manage-adb-server", action="store_true",
                   help="安装包加 /MANAGEADBSERVER=1（必须和 --setup 一起）")
    u.add_argument("--yes", action="store_true", help="不询问直接下发")
    e = sub.add_parser("enable-phone-adb", help="让已带 platform-tools 的节点打开 adb 并拉起自带服务")
    e.add_argument("--node", required=True)
    e.add_argument("--yes", action="store_true", help="不询问直接下发")
    f = sub.add_parser(
        "enable-phone-flows",
        help="set agent.json phone_flows_enabled true on an enrolled node (live-stream hosts refuse)",
        description=(
            "Queue push_config {patch:{phone_flows_enabled:true}} for one enrolled node. "
            "Live-stream hosts (CHATX_FLEET_LIVE_STREAM or live-stream.flag) refuse before any write. "
            "Social payloads (post/like/comment/follow/warmup/dm/watch) accept dry_run or predict_only: "
            "JSON true returns the planned taps/text and does not run adb input or mutating screenshots. "
            "Omit the flag for real execution, which still requires phone_flows_enabled. "
            "Turn flows off with: task <node> push_config --payload "
            "'{\"patch\":{\"phone_flows_enabled\":false}}'."
        ),
    )
    f.add_argument("--node", required=True)
    f.add_argument("--yes", action="store_true", help="queue the task without a confirm prompt")

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
            print("\n公开安装包不需要这张码。需要当场接入时：\n"
                  f"  Install-ChatXAgent.ps1 -Controller {args.controller} -Code {code}", file=sys.stderr)
        return 0
    if args.cmd == "pending":
        rows = adm.pending()
        for r in rows:
            inst = ",".join(str(i.get("name") or "") for i in (r.get("instances") or []) if isinstance(i, dict)) or "-"
            warn = str(r.get("warning") or "")
            print(f"{str(r.get('request_id') or ''):28} {str(r.get('host_name') or '-'):16} "
                  f"pair={str(r.get('pairing_code') or '-'):8} "
                  f"req_group={str(r.get('requested_group') or '-'):12} "
                  f"group={str(r.get('effective_group') or 'pending-default'):16} "
                  f"{str(r.get('client_ip') or '-'):16} {str(r.get('os') or '-'):16} inst={inst}"
                  + (f"  {warn}" if warn else "")
                  + ("  this machine was revoked" if r.get("was_revoked") else "")
                  + ("  DUPLICATE machine_id" if r.get("duplicate_machine") else ""))
        print(f"待批准 {len(rows)} 台", file=sys.stderr)
        return 0
    if args.cmd == "approve":
        res = adm.approve(args.request_id, label=args.label, group=args.group,
                          confirm_rotate=bool(args.confirm_rotate))
        _print({k: v for k, v in res.items() if k != "node_key"})
        return 0
    if args.cmd == "reject":
        _print(adm.reject(args.request_id))
        return 0
    if args.cmd == "room-key":
        if not str(args.group or "").strip():
            print("room-key 需要非空 --group。空分组会匹配所有未分组节点。", file=sys.stderr)
            return 2
        res = adm.room_key(label=args.label, group=args.group, max_uses=args.max_uses, ttl_hours=args.ttl_hours)
        _print(res)
        url = res.get("download_url") or ""
        if url:
            print("\n机房每台电脑打开这个链接（只显示这一次，不要发到公开下载页）：\n  " + url, file=sys.stderr)
        return 0
    if args.cmd == "revoke-room":
        _print(adm.revoke_room_key(args.key_id))
        return 0
    if args.cmd == "nodes":
        waiting = adm.pending()
        if waiting:
            print(f"待批准 {len(waiting)} 台（approve <request_id>）：", file=sys.stderr)
            for r in waiting:
                print(f"{'pending':14} {'pending':8} {str(r.get('effective_group') or 'pending-default'):16} "
                      f"{str(r.get('host_name') or r.get('request_id') or ''):24} "
                      f"pair={r.get('pairing_code') or '-'} ip={r.get('client_ip') or '-'}"
                      + (f"  {r.get('warning')}" if r.get("warning") else "")
                      + ("  this machine was revoked" if r.get("was_revoked") else "")
                      + ("  DUPLICATE machine_id" if r.get("duplicate_machine") else ""))
        rows = adm.nodes(group=args.group, include_revoked=args.all)
        for r in rows:
            print(f"{r.get('node_id','')[:14]:14} {str(r.get('state') or r.get('status') or ''):8} {str(r.get('group_name') or '-'):10} "
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
        if args.manage_adb_server and not args.setup:
            print("--manage-adb-server 需要同时加 --setup", file=sys.stderr)
            return 2
        mf = load_manifest(args.manifest)
        if args.node:
            ids = list(args.node)
        else:
            ids = [str(r.get("node_id")) for r in adm.nodes(group=args.group)
                   if r.get("state") == "online" and str(r.get("agent_version") or "") != str(mf.get("version") or "")]
        if not ids:
            print("没有可升级的在线节点（离线或已是该版本）", file=sys.stderr)
            return 1
        target = mf.get("setup_url") if args.setup else mf.get("url")
        print(f"将向 {len(ids)} 台节点下发 upgrade → {mf.get('version')} ({target})", file=sys.stderr)
        if not args.yes and (input("确认? [y/N] ").strip().lower() != "y"):
            return 1
        _print(adm.upgrade(mf, node_ids=ids, setup=bool(args.setup),
                           manage_adb_server=bool(args.manage_adb_server)))
        return 0
    if args.cmd == "enable-phone-adb":
        print(f"将向节点 {args.node} 下发 enable_phone_adb", file=sys.stderr)
        if not args.yes and (input("确认? [y/N] ").strip().lower() != "y"):
            return 1
        _print(adm.enable_phone_adb(args.node))
        return 0
    if args.cmd == "enable-phone-flows":
        print(f"queue push_config phone_flows_enabled=true for node {args.node}", file=sys.stderr)
        print("live-stream hosts refuse this write. Social dry_run/predict_only plans taps and does not run adb input.",
              file=sys.stderr)
        if not args.yes and (input("confirm? [y/N] ").strip().lower() != "y"):
            return 1
        _print(adm.enable_phone_flows(args.node))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
