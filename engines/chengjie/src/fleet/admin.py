"""操作端 CLI（老板本机 → 主控 → 各节点）：发注册码 / 看节点 / 下任务 / 批量升级。标准库，无第三方依赖。

    set CHATX_FLEET_CONTROLLER=https://bd2026.cc/fleet
    set CHATX_FLEET_ADMIN_TOKEN=<web_admin.auth_token>
    python -m src.fleet.admin new-code --label 机房A-01 --group 机房A [--ttl-min 60]
    python -m src.fleet.admin nodes [--group 机房A]
    python -m src.fleet.admin task <node_id> ping [--payload '{"echo":1}'] [--target '{...}'] [--ttl 900]
    python -m src.fleet.admin tasks [--node <id>] [--status queued]
    python -m src.fleet.admin upgrade --manifest https://bd2026.cc/downloads/fleet/manifest.json [--group 机房A | --node <id>]
    python -m src.fleet.admin caps                                   # 能力 / 远程操作开关（剩余分钟）/ 手机错误
    python -m src.fleet.admin remote <node_id> on --minutes 30 --machine-id <mid>   # 到点主控自动关
    python -m src.fleet.admin phone-op <node_id> <serial> screenshot  # 远程手机操作（受保护手机主控直接拒）
    python -m src.fleet.admin task-show <task_id> [--save-png shot.png]
    python -m src.fleet.admin phone-tasks <node_id> / phones / update / revoke

主控 API 需要 ``web_admin.auth_token``（Bearer）。URL 口径与 Agent 一致：``<controller>/api/fleet/...``
（公网 https://bd2026.cc/fleet/api/fleet/... 由 nginx 剥掉 /fleet 前缀，见 deploy/fleet/nginx-fleet.conf）。
没有 token 时可以用主控的操作员账号登录（会话 + CSRF）：``--env-file`` / ``CHATX_FLEET_ENV_FILE`` 指向
launch.env 一类的 KEY=VALUE 文件，读 ``FLEET_OPERATOR_USER`` / ``FLEET_OPERATOR_PASSWORD``（也认 token 和
controller 地址）。凭据只在内存里用，从不打印；输出里带 key / secret / token / password / cookie / hash 的字段一律去掉。
"""

from __future__ import annotations

import argparse
import base64
import datetime
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

ENV_CONTROLLER = "CHATX_FLEET_CONTROLLER"
ENV_TOKEN = "CHATX_FLEET_ADMIN_TOKEN"
ENV_FILE = "CHATX_FLEET_ENV_FILE"
ENV_OP_USER = "FLEET_OPERATOR_USER"
ENV_OP_PASSWORD = "FLEET_OPERATOR_PASSWORD"
# env 文件里也认这些名字（按顺序取第一个非空）
FILE_CONTROLLER_KEYS = (ENV_CONTROLLER, "FLEET_CONTROLLER_URL")
FILE_TOKEN_KEYS = (ENV_TOKEN, "FLEET_OPERATOR_TOKEN")
TIMEOUT = 20
REMOTE_OPS_MAX_MIN = 240          # 与 src.fleet.store.REMOTE_OPS_MAX_MIN 一致（此处不 import，保持标准库独立运行）
SENSITIVE = ("key", "secret", "token", "password", "cookie", "hash")
TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def api_base(url: str) -> str:
    return url.strip().rstrip("/")


