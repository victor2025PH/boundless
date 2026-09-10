# -*- coding: utf-8 -*-
"""微信客服（企业微信）回调路由（实施97 线 A）：GET 校验 + POST 事件 → 唤醒 worker 带 token 拉取。

回调**不是必需**：worker 靠 ``sync_msg`` 轮询即可跑通（桌面端无公网 IP 的默认形态）。配了回调
（``wechat_kf.callback.{token, encoding_aes_key}``，并在企微后台把「接收消息服务器 URL」指到本路由）
则从「几秒一次的轮询 + 严格频控」变成「事件秒级触发 + 10 分钟 token 免频控」。将来官网中继
（bd2026.cc）只是把同一份解密后的 ``{Token, OpenKfId}`` 经设备令牌转投到这里的 :func:`dispatch_event`
——入口再加一个，处理函数不变。

企微契约：GET 带 ``msg_signature/timestamp/nonce/echostr``，验签后**原样回明文 echostr**；POST 5 秒内
回 ``success``（或空串）即可，业务处理异步。回调 XML 内层字段：``ToUserName / CreateTime / MsgType=event /
Event=kf_msg_or_event / Token / OpenKfId``。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from src.integrations.wechat_kf import PLATFORM, KfCallbackCrypto

logger = logging.getLogger(__name__)

DEFAULT_CALLBACK_PATH = "/wechat/kf/callback"


def callback_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        blk = ((config or {}).get("wechat_kf") or {}).get("callback")
        return dict(blk) if isinstance(blk, dict) else {}
    except Exception:
        return {}


def callback_path(config: Optional[Dict[str, Any]]) -> str:
    p = str(callback_cfg(config).get("path") or DEFAULT_CALLBACK_PATH).strip() or DEFAULT_CALLBACK_PATH
    return p if p.startswith("/") else "/" + p


def find_worker(open_kfid: str) -> Any:
    """按 ``OpenKfId`` 找运行中的微信客服 worker（account_id==kfid 或 worker.open_kfid==kfid）。"""
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()
        if orch is None:
            return None
        managed = getattr(orch, "_managed", {}) or {}
        for key, m in managed.items():
            if not str(key).startswith(f"{PLATFORM}:"):
                continue
            w = getattr(m, "worker", None)
            if w is None or getattr(m, "state", "") != "running":
                continue
            if str(getattr(w, "open_kfid", "") or "") == open_kfid \
                    or str(getattr(w, "account_id", "") or "") == open_kfid:
                return w
    except Exception:
        logger.debug("[wechat_kf] 查找 worker 失败", exc_info=True)
    return None


def dispatch_event(fields: Dict[str, str]) -> bool:
    """解密后的回调字段 → 唤醒对应 worker（带 token）。返回是否找到了 worker。"""
    if str(fields.get("Event") or "") != "kf_msg_or_event":
        return False
    kfid = str(fields.get("OpenKfId") or "")
    token = str(fields.get("Token") or "")
    w = find_worker(kfid) if kfid else None
    if w is None:
        logger.info("[wechat_kf] 回调到达但无运行中 worker（open_kfid=%s）——等轮询兜底", kfid)
        return False
    try:
        w.kick(token)
    except Exception:
        logger.debug("[wechat_kf] kick 失败", exc_info=True)
        return False
    return True


def register_wechat_kf_routes(app: FastAPI, config_manager: Any) -> Optional[str]:
    """挂载回调路由。通道未启用 / 未配 token+aes_key → 不注册（轮询形态），返回 None。"""
    cfg = (getattr(config_manager, "config", None) or {})
    blk = cfg.get("wechat_kf") or {}
    if not blk.get("enabled"):
        return None
    cb = callback_cfg(cfg)
    token = str(cb.get("token") or "").strip()
    aes = str(cb.get("encoding_aes_key") or "").strip()
    if not (token and aes):
        logger.info("[wechat_kf] 未配回调 token/encoding_aes_key → 仅轮询形态")
        return None
    try:
        crypto = KfCallbackCrypto(token, aes, str(blk.get("corpid") or "").strip())
    except Exception as exc:  # noqa: BLE001
        logger.error("[wechat_kf] 回调加解密参数无效，路由未注册: %s", exc)
        return None
    path = callback_path(cfg)
    app.state.wechat_kf_callback_path = path

    async def wechat_kf_verify(request: Request) -> Response:
        q = request.query_params
        echo = crypto.verify_url(q.get("msg_signature", ""), q.get("timestamp", ""),
                                 q.get("nonce", ""), q.get("echostr", ""))
        if echo is None:
            return Response(status_code=403, content=b"invalid signature")
        return PlainTextResponse(echo)

    async def wechat_kf_event(request: Request) -> Response:
        try:
            from src.integrations.official_webhook_stats import record_error, record_event
        except Exception:  # pragma: no cover
            record_error = record_event = lambda *a, **k: None  # type: ignore
        q = request.query_params
        body = await request.body()
        fields = crypto.decrypt_callback(body, q.get("msg_signature", ""),
                                         q.get("timestamp", ""), q.get("nonce", ""))
        if fields is None:
            record_error(PLATFORM, "bad_signature")
            return Response(status_code=403, content=b"invalid signature")
        record_event(PLATFORM)
        dispatch_event(fields)
        return PlainTextResponse("success")

    app.add_api_route(path, wechat_kf_verify, methods=["GET"], name="wechat_kf_verify")
    app.add_api_route(path, wechat_kf_event, methods=["POST"], name="wechat_kf_event")
    logger.info("微信客服回调已注册: GET/POST %s", path)
    return path


def pick_servicer(servicers: Any, preferred: str = "") -> str:
    """从 ``kf/servicer/list`` 结果挑接待人：显式指定优先；否则第一个「接待中」(status=0)；再否则第一个。纯函数。"""
    if str(preferred or "").strip():
        return str(preferred).strip()
    rows = [s for s in (servicers or []) if isinstance(s, dict) and str(s.get("userid") or "").strip()]
    for s in rows:
        if int(s.get("status") or 0) == 0:
            return str(s["userid"]).strip()
    return str(rows[0]["userid"]).strip() if rows else ""


#: 企微 ``service_state`` 取值（官方）：0 未处理 / 1 智能助手接待 / 2 待接入池排队 / 3 人工接待 / 4 已结束或未开始
KF_STATE_KEYS = {0: "untouched", 1: "bot", 2: "queued", 3: "human", 4: "closed"}
_SESSION_SNAP_TTL_SEC = 30.0
_session_snap_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}


def _unwrap_api(r: Any) -> Dict[str, Any]:
    """``WeChatKfClient.api()`` 返回 ``{ok, errcode, errmsg, data:{…}}``：业务字段在 data 里。"""
    r = r if isinstance(r, dict) else {}
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    out = dict(data)
    out["errcode"] = int(r.get("errcode") if r.get("errcode") is not None else data.get("errcode") or 0)
    out["errmsg"] = str(r.get("errmsg") or data.get("errmsg") or "")
    return out


async def kf_session_snapshot(account_id: str, chat_key: str, *, now: Optional[float] = None,
                              ttl_sec: float = _SESSION_SNAP_TTL_SEC) -> Optional[Dict[str, Any]]:
    """当前会话在企微侧的接待状态（工作台会话头状态条 / send-caps.kf_session）。

    30 秒进程内缓存：坐席每发一条前端都会重拉 send-caps，不能每次都打企微接口。无 worker / 出错 → None。
    转人工/结束会话成功后调用方应 :func:`invalidate_kf_session_snapshot`。
    """
    import time as _time
    key = (str(account_id or ""), str(chat_key or ""))
    t = float(now if now is not None else _time.time())
    hit = _session_snap_cache.get(key)
    if hit and t - hit[0] <= ttl_sec:
        return dict(hit[1])
    w = find_worker(key[0])
    if w is None:
        return None
    try:
        r = _unwrap_api(await w.session_state(key[1]))
    except Exception:
        logger.debug("[wechat_kf] 读会话状态失败", exc_info=True)
        return None
    if r["errcode"] != 0:
        return None
    try:
        state = int(r.get("service_state") if r.get("service_state") is not None else -1)
    except Exception:
        state = -1
    snap = {"state": state, "state_key": KF_STATE_KEYS.get(state, "unknown"),
            "servicer_userid": str(r.get("servicer_userid") or ""), "ts": t}
    _session_snap_cache[key] = (t, snap)
    if len(_session_snap_cache) > 2000:
        for k in sorted(_session_snap_cache, key=lambda k: _session_snap_cache[k][0])[:1000]:
            _session_snap_cache.pop(k, None)
    return dict(snap)


def invalidate_kf_session_snapshot(account_id: str, chat_key: str) -> None:
    _session_snap_cache.pop((str(account_id or ""), str(chat_key or "")), None)


def register_wechat_kf_session_routes(app: FastAPI, api_auth: Any) -> None:
    """会话状态动作（实施97 线 A 第三轮）：坐席在工作台把微信客服会话**转企微人工**（客户后续由企微客服后台的
    接待人员应答）、结束会话、查当前状态。恒注册；该账号 worker 未运行 → 409。

    转人工成功后本端会话同时打 ``takeover`` 标（AI 停手）——否则企微坐席在答、我们的 AI 也在答，客户看到两个人。
    """
    from fastapi import Depends, HTTPException

    def _worker_or_409(account_id: str) -> Any:
        w = find_worker(str(account_id or ""))
        if w is None:
            raise HTTPException(409, "wechat_kf_worker_not_running")
        return w

    _unwrap = _unwrap_api

    def _mark_takeover(request: Request, account_id: str, chat_key: str) -> None:
        try:
            from src.inbox.normalizer import conv_id
            from src.inbox.takeover_rearm import record_agent_takeover
            store = getattr(request.app.state, "inbox_store", None)
            if store is not None:
                record_agent_takeover(store, conv_id(PLATFORM, account_id, chat_key))
        except Exception:
            logger.debug("[wechat_kf] 转人工后打接管标失败（已忽略）", exc_info=True)

    @app.get("/api/unified-inbox/kf/session-state")
    async def api_kf_session_state(request: Request, account_id: str = "", chat_key: str = "",
                                   _=Depends(api_auth)):
        if not account_id or not chat_key:
            raise HTTPException(400, "account_id / chat_key required")
        w = _worker_or_409(account_id)
        r = _unwrap(await w.session_state(chat_key))
        return {"ok": r["errcode"] == 0, "service_state": r.get("service_state"),
                "servicer_userid": str(r.get("servicer_userid") or ""),
                "errcode": r["errcode"], "errmsg": r["errmsg"]}

    @app.post("/api/unified-inbox/kf/transfer")
    async def api_kf_transfer(request: Request, _=Depends(api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id = str((body or {}).get("account_id") or "")
        chat_key = str((body or {}).get("chat_key") or "")
        if not account_id or not chat_key:
            raise HTTPException(400, "account_id / chat_key required")
        w = _worker_or_409(account_id)
        servicer = str((body or {}).get("servicer_userid") or "").strip()
        if not servicer:
            try:
                lst = _unwrap(await w._client.list_servicers(w.open_kfid))
                servicer = pick_servicer(lst.get("servicer_list"))
            except Exception:
                logger.debug("[wechat_kf] 拉接待人员列表失败", exc_info=True)
        if not servicer:
            raise HTTPException(409, "no_servicer_available")
        r = _unwrap(await w.transfer_to_human(chat_key, servicer))
        ok = r["errcode"] == 0
        if ok:
            _mark_takeover(request, account_id, chat_key)
            invalidate_kf_session_snapshot(account_id, chat_key)
        return {"ok": ok, "servicer_userid": servicer, "msg_code": str(r.get("msg_code") or ""),
                "errcode": r["errcode"], "errmsg": r["errmsg"]}

    @app.post("/api/unified-inbox/kf/close")
    async def api_kf_close(request: Request, _=Depends(api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id = str((body or {}).get("account_id") or "")
        chat_key = str((body or {}).get("chat_key") or "")
        if not account_id or not chat_key:
            raise HTTPException(400, "account_id / chat_key required")
        w = _worker_or_409(account_id)
        r = _unwrap(await w.close_session(chat_key))
        if r["errcode"] == 0:
            invalidate_kf_session_snapshot(account_id, chat_key)
        return {"ok": r["errcode"] == 0, "errcode": r["errcode"], "errmsg": r["errmsg"]}


__all__ = ["register_wechat_kf_routes", "register_wechat_kf_session_routes", "dispatch_event", "find_worker",
           "callback_path", "callback_cfg", "pick_servicer", "kf_session_snapshot", "invalidate_kf_session_snapshot",
           "KF_STATE_KEYS", "DEFAULT_CALLBACK_PATH"]
