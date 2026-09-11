"""统一收件箱——消息/媒体/语音发送写路径路由域（巨石拆分 slice 30）。

把 ``register_unified_inbox_routes`` 巨型闭包中的核心**写路径**子域整体外移为
``register_send_routes(app, *, api_auth, page_auth)``，由主 register 在**原位置**调用：

- ``unified-inbox/send``：文本发送（含发送前 outbound 翻译闭环 + 漏斗埋点 + 坐席首响归属）
- ``unified-inbox/send-media``：multipart 媒体发送（protocol 多开账号）
- ``unified-inbox/send-voice``：文本→TTS（可声音克隆）→OGG/Opus 语音消息发送
- ``unified-inbox/send-caps``：媒体/语音直发能力探测（供前端按钮置灰）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 30 端点契约断言）。

闭包级依赖经子注册显式入参：``page_auth``（send/media/voice）+ ``api_auth``（caps）。
依赖全部朝下：services.(_inbox_store/_get_translation_service/_resolve_conv_language)、
auth._session_agent、context._record_copilot_adopt_from_send、channel_adapters、
aggregate._INBOX_ADAPTERS；翻译/编排器/媒体落盘/TTS/转码均为 handler 内或顶部 import。
send 内的 ``_mark_send`` 为请求级嵌套闭包（坐席首响归属打点），随 send handler 保留。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

from src.ai.translation_service import normalize_lang
from src.inbox.channel_adapters import ChannelSendError, send_via_adapters
from src.utils.agent_char_usage import check_request_quota, record_request_chars
from src.inbox.normalizer import conv_id as _conv_id
from src.inbox.outbound_translate import contains_cjk, lang_is_cjk
from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
from src.web.routes.unified_inbox_auth import _session_agent
from src.web.routes.unified_inbox_context import _record_copilot_adopt_from_send
from src.web.routes.unified_inbox_services import (
    _get_translation_service,
    _inbox_store,
    _resolve_conv_engine,
    _resolve_conv_language,
)
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _perm_ok(request: Request, perm: str) -> bool:
    """按登录坐席判能力权限（P2 管理面改造：perms_json 按人覆写，master 恒 True）。

    懒 import：``resolve_user_perm`` 由并行批次在 web_user_store 落地，模块未就绪
    （ImportError）/ user_store 未暴露 / 未登录（token 链）/ 任何异常 → **一律放行**
    （fail-open：权限守卫绝不能因装配时序把发送主链打挂）。
    """
    try:
        from src.utils.web_user_store import resolve_user_perm
        us = getattr(request.app.state, "user_store", None)
        sess = request.session
        uname = str(sess.get("username") or "")
        role = str(sess.get("role") or "")
        if us is None or not uname:
            return True
        return resolve_user_perm(us, uname, role, perm)
    except Exception:
        return True


# ── C3（#148 B件排查实锤，2026-09-02）：guard=force 放行日志带操作来源 ──────────
# 旧日志只有 ``[send] guard=force kind=dup conv=…``，分不清是**坐席在守卫弹窗点了
# 「强发」**还是**失败气泡「重发」把上一轮的 force 标志顺手带了过来**（链路自带），
# 定性要多绕一轮用户确认。这里把 who/how 一次写全：
#   agent=坐席 id  sess=session 指纹（同浏览器会话稳定、不泄露 cookie）
#   entry=ui（浏览器 session）| api（Bearer 令牌：脚本/桌面壳/duty_reply）| unknown
#   src=前端 body.force_src：confirm（人点了确认）| retry_carry（重发携带旧标志）
#       | duty_reply（值守 CLI 固定带 force_lang）| -（老前端/未声明）
#   ip / cmid（client_msg_id 前 16 位，对得上前端超时对账与 send_dedup 日志）
_FORCE_SRC_ALLOWED = ("confirm", "retry_carry", "duty_reply", "api", "batch_confirm")


def _session_fingerprint(request: Request) -> str:
    """session cookie 的 8 位摘要——同一浏览器会话稳定、跨会话不同、不含明文。"""
    try:
        raw = str(request.cookies.get("session") or "")
        if not raw:
            return "-"
        import hashlib
        return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:8]
    except Exception:
        return "-"


def _request_entry(request: Request) -> str:
    """请求入口归类：ui（带登录 session）/ api（纯 Bearer）/ unknown。"""
    try:
        if "session" in request.scope:
            sess = request.session
            if sess and (sess.get("user_id") or sess.get("username")):
                return "ui"
    except Exception:
        pass
    try:
        if str(request.headers.get("Authorization") or "").startswith("Bearer "):
            return "api"
    except Exception:
        pass
    return "unknown"


def force_guard_log_fields(request: Request, body: Dict[str, Any]) -> Dict[str, str]:
    """组 guard=force 日志字段（纯观测，任何异常回退占位符，绝不影响发送）。"""
    out = {"agent": "-", "sess": "-", "entry": "unknown", "src": "-",
           "ip": "-", "cmid": "-"}
    try:
        out["agent"] = str(_session_agent(request).get("agent_id") or "-")
    except Exception:
        pass
    out["sess"] = _session_fingerprint(request)
    out["entry"] = _request_entry(request)
    try:
        src = str((body or {}).get("force_src") or "").strip().lower()[:24]
        out["src"] = src if src in _FORCE_SRC_ALLOWED else (
            f"other:{src}" if src else "-")
    except Exception:
        pass
    try:
        out["ip"] = str(request.client.host or "-") if request.client else "-"
    except Exception:
        pass
    try:
        out["cmid"] = str((body or {}).get("client_msg_id") or "-")[:16] or "-"
    except Exception:
        pass
    return out


# ── 语音/媒体投递错误 → 人话（2026-08-20 内测工单 #3：语音发送失败给用户看的是
# 裸英文 `err=ex`——VOICE_MESSAGES_FORBIDDEN 这类「对端隐私限制」被当成系统故障
# 报障。词表按 dead_peer_registry 同款「大写+下划线无关」匹配；顺序=优先级。
# 只译最终展示文案，原始异常仍进日志（定位不丢）。──────────────────────────
_SEND_ERR_HUMAN_KEYS = (
    ("err.inbox.voice_peer_privacy",
     ("VOICE_MESSAGES_FORBIDDEN", "VOICEMESSAGESFORBIDDEN")),
    ("err.inbox.send_peer_blocked",
     ("USER_IS_BLOCKED", "USERISBLOCKED", "YOU_BLOCKED_USER", "YOUBLOCKEDUSER")),
    ("err.inbox.send_flood",
     ("PEER_FLOOD", "PEERFLOOD", "FLOOD_WAIT", "FLOODWAIT", "SLOWMODE_WAIT")),
    ("err.inbox.send_write_forbidden",
     ("CHAT_WRITE_FORBIDDEN", "CHATWRITEFORBIDDEN",
      "CHAT_SEND_MEDIA_FORBIDDEN", "CHAT_SEND_PLAIN_FORBIDDEN")),
    ("err.inbox.send_peer_deactivated",
     ("INPUT_USER_DEACTIVATED", "USER_DEACTIVATED", "USERDEACTIVATED")),
)


def humanize_send_err_key(ex: Any) -> str:
    """投递异常/错误文本 → 人话 i18n 键；识别不出返回 ""（调用方走旧通用文案）。"""
    name = type(ex).__name__ if not isinstance(ex, str) else ""
    blob = (name + " " + str(ex or "")).upper().replace(" ", "_")
    for key, markers in _SEND_ERR_HUMAN_KEYS:
        for m in markers:
            if m in blob:
                return key
    return ""


def _deny_capability(request: Request, perm: str) -> None:
    """能力未授予 → 403（i18n 文案 + 机器可读响应头）；放行则无副作用。

    P0-A3 2026-08-19：带 ``X-Deny-Reason: capability``——前端 apiFetch choke point
    据此出统一「联系管理员开通」指路提示（特性探测，旧前端忽略该头零影响）。
    """
    if not _perm_ok(request, perm):
        raise HTTPException(403, tr(request, "err.perm.capability_denied"),
                            headers={"X-Deny-Reason": "capability"})


# ── 出站媒体体积上限（P3 2026-08-17：25MB 硬编码 → 按平台/配置）────────────
# inbox.media.limits_mb: {default: 25, telegram: 200, whatsapp: 64, ...}
# #169 2026-09-05：limits_mb 缺省时不再一律 25——内建平台默认表（LINE 100 按 117 号
# 真机三档实测 / TG 200 / WA 64）在 `src/inbox/media_limits.py`，LINE worker 的
# `outbound_max_bytes` 缺省同源（此前独立硬编码 20MB，比路由还低）。前端经
# send-caps.media_max_mb 拿到同一数字做上传前预检（两端同源，不各写一套）。
from src.inbox.media_limits import (  # noqa: E402
    describe_over_cap as _describe_over_cap,
    media_cap_mb_for as _media_cap_mb_for,
    platform_media_cap_mb as _media_cap_mb,
)


# ── #180：owns_media 为 False 的 501 分因（2026-09-05）─────────────────────────
# 钧机 3PZ95W：同一 LINE 账号 12:06 send_media type=voice delivered=True 成功过两次，
# 00:48 却 501「该账号不支持从收件箱发送语音（需 protocol 多开且在线）」——能力存在，
# 只是 worker 那一刻不在 running（receiver 连败→编排器重启期）。通用措辞对 LINE 号是
# 误导（坐席以为「这个号永远发不了语音」）。按 orchestrator.media_capability 分两套话：
#   no_worker / no_send_media → 「该平台账号暂不支持从工作台发送语音/媒体」
#   worker_not_running        → 「{platform} 通道正在重连（{state}），稍后再试」
# 返回体是 dict（前端 d.detail.message 已兼容），带 reason/state/backoff_sec 供媒体按钮
# 灰态 tooltip 同源（unified_inbox.html 属 J-4，落点写进结束报告）。
_PLATFORM_LABEL = {"line": "LINE", "whatsapp": "WhatsApp", "telegram": "Telegram",
                   "messenger": "Messenger", "instagram": "Instagram", "zalo": "Zalo",
                   "qq": "QQ", "qqbot": "QQ Bot"}


def media_capability_or_none(orch: Any, platform: str, account_id: str) -> Dict[str, Any]:
    """编排器分因能力（旧编排器无 media_capability 时按 owns_media 合成同形状）。"""
    fn = getattr(orch, "media_capability", None)
    if callable(fn):
        try:
            cap = dict(fn(platform, account_id) or {})
            cap.setdefault("owns", False)
            cap.setdefault("reason", "" if cap["owns"] else "no_worker")
            cap.setdefault("state", "")
            return cap
        except Exception:
            logger.debug("media_capability 探测失败（回落 owns_media）", exc_info=True)
    owns = False
    try:
        owns = bool(orch.owns_media(platform, account_id))
    except Exception:
        owns = False
    return {"owns": owns, "reason": "" if owns else "no_worker", "state": "",
            "backoff_sec": 0}


def _media_unsupported_exc(request: Request, cap: Dict[str, Any], *,
                           platform: str, kind: str) -> HTTPException:
    """按分因给 501：``kind`` ∈ media|voice。detail 为 dict（message + 机器可读字段）。"""
    reason = str(cap.get("reason") or "no_worker")
    state = str(cap.get("state") or "")
    label = _PLATFORM_LABEL.get(str(platform or "").lower(), str(platform or "").upper() or "?")
    if reason == "worker_not_running":
        msg = tr(request, f"err.inbox.{kind}_reconnecting", platform=label,
                 state=state or "restarting")
    else:
        msg = tr(request, f"err.inbox.{kind}_unsupported_platform")
    return HTTPException(501, {
        "code": f"{kind}_unsupported", "message": msg, "reason": reason,
        "state": state, "platform": str(platform or "").lower(),
        "backoff_sec": int(cap.get("backoff_sec") or 0),
    })


# P1-3（2026-08-11）：语音合成期「正在录音」气泡的 fire-and-forget 任务强引用集
# （create_task 对任务仅弱引用，防 GC 吞任务——与 voice_routes._TTS_JOBS 同教训）。
_VOICE_ACTION_TASKS: set = set()


def _fire_voice_recording_action(
    orch: Any, platform: str, account_id: str, chat_key: str,
) -> None:
    """给客户挂「正在录音」状态（best-effort，绝不阻塞/不抛）。

    orch.send_chat_action 自带能力探测与护栏（TG/WA 有；LINE/Messenger 协议无此
    能力返 False）；一次 action ~5s 自动过期，调用方在合成前/合成后各挂一次即可
    覆盖交互链常规时长，刻意不做常驻续挂循环（省去任务生命周期管理）。
    """
    try:
        t = asyncio.create_task(
            orch.send_chat_action(platform, account_id, chat_key, "record_audio"))
        _VOICE_ACTION_TASKS.add(t)
        t.add_done_callback(_VOICE_ACTION_TASKS.discard)
    except Exception:
        pass


def _account_send_block(platform: str, account_id: str) -> str:
    """该 (platform, account_id) 的发送拦截原因：``""``（放行）/ ``removed`` / ``offline``。

    - ``removed``：软删账号只读展示历史（见 ProtocolInboxAdapter），禁止发送。
    - ``offline``：已登出账号（聊天页已隐藏其会话；深链/旧标签页仍可能点发送）——
      给 409「请先重新登录」，避免打到不存在的 worker 冒笼统 5xx。防误拦：注册表说
      offline 但编排器实际有在跑的 worker（状态陈旧/刚重连）→ 放行，以运行时为准。
    注意：实时 Telegram A 线用 account_id='default'（不在注册表），不会被误拦；
    查不到/异常一律放行（宁可让下游如实失败，不做假拦截）。
    """
    if not platform or not account_id or account_id == "default":
        return ""
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(platform, account_id)
        st = str((row or {}).get("status") or "")
        if st == "removed":
            return "removed"
        if st == "offline":
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                if get_orchestrator().owns(platform, account_id):
                    return ""
            except Exception:
                # ⚠ 只加留痕，**刻意不改返回值**：这里落到下面的 return "offline" ＝
                # 拦发，与本函数 docstring 写的「查不到/异常一律放行」策略相反
                # （外层 except 是 return "" 放行）。注册表已判 offline，把「所有权
                # 探测失败」当作维持原判也说得通，所以这是个待产品决策的分歧而不是
                # 明确 bug —— 先让它可见，别静默地按拦发执行。
                logger.warning(
                    "[send] 账号所有权探测异常，按注册表原判 offline 拦发 %s/%s"
                    "（注意与本函数「异常放行」策略不一致）", platform, account_id,
                    exc_info=True)
            return "offline"
        return ""
    except Exception:
        return ""


def _raise_if_account_blocked(request: Request, platform: str, account_id: str) -> None:
    """发送前账号态闸门：removed/offline 各给对应 409 文案（坐席该做的事不同——
    removed=只能看历史；offline=去重新登录）。"""
    blk = _account_send_block(platform, account_id)
    if blk == "removed":
        raise HTTPException(409, tr(request, "err.inbox.account_removed"))
    if blk == "offline":
        raise HTTPException(409, tr(request, "err.inbox.account_offline"))
    # M-2 A2（#232）：注册表还 online 但通道其实没通（边车没有该账号会话 / worker
    # 放弃重连）→ 媒体 / 贴纸 / 语音三条人工链与文本链同一闸（send_via_adapters 内
    # 另判文本链）。结构化 detail 让前端画横幅 + 重登出路。fail-open：判定异常放行。
    try:
        from src.integrations.platform_session_health import channel_send_block_reason
        _cb = channel_send_block_reason(platform, account_id)
    except Exception:
        _cb = {}
    if _cb.get("reason"):
        raise HTTPException(503, {
            "code": "channel_disconnected",
            "platform": platform, "account_id": account_id,
            "message": tr(request, "err.inbox.channel_disconnected", platform=platform),
        })


# ── 发送护栏可见化（P0 2026-08-12，修「额度拦截被包成 ok:true 静默吞掉」）────
# 编排器护栏（Kill-Switch / 金丝雀 / 授权 / 反封号闸门）拦截时返回
# {delivered:false, blocked:reason} 而不抛错——那是给自动链「不记冷却、择机
# 重试」的契约；人工发送路由此前原样透传 → 前端只认 ok → 坐席点了毫无反应。
# 此处三件套：① 预检（fail-open，省一次翻译/TTS 合成）② 结果判定 ③ 统一 409
# detail（code=send_blocked + 人话原因 + 额度数字 + 预计释放时刻）。
# 预判/应答与护栏共用 send_gate_snapshot（同一判定函数），绝不另算一套。


def _humanize_send_failure(request: Request, raw_msg: str,
                           reason_code: str = "") -> str:
    """败因人话化（实施86 域B-1，#23/#49 实录「Server error '500/429 …'」直显）。

    能归类 → 一句人话 + 括注原始码（排查不丢线索）；归不了类保持原文——
    宁可技术味也不错贴标签误导排查。纯映射，绝不抛。
    """
    try:
        from src.inbox.send_failure_class import (
            FAILURE_CLASS_I18N, classify_send_failure)
        klass = classify_send_failure(raw_msg, reason_code)
        if not klass:
            return raw_msg
        return f"{tr(request, FAILURE_CLASS_I18N[klass])}（{reason_code or klass}）"
    except Exception:
        return raw_msg


def _trace_failed_manual_send(request: Request, platform: str, account_id: str,
                              chat_key: str, text: str, reason: str) -> str:
    """手发失败留痕（实施86 域B-1，#21「无法发送的消息也没有记录」）。

    复用 B63③ 的 status=failed 留痕行（前端既有「发送失败＋一键重发」气泡）——
    自动链早有此待遇，手发此前失败即蒸发，坐席打的字只剩 toast 一闪。
    best-effort：留痕失败绝不影响失败响应本身。
    """
    try:
        store = _inbox_store(request)
        fn = getattr(store, "record_failed_outbound", None)
        if store is None or fn is None or not str(text or "").strip():
            return ""
        cid = _conv_id(platform, account_id, chat_key)
        return str(fn(cid, text, reason=str(reason or "")[:200]) or "")
    except Exception:
        logger.debug("[send] 手发失败留痕写入失败（忽略）", exc_info=True)
        return ""


def _result_undelivered(result: Any) -> str:
    """适配器/编排器返回体的未送达判定：``blocked``（护栏拦截）/``failed``
    （worker 显式报 delivered=False）/``""``（正常）。只认显式 False——
    无 delivered 键的历史返回体（如 LINE 入队 {queued:true}）不受影响。"""
    if not isinstance(result, dict):
        return ""
    if result.get("blocked"):
        return "blocked"
    if result.get("delivered") is False:
        return "failed"
    return ""


def _undelivered_reason(result: Any, platform: str, account_id: str) -> tuple:
    """delivered=False 的败因 ``(_fail_raw, _fail_kind)``，绝不为空（P-5 C，#259）。
    见 send_failure_class.undelivered_reason；探测异常时退到固定码，不让失败响应再失败。"""
    try:
        from src.inbox.send_failure_class import undelivered_reason
        return undelivered_reason(result, platform, account_id)
    except Exception:
        _fail_raw = str((result or {}).get("error") or (result or {}).get("error_kind") or "") \
            if isinstance(result, dict) else ""
        return (_fail_raw or "adapter_no_reason: 适配器未报原因"), "adapter_no_reason"


def _log_send_fail(platform: str, account_id: str, chat_key: str,
                   _fail_raw: str, _fail_kind: str, *, tag: str = "send") -> None:
    """失败分支统一日志行 ``[send] fail conv=… reason=… kind=…``（值守 grep 口径）。"""
    logger.warning("[%s] fail conv=%s reason=%s kind=%s", tag,
                   _conv_id(platform, account_id, chat_key), _fail_raw[:200], _fail_kind or "-")


def _send_blocked_exc(
    request: Request, platform: str, account_id: str, chat_key: str,
    reason: str = "", snap: Any = None, status_code: int = 409,
) -> HTTPException:
    """护栏拦截 → 409 HTTPException（code=send_blocked，供前端分型渲染）。
    ``status_code``：TK-3 真机离线走 503，其余仍 409。

    detail 结构与语言错配/近重复守卫同族：{code, message, reason, quota?,
    frees_at?}。message 按拦因族谱出人话（额度/红灯/急停/放量/授权/会话掉线）。
    """
    from src.inbox.send_gate_status import blocked_reason_key, send_gate_snapshot
    if snap is None:
        try:
            _cm = getattr(request.app.state, "config_manager", None)
            snap = send_gate_snapshot(
                platform, account_id, chat_key,
                config=(getattr(_cm, "config", None) or {}) if _cm else {})
        except Exception:
            snap = None
    snap = snap if isinstance(snap, dict) else {}
    reason = str(reason or snap.get("reason") or "")
    quota = snap.get("quota") if isinstance(snap.get("quota"), dict) else {}
    used = int(quota.get("used") or 0)
    cap = int(quota.get("cap") or 0)
    frees_at = snap.get("frees_at")
    fam = blocked_reason_key(reason)
    if fam == "quota" and cap > 0:
        message = tr(request, "err.inbox.send_blocked_quota", used=used, cap=cap)
    else:
        message = tr(request, f"err.inbox.send_blocked_{fam}", reason=reason)
    detail: Dict[str, Any] = {
        "code": "send_blocked", "reason": reason, "message": message,
    }
    if cap > 0:
        detail["used"], detail["cap"] = used, cap
    if frees_at:
        detail["frees_at"] = float(frees_at)
    # P0 2026-08-23 急停归因：快照带 kill（scope/source/cause/expires_at）时随
    # 409 透传——点击拦截路径与 45s 轮询同源同貌，横幅立即能显示「谁冻的/几点恢复」。
    _kill = snap.get("kill") if isinstance(snap.get("kill"), dict) else None
    if _kill:
        detail["kill"] = _kill
    logger.warning(
        "[send] guard=send_blocked 护栏拦截已显式回执 conv=%s reason=%s used=%s cap=%s",
        _conv_id(platform, account_id, chat_key), reason, used, cap)
    return HTTPException(int(status_code or 409), detail)  # TK-3：真机离线可 503


def _send_gate_exc(
    request: Request, platform: str, account_id: str, chat_key: str,
    *, owned: Any = None,
) -> Any:
    """发送前预检：护栏会拦 → 返回待抛的 409（调用方自理幂等释放等收尾）；
    放行/预检自身故障 → None（fail-open，预检坏了绝不拦发送）。

    仅当编排器实际拥有该账号（enforcement 所在路径）才预检——RPA 回落路径
    此前不受这些护栏约束，预检若无条件拦就是扩大执法面（行为变更），只做
    反馈修复不做这个。"""
    try:
        if owned is None:
            from src.integrations.account_orchestrator import (
                get_orchestrator_if_running,
            )
            _orch = get_orchestrator_if_running()
            owned = bool(_orch and _orch.owns(platform, account_id))
        if not owned:
            return None
        from src.inbox.send_gate_status import send_gate_snapshot
        _cm = getattr(request.app.state, "config_manager", None)
        snap = send_gate_snapshot(
            platform, account_id, chat_key,
            config=(getattr(_cm, "config", None) or {}) if _cm else {})
        if not snap or not snap.get("blocked"):
            return None
        return _send_blocked_exc(
            request, platform, account_id, chat_key,
            reason=str(snap.get("reason") or ""), snap=snap)
    except HTTPException:
        raise
    except Exception:
        logger.debug("[send] 护栏预检自身故障（放行交编排器兜底）", exc_info=True)
        return None


def _rpa_auto_voice_enabled(request: Request, platform: str, account_id: str) -> bool:
    """RPA 会话是否配置了**设备端**语音输出（``voice_output.enabled``）。

    P0-6/C9：LINE/WhatsApp/Messenger 的语音由 RPA 在手机上经 voice_output 自动发出，
    坐席不能从收件箱输入区直发。此探测供 send-caps 标注「仅自动语音」，把语音按钮的
    禁用理由说清楚。best-effort：任何异常按 False（回落笼统「不支持」提示）。
    """
    try:
        st = request.app.state
        if platform == "line":
            svcs = list(getattr(st, "line_rpa_services", None) or [])
            single = getattr(st, "line_rpa_service", None)
        elif platform == "whatsapp":
            svcs = list(getattr(st, "whatsapp_rpa_services", None) or [])
            single = getattr(st, "whatsapp_rpa_service", None)
        elif platform == "messenger":
            svcs = list(getattr(st, "messenger_rpa_services", None) or [])
            single = getattr(st, "messenger_rpa_service", None)
        else:
            return False
        if not svcs and single is not None:
            svcs = [single]
        if not svcs:
            return False
        # 优先精确匹配账号，匹配不到（如 messenger 单例多账号）退到全部扫描
        match = [s for s in svcs
                 if str(getattr(s, "account_id", "default")) == account_id] or svcs
        for svc in match:
            cfg = getattr(svc, "_merged_cfg", None) or getattr(svc, "_cfg", None) or {}
            if not isinstance(cfg, dict):
                continue
            vo = cfg.get("voice_output") or {}
            if isinstance(vo, dict) and vo.get("enabled"):
                return True
            # messenger 多账号：voice_output 可嵌在 accounts[] 里
            for acct in (cfg.get("accounts") or []):
                if not isinstance(acct, dict):
                    continue
                avo = acct.get("voice_output") or {}
                if isinstance(avo, dict) and avo.get("enabled"):
                    return True
        return False
    except Exception:
        logger.debug("send-caps voice_output 探测失败（按 False）", exc_info=True)
        return False


async def _deliver_bubble_parts(
    request: Request, platform: str, account_id: str, chat_key: str,
    parts, adapters, *, reply_to, bcfg, origin: str = "manual",
):
    """把多条气泡按拟人节奏逐条发出（P1.5 手动路径分条）。

    语义对齐语音 split_send / autosend 分条：仅首条带 reply_to；条间
    typing + 思考/打字延迟；首条失败原样抛出（外层释放幂等键），中途失败
    已发算数、剩余丢弃。返回 (首条 result, 实发条数, 各条 message_id 列表)。

    镜像口径（#210 B，2026-09-06）：每条都独立走 ``send_via_adapters`` → 编排器
    ``send`` 成功即按**那一条的文本 + 平台 message_id** 落一行出站镜像——工作台
    行数 == 客户手机条数是这里的不变量（test_bubbles_mirror_per_part 钉住）；
    本函数**不**再为原稿整段补任何一行。``message_ids`` 回给响应体 / 日志，供
    「工作台条数与手机不一致」类报障直接对账。
    """
    from src.inbox.reply_split import plan_bubble_gaps
    _orch = None
    try:
        from src.integrations.account_orchestrator import get_orchestrator
        _o = get_orchestrator()
        if _o.owns(platform, account_id):
            _orch = _o
    except Exception:
        _orch = None
    # 条间隔预排（2026-08-09 对齐 autosend/A 线）：整组一次估值 →
    # total_budget_sec 等比压缩保节奏形状；latin_per_char_sec=英文真实手速
    # （加权刻度会把英文打字耗时低估 ~4 倍，英语客户条间仍是机关枪）。
    try:
        _gaps = plan_bubble_gaps(
            [str(p or "") for p in parts],
            gap_sec_lo=float(bcfg["gap_sec_lo"]),
            gap_sec_hi=float(bcfg["gap_sec_hi"]),
            per_char_sec=float(bcfg["per_char_sec"]),
            latin_per_char_sec=bcfg.get("latin_per_char_sec"),
            max_gap_sec=float(bcfg.get("max_gap_sec", 6.0)),
            total_budget_sec=float(bcfg.get("total_budget_sec") or 0.0),
        )
    except Exception:
        _gaps = []
    first_result = None
    sent = 0
    message_ids: list = []
    for i, part in enumerate(parts):
        if i > 0:
            # 条间「想（静默）→ 打字（挂正在输入续挂）」：与 autosend/A 线同一
            # 节奏模型——客户视角是同一个人设在打字，不该因入口不同而两种手感。
            # 兜底 3.0＝gap_sec_lo 时代缺省：规划器异常时整组不至于回到机关枪
            _gap = _gaps[i - 1] if i - 1 < len(_gaps) else 3.0
            try:
                from src.integrations.humanize_metrics import (
                    record_bubble_gap as _rbg_manual,
                )
                _rbg_manual("manual", platform, _gap)
            except Exception:
                pass
            try:
                from src.inbox.humanize import (
                    estimate_typing_lead,
                    run_presend_humanization,
                )
                _tp_part = None
                if _orch is not None:
                    async def _tp_part(_action, _p=platform,
                                       _a=account_id, _c=str(chat_key)):
                        await _orch.send_chat_action(_p, _a, _c, "typing")
                await run_presend_humanization(
                    delay=_gap, action="typing", typing=_tp_part,
                    sleep=asyncio.sleep,
                    typing_lead_sec=estimate_typing_lead(
                        part, per_char_sec=float(bcfg["per_char_sec"]),
                        latin_per_char_sec=bcfg.get("latin_per_char_sec")),
                )
            except Exception:
                await asyncio.sleep(_gap)
        try:
            res = await send_via_adapters(
                request, platform, account_id, chat_key, part, adapters,
                reply_to=(reply_to if i == 0 else None), mentions=None,
                origin=origin,
            )
        except Exception:
            if sent == 0:
                raise
            logger.warning(
                "[send] 气泡分条中途失败，已发 %d/%d platform=%s",
                sent, len(parts), platform)
            break
        # P0 2026-08-12：护栏拦截/显式未送达是**数据形态**的失败（编排器不抛），
        # 不能计入已发——首条即拦时把结果交给外层统一转 409/502（幂等释放也在
        # 外层），中途被拦按「已发算数、剩余丢弃」旧语义收口。
        if _result_undelivered(res):
            if sent == 0:
                first_result = res
            else:
                logger.warning(
                    "[send] 气泡分条中途被拦/未送达，已发 %d/%d (%s)",
                    sent, len(parts),
                    str(res.get("blocked") or res.get("error") or "")[:80])
            break
        if i == 0:
            first_result = res
        sent += 1
        message_ids.append(
            str(res.get("message_id") or "") if isinstance(res, dict) else "")
    if sent:
        logger.info(
            "[send] 气泡分条已发 %d/%d 条（工作台按条镜像）platform=%s chat=%s ids=%s",
            sent, len(parts), platform, str(chat_key)[:24],
            [m or "-" for m in message_ids])
    return first_result, sent, message_ids


# ── send-media 流式实现（M-3 A #227 / B #229 #231，2026-09-06）──────────────────
#: 超限时最多再吞多少字节让浏览器**读得到** 413——服务端一响应就关连接的话，浏览器还在
#: 推 body，只会看到连接被重置（onerror，无状态码），坐席看到的就又是「结果未知」。
#: 前端已按同源上限预检，超限请求本就罕见；256MB 以内顺手吞完（本机回环秒级），更大的
#: 直接响应，接受它可能表现为网络错误。
_SEND_MEDIA_DRAIN_MAX = 256 * 1024 * 1024
_SEND_MEDIA_WRITE_BUF = 1024 * 1024


async def _drain_stream(chunks: Any, max_bytes: int) -> int:
    """读掉并丢弃剩余请求体（上限 max_bytes），返回吞掉的字节数。任何异常吞掉即停。"""
    n = 0
    try:
        async for c in chunks:
            n += len(c)
            if n >= max_bytes:
                break
    except Exception:  # noqa: BLE001
        pass
    return n


def _too_large_exc(request: Request, size_bytes: int, cap_mb: int) -> HTTPException:
    """413 文案：知道实际大小就带「X MB 超过 Y MB（超出 Z）」，不知道就只报上限。"""
    _ov = _describe_over_cap(size_bytes, cap_mb)
    _key = ("err.inbox.file_too_large_detail" if _ov["size"] > 0
            else "err.inbox.file_too_large_mb")
    return HTTPException(413, tr(
        request, _key, mb=cap_mb, cap=cap_mb, size=_ov["size"], over=_ov["over"]))


async def _send_media_streamed(request: Request, meta: Dict[str, str], filename: str,
                               chunks: Any, *, declared: int, t0: float, mode: str):
    """把一个异步字节流当作出站媒体：校验 → 流式落盘（线程池写）→ 可选换封装 → 投递。

    ``chunks`` 是 ``bytes`` 的异步迭代器（裸流＝request.stream()；multipart＝UploadFile
    分块读）。每条退出路径都落一行 ``[send-media] ...`` 带 reason / 大小 / 耗时——8YNDKE
    的「后端零记录」以后不会再发生在路由这一层。
    """
    from src.integrations.protocol_bridge import (
        OUT_VIDEO_TRANSCODE_EXT, media_paths, media_type_from_ext,
        remux_video_to_mp4, video_transcode_available,
    )
    from starlette.concurrency import run_in_threadpool

    platform = str(meta.get("platform") or "").lower()
    account_id = str(meta.get("account_id") or "default")
    chat_key = str(meta.get("chat_key") or "")
    caption = str(meta.get("caption") or "")
    _client_msg_id = str(meta.get("client_msg_id") or "").strip()
    _fname = str(filename or "")[:80]
    _ext = (os.path.splitext(str(filename or ""))[1] or ".bin").lower()
    mtype = media_type_from_ext(_ext)
    _decl_mb = declared / (1024 * 1024) if declared else 0.0
    logger.info(
        "[send-media] request platform=%s acct=%s chat=%s file=%s kind=%s declared=%.1fMB mode=%s",
        platform, account_id, chat_key, _fname, mtype, _decl_mb, mode)

    def _rejected(reason: str, extra: str = "") -> None:
        logger.info("[send-media] rejected reason=%s platform=%s file=%s declared=%.1fMB %.2fs %s",
                    reason, platform, _fname, _decl_mb, time.perf_counter() - t0, extra)

    # 能力权限（2026-08-16）：发媒体受 chat.send_media 闸（媒体不耗字符额度）
    try:
        _deny_capability(request, "chat.send_media")
        _raise_if_account_blocked(request, platform, account_id)
    except HTTPException:
        _rejected("capability_or_blocked")
        raise

    from src.integrations.account_orchestrator import get_orchestrator
    orch = get_orchestrator()
    _cap = media_capability_or_none(orch, platform, account_id)
    if not _cap.get("owns"):
        _rejected("unsupported_account", f"caps_reason={_cap.get('reason') or ''}")
        raise _media_unsupported_exc(request, _cap, platform=platform, kind="media")
    # 护栏预检（P0 2026-08-12）：此时尚未读文件/落盘/占幂等位，被拦零清理
    _gate_ex = _send_gate_exc(request, platform, account_id, chat_key, owned=True)
    if _gate_ex is not None:
        _rejected("send_gate")
        raise _gate_ex

    _config = (getattr(getattr(request.app.state, "config_manager", None),
                       "config", None) or {})
    # 上限同源（D-M5）：按平台 + 类别（视频临时压 50MB），与 send-caps 下发前端的是同一函数
    _cap_mb = _media_cap_mb_for(_config, platform, mtype)
    _cap_bytes = _cap_mb * 1024 * 1024
    if declared and declared > _cap_bytes:
        # 裸流：Content-Length 就是文件大小，传完之前就能给出精确的 413
        _drained = await _drain_stream(chunks, min(declared, _SEND_MEDIA_DRAIN_MAX))
        _rejected("too_large", f"cap={_cap_mb}MB drained={_drained / (1024 * 1024):.1f}MB")
        raise _too_large_exc(request, declared, _cap_mb)

    # 视频容器口径（D-M4）：.mov/.m4v 无 ffmpeg 时在读文件之前就拒，前端同源白名单
    _needs_remux = mtype == "video" and _ext in OUT_VIDEO_TRANSCODE_EXT
    if _needs_remux and not video_transcode_available():
        await _drain_stream(chunks, min(declared or _SEND_MEDIA_DRAIN_MAX, _SEND_MEDIA_DRAIN_MAX))
        _rejected("video_format_unsupported", "ffmpeg_missing")
        raise HTTPException(415, tr(request, "err.inbox.video_format_unsupported", ext=_ext))

    # 头部 64KB：内容守卫看魔数（前 16 字节），多读一点让空文件/截断件也能判
    head = b""
    _it = chunks.__aiter__()
    while len(head) < 64 * 1024:
        try:
            c = await _it.__anext__()
        except StopAsyncIteration:
            break
        head += c
    if not head:
        _rejected("empty_file")
        raise HTTPException(400, tr(request, "err.inbox.empty_file"))

    # P0 2026-08-17：出站内容守卫——危险扩展名黑名单 + magic bytes 与扩展名大类一致性
    from src.inbox.media_guard import validate_outbound_media
    _mg = validate_outbound_media(str(filename or ""), head)
    if not _mg["ok"]:
        _mg_key = {
            "ext_forbidden": "err.inbox.media_ext_forbidden",
            "executable_content": "err.inbox.media_exec_content",
        }.get(str(_mg["reason"]), "err.inbox.media_magic_mismatch")
        await _drain_stream(_it, _SEND_MEDIA_DRAIN_MAX)
        _rejected("content_guard", f"guard={_mg['reason']} detected={_mg['detected'] or '?'}")
        raise HTTPException(415, tr(request, _mg_key))

    # P0 幂等键（与文本 send 同口径；置于全部校验之后、真发送之前）
    from src.inbox.send_dedup import get_send_dedup
    _dedup = get_send_dedup()
    _dedup_scope = f"media:{platform}:{account_id}:{chat_key}"
    if not _dedup.reserve(_dedup_scope, _client_msg_id):
        await _drain_stream(_it, _SEND_MEDIA_DRAIN_MAX)
        logger.info("[send-media] 幂等去重命中，拒绝重复发送 scope=%s id=%s",
                    _dedup_scope, _client_msg_id[:16])
        return {"ok": True, "duplicate": True}

    import secrets as _secrets
    _base = f"out_{account_id}_{_secrets.token_hex(6)}"
    _dest, url = media_paths(platform, _base, _ext)
    local = str(_dest)

    def _cleanup(path: str, why: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            # 删不掉不该挡住给坐席的回执，但也不能无声：反复失败会慢性占盘
            logger.warning("[send-media] %s后临时文件未能删除 path=%s", why, path, exc_info=True)

    # 流式落盘：收到的块攒到 1MB 再经线程池写盘——磁盘慢/杀软扫描都不占事件循环
    _size = 0
    _overflow = False
    _t_recv0 = time.perf_counter()
    try:
        _fh = await run_in_threadpool(open, local, "wb")
        try:
            _buf = bytearray(head)
            _size = len(head)
            if _size > _cap_bytes:
                _overflow = True
            while not _overflow:
                if len(_buf) >= _SEND_MEDIA_WRITE_BUF:
                    await run_in_threadpool(_fh.write, bytes(_buf))
                    _buf = bytearray()
                try:
                    c = await _it.__anext__()
                except StopAsyncIteration:
                    break
                _size += len(c)
                if _size > _cap_bytes:
                    _overflow = True
                    break
                _buf += c
            if _buf and not _overflow:
                await run_in_threadpool(_fh.write, bytes(_buf))
        finally:
            await run_in_threadpool(_fh.close)
    except Exception as ex:  # noqa: BLE001
        _dedup.release(_dedup_scope, _client_msg_id)
        _cleanup(local, "上传失败")
        _rejected("recv_error", f"received={_size / (1024 * 1024):.1f}MB err={type(ex).__name__}")
        raise HTTPException(502, tr(request, "err.inbox.media_send_failed", err=ex))
    _t_recv = time.perf_counter() - _t_recv0
    if _overflow:
        _dedup.release(_dedup_scope, _client_msg_id)
        _cleanup(local, "超限拒收")
        _drained = await _drain_stream(_it, _SEND_MEDIA_DRAIN_MAX)
        # 流式闸中途停下时只知道「超了」：有 Content-Length 就报它，没有就只报上限
        _rejected("too_large", f"cap={_cap_mb}MB received={_size / (1024 * 1024):.1f}MB "
                               f"drained={_drained / (1024 * 1024):.1f}MB")
        raise _too_large_exc(request, declared if mode == "stream" else 0, _cap_mb)

    # D-M4：QuickTime 容器 → MP4（线程池里跑 ffmpeg，-c copy 秒级）
    _t_remux = 0.0
    if _needs_remux:
        _t_x0 = time.perf_counter()
        _mp4_dest, _mp4_url = media_paths(platform, _base, ".mp4")
        _ok, _why = await run_in_threadpool(remux_video_to_mp4, local, str(_mp4_dest))
        _t_remux = time.perf_counter() - _t_x0
        _cleanup(local, "换封装")
        if not _ok:
            _dedup.release(_dedup_scope, _client_msg_id)
            _cleanup(str(_mp4_dest), "换封装失败")
            _rejected("video_transcode_failed", f"why={_why[:160]} remux={_t_remux:.2f}s")
            raise HTTPException(415, tr(request, "err.inbox.video_transcode_failed", ext=_ext))
        local, url = str(_mp4_dest), _mp4_url
        logger.info("[send-media] 已换封装 %s→.mp4 file=%s size=%.1fMB remux=%.2fs",
                    _ext, _fname, _size / (1024 * 1024), _t_remux)

    # P0 2026-08-17：文档无配文 → 用原始文件名作收件箱镜像文本（气泡/会话
    # 预览显示「report.pdf」而非空行；只影响坐席台可读文本，不发给客户）。
    _inbox_text = None
    if mtype == "document" and not caption.strip():
        _inbox_text = os.path.basename(str(filename or "")) or None
    _send_agent = _session_agent(request)
    _t_disp0 = time.perf_counter()
    try:
        try:
            res = await orch.send_media(
                platform, account_id, chat_key,
                media_path=local, media_url=url, media_type=mtype,
                caption=caption, inbox_text=_inbox_text, origin="manual")
        except TypeError:
            # 旧签名（无 origin/inbox_text kwarg，测试假编排器常见）→ 回落
            res = await orch.send_media(
                platform, account_id, chat_key,
                media_path=local, media_url=url, media_type=mtype,
                caption=caption)
    except Exception as ex:  # noqa: BLE001
        _dedup.release(_dedup_scope, _client_msg_id)  # 失败释放：同 id 重试可再发
        _rejected("dispatch_error", f"err={type(ex).__name__}:{str(ex)[:120]} "
                                    f"recv={_t_recv:.2f}s dispatch={time.perf_counter() - _t_disp0:.2f}s")
        raise HTTPException(502, tr(request, "err.inbox.media_send_failed", err=ex))
    _t_disp = time.perf_counter() - _t_disp0
    # P0 2026-08-12：拦截/未送达显式回执（同文本路由；防 ok:true 静默吞）
    _undeliv = _result_undelivered(res)
    if _undeliv:
        _dedup.release(_dedup_scope, _client_msg_id)
        _rejected("undelivered", f"kind={_undeliv} err={str(res.get('error') or res.get('blocked') or '')[:80]} "
                                 f"recv={_t_recv:.2f}s dispatch={_t_disp:.2f}s")
        if _undeliv == "blocked":
            raise _send_blocked_exc(
                request, platform, account_id, chat_key,
                reason=str(res.get("blocked") or ""))
        _fail_raw, _fail_kind = _undelivered_reason(res, platform, account_id)
        _log_send_fail(platform, account_id, chat_key, _fail_raw, _fail_kind, tag="send-media")
        raise HTTPException(502, tr(
            request, "err.inbox.send_not_delivered",
            msg=_humanize_send_failure(request, _fail_raw, _fail_kind)))
    cid = _conv_id(platform, account_id, chat_key)
    try:
        ibx = _inbox_store(request)
        if ibx is not None:
            ibx.record_agent_send(
                cid, _send_agent["agent_id"],
                agent_name=_send_agent.get("display_name", ""))
            # Sprint1 接管即静音：媒体发送同属坐席接管，切 manual 停 AI
            # （source=takeover，供横幅/自动接回识别）。
            from src.inbox.takeover_rearm import record_agent_takeover
            record_agent_takeover(ibx, cid)
    except Exception:
        logger.debug("record_agent_send(media) 失败", exc_info=True)
    # #88：媒体送达成功=可达铁证 → 清 dead-peer 陈旧标（与文本路由同口径）
    try:
        from src.ops.dead_peer_registry import clear_on_delivery
        if clear_on_delivery(platform, chat_key):
            logger.info(
                "[send-media] 送达成功，已解除 %s 的 dead-peer 标（自动回复恢复）", cid)
    except Exception:
        pass
    logger.info(
        "[send-media] ok platform=%s chat=%s file=%s kind=%s size=%.1fMB recv=%.2fs remux=%.2fs "
        "dispatch=%.2fs total=%.2fs mode=%s",
        platform, chat_key, _fname, mtype, _size / (1024 * 1024), _t_recv, _t_remux, _t_disp,
        time.perf_counter() - t0, mode)
    return {"ok": True, "result": res, "media_ref": url, "media_type": mtype}


def register_send_routes(app, *, api_auth, page_auth) -> None:
    """挂载文本/媒体/语音发送 + 直发能力探测端点。"""

    @app.post("/api/unified-inbox/send")
    async def api_unified_inbox_send(request: Request, _=Depends(page_auth)):
        """向指定平台/账号发送消息（可选发送前自动翻译成客户语言）。

        Body: { platform, account_id, chat_key, text,
                target_lang?, source_lang?, skip_translate?, copilot_meta? }

        发送前翻译（outbound 闭环）：
        - 不传 target_lang（或 skip_translate=true）→ 行为不变，按原文发送（向后兼容）。
        - target_lang="auto" → 用会话持久化的客户语言（conversations.language）。
        - target_lang 具体语种 → 译成该语言。
        翻译失败 / 目标==源 / 目标为空 → best-effort 回落原文，绝不阻断发送。
        返回额外字段 original_text / sent_text / translation 供前端展示「发出的实际译文」。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        text = str(body.get("text") or "").strip()
        if not chat_key or not text:
            raise HTTPException(400, tr(request, "err.inbox.chat_text_empty"))
        # 能力权限（2026-08-16）：发文字受 chat.send_text 闸；文字本身不耗字符
        # 额度，刻意不加 check_request_quota（额度只管翻译/TTS 这类算力消耗）。
        _deny_capability(request, "chat.send_text")
        _raise_if_account_blocked(request, platform, account_id)
        # 护栏预检（P0 2026-08-12）：额度/急停拦截在这里就 409 显式回执——
        # 省掉后面的翻译调用，且此时幂等键尚未占位、无需释放。fail-open。
        _gate_ex = _send_gate_exc(request, platform, account_id, chat_key)
        if _gate_ex is not None:
            raise _gate_ex

        # P0 幂等键（2026-07-29 多开/双击防双发）：前端每次提交带 client_msg_id，
        # 同 (会话, id) 在 TTL 窗口内重复提交按「已发送」应答但不再真发；
        # 发送失败会释放占位（同 id 显式重试仍可通过）。不带 id=旧行为。
        from src.inbox.send_dedup import get_send_dedup
        _client_msg_id = str(body.get("client_msg_id") or "").strip()
        _dedup = get_send_dedup()
        _dedup_scope = f"{platform}:{account_id}:{chat_key}"
        # ── A1（#148 文本件，2026-09-03）：显式重发按**原件** id 幂等 ──────────
        # 旧口径「重发换新键＝显式重试永不被拦」在断连窗里正好是双发的成因：
        # 前端超时误标失败、服务端其实发出去了，新键让幂等键完全失效（钧机
        # 64PY7D 实锤）。前端「重发」现在带 ``resend_of_cmid``＝原件
        # client_msg_id；这里按原 id 查三态登记表 + 占位表判原件到底有没有出去。
        # 键名刻意**不**复用 ``resend_of``——那个键在 B63③ 已有确定语义（失败
        # 留痕行的 store message_id，成功后改标 resent），两者混用会互相误伤。
        _resend_of_cmid = str(
            body.get("resend_of_cmid")
            or body.get("resend_of_client_msg_id") or "").strip()
        if _resend_of_cmid and _resend_of_cmid != _client_msg_id:
            from src.inbox.send_dedup import RESEND_ALLOW, resend_verdict
            from src.inbox.voice_send_tracker import get_status as _vst_status
            _prior_scope = f"text:{platform}:{account_id}:{chat_key}"
            _prior = _vst_status(_prior_scope, _resend_of_cmid)
            _prior_state = str((_prior or {}).get("state") or "unknown")
            _verdict = resend_verdict(
                _prior_state, _dedup.holds(_dedup_scope, _resend_of_cmid))
            logger.info(
                "[send] resend 幂等裁决 verdict=%s prior_state=%s conv=%s "
                "orig_cmid=%s new_cmid=%s",
                _verdict, _prior_state, _conv_id(platform, account_id, chat_key),
                _resend_of_cmid[:16], _client_msg_id[:16])
            if _verdict != RESEND_ALLOW:
                logger.warning(
                    "[send] guard=resend_dup 原件未确认失败 → 压制本次重发 "
                    "verdict=%s prior_state=%s conv=%s orig_cmid=%s "
                    "（此前此处会真发第二条＝客户收双条）",
                    _verdict, _prior_state,
                    _conv_id(platform, account_id, chat_key),
                    _resend_of_cmid[:16])
                return {
                    "ok": True, "duplicate": True,
                    "resend_suppressed": _verdict,
                    "resend_prior_state": _prior_state,
                    "result": {
                        "duplicate": True,
                        "message_id": str((_prior or {}).get("message_id") or ""),
                    },
                    "original_text": text, "sent_text": text, "translation": None,
                    "message": tr(request, "inbox.send.resend_suppressed"),
                }
        if not _dedup.reserve(_dedup_scope, _client_msg_id):
            logger.info(
                "[send] 幂等去重命中，拒绝重复发送 scope=%s id=%s",
                _dedup_scope, _client_msg_id[:16])
            return {
                "ok": True, "duplicate": True,
                "result": {"duplicate": True},
                "original_text": text, "sent_text": text, "translation": None,
            }
        # #72（0830 UDEKBY 实锤）：文本手动链补「超时→对账」——语音链早有的
        # voice_send_tracker 三态（in_flight/sent/failed）文本链漏配：慢发送
        # （限流 30-65s/条确认）撞前端超时被误报「发送失败」，坐席点「重发」
        # =客户收双条。复用同一登记表（scope 前缀 text: 区分），终局在各失败
        # /成功出口落账，前端超时先打 /send-status 对账再定论。
        from src.inbox import voice_send_tracker as _txt_tracker
        _track_scope = f"text:{platform}:{account_id}:{chat_key}"
        if _client_msg_id:
            _txt_tracker.record_start(_track_scope, _client_msg_id)
            _txt_tracker.record_stage(_track_scope, _client_msg_id, "send")

        def _track_failed(reason: str) -> None:
            if _client_msg_id:
                _txt_tracker.record_failed(_track_scope, _client_msg_id, reason)

        # —— 发送前翻译（outbound 闭环）：默认关闭，显式 target_lang / "auto" 才触发 ——
        original_text = text
        translation_info: Optional[Dict[str, Any]] = None
        target_lang = str(body.get("target_lang") or "").strip()
        source_lang = str(body.get("source_lang") or "").strip()
        skip_translate = bool(body.get("skip_translate"))
        # P1-4：出向翻译漏斗埋点用——原始请求语言/auto 标志/失败标志（best-effort）
        _raw_target = target_lang
        _is_auto = target_lang.lower() == "auto"
        _xlate_failed = False
        if _is_auto:
            target_lang = _resolve_conv_language(request, platform, account_id, chat_key)
        else:
            # 显式语种也归一（zh-cn→zh 等），避免前端/集成方传入非规范码导致守卫误判。
            target_lang = normalize_lang(target_lang)
        source_lang = normalize_lang(source_lang)
        # ── P0-198 语言错配护栏（前半）：待发文本含 CJK 而目标语是非 CJK 语种时，
        #    强制钉源语言（坐席语言=zh），绕开「detect 把『是Steven，别担心。😊』这类
        #    混排短句判成 en → source==target → identity 跳过 → 中文原样发出」的泄漏
        #    机制（2026-07-31 客户机实锤，09:51:56 中文直达英文客户手机）。
        _cjk_conflict = bool(
            target_lang and not skip_translate
            and target_lang.lower() != "unknown"
            and contains_cjk(text) and not lang_is_cjk(target_lang)
        )
        if _cjk_conflict and (not source_lang
                              or source_lang.lower() == target_lang.lower()):
            source_lang = "zh"
        if (
            target_lang
            and not skip_translate
            and target_lang.lower() not in ("unknown", source_lang.lower())
        ):
            try:
                svc = _get_translation_service(request)
                # F+：会话首选引擎（多线路对照择优后记住的）优先；无 → 现有 failover
                _pref_engine = _resolve_conv_engine(request, platform, account_id, chat_key)
                res = await svc.translate(
                    text, target_lang=target_lang, source_lang=source_lang,
                    style="chat", engine=_pref_engine,
                )
                if res.ok and (res.translated_text or "").strip():
                    # 坐席字符计量归因（2026-08-16）：出站预翻译成功=坐席主动
                    # 消费了一次翻译，按**源文本**长度记（original_text；此处
                    # text 即将被译文覆写，与 /translate 的 len(text) 同口径）。
                    record_request_chars(
                        request, "translation", len(original_text))
                    text = res.translated_text.strip()
                    translation_info = res.to_dict()
                elif not res.ok:
                    _xlate_failed = True
                # #276 Q-16（2026-09-11）：手发直发的出站翻译在这里发生——落一行
                # [manual_xlate]（预览路径在 /translate purpose=manual_out 落同名行），
                # 与 AI 链 [xlate] outbound 同粒度可对账。decided_by：conv=会话级钉死
                # 的具体语种 / auto='auto' 由 _resolve_conv_language 解析；confirmed=1
                # 表示前端「目标语≠客户语言」阻断式确认已由坐席点过。
                try:
                    _cid_log = _conv_id(platform, account_id, chat_key) or "-"
                except Exception:
                    _cid_log = "-"
                logger.info(
                    "[manual_xlate] conv=%s from=%s to=%s decided_by=%s path=send ok=%s confirmed=%s",
                    _cid_log,
                    normalize_lang(getattr(res, "source_lang", "") or source_lang or "") or "unknown",
                    target_lang, "auto" if _is_auto else "conv", int(bool(res.ok)),
                    1 if str(body.get("xl_confirmed") or "").strip() in ("1", "true", "True") else 0,
                )
            except Exception:
                _xlate_failed = True
                logger.debug("[send] 发送前翻译失败，按原文发送", exc_info=True)

        # 观察期读数：坐席对守卫弹窗选了「强发」——高强发率=守卫在扰民（阈值该松），
        # 低强发率=拦得对。持久进日志供 chatx_readout 远程统计。
        # C3（#148）：带操作来源——人点确认 vs 重发链路自带，一行日志定性。
        if body.get("force_lang") or body.get("force_dup"):
            _ff = force_guard_log_fields(request, body)
            logger.info(
                "[send] guard=force kind=%s conv=%s agent=%s sess=%s entry=%s "
                "src=%s ip=%s cmid=%s",
                "+".join(k for k, on in (("lang", body.get("force_lang")),
                                         ("dup", body.get("force_dup"))) if on),
                _conv_id(platform, account_id, chat_key),
                _ff["agent"], _ff["sess"], _ff["entry"], _ff["src"],
                _ff["ip"], _ff["cmid"])

        # ── P0-198 语言错配护栏（后半，通用兜底）：无论翻译是否被请求/是否成功，
        #    只要**即将发出的文本**仍含 CJK 而客户会话语言是非 CJK → 409 交坐席确认
        #    （body.force_lang=1 显式强发）。覆盖三条泄漏路径：identity 跳过、引擎
        #    失败回落原文、坐席把中文草稿直接填入发送。发中文给外语客户=人设当场
        #    穿帮，宁可多一次确认。
        if contains_cjk(text) and not bool(body.get("force_lang")):
            _guard_lang = normalize_lang(
                target_lang
                or _resolve_conv_language(request, platform, account_id, chat_key)
                or "")
            if (_guard_lang and _guard_lang.lower() != "unknown"
                    and not lang_is_cjk(_guard_lang)):
                _dedup.release(_dedup_scope, _client_msg_id)
                _track_failed("lang_mismatch")
                logger.warning(
                    "[send] guard=lang_mismatch 语言错配拦截（待坐席确认）conv=%s lang=%s len=%d",
                    _conv_id(platform, account_id, chat_key), _guard_lang, len(text))
                raise HTTPException(409, {
                    "code": "lang_mismatch",
                    "message": tr(request, "err.inbox.lang_mismatch",
                                  lang=_guard_lang),
                })

        # ── P0-198 近重复守卫：与最近 3 分钟内本会话已发出站消息比对，命中 → 409
        #    交坐席确认（body.force_dup=1 强发）。实锤：「生成草稿→填入并发送」对同一
        #    条入站连点三次，三条同义改写全部发出；以及把客户已答过的问题隔分钟再问。
        #    幂等键只防「同一请求重放」，防不了「每次重生都是新请求」——这道守卫补位。
        if not bool(body.get("force_dup")):
            try:
                from src.inbox.outbound_dup_guard import (
                    near_duplicate_of_recent,
                    record_dup_check,
                )
                _ibx_dup = _inbox_store(request)
                _dup_hit = None
                if _ibx_dup is not None:
                    _dup_hit = near_duplicate_of_recent(
                        text,
                        _ibx_dup.list_recent_messages(
                            _conv_id(platform, account_id, chat_key), limit=8),
                    )
                record_dup_check((_dup_hit or {}).get("level", ""))
                if _dup_hit:
                    _dedup.release(_dedup_scope, _client_msg_id)
                    _track_failed("near_duplicate")
                    logger.warning(
                        "[send] guard=near_duplicate 近重复拦截（待坐席确认）conv=%s level=%s sim=%.2f age=%.0fs",
                        _conv_id(platform, account_id, chat_key),
                        _dup_hit["level"], _dup_hit["similarity"],
                        _dup_hit["age_sec"])
                    raise HTTPException(409, {
                        "code": "near_duplicate",
                        "message": tr(request, "err.inbox.near_duplicate",
                                      age=int(_dup_hit["age_sec"])),
                    })
            except HTTPException:
                raise
            except Exception:
                logger.debug("[send] 近重复守卫异常（放行）", exc_info=True)
        else:
            try:
                from src.inbox.outbound_dup_guard import record_dup_check
                record_dup_check("", forced=True)
            except Exception:
                pass

        _send_agent = _session_agent(request)
        _consumed_drafts: list = []   # Q-10 A：本次发送置 consumed 的草稿 id（回给前端免二次 cancel）

        def _mark_send(cid: str) -> None:
            """发送成功后打坐席首响归属点（best-effort，失败不影响发送）。"""
            ibx = _inbox_store(request)
            if ibx is None or not cid:
                return
            try:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
            except Exception:
                logger.debug("record_agent_send 失败（已忽略）", exc_info=True)
            # Phase 6：坐席接管发出消息 → 清除自动回复打的「需人工」标签（闭环收口）
            try:
                from src.integrations.protocol_autoreply import clear_needs_human
                clear_needs_human(ibx, cid)
            except Exception:
                logger.debug("清除 needs-human 标签失败（已忽略）", exc_info=True)
            # Q-3（#264 D）：坐席发送成功＝插话 → 取消该会话在途 / 排队的 L2 AI 稿
            # （摘标上面已做；此前只摘标不取消，XBGPBN 23:56:23 摘标时 AI 稿早已发出）
            try:
                _asw = getattr(request.app.state, "autosend_worker", None)
                if _asw is not None and hasattr(_asw, "note_agent_send"):
                    _asw.note_agent_send(cid)
            except Exception:
                logger.debug("[inflight] agent_send 取消在途稿失败（已忽略）", exc_info=True)
            # Q-10 A（#267，CV4E22 / RU89S6）：坐席发送成功 ＝ 这条入站已被人回过 →
            # 会话在途 AI 稿（含 Tab 采用的那条 body.draft_id）同步置 consumed 并从面板
            # 移除；仅新入站再产新稿。此前只靠前端 fire-and-forget cancel，与紧随的
            # GET pending 竞态 → 旧稿被顶回、坐席每次都要多点一次「忽略」。
            try:
                from src.inbox.autodraft_helpers import consume_drafts_on_agent_send
                _consumed_drafts.extend(consume_drafts_on_agent_send(
                    ibx, cid, draft_id=str(body.get("draft_id") or ""),
                    by="agent_send"))
            except Exception:
                logger.debug("[draft] agent_send 置 consumed 失败（已忽略）", exc_info=True)
            # Sprint1 接管即静音：坐席出站即把会话切 manual，停 AI（后续入站不再产 L2/autosend，
            # protocol 直发亦让位），与 web_chat 适配器(channel_adapters.send)一致；重复调用幂等。
            # P0 2026-08-09：统一走 record_agent_takeover——打 source=takeover 标
            # （保留接管前档位），前端横幅与自动接回（takeover_rearm）都认这个标。
            try:
                from src.inbox.takeover_rearm import record_agent_takeover
                record_agent_takeover(ibx, cid)
            except Exception:
                logger.debug("接管置 manual 失败（已忽略）", exc_info=True)

        # A2 写路径收尾：发送收敛到各渠道适配器（与 collect/status 对称）。
        # 跨切面（坐席首响归属打点）统一留在路由，按 result.conversation_id 归属。
        # P4-5B 引用回复：body.reply_to={id,from_me,participant,text,sender} → 原生引用发送
        _reply_to = body.get("reply_to")
        if not isinstance(_reply_to, dict) or not _reply_to.get("id"):
            _reply_to = None
        # P4-11 群 @提及：透传成员完整 jid 列表（仅协议 worker 用；其它平台经签名探测忽略）
        _mentions = body.get("mentions")
        if not isinstance(_mentions, list) or not _mentions:
            _mentions = None
        else:
            _mentions = [str(x) for x in _mentions if x]
        # —— P1.5 多句分条（inbox.reply_style.bubbles）：翻译后的 text 已是客户可读文本。
        #    前端显式 opt-in（body.bubbles，气泡 chip 可切）+ 配置总闸 + 编排器/私聊守卫；
        #    带 @提及的发送不拆（提及只在群里有意义，群本就不拆——双保险）。
        _bubble_parts: list = []
        _bubble_cfg: Dict[str, Any] = {}
        if bool(body.get("bubbles")) and _mentions is None:
            try:
                from src.inbox.reply_split import (
                    parse_bubbles_cfg,
                    should_split_for_delivery,
                    split_reply_parts,
                )
                _cfg_root = getattr(
                    getattr(request.app.state, "config_manager", None),
                    "config", None) or {}
                _bubble_cfg = parse_bubbles_cfg(_cfg_root)
                _orch_owns = False
                try:
                    from src.integrations.account_orchestrator import get_orchestrator
                    _orch_owns = get_orchestrator().owns(platform, account_id)
                except Exception:
                    _orch_owns = False
                if should_split_for_delivery(
                    cfg=_bubble_cfg, platform=platform, chat_key=chat_key,
                    orch_owns=_orch_owns,
                ):
                    from src.inbox.reply_split import cap_max_parts_for_platform
                    _cand = split_reply_parts(
                        text,
                        max_parts=cap_max_parts_for_platform(
                            int(_bubble_cfg["max_parts"]), platform),
                        max_chars=int(_bubble_cfg["max_chars"]),
                        min_tail_chars=int(_bubble_cfg["min_tail_chars"]),
                        min_total_chars=int(_bubble_cfg["min_total_chars"]),
                        per_sentence=bool(_bubble_cfg.get("per_sentence")),
                        explicit_newline_only=bool(
                            _bubble_cfg.get("explicit_newline_only", True)),
                    )
                    if len(_cand) >= 2:
                        _bubble_parts = _cand
                    elif _cand and _cand[0] != text:
                        # 坐席开了分条 chip 但拆不出第二条（#210 短回复门）：发纯函数
                        # 折叠后的单段，不把多行原样塞进一条消息（2026-08-08 形态）
                        text = _cand[0]
            except Exception:
                logger.debug("[send] 气泡分条判定失败，整段发送", exc_info=True)
        _bubbles_info: Optional[Dict[str, Any]] = None
        try:
            if _bubble_parts:
                result, _bub_sent, _bub_ids = await _deliver_bubble_parts(
                    request, platform, account_id, chat_key,
                    _bubble_parts, _INBOX_ADAPTERS,
                    reply_to=_reply_to, bcfg=_bubble_cfg,
                )
                # message_ids：与工作台镜像行的 platform_msg_id 同值，前端/报障
                # 可直接对账「手机 N 条 == 工作台 N 行」（#210 B）
                _bubbles_info = {
                    "parts_total": len(_bubble_parts),
                    "parts_sent": _bub_sent,
                    "message_ids": list(_bub_ids or []),
                }
                try:
                    from src.inbox.reply_split import record_bubble_send
                    record_bubble_send(
                        "manual", _bub_sent,
                        partial=_bub_sent < len(_bubble_parts))
                except Exception:
                    pass
            else:
                # origin="manual"（P1 2026-08-12）：坐席人工发送用完整日额度，
                # 不受 reserve_for_manual 让路口径影响（那是给自动链的）。
                result = await send_via_adapters(
                    request, platform, account_id, chat_key, text, _INBOX_ADAPTERS,
                    reply_to=_reply_to, mentions=_mentions, origin="manual",
                )
        except ChannelSendError as ex:
            _dedup.release(_dedup_scope, _client_msg_id)  # 失败释放：同 id 重试可再发
            _track_failed(str(getattr(ex, "reason_code", "") or "send_error"))
            # 实施86 域B-1（#21/#23）：真实投递失败 → 失败留痕（坐席在消息流里
            # 看得见这条没发出去 + 一键重发）+ 败因人话化（不再裸 HTTP 状态行）。
            _trace_failed_manual_send(
                request, platform, account_id, chat_key, text,
                getattr(ex, "reason_code", "") or ex.detail)
            # M-2 A2（#232）：通道未连接 → 结构化 detail（前端画「该账号会话未建立，
            # 请重新登录」横幅 + 重登出路，而不是一条裸 toast）
            _rc = str(getattr(ex, "reason_code", "") or "")
            if _rc == "channel_disconnected":
                raise HTTPException(ex.status_code, {
                    "code": "channel_disconnected",
                    "platform": platform, "account_id": account_id,
                    "message": tr(request, "err.inbox.channel_disconnected",
                                  platform=platform),
                })
            # TK-3 B6：桥闸门 reason_code（policy_* / device_offline）走拦因族谱人话，
            # 与编排器护栏同一 409 send_blocked 形；真机离线保留 503。
            try:
                from src.inbox.send_gate_status import blocked_reason_key as _brk
                _fam = _brk(_rc)
            except Exception:
                _fam = "generic"
            if _fam == "device_offline" or str(_fam).startswith("policy"):
                raise _send_blocked_exc(
                    request, platform, account_id, chat_key, reason=_rc,
                    status_code=int(getattr(ex, "status_code", 0) or 409))
            raise HTTPException(ex.status_code, _humanize_send_failure(
                request, ex.detail, _rc))
        except Exception as _send_ex:
            _dedup.release(_dedup_scope, _client_msg_id)
            _track_failed("send_exception")
            _trace_failed_manual_send(
                request, platform, account_id, chat_key, text,
                str(_send_ex)[:200])
            raise
        # P0 2026-08-12：编排器把护栏拦截/未送达当**数据**返回（自动链契约），
        # 人工路由必须在此翻译成显式失败——此前包成 ok:true，坐席点了毫无反应，
        # 还连带打了接管标/首响归属（什么都没发出去，会话却被切 manual）。
        _undeliv = _result_undelivered(result)
        if _undeliv:
            _dedup.release(_dedup_scope, _client_msg_id)
            _track_failed(str(_undeliv))
            if _undeliv == "blocked":
                # 护栏拦截＝根本没尝试投递，刻意不留痕（重发也会被同一护栏拦，
                # 留个重发按钮只会误导坐席连点）
                raise _send_blocked_exc(
                    request, platform, account_id, chat_key,
                    reason=str(result.get("blocked") or ""))
            _fail_raw, _fail_kind = _undelivered_reason(result, platform, account_id)
            _trace_failed_manual_send(
                request, platform, account_id, chat_key, text,
                _fail_kind or _fail_raw)
            _log_send_fail(platform, account_id, chat_key, _fail_raw, _fail_kind)
            raise HTTPException(502, tr(
                request, "err.inbox.send_not_delivered",
                msg=_humanize_send_failure(
                    request, _fail_raw, _fail_kind)))
        cid = (result.get("conversation_id") if isinstance(result, dict) else None) \
            or _conv_id(platform, account_id, chat_key)
        _mark_send(cid)
        # #177（J-1 交 J-3 接线）：人工替人设说的自述事实进 AI 记忆——best-effort，
        # 模块内吞异常、零阻断发送；original_text=坐席原文（抽事实），text=实际发出
        # （出站翻译后）的文本（更新 last_reply 防复读，按客户看到的那份）。
        from src.inbox.human_outbound_memory import record_human_outbound
        record_human_outbound(
            request.app.state, platform, account_id, chat_key, original_text,
            conversation_id=cid, sent_text=text)
        # #72 终局：sent（前端超时后对账按成功收尾，绝不诱导重发）
        if _client_msg_id:
            _txt_tracker.record_sent(_track_scope, _client_msg_id, payload={
                "message_id": str((result or {}).get("message_id") or "")
                if isinstance(result, dict) else "",
            })
        # #73：手动链真实送达=可达铁证 → 清 dead-peer 陈旧标（正是 UDEKBY 实锤
        # 场景：05:47 手动送达成功，blocked 标却继续让自动链永久跳过该客户）。
        try:
            from src.ops.dead_peer_registry import clear_on_delivery
            if clear_on_delivery(platform, chat_key):
                logger.info(
                    "[send] 送达成功，已解除 %s 的 dead-peer 标（自动回复恢复）",
                    _conv_id(platform, account_id, chat_key))
        except Exception:
            pass
        # P1：发生发送前翻译时，旁路记录「实发译文 → 中文原文/质量」，供 /thread 富集出向双行
        # （跨刷新/重启/设备持久；不触碰 messages 去重）。best-effort，失败不影响发送。
        # 分条发送时只给**首段**挂完整原文（P1-198 问题1实锤：旧逻辑把整段中文原文
        # 复制到每一条分段下，坐席看到两条气泡挂着同一大段中文，误以为重复双发）。
        # 其余分段不记映射 → thread 富集查无原文 → 不渲染副行；完整原文在相邻首段。
        if translation_info and cid:
            _ibx_xl = _inbox_store(request)
            if _ibx_xl is not None:
                _sent_rows = (
                    _bubble_parts[: (_bubbles_info or {}).get("parts_sent", 0)]
                    if _bubble_parts else [text]
                )
                for _row_idx, _row_text in enumerate(_sent_rows):
                    if _row_idx > 0:
                        break  # 仅首段挂原文，防每条分段重复整段中文
                    try:
                        _ibx_xl.record_outbound_translation(
                            cid, sent_text=_row_text, original_text=original_text,
                            source_lang=source_lang, target_lang=target_lang,
                            provider=str(translation_info.get("provider") or ""),
                            error=str(translation_info.get("error") or ""),
                        )
                    except Exception:
                        logger.debug(
                            "record_outbound_translation 失败（已忽略）", exc_info=True)
        copilot_meta = body.get("copilot_meta")
        if copilot_meta and cid:
            ibx = _inbox_store(request)
            # copilot 采纳记录用坐席原始输入（而非译文），保持「坐席选了哪条草稿」语义。
            _record_copilot_adopt_from_send(
                ibx, cid, _send_agent["agent_id"], original_text, copilot_meta,
            )
        # P1-4：出向翻译漏斗埋点（覆盖率/auto 解析失败率/降级率；best-effort，绝不影响发送）
        try:
            from src.ai.outbound_translation_stats import get_outbound_translation_stats
            _translated = translation_info is not None
            _degraded = bool(
                _translated and (
                    str(translation_info.get("provider") or "").lower() in ("none", "identity")
                    or str(translation_info.get("error") or "")
                )
            )
            _funnel_kw = dict(
                requested=(bool(_raw_target) and not skip_translate),
                is_auto=_is_auto,
                auto_resolved=(bool(target_lang) if _is_auto else None),
                translated=_translated,
                target_lang=target_lang,
                degraded=_degraded,
                failed=_xlate_failed,
            )
            get_outbound_translation_stats().record_send(**_funnel_kw)
            # P3：同口径持久化进按日表，供经理看板按 7/30 日窗读取（跨重启 + 趋势）
            _ibx_fn = _inbox_store(request)
            if _ibx_fn is not None and hasattr(_ibx_fn, "record_outbound_xlate"):
                _ibx_fn.record_outbound_xlate(**_funnel_kw)
        except Exception:
            logger.debug("出向翻译漏斗埋点失败（已忽略）", exc_info=True)
        # B63③：本次发送是对某条「失败留痕」的一键重发（body.resend_of=留痕
        # message_id）→ 成功后把旧痕 failed→resent（前端收起重发按钮，历史仍
        # 如实保留那次失败）。best-effort；mark_message_resent 只接受 failed
        # 行，传错 id / 已改标都是 no-op，绝不影响发送结果。
        _resend_of = str(body.get("resend_of") or "").strip()
        if _resend_of:
            try:
                _ibx_rs = _inbox_store(request)
                if _ibx_rs is not None and hasattr(_ibx_rs, "mark_message_resent"):
                    _ibx_rs.mark_message_resent(_resend_of)
            except Exception:
                logger.debug("[send] 失败留痕改标 resent 失败（已忽略）", exc_info=True)
        # M-2 A1（#232）：把「有没有拿到平台回执」显式交给前端——delivered=True 才闪 ✓，
        # queued（RPA 入队）显「已排队发送」，其余按「已提交」。此前前端只看 ok。
        _delivered = (result.get("delivered") if isinstance(result, dict) else None)
        _queued = bool(isinstance(result, dict) and result.get("queued"))
        return {
            "ok": True,
            "result": result,
            "delivered": (True if _delivered is True else (False if _delivered is False else None)),
            "queued": _queued,
            "original_text": original_text,
            "sent_text": text,
            "translation": translation_info,
            "bubbles": _bubbles_info,
            "quote_applied": bool(
                isinstance(result, dict) and result.get("quote_applied")),
            "consumed_drafts": list(_consumed_drafts),
        }

    @app.post("/api/unified-inbox/send-gate/exempt")
    async def api_unified_inbox_send_gate_exempt(
        request: Request, _=Depends(page_auth),
    ):
        """把当前会话客户加入发送闸门白名单（P1 2026-08-12 管理员直达出路）。

        composer 护栏横幅的「白名单此客户」按钮 → 本端点 → overlay 保注释写入
        ``companion_send_gate.exempt_peers``（热生效，~秒级）。白名单只豁免
        **限额**（quota 道）；Kill-Switch/授权/金丝雀不受影响（急停不许旁路）。

        角色闸：拒 agent/viewer（与账号管理写口同排除法哲学——绝不误伤
        master/admin 与桌面壳 Bearer「主人」，只拦明确的低权限坐席/观察员）。
        """
        try:
            _role = str(request.session.get("role", "") or "")
        except Exception:
            _role = ""
        if _role in ("agent", "viewer"):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        body = await request.json()
        chat_key = str(body.get("chat_key") or "").strip()
        if not chat_key:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="chat_key"))
        cm = getattr(request.app.state, "config_manager", None)
        if cm is None or not hasattr(cm, "set_overlay_flag"):
            raise HTTPException(503, tr(request, "err.inbox.gate_exempt_cfg_na"))
        cfg = getattr(cm, "config", None) or {}
        from src.skills.companion_send_gate import peer_exempt
        peers = [str(x) for x in (
            (cfg.get("companion_send_gate") or {}).get("exempt_peers") or [])]
        if peer_exempt(cfg, chat_key):
            return {"ok": True, "already": True, "count": len(peers)}
        peers.append(chat_key)
        ok, msg = cm.set_overlay_flag("companion_send_gate.exempt_peers", peers)
        if not ok:
            raise HTTPException(500, tr(request, "err.inbox.gate_exempt_failed",
                                        msg=str(msg or "")))
        _agent = _session_agent(request)
        logger.info(
            "[send-gate] 白名单新增 peer=%s platform=%s by=%s（共 %d 条，overlay 已热生效）",
            chat_key, str(body.get("platform") or ""),
            _agent.get("agent_id") or "?", len(peers))
        return {"ok": True, "count": len(peers)}

    @app.post("/api/unified-inbox/send-media")
    async def api_unified_inbox_send_media(request: Request, _=Depends(page_auth)):
        """M6⑥：坐席从收件箱发送媒体（图片/语音/视频/文件）。

        两种请求体（M-3 A #227，2026-09-06）：
        - **裸流**（新前端）：``Content-Type`` 非 multipart，body 就是文件本体，元数据走
          query（platform / account_id / chat_key / caption / name / client_msg_id）。
          边收边落盘，Web 层不解析、不缓冲；``Content-Length`` 一到手就按上限预检并 413
          （带实际大小），不用等文件传完。
        - **multipart**（旧前端 / 脚本 / 测试）：Starlette 先把整个文件卷进临时文件，路由
          再复制一遍——两次落盘、路由在文件传完之前看不到请求。保留兼容，不再演进。
        仅 protocol 账号（编排器接管、在线）支持；其它平台返回 501（走各自 RPA 发送）。
        发送成功后媒体以 /static URL 回写线程，坐席侧立即可见。
        """
        _t0 = time.perf_counter()
        _ctype = str(request.headers.get("content-type") or "").lower()
        _meta_keys = ("platform", "account_id", "chat_key", "caption", "client_msg_id")
        if _ctype.startswith("multipart/"):
            form = await request.form()
            upload = form.get("file")
            meta = {k: str(form.get(k) or "") for k in _meta_keys}
            filename = str(getattr(upload, "filename", "") or "") if upload is not None else ""
            if not meta["chat_key"] or not filename:
                raise HTTPException(400, tr(request, "err.inbox.file_chat_empty"))

            async def _form_chunks():
                while True:
                    c = await upload.read(1024 * 1024)
                    if not c:
                        return
                    yield c

            # multipart 整体长度含表单包裹，不拿来做精确预检；超限由流式闸兜底
            return await _send_media_streamed(
                request, meta, filename, _form_chunks(), declared=0, t0=_t0, mode="multipart")
        q = request.query_params
        meta = {k: str(q.get(k) or "") for k in _meta_keys}
        filename = str(q.get("name") or "")
        if not meta["chat_key"] or not filename:
            raise HTTPException(400, tr(request, "err.inbox.file_chat_empty"))
        try:
            _declared = int(request.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            _declared = 0
        return await _send_media_streamed(
            request, meta, filename, request.stream(), declared=_declared, t0=_t0, mode="stream")

    @app.get("/api/unified-inbox/media-download")
    async def api_unified_inbox_media_download(
            request: Request, ref: str = "", name: str = "",
            _=Depends(page_auth)):
        """P0 2026-08-17：媒体显式下载（Content-Disposition: attachment）。

        ``/static`` 直链的落盘名是 ``out_<acct>_<hex>.ext`` 乱码名、且浏览器
        对图片/PDF 倾向内联预览——坐席「另存给主管」没有可靠入口。本端点把
        ``protocol_media`` 根内的文件以「消息里的原始文件名 + 强制下载」回给
        浏览器。只服务该根内文件：realpath 容纳检查防路径穿越（本机代码根是
        目录联接，realpath 口径与 static_asset_paths 教训对齐）。
        """
        from src.inbox.media_guard import (
            resolve_contained_path_any, safe_download_name)
        from src.integrations.protocol_bridge import (
            protocol_media_roots, static_media_ref_to_path)
        cand = static_media_ref_to_path(str(ref or ""))
        if not cand:
            raise HTTPException(404, tr(request, "err.inbox.media_not_found"))
        path = resolve_contained_path_any(protocol_media_roots(), cand)
        if not path or not os.path.isfile(path):
            raise HTTPException(404, tr(request, "err.inbox.media_not_found"))
        from fastapi.responses import FileResponse
        fname = safe_download_name(name, fallback=os.path.basename(path))
        return FileResponse(
            path, filename=fname, media_type="application/octet-stream")

    @app.post("/api/unified-inbox/send-voice")
    async def api_unified_inbox_send_voice(request: Request, _=Depends(page_auth)):
        """坐席发送语音回复：回复文本 → （可声音克隆）TTS 合成 → 作为语音消息发送。

        Body: { platform, account_id, chat_key, text, persona_id?, caption?, voice_cfg_override? }
        声音克隆复用 voice_profile（telegram.voice_reply / personas.*.voice_profile，
        backend=voice_clone_command/coqui_http 等）；合成后转 OGG/Opus 以"语音消息"形态发出。
        仅 protocol 账号（编排器接管、在线）支持；其它平台返回 501（走各自 RPA voice_output）。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        text = str(body.get("text") or "").strip()
        persona_id = body.get("persona_id") or None
        caption = str(body.get("caption") or "")
        cfg_override = body.get("voice_cfg_override")
        if not chat_key or not text:
            raise HTTPException(400, tr(request, "err.inbox.chat_text_empty"))
        if len(text) > 1000:
            raise HTTPException(400, tr(request, "err.inbox.text_too_long_voice"))
        # 能力权限 + 坐席额度闸（2026-08-16）：选点在 ``_dedup.reserve`` **之前**
        # ——此时幂等键尚未占位、对账表尚未登记，被拦零清理（拦在 reserve 之后
        # 就得补 release + record_failed 两处收尾）；同时天然拦在 TTS 合成之前，
        # 额度耗尽不再烧 GPU。enforce 默认关=软提醒先行，fail-open 在函数内。
        _deny_capability(request, "chat.send_voice")
        _aq = check_request_quota(request)
        if not _aq["allowed"]:
            # X-AITR-Quota：quotawall v2 机器可读头（特性探测，旧前端零影响）
            raise HTTPException(403, tr(
                request, "err.quota.agent_chars_exhausted",
                used=_aq["used"], quota=_aq["quota"]),
                headers={"X-AITR-Quota": "agent_chars"})
        _raise_if_account_blocked(request, platform, account_id)

        # P0 幂等键（与文本 send 同口径；语音双发还烧双份 TTS/GPU，更值得拦）
        from src.inbox.send_dedup import get_send_dedup
        _client_msg_id = str(body.get("client_msg_id") or "").strip()
        _dedup = get_send_dedup()
        _dedup_scope = f"voice:{platform}:{account_id}:{chat_key}"
        if not _dedup.reserve(_dedup_scope, _client_msg_id):
            logger.info(
                "[send-voice] 幂等去重命中，拒绝重复发送 scope=%s id=%s",
                _dedup_scope, _client_msg_id[:16])
            return {"ok": True, "duplicate": True}

        # P1-1 对账登记（2026-08-11）：前端超时 ≠ 发送失败——服务端可能仍在合成
        # 并最终发出。每个阶段/终局落进程级登记表，配套 send-voice-status 端点，
        # 前端超时后先对账再定论，从流程上消灭「盲目重试→客户收两条」。
        import time as _time
        from src.inbox import voice_send_tracker as _vst
        _vst.record_start(_dedup_scope, _client_msg_id)
        _t_route0 = _time.monotonic()
        _synth_ms = 0
        _conv_ms = 0

        from src.integrations.account_orchestrator import get_orchestrator
        orch = get_orchestrator()
        _cap = media_capability_or_none(orch, platform, account_id)
        if not _cap.get("owns"):
            _dedup.release(_dedup_scope, _client_msg_id)
            _vst.record_failed(_dedup_scope, _client_msg_id,
                               f"voice_unsupported:{_cap.get('reason') or 'no_worker'}")
            raise _media_unsupported_exc(request, _cap, platform=platform, kind="voice")
        # 护栏预检（P0 2026-08-12）：拦在 TTS 合成之前——额度已满还烧一次
        # GPU 克隆纯属浪费；对账表记 send_blocked（超时对账端点不再谎报 sent）。
        _gate_ex = _send_gate_exc(
            request, platform, account_id, chat_key, owned=True)
        if _gate_ex is not None:
            _dedup.release(_dedup_scope, _client_msg_id)
            _vst.record_failed(_dedup_scope, _client_msg_id, "send_blocked")
            raise _gate_ex

        # P1-3：合成期给客户挂「正在录音」气泡（TG/WA 支持；LINE/Messenger 协议
        # 无此能力，orch 内部自会返 False）。一次 action ~5s 过期：合成前 + 合成后
        # 各挂一次覆盖交互链常规时长，不引入常驻任务生命周期；best-effort 绝不阻塞。
        _fire_voice_recording_action(orch, platform, account_id, chat_key)

        # 语音出站断档台账（2026-08-05 P1）：坐席手动链此前不记账 → 手动失败对
        # watchdog._check_voice_outage 不可见（自动链低流量时，坐席连续手动失败
        # 本是最早的断档信号却进不了告警窗）。只记「已决定发语音」之后的最终成败；
        # 能力缺失(501)/配额(402)/幂等重复/语言路由拒发属策略早退，不记。
        def _vo_record(ok: bool, reason: str = "") -> None:
            try:
                from src.ai.voice_outage import get_voice_outage
                get_voice_outage().record_voice_attempt(ok, "manual", reason)
            except Exception:
                pass

        # P0-4/C3：字符额度用尽且 licensing.enforce 开 → 402 + i18n（明确错误码，
        # 而非让 TTS 深处报一串英文 code）。闸门在 TTSPipeline 内还有兜底。
        from src.licensing.quota_store import check_license_quota
        if not check_license_quota()["allowed"]:
            _vst.record_failed(_dedup_scope, _client_msg_id, "quota_exhausted")
            raise HTTPException(402, tr(request, "err.lic.chars_exhausted"),
                                headers={"X-AITR-Quota": "license_chars"})

        # ── P0-V2 译声（2026-08-19）：显式 target_lang（'auto'=会话客户语言）→
        #    先译后念（打中文、发目标语克隆声）。fail-open：翻译失败/identity 念
        #    原文并如实标记；spoken_text 决定音色语言路由/合成/质量闸/收件箱镜像，
        #    试听复用契约带语言维度（试听日语后切韩语发送绝不复用日语音频）。
        _vt_target = str(body.get("target_lang") or "").strip()
        if _vt_target:
            try:
                from src.ai.translation_service import normalize_lang as _nl
                if _vt_target.lower() == "auto":
                    _vt_target = _resolve_conv_language(
                        request, platform, account_id, chat_key)
                _vt_target = (_nl(_vt_target) or "").lower()
            except Exception:
                _vt_target = ""
            if _vt_target == "unknown":
                _vt_target = ""
        spoken_text = text
        _vxl = {"translated": False, "target_lang": "", "provider": ""}

        # ── 所听即所发（P1 2026-08-05）：优先复用坐席刚试听过的产物 ──────────
        # 试听与发送此前是两次独立合成——坐席听到 A 声、客户可能收到 B 声（克隆
        # 链中途恢复/掉线时 provider 漂移），同一句话还烧两份 TTS/字符额度。
        # 前端把 tts-test 返回的 filename 随发送带回；校验（同文本指纹/同音色键/
        # 未过期/sidecar 完整）通过 → 试听音频直接进出站管线：听到什么发什么。
        # 任何校验不过一律回落现场合成，复用簿记绝不新增发送失败面。
        # 质量闸门在复用分支刻意跳过：坐席的耳朵在试听时已把关，比时长启发强。
        _reuse = None
        _preview_fn = str(body.get("preview_filename") or "").strip()
        if _preview_fn:
            try:
                from src.integrations.shared.tts_preview import (
                    resolve_reusable_preview)
                _reuse, _rwhy = resolve_reusable_preview(
                    _preview_fn, text=text, persona_key=str(persona_id or ""),
                    target_lang=_vt_target)
                if _reuse is None:
                    logger.info(
                        "[inbox/voice-send] 试听复用未命中(%s) fn=%s → 现场合成",
                        _rwhy, _preview_fn[:64])
            except Exception:
                _reuse = None
                logger.debug("[inbox/voice-send] 试听复用解析异常（回落合成）",
                             exc_info=True)
        if _reuse is not None:
            # 复制后再消费：出站管线会转码并删除源文件；直接喂原件会让「发送
            # 中途失败 → 重试」时试听产物已被烧掉（回落合成能兜底，但白丢复用）。
            import shutil
            import tempfile as _tf
            import uuid as _uuid
            from pathlib import Path as _P
            from types import SimpleNamespace as _NS
            _meta = dict(_reuse.get("meta") or {})
            try:
                _wdir = _P(_tf.gettempdir()) / "unified_voice_send"
                _wdir.mkdir(parents=True, exist_ok=True)
                _work = _wdir / (
                    f"reuse-{_uuid.uuid4().hex[:8]}{_reuse['path'].suffix}")
                shutil.copy2(_reuse["path"], _work)
            except Exception:
                _reuse = None
                logger.debug("[inbox/voice-send] 试听产物复制失败（回落合成）",
                             exc_info=True)
            else:
                # 坐席字符计量：复用试听产物分支**不记账**——这份音频在 tts-test
                # 时已计过，这里再记＝同一段文字双计（伪 result.ok=True 勿当真合成）。
                voice_ctx = {
                    "persona_id": _meta.get("resolved_persona_id") or "",
                    "persona_source": "preview_reuse",
                    "emotion": (_NS(emotion=str(_meta.get("emotion") or ""))
                                if _meta.get("emotion") else None),
                }
                result = _NS(
                    ok=True, audio_path=str(_work), error="",
                    provider=str(_meta.get("provider") or ""),
                    voice=str(_meta.get("voice") or ""),
                    duration_sec=float(_meta.get("duration_sec") or -1.0),
                    latency_ms=0,
                    format=str(_meta.get("format") or ""),
                    extra={"fallback_from": str(_meta.get("fallback_from") or ""),
                           "reused_preview": True},
                )
                # 译声复用：sidecar 里的 spoken_text=试听时真实念出的译稿——
                # 收件箱镜像必须写客户实际听到的话，而非坐席手打的原文。
                if _meta.get("spoken_text"):
                    spoken_text = str(_meta.get("spoken_text") or "") or text
                    _vxl = {"translated": True, "target_lang": _vt_target,
                            "provider": str(_meta.get("xl_provider") or "")}
        if _reuse is None:
            # P0-V2 译声：现场合成路径先译后念（与 tts-test 同用 resolve_spoken_text，
            # 试听=发送同入参 ⇒ 同 spoken 文本；翻译服务缓存让二次调用零成本）。
            if _vt_target:
                from src.ai.voice_outbound_xlate import resolve_spoken_text
                spoken_text, _vxl = await resolve_spoken_text(
                    text, _vt_target, _get_translation_service(request))
                if _vxl.get("translated"):
                    # 译声=顺带消费一次翻译，按源文本长度记（与文本出站同口径）
                    record_request_chars(request, "translation", len(text))
            # 解析语音配置（含声音克隆 voice_profile），允许调用方临时覆盖
            cm = getattr(request.app.state, "config_manager", None)
            raw_cfg = (getattr(cm, "config", None) or {}) if cm else {}
            # 账号默认人设（2026-08-05 P0，与 A 线 sender._acc_pid / tts-test 同口径）：
            # 此前缺这一参，chat 未绑定人设时回落到域默认而非账号人设——与自动语音
            # 链解析结果分叉（同一会话，AI 自动发一种声、坐席手动发另一种声）。
            from src.ai.persona_voice import (
                resolve_account_persona_id, resolve_effective_voice_context)
            _acc_pid = ""
            try:
                _acc_pid = resolve_account_persona_id(
                    raw_cfg, platform, account_id) or ""
            except Exception:
                _acc_pid = ""
            # text=spoken_text：音色的语言路由跟**实际要念的文本**走（译声场景
            # zh 原文 → ja 译文，克隆语言/edge 音色都该按 ja 解析）。
            voice_ctx = resolve_effective_voice_context(
                raw_cfg, persona_id=persona_id, chat_key=chat_key or None,
                account_persona_id=_acc_pid or None,
                contact_key=chat_key or None, platform=platform,
                account_id=account_id, text=spoken_text)
            # 选声金标（#149）：与 tts-test 同一判据——坐席显式选的人设必须就是
            # 解析结果，否则拒发（宁可不发，不发别人的声）。
            try:
                from src.ai.persona_voice import check_voice_selection
                _sel_bad = check_voice_selection(persona_id, voice_ctx)
            except Exception:
                _sel_bad = ""
            if _sel_bad:
                logger.error(
                    "[inbox/voice-send] #149 选声失配 reason=%s requested=%s "
                    "resolved=%s → 拒发", _sel_bad, persona_id,
                    voice_ctx.get("persona_id") or "-")
                _dedup.release(_dedup_scope, _client_msg_id)
                _vst.record_failed(
                    _dedup_scope, _client_msg_id, f"voice_selection:{_sel_bad}")
                return {"ok": False, "reason": f"voice_selection:{_sel_bad}",
                        "requested_persona_id": str(persona_id or ""),
                        "resolved_persona_id": str(voice_ctx.get("persona_id") or ""),
                        "message": (
                            tr(request, "err.voice.persona_not_found",
                               persona_id=str(persona_id or ""))
                            if _sel_bad == "persona_not_found"
                            else tr(request, "err.voice.selection_mismatch",
                                    persona_id=str(persona_id or ""),
                                    resolved=str(voice_ctx.get("persona_id") or "-")))}
            voice_cfg = voice_ctx.get("voice_cfg") or {}
            _explicit_voice_override = isinstance(cfg_override, dict) and any(
                cfg_override.get(k) for k in ("voice", "backend", "voice_profile"))
            if isinstance(cfg_override, dict):
                voice_cfg.update({k: v for k, v in cfg_override.items() if v not in (None, "")})
            # 语言路由（与自动语音同口径）：edge 音色对齐文本语种，防止手动语音也踩
            # 「ja 音色念中文」类错配；坐席显式覆写 voice/backend 时尊重人工选择不路由。
            # 语种明确但无音色映射 → 明确报错回落（发错语言的语音比不发更糟）。
            if not _explicit_voice_override:
                try:
                    from src.ai.lang_voice_route import (
                        is_reject_tag, route_voice_cfg_for_text)
                    voice_cfg, _lang_route = route_voice_cfg_for_text(
                        voice_cfg, spoken_text, raw_cfg)
                    if is_reject_tag(_lang_route):
                        _vst.record_failed(
                            _dedup_scope, _client_msg_id, "lang_mismatch")
                        return {"ok": False, "reason": "lang_mismatch",
                                "message": tr(request, "err.inbox.voice_lang_mismatch",
                                              lang=_lang_route.split(":", 1)[-1])}
                except Exception:
                    logger.debug("[inbox/voice-send] 语言路由异常（忽略）", exc_info=True)
            voice_cfg["enabled"] = True

            # 合成到临时目录
            import tempfile
            from pathlib import Path as _Path
            out_dir = _Path(tempfile.gettempdir()) / "unified_voice_send"
            voice_cfg["out_dir"] = str(out_dir)
            from src.ai.tts_pipeline import TTSPipeline
            try:
                tts = TTSPipeline(voice_cfg)
                # 原文直念（P0 2026-08-10）：手打文字＝坐席的最终意图，整段跳过
                # 口语化改写链（实录：LLM 档把「你晚上吃饭了吗…」改写成**对它的
                # 回答**后念出，气泡 caption=原文、音频=另一句话）。副语言标记/
                # 情绪/变速照常。interactive=True 把 hub 候选数封顶 1（人在等，
                # synth_verify 已兜坏 take）；total_budget_sec 与 tts-test 同口径
                # （#59 起同走 clone_budget_sec 按字数动态：0.45s/字+10s，≤77 字
                # 与旧 45s 等值——「试听得出来、发送发不出」是契约破口）。前端超
                # 时另有 send-voice-status 对账三态，预算放宽不改变那套语义。
                from src.integrations.shared.tts_preview import clone_budget_sec
                _t_synth0 = _time.monotonic()
                _budget = clone_budget_sec(spoken_text)
                result = await tts.synthesize(
                    spoken_text, timeout_sec=_budget, emotion=voice_ctx.get("emotion"),
                    pre_colloquialized=True, interactive=True,
                    total_budget_sec=_budget)
                _synth_ms = int((_time.monotonic() - _t_synth0) * 1000)
            except Exception as ex:  # noqa: BLE001
                _dedup.release(_dedup_scope, _client_msg_id)
                _vo_record(False, f"tts_exception:{type(ex).__name__}")
                _vst.record_failed(_dedup_scope, _client_msg_id,
                                   f"tts_exception:{type(ex).__name__}")
                raise HTTPException(502, tr(request, "err.inbox.tts_failed", err=ex))
            if not result.ok or not result.audio_path:
                _dedup.release(_dedup_scope, _client_msg_id)
                _vo_record(False, str(getattr(result, "error", "") or "tts_failed"))
                _vst.record_failed(_dedup_scope, _client_msg_id,
                                   str(getattr(result, "error", "") or "tts_failed"))
                # 坐席链此前只有 debug 日志：合成失败在 app.log 里查不到原因（全天零记录），
                # 排障只能靠坐席口述。失败/成功各一行 INFO 是后续所有诊断的地基。
                logger.warning(
                    "[inbox/voice-send] 合成失败 platform=%s acct=%s pid=%s "
                    "len=%d provider=%s err=%s",
                    platform, account_id, voice_ctx.get("persona_id") or "",
                    len(text), getattr(result, "provider", "") or "",
                    getattr(result, "error", "") or "unknown")
                # 可行动分类（2026-08-05 P0）：hub 音色源不可用 / 音色登记不完整
                # 这两类失败给坐席「下一步做什么」的人话（换系统通用音色/重新登记），
                # 其余保持原始错误码口径。reason 字段维持机器可读。
                _err = result.error or "tts_failed"
                _msg = ""
                try:
                    from src.ai.tts_pipeline import classify_voice_error
                    _cls = classify_voice_error(_err)
                    if _cls == "hub_source_down":
                        _msg = tr(request, "err.voice.hub_source_down")
                    elif _cls == "profile_not_ready":
                        _msg = tr(request, "err.voice.profile_not_ready")
                except Exception:
                    _msg = ""
                return {"ok": False, "reason": _err,
                        "message": _msg or tr(request, "err.inbox.tts_failed",
                                              err=_err)}

            # 截断/坏音质量闸门（与 A 线 voice_reply、B 线 autosend 同口径）：时长低于
            # 该文本的物理最快语速 = 半截杂音，宁缺毋滥不发给客户（坐席可改文案重试）。
            try:
                from src.ai.tts_quality import looks_truncated, resolve_quality_gate
                _qg = resolve_quality_gate(voice_cfg)
                _dur = float(getattr(result, "duration_sec", 0.0) or 0.0)
                if _qg["enabled"]:
                    _bad, _why = looks_truncated(
                        spoken_text, _dur,
                        min_sec_per_unit=_qg["min_sec_per_unit"],
                        min_units=_qg["min_units"])
                    if _bad:
                        logger.warning(
                            "[inbox/voice-send] 疑似截断坏音(%s) provider=%s dur=%.1fs "
                            "→ 拒发 platform=%s acct=%s",
                            _why, getattr(result, "provider", "") or "", _dur,
                            platform, account_id)
                        try:
                            from src.ai.avatar_voice_stats import get_avatar_voice_stats
                            get_avatar_voice_stats().record_truncation_reject()
                        except Exception:
                            pass
                        try:
                            os.remove(result.audio_path)
                        except Exception:
                            pass
                        _vo_record(False, f"truncated:{_why}")
                        _vst.record_failed(_dedup_scope, _client_msg_id,
                                           f"truncated:{_why}")
                        # 人话+下一步（P0-3 2026-08-31）：截断是克隆链单次方差的
                        # 常见形态，裸码「truncated:...」坐席不知道该干什么。
                        return {"ok": False, "reason": "truncated",
                                "message": tr(request, "err.voice.truncated")}
            except Exception:
                logger.debug("[inbox/voice-send] 质量闸门异常（忽略）", exc_info=True)

            # #93（2026-09-01）：「应克隆未克隆」显式化（与 tts-test 同口径）
            # ——静默换声是穿帮事故，必须让前端黄条 + 降级发送确认亮起来。
            try:
                if not _explicit_voice_override:
                    from src.ai.persona_voice import CLONE_BACKENDS as _CLB
                    _vp_exp = voice_cfg.get("voice_profile") \
                        if isinstance(voice_cfg.get("voice_profile"), dict) else {}
                    _exp_backend = str(
                        (_vp_exp.get("backend") if _vp_exp.get("enabled")
                         else None)
                        or voice_cfg.get("backend") or "").strip().lower()
                    _got = str(getattr(result, "provider", "") or "").strip().lower()
                    _rex = getattr(result, "extra", {}) or {}
                    if (_exp_backend in _CLB and _got and _got not in _CLB
                            and not _rex.get("fallback_from")):
                        _rex = dict(_rex)
                        _rex["fallback_from"] = _exp_backend
                        _rex.setdefault("primary_error", "clone_not_engaged")
                        result.extra = _rex
                        logger.warning(
                            "[inbox/voice-send] #93 应克隆未克隆已补标："
                            "expected=%s got=%s persona=%s",
                            _exp_backend, _got,
                            voice_ctx.get("persona_id") or "-")
            except Exception:
                logger.debug("[inbox/voice-send] #93 补标失败（忽略）",
                             exc_info=True)

            # 坐席字符计量归因（2026-08-16）：**真合成**成功（过质量闸门）才记 tts
            # 字符；上方复用试听产物分支刻意跳过不记账——已在 tts-test 计过，重复
            # 记＝双计。按实际合成文本（译声=译文）计，与授权池同口径。
            record_request_chars(request, "tts", len(spoken_text))

        # 转 OGG/Opus，使其在 Telegram/WhatsApp 呈现为"语音消息"（ffmpeg 缺失则原样发）
        _vst.record_stage(_dedup_scope, _client_msg_id, "convert")
        _fire_voice_recording_action(orch, platform, account_id, chat_key)
        _t_conv0 = _time.monotonic()
        audio_path = result.audio_path
        try:
            from src.client.voice_sender import convert_to_ogg_opus
            converted = await asyncio.to_thread(convert_to_ogg_opus, audio_path, delete_src=True)
            if converted:
                audio_path = converted
        except Exception:
            logger.debug("OGG 转码失败，按原格式发送", exc_info=True)
        _conv_ms = int((_time.monotonic() - _t_conv0) * 1000)

        # 落到出站媒体目录（线程回写可见）+ 发送（强制 media_type=voice）
        try:
            with open(audio_path, "rb") as fh:
                data = fh.read()
            from src.integrations.protocol_bridge import save_outbound_media
            local, url, _mt = save_outbound_media(
                platform, account_id, os.path.basename(audio_path), data)
        except Exception as ex:  # noqa: BLE001
            _vo_record(False, "save_failed")
            _vst.record_failed(_dedup_scope, _client_msg_id, "save_failed")
            raise HTTPException(502, tr(request, "err.inbox.voice_save_failed", err=ex))
        finally:
            try:
                os.remove(audio_path)
            except Exception:
                pass

        _vst.record_stage(_dedup_scope, _client_msg_id, "send")
        _send_agent = _session_agent(request)
        # P1-3：镜像行带「谁的音色」（人设显示名，best-effort 空串安全）——坐席手动
        # 选别的人设音色发语音时，气泡徽标能看出「这条不是会话绑定人设的声音」。
        from src.ai.persona_voice import persona_display_name
        _voice_sender_name = persona_display_name(voice_ctx.get("persona_id"))
        _t_send0 = _time.monotonic()
        try:
            try:
                # inbox_text=spoken_text：镜像写客户**实际听到的话**（译声=译文），
                # 「音频念 A、气泡写 B」是 2026-08-10 事故的同款穿帮机制。
                res = await orch.send_media(
                    platform, account_id, chat_key,
                    media_path=local, media_url=url, media_type="voice",
                    caption=caption, inbox_text=spoken_text,
                    sender_name=_voice_sender_name, origin="manual")
            except TypeError:
                # 旧签名（无 origin kwarg，测试假编排器常见）→ 回落
                res = await orch.send_media(
                    platform, account_id, chat_key,
                    media_path=local, media_url=url, media_type="voice",
                    caption=caption, inbox_text=spoken_text,
                    sender_name=_voice_sender_name)
        except Exception as ex:  # noqa: BLE001
            _dedup.release(_dedup_scope, _client_msg_id)  # 失败释放：重试可再发
            _vo_record(False, "deliver_failed")
            _vst.record_failed(_dedup_scope, _client_msg_id, "deliver_failed")
            _hkey = humanize_send_err_key(ex)
            if _hkey:
                logger.warning("[send-voice] 投递被平台拒绝（%s）: %s",
                               _hkey, ex)
                raise HTTPException(502, tr(request, _hkey))
            # 截断兜底（2026-08-20 工单 #3 实录）：pyrogram 上传异常的 repr 可能
            # 内嵌整段 WAV 字节流（<Queue … bytes=b'RIFF…'>），err=ex 直塞模板会把
            # 满屏乱码喷给用户。全文进日志，展示层只留可读前缀。
            logger.warning("[send-voice] 投递失败: %r", ex)
            raise HTTPException(502, tr(
                request, "err.inbox.voice_send_failed",
                err=str(ex)[:160]))
        # P0 2026-08-12：拦截/未送达显式回执——此前被拦仍 _vo_record(True) +
        # record_sent，超时对账端点会对坐席谎报「已发出」。
        _undeliv = _result_undelivered(res)
        if _undeliv:
            _dedup.release(_dedup_scope, _client_msg_id)
            _reason_tag = "send_blocked" if _undeliv == "blocked" else "deliver_failed"
            _vo_record(False, _reason_tag)
            _vst.record_failed(_dedup_scope, _client_msg_id, _reason_tag)
            if _undeliv == "blocked":
                raise _send_blocked_exc(
                    request, platform, account_id, chat_key,
                    reason=str(res.get("blocked") or ""))
            _herr = str(res.get("error") or res.get("error_kind") or "")
            _hkey2 = humanize_send_err_key(_herr)
            if _hkey2:
                logger.warning("[send-voice] 未送达（%s）: %s", _hkey2, _herr)
                raise HTTPException(502, tr(request, _hkey2))
            raise HTTPException(502, tr(
                request, "err.inbox.send_not_delivered", msg=_herr))
        _send_ms = int((_time.monotonic() - _t_send0) * 1000)
        _vo_record(True)
        cid = _conv_id(platform, account_id, chat_key)
        # #88：语音送达成功=可达铁证 → 清 dead-peer 陈旧标（与文本路由同口径）
        try:
            from src.ops.dead_peer_registry import clear_on_delivery
            if clear_on_delivery(platform, chat_key):
                logger.info(
                    "[send-voice] 送达成功，已解除 %s 的 dead-peer 标（自动回复恢复）",
                    cid)
        except Exception:
            pass
        try:
            ibx = _inbox_store(request)
            if ibx is not None:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
                # Sprint1 接管即静音：语音发送同属坐席接管，切 manual 停 AI
                # （source=takeover，供横幅/自动接回识别）。
                from src.inbox.takeover_rearm import record_agent_takeover
                record_agent_takeover(ibx, cid)
        except Exception:
            logger.debug("record_agent_send(voice) 失败", exc_info=True)
        _emotion = voice_ctx.get("emotion")
        _extra = getattr(result, "extra", {}) or {}
        # 回落原因归纳（P0 2026-08-31）：与 tts-test 同源（lang_voice_route 单一
        # 归纳口）——语种改道/额度/通道故障分开，前端与观测不再猜。
        try:
            from src.ai.lang_voice_route import fallback_reason_from_extra
            _fb_reason, _fb_lang = fallback_reason_from_extra(_extra)
        except Exception:
            _fb_reason, _fb_lang = "", ""
        voice_meta = {
            "persona_id": voice_ctx.get("persona_id") or "",
            # #149：请求侧所选人设（与 tts-test 同字段，UI/对账可核「所选==所用」）
            "requested_persona_id": str(persona_id or ""),
            "persona_source": voice_ctx.get("persona_source") or "",
            "provider": getattr(result, "provider", ""),
            "voice": getattr(result, "voice", ""),
            "emotion": getattr(_emotion, "emotion", "") if _emotion else "",
            "fallback_from": _extra.get("fallback_from", ""),
            "fallback_reason": _fb_reason,
            "fallback_lang": _fb_lang,
            # P0-V2 译声（additive）：translated=false 时 target_lang 恒空串
            "translated": bool(_vxl.get("translated")),
            "target_lang": (_vxl.get("target_lang") or "") if _vxl.get("translated") else "",
        }
        _vst.record_sent(_dedup_scope, _client_msg_id, {
            "voice_meta": voice_meta,
            "reused_preview": bool(_extra.get("reused_preview")),
        })
        # 成功也留一行 INFO：口径对齐 B 线「[autosend voice] 已发语音 …」，
        # 让坐席这条链在 app.log 里可见（provider/延迟/口语化是否生效一目了然）。
        # P2 分段耗时（2026-08-11）：synth/conv/send 各占多少直接进日志——
        # 「慢在哪」从坐席口述变成一行可查。
        logger.info(
            "[inbox/voice-send] 已发语音 platform=%s acct=%s pid=%s dur=%sms "
            "provider=%s fallback=%s len=%d colloq=%s/%s prerender=%s reuse=%s "
            "xlate=%s stage_ms=synth:%d/conv:%d/send:%d total:%d",
            platform, account_id, voice_meta["persona_id"],
            getattr(result, "latency_ms", 0), voice_meta["provider"] or "-",
            voice_meta["fallback_from"] or "-", len(spoken_text),
            bool(_extra.get("colloquial")), bool(_extra.get("colloquial_llm")),
            voice_meta["provider"] == "prerendered",
            bool(_extra.get("reused_preview")),
            voice_meta["target_lang"] or "-",
            _synth_ms, _conv_ms, _send_ms,
            int((_time.monotonic() - _t_route0) * 1000))
        return {
            "ok": True, "result": res, "media_ref": url, "media_type": "voice",
            "duration_sec": getattr(result, "duration_sec", -1.0),
            "provider": getattr(result, "provider", ""),
            "voice": getattr(result, "voice", ""),
            "voice_meta": voice_meta,
            # 所听即所发：true=客户收到的就是坐席试听的那份音频（零二次合成）
            "reused_preview": bool(_extra.get("reused_preview")),
            # P0-V2 译声（additive）：客户实际听到的话 + 坐席原文
            "voice_translated": bool(_vxl.get("translated")),
            "sent_text": spoken_text,
            "original_text": text,
        }

    @app.get("/api/unified-inbox/send-voice-status")
    async def api_unified_inbox_send_voice_status(
        request: Request, platform: str = "", account_id: str = "default",
        chat_key: str = "", client_msg_id: str = "", _=Depends(page_auth),
    ):
        """语音发送对账端点（P1-1 2026-08-11）：前端超时后先问结果再定论。

        前端 60s 超时 ≠ 发送失败——服务端可能仍在合成并最终发出；坐席按直觉重试
        ＝客户收到两条一样的语音（幂等键拦不住新 client_msg_id）。本端点按
        (platform, account_id, chat_key, client_msg_id) 查进程级登记表：
        ``state``＝in_flight（带 stage: synth|convert|send）/ sent（带 voice_meta）/
        failed（带 reason）/ unknown（老后端/条目过期/从未提交成功——前端按保守
        提示处理）。等待期也可低频轮询本端点把真实阶段渲染给坐席。
        """
        from src.inbox.voice_send_tracker import get_status
        scope = (f"voice:{str(platform or '').lower()}:"
                 f"{str(account_id or 'default')}:{str(chat_key or '')}")
        return {"ok": True, **get_status(scope, str(client_msg_id or ""))}

    @app.get("/api/unified-inbox/send-status")
    async def api_unified_inbox_send_status(
        request: Request, platform: str = "", account_id: str = "default",
        chat_key: str = "", client_msg_id: str = "", _=Depends(page_auth),
    ):
        """#72（0830 UDEKBY 实锤）：文本手动发送对账端点——前端超时先问结果。

        语音链的 send-voice-status 同款三态（sent/failed/in_flight/unknown），
        文本链此前漏配：慢发送（限流 30-65s/条确认）撞前端超时被误报「失败」，
        坐席点「重发」=客户收双条（05:44 报失败、05:47 实际送达铁证）。
        前端契约：超时后先打本端点——sent=按成功收尾；failed=如实报错；
        in_flight/unknown=保守提示「稍后刷新会话确认，勿立即重发」。
        """
        from src.inbox.voice_send_tracker import get_status
        scope = (f"text:{str(platform or '').lower()}:"
                 f"{str(account_id or 'default')}:{str(chat_key or '')}")
        return {"ok": True, **get_status(scope, str(client_msg_id or ""))}

    @app.get("/api/unified-inbox/send-caps")
    async def api_unified_inbox_send_caps(
        request: Request, platform: str = "", account_id: str = "default",
        chat_key: str = "",
    ):
        """返回指定平台/账号是否支持从收件箱直发媒体/语音（protocol 多开且在线）。

        供前端事先置灰媒体/语音按钮 + tooltip 说明，把「点了才撞 501」改为「事先可知」。
        探测失败一律按「不支持」处理（保守，点击仍有 501 兜底）。

        P0-6/C9：``can_media``/``can_voice`` 分开判定（当前同经 worker.send_media 直发、
        口径一致；分字段是为能力分叉时零改契约），另返回 ``voice_mode``：
        - ``composer``  坐席可从输入区直发语音；
        - ``auto_only`` RPA 会话配置了设备端 ``voice_output``——AI 自动语音在手机上发出，
          坐席**不能**在此直发（前端应标注「仅自动语音」而非笼统禁用）；
        - ``none``      无任何语音能力。
        """
        api_auth(request)
        plat = str(platform or "").lower()
        acc = str(account_id or "default")
        can_media = False
        can_voice = False
        caps_reason = ""
        worker_state = ""
        backoff_sec = 0
        try:
            from src.integrations.account_orchestrator import get_orchestrator
            _cap = media_capability_or_none(get_orchestrator(), plat, acc)
            can_media = bool(_cap.get("owns"))
            can_voice = can_media
            if not can_media:
                # #180：按钮为什么灰——no_worker / no_send_media（平台不支持）vs
                # worker_not_running（通道重连中，稍后自己会亮）；前端 tooltip 同源
                caps_reason = str(_cap.get("reason") or "no_worker")
                worker_state = str(_cap.get("state") or "")
                backoff_sec = int(_cap.get("backoff_sec") or 0)
        except Exception:
            logger.debug("send-caps 探测失败（按不支持处理）", exc_info=True)
        # official 账号的诚实能力覆盖：官方 worker 类上恒有 send_media（owns_media
        # 恒 True），但 Zalo 运行时 not_supported、LINE/IG 未配公网 URL 时 no_public_url
        # ——能力位若不按平台分支收敛，坐席就会「按钮能点、点了报错」。
        if can_media:
            try:
                from src.integrations.account_registry import get_account_registry
                row = get_account_registry().get(plat, acc) or {}
                if str(row.get("mode") or "") == "official":
                    from src.integrations.official_api_worker import official_send_caps
                    oc = official_send_caps(plat, getattr(
                        getattr(request.app.state, "config_manager", None),
                        "config", None) or {})
                    can_media = bool(oc.get("can_media"))
                    can_voice = bool(oc.get("can_voice"))
                    caps_reason = str(oc.get("reason") or "")
            except Exception:
                logger.debug("send-caps official 覆盖失败（保持结构判定）", exc_info=True)
        voice_mode = "composer" if can_voice else "none"
        if not can_voice and _rpa_auto_voice_enabled(request, plat, acc):
            voice_mode = "auto_only"
        _caps_cfg = (getattr(getattr(request.app.state, "config_manager", None),
                             "config", None) or {})
        from src.integrations.protocol_bridge import (
            OUT_VIDEO_NATIVE_EXT as _OUT_VIDEO_NATIVE_EXT,
            OUT_VIDEO_TRANSCODE_EXT as _OUT_VIDEO_TRANSCODE_EXT,
            video_transcode_available as _video_transcode_available,
        )
        # P1.5 分条能力探测：配置开 + （非 orch_only 或编排器拥有）才亮气泡 chip。
        # 群聊判定在发送时按 chat_key 再守一道（caps 无 chat_key 维度）。
        _bubbles_on = False
        _bubbles_max = 3
        try:
            from src.inbox.reply_split import parse_bubbles_cfg
            _bc = parse_bubbles_cfg(getattr(
                getattr(request.app.state, "config_manager", None),
                "config", None) or {})
            _bubbles_max = int(_bc["max_parts"])
            # 实施96：按渠道策略封顶（抖音 ≤2、qqbot 1），与发送路由的实际拆条同源
            try:
                from src.inbox.reply_split import cap_max_parts_for_platform
                _bubbles_max = cap_max_parts_for_platform(_bubbles_max, plat)
            except Exception:
                pass
            _owns = False
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                _owns = get_orchestrator().owns(plat, acc)
            except Exception:
                _owns = False
            _bubbles_on = bool(
                _bc["enabled"] and (not _bc["orch_only"] or _owns))
        except Exception:
            _bubbles_on = False
        out = {
            "ok": True, "platform": plat, "account_id": acc,
            "can_media": can_media, "can_voice": can_voice,
            "voice_mode": voice_mode,
            # 官方通道能力受限时的机器可读原因（zalo_api_no_media / needs_public_url），
            # 前端 tooltip 可按码出更具体的解释；空串=无特殊限制。#180 起还会是
            # no_worker / no_send_media / worker_not_running（配 worker_state / backoff_sec）
            "caps_reason": caps_reason,
            "worker_state": worker_state,
            "backoff_sec": backoff_sec,
            "bubbles": _bubbles_on, "bubbles_max_parts": _bubbles_max,
            # P3 2026-08-17：该平台出站媒体体积上限（MB）——前端上传前预检与
            # 服务端 413 同源（_media_cap_mb），不再各写一套 25MB 常量。
            "media_max_mb": _media_cap_mb(_caps_cfg, plat),
            # M-3 A（D-M5）：视频类临时压顶（默认 50MB），与路由 413 同一函数；
            # 前端按文件类别二选一预检。
            "video_max_mb": _media_cap_mb_for(_caps_cfg, plat, "video"),
            # M-3 B（D-M4）：视频容器白名单同源——native 直发；transcode 由后端换封装为
            # MP4（仅 ffmpeg 可用时非空）；两组之外的视频扩展名前端选文件即拒。
            "video_exts": sorted(_OUT_VIDEO_NATIVE_EXT),
            "video_transcode_exts": (sorted(_OUT_VIDEO_TRANSCODE_EXT)
                                     if _video_transcode_available() else []),
            # 裸流上传口（新前端据此走 body=file + query 元数据；旧后端无此键＝仍走 multipart）
            "media_stream_upload": True,
        }
        # 实施96：渠道出站策略快照（禁外链 / 字数上限 / 媒体白名单…）——composer 一贴链接就提示，
        # 与发送口 channel_policy 判定同源；未登记平台不带此键。
        try:
            from src.inbox.channel_policy import snapshot as _cp_snapshot
            _ob = _cp_snapshot(plat, config=_caps_cfg)
            if _ob and not _ob.get("unlimited"):
                out["outbound_policy"] = _ob
        except Exception:
            logger.debug("send-caps outbound_policy 快照跳过", exc_info=True)
        # 实施96：平台回复窗 / 每轮配额快照（抖音 24h·6、TikTok 48h·10…）——前端画倒计时与
        # 「本轮剩余 N 条」，与发送口的 window_guard 判定同源；非窗口平台不带此键。
        if chat_key:
            try:
                from src.inbox.window_guard import snapshot as _wg_snapshot
                _rw = _wg_snapshot(plat, acc, chat_key, store=_inbox_store(request),
                                   config=_caps_cfg)
                if not _rw:
                    # 实施97：微信客服的 48h·5 条由 kf_window_guard 记账（含平台关窗事件），
                    # window_guard 对它让路 → 这里用同一 UI 契约的适配快照，前端信息带零改动
                    from src.inbox.kf_window_guard import ui_snapshot as _kf_ui_snapshot
                    _rw = _kf_ui_snapshot(plat, acc, chat_key, config=_caps_cfg)
                if _rw:
                    out["reply_window"] = _rw
            except Exception:
                logger.debug("send-caps reply_window 快照跳过", exc_info=True)
            # 实施97：微信客服会话在企微侧的接待状态（智能助手/排队/人工 xx/已结束）→ 会话头状态条；
            # 30s 进程内缓存，无 worker 不带此键
            if plat == "wechat_kf":
                try:
                    from src.integrations.wechat_kf_webhook import kf_session_snapshot
                    _ks = await kf_session_snapshot(acc, chat_key)
                    if _ks:
                        out["kf_session"] = _ks
                except Exception:
                    logger.debug("send-caps kf_session 快照跳过", exc_info=True)
        # #73-③（0830 UDEKBY）可见面：该会话 peer 挂着 dead-peer 标时坐席必须
        # 看得见「自动回复已对此人停用+原因」——此前 AI 静默跳过、坐席零感知。
        # chat_key 为可选新参（旧前端不传=响应形状不变）。
        if chat_key:
            try:
                from src.ops.dead_peer_registry import (
                    peek_dead_peer_registry, reconcile_peer_mark)
                _dpr = peek_dead_peer_registry()
                if _dpr is not None and _dpr.is_blocked(plat, chat_key):
                    # #88 二轮（0831 skuio 复测）：报 blocked 之前先 lazy 复核
                    # 「标记后有无成功出站」——启动一次性核销只覆盖 boot 时刻
                    # 已可清的标，之后才满足条件的要等下次重启；打开会话即复核
                    # 把核销时机与重启解耦（精确 conversation_id 等值查询，走索引）。
                    _cleared = False
                    try:
                        _ibx = _inbox_store(request)
                        _db = getattr(_ibx, "_db_path", None)
                        if _db:
                            _cleared = reconcile_peer_mark(
                                _dpr, _db, plat, acc, chat_key, log=logger)
                    except Exception:
                        logger.debug("send-caps dead-peer lazy 核销跳过",
                                     exc_info=True)
                    if not _cleared:
                        _dpi = _dpr.info_of(plat, chat_key) or {}
                        out["dead_peer"] = {
                            "blocked": True,
                            "reason": str(_dpi.get("reason") or ""),
                            "since": float(_dpi.get("first_ts") or 0),
                            "evidence": str(_dpi.get("evidence") or "")[:120],
                        }
            except Exception:
                logger.debug("send-caps dead-peer 探测跳过", exc_info=True)
        # #113 可见面：该会话当前的「临时行程」派生状态（AI 自述扫描 +
        # 清除水位）——运营此前完全看不见「AI 认为自己在出差」这层状态，
        # 更无从纠正。前端特性探测（无此键=旧后端，不渲染徽标）。
        if chat_key:
            try:
                from src.inbox.self_claims import extract_recent_self_statements
                _ibx2 = _inbox_store(request)
                if _ibx2 is not None:
                    _cid = f"{plat}:{acc}:{chat_key}"
                    _cleared_ts = _ibx2.get_travel_cleared_ts(_cid)
                    _hist = []
                    for _m in reversed(
                            _ibx2.list_recent_messages(_cid, limit=40) or []):
                        _hist.append({
                            "role": ("assistant"
                                     if str(_m.get("direction") or "") == "out"
                                     else "user"),
                            "content": str(_m.get("text") or ""),
                            "ts": float(_m.get("ts") or 0),
                        })
                    _tr = next(
                        (s for s in extract_recent_self_statements(
                            _hist, travel_cleared_ts=_cleared_ts)
                         if s.get("kind") == "travel"), None)
                    if _tr:
                        out["travel_state"] = {
                            "active": True,
                            "text": str(_tr.get("text") or "")[:80],
                            "ago_sec": _tr.get("ago_sec"),
                            "anchored": bool(_tr.get("anchored")),
                        }
            except Exception:
                logger.debug("send-caps travel-state 探测跳过", exc_info=True)
        return out