def read_env_file(path: str) -> Dict[str, str]:
    """KEY=VALUE 文件（launch.env 口径：# 注释、可选 export 前缀、可选成对引号）。读不到 → {}。"""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def scrub(obj: Any, depth: int = 0) -> Any:
    """去掉疑似凭据字段（键名含 key/secret/token/password/cookie/hash）；截图 base64 换成长度。"""
    if depth > 6:
        return "{...}"
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk == "png_b64":
                s = str(v or "")
                out["png_b64_len"] = len(s)
                out["png_ok"] = s.startswith("iVBORw0KGgo")
                continue
            if any(b in lk for b in SENSITIVE):
                continue
            out[k] = scrub(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [scrub(x, depth + 1) for x in obj]
    return obj


def ts8(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v or "-")
    if f <= 0:
        return "-"
    return datetime.datetime.fromtimestamp(f, TZ8).strftime("%m-%d %H:%M:%S UTC+8")


def remote_ops_text(node: Dict[str, Any]) -> str:
    if not node.get("remote_ops_enabled"):
        return "off"
    left = node.get("remote_ops_remaining_sec")
    if isinstance(left, (int, float)) and left > 0:
        return f"on(剩{max(1, int(left // 60) + (1 if left % 60 else 0))}分 by {node.get('remote_ops_enabled_by') or '-'})"
    return "on"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 - 登录成功是 303，要自己看 Set-Cookie
        return None


class SessionHttp:
    """操作员账号登录主控（POST <controller>/login → session cookie），写请求带 X-CSRF-Token。
    口令只在 login() 里用一次，不进异常信息。"""

    def __init__(self, base: str, username: str, password: str, *, opener=None) -> None:
        self.base = api_base(base)
        self._user = username
        self._password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect)
        self.logged_in = False

    def cookie(self, name: str) -> str:
        for c in self.jar:
            if c.name == name and c.value:
                return c.value
        return ""

    def _send(self, method: str, url: str, data: Optional[bytes], headers: Dict[str, str]):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, (e.read() if e.fp else b"")

    def login(self) -> None:
        form = urllib.parse.urlencode({"username": self._user, "password": self._password, "manual": "1"}).encode()
        st, _ = self._send("POST", self.base + "/login", form, {
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html",
            "User-Agent": "chatx-fleet-admin"})
        if st not in (302, 303) or not self.cookie("session"):
            raise SystemExit(f"操作员登录失败（HTTP {st}）；检查 {ENV_OP_USER} / {ENV_OP_PASSWORD}")
        self.logged_in = True

    def __call__(self, method: str, url: str, body: Optional[Dict[str, Any]],
                 headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if not self.logged_in:
            self.login()
        h = {"Accept": "application/json", "User-Agent": "chatx-fleet-admin"}
        if method != "GET":
            if not self.cookie("csrf_token"):
                self._send("GET", self.base + "/api/fleet/pending", None, dict(h))
            h["X-CSRF-Token"] = self.cookie("csrf_token")
            h["Content-Type"] = "application/json; charset=utf-8"
        h.update(headers or {})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        st, raw = self._send(method, url, data, h)
        if st >= 400:
            raise SystemExit(f"HTTP {st} {url}: {raw[:300].decode('utf-8', 'replace')}")
        return json.loads(raw.decode("utf-8") or "{}")


class Admin:
    def __init__(self, controller: str, token: str, *, http: Optional[Callable[..., Dict[str, Any]]] = None) -> None:
        self.base = api_base(controller)
        self.token = token
        self.http = http or self._http

    def _http(self, method: str, url: str, body: Optional[Dict[str, Any]],
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
             "User-Agent": "chatx-fleet-admin"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise SystemExit(f"HTTP {e.code} {url}: {raw[:300]}")

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if headers:
            return self.http(method, self.base + path, body, headers)
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

    def get_node(self, node_id: str) -> Dict[str, Any]:
        res = self.call("GET", f"/api/fleet/nodes/{urllib.parse.quote(node_id, safe='')}")
        return dict(res.get("node") or {})

    def update_node(self, node_id: str, *, label: Optional[str] = None, group: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if label is not None:
            body["label"] = label
        if group is not None:
            body["group_name"] = group
        return self.call("POST", f"/api/fleet/nodes/{urllib.parse.quote(node_id, safe='')}", body)

    def set_remote_ops(self, node_id: str, enabled: bool, *, minutes: int = 0) -> Dict[str, Any]:
        body: Dict[str, Any] = {"remote_ops_enabled": bool(enabled)}
        if enabled and minutes:
            body["remote_ops_minutes"] = int(minutes)
        return self.call("POST", f"/api/fleet/nodes/{urllib.parse.quote(node_id, safe='')}", body)

    def revoke(self, node_id: str) -> Dict[str, Any]:
        return self.call("POST", f"/api/fleet/nodes/{urllib.parse.quote(node_id, safe='')}/revoke", {})

    def phone_op(self, node_id: str, serial: str, op: str, *, payload: Optional[Dict[str, Any]] = None,
                 actor: str = "") -> Dict[str, Any]:
        path = (f"/api/fleet/nodes/{urllib.parse.quote(node_id, safe='')}/phones/"
                f"{urllib.parse.quote(serial, safe='')}/{urllib.parse.quote(op, safe='')}")
        return self.call("POST", path, payload or {}, {"X-Fleet-Actor": actor} if actor else None)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        res = self.call("GET", f"/api/fleet/tasks/{urllib.parse.quote(task_id, safe='')}")
        return dict(res.get("task") or res)

    def upgrade(self, manifest: Dict[str, Any], *, node_ids: List[str]) -> List[Dict[str, Any]]:
        payload = {k: manifest.get(k) for k in ("url", "sha256", "version") if manifest.get(k)}
        if not payload.get("url") or not payload.get("sha256"):
            raise SystemExit("manifest 缺 url / sha256")
        return [self.task(n, "upgrade", payload=payload, ttl_sec=3600) for n in node_ids]


def load_manifest(src: str) -> Dict[str, Any]:
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8-sig"))
    with open(src, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _first(d: Dict[str, str], keys) -> str:
    for k in keys:
        if str(d.get(k) or "").strip():
            return str(d[k]).strip()
    return ""


def build_admin(controller: str, token: str, env_file: str = "") -> Optional[Admin]:
    """命令行 / 环境变量优先，其次 env 文件；有 token 用 Bearer，否则用操作员账号登录。凑不齐 → None。"""
    fenv = read_env_file(env_file) if env_file else {}
    controller = controller or _first(fenv, FILE_CONTROLLER_KEYS)
    token = token or _first(fenv, FILE_TOKEN_KEYS)
    if not controller:
        return None
    if token:
        return Admin(controller, token)
    user = os.environ.get(ENV_OP_USER, "") or fenv.get(ENV_OP_USER, "")
    pw = os.environ.get(ENV_OP_PASSWORD, "") or fenv.get(ENV_OP_PASSWORD, "")
    if user and pw:
        return Admin(controller, "", http=SessionHttp(controller, user, pw))
    return None


def _find_node(adm: Admin, node_id: str, machine_id: str = "") -> Dict[str, Any]:
    rows = [r for r in adm.nodes(include_revoked=True) if r.get("node_id") == node_id]
    if len(rows) != 1:
        raise SystemExit(f"SKIP {node_id}: 找不到节点")
    if machine_id and str(rows[0].get("machine_id") or "") != machine_id:
        raise SystemExit(f"SKIP {node_id}: machine_id 对不上，没有改动")
    return rows[0]


def _json_arg(raw: str, what: str) -> Dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
    except ValueError:
        raise SystemExit(f"{what} 不是合法 JSON")
    if not isinstance(v, dict):
        raise SystemExit(f"{what} 必须是 JSON 对象")
    return v


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="chatx-fleet-admin", description="智拓群控 主控操作端")
    ap.add_argument("--controller", default=os.environ.get(ENV_CONTROLLER, ""), help="如 https://bd2026.cc/fleet")
    ap.add_argument("--token", default=os.environ.get(ENV_TOKEN, ""), help="主控 web_admin.auth_token")
    ap.add_argument("--env-file", default=os.environ.get(ENV_FILE, ""),
                    help=f"KEY=VALUE 文件（如 launch.env）：没有 token 时读 {ENV_OP_USER} / {ENV_OP_PASSWORD} 登录")
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
    a.add_argument("--expect-machine", default="", help="先核对待批准记录的 machine_id 前缀，对不上就不批")
    a.add_argument("--expect-pairing", default="", help="先核对配对码（不分大小写），对不上就不批")
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

    cp = sub.add_parser("caps", help="每台节点的能力 / 远程操作开关（剩余分钟）/ 手机错误")
    cp.add_argument("--group", default="")
    ro = sub.add_parser("remote", help="开 / 关某台节点的远程操作（开启有时限，到点主控自动关）")
    ro.add_argument("node_id")
    ro.add_argument("state", choices=["on", "off"])
    ro.add_argument("--minutes", type=int, default=0, help=f"多少分钟后自动关（1–{REMOTE_OPS_MAX_MIN}；不填用主控默认）")
    ro.add_argument("--machine-id", default="", help="先核对 machine_id，防止打错节点")
    po = sub.add_parser("phone-op", help="远程手机操作：screenshot / tap / swipe / text / key 等（以主控为准）")
    po.add_argument("node_id")
    po.add_argument("serial")
    po.add_argument("op")
    po.add_argument("--payload", default="{}")
    po.add_argument("--actor", default="cli", help="审计标注（任务 created_by 里的 via 部分）")
    tsh = sub.add_parser("task-show", help="看任务详情（截图只显示长度，可 --save-png 存盘）")
    tsh.add_argument("task_id", nargs="+")
    tsh.add_argument("--save-png", default="", help="只给一个 task_id 时，把截图存到这个路径")
    pt = sub.add_parser("phone-tasks", help="某台节点的 phone_* 任务")
    pt.add_argument("node_id")
    pt.add_argument("--limit", type=int, default=200)
    ph = sub.add_parser("phones", help="每台节点心跳里的手机")
    ph.add_argument("--group", default="")
    up = sub.add_parser("update", help="改节点备注 / 分组（先核对 machine_id）")
    up.add_argument("node_id")
    up.add_argument("--machine-id", required=True)
    up.add_argument("--label", default=None)
    up.add_argument("--group", default=None)
    rv = sub.add_parser("revoke", help="吊销一台离线节点（核对 machine_id 与主机名；在线的拒绝）")
    rv.add_argument("node_id")
    rv.add_argument("--machine-id", required=True)
    rv.add_argument("--host", required=True)

    u = sub.add_parser("upgrade", help="按 manifest.json 给节点下发 upgrade")
    u.add_argument("--manifest", required=True, help="URL 或本地路径（build_agent.py 产出）")
    u.add_argument("--group", default="")
    u.add_argument("--node", action="append", default=[])
    u.add_argument("--yes", action="store_true", help="不询问直接下发")

    args = ap.parse_args(argv)
    adm = build_admin(args.controller, args.token, args.env_file)
    if adm is None:
        print(f"需要 --controller/--token（或环境变量 {ENV_CONTROLLER} / {ENV_TOKEN}），"
              f"或用 --env-file / {ENV_FILE} 提供操作员账号 {ENV_OP_USER} / {ENV_OP_PASSWORD}", file=sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

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
        if args.expect_machine or args.expect_pairing:
            rows = [r for r in adm.pending() if r.get("request_id") == args.request_id]
            if not rows:
                print(f"SKIP {args.request_id}: 不在待批准列表", file=sys.stderr)
                return 1
            r = rows[0]
            if args.expect_machine and not str(r.get("machine_id") or "").startswith(args.expect_machine):
                print(f"SKIP {args.request_id}: machine_id 对不上", file=sys.stderr)
                return 1
            if args.expect_pairing and str(r.get("pairing_code") or "").upper() != args.expect_pairing.upper():
                print(f"SKIP {args.request_id}: 配对码对不上", file=sys.stderr)
                return 1
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
    if args.cmd == "caps":
        for r in adm.nodes(group=args.group):
            print(f"{str(r.get('node_id') or '')[:14]:14} {str(r.get('state') or ''):8} "
                  f"{str(r.get('label') or r.get('host_name') or '')[:24]:24} agent={r.get('agent_version') or '-'} "
                  f"caps={','.join(str(c) for c in (r.get('caps') or [])) or '-'} remote_ops={remote_ops_text(r)}"
                  + (f" phones_error={r.get('phones_error')}" if r.get("phones_error") else ""))
        return 0
    if args.cmd == "remote":
        on = args.state == "on"
        if on and args.minutes and not 1 <= args.minutes <= REMOTE_OPS_MAX_MIN:
            print(f"--minutes 要在 1–{REMOTE_OPS_MAX_MIN}", file=sys.stderr)
            return 2
        _find_node(adm, args.node_id, args.machine_id)
        node = adm.set_remote_ops(args.node_id, on, minutes=args.minutes).get("node") or {}
        left = node.get("remote_ops_remaining_sec")
        print(f"REMOTE {args.node_id} -> remote_ops={remote_ops_text(node)}"
              + (f" 到期 {ts8(node.get('remote_ops_expires_at'))}" if on and left else ""))
        return 0
    if args.cmd == "phone-op":
        res = adm.phone_op(args.node_id, args.serial, args.op, payload=_json_arg(args.payload, "--payload"),
                           actor=args.actor)
        t = res.get("task") or {}
        print(f"PHONEOP {args.node_id} {args.serial} {args.op} -> task_id={t.get('task_id')} status={t.get('status')}"
              + (f" detail={res.get('detail')}" if res.get("detail") else ""))
        return 0
    if args.cmd == "task-show":
        for tid in args.task_id:
            t = adm.get_task(tid)
            res = t.get("result") if isinstance(t.get("result"), dict) else {}
            if args.save_png and len(args.task_id) == 1:
                png = str(res.get("png_b64") or "")
                if not png:
                    print(f"{tid}: 没有截图", file=sys.stderr)
                    return 1
                with open(args.save_png, "wb") as f:
                    f.write(base64.b64decode(png))
                print(f"已保存 {args.save_png}", file=sys.stderr)
            row = scrub({k: t.get(k) for k in ("task_id", "node_id", "kind", "status", "detail",
                                               "created_by", "target", "result")})
            row.update({"created_at": ts8(t.get("created_at")), "pulled_at": ts8(t.get("pulled_at")),
                        "acked_at": ts8(t.get("acked_at"))})
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "phone-tasks":
        for r in adm.tasks(node_id=args.node_id, limit=args.limit):
            if not str(r.get("kind") or "").startswith("phone_"):
                continue
            print(f"{str(r.get('task_id') or '')[:14]:14} {str(r.get('kind') or ''):18} {str(r.get('status') or ''):9} "
                  f"serial={(r.get('target') or {}).get('serial') or '-'} by={r.get('created_by') or '-'} "
                  f"{ts8(r.get('created_at'))} {str(r.get('detail') or '')[:50]}")
        return 0
    if args.cmd == "phones":
        for r in adm.nodes(group=args.group):
            hb = r.get("last_heartbeat") if isinstance(r.get("last_heartbeat"), dict) else {}
            phones = r.get("phones") if "phones" in r else hb.get("phones")
            err = r.get("phones_error") if "phones_error" in r else hb.get("phones_error")
            items = [f"{p.get('serial')}:{p.get('state')}" for p in (phones or []) if isinstance(p, dict)]
            print(f"{str(r.get('node_id') or '')[:14]:14} {str(r.get('state') or ''):8} "
                  f"{str(r.get('label') or r.get('host_name') or '')[:24]:24} "
                  f"phones={len(items) if isinstance(phones, list) else '-'} {' '.join(items)}"
                  + (f" error={err}" if err else ""))
        return 0
    if args.cmd == "update":
        if args.label is None and args.group is None:
            print("没有要改的字段（--label / --group）", file=sys.stderr)
            return 2
        _find_node(adm, args.node_id, args.machine_id)
        n = adm.update_node(args.node_id, label=args.label, group=args.group).get("node") or {}
        print(f"UPDATE {args.node_id} -> label={n.get('label')} group={n.get('group_name')}")
        return 0
    if args.cmd == "revoke":
        node = _find_node(adm, args.node_id, args.machine_id)
        if str(node.get("host_name") or "") != args.host:
            print(f"SKIP {args.node_id}: 主机名对不上", file=sys.stderr)
            return 1
        if node.get("state") == "online":
            print(f"SKIP {args.node_id}: 节点在线，拒绝吊销（先停 Agent 或确认无误后在控制台操作）", file=sys.stderr)
            return 1
        _print(scrub(adm.revoke(args.node_id)))
        return 0
    if args.cmd == "upgrade":
        mf = load_manifest(args.manifest)
        if args.node:
            ids = list(args.node)
        else:
            ids = [str(r.get("node_id")) for r in adm.nodes(group=args.group)
                   if r.get("state") == "online" and str(r.get("agent_version") or "") != str(mf.get("version") or "")]
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
