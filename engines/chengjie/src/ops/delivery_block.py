"""delivery_block — 无兜底纪律的统一失败出口（2026-08-17 老板拍板）。

三原则（docs/实施33 v2）：
① 不要兜底：链路失败＝**不发消息**，绝不发替代品（换声/换引擎/垫场句/原文/装懂）；
② 失败必须响：主机弹窗「AI 回复未发出」（Toast 人话三层文案，2026-08-17 美化）
   + 错误日志上报主机（app.log ERROR）+ webhook 外发；
③ 内容不丢：调用方把没发出去的回复转工作台待发队列（本模块只管上报与计数）。

为什么骑 host_alert 而不是新造事件别名：notify_host 已自带
「ERROR 日志 + EventBus(host_alert) 镜像 → webhook + 算力机 Windows 弹窗 + 同 key 冷却」
全套出口，新增别名要连带 _EVENT_ALIASES/受众目录/格式化器/告警登记四处门禁——
复用现成通道＝零新增告警面、零漂移风险。

用法::

    from src.ops.delivery_block import report_block
    report_block("voice", reason="tts_failed", platform="telegram",
                 conversation_id=cid, detail="hub timeout")

domain 约定（新增先登记在 _KNOWN_DOMAINS，防拼写漂移）：
    chat / voice / translate / vision / asr
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger("ai_chat_assistant.delivery_block")

# 弹窗/外发去抖窗（秒）：同域 10 分钟内只响一次（老板拍板口径）；
# 计数与错误日志**不受**去抖影响——每次失败都留痕。
POPUP_COOLDOWN_SEC = 600.0

_KNOWN_DOMAINS = ("chat", "voice", "translate", "vision", "asr")

_DOMAIN_ZH = {
    "chat": "聊天生成",
    "voice": "语音合成",
    "translate": "出站翻译",
    "vision": "图像理解",
    "asr": "语音转写",
}

# ── 人话文案层（2026-08-17 弹窗美化）────────────────────────────────────
# 错误码原样丢给运营＝逼人来翻代码。三层受众一次成稿：正文给老板/坐席（人话+
# 该干什么），attribution 技术行给工程师（domain:reason·会话·时间全保留）。
# reason 部分拦截点是动态串 → 精确码优先、按域兜底，绝不因缺映射而失声。
_REASON_ZH = {
    ("chat", "cloud_failed_no_fallback"): "云端模型连续两次未返回内容",
    ("voice", "tts_failed"): "语音合成引擎失败或超时",
    ("voice", "synth_failed"): "语音合成引擎失败或超时",
    ("translate", "hold"): "出站翻译引擎不可用",
    ("asr", "enrich_failed"): "未能听清客户的语音",
    ("vision", "enrich_failed"): "未能识别客户发来的图片",
    ("vision", "promise_fulfill_failed"): "承诺的图片未能生成",
}
_DOMAIN_REASON_FALLBACK = {
    "chat": "聊天生成引擎异常",
    "voice": "语音合成引擎异常",
    "translate": "出站翻译引擎异常",
    "asr": "语音转写引擎异常",
    "vision": "识图引擎异常",
}
_DOMAIN_ACTION = {
    "chat": "打开工作台找到该会话，人工回复",
    "voice": "修复语音引擎后到待发队列一键放行（急件可先改发文字）",
    "translate": "修复翻译引擎后到待发队列一键放行",
    "asr": "打开会话听原语音，人工回复",
    "vision": "打开会话看原图，人工回复",
}


def build_popup_copy(domain: str, reason: str, *, conversation_id: str = "",
                     queued_draft: bool = False, recent_n: int = 1,
                     now: Optional[float] = None) -> Dict[str, str]:
    """拦截事件 → 三层文案（纯函数，可测）。

    返回 ``{title, message, attribution}``：
    - title：事件级标题（同链路 10 分钟内多条时带 ×N，弥补弹窗去抖吞信息）；
    - message：三行人话——发生了什么 / 系统兜住了什么 / 你该干什么；
    - attribution：技术码行（工程师排查用，Toast 小字 / webhook·日志缀尾）。
    """
    d = str(domain or "").strip().lower() or "unknown"
    r = str(reason or "").strip() or "unspecified"
    zh = _DOMAIN_ZH.get(d, d)
    n = max(1, int(recent_n or 1))
    title = f"AI 回复未发出｜{zh}" + (f" ×{n}" if n > 1 else "")
    why = _REASON_ZH.get((d, r)) or _DOMAIN_REASON_FALLBACK.get(d) or f"{zh}链路异常"
    kept = ("回复内容已拦下并转入工作台待发队列，修复后可一键放行"
            if queued_draft else "已按无兜底纪律拦下，不会向客户发出替代内容")
    act = _DOMAIN_ACTION.get(d, "请检查对应引擎/端点")
    lines = [f"{why}，该条回复未发出。", f"✅ {kept}。", f"👉 {act}。"]
    if n > 1:
        lines.append(f"（10 分钟内该链路已拦 {n} 条）")
    ts = time.strftime("%H:%M:%S", time.localtime(now if now is not None else time.time()))
    attribution = f"{d}:{r} · 会话 {conversation_id or '-'} · {ts}"
    return {"title": title, "message": "\n".join(lines), "attribution": attribution}


_ws_url_cache: Optional[str] = None


def _workspace_url() -> str:
    """工作台深链接（best-effort，缓存一次）：Toast 点击直达处理入口。

    弹窗只在算力机本机出现 → 127.0.0.1 恰好正确。端口解析顺序：
    ① env AITR_WORKSPACE_URL 整链覆写 ② 实例配置（AITR_CONFIG_PATH 父目录 /
    AITR_DATA_DIR/config / CWD/config——服务进程 CWD=实例数据根，此处相对路径
    是刻意的 C 类用法）读 web_admin.port ③ 全部失败返回空串＝Toast 不带按钮。
    """
    global _ws_url_cache
    if _ws_url_cache is not None:
        return _ws_url_cache
    url = ""
    try:
        import os
        env_url = str(os.environ.get("AITR_WORKSPACE_URL") or "").strip()
        if env_url:
            url = env_url
        else:
            from pathlib import Path
            candidates = []
            _cp = str(os.environ.get("AITR_CONFIG_PATH") or "").strip()
            if _cp:
                candidates.append(Path(_cp).parent)
            _dd = str(os.environ.get("AITR_DATA_DIR") or "").strip()
            if _dd:
                candidates.append(Path(_dd) / "config")
            candidates.append(Path.cwd() / "config")
            for cdir in candidates:
                for name in ("config.local.yaml", "config.yaml"):
                    f = cdir / name
                    if not f.is_file():
                        continue
                    try:
                        import yaml
                        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                        port = int(((data.get("web_admin") or {}).get("port")) or 0)
                        if port > 0:
                            url = f"http://127.0.0.1:{port}/workspace"
                            break
                    except Exception:
                        continue
                if url:
                    break
    except Exception:
        url = ""
    _ws_url_cache = url
    return url

_lock = threading.Lock()
_by_domain: Dict[str, int] = {}
_by_reason: Dict[str, int] = {}
_recent: Deque[Dict[str, Any]] = deque(maxlen=50)
_last_ts: Dict[str, float] = {}


def report_block(
    domain: str,
    *,
    reason: str = "",
    platform: str = "",
    conversation_id: str = "",
    detail: str = "",
    queued_draft: bool = False,
) -> None:
    """上报一次「链路失败 → 消息未发出」。绝不抛异常（上报失败不得二次伤害主链）。

    - 计数 + 环形明细（每次都记）
    - logger.error（主机错误日志，每次都记）
    - notify_host：弹窗 + EventBus(host_alert) → webhook（同域 10min 去抖）
    """
    try:
        d = str(domain or "").strip().lower() or "unknown"
        if d not in _KNOWN_DOMAINS:
            logger.warning("[delivery_block] 未登记的 domain=%r（照记不拒）", d)
        r = str(reason or "").strip() or "unspecified"
        now = time.time()
        with _lock:
            _by_domain[d] = _by_domain.get(d, 0) + 1
            _by_reason[f"{d}:{r}"] = _by_reason.get(f"{d}:{r}", 0) + 1
            _last_ts[d] = now
            _recent.append({
                "ts": now, "domain": d, "reason": r,
                "platform": str(platform or ""),
                "conversation_id": str(conversation_id or ""),
                "queued_draft": bool(queued_draft),
            })
            # 去抖窗内同域累计（含本次）：弹窗 10min 只响一次，后续失败原本全静默
            # ——把计数带进下一次弹窗标题（×N），信息不丢。
            _recent_n = sum(
                1 for ev in _recent
                if ev.get("domain") == d and now - float(ev.get("ts") or 0) <= POPUP_COOLDOWN_SEC
            )
        logger.error(
            "[delivery_block] domain=%s reason=%s conv=%s platform=%s queued=%s%s",
            d, r, conversation_id or "-", platform or "-",
            "Y" if queued_draft else "N",
            f" | {detail}" if detail else "")
        try:
            from src.utils.host_alert import notify_host
            copy = build_popup_copy(
                d, r, conversation_id=str(conversation_id or ""),
                queued_draft=bool(queued_draft), recent_n=_recent_n, now=now)
            notify_host(
                copy["title"], copy["message"],
                key=f"deliv:{d}",
                cooldown_sec=POPUP_COOLDOWN_SEC,
                open_url=_workspace_url(),
                attribution=copy["attribution"],
            )
        except Exception:
            logger.debug("[delivery_block] notify_host 失败", exc_info=True)
    except Exception:
        try:
            logger.debug("[delivery_block] report_block 自身异常", exc_info=True)
        except Exception:
            pass


def snapshot() -> Dict[str, Any]:
    """进程级计数快照（供 metrics/看板）。"""
    with _lock:
        return {
            "by_domain": dict(_by_domain),
            "by_reason": dict(_by_reason),
            "last_ts": dict(_last_ts),
            "recent": list(_recent)[-10:],
            "total": sum(_by_domain.values()),
        }


# 坐席红条只看近窗（默认 30min）：进程累计会把早已修好的旧失败一直挂着。
SEAT_BANNER_MAX_AGE_SEC = 1800.0


def seat_banner(*, now: Optional[float] = None,
                max_age_sec: float = SEAT_BANNER_MAX_AGE_SEC) -> Dict[str, Any]:
    """坐席工作台红条快照：无密钥/无路径，只报近窗内还在响的域。

    B73（实施67 P1-10）：附 ``top_reason``＝近窗最高频拦截原因（人话）——横幅
    只说「已安全拦下」不说为什么，用户与值守都无从下手（钧机 v1.054 实录）。
    """
    snap = snapshot()
    t = float(now if now is not None else time.time())
    last = {
        k: float(v)
        for k, v in (snap.get("last_ts") or {}).items()
        if t - float(v or 0) <= max_age_sec
    }
    recent_evs = [
        ev for ev in (snap.get("recent") or [])
        if t - float(ev.get("ts") or 0) <= max_age_sec
    ]
    recent = [
        {
            "domain": ev.get("domain"),
            "reason": ev.get("reason"),
            "conversation_id": ev.get("conversation_id") or "",
            # queued=True ＝内容已拦下转待发（坐席去放行，橙色语义）；
            # False ＝连稿都没生成（客户在等回复，红色语义）。前端分级渲染用。
            "queued": bool(ev.get("queued_draft")),
        }
        for ev in recent_evs
    ]
    # 近窗最高频 (domain, reason)：环形明细现算（by_reason 是进程累计，会把
    # 修好的旧故障顶成主因）；人话映射复用弹窗同一词表，绝不吐裸错误码。
    top_reason = ""
    top_reason_domain = ""
    top_reason_code = ""
    top_reason_n = 0
    if recent_evs:
        _cnt: Dict[tuple, int] = {}
        for ev in recent_evs:
            _k = (str(ev.get("domain") or ""), str(ev.get("reason") or ""))
            _cnt[_k] = _cnt.get(_k, 0) + 1
        (top_reason_domain, top_reason_code), top_reason_n = max(
            _cnt.items(), key=lambda kv: kv[1])
        top_reason = (_REASON_ZH.get((top_reason_domain, top_reason_code))
                      or _DOMAIN_REASON_FALLBACK.get(top_reason_domain)
                      or top_reason_code or "")
    return {
        "active": bool(last),
        "by_domain": {k: int((snap.get("by_domain") or {}).get(k) or 0) for k in last},
        "recent": recent[-5:],
        "total": int(snap.get("total") or 0),
        "top_reason": top_reason,
        "top_reason_domain": top_reason_domain,
        "top_reason_code": top_reason_code,
        "top_reason_n": int(top_reason_n),
    }


def recent_blocks_for(
    conversation_id: str,
    *,
    max_age_sec: float = SEAT_BANNER_MAX_AGE_SEC,
    now: Optional[float] = None,
) -> list:
    """近窗内**指定会话**的拦截明细（收件箱诊断面板「这条为什么没回」的现场证据）。

    读环形明细（50 条）按会话过滤 + 时窗（默认与坐席红条同 30min 口径）。
    进程重启即清——明细本就是近窗语义，历史口径由消息表/待发队列承载。
    只读；空会话 id / 无匹配 → []。
    """
    cid = str(conversation_id or "")
    if not cid:
        return []
    t = float(now if now is not None else time.time())
    with _lock:
        evs = list(_recent)
    return [
        {
            "ts": float(ev.get("ts") or 0.0),
            "domain": str(ev.get("domain") or ""),
            "reason": str(ev.get("reason") or ""),
            "queued": bool(ev.get("queued_draft")),
        }
        for ev in evs
        if str(ev.get("conversation_id") or "") == cid
        and t - float(ev.get("ts") or 0.0) <= max_age_sec
    ]


def reset_for_tests() -> None:
    with _lock:
        _by_domain.clear()
        _by_reason.clear()
        _recent.clear()
        _last_ts.clear()
