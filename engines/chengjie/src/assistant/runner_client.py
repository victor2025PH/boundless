# -*- coding: utf-8 -*-
"""小智后端侧「Windows 操控 runner」HTTP 客户端（实施91 P1-0，2026-08-30）。

职责：把小智的侦察/动作请求发给受控机上的 runner 服务（pc_runner.py）。

设计（docs/实施91 §3.2/§4；改前先读）：
- **目标只能来自白名单**：base_url 由 runner_pairing.get_machine 换出（LLM 只
  提名 machine_id），token 由 config 取——**client 绝不接受调用方传入的裸 URL**。
- **token 不入日志/不回前端**：只在请求头出现；resolve_target 返回它仅供本
  模块内部发请求，路由层拿到响应里绝不含 token。
- **失败诚实回落**：runner 离线/超时/坏响应一律返回 ``{ok:False,error:...}``，
  绝不崩、绝不假成功（受控机是外部进程，不可信其一定在）。
- 低依赖：HTTP 用标准库 urllib（不引 requests），传输壳薄，纯逻辑可测。

门禁 ``tests/test_runner_client.py``（纯函数 + mock 传输，非 Windows 也跑）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from src.assistant import runner_pairing as rp

logger = logging.getLogger(__name__)

HEALTH_TIMEOUT_SEC = 5.0
INSPECT_TIMEOUT_SEC = 15.0


def _pc_cfg(config: Any) -> Dict[str, Any]:
    try:
        a = (config or {}).get("assistant") or {}
        pc = a.get("pc_runner") or {}
        return pc if isinstance(pc, dict) else {}
    except Exception:
        return {}


def resolve_target(machine_id: str,
                   config: Any) -> Tuple[Optional[str], str, str]:
    """(base_url, token, reason)。base_url=None 时 reason 说明为何拿不到。

    token 优先取该机条目的 per-machine token，回落全局 ``pc_runner.token``。
    """
    m = rp.get_machine(machine_id)
    if not m:
        # 不在白名单 / 被踢下线 —— 区分二者便于前端给对话术
        return None, "", ("revoked" if rp.is_revoked(machine_id)
                          else "not_whitelisted")
    pc = _pc_cfg(config)
    token = ""
    for e in (pc.get("machines") or []):
        if isinstance(e, dict) and str(e.get("id")) == machine_id:
            token = str(e.get("token") or "")
            break
    if not token:
        token = str(pc.get("token") or "")
    if not token:
        return None, "", "no_token"
    return m["base_url"], token, "ok"


def build_headers(token: str, actor: str = "") -> Dict[str, str]:
    h = {"Authorization": f"Bearer {token}",
         "Content-Type": "application/json"}
    if actor:
        # 只带消毒后的短标识（谁在操作），绝不带原始查询/敏感串
        h["X-PC-Actor"] = str(actor)[:60]
    return h


def parse_response(status: int, body: Any) -> Dict[str, Any]:
    """归一化 runner 响应。非 dict / 非 200-4xx 结构一律判失败。"""
    if not isinstance(body, dict):
        return {"ok": False, "error": "bad_response",
                "detail": f"status={status}"}
    if "ok" not in body:
        return {"ok": False, "error": "bad_response",
                "detail": f"status={status}"}
    return body


# ── HTTP 传输（薄壳，可 monkeypatch _http_json 做端到端 mock）──────────────
def _http_json(method: str, url: str, headers: Dict[str, str],
               payload: Optional[Dict[str, Any]],
               timeout: float) -> Tuple[int, Any]:  # pragma: no cover - IO
    import urllib.request

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, None


def probe(machine_id: str, config: Any) -> Dict[str, Any]:
    """探活受控机 runner（GET /health）。离线=诚实 offline，不崩。"""
    base, token, reason = resolve_target(machine_id, config)
    if not base:
        return {"ok": False, "error": reason}
    try:
        status, body = _http_json("GET", base + "/health",
                                  build_headers(token), None,
                                  HEALTH_TIMEOUT_SEC)
        return parse_response(status, body)
    except Exception as ex:
        logger.debug("runner probe 失败 %s: %s", machine_id, ex)
        return {"ok": False, "error": "offline", "detail": str(ex)[:120]}


def inspect(machine_id: str, tool: str, args: Optional[Dict[str, Any]],
            config: Any, actor: str = "") -> Dict[str, Any]:
    """向受控机 runner 发只读侦察/动作请求（POST /call）。

    离线/超时/坏响应一律 {ok:False,error}，绝不崩绝不假成功。
    """
    base, token, reason = resolve_target(machine_id, config)
    if not base:
        return {"ok": False, "error": reason}
    payload = {"tool": str(tool or ""), "args": args or {}}
    try:
        status, body = _http_json("POST", base + "/call",
                                  build_headers(token, actor), payload,
                                  INSPECT_TIMEOUT_SEC)
        return parse_response(status, body)
    except Exception as ex:
        logger.debug("runner inspect 失败 %s/%s: %s", machine_id, tool, ex)
        return {"ok": False, "error": "offline", "detail": str(ex)[:120]}
