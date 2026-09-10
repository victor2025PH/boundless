"""TikTok × 智聊（chengjie）脑手分离 —— huoke 侧（TK-3 ①-A，2026-09-10）。

默认 ``reply_engine: local``：本模块零调用，``check_inbox`` / ``_handle_inbox_message`` 行为与改前一致。
``reply_engine: chengjie``：huoke 只做手——把读到的私信 POST 到智聊 ``/api/tiktok/huoke/dm``，
再轮询 ``/handback`` 认领 → ``send_dm`` → ``/handback/ack``。不跑 ChatBrain / 协调器 CRM / 本地 AutoReply。

``auto_reply=True`` 与 ``chengjie`` **互斥**：chengjie 模式下忽略 auto_reply（不跑本地大脑），打一条警告。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

ENGINE_LOCAL = "local"
ENGINE_CHENGJIE = "chengjie"
DM_PATH = "/api/tiktok/huoke/dm"
DEVICES_PATH = "/api/tiktok/huoke/devices"
HANDBACK_PATH = "/api/tiktok/huoke/handback"
HANDBACK_ACK_PATH = "/api/tiktok/huoke/handback/ack"


def _load_yaml() -> Dict[str, Any]:
    try:
        from src.host.device_registry import config_file
        path = config_file("apps/tiktok.yaml")
        if not path.is_file():
            return {}
        import yaml
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        log.debug("[tiktok-chengjie] 读 tiktok.yaml 失败", exc_info=True)
        return {}


def _is_normalized(cfg: Any) -> bool:
    return isinstance(cfg, dict) and "device_account_map" in cfg and "endpoint" in cfg


def reply_cfg(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """归一化 ``reply_engine`` 块。缺省 / 坏值 → local（零 diff）。已归一化的 dict 原样返回。"""
    if _is_normalized(raw):
        return raw  # type: ignore[return-value]
    src = raw if isinstance(raw, dict) else _load_yaml()
    engine = str(src.get("reply_engine") or ENGINE_LOCAL).strip().lower()
    if engine not in (ENGINE_LOCAL, ENGINE_CHENGJIE):
        engine = ENGINE_LOCAL
    blk = src.get("chengjie") if isinstance(src.get("chengjie"), dict) else {}
    endpoint = str(blk.get("endpoint") or os.environ.get("CHENGJIE_TIKTOK_ENDPOINT") or "").rstrip("/")
    token = str(blk.get("token") or os.environ.get("CHENGJIE_API_TOKEN") or "")
    amap = blk.get("device_account_map") if isinstance(blk.get("device_account_map"), dict) else {}
    return {
        "reply_engine": engine,
        "endpoint": endpoint,
        "token": token,
        "username": str(blk.get("username") or ""),
        "account_id": str(blk.get("account_id") or ""),
        "timezone": str(blk.get("timezone") or ""),
        "device_account_map": {str(k): v for k, v in amap.items()},
        "timeout_sec": max(2.0, float(blk.get("timeout_sec") or 8)),
        "handback_limit": max(1, min(20, int(blk.get("handback_limit") or 5))),
    }


def is_chengjie(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return reply_cfg(cfg if isinstance(cfg, dict) and "reply_engine" in cfg else cfg)["reply_engine"] == ENGINE_CHENGJIE


def validate_auto_reply(auto_reply: bool, cfg: Optional[Dict[str, Any]] = None) -> str:
    """互斥校验：chengjie + auto_reply → 警告文案（调用方应忽略 auto_reply，不失败整次巡检）。"""
    if auto_reply and is_chengjie(cfg):
        return "reply_engine=chengjie 与 auto_reply=True 互斥：忽略本地大脑，只把私信交给智聊"
    return ""


def account_for_device(device_id: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    c = reply_cfg(cfg if isinstance(cfg, dict) and "reply_engine" in cfg else cfg)
    raw = c["device_account_map"].get(str(device_id) or "")
    if isinstance(raw, dict):
        aid = str(raw.get("account_id") or raw.get("username") or c["account_id"] or device_id)
        return {
            "account_id": aid,
            "username": str(raw.get("username") or c["username"] or ""),
            "timezone": str(raw.get("timezone") or c["timezone"] or ""),
        }
    if isinstance(raw, str) and raw.strip():
        return {"account_id": raw.strip(), "username": c["username"], "timezone": c["timezone"]}
    return {
        "account_id": c["account_id"] or str(device_id or "huoke"),
        "username": c["username"],
        "timezone": c["timezone"],
    }


def messages_from_conversation(conv_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``_read_conversation`` 产物 → 智聊 ``/dm`` 的 messages 列表（含 huoke 自己发的 out 回显）。"""
    peer = str((conv_data or {}).get("contact") or "").strip().lstrip("@")
    out: List[Dict[str, Any]] = []
    for i, m in enumerate((conv_data or {}).get("messages") or []):
        if not isinstance(m, dict):
            continue
        text = str(m.get("text") or "").strip()
        direction = "out" if str(m.get("direction") or "").lower() in ("outbound", "out", "sent", "me") else "in"
        if not text and not m.get("media_type"):
            continue
        mid = str(m.get("msg_id") or f"{peer}:{direction}:{i}:{text[:40]}")
        item: Dict[str, Any] = {
            "msg_id": mid,
            "peer_username": peer,
            "peer_name": peer,
            "text": text,
            "direction": direction,
        }
        if m.get("media_type"):
            item["media_type"] = str(m["media_type"])
        rel = {}
        if m.get("is_mutual") is not None:
            rel["is_mutual"] = bool(m["is_mutual"])
        if m.get("is_follower") is not None:
            rel["is_follower"] = bool(m["is_follower"])
        if rel:
            item["relation"] = rel
        if m.get("thread_type"):
            item["thread_type"] = str(m["thread_type"])
        out.append(item)
    return out


