# -*- coding: utf-8 -*-
"""platform/replybus/client.py — 决策回执总线『瘦客户端』(纯 stdlib，可降级，同步问答)。

见 CONTRACT.md：产业链去重后，**TG-AI智控王 = 执行层**(拥有并操作 Telegram .session、负责真正
发消息)，**chengjie = 承接大脑**(决定怎么回)。智控王收到一条入站私信 → 拿消息信封问
chengjie 的 /api/replybus/decide『这条怎么回』→ chengjie 返回**决策**(send/draft/silent/handoff)
→ 智控王在**自己的 session 上**执行发送。本客户端两条铁律：

  1) 总线可选：环境变量 BOUNDLESS_BUS_URL 未设置 → 单机模式：decide 直接返回
     {"available": False, "action": "fallback"}，调用方走本地 ai_auto_chat 兜底，照常独立运行。
     设置了 → 联网问询；问询失败(超时/连接失败/HTTP 错误/JSON 解析失败/决策非法)同样收敛为
     action="fallback"。
  2) fail-soft：decide() 任何情况都**不抛异常**——拿不到有效决策就 fallback，收发主路径绝不被
     总线拖垮。决策是同步时效数据，过时决策不可补投，故本客户端刻意**没有 outbox**
     (与 leadbus 的关键差别)：错过 = 本地兜底，仅此而已。

防双发(本契约头等大事，详见 CONTRACT.md §5)：**执行权归智控王**——chengjie 只返回决策，
绝不在该会话 session 上自己发送(承接用号与获客用号分池)；决策响应里不存在任何"已发送"语义。
客户端侧的配合：只有 available=True 且 action 合法的决策才交给执行，否则一律 fallback——
"总线决策"与"本地兜底"两条路径互斥；问询超时后 chengjie 即便迟到算出决策，它不持有该
session 也无从发送，天然无双发。

隐私红线：message.text 是入站私信原文，只作为本次请求载荷发往 chengjie，
本客户端**不落任何日志/事件**(自测输出也不打印原文)；观测计数由调用方另发
observability 事件(不含原文)。

依赖铁律：只用 stdlib(urllib/json/os/typing)，不 import engines/products/website，
也不 import 第三方包 —— 守住 "platform 不反向依赖"。

用法：
    from client import ReplyBusClient
    rb = ReplyBusClient()                     # bus_url 读环境变量 BOUNDLESS_BUS_URL(与 leadbus 同源)
    d = rb.decide({
        "platform": "telegram", "account": "acct_pool_007",
        "external_id": "tg:987654321", "text": "在吗？想了解一下代发",
        "msg_id": "m_10086", "session_id": "s_tg_987654321",
        "context_hint": {"lang": "zh", "funnel_stage": "new"},
    })
    if d.get("available") and d["action"] == "send":
        ...  # 在本机 session 上发 d["text"]（建议先等 d.get("delay_ms", 0) 拟人节律）
    elif d["action"] == "fallback":
        ...  # 本地 ai_auto_chat 兜底(总线可选)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BUS_ENV_VAR = "BOUNDLESS_BUS_URL"
DECIDE_PATH = "/api/replybus/decide"
STATUS_PATH = "/api/replybus/status"

# 服务端可返回的决策动作；"fallback" 不在其中——它只由客户端本地合成(总线不可达/决策非法)
ACTIONS = ("send", "draft", "silent", "handoff")


class ReplyBusClient:
    def __init__(self, bus_url: Optional[str] = None, timeout: float = 6.0,
                 auth_token: Optional[str] = None):
        # bus_url 显式参数 > 环境变量 BOUNDLESS_BUS_URL > None(单机模式)
        # timeout 缺省 6s：decide 在对话热路径上，比 leadbus(8s) 更紧，超时即本地兜底
        raw = bus_url if bus_url is not None else os.environ.get(BUS_ENV_VAR, "").strip()
        self.bus_url = raw.rstrip("/") if raw else None
        self.timeout = timeout
        # 2026-07-19 追加（与 leadbus/enable 同批修复）：chengjie 若配置了
        # web_admin.auth_token，/api/replybus/* 同样走 _api_auth（支持 Bearer），
        # 无 token 时会一直 401→client.py 收敛为 action=fallback（现象上不报错，
        # 但总线形同虚设，见 chengjie replybus_routes.py 交付说明里的已知缺口）。
        self.auth_token = (auth_token if auth_token is not None
                           else os.environ.get("BOUNDLESS_BUS_TOKEN", "").strip() or None)

    def _headers(self, base: Dict[str, str]) -> Dict[str, str]:
        if self.auth_token:
            base = dict(base)
            base["Authorization"] = f"Bearer {self.auth_token}"
        return base

    # ---- 校验（消息信封最小约束；宽松但拦住明显残缺）----
    @staticmethod
    def envelope_error(message: Any) -> Optional[str]:
        if not isinstance(message, dict):
            return f"message 必须是 dict，得到 {type(message).__name__}"
        for key in ("platform", "external_id", "text"):
            val = message.get(key)
            if not isinstance(val, str) or not val.strip():
                return f"message.{key} 必填(非空字符串)"
        hint = message.get("context_hint")
        if hint is not None and not isinstance(hint, dict):
            return "message.context_hint 必须是 dict"
        return None

    # ---- 内部 HTTP（可降级：任何失败都收敛为 dict，不抛给调用方）----
    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.bus_url:
            return {"available": False, "error": "BOUNDLESS_BUS_URL 未配置(单机模式)"}
        url = self.bus_url + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=self._headers(headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                out = json.loads(body) if body else {}
                if isinstance(out, dict):
                    out.setdefault("available", True)
                    return out
                return {"available": True, "data": out}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return {"available": False, "error": f"HTTP {e.code}", "detail": detail}
        except Exception as e:  # 连接失败/超时/JSON 解析失败 —— 一律降级
            return {"available": False, "error": str(e)[:200]}

    # ---- 契约方法（见 CONTRACT.md §2）----
    def decide(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """问承接大脑『这条入站私信怎么回』，返回决策。永不抛异常。

        返回（调用方只按 action 分派即可，见 CONTRACT.md §4/§5）：
          - 联网且决策有效：{"available":True,"action":"send|draft|silent|handoff",
                             "text"?,"media"?,"persona"?,"delay_ms"?,"reason"?}
          - 单机模式：      {"available":False,"action":"fallback","mode":"standalone"}
          - 问询失败：      {"available":False,"action":"fallback","error":...}
                            （超时/连接失败/HTTP 错误/总线返回非法决策，一律收敛）
          - 信封非法：      {"available":False,"action":"fallback","rejected":True,"error":...}
                            （调用方 bug，不发起 HTTP；兜底照走，顺带修信封）

        防双发：返回里没有也永远不会有"chengjie 已发送"语义——执行权归调用方(智控王)；
        action="fallback" 时才走本地 ai_auto_chat，两条路径互斥。
        隐私：message.text 只作请求载荷，本方法不打印/不落盘任何原文。
        """
        err = self.envelope_error(message)
        if err is not None:
            return {"available": False, "action": "fallback", "rejected": True, "error": err}
        if not self.bus_url:
            return {"available": False, "action": "fallback", "mode": "standalone"}
        resp = self._request("POST", DECIDE_PATH, {"message": message})
        if not resp.get("available"):
            resp["available"] = False
            resp["action"] = "fallback"
            return resp
        action = resp.get("action")
        if action not in ACTIONS:
            # 总线可达但决策不合法(缺 action / 未知动作)——宁可兜底，不执行可疑决策
            return {"available": False, "action": "fallback",
                    "error": f"总线返回非法决策(action={action!r})"}
        return resp

    def status(self) -> Dict[str, Any]:
        """GET /api/replybus/status —— 承接中台决策口是否就绪。单机模式返回 available=False。"""
        return self._request("GET", STATUS_PATH, None)

    def available(self) -> bool:
        """承接中台 replybus 决策口是否可达(HTTP 可达且决策管线已加载)。"""
        st = self.status()
        return bool(st.get("available")) and bool(st.get("ready", st.get("available")))


def _selftest() -> int:
    """四条路径全测：单机 fallback / 非法信封拦截 / mock chengjie 联网决策 / 连接失败收敛。"""
    import http.server
    import socketserver
    import threading

    failures: List[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    good = {
        "platform": "telegram",
        "account": "acct_pool_007",
        "external_id": "tg:987654321",
        "text": "在吗？想了解一下代发怎么合作",
        "msg_id": "m_10086",
        "session_id": "s_tg_987654321",
        "context_hint": {"lang": "zh", "funnel_stage": "new"},
    }

    print("== replybus 瘦客户端自测（client.py --selftest）==")

    print("[1/4] 单机模式：无 bus_url → decide 返回 action=fallback（本地兜底，不抛异常）")
    rb0 = ReplyBusClient(bus_url="")
    r0 = rb0.decide(dict(good))
    check("单机 decide action=fallback 且 available=False",
          r0.get("action") == "fallback" and r0.get("available") is False)
    check("单机 decide 标记 mode=standalone", r0.get("mode") == "standalone")
    check("单机 status() 降级不抛", rb0.status().get("available") is False)
    check("单机 available()=False", rb0.available() is False)

    print("[2/4] 非法信封：envelope_error 拦截（rejected，不发起 HTTP）")
    bad_cases = [
        ("缺 platform", {"external_id": "tg:1", "text": "hi"}),
        ("缺 external_id", {"platform": "telegram", "text": "hi"}),
        ("缺 text", {"platform": "telegram", "external_id": "tg:1"}),
        ("text 为空白", {"platform": "telegram", "external_id": "tg:1", "text": "   "}),
        ("context_hint 非 dict", {"platform": "telegram", "external_id": "tg:1",
                                  "text": "hi", "context_hint": "oops"}),
        ("message 非 dict", "oops"),
    ]
    for why, bad in bad_cases:
        r = rb0.decide(bad)
        check(f"拦截 {why}",
              r.get("rejected") is True and r.get("action") == "fallback"
              and isinstance(ReplyBusClient.envelope_error(bad), str))

    print("[3/4] mock chengjie：联网决策 action=send，服务端收到完整信封")
    received: List[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音（也避免入站原文出现在测试日志）
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                body = {}
            received.append(body)
            msg = body.get("message", {}) if isinstance(body, dict) else {}
            if msg.get("msg_id") == "m_bad_action":
                payload: Dict[str, Any] = {"action": "broadcast_all"}  # 非法动作：客户端必须收敛 fallback
            else:
                payload = {"action": "send", "text": "可以的，方便说下您主要发什么品类吗？",
                           "persona": "sales_amy", "delay_ms": 1200, "reason": "new_lead_greeting"}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ready": true}')

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{port}"
            rb1 = ReplyBusClient(bus_url=base)
            check("available() 探针为真", rb1.available() is True)
            r1 = rb1.decide(dict(good))
            check("联网 decide available=True 且 action=send",
                  r1.get("available") is True and r1.get("action") == "send")
            check("决策带回复文本/人设/延迟",
                  bool(r1.get("text")) and r1.get("persona") == "sales_amy"
                  and r1.get("delay_ms") == 1200)
            check("决策不含'已发送'语义字段（执行权归智控王）",
                  "sent" not in r1 and "delivered" not in r1)
            env = received[-1].get("message", {}) if received else {}
            check("mock 服务端收到信封（platform/account/external_id/text/msg_id 齐全）",
                  env.get("platform") == "telegram" and env.get("account") == "acct_pool_007"
                  and env.get("external_id") == "tg:987654321" and env.get("text") == good["text"]
                  and env.get("msg_id") == "m_10086")

            n_before = len(received)
            r_rej = rb1.decide({"platform": "telegram"})
            check("联网下非法信封同样 rejected 且不发 HTTP",
                  r_rej.get("rejected") is True and len(received) == n_before)

            r_bad = rb1.decide(dict(good, msg_id="m_bad_action"))
            check("总线返回非法动作 → 收敛为 fallback（不执行可疑决策）",
                  r_bad.get("action") == "fallback" and r_bad.get("available") is False)
        finally:
            srv.shutdown()

    print("[4/4] 连接失败：总线端口已关 → decide 收敛为 fallback（不抛异常）")
    rb2 = ReplyBusClient(bus_url=f"http://127.0.0.1:{port}", timeout=2.0)
    r2 = rb2.decide(dict(good))
    check("连接失败 decide action=fallback 且带 error",
          r2.get("action") == "fallback" and r2.get("available") is False and bool(r2.get("error")))

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


if __name__ == "__main__":
    import sys
    # Windows 下 stdout 默认本地代码页(cp936 等)打中文会乱码，统一 UTF-8（与 enable/leadbus 一致）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if sys.argv[1:2] == ["--selftest"]:
        sys.exit(_selftest())
    # 缺省：打印当前配置（不打印任何消息原文）
    rb = ReplyBusClient()
    print(f"[replybus.client] bus_url={rb.bus_url or '(单机模式)'}")
    print(f"  available()={rb.available()}  (承接中台未在线/单机属正常，客户端已降级不抛错)")
    sys.exit(0)
