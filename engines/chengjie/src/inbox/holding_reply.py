"""L3 缓冲话术（人审挂起期的「先接住客户」拟人兜底）。

痛点（真实事故）：客户发了敏感/不满消息（如「我生气了啊」→ 不满/投诉）被判 **L3**
（medium 风险，必须人工审批），全自动投递不碰 L3 → 客户被**已读不回甚至完全无响应**，
直到 SLA 告警才被发现（曾积压 2744 分钟）。对陪伴场景，这一段沉默足以让关系崩塌。

本模块补上人审挂起期的「拟人缓冲」：L3 草稿定级后，先给会话**发平台已读回执**（真人先看），
再发**一句安全的通用缓冲话术**（"稍等我看看哈~"），按客户语言、每会话冷却、危机场景跳过。
它**不替代**人工审批（L3 真回复仍等坐席放行），只把「冷冰冰的沉默」变成「对方在看、在想」。

安全设计：
- 缓冲话术是**固定安全短语**（无报价/承诺/敏感内容），绝不触发新的风险；
- **每会话冷却**（默认 30min）：客户连发多条抱怨只安抚一次，不刷「稍等」；
- **危机场景跳过**（severe/elevated 交给专门的危机兜底话术，绝不用泛化「稍等」搪塞）；
- **默认关**（``inbox.l2_autosend.holding.enabled=false``）→ 零行为变更；
- 纯决策（``pick_holding_text`` / ``should_send_holding``）无 IO、可单测。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 内置多语言缓冲话术（安全通用、口语、无承诺）。可被 config 的 templates 覆盖/扩充。
# 键为归一化语言码（zh/en/ja/ko/…）；未命中语言回落 en，再回落 zh。
_DEFAULT_TEMPLATES: Dict[str, List[str]] = {
    "zh": [
        "稍等我一下下哈～",
        "等我看看哦，马上回你",
        "让我想想怎么跟你说～",
        "嗯嗯我在看，给我一点点时间",
    ],
    "en": [
        "Give me a sec, I'll get right back to you 🙏",
        "Hold on a moment, let me check~",
        "Let me think about this for a bit, okay?",
    ],
    "ja": [
        "ちょっと待ってね、すぐ返すね🙏",
        "少し確認するね〜",
    ],
    "ko": [
        "잠깐만 기다려줘, 금방 답할게🙏",
        "조금만 시간 줘~ 확인해볼게",
    ],
}

# 冷却状态（进程内；重启丢失最多多发一次缓冲话术，可接受，无需落库）。
_COOLDOWN: Dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()

# 可观测指标（供 /api/drafts/autosend-status 暴露）。
_METRICS: Dict[str, Any] = {
    "sent": 0, "skipped_cooldown": 0, "skipped_crisis": 0,
    "skipped_disabled": 0, "failed": 0, "last_ts": 0.0, "last_lang": "",
}
_METRICS_LOCK = threading.Lock()

_DEFAULT_COOLDOWN_SEC = 1800.0  # 每会话缓冲话术冷却（30min）


def resolve_holding_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``inbox.l2_autosend.holding`` 块（缺失 → 空 dict → enabled 视为 false）。"""
    try:
        return dict(
            (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get("holding")
            or {}
        )
    except Exception:
        return {}


def pick_holding_text(lang: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """按语言挑一句缓冲话术（config.templates 覆盖内置；随机避免每次同句）。

    语言回落链：精确语言 → en → zh → 内置 zh 首条。空模板列表视为未命中继续回落。
    """
    templates = dict(_DEFAULT_TEMPLATES)
    if isinstance(cfg, dict) and isinstance(cfg.get("templates"), dict):
        for k, v in cfg["templates"].items():
            if isinstance(v, list) and v:
                templates[str(k).strip().lower()] = [str(x) for x in v if str(x).strip()]
    low = str(lang or "").strip().lower().split("-")[0]
    for key in (low, "en", "zh"):
        pool = templates.get(key)
        if pool:
            return random.choice(pool)
    return _DEFAULT_TEMPLATES["zh"][0]


def should_send_holding(
    conv_id: str, *, now: Optional[float] = None,
    cooldown_sec: float = _DEFAULT_COOLDOWN_SEC,
) -> bool:
    """该会话此刻是否允许发缓冲话术（冷却判定，线程安全）。

    在冷却窗口内 → False（不刷屏）；否则 True 并**立即占用冷却**（防并发重复发）。
    """
    if not conv_id:
        return False
    t = time.time() if now is None else now
    with _COOLDOWN_LOCK:
        until = _COOLDOWN.get(conv_id, 0.0)
        if t < until:
            return False
        _COOLDOWN[conv_id] = t + max(0.0, cooldown_sec)
        return True


def reset_cooldown(conv_id: str = "") -> None:
    """测试/运维钩子：清指定会话（空=全部）冷却态。"""
    with _COOLDOWN_LOCK:
        if conv_id:
            _COOLDOWN.pop(conv_id, None)
        else:
            _COOLDOWN.clear()


def _record(metric: str, lang: str = "") -> None:
    with _METRICS_LOCK:
        _METRICS[metric] = int(_METRICS.get(metric, 0)) + 1
        _METRICS["last_ts"] = time.time()
        if lang:
            _METRICS["last_lang"] = lang


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        return dict(_METRICS)


async def maybe_send_holding_reply(
    assistant: Any, platform: str, account_id: str, chat_key: str,
    conv_id: str, *, peer_text: str = "", lang: str = "",
) -> bool:
    """L3 挂起时给会话「先接住」：自动已读 +（可选）一句缓冲话术。返回是否发出了缓冲话术。

    gated（默认关）；仅编排器接管的账号（owns_media）走此路径。危机场景（severe/elevated）
    跳过泛化缓冲（交危机兜底）。任何失败都软降级（记 debug/指标），绝不抛、不阻断主流程。
    """
    _cfg = assistant.config.config or {}
    hb = resolve_holding_cfg(_cfg)
    if not hb.get("enabled"):
        return False
    from src.integrations.account_orchestrator import get_orchestrator as _go
    _orch = _go(_cfg)
    # 与语音/图片同口径：仅对编排器管理的账号发（原生 standalone 不归编排器）
    if not _orch.owns_media(platform, account_id):
        return False

    # 危机跳过：severe/elevated 有专门的危机兜底话术，绝不用「稍等」搪塞
    try:
        if str(peer_text or "").strip():
            from src.utils.wellbeing_guard import detect_crisis as _dc
            if str((_dc(peer_text) or {}).get("level") or "none").lower() in (
                "severe", "elevated"
            ):
                _record("skipped_crisis")
                return False
    except Exception:
        pass

    # 先自动已读（真人先看后回；即便下面不发话术也让客户看到「已读」）——
    # mark_read best-effort，不支持/失败静默。
    try:
        await _orch.mark_read(platform, account_id, str(chat_key))
    except Exception:
        logger.debug("[holding] mark_read 失败 conv=%s", conv_id, exc_info=True)

    # 缓冲话术开关（可只已读不发话术）：send_text=false 时到此为止
    if not bool(hb.get("send_text", True)):
        return False

    try:
        _cd = float(hb.get("cooldown_sec", _DEFAULT_COOLDOWN_SEC) or _DEFAULT_COOLDOWN_SEC)
    except (TypeError, ValueError):
        _cd = _DEFAULT_COOLDOWN_SEC
    if not should_send_holding(conv_id, cooldown_sec=_cd):
        _record("skipped_cooldown")
        return False

    text = pick_holding_text(lang, hb)
    if not text:
        return False

    # 发缓冲话术前的短打字节奏（已读上面已做；这里只补「正在输入 → 稍等」）：
    # 让缓冲话术不再「已读完瞬间蹦出」，与 autosend 共用同一 humanize 协作器。
    # typing_delay_sec=0（默认）→ 无打字延迟，退化为原即时行为。
    import asyncio
    try:
        _typing_delay = float(hb.get("typing_delay_sec", 0.0) or 0.0)
    except (TypeError, ValueError):
        _typing_delay = 0.0
    if _typing_delay > 0:
        try:
            from src.inbox.humanize import run_presend_humanization

            async def _typing(_action):
                async def _tc():
                    return await _orch.send_chat_action(
                        platform, account_id, str(chat_key), "typing")
                _wl2 = getattr(assistant, "_web_loop", None)
                if _wl2 is not None and _wl2.is_running():
                    _tf = asyncio.run_coroutine_threadsafe(_tc(), _wl2)
                    return await asyncio.wrap_future(_tf)
                return await _tc()

            # holding 的 typing_delay 是运营显式配置的「打字 N 秒」意图（非自适应残余），
            # 故 min_typing_delay=0 绕过超短护栏——配了就显示打字。
            await run_presend_humanization(
                delay=_typing_delay, action="typing",
                mark_read=None, typing=_typing, sleep=asyncio.sleep,
                min_typing_delay=0.0)
        except Exception:
            logger.debug("[holding] 打字节奏失败（忽略）", exc_info=True)

    async def _coro():
        return await _orch.send(platform, account_id, chat_key, text)

    try:
        _wl = getattr(assistant, "_web_loop", None)
        if _wl is not None and _wl.is_running():
            _f = asyncio.run_coroutine_threadsafe(_coro(), _wl)
            res = await asyncio.wrap_future(_f)
        else:
            res = await _coro()
    except Exception:
        _record("failed", lang)
        logger.debug("[holding] 缓冲话术发送失败 conv=%s", conv_id, exc_info=True)
        return False
    ok = bool(isinstance(res, dict) and res.get("delivered"))
    if ok:
        _record("sent", lang)
        assistant.logger.info(
            "[holding] L3 缓冲话术已发 conv=%s lang=%s: %s",
            conv_id, lang or "?", text)
    else:
        _record("failed", lang)
    return ok


__all__ = [
    "resolve_holding_cfg",
    "pick_holding_text",
    "should_send_holding",
    "reset_cooldown",
    "metrics_snapshot",
    "maybe_send_holding_reply",
]
