"""D3 健康看门狗 —— 周期巡检 D1 运行时健康，异常时主动告警（EventBus → Webhook）。

设计要点
========
- **复用 D1 检测**：直接调用 :func:`collect_health`（与 ``/api/admin/health`` 同口径），
  不重复造检测逻辑。
- **复用既有投递**：异常时 ``EventBus.publish("health_alert", ...)``，由 ``WebhookNotifier``
  按订阅推送（Telegram/WhatsApp/Messenger/JSON），无需新投递通道。
- **去抖**：仅在「健康签名变化」时告警（如新组件转 fail / 恢复），避免每个巡检周期刷屏；
  WebhookNotifier 自身的 1/小时速率限制是第二层兜底。
- **恢复通知**：从异常恢复到全绿时补发一条「已恢复」，闭环值班体验。

:func:`collect_health` 为采集器（route 与 watchdog 共用）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _collect_workers(state) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    specs = [
        ("autosend", "L2 自动发送 Worker", "autosend_worker"),
        ("autoclaim", "自动认领 Worker", "auto_claim_worker"),
    ]
    for wid, name, attr in specs:
        w = getattr(state, attr, None)
        if w is None:
            out.append({"id": wid, "name": name, "present": False})
            continue
        snap: Dict[str, Any] = {}
        try:
            snap = w.status_snapshot()
        except Exception:
            logger.debug("worker %s 快照失败（已忽略）", attr, exc_info=True)
        out.append({
            "id": wid, "name": name, "present": True,
            "running": bool(snap.get("running")),
            "circuit_open": bool(snap.get("circuit_open")),
            "last_error": snap.get("last_error", ""),
        })
    return out


def _pending_drafts(state) -> Optional[int]:
    svc = getattr(state, "draft_service", None)
    if svc is None or not hasattr(svc, "list_drafts"):
        return None
    try:
        rows = svc.list_drafts(status="pending", limit=1000)
        return len(rows or [])
    except Exception:
        logger.debug("草稿队列统计失败（已忽略）", exc_info=True)
        return None


def audio_probe_target(config: Dict[str, Any]) -> str:
    """决策：该不该探测 LAN GPU 音频服务，探哪个 /health（纯函数）。

    只在 voice_recognition 启用、provider 为 OpenAI 兼容、且 base_url 指向
    **私网主机**（我们自建的 asr176 服务）时返回探测 URL；公网云 ASR（无 /health
    契约）返回空串不探，避免误报 warn。
    """
    vr = (config.get("voice_recognition") or {}) if isinstance(config, dict) else {}
    if not vr.get("enabled", False):
        return ""
    if str(vr.get("provider") or "").strip().lower() not in ("openai", "openai_compatible"):
        return ""
    base = str(vr.get("base_url") or "").strip().rstrip("/")
    if not base or "://" not in base:
        return ""
    host = base.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    import re as _re
    private = (
        host in ("localhost", "127.0.0.1")
        or host.startswith("192.168.") or host.startswith("10.")
        or bool(_re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host))
    )
    if not private:
        return ""
    root = base[:-3] if base.endswith("/v1") else base
    return root + "/health"


# /health 探测 60s TTL 缓存：collect_health 被 watchdog tick + 各看板 API 频繁调用，
# 不能每次都打网络（探测自带 3s 超时，不可达时会拖慢调用方）。
_AUDIO_PROBE_CACHE: Dict[str, Any] = {"ts": 0.0, "url": "", "result": None}
_AUDIO_PROBE_TTL_SEC = 60.0


def probe_audio_service(config: Dict[str, Any], *, force: bool = False) -> Optional[Dict[str, Any]]:
    """探测自建 GPU 音频服务 /health（带 TTL 缓存）。返回 None = 未配置远端音频服务。"""
    url = audio_probe_target(config)
    if not url:
        return None
    now = time.time()
    if (not force and _AUDIO_PROBE_CACHE["result"] is not None
            and _AUDIO_PROBE_CACHE["url"] == url
            and now - _AUDIO_PROBE_CACHE["ts"] < _AUDIO_PROBE_TTL_SEC):
        return _AUDIO_PROBE_CACHE["result"]
    result: Dict[str, Any] = {"url": url, "reachable": False}
    try:
        import json as _json
        import urllib.request
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        result.update({
            "reachable": True,
            "latency_ms": int((time.time() - t0) * 1000),
            "asr_loaded": bool(data.get("asr_loaded")),
            "ser_loaded": bool(data.get("ser_loaded")),
            # /health 报 ser_model 非空 = 服务配置了 SER → ser_loaded 才有意义
            "ser_expected": bool(str(data.get("ser_model") or "").strip()),
            "device": str(data.get("device") or ""),
            "model": str(data.get("model") or ""),
        })
    except Exception as e:
        result["error"] = str(e)[:120]
    _AUDIO_PROBE_CACHE.update({"ts": now, "url": url, "result": result})
    return result


def avatar_probe_target(config: Dict[str, Any]) -> str:
    """决策：该不该探测 AvatarHub 7852（在线语音主力）的 /health（纯函数）。

    仅 ``avatar_voice.enabled`` 时探测；7858（懒加载批量服务，空闲卸载是常态）与
    远端 STT（有 176+本机 CPU 三级兜底）**不进灯**——避免把正常态/软降级报成异常，
    三端点明细看 ops-overview「🎙️ AvatarHub 语音」卡。
    """
    av = (config.get("avatar_voice") or {}) if isinstance(config, dict) else {}
    if not av.get("enabled", False):
        return ""
    base = str(av.get("base_url") or "http://127.0.0.1:7852").strip().rstrip("/")
    if not base:
        return ""
    return base + "/health"


_AVATAR_PROBE_CACHE: Dict[str, Any] = {"ts": 0.0, "url": "", "result": None}
_AVATAR_PROBE_TTL_SEC = 60.0


def probe_avatar_voice(config: Dict[str, Any], *, force: bool = False) -> Optional[Dict[str, Any]]:
    """探测 AvatarHub 7852 /health（带 TTL 缓存）。返回 None = 未启用。"""
    url = avatar_probe_target(config)
    if not url:
        return None
    now = time.time()
    if (not force and _AVATAR_PROBE_CACHE["result"] is not None
            and _AVATAR_PROBE_CACHE["url"] == url
            and now - _AVATAR_PROBE_CACHE["ts"] < _AVATAR_PROBE_TTL_SEC):
        return _AVATAR_PROBE_CACHE["result"]
    result: Dict[str, Any] = {"url": url, "reachable": False, "models_loaded": False}
    try:
        import json as _json
        import urllib.request
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        result.update({
            "reachable": True,
            "latency_ms": int((time.time() - t0) * 1000),
            "models_loaded": bool(
                isinstance(data, dict) and data.get("ok") is True
                and data.get("models_loaded", True)),
        })
    except Exception as e:
        result["error"] = str(e)[:120]
    _AVATAR_PROBE_CACHE.update({"ts": now, "url": url, "result": result})
    return result


def collect_health(app, config_manager=None, *, pending_threshold: int = 200) -> Dict[str, Any]:
    """采集运行时健康（route 与 watchdog 共用）。返回 build_health 的结果。"""
    from src.utils.health import build_health, is_placeholder

    state = getattr(app, "state", app)
    config = getattr(config_manager, "config", None) or {}

    inbox = getattr(state, "inbox_store", None)
    db_ok = bool(inbox.ping()) if (inbox is not None and hasattr(inbox, "ping")) else False

    ai = config.get("ai") or {}
    ai_provider = str(ai.get("provider") or "").strip()
    ai_key_ok = not is_placeholder(ai.get("api_key"))

    lic_state = lic_plan = ""
    lic_ro = False
    try:
        from src.licensing import get_license_manager
        st = get_license_manager().status()
        lic_state, lic_plan, lic_ro = st.state, st.plan, bool(st.read_only)
    except Exception:
        logger.debug("授权状态读取失败（已忽略）", exc_info=True)

    ready = configured = total = 0
    try:
        from src.utils.channel_setup import channel_status
        chs = channel_status(config)
        total = len(chs)
        ready = sum(1 for c in chs if c.get("ready"))
        configured = sum(1 for c in chs if c.get("configured"))
    except Exception:
        logger.debug("渠道状态读取失败（已忽略）", exc_info=True)

    audio = None
    try:
        audio = probe_audio_service(config)
    except Exception:
        logger.debug("音频服务探测失败（已忽略）", exc_info=True)

    avatar = None
    try:
        avatar = probe_avatar_voice(config)
    except Exception:
        logger.debug("AvatarHub 语音探测失败（已忽略）", exc_info=True)

    return build_health(
        db_ok=db_ok,
        ai_provider=ai_provider, ai_key_ok=ai_key_ok,
        license_state=lic_state, license_read_only=lic_ro, license_plan=lic_plan,
        channels_ready=ready, channels_configured=configured, channels_total=total,
        workers=_collect_workers(state),
        pending_drafts=_pending_drafts(state),
        pending_threshold=pending_threshold,
        audio_service=audio,
        avatar_voice=avatar,
    )


def health_signature(health: Dict[str, Any]) -> str:
    """把「异常组件集合」压成签名，用于去抖（只在变化时告警）。"""
    bad = sorted(
        f"{c.get('id')}:{c.get('status')}"
        for c in (health.get("components") or [])
        if c.get("status") in ("fail", "warn")
    )
    return "|".join(bad)


def problems_of(health: Dict[str, Any]) -> List[Dict[str, Any]]:
    """提取需要告警的异常组件（fail + warn）。"""
    return [
        {"id": c.get("id"), "name": c.get("name"), "status": c.get("status"),
         "detail": c.get("detail")}
        for c in (health.get("components") or [])
        if c.get("status") in ("fail", "warn")
    ]


class HealthWatchdog:
    """周期巡检运行时健康，状态变化时经 EventBus 发 ``health_alert``。

    Usage::

        wd = HealthWatchdog(app=web_app, config_manager=cm, interval_sec=300)
        asyncio.create_task(wd.run())
        wd.stop()
    """

    def __init__(
        self,
        *,
        app,
        config_manager=None,
        interval_sec: float = 300.0,
        pending_threshold: int = 200,
        alert_on_warn: bool = False,
        billing_interval_sec: float = 3600.0,
        incident_retention_days: float = 30.0,
        weekly_report_enabled: bool = False,
        weekly_interval_sec: float = 604800.0,
    ) -> None:
        self._app = app
        self._config_manager = config_manager
        self._interval = max(30.0, float(interval_sec))
        self._pending_threshold = int(pending_threshold)
        # 计费巡检比健康巡检稀疏（默认 1h）：对账单是月窗聚合，无需每个健康周期都算。
        self._billing_interval = max(self._interval, float(billing_interval_sec))
        self._last_billing_check_ts = 0.0
        # 已关闭事件保留期（天）；<=0 关闭清理。每日节流跑一次 DELETE，防表无限膨胀。
        self._retention_days = float(incident_retention_days)
        self._purge_interval = 86400.0
        self._last_purge_ts = 0.0
        # H1：运营周报自动外发（默认关，遵循「新子系统默认 enabled:false」）。
        # _last_weekly_ts 初始化为「现在」→ 首份周报在启动一个周期后才发，避免每次重启刷屏。
        self._weekly_enabled = bool(weekly_report_enabled)
        self._weekly_interval = max(3600.0, float(weekly_interval_sec))
        self._last_weekly_ts = time.time()
        self.total_weekly_reports: int = 0
        # 默认只对 fail（red）告警；warn 噪音大，可显式开
        self._alert_on_warn = bool(alert_on_warn)
        self._stop_evt = asyncio.Event()
        self._running = False
        self._last_sig: Optional[str] = None
        self._last_light: str = "green"
        self._last_billing_sig: Optional[str] = None
        # 草稿质量告警去抖（记忆命中率/p95 延迟/风险分类回检）
        self._last_draft_quality_sig: Optional[str] = None
        # AI 回复质量退化告警去抖（采纳/弃用率 + 高危量环比，基于 ai_safety_summary）
        self._last_ai_quality_sig: Optional[str] = None
        # 实时语音通话退化告警去抖（主机健康/接通率/不可达，基于 RealtimeVoiceStats）
        self._last_realtime_voice_sig: Optional[str] = None
        # 编排器受管 worker 崩溃告警去抖（某账号 protocol/web worker 进 error 态）
        self._last_orch_worker_sig: Optional[str] = None
        # 记忆 key 漂移巡检（裸 key 复发）——结构性数据，独立稀疏节流 + 去抖
        self._last_drift_check_ts: float = 0.0
        self._last_drift_sig: Optional[str] = None
        # 云端余额水位巡检（独立稀疏节流；间隔在 _check_cloud_balance 内读配置）
        self._last_cloud_balance_ts: float = 0.0
        # 本地兜底顶班提醒：上次观测的兜底出话累计数 + 首次观测到顶班的时间
        self._fb_duty_last_calls: Optional[int] = None
        self._fb_duty_since_ts: float = 0.0
        self._fb_duty_idle_ticks: int = 0
        # AvatarHub 7852 持续掉线升级提醒：首次探测到掉线的时刻 + 已首提标记 + 上次重提时刻
        self._avatar_down_since: float = 0.0
        self._avatar_alerted: bool = False
        self._avatar_last_remind: float = 0.0
        # 告警种类：down=不可达/未载入（health 探测红）；hang=半死（health 绿但合成连败，
        # 2026-07-14 事故形态）。恢复语义不同：hang 需要「失败后真的又成过一次」的正面证据。
        self._avatar_alert_kind: str = ""
        # 缺口自动入库：稀疏节流（默认 1h 一轮）+ 每日预算
        self._last_auto_stock_ts: float = 0.0
        self._auto_stock_day: str = ""
        self._auto_stock_added_today: int = 0
        # 出站媒体承诺未兑现升级提醒（Phase21a）：delta 口径累加净撤回数 + 首提/重提去抖
        self._promise_last_ret: Optional[int] = None
        self._promise_last_ful: Optional[int] = None
        self._promise_bad: int = 0            # 累计「净撤回」数（撤回多于兑现的差）
        self._promise_idle_ticks: int = 0
        self._promise_alerted: bool = False
        self._promise_last_remind: float = 0.0
        self.total_alerts: int = 0
        self.total_recoveries: int = 0
        self.total_billing_alerts: int = 0
        self.total_draft_quality_alerts: int = 0
        self.total_ai_quality_alerts: int = 0
        self.total_realtime_voice_alerts: int = 0
        self.total_orchestrator_worker_alerts: int = 0
        self.total_memory_key_drift_alerts: int = 0
        self.total_platform_session_reminders: int = 0
        self.total_cloud_balance_alerts: int = 0
        self.total_fallback_duty_reminders: int = 0
        self.total_avatar_voice_reminders: int = 0
        self.total_media_promise_alerts: int = 0
        self.total_auto_stocked: int = 0
        self.total_tg_call_reminders: int = 0
        # 原生通话主机升级式提醒状态（镜像 avatar_voice 那套）
        self._tgcall_down_since: float = 0.0
        self._tgcall_alerted: bool = False
        self._tgcall_last_remind: float = 0.0
        self.last_check_ts: float = 0.0
        self.last_light: str = "green"

    async def run(self) -> None:
        self._running = True
        self._stop_evt.clear()
        logger.info("HealthWatchdog 已启动（interval=%.0fs alert_on_warn=%s）",
                    self._interval, self._alert_on_warn)
        # 启动后稍等，避开冷启动期的瞬时 fail（worker 尚未 running）
        try:
            await asyncio.wait_for(self._stop_evt.wait(), timeout=min(60.0, self._interval))
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop_evt.is_set():
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._tick)
            except Exception:
                logger.debug("HealthWatchdog tick 异常（已忽略）", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval)
                break
            except asyncio.TimeoutError:
                pass
        self._running = False
        logger.info("HealthWatchdog 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    def _evaluate_health(self) -> Dict[str, Any]:
        """采集健康并按签名变化 emit 告警/恢复（_tick 与 recheck 共用）。返回 health。"""
        health = collect_health(self._app, self._config_manager,
                                pending_threshold=self._pending_threshold)
        self.last_check_ts = time.time()
        light = str(health.get("light") or "green")
        self.last_light = light

        # 决定「是否处于告警态」：red 必告；yellow 仅在开关打开时告
        alerting = (light == "red") or (light == "yellow" and self._alert_on_warn)
        sig = health_signature(health) if alerting else ""

        if alerting:
            if sig != self._last_sig:
                self._emit_alert(health)
                self.total_alerts += 1
            self._last_sig = sig
            self._last_light = light
        else:
            # 从异常恢复 → 补发恢复通知
            if self._last_light in ("red", "yellow") and self._last_sig:
                self._emit_recovery(health)
                self.total_recoveries += 1
            self._last_sig = None
            self._last_light = light
        return health

    def recheck(self) -> Dict[str, Any]:
        """按需立即重巡健康（H2 一键动作）：复用 _evaluate_health，会即时开/关事件。

        只跑健康部分（不触发计费/清理/周报），让主管修复后点一下即可看到事件自动恢复。
        """
        return self._evaluate_health()

    def _tick(self) -> None:
        self._evaluate_health()

        # E3：计费异常巡检（超席位/超额），独立去抖，经 D3 通道外发。
        try:
            self._check_billing()
        except Exception:
            logger.debug("计费巡检异常（已忽略）", exc_info=True)

        # 统一草稿引擎质量巡检（记忆命中率/p95/风险分类回检），独立去抖。
        try:
            self._check_draft_quality()
        except Exception:
            logger.debug("草稿质量巡检异常（已忽略）", exc_info=True)

        # AI 回复质量退化巡检（采纳/弃用率 + 高危量环比），默认关、独立去抖。
        try:
            self._check_ai_quality()
        except Exception:
            logger.debug("AI 质量巡检异常（已忽略）", exc_info=True)

        # 实时语音通话退化巡检（主机健康/接通率/不可达），默认关、独立去抖。
        try:
            self._check_realtime_voice()
        except Exception:
            logger.debug("实时语音告警巡检异常（已忽略）", exc_info=True)

        # 编排器受管 worker 崩溃巡检（P6）：某账号 worker 进 error 态即主动外发，独立去抖。
        try:
            self._check_orchestrator_workers()
        except Exception:
            logger.debug("编排器 worker 巡检异常（已忽略）", exc_info=True)

        # 平台会话持续掉线提醒（P4）：worker push 的掉线转移只告警一次，若长时间没人修
        # 则周期性再提醒（升级式：after_min 首提，之后每 interval_min 一条）。
        try:
            self._check_platform_sessions()
        except Exception:
            logger.debug("平台会话持续掉线巡检异常（已忽略）", exc_info=True)

        # AvatarHub 7852 持续掉线升级提醒：黄灯只在看板可见（alert_on_warn=False），
        # 掉线超阈值后主动外发（首提 + 周期重提，恢复补发恢复通知）。
        try:
            self._check_avatar_voice()
        except Exception:
            logger.debug("AvatarHub 语音巡检异常（已忽略）", exc_info=True)

        # 原生通话主机（brain=s2s 复用 MiniCPM-o 176:7860）持续不可用升级提醒：掉了=打进来
        # 的电话全接不了（陪护"她会接电话"卖点静默失效），比看板黄灯更该主动轰人。
        try:
            self._check_native_call()
        except Exception:
            logger.debug("原生通话主机巡检异常（已忽略）", exc_info=True)

        # 备货缺口自动入库（Phase5）：高频短句缺口达标自动进台词库（守卫+每日预算），
        # 夜间计划任务渲染兜底——「看缺口→补台词」不再需要人。
        try:
            self._check_avatar_auto_stock()
        except Exception:
            logger.debug("缺口自动入库巡检异常（已忽略）", exc_info=True)

        # 出站媒体承诺未兑现升级提醒（Phase21a）：AI 文本承诺发图/语音却撤回=信任受损，
        # 看板黄条只被动可见；本窗口净撤回累加达阈值→主动外发（首提+周期重提，恢复清零）。
        try:
            self._check_media_promise()
        except Exception:
            logger.debug("媒体承诺巡检异常（已忽略）", exc_info=True)

        # 云端余额水位巡检（2026-07-12）：DeepSeek 预付费余额低于阈值 → 主机弹窗+远程镜像，
        # 防「扣完才发现」。独立稀疏节流（默认 1h 一探），未启用/非 DeepSeek 主链零开销。
        try:
            self._check_cloud_balance()
        except Exception:
            logger.debug("云端余额巡检异常（已忽略）", exc_info=True)

        # 备用 Key 主动探活（2026-07-12 下午）：池 key 每日 1-token chat ping——
        # 「被封/装错端点/模型名失效」只有真打一次才暴露，关掉「切过去才发现备用也坏」窗口。
        try:
            self._check_pool_key_pings()
        except Exception:
            logger.debug("备用 Key 探活巡检异常（已忽略）", exc_info=True)

        # 本地兜底「长期顶班」提醒（2026-07-12）：熔断弹窗只在开路瞬间发一次；若云端
        # 一直不恢复、兜底持续出话超过 after_min，升级式再提醒（interval_min 重提）。
        try:
            self._check_local_fallback_duty()
        except Exception:
            logger.debug("本地兜底顶班巡检异常（已忽略）", exc_info=True)

        # 实时语音趋势落库兜底 sync（旁路漏记时补写当日增量）。
        try:
            self._sync_realtime_voice_trend()
        except Exception:
            logger.debug("实时语音趋势 sync 异常（已忽略）", exc_info=True)

        # 出站路由回落率趋势 sync（P8）：累计计数器的增量按日落库，供看板 7 天曲线。
        try:
            self._sync_send_route_trend()
        except Exception:
            logger.debug("出站路由趋势 sync 异常（已忽略）", exc_info=True)

        # 出站路由回落率趋势落库 sync（P8：累计计数器的当日增量，默认关时 no-op）。
        try:
            self._sync_send_route_trend()
        except Exception:
            logger.debug("出站路由趋势 sync 异常（已忽略）", exc_info=True)

        # 记忆 key 漂移巡检（裸 key 复发 → 记忆对引擎不可见），稀疏节流 + 独立去抖。
        try:
            self._check_memory_key_drift()
        except Exception:
            logger.debug("记忆 key 漂移巡检异常（已忽略）", exc_info=True)

        # 运维卫生：按保留期清理已关闭事件（每日节流一次）。
        try:
            self._maybe_purge_incidents()
        except Exception:
            logger.debug("事件清理异常（已忽略）", exc_info=True)

        # H1：运营周报自动外发（每周节流一次，默认关）。
        try:
            self._maybe_weekly_report()
        except Exception:
            logger.debug("运营周报生成异常（已忽略）", exc_info=True)

    def _license_quota(self) -> Dict[str, Any]:
        try:
            from src.licensing import get_license_manager
            st = get_license_manager().status()
            return {
                "plan": st.plan, "state": st.state,
                "customer": getattr(st, "customer", ""),
                "seats": st.seats, "channels": list(st.channels),
            }
        except Exception:
            return {"plan": "community", "state": "unavailable", "customer": "",
                    "seats": 0, "channels": []}

    def _check_billing(self, *, now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        # 节流：距上次计费巡检不足 billing_interval 则跳过（首次 last=0 必跑）。
        if self._last_billing_check_ts and (ts - self._last_billing_check_ts) < self._billing_interval:
            return
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "get_usage_stats"):
            return
        self._last_billing_check_ts = ts
        from src.utils.ops_overview import billing_anomalies

        statement = self._compute_statement()
        if statement is None:
            return
        anomalies = billing_anomalies(statement)
        sig = "|".join(sorted(a.get("code", "") for a in anomalies))

        if anomalies:
            if sig != self._last_billing_sig:
                self._emit_billing_alert(anomalies)
                self.total_billing_alerts += 1
            self._last_billing_sig = sig
        else:
            if self._last_billing_sig:
                # 本进程内 alert→green 的正常恢复：resolve + 外发恢复通知
                self._emit_billing_recovery()
            else:
                # 进程刚起且当前无异常：静默 reconcile 掉上一进程遗留的 open 计费事件。
                # （修复某计费异常后重启时，in-memory 签名为空，否则旧 red 事件会一直挂着，
                #  既不在本进程内 emit 恢复，也无人关闭。）静默关闭，不外发恢复通知。
                inbox = self._inbox()
                if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                    try:
                        n = inbox.resolve_open_incidents(kind="billing") or 0
                        if n:
                            logger.info(
                                "HealthWatchdog 启动 reconcile：关闭遗留计费事件 %d 条", n)
                    except Exception:
                        logger.debug("计费事件 reconcile 失败（已忽略）", exc_info=True)
            self._last_billing_sig = None

    def _compute_statement(self) -> Optional[Dict[str, Any]]:
        """算当月对账单（_check_billing 与周报共用）。失败/无 store 返回 None。"""
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "get_usage_stats"):
            return None
        from src.utils.billing import compute_statement
        config = getattr(self._config_manager, "config", None) or {}
        lt = time.localtime()
        try:
            return compute_statement(
                inbox, lt.tm_year, lt.tm_mon,
                license_status=self._license_quota(), pricing=config.get("pricing"),
            )
        except Exception:
            logger.debug("对账单计算失败（已忽略）", exc_info=True)
            return None

    def _emit_billing_alert(self, anomalies: List[Dict[str, Any]]) -> None:
        # E3↔E2：计费异常也进 ops_incidents（kind=billing），与健康事件统一可 ack/指派/恢复。
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                has_fail = any(a.get("severity") == "fail" for a in anomalies)
                problems = [
                    {"id": a.get("code"), "name": "计费", "status": a.get("severity"),
                     "detail": a.get("message")}
                    for a in anomalies
                ]
                inbox.open_or_update_incident(
                    kind="billing",
                    signature="|".join(sorted(a.get("code", "") for a in anomalies)),
                    light="red" if has_fail else "yellow",
                    summary={"anomalies": len(anomalies)},
                    problems=problems,
                )
        except Exception:
            logger.debug("计费事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("billing_alert", {
                "anomalies": anomalies, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出计费异常告警：%d 项", len(anomalies))
        except Exception:
            logger.debug("billing_alert 发布失败（已忽略）", exc_info=True)

    def _emit_billing_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="billing")
        except Exception:
            logger.debug("计费事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("billing_alert", {"anomalies": [], "recovered": True})
            logger.info("HealthWatchdog 发出计费恢复通知")
        except Exception:
            logger.debug("billing recovery 发布失败（已忽略）", exc_info=True)

    def _check_draft_quality(self) -> None:
        """统一草稿引擎质量巡检：基于**窗口速率**评估（可触发、可恢复）。

        三项规则（阈值见 ``inbox.auto_draft.quality_alert``）：
          - 记忆命中率过低 → 自动回复可能「记不住」客户信息
          - p95 生成延迟过高 → 延迟预算被突破
          - 低风险快路占比过高 → 风险分类可能过宽（敏感消息或未走全栈/审核）

        用**窗口**速率而非累计：累计率一旦退化无法回弹，无法表达「已恢复」；窗口率
        随近 1h 流量实时升降，触发与恢复都灵敏。窗口样本不足时静默（不改状态）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        qa = (((cfg.get("inbox") or {}).get("auto_draft") or {}).get("quality_alert")
              or {}) if isinstance(cfg, dict) else {}
        if not qa.get("enabled", True):
            return
        try:
            from src.monitoring.metrics_store import get_metrics_store
            snap = get_metrics_store().get_inbox_draft_metrics()
        except Exception:
            return

        window = snap.get("window") or {}
        win_gen = int(window.get("generated") or 0)
        min_samples = int(qa.get("min_samples", 30))
        if win_gen < max(1, min_samples):
            return  # 样本不足，静默（不改变既有告警/恢复态）

        def _rate(name: str) -> float:
            return (int(window.get(name) or 0) / win_gen) if win_gen else 0.0

        problems: List[Dict[str, Any]] = []

        # 分级：严重退化升 red（fail），轻微越界为 yellow（warn）。
        mem_min = float(qa.get("memory_hit_min", 0.30))
        mem_severe = float(qa.get("memory_hit_severe", 0.15))
        mem_r = _rate("memory_hit")
        if mem_r < mem_min:
            problems.append({
                "id": "memory_hit_low", "name": "草稿记忆命中率",
                "status": "fail" if mem_r < mem_severe else "warn",
                "detail": (f"近窗口记忆命中率 {mem_r:.0%} < 阈值 {mem_min:.0%}"
                           f"{'（严重失忆）' if mem_r < mem_severe else '（自动回复可能记不住客户信息）'}"),
            })

        latency = snap.get("latency") or {}
        p95_max = int(qa.get("p95_ms_max", 8000))
        p95_severe = int(qa.get("p95_ms_severe", p95_max * 2))
        p95 = int(latency.get("p95_ms") or 0)
        if latency.get("count") and p95 > p95_max:
            problems.append({
                "id": "latency_high", "name": "草稿生成 p95 延迟",
                "status": "fail" if p95 > p95_severe else "warn",
                "detail": f"p95 {p95}ms > 阈值 {p95_max}ms（n={latency.get('count')}）",
            })

        fp_max = float(qa.get("fast_path_ratio_max", 0.98))
        fp_ratio = _rate("fast_path")
        if fp_ratio > fp_max:
            problems.append({
                "id": "risk_classify_loose", "name": "风险分类可能过宽",
                "status": "warn",  # 配置质量信号，非故障 → 恒 yellow
                "detail": (f"近窗口低风险快路占比 {fp_ratio:.0%} > 阈值 {fp_max:.0%}"
                           "（几乎全判低风险，敏感消息可能未走全栈/人工审核）"),
            })

        # 签名带上 status：轻微→严重的升级会改变签名 → 重新发一条（值班能感知升级）。
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        light = "red" if any(p["status"] == "fail" for p in problems) else "yellow"
        if problems:
            if sig != self._last_draft_quality_sig:
                self._emit_draft_quality_alert(problems, light)
                self.total_draft_quality_alerts += 1
            self._last_draft_quality_sig = sig
        else:
            if self._last_draft_quality_sig:
                self._emit_draft_quality_recovery()
            self._last_draft_quality_sig = None

    def _emit_draft_quality_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="draft_quality",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("草稿质量事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("draft_quality_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出草稿质量告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("draft_quality_alert 发布失败（已忽略）", exc_info=True)

    def _emit_draft_quality_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="draft_quality")
        except Exception:
            logger.debug("草稿质量事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("draft_quality_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出草稿质量恢复通知")
        except Exception:
            logger.debug("draft_quality recovery 发布失败（已忽略）", exc_info=True)

    def _check_ai_quality(self, *, now: Optional[float] = None) -> None:
        """AI 回复质量退化巡检（F1）：基于 ``ai_safety_summary`` 处置结果口径评估采纳/弃用率
        与高危量环比，退化即落 ``ops_incidents(kind=ai_quality)`` 供值班 ack/指派，恢复自动
        resolve。**默认关**（阈值须按真实分布校准后再开）；样本不足静默；去抖同其余巡检。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        aq = ((((cfg.get("inbox") or {}).get("ai_quality_alert")) or {})
              if isinstance(cfg, dict) else {})
        if not aq.get("enabled", False):
            return
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "ai_safety_summary"):
            return
        now = float(now if now is not None else time.time())
        window = max(1, int(aq.get("window_days", 7) or 7)) * 86400
        try:
            cur = inbox.ai_safety_summary(since_ts=now - window)
            prev = inbox.ai_safety_summary(since_ts=now - 2 * window, until_ts=now - window)
        except Exception:
            return  # 读失败静默，不改变既有告警/恢复态
        from src.utils.ai_quality_alert import evaluate_ai_quality
        res = evaluate_ai_quality(cur, prev, aq)
        problems = res.get("problems") or []
        light = res.get("light") or "green"
        # 签名带 status：warn→fail 升级会改签名 → 重发一条（值班能感知升级）。
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        if problems:
            if sig != self._last_ai_quality_sig:
                self._emit_ai_quality_alert(problems, light)
                self.total_ai_quality_alerts += 1
            self._last_ai_quality_sig = sig
        else:
            if self._last_ai_quality_sig:
                self._emit_ai_quality_recovery()
            self._last_ai_quality_sig = None

    def _emit_ai_quality_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="ai_quality",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("AI 质量事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ai_quality_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出 AI 质量告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("ai_quality_alert 发布失败（已忽略）", exc_info=True)

    def _emit_ai_quality_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="ai_quality")
        except Exception:
            logger.debug("AI 质量事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ai_quality_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出 AI 质量恢复通知")
        except Exception:
            logger.debug("ai_quality recovery 发布失败（已忽略）", exc_info=True)

    def _check_realtime_voice(self) -> None:
        """实时语音通话退化巡检（B 线）：基于 ``RealtimeVoiceStats`` 评估主机健康/接通率/
        主机不可达，退化即落 ``ops_incidents(kind=realtime_voice)``。**默认关**；功能未启用
        或样本不足静默；去抖同其余巡检。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        rtv = (cfg.get("realtime_voice") or {}) if isinstance(cfg, dict) else {}
        if not rtv.get("enabled", False):
            return
        alert_cfg = (rtv.get("alert") or {}) if isinstance(rtv, dict) else {}
        if not alert_cfg.get("enabled", False):
            return
        try:
            from src.ai.realtime_voice_stats import get_realtime_voice_stats
            from src.utils.realtime_voice_alert import evaluate_realtime_voice_alert
            stats = get_realtime_voice_stats().dump()
            res = evaluate_realtime_voice_alert(stats, alert_cfg)
        except Exception:
            return
        problems = res.get("problems") or []
        light = res.get("light") or "green"
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        if problems:
            if sig != self._last_realtime_voice_sig:
                self._emit_realtime_voice_alert(problems, light)
                self.total_realtime_voice_alerts += 1
            self._last_realtime_voice_sig = sig
        else:
            if self._last_realtime_voice_sig:
                self._emit_realtime_voice_recovery()
            else:
                # 进程刚起且当前无异常：静默 reconcile 遗留 open 事件（重启后 stats 归零，
                # 内存签名空，否则旧 red 事件会一直挂着且不会 emit 恢复）。
                inbox = self._inbox()
                if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                    try:
                        n = inbox.resolve_open_incidents(kind="realtime_voice") or 0
                        if n:
                            logger.info(
                                "HealthWatchdog 启动 reconcile：关闭遗留实时语音事件 %d 条", n)
                    except Exception:
                        logger.debug("实时语音事件 reconcile 失败（已忽略）", exc_info=True)
            self._last_realtime_voice_sig = None

    def _sync_realtime_voice_trend(self) -> None:
        """E 线兜底：watchdog tick 把进程 stats 与上次同步快照 diff 写入趋势库（旁路漏记时补）。"""
        cfg = getattr(self._config_manager, "config", None) or {}
        rtv = (cfg.get("realtime_voice") or {}) if isinstance(cfg, dict) else {}
        if not rtv.get("enabled", False) or not rtv.get("trend_log", False):
            return
        try:
            from src.ai.realtime_voice_stats import get_realtime_voice_stats
            from src.ai.realtime_voice_trend_store import sync_realtime_voice_trend_from_stats
            sync_realtime_voice_trend_from_stats(get_realtime_voice_stats().dump())
        except Exception:
            logger.debug("实时语音趋势 sync 失败（已忽略）", exc_info=True)

    def _sync_send_route_trend(self) -> None:
        """P8：watchdog tick 把出站路由累计计数（SendRouteStats）的增量按日落库。

        默认关（``inbox.send_route.trend_log=false``）→ sync 恒 no-op（store 未装配即静默）。
        累计计数器口径：sync-from-stats 只写正增量，重启归零后以新基线重启不写负值。
        """
        try:
            from src.inbox.send_route_stats import get_send_route_stats
            from src.inbox.send_route_trend_store import (
                sync_send_route_trend_from_stats,
            )
            sync_send_route_trend_from_stats(get_send_route_stats().dump())
        except Exception:
            logger.debug("出站路由趋势 sync 失败（已忽略）", exc_info=True)

    def _emit_realtime_voice_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="realtime_voice",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("实时语音事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("realtime_voice_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出实时语音告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("realtime_voice_alert 发布失败（已忽略）", exc_info=True)

    def _emit_realtime_voice_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="realtime_voice")
        except Exception:
            logger.debug("实时语音事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("realtime_voice_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出实时语音恢复通知")
        except Exception:
            logger.debug("realtime_voice recovery 发布失败（已忽略）", exc_info=True)

    def _check_platform_sessions(self, *, now: Optional[float] = None) -> None:
        """平台会话「持续不健康」提醒（P4，闭环 P0-2 的告警时效性）。

        掉线转移告警（session-status 路由发）只发一次；若会话掉线 ``after_min``
        分钟仍未恢复 → 补一条「还没人修」提醒，之后每 ``interval_min`` 分钟一条。
        节流状态在 ``PlatformSessionHealth`` 内（恢复自动清零），本方法无自有状态。
        配置：``health_watchdog.session_stale_remind.{enabled,after_min,interval_min}``
        （默认开，30min 首提 / 4h 重提；notifier 每小时限流是第二层兜底）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        sr = (((cfg.get("health_watchdog") or {}).get("session_stale_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not sr.get("enabled", True):
            return
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            store = get_platform_session_health()
        except Exception:
            return
        after_sec = max(60.0, float(sr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(sr.get("interval_min", 240) or 240) * 60.0)
        due = store.due_reminders(min_age_sec=after_sec, interval_sec=interval_sec,
                                  now=now)
        if not due:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            bus = get_event_bus()
        except Exception:
            return
        for key, sess in due.items():
            plat, _, acct = key.partition(":")
            down_min = int(float(sess.get("down_sec") or 0) // 60)
            bus.publish("platform_session_alert", {
                "platform": plat,
                "account_id": acct,
                "login_id": str(sess.get("login_id") or ""),
                "status": str(sess.get("status") or ""),
                "detail": str(sess.get("detail") or ""),
                "reminder": True,
                "down_minutes": down_min,
                # 独立限流键：与掉线转移告警分开（30min 首提不被 1h 窗口误吞），
                # 多账号互不挤占。
                "rate_key": f"{key}:remind",
            })
            self.total_platform_session_reminders += 1

    def _check_avatar_voice(self, *, now: Optional[float] = None) -> None:
        """AvatarHub 7852（在线语音克隆主力）持续掉线/半死的升级式提醒。

        黄灯（health 组件 warn）在 ``alert_on_warn=False`` 下只在看板可见——语音降级
        edge 通用声聊天不中断，但克隆音色事实上不可用；若长时间没人修，「拟人声」
        这一产品卖点在静默流失。本巡检把「持续掉线」升级为主动外发：
          - 掉线 ≥ ``after_min``（默认 30min）→ 首提（EventBus ``avatar_voice_alert``）；
          - 仍未恢复 → 每 ``interval_min``（默认 240min）重提一条；
          - 恢复 → 补发恢复通知 + 状态清零。
        配置 ``health_watchdog.avatar_voice_remind.{enabled,after_min,interval_min}``
        （默认开）；avatar_voice 未启用时探测返回 None → 天然静默零误报。
        探测复用 ``probe_avatar_voice``（60s TTL 缓存，tick 间隔 300s → 每 tick 新鲜）。

        **半死检测**（2026-07-14 事故：13:42–16:01 /health 一直 200、register_spk
        正常，但 /v1/tts/clone 全部超时 → 全部语音静默回落 edge 两个多小时，
        health-only 探测全程绿灯零告警）：probe 绿 && 真实合成连败
        ``hang_fail_streak``（默认 3）次 && 最近失败在 ``hang_fresh_min``（默认 20min）
        内 → 视同不可用（kind=hang），按 ``hang_after_min``（默认 20min，给外部
        EmotionTTSWatchdog 自动重启留窗口——它没修好才轰人）走同一套升级提醒。
        hang 的**恢复需要正面证据**（失败之后真的又成功合成过一次）；无流量导致
        证据陈旧时保持现状——既不误报恢复也不无凭据重提。信号源
        ``avatar_voice_stats.hang_signal()``（只有真实合成路径喂数）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        ar = (((cfg.get("health_watchdog") or {}).get("avatar_voice_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not ar.get("enabled", True):
            return
        probe = probe_avatar_voice(cfg)
        if probe is None:
            return  # avatar_voice 未启用
        ts = float(now if now is not None else time.time())
        probe_healthy = bool(probe.get("reachable") and probe.get("models_loaded"))

        # ── 半死信号（health 绿但合成挂死）────────────────────────────────
        hang_streak_need = int(ar.get("hang_fail_streak", 3) or 0)
        hang_fresh_sec = max(300.0, float(ar.get("hang_fresh_min", 20) or 20) * 60.0)
        hang_active = False
        hang_cleared = True
        sig: Dict[str, Any] = {}
        if probe_healthy and hang_streak_need > 0:
            try:
                from src.ai.avatar_voice_stats import get_avatar_voice_stats
                sig = get_avatar_voice_stats().hang_signal()
            except Exception:
                sig = {}
            streak = int(sig.get("fail_streak") or 0)
            last_fail = float(sig.get("last_fail_ts") or 0.0)
            last_ok = float(sig.get("last_ok_ts") or 0.0)
            hang_active = (streak >= hang_streak_need
                           and last_fail > 0.0
                           and ts - last_fail <= hang_fresh_sec)
            # 正面恢复证据 = 最近一次失败之后又真的成过；从未失败过也算干净
            hang_cleared = (last_fail <= 0.0) or (last_ok >= last_fail)

        healthy = probe_healthy and not hang_active

        if healthy:
            if self._avatar_alerted:
                if self._avatar_alert_kind == "hang" and not hang_cleared:
                    # hang 告警后无流量（信号陈旧）：没有成功合成的正面证据，
                    # 不发恢复通知也不清零——下一条真实语音见分晓
                    return
                # 从「已告警」恢复 → 补发恢复通知（未曾告警的抖动恢复不发，防噪）
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("avatar_voice_alert", {
                        "recovered": True,
                        "rate_key": "avatar_voice:recovered",
                    })
                    logger.info("HealthWatchdog 发出 AvatarHub 语音恢复通知")
                except Exception:
                    logger.debug("avatar_voice recovery 发布失败（已忽略）", exc_info=True)
            self._avatar_down_since = 0.0
            self._avatar_alerted = False
            self._avatar_last_remind = 0.0
            self._avatar_alert_kind = ""
            return

        kind = "down" if not probe_healthy else "hang"
        self._avatar_alert_kind = kind
        if not self._avatar_down_since:
            self._avatar_down_since = ts
            return
        down_sec = ts - self._avatar_down_since
        if kind == "hang":
            # 默认 20min：外部合成级看门狗（EmotionTTSWatchdog，两振×5min+重启~3min）
            # 通常 ~15min 内自愈；仍 hang 说明自动重启没救回来，才升级轰人
            after_sec = max(60.0, float(ar.get("hang_after_min", 20) or 20) * 60.0)
        else:
            after_sec = max(60.0, float(ar.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(ar.get("interval_min", 240) or 240) * 60.0)
        due = (
            (not self._avatar_alerted and down_sec >= after_sec)
            or (self._avatar_alerted
                and ts - self._avatar_last_remind >= interval_sec)
        )
        if not due:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("avatar_voice_alert", {
                "reachable": bool(probe.get("reachable")),
                "models_loaded": bool(probe.get("models_loaded")),
                "url": str(probe.get("url") or ""),
                "error": str(probe.get("error") or ""),
                "hang": kind == "hang",
                "fail_streak": int(sig.get("fail_streak") or 0),
                "down_minutes": int(down_sec // 60),
                "reminder": bool(self._avatar_alerted),
                # 独立限流键：首提/重提不与其他事件挤 1h 窗
                "rate_key": "avatar_voice:remind",
            })
        except Exception:
            logger.debug("avatar_voice alert 发布失败（已忽略）", exc_info=True)
            return
        self._avatar_alerted = True
        self._avatar_last_remind = ts
        self.total_avatar_voice_reminders += 1

    def _check_native_call(self, *, now: Optional[float] = None) -> None:
        """原生通话主机持续不可用的升级式提醒（镜像 ``_check_avatar_voice``）。

        原生来电（brain=s2s）的大脑=`realtime_voice` 那台 MiniCPM-o 主机；它挂了 → 打进来的
        电话全接不了（决策会走 host_cold 拒接+补偿，用户端体验=「总是没接」），"她会接电话"
        这个陪护卖点静默失效。看板黄灯不够，超阈值主动外发。

        配置 ``health_watchdog.tg_call_remind.{enabled,after_min,interval_min}``（默认开）；
        telegram_calls 未启用 / 非 s2s → 探测返回 None → 天然静默零误报。探测复用
        ``voicecall.health.probe_call_host``（60s TTL），与就绪度体检、ops 卡同一事实源。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        cr = (((cfg.get("health_watchdog") or {}).get("tg_call_remind")) or {}) \
            if isinstance(cfg, dict) else {}
        if not cr.get("enabled", True):
            return
        try:
            from src.voicecall.health import probe_call_host
        except Exception:
            return
        probe = probe_call_host(cfg)
        if probe is None:
            return  # telegram_calls 未启用 / 非 s2s
        ts = float(now if now is not None else time.time())
        healthy = bool(probe.get("reachable") and probe.get("model_loaded"))

        if healthy:
            if self._tgcall_alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("tg_call_alert", {
                        "recovered": True, "rate_key": "tg_call:recovered"})
                    logger.info("HealthWatchdog 发出原生通话主机恢复通知")
                except Exception:
                    logger.debug("tg_call recovery 发布失败（已忽略）", exc_info=True)
            self._tgcall_down_since = 0.0
            self._tgcall_alerted = False
            self._tgcall_last_remind = 0.0
            return

        if not self._tgcall_down_since:
            self._tgcall_down_since = ts
            return
        down_sec = ts - self._tgcall_down_since
        after_sec = max(60.0, float(cr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(cr.get("interval_min", 240) or 240) * 60.0)
        due = (
            (not self._tgcall_alerted and down_sec >= after_sec)
            or (self._tgcall_alerted and ts - self._tgcall_last_remind >= interval_sec)
        )
        if not due:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("tg_call_alert", {
                "reachable": bool(probe.get("reachable")),
                "model_loaded": bool(probe.get("model_loaded")),
                "url": str(probe.get("url") or ""),
                "error": str(probe.get("error") or ""),
                "down_minutes": int(down_sec // 60),
                "reminder": bool(self._tgcall_alerted),
                "rate_key": "tg_call:remind",
            })
        except Exception:
            logger.debug("tg_call alert 发布失败（已忽略）", exc_info=True)
            return
        self._tgcall_alerted = True
        self._tgcall_last_remind = ts
        self.total_tg_call_reminders += 1

    def _check_avatar_auto_stock(self, *, now: Optional[float] = None) -> None:
        """备货缺口自动入库（Phase5）：达标短句自动进台词库，运营零操作。

        策略（配置 ``avatar_voice.prerender.auto_stock.{enabled,min_count,max_per_day}``，
        **默认关**——新子系统约定）：
          - 每小时扫一轮 stats 的缺口 Top-N；
          - ``qualify_auto_stock`` 守卫（频次阈值/长度/数字/URL/敏感词）；
          - 单人设占比 ≥80% 进该人设专属库，否则进 ``_common``；
          - 每日预算 ``max_per_day``（默认 10）防台词库被一次性措辞灌爆；
          - 写入即止——渲染交给夜间 AvatarPrerenderNightly（幂等增量）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        av = (cfg.get("avatar_voice") or {}) if isinstance(cfg, dict) else {}
        pre = av.get("prerender") if isinstance(av.get("prerender"), dict) else {}
        asc = pre.get("auto_stock") if isinstance(pre.get("auto_stock"), dict) else {}
        if not (av.get("enabled") and asc.get("enabled", False)):
            return
        ts = float(now if now is not None else time.time())
        if self._last_auto_stock_ts and (ts - self._last_auto_stock_ts) < 3600.0:
            return
        self._last_auto_stock_ts = ts
        # 每日预算滚动
        day = time.strftime("%Y%m%d", time.localtime(ts))
        if day != self._auto_stock_day:
            self._auto_stock_day = day
            self._auto_stock_added_today = 0
        max_per_day = max(0, int(asc.get("max_per_day", 10) or 10))
        budget = max_per_day - self._auto_stock_added_today
        if budget <= 0:
            return
        try:
            from src.ai.avatar_voice_stats import get_avatar_voice_stats
            from src.ai.voice_prerender import auto_stock_from_misses
            misses = get_avatar_voice_stats().dump().get("top_misses") or []
            rv = auto_stock_from_misses(
                misses, min_count=int(asc.get("min_count", 5) or 5),
                max_add=budget)
        except Exception:
            logger.debug("auto_stock 执行失败（已忽略）", exc_info=True)
            return
        n = len(rv.get("added") or [])
        if n:
            self._auto_stock_added_today += n
            self.total_auto_stocked += n
            logger.info("缺口自动入库 %d 条：%s", n,
                        "; ".join(f"{a['text']}→{a['target']}"
                                  for a in rv["added"]))

    def _check_media_promise(self, *, now: Optional[float] = None) -> None:
        """出站媒体承诺未兑现升级式提醒（Phase21a）。

        事故场景：AI 文本承诺「等我拍张给你」，但发图链失败 → 守卫**撤回**承诺改发
        台阶文本。撤回本身是正确的兜底（好过发谎话），但**频繁撤回**说明发图/语音
        链在持续坏、用户被反复放鸽子——信任受损。ops 卡的黄条是被动可见，本巡检把
        「持续净撤回」升级为主动外发。

        判定口径＝**窗口 delta**（累计计数器不能表达恢复）。承诺兑现有两条链：B 线同步
        （``promise_fulfilled``）+ A 线异步（``promise_fulfilled_async``）；承诺落空也有两种：
        撤回改台阶文本（``promise_retracted``）+ 异步兑现失败（``promise_fulfill_failed``）。
        本巡检聚合两侧——**坏** = 撤回 + 异步失败，**好** = 同步兑现 + 异步兑现——按窗口
        增量算净坏累加进 ``_promise_bad``；达 ``min_retracted``（默认 3）→ 首提，之后每
        ``interval_min``（默认 240）重提；连续 2 个 tick 无净坏（含无活动）→ 判恢复清零。
        配置 ``health_watchdog.media_promise_remind.{enabled,min_retracted,interval_min}``
        （默认开）。承诺守卫未产生任何事件时天然静默（baseline 后无 delta）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        pr = (((cfg.get("health_watchdog") or {}).get("media_promise_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not pr.get("enabled", True):
            return
        try:
            from src.inbox.image_autosend import metrics_snapshot
            snap = metrics_snapshot()
        except Exception:
            return
        # 坏＝撤回 + 异步兑现失败；好＝同步兑现 + 异步兑现（两条链聚合，口径完整）
        cur_ret = (int(snap.get("promise_retracted") or 0)
                   + int(snap.get("promise_fulfill_failed") or 0))
        cur_ful = (int(snap.get("promise_fulfilled") or 0)
                   + int(snap.get("promise_fulfilled_async") or 0))
        prev_ret = self._promise_last_ret
        prev_ful = self._promise_last_ful
        self._promise_last_ret = cur_ret
        self._promise_last_ful = cur_ful
        if prev_ret is None or prev_ful is None:
            return  # 首个周期只建基线（避免重启后把历史累计当本窗口增量误报）
        d_ret = max(0, cur_ret - prev_ret)
        d_ful = max(0, cur_ful - prev_ful)
        ts = float(now if now is not None else time.time())
        min_count = max(1, int(pr.get("min_retracted", 3) or 3))
        interval_sec = max(600.0, float(pr.get("interval_min", 240) or 240) * 60.0)

        if d_ret > d_ful:
            self._promise_bad += (d_ret - d_ful)
            self._promise_idle_ticks = 0
        else:
            # 本窗口无净坏（兑现 ≥ 落空，含无活动）：连续 2 tick 即判恢复清零。
            # 「无活动也算 idle」＝问题停了就该恢复（不因缺兑现事件永久卡在告警态刷屏）。
            self._promise_idle_ticks += 1
            if self._promise_idle_ticks >= 2:
                if self._promise_alerted:
                    self._emit_media_promise_recovery()
                self._promise_bad = 0
                self._promise_alerted = False
                self._promise_last_remind = 0.0
                return

        if self._promise_bad < min_count:
            return
        due = (
            (not self._promise_alerted)
            or (ts - self._promise_last_remind >= interval_sec)
        )
        if not due:
            return
        # 语音链同期回落也附带上（不单独告警，避免与 avatar_voice 告警重复，只做上下文）
        voice_fb = {}
        try:
            from src.inbox.voice_autosend import metrics_snapshot as _vsnap
            vr = (_vsnap().get("fallback_reasons") or {})
            voice_fb = {k: int(vr.get(k) or 0)
                        for k in ("7852_unready", "edge_rejected") if vr.get(k)}
        except Exception:
            voice_fb = {}
        self._emit_media_promise_alert(
            net_retracted=int(self._promise_bad),
            promise_retracted=cur_ret,
            promise_fulfilled=cur_ful,
            voice_fallback=voice_fb,
            reminder=bool(self._promise_alerted),
        )
        self._promise_alerted = True
        self._promise_last_remind = ts
        self.total_media_promise_alerts += 1

    def _emit_media_promise_alert(
        self, *, net_retracted: int, promise_retracted: int,
        promise_fulfilled: int, voice_fallback: Dict[str, Any],
        reminder: bool,
    ) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("media_promise_alert", {
                "net_retracted": int(net_retracted),
                "promise_retracted": int(promise_retracted),
                "promise_fulfilled": int(promise_fulfilled),
                "voice_fallback": dict(voice_fallback or {}),
                "reminder": bool(reminder),
                "recovered": False,
                # 独立限流键：首提/重提不与其他事件挤 1h 窗
                "rate_key": "media_promise:remind",
            })
            logger.warning(
                "HealthWatchdog 发出媒体承诺未兑现告警：净撤回=%d（累计撤回=%d 兑现=%d）",
                net_retracted, promise_retracted, promise_fulfilled)
        except Exception:
            logger.debug("media_promise alert 发布失败（已忽略）", exc_info=True)

    def _emit_media_promise_recovery(self) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("media_promise_alert", {
                "recovered": True,
                "rate_key": "media_promise:recovered",
            })
            logger.info("HealthWatchdog 发出媒体承诺兑现恢复通知")
        except Exception:
            logger.debug("media_promise recovery 发布失败（已忽略）", exc_info=True)

    def _check_cloud_balance(self, *, now: Optional[float] = None) -> None:
        """云端余额水位巡检：主 Key + 备用池全部 DeepSeek 凭证，低于阈值 → 主机告警。

        备用 Key 悄悄欠费/过期是最阴的坑（等主 Key 挂了才发现备用也是空的），
        故逐 key 巡检、逐 key 告警（host_alert 按 provider 名独立去抖）。
        配置 ``ops.cloud_credentials.{enabled,balance_warn_cny,probe_interval_sec,
        remind_sec}``（默认关）。稀疏节流按 probe_interval_sec（默认 1h）；低水位
        重提冷却由 host_alert 按 remind_sec（默认 6h）去抖——充值恢复后自然停。
        余额接口 401/403 = 该 key 本身已坏 → 走 key 失效告警口径。
        网络不可达保持静默（主链熔断有自己的告警，这里探不到≠余额有事）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        from src.utils.cloud_credentials import collect_cloud_balances, credentials_config
        cc = credentials_config(cfg)
        if not cc["enabled"]:
            return
        ts = float(now if now is not None else time.time())
        if self._last_cloud_balance_ts and (ts - self._last_cloud_balance_ts) < cc["probe_interval_sec"]:
            return
        self._last_cloud_balance_ts = ts
        for summary in collect_cloud_balances(cfg):
            status = str(summary.get("status") or "")
            if status == "low":
                from src.utils.host_alert import notify_balance_low
                if notify_balance_low(
                    str(summary.get("provider") or "DeepSeek"),
                    float(summary.get("balance") or 0.0),
                    float(summary.get("threshold") or 0.0),
                    str(summary.get("currency") or "CNY"),
                    cooldown_sec=float(summary.get("remind_sec") or 21600.0),
                ):
                    self.total_cloud_balance_alerts += 1
            elif status == "auth_failed":
                from src.utils.host_alert import notify_key_failure
                if notify_key_failure(
                    str(summary.get("provider") or "DeepSeek"),
                    f"余额接口鉴权失败（{summary.get('error') or 'HTTP 401'}），Key 可能已失效",
                ):
                    self.total_cloud_balance_alerts += 1

    def _check_pool_key_pings(self, *, now: Optional[float] = None) -> None:
        """备用 Key 主动探活：每日一轮 1-token chat ping（节流在 run_chat_pings 内）。

        - key 层拒绝（401/402/403）→ key 失效告警（按 key 名独立去抖）；
        - 端点通但持续 4xx/5xx（模型名错/端点装错）→ 同样告警（探活的意义就在这）；
        - 网络不可达 → 静默（可能整网问题，主链熔断有自己的告警）。
        配置 ``ops.cloud_credentials.chat_ping.{enabled,interval_sec}``（随父开关）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        from src.utils.cloud_credentials import run_chat_pings
        for r in run_chat_pings(cfg, now=now):
            if r.get("ok"):
                continue
            if not r.get("reachable"):
                continue  # 网络层问题不按 key 坏处理
            from src.utils.host_alert import notify_key_failure
            if notify_key_failure(
                f"备用:{r.get('name')}",
                f"备用 Key 探活失败（{r.get('error') or 'HTTP ' + str(r.get('http_status'))}）"
                "——主 Key 故障时该备用 Key 将无法顶班，请尽快更换",
                cooldown_sec=21600.0,
            ):
                self.total_cloud_balance_alerts += 1

    def _check_local_fallback_duty(self, *, now: Optional[float] = None) -> None:
        """本地兜底「长期顶班」升级提醒：云端挂了的瞬时弹窗（熔断开路）只发一次；
        若兜底持续出话 ``after_min`` 分钟仍未恢复 → 再提醒，之后每 ``interval_min`` 一条。

        判定口径＝兜底出话计数增量（``AIClient.get_stats().local_fallback_calls``）：
        两个巡检周期间有增量 = 云端仍不可用且有真实流量在被兜底扛着。连续 2 个无增量
        周期（约 10 分钟无兜底出话）重置顶班计时——宁可少弹不多弹（无流量期误重置的
        代价只是下次重新起算 after_min）。配置
        ``health_watchdog.fallback_duty_remind.{enabled,after_min,interval_min}``（默认开）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        fr = (((cfg.get("health_watchdog") or {}).get("fallback_duty_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not fr.get("enabled", True):
            return
        state = getattr(self._app, "state", self._app)
        ai = getattr(state, "ai_client", None)
        if ai is None:
            return
        try:
            calls = int((ai.get_stats() or {}).get("local_fallback_calls") or 0)
        except Exception:
            return
        prev = self._fb_duty_last_calls
        self._fb_duty_last_calls = calls
        if prev is None:
            return  # 首个周期只建基线
        ts = float(now if now is not None else time.time())
        if calls > prev:
            self._fb_duty_idle_ticks = 0
            if not self._fb_duty_since_ts:
                self._fb_duty_since_ts = ts
            after_sec = max(60.0, float(fr.get("after_min", 30) or 30) * 60.0)
            if ts - self._fb_duty_since_ts >= after_sec:
                duty_min = int((ts - self._fb_duty_since_ts) // 60)
                from src.utils.host_alert import notify_host
                if notify_host(
                    "云端 AI 仍由本地兜底顶班",
                    (f"云端主模型已持续不可用约 {duty_min} 分钟，本地兜底模型仍在代答"
                     f"（累计出话 {calls} 次）。用户对话未中断，但请尽快恢复云端（检查网络/Key/余额）。"),
                    key="fallback_duty",
                    cooldown_sec=max(600.0, float(fr.get("interval_min", 240) or 240) * 60.0),
                ):
                    self.total_fallback_duty_reminders += 1
        else:
            self._fb_duty_idle_ticks += 1
            if self._fb_duty_idle_ticks >= 2:
                self._fb_duty_since_ts = 0.0

    def _check_orchestrator_workers(self) -> None:
        """编排器受管 worker 崩溃巡检（P6）：某账号 protocol/web worker 进入 ``error`` 态
        （编排器退避重试仍未恢复）→ 经 EventBus 主动外发，而非只在 ops 看板可见（P2-b）。

        **自然门控**：编排器未运行/无受管账号即静默（RPA-only、编排器未启用零误报）。
        去抖：按 ``(worker, severity)`` 签名，仅错误集合/严重度变化时 emit；恢复补发一次。
        严重度：``restarts>=3`` → red(fail，真实掉线)，否则 yellow(warn，瞬时抖动/重连)。
        """
        try:
            from src.integrations.account_orchestrator import (
                get_orchestrator_if_running,
            )
            from src.utils.ops_overview import orchestrator_worker_problems
            orch = get_orchestrator_if_running()
            if orch is None:
                return
            status = orch.status()
        except Exception:
            return
        # 无受管账号：若此前报过异常，静默 reconcile 掉遗留 open 事件
        if int((status or {}).get("total") or 0) <= 0:
            if self._last_orch_worker_sig:
                self._emit_orchestrator_worker_recovery()
                self._last_orch_worker_sig = None
            return

        problems = orchestrator_worker_problems(status)
        light = ("red" if any(p["status"] == "fail" for p in problems)
                 else ("yellow" if problems else "green"))
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        if problems:
            if sig != self._last_orch_worker_sig:
                self._emit_orchestrator_worker_alert(problems, light)
                self.total_orchestrator_worker_alerts += 1
            self._last_orch_worker_sig = sig
        else:
            if self._last_orch_worker_sig:
                self._emit_orchestrator_worker_recovery()
            else:
                # 进程刚起且当前无异常：静默 reconcile 遗留 open 事件（重启后签名空）
                inbox = self._inbox()
                if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                    try:
                        n = inbox.resolve_open_incidents(
                            kind="orchestrator_worker") or 0
                        if n:
                            logger.info(
                                "HealthWatchdog 启动 reconcile：关闭遗留编排器 worker 事件 %d 条",
                                n)
                    except Exception:
                        logger.debug("编排器 worker 事件 reconcile 失败（已忽略）",
                                     exc_info=True)
            self._last_orch_worker_sig = None

    def _emit_orchestrator_worker_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="orchestrator_worker",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("编排器 worker 事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("orchestrator_worker_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出编排器 worker 告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("orchestrator_worker_alert 发布失败（已忽略）", exc_info=True)

    def _emit_orchestrator_worker_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="orchestrator_worker")
        except Exception:
            logger.debug("编排器 worker 事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("orchestrator_worker_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出编排器 worker 恢复通知")
        except Exception:
            logger.debug("orchestrator_worker recovery 发布失败（已忽略）", exc_info=True)

    def _skill_manager(self):
        return getattr(getattr(self._app, "state", self._app), "skill_manager", None)

    def _check_memory_key_drift(self, *, now: Optional[float] = None) -> None:
        """记忆 key 漂移巡检：裸 key（无 ``platform:`` 前缀）复发即告警。

        一次性迁移（:mod:`src.utils.episodic_key_migration`）清存量后，若某入口又漏传
        platform，记忆会重新落到裸 key、对收件箱引擎不可见 → 静默拉低命中率。本巡检
        让漂移**自我守护**：``bare_keys`` 超阈即发 ``memory_key_drift`` 事件（可恢复）。

        结构性数据（key 集合慢变），故独立稀疏节流（默认 1h）；阈值见
        ``inbox.auto_draft.key_drift_alert``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        kd = (((cfg.get("inbox") or {}).get("auto_draft") or {}).get("key_drift_alert")
              or {}) if isinstance(cfg, dict) else {}
        if not kd.get("enabled", True):
            return
        ts = float(now if now is not None else time.time())
        interval = max(self._interval, float(kd.get("interval_sec", 3600)))
        if self._last_drift_check_ts and (ts - self._last_drift_check_ts) < interval:
            return
        sm = self._skill_manager()
        if sm is None or not hasattr(sm, "episodic_key_health"):
            return
        try:
            health = sm.episodic_key_health(sample=5)
        except Exception:
            return
        if not health.get("enabled"):
            return
        self._last_drift_check_ts = ts

        bare = int(health.get("bare_keys") or 0)
        bare_max = int(kd.get("bare_keys_max", 0))
        bare_severe = int(kd.get("bare_keys_severe", 50))
        problems: List[Dict[str, Any]] = []
        if bare > bare_max:
            samples = ", ".join(
                str(s.get("key")) for s in (health.get("bare_samples") or [])[:5]
            )
            problems.append({
                "id": "memory_key_drift", "name": "记忆 key 漂移",
                "status": "fail" if bare >= bare_severe else "warn",
                "detail": (
                    f"检测到 {bare} 个裸 key（无 platform 前缀，含 "
                    f"{int(health.get('bare_facts') or 0)} 条事实）对收件箱引擎不可见 → "
                    f"拉低命中率；样例: {samples}。"
                    "可运行 src.utils.episodic_key_migration 并入 canonical key"
                ),
            })

        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        light = "red" if any(p["status"] == "fail" for p in problems) else "yellow"
        if problems:
            if sig != self._last_drift_sig:
                self._emit_memory_key_drift_alert(problems, light)
                self.total_memory_key_drift_alerts += 1
            self._last_drift_sig = sig
        else:
            if self._last_drift_sig:
                self._emit_memory_key_drift_recovery()
            self._last_drift_sig = None

    def _emit_memory_key_drift_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="memory_key_drift",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("记忆 key 漂移事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("memory_key_drift_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出记忆 key 漂移告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("memory_key_drift_alert 发布失败（已忽略）", exc_info=True)

    def _emit_memory_key_drift_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="memory_key_drift")
        except Exception:
            logger.debug("记忆 key 漂移事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("memory_key_drift_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出记忆 key 漂移恢复通知")
        except Exception:
            logger.debug("memory_key_drift recovery 发布失败（已忽略）", exc_info=True)

    def _maybe_purge_incidents(self, *, now: Optional[float] = None) -> int:
        if self._retention_days <= 0:
            return 0
        ts = float(now if now is not None else time.time())
        if self._last_purge_ts and (ts - self._last_purge_ts) < self._purge_interval:
            return 0
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "purge_resolved_incidents"):
            return 0
        self._last_purge_ts = ts
        cutoff = ts - self._retention_days * 86400.0
        n = inbox.purge_resolved_incidents(cutoff)
        if n:
            logger.info("HealthWatchdog 清理已关闭运维事件 %d 条（保留 %.0f 天）",
                        n, self._retention_days)
        return n

    def _build_weekly_report(self, *, days: int = 7, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """无 request 装配运营周报：事件统计 + 自动化价值 + 计费 + 环比上周。

        ROI 的「经营/首响」段需 request（依赖 _daily_report_rows），watchdog 取不到，
        故周报以「运维 + 自动化 + 计费」为主，business 段从缺（build_ops_report 优雅降级）。
        """
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "get_incident_stats"):
            return None
        from src.utils.ops_intel import automation_value, build_ops_report, weekly_compare

        ts = float(now if now is not None else time.time())
        span_sec = days * 86400.0
        since = ts - span_sec
        prev_since = since - span_sec

        config = getattr(self._config_manager, "config", None) or {}
        roi_cfg = ((config.get("workspace") or {}).get("roi") or {})
        sec_per_reply = int(roi_cfg.get("sec_per_reply") or 180)
        cost_per_hour = float(roi_cfg.get("cost_per_hour") or 0)

        def _roi_for(since_ts: float, until_ts: Optional[float]) -> Dict[str, Any]:
            auto_stats = {}
            if hasattr(inbox, "get_automation_roi_stats"):
                try:
                    auto_stats = (inbox.get_automation_roi_stats(since_ts, until_ts=until_ts)
                                  if until_ts is not None
                                  else inbox.get_automation_roi_stats(since_ts))
                except TypeError:
                    auto_stats = inbox.get_automation_roi_stats(since_ts)
                except Exception:
                    logger.debug("自动化统计失败（已忽略）", exc_info=True)
            return {"automation": automation_value(
                auto_stats, sec_per_reply=sec_per_reply, cost_per_hour=cost_per_hour)}

        cur_inc = inbox.get_incident_stats(since)
        prev_inc = inbox.get_incident_stats(prev_since, until_ts=since)
        cur_roi = _roi_for(since, None)
        prev_roi = _roi_for(prev_since, since)
        billing = self._compute_statement()

        # weekly_compare 只读 incidents.total 与 automation 几个键，故用轻量 view 即可，
        # 避免为算环比额外整套 build_ops_report（构建从 3 次降到 1 次）。
        compare = weekly_compare(
            {"incidents": {"total": cur_inc.get("total")}, "automation": cur_roi["automation"]},
            {"incidents": {"total": prev_inc.get("total")}, "automation": prev_roi["automation"]},
        )
        return build_ops_report(days=days, incident_stats=cur_inc, roi=cur_roi,
                                billing=billing, compare=compare)

    def _maybe_weekly_report(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self._weekly_enabled:
            return None
        ts = float(now if now is not None else time.time())
        if self._last_weekly_ts and (ts - self._last_weekly_ts) < self._weekly_interval:
            return None
        report = self._build_weekly_report(now=ts)
        if report is None:
            return None
        self._last_weekly_ts = ts
        self.total_weekly_reports += 1
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ops_report", report)
            logger.info("HealthWatchdog 发出运营周报（事件 %d 起）",
                        (report.get("incidents") or {}).get("total", 0))
        except Exception:
            logger.debug("ops_report 发布失败（已忽略）", exc_info=True)
        return report

    def _inbox(self):
        return getattr(getattr(self._app, "state", self._app), "inbox_store", None)

    def _emit_alert(self, health: Dict[str, Any]) -> None:
        problems = problems_of(health)
        # E2：先落表为运维事件（按健康签名去重 open/update），可追踪到处理人。
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="health",
                    signature=health_signature(health),
                    light=str(health.get("light") or ""),
                    summary=health.get("summary") or {},
                    problems=problems,
                )
        except Exception:
            logger.debug("运维事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("health_alert", {
                "light": health.get("light"),
                "problems": problems,
                "summary": health.get("summary"),
                "recovered": False,
            })
            logger.warning("HealthWatchdog 发出健康告警：light=%s 异常 %d 项",
                           health.get("light"), len(problems))
        except Exception:
            logger.debug("health_alert 发布失败（已忽略）", exc_info=True)

    def _emit_recovery(self, health: Dict[str, Any]) -> None:
        # E2：健康恢复时把未关闭的「健康」事件标 resolved（不动计费事件）。
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="health")
        except Exception:
            logger.debug("运维事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("health_alert", {
                "light": "green", "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出恢复通知")
        except Exception:
            logger.debug("health recovery 发布失败（已忽略）", exc_info=True)

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "interval_sec": self._interval,
            "alert_on_warn": self._alert_on_warn,
            "total_alerts": self.total_alerts,
            "total_recoveries": self.total_recoveries,
            "total_billing_alerts": self.total_billing_alerts,
            "total_draft_quality_alerts": self.total_draft_quality_alerts,
            "total_ai_quality_alerts": self.total_ai_quality_alerts,
            "total_realtime_voice_alerts": self.total_realtime_voice_alerts,
            "total_orchestrator_worker_alerts": self.total_orchestrator_worker_alerts,
            "total_memory_key_drift_alerts": self.total_memory_key_drift_alerts,
            "total_weekly_reports": self.total_weekly_reports,
            "total_cloud_balance_alerts": self.total_cloud_balance_alerts,
            "total_fallback_duty_reminders": self.total_fallback_duty_reminders,
            "total_avatar_voice_reminders": self.total_avatar_voice_reminders,
            "total_media_promise_alerts": self.total_media_promise_alerts,
            "last_check_ts": self.last_check_ts,
            "last_light": self.last_light,
        }
