"""D1 运行时健康聚合（区别于 P2-1 上线就绪清单）。

上线清单（golive）回答「能不能开张」（一次性配置就绪）；本模块回答「现在跑得健不健康」
（运行时存活/积压/熔断）。把 DB 连通、AI 配置、授权状态、渠道在线、后台 worker 存活、
草稿队列积压聚合成一张运行时红绿灯。

:func:`build_health` 为**纯函数**（入参已是采集好的轻量信号），便于单测；I/O 采集留给路由层。
状态三态：``ok`` / ``warn`` / ``fail``；总体灯：任一 fail→red，无 fail 有 warn→yellow，全 ok→green。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

_PLACEHOLDER = ("your_", "<", "changeme", "xxxx", "请填写", "填写")


def is_placeholder(val: Any) -> bool:
    s = str(val if val is not None else "").strip()
    if not s:
        return True
    low = s.lower()
    return any(t in low for t in _PLACEHOLDER)


def _comp(id_: str, name: str, status: str, detail: str) -> Dict[str, Any]:
    return {"id": id_, "name": name, "status": status, "detail": detail}


def build_health(
    *,
    db_ok: bool,
    ai_provider: str = "",
    ai_key_ok: bool = False,
    license_state: str = "",
    license_read_only: bool = False,
    license_plan: str = "",
    channels_ready: int = 0,
    channels_configured: int = 0,
    channels_total: int = 0,
    workers: Optional[List[Dict[str, Any]]] = None,
    pending_drafts: Optional[int] = None,
    pending_threshold: int = 200,
    audio_service: Optional[Dict[str, Any]] = None,
    avatar_voice: Optional[Dict[str, Any]] = None,
    sla_backlog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """聚合运行时健康（纯函数）。返回 {ok, light, components, summary, ts}。"""
    comps: List[Dict[str, Any]] = []

    # 1) 数据库连通——硬性（挂了整站不可用）
    if db_ok:
        comps.append(_comp("db", "数据库连通", "ok", "SELECT 1 正常"))
    else:
        comps.append(_comp("db", "数据库连通", "fail", "无法访问持久层"))

    # 2) AI 大模型可用——硬性（核心能力）
    if ai_provider and ai_key_ok:
        comps.append(_comp("ai", "AI 大模型", "ok", f"provider={ai_provider}"))
    elif ai_provider:
        comps.append(_comp("ai", "AI 大模型", "fail", "api_key 为空/占位"))
    else:
        comps.append(_comp("ai", "AI 大模型", "fail", "未配置 provider"))

    # 3) 授权状态——expired/invalid 且只读→fail；过期未强制→warn；其余 ok
    st = str(license_state or "")
    if st in ("active", "grace", "unlicensed", "unavailable", "community", ""):
        detail = f"plan={license_plan or 'community'}（{st or 'community'}）"
        comps.append(_comp("license", "授权状态", "ok", detail))
    elif license_read_only:
        comps.append(_comp("license", "授权状态", "fail",
                           f"授权 {st}，系统已降级只读"))
    else:
        comps.append(_comp("license", "授权状态", "warn", f"授权 {st}，请尽快续期"))

    # 4) 渠道在线——软性（纯 web chat / AI 也能跑）
    if channels_ready > 0:
        comps.append(_comp("channels", "消息渠道", "ok",
                           f"{channels_ready}/{channels_total} 渠道就绪"))
    elif channels_configured > 0:
        comps.append(_comp("channels", "消息渠道", "warn", "已配置但无渠道就绪/登录"))
    else:
        comps.append(_comp("channels", "消息渠道", "warn", "尚无渠道接入"))

    # 5) 后台 worker 存活——已挂载但未运行→fail；熔断→warn；运行→ok
    for w in (workers or []):
        name = str(w.get("name") or w.get("id") or "worker")
        wid = str(w.get("id") or name)
        if not w.get("present"):
            continue  # 未启用的 worker 不计入健康
        if not w.get("running"):
            comps.append(_comp(f"worker_{wid}", name, "fail", "已挂载但未运行"))
        elif w.get("circuit_open"):
            le = str(w.get("last_error") or "")
            comps.append(_comp(f"worker_{wid}", name, "warn",
                               f"熔断中{('：' + le) if le else ''}"))
        else:
            comps.append(_comp(f"worker_{wid}", name, "ok", "运行中"))

    # 5.5) LAN GPU 音频服务（ASR/SER）——软性：不可达时链路自动降级本机 CPU 转写，
    # 不算 fail 只亮黄灯提示「在降级跑」。None = 未配置远端音频服务（不出该组件）。
    if audio_service is not None:
        _url = str(audio_service.get("url") or "")
        if not audio_service.get("reachable"):
            _err = str(audio_service.get("error") or "")[:60]
            comps.append(_comp(
                "audio", "语音服务(ASR/SER)", "warn",
                f"GPU 音频服务不可达（已降级 CPU 兜底）{_url}{('：' + _err) if _err else ''}"))
        else:
            _asr_ok = bool(audio_service.get("asr_loaded"))
            _ser_need = bool(audio_service.get("ser_expected"))
            _ser_ok = bool(audio_service.get("ser_loaded"))
            if _asr_ok and (not _ser_need or _ser_ok):
                _lat = audio_service.get("latency_ms")
                comps.append(_comp(
                    "audio", "语音服务(ASR/SER)", "ok",
                    f"在线，模型已装载（{int(_lat)}ms）" if _lat is not None else "在线，模型已装载"))
            else:
                comps.append(_comp(
                    "audio", "语音服务(ASR/SER)", "warn",
                    "在线但模型未装载（预热中或装载失败，首答会慢）"))

    # 5.6) AvatarHub 在线语音（本机 7852 情感克隆）——软性：不可达时语音自动
    # 回落 edge 通用声（不阻塞聊天），只亮黄灯提示「克隆声在降级」。
    # None = avatar_voice 未启用（不出该组件）。7858/远端 STT 不进灯（懒加载/多级兜底
    # 属正常态），三端点明细见 ops-overview AvatarHub 卡。
    if avatar_voice is not None:
        _aurl = str(avatar_voice.get("url") or "")
        if not avatar_voice.get("reachable"):
            _aerr = str(avatar_voice.get("error") or "")[:60]
            comps.append(_comp(
                "avatar_voice", "在线语音(7852克隆)", "warn",
                f"CosyVoice3 不可达（语音已降级 edge 通用声）{_aurl}"
                f"{('：' + _aerr) if _aerr else ''}"))
        elif not avatar_voice.get("models_loaded"):
            comps.append(_comp(
                "avatar_voice", "在线语音(7852克隆)", "warn",
                "在线但模型未载入（语音暂走 edge 兜底）"))
        else:
            _alat = avatar_voice.get("latency_ms")
            comps.append(_comp(
                "avatar_voice", "在线语音(7852克隆)", "ok",
                f"在线，模型已装载（{int(_alat)}ms）" if _alat is not None
                else "在线，模型已装载"))

    # 6) 草稿队列积压——超阈值→warn（提示处理不过来/worker 卡顿）
    if pending_drafts is not None:
        if pending_drafts > pending_threshold:
            comps.append(_comp("queue", "草稿队列", "warn",
                               f"待处理 {pending_drafts} 条（> {pending_threshold}）"))
        else:
            comps.append(_comp("queue", "草稿队列", "ok",
                               f"待处理 {pending_drafts} 条"))

    # 6.5) 草稿 SLA 严重超时——与 6) 的「数量」维度互补的「等待时长」维度。
    # 动机（2026-07-29 实测）：一条 L3 草稿无人认领躺了 6.7 天，队列总数远低于阈值
    # 故 6) 全绿，而 K2 自动再分配只处理「已认领+坐席断线」的、永远跳过无主草稿
    # → 没有任何一盏灯会亮。把 SLAWatcher 的越线快照接进健康灯后，此类积压经
    # health_watchdog 既有的 health_alert 告警链**主动外发**（复用已接通的通道，
    # 而不是指望运营去订阅新事件别名）。None = watcher 未挂载（不出该组件）。
    if sla_backlog is not None:
        _unclaimed = int(sla_backlog.get("unclaimed_escalating") or 0)
        _esc = int(sla_backlog.get("escalating_now") or 0)
        _maxw = int(sla_backlog.get("max_wait_min") or 0)
        _wait_txt = f"最长等待 {_maxw / 60:.0f}h" if _maxw >= 60 else f"最长等待 {_maxw}min"
        if _unclaimed > 0:
            comps.append(_comp(
                "sla_backlog", "草稿 SLA 严重超时", "warn",
                f"{_unclaimed} 条严重超时且**无人认领**（{_wait_txt}）——"
                "自动再分配不覆盖无主草稿，需人工认领处置"))
        elif _esc > 0:
            comps.append(_comp(
                "sla_backlog", "草稿 SLA 严重超时", "warn",
                f"{_esc} 条严重超时（已认领，{_wait_txt}）"))
        else:
            _br = int(sla_backlog.get("breaching_now") or 0)
            comps.append(_comp(
                "sla_backlog", "草稿 SLA 严重超时", "ok",
                f"无严重超时（越线 {_br} 条，{_wait_txt}）" if _br
                else "无越线草稿"))

    fails = sum(1 for c in comps if c["status"] == "fail")
    warns = sum(1 for c in comps if c["status"] == "warn")
    oks = sum(1 for c in comps if c["status"] == "ok")
    light = "red" if fails else ("yellow" if warns else "green")
    return {
        "ok": True,
        "light": light,
        "healthy": fails == 0,
        "components": comps,
        "summary": {"ok": oks, "warn": warns, "fail": fails, "total": len(comps)},
        "ts": time.time(),
    }
