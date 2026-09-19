"""桌面桥 ``kind=voice`` 命令回执（ack）的服务端处置（P0-6，2026-09-19）。

驱动把语音「录」进微信后回 ``/api/desktop/outbound/ack``；此前该端点只改队列状态，于是：
- 收件箱里没有「AI 发了一条语音、念的是什么、谁的音色」的出站行（驱动扫屏镜像只在下一次有
  未读时才发生，且屏上只有「语音 N 秒」占位，念稿靠进程内 TTL 队列充实——驱动一重启就丢）；
- ``record_voice_sent`` 指标不打，语音断档看门狗看不见桥链；
- 驱动因本机能力问题（声卡没就绪 / 媒体拿不到 / 进不了录音态）失败时客户什么都收不到。

本模块把这三件事收口到 ack 一处，**协议直发链 ``send_media(inbox_text=…, sender_name=…)`` 同口径**：

- 成功 → 立刻镜像出站行（direction=out, media_type=voice, media_ref=音频 URL, text=念稿,
  source.sender_name=人设显示名, source.sent_by=ai, source.voice_duration_ms）+ ``record_voice_sent``。
  驱动之后扫到己方语音气泡时按 ``_pending_voice_mirror`` 命中**跳过**，不落第二行。
- 失败且 stage ∈ {record, play}（能力问题、没碰到会话、驱动不冻结）→ 用同一念稿回落一条
  ``kind=text`` 入队（照常过闸门）+ ``record_voice_fallback``；
  语音专属配额拒（``policy:voice_daily_cap`` / ``policy:voice_per_peer_daily_cap``，P2）同样回落文字；
  其他 stage（title/send/echo/cancel＝守卫冻结；其余 policy 拒）→ 只记指标，命令按文字同款进人审。

纯函数 + 注入依赖（队列 / store / 落库函数），可单测。任何异常都吞掉——回执端点绝不因镜像失败 5xx。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: 能力型失败（未进入会话/未点发送）→ 回落文字；其余按原语义（冻结 / 人审）
FALLBACK_STAGES = frozenset({"record", "play"})
#: 语音专属配额拒发（P2）：配额只限「用语音」，不限「回复」本身 → 同稿改发文字。其余 policy 拒
#: （日上限/非工作时段/对方没来过信）文字也一样发不出去，照旧进人审。
FALLBACK_POLICY_REASONS = frozenset({"voice_daily_cap", "voice_per_peer_daily_cap"})
#: record 阶段值得在指标上单列的原因（其余仍归 ``driver_record``）：坐席正在用麦是业务态，不是通路故障
RECORD_REASON_BUCKETS = frozenset({"mic_busy"})
BRIDGE_KIND = "wechat_pc"


def parse_ack_error(error: str) -> Dict[str, str]:
    """``guard:<stage>:<reason>`` / ``policy:<reason>`` / 其他 → ``{kind, stage, reason}``。"""
    e = str(error or "").strip()
    if not e:
        return {"kind": "", "stage": "", "reason": ""}
    parts = e.split(":", 2)
    if parts[0] == "guard":
        return {"kind": "guard", "stage": parts[1] if len(parts) > 1 else "",
                "reason": parts[2] if len(parts) > 2 else ""}
    if parts[0] == "policy":
        return {"kind": "policy", "stage": "policy", "reason": ":".join(parts[1:])}
    return {"kind": "other", "stage": "", "reason": e}


def should_fallback_to_text(error: str) -> bool:
    p = parse_ack_error(error)
    if p["kind"] == "guard":
        return p["stage"] in FALLBACK_STAGES
    return p["kind"] == "policy" and p["reason"] in FALLBACK_POLICY_REASONS


def mirror_voice_out_row(store: Any, item: Dict[str, Any], *, now: float,
                         ingest: Optional[Callable[..., Any]] = None) -> Optional[str]:
    """把已发出的语音以出站行落收件箱；返回 conversation_id（失败 None）。"""
    if store is None:
        return None
    if ingest is None:
        from src.integrations.protocol_bridge import ingest_incoming as ingest
    text = str(item.get("inbox_text") or item.get("text") or "").strip()
    media_ref = str(item.get("media_url") or "").strip()
    src: Dict[str, Any] = {"sent_by": "ai", "bridge": BRIDGE_KIND}
    sender = str(item.get("sender_name") or "").strip()
    if sender:
        src["sender_name"] = sender
    try:
        dur = int(item.get("duration_ms") or 0)
    except (TypeError, ValueError):
        dur = 0
    if dur > 0:
        src["voice_duration_ms"] = dur
    return ingest(
        store,
        platform=str(item.get("platform") or ""),
        account_id=str(item.get("account_id") or ""),
        chat_key=str(item.get("chat_key") or ""),
        text=text,
        ts=float(now),
        msg_id="",
        direction="out",
        media_type="voice",
        media_ref=media_ref,
        source=src,
        sender_name=sender,
    )


def handle_voice_ack(
    queue: Any,
    item: Optional[Dict[str, Any]],
    *,
    ok: bool,
    error: str = "",
    store: Any = None,
    config: Optional[Dict[str, Any]] = None,
    registry: Any = None,
    now: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """ack 已落库之后调用。非 voice 命令 → ``{"handled": False}``。绝不抛。"""
    if not isinstance(item, dict) or str(item.get("kind") or "text").lower() != "voice":
        return {"handled": False}
    import time as _t
    ts = float(now if now is not None else _t.time())
    out: Dict[str, Any] = {"handled": True, "ok": bool(ok)}
    try:
        from src.inbox.voice_autosend import record_voice_fallback, record_voice_sent
    except Exception:  # pragma: no cover - 极端导入失败也不影响回执
        def record_voice_sent(*a, **k):  # type: ignore
            return None

        def record_voice_fallback(*a, **k):  # type: ignore
            return None
    try:
        dur = int(item.get("duration_ms") or 0)
    except (TypeError, ValueError):
        dur = 0
    if ok:
        try:
            record_voice_sent(dur, synth_meta={
                "provider": "desktop_bridge",
                "persona_id": str(item.get("sender_name") or ""),
                "audio_duration_ms": dur,
            })
        except Exception:
            logger.debug("[desktop_voice_ack] 指标记录失败", exc_info=True)
        cid = None
        try:
            cid = mirror_voice_out_row(store, item, now=ts)
        except Exception:
            logger.debug("[desktop_voice_ack] 语音出站行镜像失败", exc_info=True)
        out["mirrored"] = bool(cid)
        out["conversation_id"] = cid or ""
        logger.info("[desktop_voice_ack] 语音已发 id=%s chat=%s dur=%sms echo=%r mirrored=%s",
                    item.get("id"), item.get("chat_key"), dur,
                    str((extra or {}).get("echo") or "")[:24], bool(cid))
        return out
    parsed = parse_ack_error(error)
    reason = ("driver_" + (parsed["stage"] or parsed["kind"] or "unknown"))
    if parsed["kind"] == "policy" and parsed["reason"] in FALLBACK_POLICY_REASONS:
        reason = "driver_" + parsed["reason"]      # driver_voice_daily_cap / driver_voice_per_peer_daily_cap
    elif parsed["kind"] == "guard" and parsed["stage"] == "record":
        # record 阶段细分：mic_busy（坐席在开会/通话，P2-3）单列为 driver_mic_busy，断档台账按此跳过
        head = parsed["reason"].split(":", 1)[0].strip()
        if head in RECORD_REASON_BUCKETS:
            reason = "driver_" + head
    try:
        record_voice_fallback(reason)
    except Exception:
        pass
    out["reason"] = reason
    if not should_fallback_to_text(error):
        logger.warning("[desktop_voice_ack] 语音失败 id=%s chat=%s error=%s（不回落文字，按文字同款处置）",
                       item.get("id"), item.get("chat_key"), error)
        return out
    text = str(item.get("text") or "").strip()
    if not text or queue is None:
        return out
    try:
        res = queue.enqueue(
            str(item.get("platform") or ""), str(item.get("account_id") or ""),
            str(item.get("chat_key") or ""), text, kind="text",
            conversation_id=str(item.get("conversation_id") or ""),
            config=config, registry=registry, now=ts,
            reply_group=str(item.get("reply_group") or ""),
        )
    except Exception:
        logger.debug("[desktop_voice_ack] 文字回落入队异常", exc_info=True)
        res = {"enqueued": False, "blocked": "exception"}
    out["fallback"] = res
    if res.get("enqueued"):
        logger.info("[desktop_voice_ack] 语音失败 id=%s stage=%s reason=%s → 已回落文字 id=%s",
                    item.get("id"), parsed["stage"], parsed["reason"], res.get("id"))
    else:
        logger.warning("[desktop_voice_ack] 语音失败 id=%s stage=%s，文字回落被拦 %s",
                       item.get("id"), parsed["stage"], res.get("blocked"))
    return out


__all__ = ["handle_voice_ack", "mirror_voice_out_row", "parse_ack_error", "should_fallback_to_text",
           "FALLBACK_STAGES", "FALLBACK_POLICY_REASONS", "RECORD_REASON_BUCKETS"]