def _http(cfg: Dict[str, Any], method: str, path: str, *, query: str = "", body: Any = None,
          http: Optional[Callable] = None) -> Tuple[int, Any]:
    url = f"{cfg['endpoint']}{path}" + (f"?{query}" if query else "")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    if http is not None:
        return http(method, url, headers, body)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout_sec") or 8)) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return int(resp.status), json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {"error": str(e)}
        return int(e.code), payload
    except Exception as e:
        log.warning("[tiktok-chengjie] HTTP %s %s 失败: %s", method, path, e)
        return 0, {"error": str(e)}


def bind_device(device_id: str, *, cfg: Optional[Dict[str, Any]] = None, http: Optional[Callable] = None
                ) -> Tuple[int, Dict[str, Any]]:
    c = reply_cfg(cfg if isinstance(cfg, dict) and "reply_engine" in cfg else cfg)
    acc = account_for_device(device_id, c)
    payload = {"device_id": device_id, "account_id": acc["account_id"],
               "username": acc["username"], "timezone": acc["timezone"]}
    status, resp = _http(c, "POST", DEVICES_PATH, body=payload, http=http)
    return status, resp if isinstance(resp, dict) else {"ok": False}


def post_dm(conv_data: Dict[str, Any], *, device_id: str, cfg: Optional[Dict[str, Any]] = None,
            http: Optional[Callable] = None) -> Tuple[int, Dict[str, Any]]:
    c = reply_cfg(cfg if isinstance(cfg, dict) and "reply_engine" in cfg else cfg)
    acc = account_for_device(device_id, c)
    msgs = messages_from_conversation(conv_data)
    if not msgs:
        return 200, {"ok": True, "accepted": 0, "echo": 0, "skipped": "empty"}
    payload = {"device_id": device_id, "account_id": acc["account_id"],
               "username": acc["username"], "timezone": acc["timezone"], "messages": msgs}
    status, resp = _http(c, "POST", DM_PATH, body=payload, http=http)
    return status, resp if isinstance(resp, dict) else {"ok": False}


