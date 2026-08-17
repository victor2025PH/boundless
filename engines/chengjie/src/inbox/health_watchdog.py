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


def _sla_backlog(state) -> Optional[Dict[str, Any]]:
    """SLAWatcher 越线快照（供健康灯的「严重超时」组件）。未挂载/取不到 → None。"""
    sw = getattr(state, "sla_watcher", None)
    if sw is None or not hasattr(sw, "status_snapshot"):
        return None
    try:
        snap = sw.status_snapshot() or {}
    except Exception:
        logger.debug("SLAWatcher 快照失败（已忽略）", exc_info=True)
        return None
    return {
        "breaching_now": snap.get("breaching_now"),
        "max_wait_min": snap.get("max_wait_min"),
        "escalating_now": snap.get("escalating_now"),
        "unclaimed_escalating": snap.get("unclaimed_escalating"),
    }


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


def _is_private_host(host: str) -> bool:
    """RFC1918 私网/本机判定（探活只认自建 LAN 算力，公网云端点不探防误报）。"""
    import re as _re
    return (host in ("localhost", "127.0.0.1")
            or host.startswith("192.168.") or host.startswith("10.")
            or bool(_re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)))


def lan_gpu_probe_targets(config: Dict[str, Any]) -> List[str]:
    """收集要巡检的 LAN GPU 主机根地址（纯函数：按主机去重、只认私网）。

    口径＝「聊天主链之外、挂了会**静默降级**的 LAN 算力依赖」：嵌入双活
    （ai.embedding_base_urls/embedding_base_url）、本地兜底 LLM（ai.fallback，
    enabled 才算）、视觉双活（vision.base_urls/base_url）、本地 MT
    （translation.engines.ollama_mt.base_urls/base_url）。多个子系统常指向同一台
    主机（176/140）——按 ``scheme://host:port`` 去重后逐台探 Ollama ``/api/version``
    （零模型加载、毫秒级）。公网端点（云 API）不收：无该契约且自有告警面；
    云端部署无 LAN 端点 → 空清单 → 巡检天然静默。
    """
    if not isinstance(config, dict):
        return []
    bases: List[str] = []
    ai = config.get("ai") or {}
    emb = ai.get("embedding_base_urls") or ai.get("embedding_base_url") or []
    if isinstance(emb, str):
        emb = [u.strip() for u in emb.split(",")]
    bases += [str(u or "") for u in (emb if isinstance(emb, (list, tuple)) else [])]
    fb = ai.get("fallback") or {}
    if isinstance(fb, dict) and fb.get("enabled"):
        bases.append(str(fb.get("base_url") or ""))
    vis = config.get("vision") or {}
    vb = vis.get("base_urls") or vis.get("base_url") or []
    if isinstance(vb, str):
        vb = [vb]
    bases += [str(u or "") for u in (vb if isinstance(vb, (list, tuple)) else [])]
    mt = (((config.get("translation") or {}).get("engines") or {})
          .get("ollama_mt") or {})
    if isinstance(mt, dict):
        mb = mt.get("base_urls") or mt.get("base_url") or []
        if isinstance(mb, str):
            mb = [u.strip() for u in mb.split(",")]
        bases += [str(u or "") for u in (mb if isinstance(mb, (list, tuple)) else [])]

    out: List[str] = []
    seen = set()
    for b in bases:
        b = b.strip().rstrip("/")
        if not b or "://" not in b:
            continue
        if b.endswith("/v1"):
            b = b[:-3].rstrip("/")
        hostport = b.split("://", 1)[1].split("/", 1)[0]
        if not _is_private_host(hostport.split(":", 1)[0]):
            continue
        root = b.split("://", 1)[0] + "://" + hostport
        if root not in seen:
            seen.add(root)
            out.append(root)
    return out


def probe_lan_gpu_host(root: str, *, timeout: float = 3.0) -> Dict[str, Any]:
    """探一台 LAN GPU 主机的 Ollama ``/api/version``（无缓存，调用方自控频率）。"""
    result: Dict[str, Any] = {"url": root, "reachable": False}
    try:
        import urllib.request
        t0 = time.time()
        with urllib.request.urlopen(root + "/api/version", timeout=timeout) as resp:
            resp.read()
        result.update({"reachable": True,
                       "latency_ms": int((time.time() - t0) * 1000)})
    except Exception as e:
        result["error"] = str(e)[:120]
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
        sla_backlog=_sla_backlog(state),
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
        # 外部 API 版本临期巡检（2026-08-03）：CI 门禁只在**有人提交代码**时跑，
        # 而这类失效是**日历驱动**的——仓库几个月没动，版本照样走向死亡。
        # v19.0 死了 74 天无人知，正是因为没有任何东西按日历看着它。
        self._last_api_version_ts: float = 0.0
        self._apiver_alerted: bool = False
        self.total_api_version_alerts: int = 0
        # 试用领取兑现巡检（P2）：只在「本地有未兑现的单」时才真跑，正常部署零开销
        self._last_trial_poll_ts: float = 0.0
        # 授权字符额度水位巡检（P4c）：稀疏节流 + 「告过警」标记（恢复通知只发给告过警的）
        self._last_license_quota_ts: float = 0.0
        self._license_quota_alerted: bool = False
        # 本地兜底顶班提醒：上次观测的兜底出话累计数 + 首次观测到顶班的时间
        self._fb_duty_last_calls: Optional[int] = None
        self._fb_duty_since_ts: float = 0.0
        self._fb_duty_idle_ticks: int = 0
        # AvatarHub 7852 持续掉线升级提醒：首次探测到掉线的时刻 + 已首提标记 + 上次重提时刻
        self._avatar_down_since: float = 0.0
        self._avatar_alerted: bool = False
        self._avatar_last_remind: float = 0.0
        # 履约端（厂商机签发链单点）停摆升级提醒：首次不健康时刻 + 已首提 + 上次重提 + 种类
        self._fulfiller_bad_since: float = 0.0
        self._fulfiller_alerted: bool = False
        self._fulfiller_last_remind: float = 0.0
        self._fulfiller_kind: str = ""
        # 告警种类：down=不可达/未载入（health 探测红）；hang=半死（health 绿但合成连败，
        # 2026-07-14 事故形态）。恢复语义不同：hang 需要「失败后真的又成过一次」的正面证据。
        self._avatar_alert_kind: str = ""
        # LAN GPU 主机宕机升级提醒（2026-08-01 176 整机静默下线两小时事故）：
        # 每主机独立状态 {down_since, alerted, last_remind}——故障转移网兜住了业务，
        # 但运维必须知道冗余已经归零。
        self._lan_gpu_state: Dict[str, Dict[str, float]] = {}
        self.total_lan_gpu_reminders: int = 0
        # 口语化 LLM 持续连败升级提醒（2026-07-15 九连败静默事故）：状态镜像 avatar hang
        self._colloquial_down_since: float = 0.0
        self._colloquial_alerted: bool = False
        self._colloquial_last_remind: float = 0.0
        # 缺口自动入库：稀疏节流（默认 1h 一轮）+ 每日预算
        self._last_auto_stock_ts: float = 0.0
        self._auto_stock_day: str = ""
        self._auto_stock_added_today: int = 0
        # 相册场景缺口自动补货（P2 图文一致性）：同款节流+预算，写计划文件
        self._last_media_restock_ts: float = 0.0
        self._media_restock_day: str = ""
        self._media_restock_added_today: int = 0
        # 人工通过投递链静默断裂巡检（2026-07-29）：观测窗起点 = 本进程起来的时刻
        # （worker 计数器是进程内的，必须与之同窗口才可比），+ 首提/重提去抖。
        self._hd_since_ts: float = time.time()
        self._hd_bad_since: float = 0.0
        self._hd_alerted: bool = False
        self._hd_last_remind: float = 0.0
        self.total_human_deliver_alerts: int = 0
        # 草稿积压无人处理巡检（2026-07-29）：SLA watcher 只盯 L3/L4，**L1 是盲区**
        # （见 _check_draft_backlog），而 L1 恰恰是「必须人来处理」的那一档。
        self._db_alerted: bool = False
        self._db_last_remind: float = 0.0
        self.total_draft_backlog_alerts: int = 0
        # CSRF 写请求拦截激增巡检（P1 2026-07-31）：中间件拒绝已进 csrf_stats 看板，
        # 但看板要有人开才有用——窗口增量达阈值主动外发。样本=(ts,total,by_kind,by_path)。
        self._csrf_samples: List[tuple] = []
        self._csrf_alerted: bool = False
        self._csrf_last_remind: float = 0.0
        # 出站投递健康（滚动窗样本 + 升级式提醒状态）
        self._outbound_samples: List[tuple] = []
        self._outbound_alerted: bool = False
        self._outbound_last_remind: float = 0.0
        self.total_csrf_reject_alerts: int = 0
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
        self.total_license_quota_alerts: int = 0
        self.total_fallback_duty_reminders: int = 0
        self.total_avatar_voice_reminders: int = 0
        self.total_trial_fulfiller_reminders: int = 0
        self.total_media_promise_alerts: int = 0
        self.total_auto_stocked: int = 0
        self.total_media_restock_planned: int = 0
        self.total_tg_call_reminders: int = 0
        self.total_colloquial_llm_reminders: int = 0
        # 原生通话主机升级式提醒状态（镜像 avatar_voice 那套）
        self._tgcall_down_since: float = 0.0
        self._tgcall_alerted: bool = False
        self._tgcall_last_remind: float = 0.0
        # 日志巡检（triage）：稀疏节流（默认 6h 一轮）；只在出现新错误类/激增时经
        # host_alert 外发。默认关（新子系统约定）。
        self._last_triage_ts: float = 0.0
        self.total_log_triage_alerts: int = 0
        # 跨平台身份影子周期扫描（P3.2）：内存节流 ts（冷启动采纳 state 文件里的
        # last_scan_ts——重启不重扫）+ 成功扫描计数。默认关（contacts.identity_shadow）。
        self._ishadow_last_ts: float = 0.0
        self.total_identity_shadow_scans: int = 0
        # 官网订单拉取回流（goals order_pull）：稀疏节流 + 结算计数。默认关。
        self._last_goal_pull_ts: float = 0.0
        self.total_goal_orders_settled: int = 0
        # 流失挽回扫描（goals winback）：小时级节流。默认关。
        self._last_goal_winback_ts: float = 0.0
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
        # LAN GPU 主机宕机升级提醒（嵌入/视觉/兜底 LLM/本地 MT 所在主机整机下线时，
        # 各链路静默转移备点——业务不断，但冗余归零必须有人知道）。
        try:
            self._check_lan_gpu_hosts()
        except Exception:
            logger.debug("LAN GPU 主机巡检异常（已忽略）", exc_info=True)

        # 人工通过投递链静默断裂（坐席点了「发送」但一条都没真发出去）——
        # 这条链曾整条不存在过（实测 14 天零人工投递），且注入是静默的，值得主动探。
        try:
            self._check_human_deliver_chain()
        except Exception:
            logger.debug("人工投递链巡检异常（已忽略）", exc_info=True)

        # 草稿积压：L1（必须人审那一档）在 SLA 告警里是盲区，实测烂到 214h 无人知
        try:
            self._check_draft_backlog()
        except Exception:
            logger.debug("草稿积压巡检异常（已忽略）", exc_info=True)

        # CSRF 写请求拦截激增：某前端宿主写通道断了（cookie_no_header）或有人在
        # 跨站探测——2026-07 人设切换事故静默烂了两周，这类信号必须主动轰人
        try:
            self._check_csrf_rejects()
        except Exception:
            logger.debug("CSRF 拦截巡检异常（已忽略）", exc_info=True)

        # 出站投递不健康：看板已有数，但「看板要有人开才有用」——链路故障 /
        # 闸门被关着 / RPA 队列只进不出，三类各自主动外发（业务性 403、24h 窗口
        # 过期刻意不轰人，见 outbound_delivery_health 模块 docstring）。
        try:
            self._check_outbound_delivery()
        except Exception:
            logger.debug("出站投递巡检异常（已忽略）", exc_info=True)

        # 试用履约端（厂商机）停摆：客服台心跳卡是被动的，得有人去看；这里升级为主动外发。
        try:
            self._check_trial_fulfiller()
        except Exception:
            logger.debug("试用履约端巡检异常（已忽略）", exc_info=True)

        # 原生通话主机（brain=s2s 复用 MiniCPM-o 176:7860）持续不可用升级提醒：掉了=打进来
        # 的电话全接不了（陪护"她会接电话"卖点静默失效），比看板黄灯更该主动轰人。
        try:
            self._check_native_call()
        except Exception:
            logger.debug("原生通话主机巡检异常（已忽略）", exc_info=True)

        # 口语化 LLM 持续连败升级提醒（2026-07-15 事故）：端点挂掉只会 60s 熔断-重试
        # 无限循环，语音口语化静默降级规则档，无人知晓——超阈值主动外发。
        try:
            self._check_colloquial_llm()
        except Exception:
            logger.debug("口语化 LLM 巡检异常（已忽略）", exc_info=True)

        # 备货缺口自动入库（Phase5）：高频短句缺口达标自动进台词库（守卫+每日预算），
        # 夜间计划任务渲染兜底——「看缺口→补台词」不再需要人。
        try:
            self._check_avatar_auto_stock()
        except Exception:
            logger.debug("缺口自动入库巡检异常（已忽略）", exc_info=True)

        # 相册场景缺口自动补货（P2 图文一致性）：点名场景反复要不到 + 相册零备货
        # → 写补货计划（渲染交夜间 CLI album_restock）——「看缺口→补图」零人工。
        try:
            self._check_media_restock()
        except Exception:
            logger.debug("相册补货巡检异常（已忽略）", exc_info=True)

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

        # 外部 API 版本临期巡检（2026-08-03）：把「供应商公布的死期」按日历盯住，
        # 补上「CI 门禁只在有人提交时才跑」的缺口。健康时完全静默。
        try:
            self._check_api_version_expiry()
        except Exception:
            logger.debug("外部 API 版本巡检异常（已忽略）", exc_info=True)

        # 授权字符额度水位巡检（P4c）：临近/触顶 → host_alert 主动外发并指路
        # 「会员中心 → 兑换加量包」，替代「翻译突然被拦才发现额度没了」。
        # 无授权/不限量部署天然静默（included=0 直接返回），零误报零开销。
        try:
            self._check_license_quota()
        except Exception:
            logger.debug("授权额度巡检异常（已忽略）", exc_info=True)

        # 试用领取兑现巡检（P2）：官网建单后厂商机异步签发，用户早就关掉向导去干活了。
        # 没有这条后台轮询，授权就只在「首启窗口恰好还开着」时才落地——而那个窗口
        # 一辈子只弹一次。这里让它与 UI 彻底解耦：领过单就一直查到激活为止。
        try:
            self._check_trial_claim()
        except Exception:
            logger.debug("试用领取巡检异常（已忽略）", exc_info=True)

        # 日志巡检（2026-07-23）：把 triage_watch 的「基线抑制+偏差检测」接进 in-process
        # 看门狗——出现新错误类/激增时经 host_alert 走既有 EventBus→Webhook→Telegram
        # 外发（比独立计划任务只能落 app.log/弹窗更能触达机主）。默认关、6h 稀疏节流。
        try:
            self._check_log_triage()
        except Exception:
            logger.debug("日志巡检异常（已忽略）", exc_info=True)

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

        # 跨平台身份影子周期扫描（P3.2）：只读发现「疑似同一人」配对，state 落文件
        # 供看板读。默认关；稀疏节流（默认 6h）；异常全吞绝不影响巡检主流程。
        try:
            self._check_identity_shadow()
        except Exception:
            logger.warning("身份影子扫描巡检异常（已忽略）", exc_info=True)

        # 官网订单拉取回流（goals P2 成交闭环）：引擎在 NAT 后收不到官网 push，
        # 主动拉带 ref 的已付款单结算目标 done。默认关；稀疏节流（默认 10min）。
        try:
            self._check_goal_order_pull()
        except Exception:
            logger.debug("官网订单拉取巡检异常（已忽略）", exc_info=True)

        # 流失挽回扫描（goals P6）：留存目标流失冷却后自动起低频挽回目标；
        # 顺手把静默会话的到期目标结算落库。默认关；小时级节流。
        try:
            self._check_goal_winback()
        except Exception:
            logger.debug("流失挽回巡检异常（已忽略）", exc_info=True)

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

    @staticmethod
    def _session_expected_online(key: str) -> bool:
        """会话键（``platform:account_id``）对应的账号是否仍被期望在线。

        判据取注册表 ``status``（持久事实——不受 Node push 与本地登出操作的到达顺序
        影响）：只有 ``online`` 才是「编排器会去拉起、掉了就该有人修」的号。已登出
        （offline）/已删除（无记录）的号编排器根本不会拉起（``desired_accounts`` 只收
        online），催也无从下手。注册表读不出来 → 返回 True，不因巡检自身故障漏报真掉线。
        """
        plat, _, acct = key.partition(":")
        if not plat or not acct:
            return True
        try:
            from src.integrations.account_registry import get_account_registry
            row = get_account_registry().get(plat, acct)
        except Exception:
            return True
        return bool(row) and str(row.get("status") or "") == "online"

    def _check_platform_sessions(self, *, now: Optional[float] = None) -> None:
        """平台会话「持续不健康」提醒（P4，闭环 P0-2 的告警时效性）。

        掉线转移告警（session-status 路由发）只发一次；若会话掉线 ``after_min``
        分钟仍未恢复 → 补一条「还没人修」提醒，之后每 ``interval_min`` 分钟一条。
        节流状态在 ``PlatformSessionHealth`` 内（恢复自动清零），本方法无自有状态。
        配置：``health_watchdog.session_stale_remind.{enabled,after_min,interval_min}``
        （默认开，30min 首提 / 4h 重提；notifier 每小时限流是第二层兜底）。

        只催**期望在线**的号（``_session_expected_online``）：运营在坐席里主动登出/
        删除后，Node 微服务会 push 一条 ``logged_out``——那是不健康态（发送前快速失败
        要用），但**不是故障**，催人去「修」一个自己刚下线的账号只会让人不再信这个告警。
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
        due = {k: v for k, v in due.items() if self._session_expected_online(k)}
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

    #: `decided_by` 里属「系统自动」的值——统计人工通过时必须排除
    _HD_SYSTEM_DECIDERS = frozenset({
        "autosend_worker", "mode_downgraded", "send_blocked", "stale_peer",
        "system", "composer_adopt", "",
    })

    def human_approved_since(self, since_ts: float, *, limit: int = 500) -> int:
        """统计 ``since_ts`` 之后**由人**通过的 inbox 草稿数（纯读，异常返回 0）。

        为什么不查 ``draft_audit_log``：``resolve_with_audit`` 只在 L3/L4 或 autosend
        动作时写审计行——**L1/L2 的人工通过根本不落审计**（实测：试聊草稿是 L1，
        人工 approve 后审计里查不到）。而 ``reply_drafts`` 行每次 resolve 都会更新
        ``status``/``decided_by``/``decided_at``，是唯一完整的人工处置事实源。
        """
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "list_drafts"):
            return 0
        try:
            rows = inbox.list_drafts(
                source_kind="inbox", status="approved", limit=limit) or []
        except Exception:
            logger.debug("human_approved_since 读草稿失败（忽略）", exc_info=True)
            return 0
        n = 0
        for r in rows:
            try:
                if float(r.get("decided_at") or 0) < since_ts:
                    continue
                if str(r.get("decided_by") or "").strip() in self._HD_SYSTEM_DECIDERS:
                    continue
                n += 1
            except Exception:
                continue
        return n

    #: 每 tick 最多为多少条超龄草稿查「是否已回过」（各查一次会话消息）。超出部分
    #: 保守算作「客户在等」——宁可多报不漏报。积压通常个位数，够用且不压 DB。
    _BACKLOG_REPLY_PROBE_CAP = 50

    def _check_draft_backlog(self, *, now: Optional[float] = None) -> None:
        """待审草稿长期无人处理 → 主动轰人（补 SLA 告警的 **L1 盲区**）。

        为什么需要它（2026-07-29 实测）：生产待审队列 7 条，年龄最老 **214h（8.9 天）**，
        其中 **6 条是 L1**。而 `SLAWatcher._check_sla_breach` 明确只看 L3/L4
        （`autopilot_level not in ("L3","L4") → continue`）——于是三档里：

          L2（auto_ai+low）→ worker 自动发，没人管也会发出去；
          L3/L4（medium/high）→ 有逐条 SLA 告警；
          **L1（review+low）→ 既不自动发、也没有任何告警** ⇒ 无声烂掉。

        偏偏 L1 是**唯一「必须人来处理」**的那一档，告警洞正好开在最需要人的地方。
        （那条 L3 也烂了 167h，说明还有第二层问题：告警只进工作台铃铛、webhook 默认关。
        本检查走 ops 告警出口，与铃铛互补。）

        刻意做成**聚合**信号而非逐条：L1 是低风险日常稿，逐条告警＝噪音；
        「N 条超过 X 小时无人处理」才是运维该看的（排班/注意力问题，非单条草稿问题）。
        配置 ``health_watchdog.draft_backlog_remind.{enabled,min_age_hours,min_count,
        interval_min}``（默认开：≥3 条超 24h 触发，每 4h 重提，清空自动补恢复通知）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("draft_backlog_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        svc = getattr(getattr(self._app, "state", self._app), "draft_service", None)
        if svc is None or not hasattr(svc, "list_drafts"):
            return
        min_age_h = max(1.0, float(br.get("min_age_hours", 24) or 24))
        min_count = max(1, int(br.get("min_count", 3) or 3))
        ts = float(now if now is not None else time.time())
        cutoff = ts - min_age_h * 3600.0
        try:
            rows = svc.list_drafts(status="pending", limit=500) or []
        except Exception:
            logger.debug("草稿积压巡检取数失败（忽略）", exc_info=True)
            return

        aged: List[Dict[str, Any]] = []
        for d in rows:
            try:
                created = float(d.get("created_ts") or 0)
            except (TypeError, ValueError):
                continue
            if created > 0 and created < cutoff:
                aged.append(d)

        # 把「账目残留」与「客户真的在等」分开：坐席走「采用文案→手动发送」时
        # 发送路由不处置草稿行，那行会一直 pending。两者处置完全不同（前者清账、
        # 后者要回客户），混成一个数字会让运维对告警失去信任 → 告警按「真的在等」
        # 触发，孤儿数另报。逐条要查一次会话消息，故按 _BACKLOG_REPLY_PROBE_CAP 封顶
        # （超出部分保守算作在等，宁可多报不漏报）。
        stale: List[Dict[str, Any]] = []
        already_replied = 0
        probe_budget = self._BACKLOG_REPLY_PROBE_CAP
        for d in aged:
            if probe_budget > 0 and hasattr(svc, "conversation_replied_after"):
                probe_budget -= 1
                try:
                    if svc.conversation_replied_after(d):
                        already_replied += 1
                        continue
                except Exception:
                    logger.debug("积压巡检判「已回过」失败（按在等计）", exc_info=True)
            stale.append(d)

        if len(stale) < min_count:
            if self._db_alerted and not stale:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("draft_backlog_alert", {
                        "recovered": True,
                        "rate_key": "draft_backlog:recovered",
                    })
                    logger.info("HealthWatchdog 发出草稿积压恢复通知")
                except Exception:
                    logger.debug("draft_backlog recovery 发布失败（忽略）", exc_info=True)
                self._db_alerted = False
                self._db_last_remind = 0.0
            return

        interval_sec = max(600.0, float(br.get("interval_min", 240) or 240) * 60.0)
        if self._db_alerted and ts - self._db_last_remind < interval_sec:
            return

        by_level: Dict[str, int] = {}
        for d in stale:
            lvl = str(d.get("autopilot_level") or "?") or "?"
            by_level[lvl] = by_level.get(lvl, 0) + 1
        oldest_h = 0.0
        for d in stale:
            try:
                oldest_h = max(oldest_h, (ts - float(d.get("created_ts") or ts)) / 3600.0)
            except (TypeError, ValueError):
                continue
        # SLA 逐条告警覆盖不到的那部分（L1 等），单独点名——这才是本检查的存在理由
        uncovered = sum(n for lvl, n in by_level.items() if lvl not in ("L3", "L4"))
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("draft_backlog_alert", {
                "stale_count": len(stale),
                "min_age_hours": min_age_h,
                "oldest_hours": round(oldest_h, 1),
                "by_level": by_level,
                "sla_uncovered": uncovered,
                # 账目残留（内容已人工回过、草稿行没人处置）——与「客户在等」分开报
                "already_replied": already_replied,
                "reminder": bool(self._db_alerted),
                "rate_key": "draft_backlog:remind",
            })
        except Exception:
            logger.debug("draft_backlog alert 发布失败（忽略）", exc_info=True)
            return
        self._db_alerted = True
        self._db_last_remind = ts
        self.total_draft_backlog_alerts += 1
        logger.warning(
            "待审草稿积压：%d 条超过 %.0fh 客户仍在等（最老 %.0fh，分级 %s；"
            "其中 %d 条不在 SLA 逐条告警覆盖内；另有 %d 条已人工回过仅账目残留）",
            len(stale), min_age_h, oldest_h, by_level, uncovered, already_replied)

    def _check_csrf_rejects(self, *, now: Optional[float] = None) -> None:
        """CSRF 写请求拦截在观察窗内激增 → 主动轰人（P1，2026-07-31 事故沉淀）。

        为什么需要它：中间件拒绝已进 csrf_stats（metrics/Prometheus/ops 卡），但
        「人设切换失败」事故的教训正是**看板要有人开才有用**——坐席撞墙两周，
        后台数据全有、没人被通知。两类形态都值得轰人：
          - ``cookie_no_header`` 激增 ＝ 某个前端宿主的写通道又断了（新页面没带凭证/
            补丁被删/客户端回归）——功能事故；
          - ``origin/referer_mismatch``/``bare`` 激增 ＝ 反代配置漂移或有人跨站探测
            ——安全信号。
        判据＝滚动窗增量（样本=(ts,total,by_kind,by_path) 逐 tick 采样）：
        窗口内新增 ≥ ``min_count`` → 首提 + ``interval_min`` 重提；窗口内增量归零
        → 补发恢复通知。基线取窗口内最老样本 → 增量只会**低估不会高估**（不误报）。
        配置 ``health_watchdog.csrf_reject_remind.{enabled,min_count,window_min,
        interval_min}``（默认开：60 分钟窗内 ≥5 次触发，4h 重提）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        cr = (((cfg.get("health_watchdog") or {}).get("csrf_reject_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not cr.get("enabled", True):
            return
        try:
            from src.web.csrf_stats import get_csrf_reject_stats
            d = get_csrf_reject_stats().dump()
        except Exception:
            return
        ts = float(now if now is not None else time.time())
        window_sec = max(300.0, float(cr.get("window_min", 60) or 60) * 60.0)
        min_count = max(1, int(cr.get("min_count", 5) or 5))
        total = int(d.get("total") or 0)
        kinds = dict(d.get("by_kind") or {})
        paths = dict(d.get("by_path") or {})
        self._csrf_samples.append((ts, total, kinds, paths))
        self._csrf_samples = [s for s in self._csrf_samples
                              if ts - s[0] <= window_sec]
        _, base_total, base_kinds, base_paths = self._csrf_samples[0]
        delta = total - base_total

        if delta < min_count:
            if self._csrf_alerted and delta == 0:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("csrf_reject_alert", {
                        "recovered": True,
                        "rate_key": "csrf_reject:recovered",
                    })
                    logger.info("HealthWatchdog 发出 CSRF 拦截恢复通知")
                except Exception:
                    logger.debug("csrf_reject recovery 发布失败（忽略）", exc_info=True)
                self._csrf_alerted = False
                self._csrf_last_remind = 0.0
            return

        interval_sec = max(600.0, float(cr.get("interval_min", 240) or 240) * 60.0)
        if self._csrf_alerted and ts - self._csrf_last_remind < interval_sec:
            return

        # 窗口内的形态/接口增量（负值理论不出现——计数只增；防御性夹 0）
        kind_delta = {k: v - int(base_kinds.get(k, 0))
                      for k, v in kinds.items() if v - int(base_kinds.get(k, 0)) > 0}
        path_delta = {p: v - int(base_paths.get(p, 0))
                      for p, v in paths.items() if v - int(base_paths.get(p, 0)) > 0}
        top_paths = dict(sorted(path_delta.items(),
                                key=lambda kv: (-kv[1], kv[0]))[:3])
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("csrf_reject_alert", {
                "count": delta,
                "window_min": round(window_sec / 60.0),
                "total": total,
                "by_kind": kind_delta,
                "top_paths": top_paths,
                "reminder": bool(self._csrf_alerted),
                "rate_key": "csrf_reject:remind",
            })
        except Exception:
            logger.debug("csrf_reject alert 发布失败（忽略）", exc_info=True)
            return
        self._csrf_alerted = True
        self._csrf_last_remind = ts
        self.total_csrf_reject_alerts += 1
        logger.warning(
            "CSRF 写请求拦截激增：%d 分钟窗内 %d 次（形态 %s；接口 Top %s）",
            round(window_sec / 60.0), delta, kind_delta, top_paths)

    def _check_outbound_delivery(self, *, now: Optional[float] = None) -> None:
        """出站投递不健康 → 主动轰人（把出站看板从「要人开」变成「会来找人」）。

        判定全在纯函数 ``outbound_delivery_health.diagnose`` 里（该模块 docstring
        解释了为什么**不能**用一个扁平的失败率阈值：Discord 403 / 24h 窗口过期这类
        业务性失败每天都有，混进同一个阈值会让告警在一周内被无视）。这里只负责
        采样、去抖、发布。

        三类各自独立、按平台分开：``infra``（有人能修的链路故障）/ ``blocked``
        （闸门被人关着）/ ``stuck_queue``（RPA 队列只进不出）。
        配置 ``health_watchdog.outbound_delivery_remind.{enabled,window_min,
        interval_min,min_infra,min_infra_rate,min_blocked,min_queued_stuck}``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        od = (((cfg.get("health_watchdog") or {}).get("outbound_delivery_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not od.get("enabled", True):
            return
        try:
            from src.integrations.shared.outbound_delivery_health import (
                diagnose, snapshot, summarize, worst_kind,
            )
            from src.integrations.shared.outbound_delivery_stats import (
                get_outbound_delivery_stats,
            )
            snap = snapshot(get_outbound_delivery_stats().dump())
        except Exception:
            return
        ts = float(now if now is not None else time.time())
        window_sec = max(300.0, float(od.get("window_min", 30) or 30) * 60.0)
        self._outbound_samples.append((ts, snap))
        self._outbound_samples = [s for s in self._outbound_samples
                                  if ts - s[0] <= window_sec]
        # 单样本时基线==当前 ⇒ 增量恒 0 ⇒ 不会在刚启动就凭历史累计值误报。
        base = self._outbound_samples[0][1]

        findings = diagnose(
            base, snap,
            min_infra=max(1, int(od.get("min_infra", 5) or 5)),
            min_infra_rate=float(od.get("min_infra_rate", 0.25) or 0.25),
            min_blocked=max(1, int(od.get("min_blocked", 10) or 10)),
            min_queued_stuck=max(1, int(od.get("min_queued_stuck", 5) or 5)),
        )
        if not findings:
            if self._outbound_alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("outbound_delivery_alert", {
                        "recovered": True,
                        "rate_key": "outbound_delivery:recovered",
                    })
                    logger.info("HealthWatchdog 发出出站投递恢复通知")
                except Exception:
                    logger.debug("outbound_delivery recovery 发布失败（忽略）",
                                 exc_info=True)
                self._outbound_alerted = False
                self._outbound_last_remind = 0.0
            return

        interval_sec = max(600.0, float(od.get("interval_min", 60) or 60) * 60.0)
        if self._outbound_alerted and ts - self._outbound_last_remind < interval_sec:
            return

        summary = summarize(findings)
        kind = worst_kind(findings)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("outbound_delivery_alert", {
                "kind": kind,
                "summary": summary,
                "findings": findings[:6],
                "window_min": round(window_sec / 60.0),
                "reminder": bool(self._outbound_alerted),
                # rate_key 带上类别：链路故障与「闸门没打开」是两件事，
                # 挤同一个限流窗会让后来的那类被吞掉。
                "rate_key": f"outbound_delivery:{kind or 'any'}",
            })
        except Exception:
            logger.debug("outbound_delivery alert 发布失败（忽略）", exc_info=True)
            return
        self._outbound_alerted = True
        self._outbound_last_remind = ts
        logger.warning("出站投递异常（%d 分钟窗）：%s",
                       round(window_sec / 60.0), summary)

    def _check_human_deliver_chain(self, *, now: Optional[float] = None) -> None:
        """坐席「通过」了草稿却一条都没真投递出去 → 投递链静默断裂，主动轰人。

        为什么需要它（2026-07-29）：「人工通过 inbox 草稿」这条链曾**整条不存在**——
        坐席点「发送」只把 DB 标成 approved，全库没有任何消费者真发出去，实测 14 天
        零人工投递、坐席以为发了、客户什么也没收到。修复靠 bootstrap 注入
        ``DraftService.set_inbox_deliver_callback``；但**注入本身是静默的**：配置漂移、
        注入抛异常被吞、将来重构漏接线，都会让它悄悄回到「只标记不发送」——
        而坐席端和客户端都看不出区别。

        本检查是那条链的**被动活体探针**（零成本、比周批真发演练更早）：
        同一进程窗口内「有人真的通过过草稿」却「投递计数恒 0 且零失败」＝一次投递
        都没被尝试过 → 链断了。刻意用「零尝试」而非「有失败」：失败已有
        ``autosend_deliver_failed`` 事件覆盖，这里专抓**静默不作为**。

        判据（2026-07-29 升级为两档，接线状态优先）：
          - **未接线**（``DraftService.inbox_deliver_wired`` 为 False）＝确定性根因，
            零流量即可判，且 worker 压根不存在时（``l2_autosend.enabled=false`` ⇒
            worker 不创建）同样成立——这正是最容易断的形态；
          - **已接线但计数恒 0**＝推断性根因（真尝试过会留下成功或失败痕迹）。
        刻意**不再按 ``deliver_enabled`` 静默**：人工投递已与 `l2_autosend.deliver`
        解耦（后者管「AI 可否自己发」），旧闸门会恰好在新能力所在的人审档闭嘴。

        零误报前提（缺一不告警）：
          - ``inbox.auto_draft.human_deliver`` 未被显式关掉（关=运营刻意只标记）；
          - 人工通过数 ≥ ``min_approvals``（默认 3，避免单条巧合）；
          - ``total_human_delivered == 0`` **且** ``total_human_deliver_errors == 0``。
        配置 ``health_watchdog.human_deliver_remind.{enabled,after_min,interval_min,
        min_approvals}``（默认开，30min 首提 / 240min 重提，恢复补发通知）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        hr = (((cfg.get("health_watchdog") or {}).get("human_deliver_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not hr.get("enabled", True):
            return
        # 运营显式选择「通过仅 DB 标记」时接线本就不存在 → 不是故障，绝不告警
        _ad = ((cfg.get("inbox") or {}).get("auto_draft") or {}) if isinstance(cfg, dict) else {}
        if not bool(_ad.get("human_deliver", True)):
            return
        # 与 _inbox() 同款访问：self._app 可能是 app 或已是 state（测试常直接给 state）
        _state = getattr(self._app, "state", self._app)
        worker = getattr(_state, "autosend_worker", None)
        # 接线状态是**比计数器更早**的信号（零流量也能判）。2026-07-29 起人工投递
        # 与 `l2_autosend.deliver` 解耦（deliver 管「AI 可否自己发」，不闸人的决定），
        # 所以这里**不能再按 deliver_enabled 静默**——否则恰好在新能力所在的配置
        # （deliver=false 的人审档）闭嘴。未接线时 worker 可能压根不存在
        # （l2_autosend.enabled=false ⇒ worker 不创建），故 worker=None 也要继续查。
        wired = None
        try:
            _dsvc = getattr(_state, "draft_service", None)
            if _dsvc is not None and hasattr(_dsvc, "inbox_deliver_wired"):
                wired = bool(_dsvc.inbox_deliver_wired)
        except Exception:
            wired = None
        snap: Dict[str, Any] = {}
        if worker is not None and hasattr(worker, "status_snapshot"):
            try:
                snap = worker.status_snapshot() or {}
            except Exception:
                snap = {}
        if wired is None:
            # 接线状态未知（读不到 draft_service，多见于测试/异常态）→ 退回旧的保守
            # 闸门：只有配置本就真发时才据计数推断，宁可漏报不误报。
            if not snap or not snap.get("deliver_enabled"):
                return
        elif wired is False:
            pass            # 明确未接线：下面只要有足够人工通过就是「链断」
        elif not snap:
            return          # 已接线但拿不到计数（worker 形态异常）→ 交由别的巡检
        ts = float(now if now is not None else time.time())
        delivered = int(snap.get("total_human_delivered") or 0)
        errors = int(snap.get("total_human_deliver_errors") or 0)

        if delivered > 0 or errors > 0:
            # 链路已被证明活着（投递成功过，或至少真尝试过并失败——失败另有告警）
            if self._hd_alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("human_deliver_alert", {
                        "recovered": True,
                        "rate_key": "human_deliver:recovered",
                    })
                    logger.info("HealthWatchdog 发出人工投递链恢复通知")
                except Exception:
                    logger.debug("human_deliver recovery 发布失败（忽略）", exc_info=True)
            self._hd_bad_since = 0.0
            self._hd_alerted = False
            self._hd_last_remind = 0.0
            return

        min_appr = max(1, int(hr.get("min_approvals", 3) or 3))
        approved = self.human_approved_since(self._hd_since_ts)
        if approved < min_appr:
            self._hd_bad_since = 0.0    # 还没有足够人工处置 → 不构成证据
            return

        if not self._hd_bad_since:
            self._hd_bad_since = ts
            return
        bad_sec = ts - self._hd_bad_since
        after_sec = max(60.0, float(hr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(hr.get("interval_min", 240) or 240) * 60.0)
        due = (
            (not self._hd_alerted and bad_sec >= after_sec)
            or (self._hd_alerted and ts - self._hd_last_remind >= interval_sec)
        )
        if not due:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("human_deliver_alert", {
                "human_approved": approved,
                "delivered": delivered,
                "deliver_errors": errors,
                "bad_minutes": int(bad_sec // 60),
                "reminder": bool(self._hd_alerted),
                # 未接线=确定性根因（可直接指路配置/接线），计数恒 0=推断性
                "not_wired": (wired is False),
                "rate_key": "human_deliver:remind",
            })
        except Exception:
            logger.debug("human_deliver alert 发布失败（忽略）", exc_info=True)
            return
        self._hd_alerted = True
        self._hd_last_remind = ts
        self.total_human_deliver_alerts += 1
        if wired is False:
            logger.warning(
                "坐席已人工通过 %d 条草稿，但人工投递回调**未接线**"
                "（DraftService.inbox_deliver_wired=False）——只标记不发送，"
                "客户一条都没收到；查 inbox.auto_draft.human_deliver 与 bootstrap 注入",
                approved)
        else:
            logger.warning(
                "坐席已人工通过 %d 条草稿，但本进程人工投递计数恒 0（零尝试）"
                "——投递回调疑似未接线，客户可能一条都没收到",
                approved)

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

    def _check_lan_gpu_hosts(self, *, now: Optional[float] = None) -> None:
        """LAN GPU 主机（嵌入/视觉/兜底 LLM/本地 MT 所在）宕机的升级式提醒。

        2026-08-01 实锤：176（5090 主力）整机下线约两小时——嵌入/视觉/MT 全部静默
        转移备点、坐席只感到偶尔慢一拍，**零告警**。故障转移网太称职反而掩盖了
        单点损失：此时本地兜底冗余已归零，DeepSeek 再出问题就没有第二道防线。
        本巡检把「主机持续不可达」升级为主动外发：
          - 不可达 ≥ ``after_min``（默认 30min）→ 首提（EventBus ``lan_gpu_alert``）；
          - 仍未恢复 → 每 ``interval_min``（默认 240min）重提；
          - 恢复 → 补发恢复通知 + 状态清零（未告警过的抖动恢复不发，防噪）。
        目标清单派生自配置（``lan_gpu_probe_targets``），按主机去重、只认私网；
        云端部署无 LAN 端点 → 空清单天然静默。探针 ``/api/version`` 零模型加载
        毫秒级；宕机主机 3s 超时封顶（每 tick 最多 3s×N，与音频探针同量级）。
        配置 ``health_watchdog.lan_gpu_remind.{enabled,after_min,interval_min}``（默认开）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        lr = (((cfg.get("health_watchdog") or {}).get("lan_gpu_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not lr.get("enabled", True):
            return
        targets = lan_gpu_probe_targets(cfg if isinstance(cfg, dict) else {})
        if not targets:
            return
        ts = float(now if now is not None else time.time())
        after_sec = max(60.0, float(lr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(lr.get("interval_min", 240) or 240) * 60.0)
        for root in targets:
            st = self._lan_gpu_state.setdefault(
                root, {"down_since": 0.0, "alerted": 0.0, "last_remind": 0.0})
            probe = probe_lan_gpu_host(root)
            host = root.split("://", 1)[1]
            if probe.get("reachable"):
                if st["alerted"]:
                    # 从「已告警」恢复 → 补发恢复通知（抖动恢复不发，防噪）
                    try:
                        from src.integrations.shared.event_bus import get_event_bus
                        get_event_bus().publish("lan_gpu_alert", {
                            "recovered": True, "host": host, "url": root,
                            "rate_key": f"lan_gpu:{host}:recovered",
                        })
                        logger.info("HealthWatchdog 发出 LAN GPU 主机恢复通知 %s", host)
                    except Exception:
                        logger.debug("lan_gpu recovery 发布失败（已忽略）", exc_info=True)
                st["down_since"] = 0.0
                st["alerted"] = 0.0
                st["last_remind"] = 0.0
                continue
            if not st["down_since"]:
                st["down_since"] = ts    # 首见不可达：只记时点，给抖动一个窗口
                continue
            down_sec = ts - st["down_since"]
            due = ((not st["alerted"] and down_sec >= after_sec)
                   or (st["alerted"] and ts - st["last_remind"] >= interval_sec))
            if not due:
                continue
            was_reminder = bool(st["alerted"])
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("lan_gpu_alert", {
                    "host": host, "url": root,
                    "error": str(probe.get("error") or ""),
                    "down_minutes": int(down_sec // 60),
                    "reminder": was_reminder,
                    # 每主机独立限流键：176 与 140 同时出事要各自能报
                    "rate_key": f"lan_gpu:{host}",
                })
            except Exception:
                logger.debug("lan_gpu alert 发布失败（已忽略）", exc_info=True)
                continue
            st["alerted"] = 1.0
            st["last_remind"] = ts
            self.total_lan_gpu_reminders += 1
            # 日志=本机唯一保证在的持久告警通道（webhook 可能 0 通道、SSE 有白名单
            # 且只对在线页面直播）——与 draft_backlog 同款落一行，实弹验证/事后
            # 追溯都靠它（2026-08-01 首次验收时发现只发总线没落日志的盲区）。
            logger.warning(
                "LAN GPU 主机不可达：%s 已 %d 分钟（嵌入/视觉/兜底 LLM/本地 MT "
                "已静默转移备点，本地冗余归零）%s",
                host, int(down_sec // 60), "（重提）" if was_reminder else "")

    def _check_trial_fulfiller(self, *, now: Optional[float] = None) -> None:
        """试用履约端（厂商机）停摆的升级式提醒。

        签发链上唯一的单点是厂商机：它不跑，用户点了「免费领取」就永远停在
        「正在签发」——客户端不报错、台账只多一条 pending，**整条链静默失效**。
        客服控制台已有心跳卡，但那是被动的；本巡检把它升级为主动外发：
          - 不健康持续 ≥ ``after_min``（默认 15min）→ 首提（EventBus ``trial_fulfiller_alert``）；
          - 仍未恢复 → 每 ``interval_min``（默认 240min）重提；
          - 恢复 → 补发恢复通知 + 清零（未曾告警的抖动恢复不发，防噪）。

        配置 ``licensing.trial.fulfiller_watch.{enabled,site_url,admin_key_file,
        stale_min,backlog_min,after_min,interval_min}``——**默认关**，只有厂商机该开
        （别的机器开了只会对着别人的台账瞎报）。未启用时 probe 返回 None 天然静默。

        三种不健康分开报，因为处置动作不同：``stale`` 履约端没来取待办（任务被删/
        python 路径变了/机器关了）；``stuck`` 它在来却清不掉活（回填失败/签不出）；
        ``unreachable`` 连台账都取不到（本机网络或官网出问题，链路状态未知）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        try:
            from src.licensing.trial_fulfiller_watch import probe as _probe
            from src.licensing.trial_fulfiller_watch import watch_target
        except Exception:
            return
        target = watch_target(cfg)
        if target is None:
            return  # 非厂商机 / 未启用
        snap = _probe(cfg, now=now)
        if snap is None:
            return
        ts = float(now if now is not None else time.time())

        if snap.get("healthy"):
            if self._fulfiller_alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("trial_fulfiller_alert", {
                        "recovered": True,
                        "rate_key": "trial_fulfiller:recovered",
                    })
                    logger.info("HealthWatchdog 发出试用履约端恢复通知")
                except Exception:
                    logger.debug("trial_fulfiller recovery 发布失败（已忽略）", exc_info=True)
            self._fulfiller_bad_since = 0.0
            self._fulfiller_alerted = False
            self._fulfiller_last_remind = 0.0
            self._fulfiller_kind = ""
            return

        kind = str(snap.get("kind") or "stale")
        # 换了故障种类（如 stale → stuck）视作新问题：立刻重新计时并允许再首提，
        # 否则「换了病因」会被上一次的重提间隔压住，处置线索也就丢了。
        if kind != self._fulfiller_kind:
            self._fulfiller_kind = kind
            self._fulfiller_bad_since = ts
            self._fulfiller_alerted = False
            return
        if not self._fulfiller_bad_since:
            self._fulfiller_bad_since = ts
            return
        bad_sec = ts - self._fulfiller_bad_since
        after_sec = max(60.0, float(target.get("after_min", 15) or 15) * 60.0)
        interval_sec = max(600.0, float(target.get("interval_min", 240) or 240) * 60.0)
        due = (
            (not self._fulfiller_alerted and bad_sec >= after_sec)
            or (self._fulfiller_alerted
                and ts - self._fulfiller_last_remind >= interval_sec)
        )
        if not due:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("trial_fulfiller_alert", {
                "kind": kind,
                "never": bool(snap.get("never")),
                "heartbeat_min": int(snap.get("heartbeat_min") or -1),
                "pending": int(snap.get("pending") or 0),
                "backlog_min": int(snap.get("backlog_min") or 0),
                "site": str(snap.get("site") or ""),
                "down_minutes": int(bad_sec // 60),
                "reminder": bool(self._fulfiller_alerted),
                "rate_key": "trial_fulfiller:remind",
            })
        except Exception:
            logger.debug("trial_fulfiller alert 发布失败（已忽略）", exc_info=True)
            return
        self._fulfiller_alerted = True
        self._fulfiller_last_remind = ts
        self.total_trial_fulfiller_reminders += 1

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

    def _check_colloquial_llm(self, *, now: Optional[float] = None) -> None:
        """口语化 LLM 持续连败的升级式提醒（2026-07-15 九连败静默事故修复）。

        事故形态：本地口语化 LLM 端点长期不可用 → ``voice_colloquial_llm`` 每 60s
        「熔断-重试」无限循环，日志有记录但无人被通知；语音口语化静默降级规则档
        （活人感 A 档失效），拟人化卖点在静默流失。判定口径镜像 avatar hang：
          - ``fail_streak`` ≥ 阈值（默认 6）且最近失败在 ``fresh_min``（默认 60min）内
            → 视为持续挂死，计 down_since；
          - 持续 ≥ ``after_min``（默认 30min）→ 首提（EventBus ``colloquial_llm_alert``），
            之后每 ``interval_min``（默认 240min）重提；
          - 恢复需要**正面证据**（失败之后真的又成功改写过一次）→ 补发恢复通知。
        配置 ``health_watchdog.colloquial_llm_remind.{enabled,fail_streak,fresh_min,
        after_min,interval_min}``（默认开）；模块从未被调用（无语音流量/功能关）时
        信号全零 → 天然静默零误报。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        cr = (((cfg.get("health_watchdog") or {}).get("colloquial_llm_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not cr.get("enabled", True):
            return
        try:
            from src.ai.voice_colloquial_llm import health_signal
            sig = health_signal()
        except Exception:
            return
        ts = float(now if now is not None else time.time())
        streak_need = max(1, int(cr.get("fail_streak", 6) or 6))
        fresh_sec = max(300.0, float(cr.get("fresh_min", 60) or 60) * 60.0)
        streak = int(sig.get("fail_streak") or 0)
        last_fail = float(sig.get("last_fail_ts") or 0.0)
        last_ok = float(sig.get("last_ok_ts") or 0.0)

        bad = (streak >= streak_need and last_fail > 0.0
               and ts - last_fail <= fresh_sec)
        # 正面恢复证据 = 最近一次失败之后又真的成功过；从未失败过也算干净
        cleared = (last_fail <= 0.0) or (last_ok >= last_fail)

        if not bad:
            if self._colloquial_alerted:
                if not cleared:
                    # 无流量导致信号陈旧：既不误报恢复也不重提，下一条真实语音见分晓
                    return
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("colloquial_llm_alert", {
                        "recovered": True,
                        "rate_key": "colloquial_llm:recovered",
                    })
                    logger.info("HealthWatchdog 发出口语化 LLM 恢复通知")
                except Exception:
                    logger.debug("colloquial_llm recovery 发布失败（已忽略）",
                                 exc_info=True)
            self._colloquial_down_since = 0.0
            self._colloquial_alerted = False
            self._colloquial_last_remind = 0.0
            return

        if not self._colloquial_down_since:
            self._colloquial_down_since = ts
            return
        down_sec = ts - self._colloquial_down_since
        after_sec = max(60.0, float(cr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(cr.get("interval_min", 240) or 240) * 60.0)
        due = (
            (not self._colloquial_alerted and down_sec >= after_sec)
            or (self._colloquial_alerted
                and ts - self._colloquial_last_remind >= interval_sec)
        )
        if not due:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("colloquial_llm_alert", {
                "fail_streak": streak,
                "in_cooldown": bool(sig.get("in_cooldown")),
                "down_minutes": int(down_sec // 60),
                "reminder": bool(self._colloquial_alerted),
                "rate_key": "colloquial_llm:remind",
            })
        except Exception:
            logger.debug("colloquial_llm alert 发布失败（已忽略）", exc_info=True)
            return
        self._colloquial_alerted = True
        self._colloquial_last_remind = ts
        self.total_colloquial_llm_reminders += 1

    def _log_triage_path(self, lt: Dict[str, Any]) -> Optional[str]:
        """决定巡检的日志路径：配置覆写 > 本实例 logging.file > None（跳过）。"""
        p = str(lt.get("log_file") or "").strip()
        if p:
            return p
        cfg = getattr(self._config_manager, "config", None) or {}
        if isinstance(cfg, dict):
            lf = str(((cfg.get("logging") or {}).get("file")) or "").strip()
            if lf:
                return lf
        return None

    def _check_log_triage(self, *, now: Optional[float] = None) -> None:
        """日志巡检：新错误类/激增 → host_alert 外发（默认关，6h 稀疏节流）。

        复用 ``scripts.triage_watch.scan_once``（与独立 CLI/计划任务同一偏差检测核心，
        零逻辑漂移）：首跑只建立基线不告警；之后只对 ①基线里没有的新模板 ②已知模板
        激增 上报，且每轮把当前计数并入基线 → 同一异常只轰一次（下轮成已知基线）。
        告警走 ``host_alert.notify_host``（EventBus host_alert → Webhook → Telegram），
        finding 集合作为去抖 key（相同异常集在 cooldown 内不重发）。

        配置 ``health_watchdog.log_triage.{enabled=false, interval_min=360, window_hours=6,
        log_file, new_min_count, warn_new_min_count, surge_factor, surge_min, cooldown_min}``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        lt = (((cfg.get("health_watchdog") or {}).get("log_triage")) or {}) \
            if isinstance(cfg, dict) else {}
        if not lt.get("enabled", False):
            return
        ts = float(now if now is not None else time.time())
        interval_sec = max(600.0, float(lt.get("interval_min", 360) or 360) * 60.0)
        if self._last_triage_ts and (ts - self._last_triage_ts) < interval_sec:
            return
        self._last_triage_ts = ts

        from pathlib import Path as _Path
        log_file = self._log_triage_path(lt)
        if not log_file or not _Path(log_file).exists():
            return
        try:
            from scripts.triage_watch import (
                _default_baseline_path, merge_baseline, save_baseline,
                scan_once, summarize,
            )
        except Exception:
            logger.debug("triage_watch 导入失败（已忽略）", exc_info=True)
            return

        log_path = _Path(log_file)
        baseline_path = _default_baseline_path(log_path)
        window_hours = float(lt.get("window_hours", 6) or 6)
        try:
            res = scan_once(
                log_path, baseline_path,
                window_hours=window_hours,
                new_min_count=int(lt.get("new_min_count", 1) or 1),
                warn_new_min_count=int(lt.get("warn_new_min_count", 10) or 10),
                surge_factor=float(lt.get("surge_factor", 3.0) or 3.0),
                surge_min=int(lt.get("surge_min", 20) or 20),
            )
        except Exception:
            logger.debug("triage 扫描异常（已忽略）", exc_info=True)
            return

        # 每轮把当前计数并入基线（含首跑）——同一异常只告一次，之后成为已知基线。
        try:
            save_baseline(baseline_path, merge_baseline(res.baseline, res.groups))
        except Exception:
            logger.debug("triage 基线写入失败（已忽略）", exc_info=True)

        if not res.findings:
            return
        summary = summarize(res.findings, window_hours=window_hours)
        errs = sum(1 for f in res.findings if f.level == "ERROR")
        title = "日志巡检告警" + (f"（{errs} 类新错误）" if errs else "（WARNING 激增）")
        # 去抖 key = finding 集合指纹；cooldown 默认 = interval，相同异常集不重发
        import hashlib
        fp = hashlib.sha1(
            "|".join(sorted(f"{f.level}:{f.logger}:{f.template}" for f in res.findings))
            .encode("utf-8", "replace")
        ).hexdigest()[:12]
        cooldown_sec = max(600.0, float(lt.get("cooldown_min", lt.get("interval_min", 360))
                                        or 360) * 60.0)
        try:
            from src.utils.host_alert import notify_host
            fired = notify_host(title, summary, key=f"triage:{fp}", cooldown_sec=cooldown_sec)
            if fired:
                self.total_log_triage_alerts += 1
                logger.info("HealthWatchdog 发出日志巡检告警: %s", title)
        except Exception:
            logger.debug("triage host_alert 发布失败（已忽略）", exc_info=True)

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

    def _check_media_restock(self, *, now: Optional[float] = None) -> None:
        """相册场景缺口自动补货（P2 图文一致性）：需求侧反复要不到 + 供给侧
        零备货 → 写补货**计划文件**（渲染交 ``scripts/album_restock.py``
        夜间低峰跑，主进程零 GPU 占用）。

        配置 ``companion.selfie.consistency.auto_restock.{enabled,min_unmet,
        per_scene,max_per_day}``（**默认关**——新子系统约定）；每小时一轮 +
        每日入队预算；同 (人设,场景) pending 去重在 ``plan_add`` 内。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        scfg = ((cfg.get("companion") or {}).get("selfie") or {}) \
            if isinstance(cfg, dict) else {}
        if not scfg.get("enabled", False):
            return
        from src.companion.media_restock import (load_plan, plan_add,
                                                 qualify_restock_targets,
                                                 resolve_restock_cfg, save_plan)
        rc = resolve_restock_cfg(scfg)
        if not rc["enabled"]:
            return
        ts = float(now if now is not None else time.time())
        if self._last_media_restock_ts and (ts - self._last_media_restock_ts) < 3600.0:
            return
        self._last_media_restock_ts = ts
        day = time.strftime("%Y%m%d", time.localtime(ts))
        if day != self._media_restock_day:
            self._media_restock_day = day
            self._media_restock_added_today = 0
        budget = int(rc["max_per_day"]) - self._media_restock_added_today
        if budget <= 0:
            return
        try:
            from src.companion.media_gap import (collect_scene_supply,
                                                 scene_gap_report)
            from src.inbox.image_autosend import metrics_snapshot as _ims
            snap = _ims()
            supply = collect_scene_supply(scfg)
            gap = scene_gap_report(
                supply, snap.get("scene_demand"), snap.get("scene_unmet"))
            targets = qualify_restock_targets(
                gap.get("rows"), supply,
                min_unmet=int(rc["min_unmet"]), max_targets=budget)
        except Exception:
            logger.debug("相册补货缺口计算失败（已忽略）", exc_info=True)
            return
        if not targets:
            return
        plan = load_plan(rc["plan_path"])
        added = []
        for pid, scene in targets:
            if plan_add(plan, pid, scene, int(rc["per_scene"]), now=ts):
                added.append(f"{pid or '(root)'}:{scene}")
        if added and save_plan(rc["plan_path"], plan):
            self._media_restock_added_today += len(added)
            self.total_media_restock_planned += len(added)
            logger.info("相册补货计划入队 %d 项：%s（渲染交夜间 album_restock）",
                        len(added), "; ".join(added))

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

    def _check_trial_claim(self, *, now: Optional[float] = None) -> None:
        """试用领取兑现巡检（P2）：把「授权到账」从 UI 手里拿走。

        用户在首启向导点了「免费领取」之后就走了——厂商机可能一分钟后才签发，
        客服核销赠量更可能是几小时后。没有这条后台轮询，两者都只能等下次有人
        恰好打开那个一辈子只弹一次的向导。

        触发条件极窄，正常部署零开销：**本地有单 且 还没激活或还没领到赠量**。
        单子领完（已激活 + 赠量已入账）或本机试用已用尽 → 永久静默。
        节流 ``licensing.trial.poll_interval_sec``（默认 120s）。
        """
        try:
            from src.licensing import trial_claim_client as tc
        except Exception:
            return
        cfg = getattr(self._config_manager, "config", None) or {}
        state = tc.load_state(cfg)
        if not state.get("claim_id") or state.get("exhausted"):
            return
        if state.get("activated_at") and state.get("topup_redeemed_at"):
            return  # 授权与赠量都到位，这条单子的生命周期结束

        interval = float(((cfg.get("licensing") or {}).get("trial") or {})
                         .get("poll_interval_sec") or 120)
        ts = float(now if now is not None else time.time())
        if self._last_trial_poll_ts and (ts - self._last_trial_poll_ts) < interval:
            return
        self._last_trial_poll_ts = ts

        res = tc.poll(config=cfg)
        if not res.get("ok"):
            return  # 网络抖动：下一轮再来，不告警（用户此刻用体验档照常干活）
        lic, voucher = str(res.get("license") or ""), str(res.get("topup_voucher") or "")
        if not lic and not voucher:
            return
        from src.web.routes.license_routes import _consume_trial_payload
        out = _consume_trial_payload({"ok": True}, lic, voucher)
        if out.get("activated"):
            logger.info("[trial] 后台轮询已激活 7 天试用授权（无需用户回到向导）")
        if out.get("gift_redeemed"):
            logger.info("[trial] 后台轮询已入账客服赠量 %s 字符", out.get("gift_chars") or 0)

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

    def _check_api_version_expiry(self, *, now: Optional[float] = None) -> None:
        """外部 API 版本临期/过期 → 主动告警（补 CI 门禁的**日历盲区**）。

        为什么单有 CI 门禁不够（2026-08-03 事故复盘）：门禁只在**有人提交代码**时跑，
        而这类失效是**日历驱动**的。Messenger 告警端点钉的 v19.0 过期 74 天无人知道，
        期间仓库一直有提交——但当时根本没有那条门禁；反过来，就算有门禁，一个几个月
        没人动的模块同样能悄悄走到死期。两条腿才站得住：
        提交时由 ``tests/test_external_api_lifecycle.py`` 拦，日历时由这里盯。

        **只在真正需要动手时出声**（``critical``/``expired``/``unknown``）：
        ``warn``（半年内到期）留给看板与门禁，这里出声就是噪音，运维会学会无视它。
        去抖交给 ``notify_host`` 的 per-key 冷却（默认 24h 重提一次）；恢复后补一条
        恢复通知并复位，免得运维不确定「到底修好没有」。

        配置 ``health_watchdog.api_version_expiry.{enabled,check_interval_sec,remind_sec}``
        ——与 avatar_voice/draft_backlog 等健康类巡检一致 **默认开**（它不是新功能，
        是安全网；且健康时零输出、零 IO：纯日期运算）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        conf = ((cfg.get("health_watchdog") or {}).get("api_version_expiry") or {})
        if conf.get("enabled") is False:
            return
        interval = float(conf.get("check_interval_sec") or 43200.0)  # 12h
        ts = float(now if now is not None else time.time())
        if self._last_api_version_ts and (ts - self._last_api_version_ts) < interval:
            return
        self._last_api_version_ts = ts

        from src.utils.external_api_lifecycle import health_report
        from src.utils.host_alert import notify_host

        report = health_report()
        bad = [s for s in report.values() if s.level in ("expired", "critical", "unknown")]
        if not bad:
            if self._apiver_alerted:
                notify_host(
                    "外部 API 版本已恢复",
                    "此前临期/过期的外部 API 版本已全部升到安全范围。",
                    key="apiver:recovered", cooldown_sec=0.0,
                )
                self._apiver_alerted = False
            return

        lines = []
        for st in bad:
            if st.level == "expired":
                when = f"已过期 {abs(st.days_left or 0)} 天"
            elif st.level == "unknown":
                when = "不在官方支持列表内（版本写错或早已退役）"
            else:
                when = f"仅剩 {st.days_left} 天"
            # 静默失败的那类要单独点名——它过期后线上不会有任何信号
            silent = "（过期不报错，会静默改用别的版本）" if st.pin.failure_mode == "silent_fallforward" else ""
            lines.append(f"· {st.pin.label} {st.pin.version}：{when}{silent}\n  → {st.pin.remediation}")

        if notify_host(
            "外部 API 版本需要升级",
            "\n".join(lines) + "\n（单一事实源见各 pin 的 owner 文件；"
                               "升版本，不要改到期日——那是供应商公布的事实）",
            key="apiver:stale",
            cooldown_sec=float(conf.get("remind_sec") or 86400.0),
        ):
            self._apiver_alerted = True
            self.total_api_version_alerts += 1

    def _check_identity_shadow(self, *, now: Optional[float] = None) -> None:
        """跨平台身份影子周期扫描（P3.2）：只读发现「疑似同一人」跨平台会话配对，
        压缩结果落 ``config/identity_shadow_state.json`` 供 ops 看板 / metrics 读。

        配置 ``contacts.identity_shadow.{enabled,scan_interval_hours,max_rows}``
        （默认 关/6h/2000）。未启用零开销（每 tick 一次 dict 取值即返回）；扫描只读
        （sqlite mode=ro，复用 run_shadow_scan，**绝不写关联**）；节流基准＝内存 ts，
        冷启动采纳 state 文件 last_scan_ts（重启不重扫）；ts 在扫描前推进——持续失败
        也只按周期重试，不会每 tick 刷。_tick 跑在 executor 线程，阻塞读库无害。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        from src.utils.identity_shadow_periodic import (
            read_state, run_periodic_scan, shadow_periodic_config, state_path,
        )
        pcfg = shadow_periodic_config(cfg)
        if not pcfg["enabled"]:
            return
        from pathlib import Path
        cfg_path = str(getattr(self._config_manager, "config_path", "")
                       or "config/config.yaml")
        cfg_dir = Path(cfg_path).parent
        ts = float(now if now is not None else time.time())
        if not self._ishadow_last_ts:
            self._ishadow_last_ts = float(
                (read_state(state_path(cfg_dir)) or {}).get("last_scan_ts") or 0.0)
        interval_sec = pcfg["scan_interval_hours"] * 3600.0
        if self._ishadow_last_ts and (ts - self._ishadow_last_ts) < interval_sec:
            return
        self._ishadow_last_ts = ts
        st = run_periodic_scan(cfg, cfg_dir, now=ts)
        if st.get("ok"):
            self.total_identity_shadow_scans += 1
            counts = st.get("counts") or {}
            logger.info(
                "身份影子扫描完成: 会话=%s 候选=%s 配对=%s (high=%s medium=%s low=%s 已关联=%s)",
                st.get("scanned_conversations"), st.get("candidates"), st.get("pairs"),
                counts.get("high"), counts.get("medium"), counts.get("low"),
                counts.get("already_linked"),
            )
        else:
            logger.warning("身份影子扫描失败（已忽略，按周期重试）: %s", st.get("error"))

    def _check_goal_order_pull(self, *, now: Optional[float] = None) -> None:
        """官网订单拉取回流（goals P2 成交闭环，默认关）。

        部署形态：官网在公网 VPS、本引擎在 NAT 后——官网 push（order-hook）
        打不进来，引擎**主动拉** ``GET {site}/api/admin/orders``（x-setup-key
        鉴权，与履约机同通道），筛带 ``ref``（会话归因串）且 paid/activated
        的订单，经 ``service.settle_order_ref``（与 order-hook 路由同一入口）
        把对应目标结算 done。幂等由 settle 层保证（同 order_id → dup）。
        配置 ``companion.goals.order_pull.{enabled,site_url,admin_key,
        interval_min,timeout_sec}``；goals 总闸关时天然静默。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        from src.companion.goals.order_pull import (
            pull_and_settle, resolve_pull_cfg,
        )
        pc = resolve_pull_cfg(cfg)
        if not (pc["enabled"] and pc["site_url"] and pc["admin_key"]):
            return
        from src.companion.goals.service import goals_enabled
        if not goals_enabled(cfg):
            return
        ts = float(now if now is not None else time.time())
        interval_sec = max(300.0, pc["interval_min"] * 60.0)
        if self._last_goal_pull_ts and (ts - self._last_goal_pull_ts) < interval_sec:
            return
        self._last_goal_pull_ts = ts
        from src.companion.goals.service import get_configured_store
        store = get_configured_store(
            cfg, getattr(self._config_manager, "config_path", None))
        summary = pull_and_settle(store, cfg, now=ts)
        settled = int(summary.get("settled") or 0)
        if settled:
            self.total_goal_orders_settled += settled
        if settled or summary.get("errors"):
            logger.info(
                "官网订单拉取: 总单=%s 候选=%s 结算=%s 重复=%s 未匹配=%s 错误=%s",
                summary.get("pulled"), summary.get("candidates"), settled,
                summary.get("dup"), summary.get("unmatched"),
                summary.get("errors"))

    def _check_goal_winback(self, *, now: Optional[float] = None) -> None:
        """流失挽回扫描（goals P6，默认关）。

        留存目标 deadline 过后 ``cooldown_days`` 没续费（expired=真实流失）→
        自动起低频 ``engagement_reactivate`` 挽回目标（一次流失只挽回一次、
        每日预算、窗口上限防陈年流失群发）；候选里还挂 active 的静默会话先
        settle-on-read 结算落库（流失报表盲区补齐）。配置
        ``companion.goals.retention.winback.{enabled,cooldown_days,max_age_days,
        template,days,max_per_day,interval_min}``；goals 总闸关时天然静默。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        from src.companion.goals.service import goals_enabled, resolve_goals_cfg
        if not goals_enabled(cfg):
            return
        wb = ((resolve_goals_cfg(cfg).get("retention") or {})
              .get("winback") or {})
        if not (isinstance(wb, dict) and wb.get("enabled", False)):
            return
        ts = float(now if now is not None else time.time())
        try:
            interval_min = float(wb.get("interval_min", 60) or 60)
        except (TypeError, ValueError):
            interval_min = 60.0
        interval_sec = max(600.0, interval_min * 60.0)
        if self._last_goal_winback_ts and (ts - self._last_goal_winback_ts) < interval_sec:
            return
        self._last_goal_winback_ts = ts
        from src.companion.goals.service import (
            get_configured_store, run_winback_scan,
        )
        store = get_configured_store(
            cfg, getattr(self._config_manager, "config_path", None))
        summary = run_winback_scan(store, cfg, inbox_store=self._inbox(), now=ts)
        if summary.get("created") or summary.get("swept"):
            logger.info(
                "流失挽回扫描: 候选=%s 清扫=%s 新建=%s 跳过=%s 预算触顶=%s",
                summary.get("candidates"), summary.get("swept"),
                summary.get("created"), summary.get("skipped"),
                summary.get("budget_hit"))

    def _check_license_quota(self, *, now: Optional[float] = None) -> None:
        """授权字符额度水位巡检（P4c）：临近触顶提前提醒、触顶点名、恢复报平安。

        口径＝``quota_store.check_license_quota``（含加量包的 included 合计）：
        - ``used/included >= warn_pct``（默认 85%）→「即将用尽」提醒；
        - ``exceeded``（gate 同款判定）→「已用尽」升级提醒（独立 key，不被 warn
          冷却窗压住）；enforce 开=功能已被限制、关=仍在放行，文案如实区分；
        - 兑换加量包/换新授权后回落阈值下 → 给**告过警的**部署补一条恢复通知。
        无授权/不限量（included=0）天然静默。配置
        ``health_watchdog.quota_remind.{enabled,warn_pct,interval_min}``（默认开/85/360，
        与 fallback_duty_remind 同族——授权额度只在有额度授权的部署上有意义，
        默认开零误报）。重提去抖交给 notify_host 按 key 冷却。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        qr = (((cfg.get("health_watchdog") or {}).get("quota_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not qr.get("enabled", True):
            return
        ts = float(now if now is not None else time.time())
        if self._last_license_quota_ts and (ts - self._last_license_quota_ts) < 600.0:
            return  # 10min 稀疏节流（读数=一次 sqlite SUM，纯为省无谓轮询）
        self._last_license_quota_ts = ts
        from src.licensing.quota_store import check_license_quota
        q = check_license_quota()
        included = int(q.get("included") or 0)
        if included <= 0:
            self._license_quota_alerted = False
            return
        used = int(q.get("used") or 0)
        pct = used * 100.0 / included
        warn_pct = float(qr.get("warn_pct", 85) or 85)
        remind_sec = max(600.0, float(qr.get("interval_min", 360) or 360) * 60.0)
        from src.utils.host_alert import notify_host
        if bool(q.get("exceeded")):
            effect = ("翻译/TTS 已被限制" if bool(q.get("enforce"))
                      else "当前未开启强制，功能仍在放行")
            if notify_host(
                "字符额度已用尽",
                (f"授权字符额度已用尽：{used:,} / {included:,}（{effect}）。"
                 "请购买字符加量包（会员中心 → 兑换加量包）或升级套餐。"),
                key="license_quota:exceeded",
                cooldown_sec=remind_sec,
            ):
                self.total_license_quota_alerts += 1
            self._license_quota_alerted = True
        elif pct >= warn_pct:
            if notify_host(
                "字符额度即将用尽",
                (f"授权字符额度已使用 {pct:.0f}%（{used:,} / {included:,}，"
                 f"剩余 {max(0, included - used):,}）。耗尽后翻译/TTS 将受限，"
                 "建议提前购买字符加量包（会员中心 → 兑换加量包）。"),
                key="license_quota:warn",
                cooldown_sec=remind_sec,
            ):
                self.total_license_quota_alerts += 1
            self._license_quota_alerted = True
        elif self._license_quota_alerted:
            notify_host(
                "字符额度已恢复",
                (f"字符额度回落到安全水位：{used:,} / {included:,}"
                 f"（{pct:.0f}%）。加量包/新授权已生效。"),
                key="license_quota:recover",
                cooldown_sec=0.0,
            )
            self._license_quota_alerted = False

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
            "total_license_quota_alerts": self.total_license_quota_alerts,
            "total_fallback_duty_reminders": self.total_fallback_duty_reminders,
            "total_avatar_voice_reminders": self.total_avatar_voice_reminders,
            "total_media_promise_alerts": self.total_media_promise_alerts,
            "total_colloquial_llm_reminders": self.total_colloquial_llm_reminders,
            "total_identity_shadow_scans": self.total_identity_shadow_scans,
            "last_check_ts": self.last_check_ts,
            "last_light": self.last_light,
        }