def handle_inbox_chengjie(conv_data: Dict[str, Any], *, device_id: str,
                          cfg: Optional[Dict[str, Any]] = None, http: Optional[Callable] = None
                          ) -> Dict[str, Any]:
    """替代 ``_handle_inbox_message``：转发私信，不本地回复。"""
    status, resp = post_dm(conv_data, device_id=device_id, cfg=cfg, http=http)
    return {
        "action": "forwarded" if status == 200 and resp.get("ok") else "forward_failed",
        "http_status": status,
        "accepted": int(resp.get("accepted") or 0),
        "echo": int(resp.get("echo") or 0),
        "drafted": int(resp.get("drafted") or 0),
        "error": resp.get("error") or "",
    }


def drain_handback(*, device_id: str, send_dm: Callable[[str, str], bool],
                   cfg: Optional[Dict[str, Any]] = None, http: Optional[Callable] = None
                   ) -> Dict[str, Any]:
    """认领回传队列 → 真机 ``send_dm`` → 回执。``send_dm(recipient, text) -> bool``。"""
    c = reply_cfg(cfg if isinstance(cfg, dict) and "reply_engine" in cfg else cfg)
    if not is_chengjie(c) or not c["endpoint"]:
        return {"ok": True, "skipped": "not_chengjie", "claimed": 0, "sent": 0, "failed": 0}
    acc = account_for_device(device_id, c)
    from urllib.parse import quote
    q = f"device_id={quote(device_id)}&account_id={quote(acc['account_id'])}&limit={c['handback_limit']}"
    status, resp = _http(c, "GET", HANDBACK_PATH, query=q, http=http)
    items = (resp or {}).get("items") if isinstance(resp, dict) else None
    if status != 200 or not isinstance(items, list):
        return {"ok": False, "error": (resp or {}).get("error") or f"http_{status}", "claimed": 0, "sent": 0, "failed": 0}
    sent = failed = 0
    details: List[Dict[str, Any]] = []
    for it in items:
        item_id = it.get("id")
        recipient = str(it.get("username") or it.get("user_id") or "").lstrip("@")
        text = str(it.get("text") or "")
        err = ""
        ok = False
        if not recipient or not text:
            err = "missing_recipient_or_text"
        else:
            try:
                ok = bool(send_dm(recipient, text))
                if not ok:
                    err = "send_failed"
            except Exception as e:
                err = f"exception:{type(e).__name__}"
                log.warning("[tiktok-chengjie] send_dm 异常 item=%s: %s", item_id, e)
        ack_body = {"item_id": item_id, "ok": ok, "device_id": device_id, "error": err}
        _http(c, "POST", HANDBACK_ACK_PATH, body=ack_body, http=http)
        if ok:
            sent += 1
        else:
            failed += 1
        details.append({"item_id": item_id, "ok": ok, "error": err, "recipient": recipient})
    return {"ok": True, "claimed": len(items), "sent": sent, "failed": failed, "items": details}


def dispatch_inbox_reply(*, auto_reply: bool, classifier: Any, conv_data: Dict[str, Any],
                         device_id: str, local_handler: Callable, cfg: Optional[Dict[str, Any]] = None,
                         http: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
    """``check_inbox`` 分叉：chengjie → 转发；否则沿用 ``local_handler``（``_handle_inbox_message``）。"""
    c = reply_cfg(cfg if isinstance(cfg, dict) and "reply_engine" in cfg else cfg)
    warn = validate_auto_reply(auto_reply, c)
    if warn:
        log.warning("[tiktok-chengjie] %s", warn)
    if is_chengjie(c):
        return handle_inbox_chengjie(conv_data, device_id=device_id, cfg=c, http=http)
    if auto_reply and classifier is not None:
        return local_handler()
    return None
