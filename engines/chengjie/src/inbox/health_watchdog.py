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
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def stderr_storm_verdict(prev: Optional[Dict[str, Any]],
                         cur: Dict[str, Any], *,
                         growth_bytes: int) -> Dict[str, Any]:
    """boot err 日志「异常增长」判定（纯函数，2026-08-27 宕机沉淀）。

    ``prev``/``cur``＝``{"path", "size"}`` 观测快照（同一文件两次采样）。
    判据＝**绝对增量**：err 日志健康态应恒近零，两次巡检间涨 ``growth_bytes``
    （默认 15MB）只可能是「每条日志都在倒堆栈」级别的风暴（当日实测 ~570KB/s），
    不做速率归一（简单可解释，慢泄漏不属本哨兵管）。

    返回 ``{"alert": bool, "grew": int}``；换文件（新 boot）/首见＝建立基线不告警。
    """
    try:
        if not prev or str(prev.get("path")) != str(cur.get("path")):
            return {"alert": False, "grew": 0}
        grew = int(cur.get("size") or 0) - int(prev.get("size") or 0)
        return {"alert": grew >= int(growth_bytes), "grew": max(0, grew)}
    except (TypeError, ValueError):
        return {"alert": False, "grew": 0}


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


def _loop_heartbeat(obj: Any) -> Optional[Dict[str, Any]]:
    """对象型常备循环（SprintGoalTicker / CareDispatcher）→ 与 dict 心跳同构。

    D1b P0-5：两者都有 ``last_tick_ts``（run_once 入口即打点，配置关闸也照跳）、
    ``_interval``（停摆阈值按拍放宽）、``is_running()``（asyncio task 活性）。
    对象缺失 → None（= 未挂载，与 dict 心跳缺失同罪）；属性读取异常 → 视同
    「有对象但零心跳」，让停摆判定按宽限走而不是崩掉整个巡检。
    """
    if obj is None:
        return None
    hb: Dict[str, Any] = {"last_tick_ts": 0.0, "ticks": 0,
                          "interval_sec": 0.0, "running": None}
    try:
        hb["last_tick_ts"] = float(getattr(obj, "last_tick_ts", 0.0) or 0.0)
        hb["ticks"] = int(getattr(obj, "ticks", 0) or 0)
        hb["interval_sec"] = float(getattr(obj, "_interval", 0.0) or 0.0)
        fn = getattr(obj, "is_running", None)
        if callable(fn):
            hb["running"] = bool(fn())
    except Exception:
        logger.debug("loop heartbeat 读取失败（按零心跳）", exc_info=True)
    return hb


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

    端点解析与 ``AvatarVoiceClient`` 同口径：多端点部署时 ``base_urls[0]``（主端点）
    优先于单数 ``base_url``。2026-08-18 实锤：本机 7852 退役迁 140 后 overlay 只配了
    ``base_urls``，本函数仍回落 127.0.0.1 → 探已退役旧服务 → 每 4h 一条假「掉线」
    告警（真身 140:7852 全程健康、客户端用的就是它）。
    """
    cfg = config if isinstance(config, dict) else {}
    # ── 2026-08-29：TTS 主力迁 minicpm_clone（104 专属 IndexTTS-2）后必须跟着改探针 ──
    # 智聊全部人设的 voice_profile.backend 已是 minicpm_clone，avatar_voice 的
    # 140:7852 不再有消费方。继续探它＝探一个自己不用的端点：140 掉线会报成「智聊
    # 语音掉线」（假告警），而真正在用的 104 挂了反而没有灯——与本函数注释里 2026-08-18
    # 那次「7852 退役迁 140 后仍探 127.0.0.1」是同一类错位，只是方向换了一次。
    # 判据取 `minicpm_clone.enabled + base_url`：那正是 `_try_minicpm_clone` 的门控条件。
    mc = cfg.get("minicpm_clone") if isinstance(cfg.get("minicpm_clone"), dict) else {}
    if mc.get("enabled", False):
        _mc = str(mc.get("base_url") or "").strip().rstrip("/")
        if _mc:
            return _mc + "/health"
    av = (cfg.get("avatar_voice") or {}) if isinstance(cfg, dict) else {}
    if not av.get("enabled", False):
        return ""
    base = ""
    raw = av.get("base_urls")
    if isinstance(raw, (list, tuple)):
        for u in raw:
            u = str(u or "").strip()
            if u:
                base = u
                break
    if not base:
        base = str(av.get("base_url") or "http://127.0.0.1:7852").strip()
    base = base.rstrip("/")
    if not base:
        return ""
    return base + "/health"


def avatar_probe_host_is_local(url: str) -> bool:
    """探针目标是否本机服务（127.0.0.1/localhost，纯函数）。

    救援计划任务（EmotionTTS_Boot/EmotionTTSWatchdog）是**本机** schtasks——
    探针目标迁到远端主机（如 140:7852）后仍探本机任务状态是张冠李戴：远端服务的
    自愈链在远端，本机任务停用是本地 TTS 退役后的刻意状态，不构成「救援链断裂」。
    解析失败按非本机处理（宁可少说一行，不误报）。
    """
    try:
        from urllib.parse import urlparse
        host = str(urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


_AVATAR_PROBE_CACHE: Dict[str, Any] = {"ts": 0.0, "url": "", "result": None}
_AVATAR_PROBE_TTL_SEC = 60.0


def health_payload_ready(data: Any) -> bool:
    """语音服务 ``/health`` 返回体 → 「可用且模型已加载」（纯函数）。

    两套契约并存，探针必须都认：
      AvatarHub 7852：``{"ok": true, "models_loaded": true}``
      IndexTTS-2 节点 104:7865（fish_speech 契约）：``{"status": "ok", "model_loaded": true}``
    2026-09-10 实锤：探针只认前者 → 104 全程健康却被判 ``models_loaded=False`` →
    每 4h 一条假「智聊语音掉线」告警，连发 10 条（tts 真活探针同期全绿）。
    未声明加载字段视为已加载（老服务没有该字段）；显式 false 才算未就绪。
    """
    if not isinstance(data, dict):
        return False
    ok = data.get("ok") is True or str(data.get("status") or "").lower() in ("ok", "healthy", "ready")
    if not ok:
        return False
    for key in ("models_loaded", "model_loaded"):
        if key in data:
            return bool(data.get(key))
    return True


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
            "models_loaded": health_payload_ready(data),
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
    # 语音口语化专属 LAN 端点（avatar_voice.colloquial.llm_endpoints，2026-08-01）：
    # 挂了会静默降级到 cloud/规则档，同属「该被巡检点名」的算力依赖。
    col = (((config.get("avatar_voice") or {}).get("colloquial")) or {})
    if isinstance(col, dict):
        for item in (col.get("llm_endpoints") or []):
            if isinstance(item, dict):
                bases.append(str(item.get("base_url") or ""))

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


def lan_gpu_remind_enabled(config: Dict[str, Any]) -> bool:
    """LAN GPU 巡检的开关判定（纯函数）——桌面客户机默认关（L-6 B，2026-09-06）。

    「本地冗余归零」是办公室集群的运维概念：176/140 挂了、坐席只感到慢一拍、运维必须
    知道。装在客户机上的桌面包却也带着同一套巡检：内测种子留下的 LAN 端点
    （``avatar_voice.colloquial.llm_endpoints`` 的 173/198 等）在客户家里永远探不通 →
    每 30 分钟一条红 toast「LAN GPU 不可达 本地冗余归零」（skuio 机 C6EFRS 实锤），
    而客户机上根本不存在「本地冗余」这回事。桌面态：``health_watchdog.lan_gpu_remind
    .enabled`` **显式** true 才开（内部坐席机想看可自己开）；服务器/办公室部署默认开不变。
    """
    lr = (((config or {}).get("health_watchdog") or {}).get("lan_gpu_remind")
          or {}) if isinstance(config, dict) else {}
    if "enabled" in lr:
        return bool(lr.get("enabled"))
    from src.utils.desktop_mode import is_desktop_client
    return not is_desktop_client(config)


# LAN GPU 主机探活路径，按契约逐个试，任一 2xx 即算可达：
#   /api/version  Ollama 专有（零模型加载、毫秒级）
#   /v1/models    OpenAI 兼容通用——vLLM / llama.cpp server / Ollama 都有
#   /health       vLLM 与多数自建推理服务
# 2026-09-10 实锤：173:8001 是 vLLM，只打 /api/version 必 404 → 「不可达」累到 1002 分钟、
# 每 4h 一条假告警，而该主机全程在线（/v1/models 正常返回 chatx）。
_LAN_GPU_PROBE_PATHS: Tuple[str, ...] = ("/api/version", "/v1/models", "/health")


def probe_lan_gpu_host(root: str, *, timeout: float = 3.0) -> Dict[str, Any]:
    """探一台 LAN GPU 主机是否在线（无缓存，调用方自控频率）。

    依次打 ``_LAN_GPU_PROBE_PATHS``：连接层失败（拒连/超时/DNS）立即判不可达——主机
    真挂了不必再试别的路径白等；只有 HTTP 层非 2xx（404/405 等＝服务在、契约不同）
    才换下一条路径。返回 ``probe`` 字段记录命中的路径，方便日志分辨主机类型。
    """
    result: Dict[str, Any] = {"url": root, "reachable": False}
    import urllib.error
    import urllib.request
    last_err = ""
    for path in _LAN_GPU_PROBE_PATHS:
        t0 = time.time()
        try:
            with urllib.request.urlopen(root + path, timeout=timeout) as resp:
                resp.read()
            result.update({"reachable": True, "probe": path,
                           "latency_ms": int((time.time() - t0) * 1000)})
            return result
        except urllib.error.HTTPError as e:
            last_err = f"{path}: HTTP {e.code}"
            continue
        except Exception as e:
            last_err = f"{path}: {str(e)[:100]}"
            break
    result["error"] = last_err[:120]
    return result


def probe_alert_link(state, config) -> Optional[Dict[str, Any]]:
    """告警外发链路探针（进程内只读：store mtime 快照 + notifier 状态，零网络 IO）。

    None＝检查被显式关闭（``ops.alert_link_health: false``——刻意不配外发通道的
    部署不必长期黄灯）；**默认开**：「告警报进虚空」（0 通道，SLA/积压/host_alert
    只进日志与铃铛）正是最需要被看见的出货默认态，L3 草稿烂 167h 的根因。
    返回 ``build_health(alert_link=)`` 需要的最小子集（verdict+计数）；完整明细走
    ``GET /api/admin/alert-link-status`` 与 ops-overview「🔔 告警链路」卡（同一
    聚合器 ``collect_alert_link_status``，永远一个口径）。
    """
    knob = ((config or {}).get("ops") or {}).get("alert_link_health", True)
    if isinstance(knob, dict):
        knob = knob.get("enabled", True)
    if knob is False:
        return None
    from src.integrations.alert_link_status import collect_alert_link_status

    s = collect_alert_link_status(config, getattr(state, "webhook_notifier", None))
    a = s.get("audit") or {}
    p = s.get("process") or {}
    return {
        "verdict": s.get("verdict"),
        "channels_enabled": int(a.get("channels_enabled") or 0),
        "covered": int(a.get("covered_count") or 0),
        "focus": int(a.get("focus_count") or 0),
        "uncovered": len(a.get("uncovered") or []),
        "total_errors": int(p.get("total_errors") or 0),
    }


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

    alert_link = None
    try:
        alert_link = probe_alert_link(state, config)
    except Exception:
        logger.debug("告警链路探测失败（已忽略）", exc_info=True)

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
        alert_link=alert_link,
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
        daily_report_enabled: bool = False,
        daily_interval_sec: float = 86400.0,
        surface_in_ui: bool = True,
    ) -> None:
        self._app = app
        self._config_manager = config_manager
        # 跨重启的提醒状态账本（2026-09-10 运维群降噪）：草稿积压 / 案例积压 / 入站漏球 /
        # 语音掉线 / LAN GPU 五个高频巡检的 alerted / first_seen / last_remind / 内容指纹
        # 都从这里读写——此前是实例属性，每次重启整轮重发且「已 X 分钟」从零重算。
        # 其余巡检仍是内存态（沿用旧属性），迁移时把 _db_* 那组属性当模板。
        self.__dict__["_remind_ledger"] = self._build_remind_ledger()
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
        # WP-3 老板日报推送（默认关；同 ops_report 通道、period="daily" 标记）。
        # _last_daily_ts 同周报初始化为「现在」——首份日报在启动一天后才发，防重启刷屏。
        self._daily_enabled = bool(daily_report_enabled)
        self._daily_interval = max(3600.0, float(daily_interval_sec))
        self._last_daily_ts = time.time()
        self.total_daily_reports: int = 0
        # 默认只对 fail（red）告警；warn 噪音大，可显式开
        self._alert_on_warn = bool(alert_on_warn)
        # #170：alert_on_warn 只管**外部** webhook 要不要收黄灯；「坐席在工作台里
        # 看得见」是另一回事——被埋会话 8 个 86h 在 alert_on_warn=False 下只写日志，
        # 用户翻不到。surface_in_ui（默认开，配置 health_watchdog.surface_in_ui 可关）
        # 把这类「客户在等」的巡检结论写进工作台通知中心（进程级 notif_queue，按 id
        # 合并、归零即撤），与 webhook 通道正交。
        self._surface_in_ui = bool(surface_in_ui)
        self._ui_surfaced: Dict[str, bool] = {}
        # 启动一次性卫生扫描（#207 已消化的「需人工」标 / #170 已删会话的孤儿未读）
        self._hygiene_done = False
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
        # 试用领取兑现巡检（P2）：只在「本地有未兑现的单」时才真跑，正常部署零开销
        self._last_trial_poll_ts: float = 0.0
        # 授权字符额度水位巡检（P4c）：稀疏节流 + 「告过警」标记（恢复通知只发给告过警的）
        self._last_license_quota_ts: float = 0.0
        self._license_quota_alerted: bool = False
        # Token 钱包水位巡检（quotawall v2 P2-4）：同款稀疏节流 + 告过警标记
        self._last_token_wallet_ts: float = 0.0
        self._token_wallet_alerted: bool = False
        # 本地兜底顶班提醒：上次观测的兜底出话累计数 + 首次观测到顶班的时间
        self._fb_duty_last_calls: Optional[int] = None
        self._fb_duty_since_ts: float = 0.0
        self._fb_duty_idle_ticks: int = 0
        # AvatarHub 7852 持续掉线升级提醒：_avatar_down_since / _avatar_alerted /
        # _avatar_last_remind / _avatar_alert_kind 是账本属性（键 avatar_voice），见类尾。
        # 语音出站断档巡检（2026-08-02）：hub 音色档 404 → strict 全拒发 5 天零告警
        # ——avatar hang 检测（探针绿+streak 新鲜度）对「低流量下零星请求全失败」
        # 不敏感。判据换成 voice_outage 滚动窗「尝试 ≥N 且 0 成功」。
        self._vo_alerted: bool = False
        self._vo_last_remind: float = 0.0
        # 单链断档（2026-08-22）：上面那组是**全局**口径（窗内 0 成功），一条健康的
        # 链就能把另一条全灭的链盖住。接进 wa_rpa/mr_rpa 后台账有 5 个 source，
        # 「一条全灭、其余正常」从边角情形变成常态形态，故按 source 各记一份状态
        # （共用一份计时器会让先响的那条把后来的顶掉）。
        self._vo_src_alerted: Dict[str, bool] = {}
        self._vo_src_last_remind: Dict[str, float] = {}
        self.total_voice_outage_alerts: int = 0
        # 履约端（厂商机签发链单点）停摆升级提醒：首次不健康时刻 + 已首提 + 上次重提 + 种类
        self._fulfiller_bad_since: float = 0.0
        self._fulfiller_alerted: bool = False
        self._fulfiller_last_remind: float = 0.0
        self._fulfiller_kind: str = ""
        # 告警种类：down=不可达/未载入（health 探测红）；hang=半死（health 绿但合成连败，
        # 2026-07-14 事故形态）。恢复语义不同：hang 需要「失败后真的又成过一次」的正面证据。
        # hub 引擎目录离线巡检（2026-08-22「fish 冒充 IndexTTS-2」事故）：与上面
        # 三个 _avatar_* 状态**刻意分开**——那组盯的是本机 7852，这组盯的是 hub
        # （另一台机、另一套失败域）；共用计时器会让两边的首提/重提互相顶掉。
        self._hub_engine_down_since: float = 0.0
        self._hub_engine_alerted: bool = False
        self._hub_engine_last_remind: float = 0.0
        self.total_hub_engine_reminders: int = 0
        # LAN GPU 主机宕机升级提醒（2026-08-01 176 整机静默下线两小时事故）：
        # 每主机独立状态 {down_since, alerted, last_remind}——故障转移网兜住了业务，
        # 但运维必须知道冗余已经归零。内存字典是账本（键 lan_gpu:<root>）的工作副本：
        # 首次取用从账本回灌、每次改动直写账本（见 _lan_gpu_host_state / _lan_gpu_persist）。
        self._lan_gpu_state: Dict[str, Dict[str, float]] = {}
        self.total_lan_gpu_reminders: int = 0
        self._last_compute_lane_ts: float = 0.0
        self.total_compute_lane_reminders: int = 0
        # 口语化 LLM 持续连败升级提醒（2026-07-15 九连败静默事故）：状态镜像 avatar hang
        self._colloquial_down_since: float = 0.0
        self._colloquial_alerted: bool = False
        self._colloquial_last_remind: float = 0.0
        # 本地主链保险（2026-08-15，与中枢 176 顺序契约配套的我方半边）：
        # ai.primary=local* 而本地 vLLM 连续探测失败 → 单向热切 cloud + 告警；
        # 恢复只发通知不自动升（切回归中枢执行器「起+暖机后切回」步骤）。
        self._apg_fail_count: int = 0
        self._apg_first_fail_ts: float = 0.0
        self._apg_switched: bool = False
        # 锁在本地档期间已发过「连败未切档」告警（端点恢复时补一次恢复通知后清位）
        self._apg_locked_alerted: bool = False
        self.total_ai_primary_guard_switches: int = 0
        # 托管代理生命周期巡检（一键代理 P2）：稀疏节流（默认 1h）+ 低库存签名去重
        # （持续态每轮都会算出来，签名变了才重发，防每小时复读同一份缺货单）。
        self._pxm_last_ts: float = 0.0
        self._pxm_low_sig: str = ""
        self._pxm_low_last_remind: float = 0.0
        # JIT（http 供给）的上游余额水位（P4）：余额就是库存——低于 min_alert 首提，
        # 4h 重提，充值回升自动清零（恢复不推送，看板可见即可）。
        self._pxm_bal_alerted: bool = False
        self._pxm_bal_last_remind: float = 0.0
        self.total_proxy_managed_alerts: int = 0
        # 缺口自动入库：稀疏节流（默认 1h 一轮）+ 每日预算
        self._last_auto_stock_ts: float = 0.0
        self._auto_stock_day: str = ""
        self._auto_stock_added_today: int = 0
        # 相册场景缺口自动补货（P2 图文一致性）：同款节流+预算，写计划文件
        self._last_media_restock_ts: float = 0.0
        self._media_restock_day: str = ""
        self._media_restock_added_today: int = 0
        # Telegram 历史自动补缺口（2026-08-02）：15min 节流 + 触发计数
        self._last_tg_autosync_ts: float = 0.0
        self.total_tg_autosyncs: int = 0
        # 人工通过投递链静默断裂巡检（2026-07-29）：观测窗起点 = 本进程起来的时刻
        # （worker 计数器是进程内的，必须与之同窗口才可比），+ 首提/重提去抖。
        self._hd_since_ts: float = time.time()
        self._hd_bad_since: float = 0.0
        self._hd_alerted: bool = False
        self._hd_last_remind: float = 0.0
        self.total_human_deliver_alerts: int = 0
        # 撤销设定「复活」巡检（2026-08-04）：稀疏扫描 + 冲突指纹去抖 + 恢复通知
        self._retired_scan_ts: float = 0.0
        self._retired_fp: str = ""
        self._retired_alerted: bool = False
        self._retired_last_remind: float = 0.0
        self.total_persona_retired_alerts: int = 0
        # 草稿积压无人处理巡检（2026-07-29）：SLA watcher 只盯 L3/L4，**L1 是盲区**
        # （见 _check_draft_backlog），而 L1 恰恰是「必须人来处理」的那一档。
        # _db_alerted / _db_last_remind 是账本属性（键 draft_backlog）。
        self.total_draft_backlog_alerts: int = 0
        # 系统标签泄漏巡检（2026-09-12「[我方语音消息]」事故）：按落库出站行兜底看见
        # 「LLM 把上下文系统标注照抄进正文并发给了客户」——出稿口守卫漏了/别的生成链
        # 没过守卫/老进程未装载新代码，都在这里现形。账本键 label_leak。
        self.total_label_leak_alerts: int = 0
        self.total_frontend_error_alerts: int = 0
        # 账号真相真幽灵巡检（2026-08-17 P4b）：会话库有、注册表没有、且不是
        # web 工作台。desktop 镜像号在册，不算泄漏；已登出未读被 summary 清零，
        # 原「历史未读」告警永远不响——改盯这个泄漏面。
        self._at_alerted: bool = False
        self._at_last_remind: float = 0.0
        self.total_accounts_truth_alerts: int = 0
        # 入站漏球巡检（P0 2026-08-05）：客户最后一句无回复且**无草稿** → 拟稿链
        # 丢球（draft_backlog 只看「有稿没人处理」，这里补「压根没稿」的盲区）。
        # _ui_alerted / _ui_last_remind 是账本属性（键 unanswered_inbound）。
        self.total_unanswered_inbound_alerts: int = 0
        # 回复额度触顶聚合巡检（P1 2026-08-12）：逐会话 bot_peer_alert 只在首次
        # 拦截各响一次，「多个会话同日触顶」这个**面**级信号（预算配小/撞 bot 波）
        # 此前无人聚合（见 _check_reply_budget）。
        self._rb_alerted: bool = False
        self._rb_last_remind: float = 0.0
        self.total_reply_budget_alerts: int = 0
        # 坐席字符额度水位聚合巡检（2026-08-16）：warn（达 quota_alert_pct）/
        # over（超额）两桶聚合外发（见 _check_agent_quota）；未开 usage.agent_chars
        # 计量的部署恒静默，故父开关默认开无噪音风险。
        self._aq_alerted: bool = False
        self._aq_last_remind: float = 0.0
        self.total_agent_quota_alerts: int = 0
        # 被埋会话巡检（P0-198，2026-08-04）：归档着却有未读入站＝客户在等，而工作台
        # 所有默认视图都看不见它（见 _check_buried_conversations）。
        self._bc_alerted: bool = False
        self._bc_last_remind: float = 0.0
        self.total_buried_conv_alerts: int = 0
        # 幽灵未读巡检（#159，2026-09-03）：徽标口径与旧全库口径的差额——
        # 「账号栏显示 7、清单一条没有」这类事故不该靠客户截图才发现
        # （#145件④ 从 0817 挂到 0903 才等到一份带现场的诊断包）。
        self._pu_alerted: bool = False
        self._pu_last_remind: float = 0.0
        self.total_phantom_unread_alerts: int = 0
        # 案例积压巡检（2026-08-03 案例中心 P4）：AI 立了案没人认领/处理 →
        # 案例中心就退化回「永远 0 的看板」老病。聚合告警 + 危机级单独点名。
        # _cb_alerted / _cb_last_remind 是账本属性（键 case_backlog）。
        self.total_case_backlog_alerts: int = 0
        # 账号接入漏斗停摆巡检（2026-08-02）：ops 卡已能显示 stalled，但**看板要有人开
        # 才有用**——2026-07-25 的 LINE 扫码 100% 失败正是烂了多日无人知。这里把它升级
        # 为主动外发。state: key(platform:mode) → 上次告警时的 started 计数 / 上次提醒时刻。
        # 接入漏斗：只记「上次见到的累计 authorized」——恢复通知要正面证据（真的又成过
        # 一次），停摆判据本身由 stats 侧的 started_since_success 自带基线
        self._lf_base: Dict[str, int] = {}
        self._lf_alerted: Dict[str, int] = {}
        self._lf_last_remind: Dict[str, float] = {}
        self.total_login_funnel_alerts: int = 0
        # CSRF 写请求拦截激增巡检（P1 2026-07-31）：中间件拒绝已进 csrf_stats 看板，
        # 但看板要有人开才有用——窗口增量达阈值主动外发。样本=(ts,total,by_kind,by_path)。
        self._csrf_samples: List[tuple] = []
        self._csrf_alerted: bool = False
        self._csrf_last_remind: float = 0.0
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
        # 内嵌网页端选择器失配提醒（2026-08-10）：节流状态在 InjectHealthStore 内
        # （恢复自动清零），本类只记外发计数。
        self.total_inject_health_reminders: int = 0
        # 入站半死态提醒（P0 2026-08-04）：账号 authorized（登录探测绿）但侧栏有未读、
        # 进线程读取持续全失败＝看得到读不到（E2EE 卡 Loading 等）。节流状态在
        # PlatformSessionHealth 内（恢复自动清零），本类只记外发计数。
        self.total_inbox_read_stall_reminders: int = 0
        self.total_cloud_balance_alerts: int = 0
        self.total_license_quota_alerts: int = 0
        self.total_token_wallet_alerts: int = 0
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
        # D1b P0-5：自动推进有货零真发计数（节流位已迁 remind_ledger，见 _RK_GOAL_SENDS）
        self.total_goal_sprint_stall_alerts: int = 0
        self.last_check_ts: float = 0.0
        self.last_light: str = "green"

    async def run(self) -> None:
        self._running = True
        self._stop_evt.clear()
        logger.info("HealthWatchdog 已启动（interval=%.0fs alert_on_warn=%s surface_in_ui=%s）",
                    self._interval, self._alert_on_warn, self._ui_surface_enabled())
        # 启动后稍等，避开冷启动期的瞬时 fail（worker 尚未 running）
        try:
            await asyncio.wait_for(self._stop_evt.wait(), timeout=min(60.0, self._interval))
            return
        except asyncio.TimeoutError:
            pass
        lane_task = asyncio.create_task(self._compute_lane_loop())
        try:
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
        finally:
            lane_task.cancel()
            try:
                await lane_task
            except (asyncio.CancelledError, Exception):
                pass
            self._running = False
            logger.info("HealthWatchdog 已停止")

    async def _compute_lane_loop(self) -> None:
        """三路算力探活：独立于 5 分钟健康拍，约每分钟一圈，欠费/故障每 3 分钟催运维群。"""
        while not self._stop_evt.is_set():
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._check_compute_lanes)
            except Exception:
                logger.debug("算力三路巡检异常（已忽略）", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=60.0)
                return
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_evt.set()

    # ── 跨重启提醒状态账本（2026-09-10 运维群降噪 P0.2/P0.3）───────────────────
    @property
    def _remind(self):
        """提醒账本（懒建：单测常用 ``__new__`` 绕过 ``__init__`` 直接调巡检方法）。"""
        led = self.__dict__.get("_remind_ledger")
        if led is None:
            led = self._build_remind_ledger()
            self.__dict__["_remind_ledger"] = led
        return led

    def _build_remind_ledger(self):
        from src.inbox.remind_ledger import RemindLedger, state_path
        cm = getattr(self, "_config_manager", None)
        cfg_path = str(getattr(cm, "config_path", "") or "").strip()
        path = None
        if cfg_path:
            try:
                from pathlib import Path as _RLPath
                path = state_path(_RLPath(cfg_path).parent)
            except Exception:
                path = None
        try:
            return RemindLedger(path)
        except Exception:
            logger.debug("提醒状态账本初始化失败，退化为内存态", exc_info=True)
            return RemindLedger(None)

    @staticmethod
    def _unchanged_interval_sec(block: Dict[str, Any], interval_sec: float,
                                default_min: Optional[float] = None) -> float:
        """内容指纹未变时的重提间隔：配置 ``unchanged_interval_min``；未配时取 ``default_min``
        （积压类巡检传 1440＝「同样那几条稿 / 案例」一天提一次足够），``default_min`` 为 None
        则与常规间隔相同（硬件掉线类：还没恢复就照常提）。结果不短于常规间隔。"""
        raw = (block or {}).get("unchanged_interval_min")
        if raw in (None, ""):
            if default_min is None:
                return float(interval_sec)
            mins = float(default_min)
        else:
            try:
                mins = float(raw)
            except (TypeError, ValueError):
                mins = float(default_min or 0.0)
        return max(float(interval_sec), mins * 60.0)

    # 旧属性名保留为账本视图（巡检代码 / 单测沿用 `_db_alerted` 等写法零改动）
    _RK_DRAFT = "draft_backlog"
    _RK_LABEL = "label_leak"
    _RK_FE = "frontend_error"
    _RK_CASE = "case_backlog"
    _RK_UNANSWERED = "unanswered_inbound"
    _RK_AVATAR = "avatar_voice"
    _RK_LAN_GPU = "lan_gpu:"
    # 2026-09-16：goal stall 节流落盘——重启不再 1 分钟内整轮重发
    _RK_GOAL_SENDS = "goal_sprint_sends"
    _RK_GOAL_STALL = "goal_sprint_goal:"

    @property
    def _goal_sprint_stall_alerted(self) -> float:
        """兼容旧单测/日志：账本已告过警时返回 last_remind，否则 0。"""
        return (self._remind.last_remind(self._RK_GOAL_SENDS)
                if self._remind.alerted(self._RK_GOAL_SENDS) else 0.0)

    @_goal_sprint_stall_alerted.setter
    def _goal_sprint_stall_alerted(self, v: float) -> None:
        ts = float(v or 0.0)
        if ts <= 0:
            self._remind.resolve(self._RK_GOAL_SENDS)
            return
        self._remind.mark_sent(self._RK_GOAL_SENDS, now=ts)

    @property
    def _goal_stalled_alerted(self) -> Dict[str, float]:
        """单目标 stall 节流视图：goal_id → last_remind（只读快照，写入走账本）。"""
        prefix = self._RK_GOAL_STALL
        out: Dict[str, float] = {}
        for key in self._remind.keys():
            if not str(key).startswith(prefix):
                continue
            if not self._remind.alerted(key):
                continue
            out[str(key)[len(prefix):]] = self._remind.last_remind(key)
        return out

    @_goal_stalled_alerted.setter
    def _goal_stalled_alerted(self, v: Any) -> None:
        # 旧测试偶发整表赋值；新路径以账本为准，赋值仅用于清零兼容。
        if not v:
            prefix = self._RK_GOAL_STALL
            for key in list(self._remind.keys()):
                if str(key).startswith(prefix):
                    self._remind.resolve(key)

    @property
    def _db_alerted(self) -> bool:
        return self._remind.alerted(self._RK_DRAFT)

    @_db_alerted.setter
    def _db_alerted(self, v: bool) -> None:
        self._remind.set_alerted(self._RK_DRAFT, bool(v))

    @property
    def _db_last_remind(self) -> float:
        return self._remind.last_remind(self._RK_DRAFT)

    @_db_last_remind.setter
    def _db_last_remind(self, v: float) -> None:
        self._remind.set_last_remind(self._RK_DRAFT, float(v or 0.0))

    @property
    def _cb_alerted(self) -> bool:
        return self._remind.alerted(self._RK_CASE)

    @_cb_alerted.setter
    def _cb_alerted(self, v: bool) -> None:
        self._remind.set_alerted(self._RK_CASE, bool(v))

    @property
    def _cb_last_remind(self) -> float:
        return self._remind.last_remind(self._RK_CASE)

    @_cb_last_remind.setter
    def _cb_last_remind(self, v: float) -> None:
        self._remind.set_last_remind(self._RK_CASE, float(v or 0.0))

    @property
    def _ui_alerted(self) -> bool:
        return self._remind.alerted(self._RK_UNANSWERED)

    @_ui_alerted.setter
    def _ui_alerted(self, v: bool) -> None:
        self._remind.set_alerted(self._RK_UNANSWERED, bool(v))

    @property
    def _ui_last_remind(self) -> float:
        return self._remind.last_remind(self._RK_UNANSWERED)

    @_ui_last_remind.setter
    def _ui_last_remind(self, v: float) -> None:
        self._remind.set_last_remind(self._RK_UNANSWERED, float(v or 0.0))

    @property
    def _avatar_alerted(self) -> bool:
        return self._remind.alerted(self._RK_AVATAR)

    @_avatar_alerted.setter
    def _avatar_alerted(self, v: bool) -> None:
        self._remind.set_alerted(self._RK_AVATAR, bool(v))

    @property
    def _avatar_last_remind(self) -> float:
        return self._remind.last_remind(self._RK_AVATAR)

    @_avatar_last_remind.setter
    def _avatar_last_remind(self, v: float) -> None:
        self._remind.set_last_remind(self._RK_AVATAR, float(v or 0.0))

    @property
    def _avatar_down_since(self) -> float:
        return self._remind.first_seen(self._RK_AVATAR)

    @_avatar_down_since.setter
    def _avatar_down_since(self, v: float) -> None:
        self._remind.set_first_seen(self._RK_AVATAR, float(v or 0.0))

    @property
    def _avatar_alert_kind(self) -> str:
        return str(self._remind.meta(self._RK_AVATAR, "kind", "") or "")

    @_avatar_alert_kind.setter
    def _avatar_alert_kind(self, v: str) -> None:
        self._remind.set_meta(self._RK_AVATAR, "kind", str(v or ""))

    def _lan_gpu_host_state(self, root: str) -> Dict[str, float]:
        """某 LAN GPU 主机的提醒状态：内存副本缺席时从账本回灌（重启后接着算时长/间隔）。"""
        st = self._lan_gpu_state.get(root)
        if st is None:
            rec = self._remind.get(self._RK_LAN_GPU + root)
            st = {
                "down_since": float(rec.get("first_seen") or 0.0),
                "alerted": 1.0 if rec.get("alerted") else 0.0,
                "last_remind": float(rec.get("last_remind") or 0.0),
            }
            self._lan_gpu_state[root] = st
        return st

    def _lan_gpu_persist(self, root: str, st: Dict[str, float], *, fp: Optional[str] = None,
                         summary: Optional[str] = None) -> None:
        key = self._RK_LAN_GPU + root
        if not st.get("down_since") and not st.get("alerted"):
            self._remind.resolve(key)
            return
        self._remind.set_first_seen(key, float(st.get("down_since") or 0.0))
        self._remind.set_alerted(key, bool(st.get("alerted")))
        self._remind.set_last_remind(key, float(st.get("last_remind") or 0.0))
        if fp is not None:
            self._remind.set_meta(key, "fp", fp)
        if summary is not None:
            self._remind.set_meta(key, "summary", summary[:200])

    @staticmethod
    def _fmt_hours(hours: float) -> str:
        h = max(0.0, float(hours or 0.0))
        if h < 1:
            return f"{int(round(h * 60))} 分钟"
        if h < 48:
            return f"{h:.0f} 小时"
        d = h / 24.0
        return f"{d:.1f} 天" if d < 10 else f"{d:.0f} 天"

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

        # 启动一次性卫生扫描（首 tick 跑一次；store 未就绪则下 tick 再试）
        if not self._hygiene_done:
            try:
                self._startup_hygiene()
            except Exception:
                logger.debug("启动卫生扫描异常（已忽略）", exc_info=True)

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

        # 接管自动接回（P0 2026-08-09）：坐席手动出站把会话钉在 manual 后，
        # 静默超时自动恢复档位（takeover_rearm，默认关；只碰 source=takeover*）。
        try:
            self._check_takeover_rearm()
        except Exception:
            logger.debug("接管接回巡检异常（已忽略）", exc_info=True)

        # 真发总闸自动过期（#142 2026-09-02，默认关）：总闸被顺手关下后没人记得
        # 打开＝全自动会话长期静默只拟稿。配置 pause_auto_resume_hours>0 时关满
        # N 小时自动恢复 + 群外通知；只撤销有留痕的翻动（yaml 手改不动）。
        try:
            self._check_autosend_gate_expiry()
        except Exception:
            logger.debug("真发总闸自动过期巡检异常（已忽略）", exc_info=True)

        # AI 对聊提醒（P0-6 2026-08-09）：全自动会话双向高频互发（受管账号
        # 互聊/测试是允许行为）→ 只标记+提醒绝不拦截；默认开、独立去抖。
        try:
            self._check_mutual_chat()
        except Exception:
            logger.debug("AI 对聊提醒巡检异常（已忽略）", exc_info=True)

        # 停泊态草稿卡死回收（P1 2026-08-09）：enriching 中途进程重启 → 草稿
        # 永久停泊 + 幂等保护挡住该会话所有后续拟稿（默认开，状态机泄漏修复）。
        try:
            self._check_stale_enriching()
        except Exception:
            logger.debug("停泊草稿回收异常（已忽略）", exc_info=True)

        # 自动化覆盖率按日快照（P2 2026-08-09，默认关）：给覆盖率卡供趋势线
        # ——「有效全自动占比在掉」比当下值更需要行动。
        try:
            self._check_coverage_trend()
        except Exception:
            logger.debug("覆盖率趋势快照异常（已忽略）", exc_info=True)

        # 平台会话持续掉线提醒（P4）：worker push 的掉线转移只告警一次，若长时间没人修
        # 则周期性再提醒（升级式：after_min 首提，之后每 interval_min 一条）。
        try:
            self._check_platform_sessions()
        except Exception:
            logger.debug("平台会话持续掉线巡检异常（已忽略）", exc_info=True)

        # stderr 风暴哨兵（2026-08-27 06:11 宕机沉淀）：boot err 日志异常增长＝
        # 进程内有「每条日志都在倒堆栈」级别的风暴（当日 45min 写满 1.5GB 拖死
        # 实例，全程零告警）。增长超阈值即经 host_alert 点名，早于进程被拖死。
        try:
            self._check_stderr_storm()
        except Exception:
            logger.debug("stderr 风暴哨兵异常（已忽略）", exc_info=True)

        # Messenger worker 代码分叉提醒（实施74/实施69 P1-4）：worker 自报
        # code_stale 持续超阈 → 点名重启装载，消「改了没重启」27 小时盲区。
        try:
            self._check_messenger_code_stale()
        except Exception:
            logger.debug("code_stale 巡检异常（已忽略）", exc_info=True)

        # 人工接管超时提醒（驾驶舱 P0 2026-08-13）：坐席接管会话后忘了交还——
        # 接管期间该客户的 AI 全停＝没人管，超时必须有人被点名。
        try:
            self._check_takeover_overdue()
        except Exception:
            logger.debug("接管超时巡检异常（已忽略）", exc_info=True)

        # 危机升级 → 工作台落点桥（#185 D7 2026-09-05）：R8 此前只走 webhook，
        # 桌面包没配 ⇒ severe 升级没人被叫到。首 tick 装落库监听器（推），之后
        # 每 tick 补扫水位以上的升级事件（扫）→ 需人工徽标 + 置顶 + 案例三落点。
        try:
            self._check_crisis_escalation_bridge()
        except Exception:
            logger.debug("危机升级桥巡检异常（已忽略）", exc_info=True)

        # 内嵌网页端选择器持续失配（2026-08-10）：登录态好着、页面也在，但注入脚本抓
        # 不到气泡/输入框＝官方改版了。与上面「会话不健康」正交（那个重登、这个改选择器）。
        try:
            self._check_inject_health()
        except Exception:
            logger.debug("注入健康巡检异常（已忽略）", exc_info=True)

        # 入站半死态（P0 2026-08-04）：账号登录探测绿、侧栏有未读、进线程读取持续全失败
        # ＝看得到读不到（E2EE 卡 Loading 实锤 4 天零告警）。与上面「会话不健康」正交。
        try:
            self._check_inbox_read_stall()
        except Exception:
            logger.debug("入站半死态巡检异常（已忽略）", exc_info=True)

        # Messenger 注册表×sidecar 会话对账（P2 2026-08-13）：期望在线的号在 sidecar
        # 无会话（重启恢复失败/会话被清）→ 上面两条巡检都看不见（零事件零心跳），单独对账。
        try:
            self._check_messenger_not_restored()
        except Exception:
            logger.debug("messenger 会话对账巡检异常（已忽略）", exc_info=True)

        # 常备扫描循环停摆（P4 2026-08-09）：目标结算/提醒 + 工作链推进的心跳
        # 停走即告警——两者都曾挂死调度器静默从未运行，这类病不许再靠人发现。
        try:
            self._check_scan_loop_stall()
        except Exception:
            logger.debug("扫描循环停摆巡检异常（已忽略）", exc_info=True)

        # AvatarHub 7852 持续掉线升级提醒：黄灯只在看板可见（alert_on_warn=False），
        # 掉线超阈值后主动外发（首提 + 周期重提，恢复补发恢复通知）。
        try:
            self._check_avatar_voice()
        except Exception:
            logger.debug("AvatarHub 语音巡检异常（已忽略）", exc_info=True)
        # hub 引擎目录离线哨兵（2026-08-22「fish 冒充 IndexTTS-2」事故）：
        # 钉的引擎在目录里 available:false → 顶包/拒发，且**零流量时无人知**。
        try:
            self._check_hub_engine()
        except Exception:
            logger.debug("hub 引擎目录巡检异常（已忽略）", exc_info=True)
        # 出图模型「被删」哨兵（2026-08-22 事故：176 模型整树被清空数小时无人知）。
        try:
            self._check_image_models()
        except Exception:
            logger.debug("出图模型巡检异常（已忽略）", exc_info=True)
        # 语音出站断档：探针绿也可能整链拒发（hub 音色档 404 实锤 5 天零告警），
        # 按 voice_outage 台账滚动窗判「有请求全在失败」，低流量同样敏感。
        try:
            self._check_voice_outage()
        except Exception:
            logger.debug("语音出站断档巡检异常（已忽略）", exc_info=True)
        # LAN GPU 主机宕机升级提醒（嵌入/视觉/兜底 LLM/本地 MT 所在主机整机下线时，
        # 各链路静默转移备点——业务不断，但冗余归零必须有人知道）。
        try:
            self._check_lan_gpu_hosts()
        except Exception:
            logger.debug("LAN GPU 主机巡检异常（已忽略）", exc_info=True)
        try:
            self._check_compute_lanes()
        except Exception:
            logger.debug("算力三路巡检异常（已忽略）", exc_info=True)

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

        # 系统标签泄漏：AI 出站正文以「[我方语音消息]」类系统标注开头＝已发给客户的穿帮
        # （2026-09-12 事故 7 分钟 4 例，靠坐席截图才发现）
        try:
            self._check_label_leak()
        except Exception:
            logger.debug("标签泄漏巡检异常（已忽略）", exc_info=True)

        # 前端脚本 bug（ReferenceError / SyntaxError beacon）：模板热更新直上生产，坏符号
        # 一出现全体坐席同时踩；beacon 与 ops 卡早就有，缺的是「有人被叫醒」这一环
        # （2026-09-15 `_psnArRender` 人设工坊整体不可用 3.5 天零告警）
        try:
            self._check_frontend_errors()
        except Exception:
            logger.debug("前端脚本错误巡检异常（已忽略）", exc_info=True)

        # 报障 outbox 补传兜底：原只挂在 hosted_gateway 的每小时刷新轮上，而该轮
        # 只在托管/桌面版启动（_wants_hosted）——源码态 / 自建服实例的「网络恢复后
        # 自动补传」从未跑过（2026-09-16 zhiliao 实锤：坏件躺 8h 无人补传）
        try:
            self._check_diag_outbox()
        except Exception:
            logger.debug("报障 outbox 补传巡检异常（已忽略）", exc_info=True)

        # 托管代理生命周期（一键代理 P2）：自动续期 / 到期回收 / 低库存预警——
        # 缺这一环＝「代理过期了坐席还在用它登号」（连接莫名失败，排查成本极高）
        try:
            self._check_proxy_managed()
        except Exception:
            logger.debug("托管代理巡检异常（已忽略）", exc_info=True)

        # 账号真相真幽灵：会话库有、注册表没有（剔除 web 工作台 / 桌面镜像）
        try:
            self._check_accounts_truth()
        except Exception:
            logger.debug("账号真相巡检异常（已忽略）", exc_info=True)

        # 回复额度触顶聚合：多个会话同日烧穿预算＝系统性状况（额度配小/bot 波次），
        # 逐会话告警看不出「面」；含 near（≥80%）计数给提前量
        try:
            self._check_reply_budget()
        except Exception:
            logger.debug("回复额度巡检异常（已忽略）", exc_info=True)

        # 坐席字符额度水位：路由层 enforce 闸默认关（软提醒先行），运营对「谁快用满/
        # 谁已超额」的唯一主动信号就是这条聚合告警——没有它，软提醒期=没人知道
        try:
            self._check_agent_quota()
        except Exception:
            logger.debug("坐席字符额度巡检异常（已忽略）", exc_info=True)

        # 入站漏球：最后一条是客户消息、既没回也没拟稿——draft_backlog 只覆盖
        # 「有稿没人处理」，这里补「压根没稿」的盲区（2026-08-05 实锤：客户 22:27
        # 高意向消息整晚零出站零草稿，无任何信号）
        try:
            self._check_unanswered_inbound()
        except Exception:
            logger.debug("入站漏球巡检异常（已忽略）", exc_info=True)

        # 被埋会话：归档着却有未读——客户在等，工作台所有默认视图都看不见它
        try:
            self._check_buried_conversations()
        except Exception:
            logger.debug("被埋会话巡检异常（已忽略）", exc_info=True)

        # 幽灵未读（#159）：徽标口径与旧全库口径的差额——账号栏那个数字里有
        # 多少是「清单上点不开」的残值。等客户截图发现太慢（#145件④ 等了两周）。
        try:
            self._check_phantom_unread()
        except Exception:
            logger.debug("幽灵未读巡检异常（已忽略）", exc_info=True)

        # 案例积压：AI 判定「需要人跟进」的案例无人认领/处理——危机级尤其不能等
        try:
            self._check_case_backlog()
        except Exception:
            logger.debug("案例积压巡检异常（已忽略）", exc_info=True)

        # 演练残影自动清扫：白天 ad-hoc 对练留下的 drill 案例不该等人手点清
        try:
            self._check_drill_hygiene()
        except Exception:
            logger.debug("演练残影清扫异常（已忽略）", exc_info=True)

        # 账号接入漏斗停摆：某平台某方式「发起 N 次、成功 0 次」= 那条登录链路事实上
        # 不可用。看板能看见，但得有人开；这里主动轰人（零流量天然静默）。
        try:
            self._check_login_funnel()
        except Exception:
            logger.debug("接入漏斗巡检异常（已忽略）", exc_info=True)

        # CSRF 写请求拦截激增：某前端宿主写通道断了（cookie_no_header）或有人在
        # 跨站探测——2026-07 人设切换事故静默烂了两周，这类信号必须主动轰人
        try:
            self._check_csrf_rejects()
        except Exception:
            logger.debug("CSRF 拦截巡检异常（已忽略）", exc_info=True)

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

        # 四域真活探针（2026-08-17 无兜底纪律规则 8）：真合成/真翻译/真识图/真转写。
        # health 200 不算活——8/16 index CPU 爬行全天断档零告警的机制化收口。
        try:
            self._check_true_probes()
        except Exception:
            # 刻意用 WARNING 而非本文件惯用的 DEBUG：探针**本身就是兜底机制**，
            # 它静默停摆是最坏的失败模式。2026-08-27 实锤：接线漏了一个局部 Path
            # 导入 → NameError 被 DEBUG 吞掉 → 探针从重启起整段不跑、日志零痕迹，
            # 只有「轮次完成那行不再出现」这一个极易被忽略的负面信号。
            logger.warning("真活探针巡检异常（本轮跳过，探针未生效）", exc_info=True)

        # 探针停摆自检（同日续）：上面那条 WARNING 只进日志，没人读。本检查**独立
        # 于** _check_true_probes（它整段抛异常时仍会跑），按状态文件是否陈旧判断
        # 「探针自己死了」并外发。零误报由 stalled_verdict 的构造保证。
        try:
            self._check_probe_stalled()
        except Exception:
            # 与 _check_true_probes 同理用 WARNING：这条是「看门狗的看门狗」，是最后
            # 一道防线，它自己静默死掉就再没有任何东西会发现探针停摆。
            # （2026-08-27 `tools/watchdog_check_audit.py` 首跑就把它标成 BLIND——
            #  我当天刚加的检查，自己没有任何正面信号。审计抓的第一个就是作者本人。）
            logger.warning("探针停摆自检异常（本轮跳过，停摆将无人发现）", exc_info=True)

        # 本地主链保险（2026-08-15）：local_only 语义下 vLLM 猝死＝全站 canned 且
        # 绝不回落云端——中枢执行器救不回来的窗口由本检查单向热切 cloud 兜住。
        try:
            self._check_ai_primary_guard()
        except Exception:
            logger.debug("本地主链保险巡检异常（已忽略）", exc_info=True)

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

        # Telegram 历史自动补缺口（2026-08-02）：实时镜像只覆盖在线时段，停机窗口
        # 丢的消息不会自己回来 → 到期账号自动跑一轮账号级云端同步（dedup 落库，
        # 不触发自动回复/SSE），重启/断线后缺口 ≤per_chat 条即自愈。
        try:
            self._check_tg_history_autosync()
        except Exception:
            logger.debug("tg 历史自动同步巡检异常（已忽略）", exc_info=True)

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

        # 授权字符额度水位巡检（P4c）：临近/触顶 → host_alert 主动外发并指路
        # 「会员中心 → 兑换加量包」，替代「翻译突然被拦才发现额度没了」。
        # 无授权/不限量部署天然静默（included=0 直接返回），零误报零开销。
        try:
            self._check_license_quota()
        except Exception:
            logger.debug("授权额度巡检异常（已忽略）", exc_info=True)

        # Token 钱包水位（quotawall v2 P2-4）：enforce 生效的部署钱包耗尽＝AI 增强
        # 功能静默降级免费路径（服务不断线但体验降档）——降级 pill 是被动面，这里
        # 补主动外发；影子记账（enforce=false）期不告警（那个阶段看 ops 影子卡）。
        try:
            self._check_token_wallet()
        except Exception:
            logger.debug("Token 钱包巡检异常（已忽略）", exc_info=True)

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

        # 撤销设定复活巡检（2026-08-04）：直改 profiles_runtime.yaml 的写入方
        # （agent 批量丰富/运维手改）绕过 Studio 保存路径的 retired_conflicts 检测
        # → 以内存态（热重载几秒内跟文件）兜底扫「被删的设定又被写回档案」。
        try:
            self._check_persona_retired_conflicts()
        except Exception:
            logger.debug("撤销设定复活巡检异常（已忽略）", exc_info=True)

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

        # D1b P0-5：引擎说能发、库里有够老的 auto 目标，24h 却零真发。
        try:
            self._check_goal_sprint_liveness()
        except Exception:
            logger.debug("自动推进真发巡检异常（已忽略）", exc_info=True)

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

        # WP-3：老板日报自动外发（每日节流一次，默认关；与周报同通道不冲突）。
        try:
            self._maybe_daily_report()
        except Exception:
            logger.debug("运营日报生成异常（已忽略）", exc_info=True)

        # P2.2（2026-09-10）：公网入口可达性——只驱动卡片链接回落内网，不另发告警。
        try:
            self._check_public_link()
        except Exception:
            logger.debug("公网入口探测异常（已忽略）", exc_info=True)

        # P1.2（2026-09-10）：每日运维摘要（固定钟点一张卡：未处理项/探针/昨日花费；默认关）。
        try:
            self._maybe_daily_digest()
        except Exception:
            logger.debug("每日运维摘要异常（已忽略）", exc_info=True)

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
        影响）：``online`` 是「编排器会去拉起、掉了就该有人修」的号。已删除（无记录/
        removed）的号编排器根本不会拉起（``desired_accounts`` 只收 online），催也无从
        下手。注册表读不出来 → 返回 True，不因巡检自身故障漏报真掉线。

        ``offline`` 分两档（2026-08-16，messenger 号自灭 17 天零提醒的事故修）：
        - ``meta.offline_reason`` 以 ``worker:`` 开头＝**自灭型掉线**（cookie 失效/
          风控/崩溃循环放弃，由 session-status push 或 ``report_session_transition``
          翻状态时落标）——没人决定退役它，属「该有人修」，**继续催**；重新登录
          （authorized 归位清标记）或删除账号即自然停催。
        - ``operator``（运营主动登出，``_clear_session_creds`` 落标）或**无标记**
          （历史存量行，来历不明——宁静默不误催）→ 不催，维持旧行为。

        2026-08-27 状态中心 v2：判据本体移居
        ``platform_session_health.session_expected_online``（坐席横幅快照同源
        消费——催不催与亮不亮必须一个口径），本方法保留为委托壳（既有调用点/
        测试零改动）。
        """
        from src.integrations.platform_session_health import (
            session_expected_online,
        )
        return session_expected_online(key)

    def _check_inject_health(self, *, now: Optional[float] = None) -> None:
        """内嵌网页端「选择器持续失配」提醒（2026-08-10）。

        桌面壳把官方网页端嵌进 webview 再注入脚本做翻译/回流，这套模式的长期命门就是
        **官方一改版选择器就失配**。此前失配态只在壳层状态条 + 运营看板可见（
        ``/api/desktop/inject-health/alerts``），没有任何外发出口——这正是本仓反复吃过的
        「报进虚空」：L3 草稿在 SLA 覆盖内烂了 167h，就因为最后一公里从未接通。

        与 ``_check_platform_sessions`` 正交：那条盯「会话掉线/需重登」（登录态问题），
        这条盯「登录态好着、页面也在，但脚本抓不到东西」（DOM 契约问题），修法完全不同
        （前者重登、后者改选择器覆写层 ``config/desktop_selector_profiles.json``）。

        升级式：失配持续 ``after_min`` → 首提，之后每 ``interval_min`` 一条；节流状态在
        ``InjectHealthStore`` 内（恢复自动清零）。**陈旧上报一律不催**（壳已关/账号已卸，
        见 ``due_reminders`` 的 stale 防线）。配置
        ``health_watchdog.inject_health_remind.{enabled,after_min,interval_min}``
        （默认开，20min 首提 / 4h 重提）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        sr = (((cfg.get("health_watchdog") or {}).get("inject_health_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not sr.get("enabled", True):
            return
        try:
            from src.web.desktop_inject_health import (
                EXTRACT_STATUS_KEYS, get_inject_health_store, missing_selectors,
            )
            store = get_inject_health_store()
        except Exception:
            return
        after_sec = max(60.0, float(sr.get("after_min", 20) or 20) * 60.0)
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
        for key, rec in due.items():
            status = str(rec.get("status") or "")
            # 该校准哪个字段：提取器类失效在 selectors 布尔表里全绿（元素确实在），
            # 只能按 status 反推，否则运营会被引去改一个没坏的选择器。
            field = EXTRACT_STATUS_KEYS.get(status) or (
                "composer" if status == "mismatch_composer" else "bubble")
            missing = missing_selectors(rec)
            bus.publish("inject_health_alert", {
                "platform": str(rec.get("platform") or ""),
                "account_id": str(rec.get("account_id") or ""),
                "status": status,
                "field": field,
                "generic": bool(rec.get("generic")),
                "bubbles": int(rec.get("bubbles") or 0),
                "missing_selectors": missing,
                "down_minutes": int(float(rec.get("mismatch_secs") or 0) // 60),
                "first_reminder": bool(rec.get("first_reminder")),
                # 独立限流键：多账号互不挤占 notifier 的限流窗（与会话类告警也分开）。
                "rate_key": f"inject:{key}",
            })
            self.total_inject_health_reminders += 1

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
                ensure_seeded_from_registry, get_platform_session_health,
            )
            # 种子化自给（2026-08-16）：健康表是进程内存态，重启后离线号靠注册表
            # 种子接续——此前种子只挂在 web 读路径上惰性触发，看门狗若先于任何
            # 页面访问跑到这里会对「重启前就死了的号」失明。幂等，进程内只种一次。
            ensure_seeded_from_registry()
            store = get_platform_session_health()
        except Exception:
            return
        after_sec = max(60.0, float(sr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(sr.get("interval_min", 240) or 240) * 60.0)
        # 催办衰减（2026-08-27）：掉线超 stale_after_hours（默认 48h）的号降频为
        # stale_interval_min（默认 1440=日更）——「每 4h 轰一个没人修的死号」只会
        # 让人把告警频道整体静音；置 0 关衰减恢复旧行为。
        stale_after_sec = max(0.0, float(
            sr.get("stale_after_hours", 48) or 0) * 3600.0)
        stale_interval_sec = max(0.0, float(
            sr.get("stale_interval_min", 1440) or 0) * 60.0)
        due = store.due_reminders(min_age_sec=after_sec, interval_sec=interval_sec,
                                  now=now,
                                  stale_after_sec=stale_after_sec,
                                  stale_interval_sec=stale_interval_sec)
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

    def _check_stderr_storm(self, *, now: Optional[float] = None,
                            logs_dir: Optional[str] = None) -> None:
        """stderr 风暴哨兵（2026-08-27 06:11 宕机沉淀）。

        事故：日志轮转被外部句柄挡死 + okline 零退避重连共振 → 每条日志向
        stderr 倒整段堆栈 → boot err 日志 45 分钟写满 1.5GB、web 监听被拖死，
        全程零告警（err 日志不经 logging 体系，既有健康面全部看不见它）。

        判据（``stderr_storm_verdict`` 纯函数）：最新 ``boot_*.err.log`` 两次
        巡检间**绝对增量** ≥ ``growth_mb``（默认 15MB）→ 经 ``notify_host``
        告警（日志 + EventBus host_alert 镜像 + 算力机弹窗，自带 30min 冷却）。
        新 boot（文件名变）自动重建基线；logs 目录按进程 CWD 解析（服务进程
        CWD＝实例数据根，boot 日志正落在那里的 ``logs/``）。

        配置 ``health_watchdog.stderr_storm.{enabled,growth_mb}``（默认开/15）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        sc = (((cfg.get("health_watchdog") or {}).get("stderr_storm"))
              or {}) if isinstance(cfg, dict) else {}
        if not sc.get("enabled", True):
            return
        growth_bytes = max(1, int(float(sc.get("growth_mb", 15) or 15)
                                  * 1024 * 1024))
        import glob
        import os
        base = logs_dir or "logs"
        try:
            candidates = glob.glob(os.path.join(base, "boot_*.err.log"))
            if not candidates:
                return
            newest = max(candidates, key=os.path.getmtime)
            cur = {"path": newest, "size": int(os.path.getsize(newest))}
        except OSError:
            return
        prev = getattr(self, "_stderr_state", None)
        self._stderr_state = cur
        verdict = stderr_storm_verdict(prev, cur, growth_bytes=growth_bytes)
        if not verdict["alert"]:
            return
        self.total_stderr_storm_alerts = getattr(
            self, "total_stderr_storm_alerts", 0) + 1
        grew_mb = verdict["grew"] / 1024 / 1024
        try:
            from src.utils.host_alert import notify_host
            notify_host(
                "stderr 风暴（进程级日志异常）",
                (f"{os.path.basename(cur['path'])} 在一个巡检间隔内增长 "
                 f"{grew_mb:.0f}MB——进程内可能存在「每条日志倒堆栈」级风暴"
                 f"（日志轮转被占用/第三方库热循环）。请尽快查看该文件尾部定位"
                 f"根因；置之不理可能拖死实例（2026-08-27 06:11 实锤）。"),
                key="stderr_storm", cooldown_sec=1800.0,
            )
        except Exception:
            logger.warning("[watchdog] stderr 风暴：%s 增长 %.0fMB（host_alert "
                           "出口异常，仅本地记录）", cur["path"], grew_mb)

    def _fetch_messenger_worker_snapshot(self) -> Optional[Dict[str, Any]]:
        """读 messenger worker ``GET /accounts`` 的 ``worker`` 段；不可达返回 None。

        独立成方法＝测试可 monkeypatch；阻塞 HTTP 安全（tick 在线程池里跑）。
        """
        try:
            from src.integrations.messenger_web_login import service_base_url
            cfg = getattr(self._config_manager, "config", None) or {}
            import httpx
            r = httpx.get(f"{service_base_url(cfg)}/accounts", timeout=5.0)
            r.raise_for_status()
            data = r.json()
            w = data.get("worker") if isinstance(data, dict) else None
            return w if isinstance(w, dict) else {}
        except Exception:
            return None

    def _check_messenger_code_stale(self, *, now: Optional[float] = None) -> None:
        """实施74（实施69 P1-4）：messenger worker「代码分叉」升级提醒。

        实锤（实施69 §1.1）：实施68 的 PIN/中继修复在盘上躺了 **27 小时**没被
        在跑 worker 装载——`code_stale:true` 是 worker 的优秀自报，但只躺在
        /accounts 响应里无人消费（Python 实例重启 ≠ Node worker 重启，两个
        动作极易只做前者）。本检查把它接进告警：

        - 判据＝worker 自报 ``code_stale=true`` 持续 ≥ ``after_min``（默认 30，
          给「先落盘、稍后随窗重启」的正常发布节奏留缓冲）→ ``notify_host``
          （key 固定 + ``interval_min`` 冷却重提）；
        - 恢复（false / worker 重启换 boot）→ 曾告警才补恢复通知，状态清零；
        - messenger web 未启用 / worker 不可达 → 静默返回不计时（可达性归
          session_stale_remind / relogin 分诊管，本检查只管「活着但代码旧」）。

        配置 ``health_watchdog.code_stale_remind.{enabled,after_min,interval_min}``
        （默认 开/30/240）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        cs = (((cfg.get("health_watchdog") or {}).get("code_stale_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not cs.get("enabled", True):
            return
        try:
            from src.integrations.messenger_web_login import web_enabled
            if not web_enabled(cfg):
                return
        except Exception:
            return
        ts = float(now if now is not None else time.time())
        after_sec = max(60.0, float(cs.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(
            cs.get("interval_min", 240) or 240) * 60.0)
        snap = self._fetch_messenger_worker_snapshot()
        if snap is None:
            return  # 不可达：不计时不清零（缺证据不动状态）
        stale = bool(snap.get("code_stale"))
        if not stale:
            if getattr(self, "_code_stale_alerted", False):
                try:
                    from src.utils.host_alert import notify_host
                    notify_host(
                        "Messenger worker 代码已同步",
                        "worker 已装载磁盘最新代码（code_stale 清零）。",
                        key="msgr_code_stale_ok", cooldown_sec=60.0)
                except Exception:
                    logger.debug("[code_stale] 恢复通知失败", exc_info=True)
            self._code_stale_since = 0.0
            self._code_stale_alerted = False
            return
        since = float(getattr(self, "_code_stale_since", 0.0) or 0.0)
        if since <= 0:
            self._code_stale_since = ts
            return
        if ts - since < after_sec:
            return
        stale_min = int((ts - since) // 60)
        try:
            from src.utils.host_alert import notify_host
            fired = notify_host(
                "Messenger worker 代码分叉",
                (f"messenger-web worker 在跑代码落后磁盘已 ≥{stale_min} 分钟"
                 f"（code_stale=true，boot_ts={snap.get('boot_ts', '?')}）。"
                 f"盘上的修复没有被装载——请在合适窗口重启 worker"
                 f"（计划任务 Messenger-Web-Service 会用新代码自动拉活）。"
                 f"实施69 实锤：该分叉曾静默存活 27 小时。"),
                key="msgr_code_stale", cooldown_sec=interval_sec)
            if fired:
                self._code_stale_alerted = True
                self.total_code_stale_alerts = getattr(
                    self, "total_code_stale_alerts", 0) + 1
        except Exception:
            logger.debug("[code_stale] 告警外发失败", exc_info=True)

    def _check_takeover_overdue(self, *, now: Optional[float] = None) -> None:
        """人工接管超时提醒（驾驶舱 P0，2026-08-13）。

        「一键接管」把会话切 manual、AI 全停——坐席处理完**忘了交还**时，该客户
        从此没人管（AI 停着、人以为完事了），必须有升级式提醒点名。
        配置 ``health_watchdog.takeover_remind.{enabled,after_min,interval_min}``
        （默认开，2h 首提 / 之后每 1h 一条）。只对**进行中**接管提醒；交还即
        自动静默（节流表按 conversation_id 清理）。fail-open：注册表读取异常
        本轮跳过。提醒经 EventBus ``takeover_alert``（订阅别名 ``takeover``），
        rate_key 按会话区分，多个超时接管互不挤限流窗。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        tr_cfg = (((cfg.get("health_watchdog") or {}).get("takeover_remind"))
                  or {}) if isinstance(cfg, dict) else {}
        if not tr_cfg.get("enabled", True):
            return
        after_sec = max(300.0, float(tr_cfg.get("after_min", 120) or 120) * 60.0)
        interval_sec = max(600.0, float(
            tr_cfg.get("interval_min", 60) or 60) * 60.0)
        try:
            from src.inbox.takeover import list_active, overdue_takeovers
            overdue = overdue_takeovers(after_sec, now=now)
            active_ids = {str(e.get("conversation_id") or "")
                          for e in list_active()}
        except Exception:
            logger.debug("接管注册表读取失败（本轮跳过）", exc_info=True)
            return
        ts = time.time() if now is None else float(now)
        # 节流表（进程内）：交还/消失的会话清掉，防表无限长；重启丢状态最坏
        # 多提醒一次，可接受。
        reminded: Dict[str, float] = getattr(self, "_takeover_reminded", None) or {}
        reminded = {k: v for k, v in reminded.items() if k in active_ids}
        self._takeover_reminded = reminded
        if not overdue:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            bus = get_event_bus()
        except Exception:
            return
        for e in overdue:
            cid = str(e.get("conversation_id") or "")
            if not cid:
                continue
            last = reminded.get(cid, 0.0)
            if last and (ts - last) < interval_sec:
                continue
            reminded[cid] = ts
            bus.publish("takeover_alert", {
                "conversation_id": cid,
                "platform": str(e.get("platform") or ""),
                "by": str(e.get("by") or ""),
                "elapsed_min": int(float(e.get("elapsed_sec") or 0) // 60),
                "rate_key": f"{cid}:takeover_remind",
            })
            self.total_takeover_reminders = getattr(
                self, "total_takeover_reminders", 0) + 1

    def _check_inbox_read_stall(self, *, now: Optional[float] = None) -> None:
        """入站「半死态」升级提醒（P0，2026-08-04 Messenger 黑洞事故直接解药）。

        与 ``_check_platform_sessions`` 正交：那条盯「会话不健康（needs_login 等）」，
        这条盯**会话看着健康、内容却读不到**——账号 status=authorized（有输入框、有会话
        列表 → 登录探测绿），但侧栏有未读、外部 worker 进线程读取持续全失败。实测根因
        是 cookie 快照恢复了登录、E2EE 设备密钥没恢复，消息区永久卡 Loading；轮询一路
        「健康」，坐席看到未读却零入站，掉进去 4 天无人知。信号本身平台无关（选择器失配、
        密钥丢失同样命中），故不做 messenger 专属探针。

        升级式：stalled 持续 ``after_min`` → 首提，之后每 ``interval_min`` 一条；节流在
        ``PlatformSessionHealth`` 内（下一次心跳读到内容即 recover 清零）。只催**期望在线**
        的号（``_session_expected_online``，与会话掉线提醒同口径——运营自己登出的号不催）。
        配置 ``health_watchdog.inbox_read_stall_remind.{enabled,after_min,interval_min}``
        （默认开，20min 首提 / 4h 重提）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        sr = (((cfg.get("health_watchdog") or {}).get("inbox_read_stall_remind"))
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
        after_sec = max(60.0, float(sr.get("after_min", 20) or 20) * 60.0)
        interval_sec = max(600.0, float(sr.get("interval_min", 240) or 240) * 60.0)
        due = store.due_inbox_stalls(min_age_sec=after_sec, interval_sec=interval_sec,
                                     now=now)
        due = {k: v for k, v in due.items() if self._session_expected_online(k)}
        if not due:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            bus = get_event_bus()
        except Exception:
            return
        for key, h in due.items():
            plat, _, acct = key.partition(":")
            down_min = int(float(h.get("down_sec") or 0) // 60)
            if str(h.get("stall_kind") or "") == "e2ee_placeholder":
                _why = (f"看得到未读({int(h.get('unread') or 0)})却读不到内容："
                        f"会话列表 {int(h.get('conv_count') or 0)} 条中 "
                        f"{int(round(float(h.get('e2ee_ratio') or 0) * 100))}% 是"
                        f"端到端加密占位预览（解密不可用）。")
            else:
                _why = (f"看得到未读({int(h.get('unread') or 0)})却读不到内容："
                        f"进线程读取连续失败 {int(h.get('read_fails') or 0)}/"
                        f"{int(h.get('read_attempts') or 0)}。")
            bus.publish("platform_session_alert", {
                "platform": plat,
                "account_id": acct,
                "status": "inbox_stalled",
                "detail": _why + "多为登录态在、E2EE 消息未解密"
                          "（需在服务器上完整重登该账号）。",
                "reminder": True,
                "down_minutes": down_min,
                "unread": int(h.get("unread") or 0),
                # 独立限流键：与会话掉线告警、掉线重提都分开，多账号互不挤占。
                "rate_key": f"{key}:inbox_stall",
            })
            self.total_inbox_read_stall_reminders += 1

    def _check_messenger_not_restored(self, *, now: Optional[float] = None) -> None:
        """注册表期望在线（online）的 Messenger 账号在 sidecar 无会话 → 升级提醒。

        盲区背景（P2 2026-08-13，实测账号烂了多日无告警）：
        ``_check_platform_sessions`` 要「不健康状态**事件**」、``_check_inbox_read_stall``
        要「入站**心跳**」——一个从未 push 过任何事件的账号（sidecar 重启后恢复失败/
        会话被清）在两条巡检里都是隐形的。本检查直接对账
        「注册表 online × sidecar /accounts」差集（经 messenger_readiness_collect 的
        60s TTL 共享探针，与 /api/workspace/metrics.messenger_readiness 同源同判定）。

        零误报边界：sidecar 探活失败 / 账号清单拉取失败一律跳过本轮（那是另一类
        故障，别误归因成「账号丢了」；状态不清零防抖）；注册表 offline（运营登出/
        从未登录）不催——与 ``_session_expected_online`` 同哲学，产品面（收件箱
        chip / tools/diagnose_messenger.py）负责提示它。恢复（重新出现在 sidecar）
        对**告警过的**账号补恢复通知并清零。
        配置 ``health_watchdog.messenger_restore_remind.{enabled,after_min,interval_min}``
        （默认开，30min 首提 / 4h 重提）。
        """
        ts = time.time() if now is None else float(now)
        cfg = getattr(self._config_manager, "config", None) or {}
        sr = (((cfg.get("health_watchdog") or {}).get("messenger_restore_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not sr.get("enabled", True):
            return
        try:
            from src.integrations.messenger_readiness_collect import (
                collect_messenger_readiness,
            )
            snap = collect_messenger_readiness(cfg, now=ts)
        except Exception:
            return
        # 状态惰性初始化（测试用 __new__ 绕开 __init__ 的既有惯例友好）
        first = getattr(self, "_msgr_missing_first_seen", None)
        if first is None:
            first = {}
            self._msgr_missing_first_seen = first
        last_remind = getattr(self, "_msgr_missing_last_remind", None)
        if last_remind is None:
            last_remind = {}
            self._msgr_missing_last_remind = last_remind
        alerted = getattr(self, "_msgr_missing_alerted", None)
        if alerted is None:
            alerted = set()
            self._msgr_missing_alerted = alerted

        if not (snap.get("sources") or {}).get("sidecar_reachable"):
            return  # sidecar 自身故障：不对账、不清状态（防误归因/防抖）
        if not (snap.get("gates") or {}).get("web_effective"):
            first.clear()
            last_remind.clear()
            alerted.clear()
            return

        after_sec = max(60.0, float(sr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(sr.get("interval_min", 240) or 240) * 60.0)

        missing_now = set()
        for a in snap.get("accounts") or []:
            aid = str(a.get("account_id") or "")
            if not aid or a.get("in_sidecar") or not a.get("in_registry"):
                continue
            if str(a.get("registry_status") or "") != "online":
                continue
            missing_now.add(aid)

        def _bus():
            from src.integrations.shared.event_bus import get_event_bus
            return get_event_bus()

        # 恢复：只对告警过的账号补恢复通知（未告警的抖动恢复不发，防噪）
        for aid in list(first):
            if aid in missing_now:
                continue
            if aid in alerted:
                try:
                    _bus().publish("platform_session_alert", {
                        "platform": "messenger",
                        "account_id": aid,
                        "status": "restored",
                        "recovered": True,
                        "detail": "sidecar 会话已恢复（此前注册表期望在线但无会话）",
                        "rate_key": f"messenger:{aid}:not_restored:recover",
                    })
                except Exception:
                    pass
            first.pop(aid, None)
            last_remind.pop(aid, None)
            alerted.discard(aid)

        for aid in sorted(missing_now):
            since = first.setdefault(aid, ts)
            if (ts - since) < after_sec:
                continue
            prev = float(last_remind.get(aid) or 0.0)
            if prev and (ts - prev) < interval_sec:
                continue
            try:
                _bus().publish("platform_session_alert", {
                    "platform": "messenger",
                    "account_id": aid,
                    "status": "not_restored",
                    "reminder": True,
                    "down_minutes": int((ts - since) // 60),
                    "detail": "注册表期望在线，但 messenger-web 服务器浏览器没有该账号"
                              "会话（多为重启后恢复失败/会话被清）。到统一收件箱对它"
                              "「重新登录」（服务器完整流程）。",
                    "rate_key": f"messenger:{aid}:not_restored",
                })
            except Exception:
                continue
            last_remind[aid] = ts
            alerted.add(aid)
            self.total_messenger_restore_reminders = getattr(
                self, "total_messenger_restore_reminders", 0) + 1

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

    def _check_takeover_rearm(self, *, now: Optional[float] = None) -> None:
        """坐席接管静默超时 → 自动把会话档位接回（P0 2026-08-09）。

        为什么需要它（.198/.104 实测）：「接管即静音」把会话钉死在 manual 且
        **无任何恢复机制**——两台坐席机各有会话在 manual 卡了 27 小时，坐席
        全程不知道是自己那条手动消息关掉了 AI。sweep 逻辑（含「只碰
        source=takeover*、恢复到接管前档位」的全部语义）在
        ``takeover_rearm.sweep_takeover_rearm`` 纯核心里，这里只是接线；
        配置 ``inbox.takeover_rearm.{enabled,after_minutes}``，默认关。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        store = getattr(getattr(self._app, "state", self._app),
                        "inbox_store", None)
        if store is None:
            return
        from src.inbox.takeover_rearm import sweep_takeover_rearm
        _app_state = getattr(self._app, "state", self._app)

        def _on_restored(cid: str, row: Dict[str, Any], target: str) -> None:
            # 接力记忆 P0-3：自动接回也是一次 manual → AI 交接，同样归纳接力摘要。
            # row.updated_at＝最后一次人工出站时刻（接管态每次出站刷新），当 prev_meta 传入。
            from src.inbox.handoff_memory import record_handoff
            record_handoff(
                _app_state, store, cid,
                prev_meta={"mode": "manual", "source": row.get("source"),
                           "updated_at": row.get("updated_at")},
                new_mode=target, by="rearm", now=now)

        res = sweep_takeover_rearm(store, cfg, now=now, on_restored=_on_restored)
        if int(res.get("restored") or 0) > 0:
            logger.info("[takeover_rearm] 本轮自动接回 %s 个会话：%s",
                        res.get("restored"),
                        ", ".join(res.get("restored_cids") or [])[:400])

    def _check_autosend_gate_expiry(self, *, now: Optional[float] = None) -> None:
        """真发总闸可选自动过期（#142 件三，默认关＝零行为变更）。

        总闸「暂停真发」的设计定位是应急刹车，实际却常被顺手带到后长期留在
        关位（#140 实录：会话档全自动、总闸关着、AI 只拟稿零外发、无人知晓）。
        配置 ``inbox.l2_autosend.pause_auto_resume_hours`` > 0 后，关满 N 小时
        自动恢复 + ``autosend_gate_alert`` 群外通知。全部语义（含「只撤销有
        留痕的翻动」的安全边界）在 ``autosend_gate_state.sweep_gate_auto_resume``
        纯核心里，这里只接线 config_manager 与热接线闭包。
        """
        cm = self._config_manager
        if cm is None:
            return
        from src.inbox.autosend_gate_state import sweep_gate_auto_resume
        rewire = getattr(getattr(self._app, "state", self._app),
                         "autosend_rewire", None)
        res = sweep_gate_auto_resume(cm, rewire=rewire, now=now)
        if res.get("resumed"):
            logger.info("[autosend_gate] 总闸自动过期恢复完成：paths=%s rewire=%s",
                        res.get("paths"), res.get("rewire"))

    def _check_mutual_chat(self, *, now: Optional[float] = None) -> None:
        """AI 对聊「标记+提醒」（P0-6 2026-08-09；绝不拦截）。

        运营方针：受管账号互聊是允许的测试手段——风险只在「没人知道它在
        跑」（双边烧配额）。判定/去抖纯核心在 ``mutual_chat_monitor``，这里
        只接线：小时级扫描节流 → 满足「全自动 + 窗口内双向高频」的会话 →
        每会话按重提间隔去抖 → 聚合成**一条** ``ai_mutual_chat_alert``
        （逐会话逐条＝噪音；别名 ``ai_mutual_chat``，business 受众）。
        配置 ``health_watchdog.mutual_chat_remind``（默认开，纯观测）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        from src.inbox.mutual_chat_monitor import (
            filter_due_reminders,
            mutual_chat_cfg,
            scan_mutual_chat,
        )
        mc = mutual_chat_cfg(cfg)
        if not mc["enabled"]:
            return
        ts = float(now if now is not None else time.time())
        last = float(getattr(self, "_mutual_chat_last_scan", 0.0) or 0.0)
        if last and (ts - last) < mc["interval_min"] * 60.0:
            return
        self._mutual_chat_last_scan = ts
        store = getattr(getattr(self._app, "state", self._app),
                        "inbox_store", None)
        if store is None:
            return
        items = scan_mutual_chat(store, mc, now=ts)
        if not items:
            return
        ledger = getattr(self, "_mutual_chat_ledger", None)
        if ledger is None:
            ledger = {}
            self._mutual_chat_ledger = ledger
        due = filter_due_reminders(
            items, ledger, now=ts,
            remind_interval_hours=mc["remind_interval_hours"])
        if not due:
            return
        logger.info(
            "[mutual_chat] AI 对聊提醒：%d 个全自动会话双向高频（%s）",
            len(due),
            ", ".join(d["conversation_id"] for d in due)[:300])
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ai_mutual_chat_alert", {
                "conversations": due,
                "window_hours": mc["window_hours"],
                "rate_key": "ai_mutual_chat",
            })
        except Exception:
            logger.debug("[mutual_chat] 事件发布失败（已忽略）", exc_info=True)

    def _check_coverage_trend(self, *, now: Optional[float] = None) -> None:
        """自动化覆盖率按日快照（P2 2026-08-09；``ops.automation_coverage_trend``）。

        REPLACE 语义（同日最后一次写胜出＝当日最新态），interval_min 节流
        （默认 60 分钟）保证零热路开销；聚合与卡片同源
        （collect_automation_coverage），趋势读数绝不另算一套口径。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        from src.inbox.automation_coverage_trend import trend_cfg
        tc = trend_cfg(cfg)
        if not tc["enabled"]:
            return
        ts = float(now if now is not None else time.time())
        last = float(getattr(self, "_cov_trend_last_ts", 0.0) or 0.0)
        if last and (ts - last) < tc["interval_min"] * 60.0:
            return
        store = getattr(getattr(self._app, "state", self._app),
                        "inbox_store", None)
        if store is None:
            return
        from src.inbox.automation_coverage import collect_automation_coverage
        from src.inbox.automation_coverage_trend import (
            get_coverage_trend_store,
        )
        snap = collect_automation_coverage(store, cfg, now=ts)
        if not int((snap.get("totals") or {}).get("conversations") or 0):
            return   # 零会话不落行（空库/冷启动，落 0 会污染趋势）
        get_coverage_trend_store().record_snapshot(snap, now=ts)
        self._cov_trend_last_ts = ts

    def _check_stale_enriching(self, *, now: Optional[float] = None) -> None:
        """停泊态（enriching）草稿卡死回收（P1 2026-08-09，状态机泄漏修复）。

        enriching 是「人设产线补全中」的停泊态，正常几秒内翻 pending；補全
        协程有三层失败兜底（enrich 失败/调度失败/异常都 release）——**唯独
        进程重启**没有任何一层接得住：停泊行永久卡死，且 auto_generate_draft
        的幂等保护（「已有 pending/enriching 则跳过」）会从此**挡住该会话的
        所有后续自动拟稿**，客户消息永远无人回、界面上还什么都看不见（enriching
        不在待审队列里显示）。桌面机天天重启，这是必然踩中的静默坑。

        修法＝超时 release（翻 pending 保留规则模板占位，与产线失败兜底同一条
        降级路径，语义零新增）。默认开（这是泄漏修复不是新功能）；配置
        ``health_watchdog.stale_enriching_release.{enabled,after_min}``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        er = (((cfg.get("health_watchdog") or {})
               .get("stale_enriching_release")) or {}) if isinstance(cfg, dict) else {}
        if not er.get("enabled", True):
            return
        svc = getattr(getattr(self._app, "state", self._app), "draft_service", None)
        if svc is None or not hasattr(svc, "release_enriching_draft"):
            return
        after_min = max(2.0, float(er.get("after_min", 15) or 15))
        ts = float(now if now is not None else time.time())
        cutoff = ts - after_min * 60.0
        try:
            rows = svc.list_drafts(status="enriching", limit=200) or []
        except Exception:
            logger.debug("停泊草稿取数失败（忽略）", exc_info=True)
            return
        released = 0
        for d in rows:
            try:
                created = float(d.get("created_ts") or 0)
                did = str(d.get("draft_id") or "")
            except Exception:
                continue
            if not did or created <= 0 or created >= cutoff:
                continue
            try:
                if svc.release_enriching_draft(did):
                    released += 1
            except Exception:
                logger.debug("停泊草稿回收失败 draft_id=%s（跳过）", did,
                             exc_info=True)
        if released:
            logger.info(
                "[stale_enriching] 回收 %s 条卡死停泊草稿（>%.0f 分钟未收尾，"
                "translated to pending 待人审/自动链接手）", released, after_min)

    def _check_proxy_managed(self, *, now: Optional[float] = None) -> None:
        """托管代理生命周期巡检（一键代理 P2）：续期 / 到期回收 / 低库存。

        决策核心在 ``proxy_lifecycle.run_lifecycle_sweep``（可注入、离线可测），
        这里只做：配置闸 + 稀疏节流（``proxies.managed.lifecycle.interval_min``，
        默认 1h）+ 钱侧回调（token_ledger）+ 告警发布。

        - 续期/到期/预警类告警的 once-per-period 去重在 sweep 内（台账标记），
          这里原样 publish；
        - **低库存是持续态**（每轮都会算出同一份缺货单）→ 这里按签名去重：
          缺货名单变了立即发，没变每 4h 重提一次，补货后自动静默。
        - ``proxies.managed.enabled=false``（出厂默认）恒静默，零开销。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        try:
            from src.integrations.proxy_provider import parse_managed_cfg

            mcfg = parse_managed_cfg(cfg)
        except Exception:
            return
        if not mcfg.get("enabled"):
            return
        ts = float(now if now is not None else time.time())
        life = mcfg.get("lifecycle") or {}
        interval = max(300.0, float(life.get("interval_min", 60) or 60) * 60.0)
        if self._pxm_last_ts and ts - self._pxm_last_ts < interval:
            return
        self._pxm_last_ts = ts

        try:
            from src.integrations.proxy_lifecycle import run_lifecycle_sweep
            from src.integrations.proxy_pool import get_proxy_pool
            from src.integrations.proxy_subscription import get_proxy_subscriptions
            from src.licensing.token_ledger import (
                check_token_balance, record_fixed_spend, token_ledger_enabled,
            )

            try:
                billing_on = bool(token_ledger_enabled())
            except Exception:
                billing_on = False

            def _balance(wallet: str) -> Optional[int]:
                if not billing_on:
                    return None  # 未启用计费 → 免扣续期（与首购 billing=off 同口径）
                try:
                    return int(check_token_balance(wallet).get("balance") or 0)
                except Exception:
                    # 账本读不到按 0 余额＝本轮不续（宁可到期停，绝不盲扣）
                    logger.debug("托管代理续期读余额失败（按 0）", exc_info=True)
                    return 0

            def _charge(wallet: str, tokens: int) -> int:
                try:
                    # 续期用独立 action（与首购 proxy_managed 分开）：对账时
                    # 「新购 vs 续费」是两列账，混在一起就分不出流失/续费率。
                    return int(record_fixed_spend(
                        wallet, "proxy_managed_renew", tokens, now=ts) or 0)
                except Exception:
                    logger.debug("托管代理续期扣费失败（记 unbilled）", exc_info=True)
                    return 0

            # 上游续期直通（P4 JIT）：按**订阅行**的 provider 分流——http 单必须
            # 先续上游（IP 活在上游时钟上），stock/mock 期留下的旧单直接放行。
            # 用当前配置的 http 段建客户端：即便运营已把 provider 切回 stock，
            # 存量 http 订阅的续期仍要走上游。
            from src.integrations.proxy_provider import HttpProxyProvider

            _http_prov = HttpProxyProvider(
                mcfg.get("http") if isinstance(mcfg.get("http"), dict) else {})

            def _prolong(order_ref: str, period_days: int,
                         sub_provider: str) -> "tuple[bool, float]":
                if sub_provider != "http":
                    return True, 0.0  # 自有库存没有上游时钟
                try:
                    import asyncio as _a

                    ok, expires, reason = _a.run(_http_prov.prolong(
                        order_ref=order_ref, period_days=period_days))
                    if not ok:
                        logger.info("[proxy_managed] 上游续期被拦 order=%s: %s",
                                    order_ref, reason)
                    return ok, expires
                except Exception:
                    logger.debug("上游续期调用异常（按失败）", exc_info=True)
                    return False, 0.0

            summary = run_lifecycle_sweep(
                store=get_proxy_subscriptions(), pool=get_proxy_pool(),
                mcfg=mcfg, balance_fn=_balance, charge_fn=_charge,
                prolong_fn=_prolong, now=ts)
        except Exception:
            logger.debug("托管代理生命周期巡检失败（忽略）", exc_info=True)
            return

        alerts = list(summary.get("alerts") or [])

        # JIT（http 供给）上游余额水位：余额就是库存。低于 min_alert → 首提 +
        # 4h 重提；充值回升自动清零。探测失败（None）不告警——网络抖动 ≠ 没钱，
        # 上游真欠费会在下单环节以 upstream_http_4xx 显形。
        try:
            bal_cfg = ((mcfg.get("http") or {}).get("balance")
                       if isinstance((mcfg.get("http") or {}).get("balance"),
                                     dict) else {})
            min_bal = float(bal_cfg.get("min_alert") or 0)
            if str(mcfg.get("provider") or "") == "http" and min_bal > 0:
                import asyncio as _asyncio

                from src.integrations.proxy_provider import build_provider

                provider = build_provider(mcfg, pool=None)
                bal = (_asyncio.run(provider.vendor_balance())
                       if provider is not None else None)
                if bal is not None and bal < min_bal:
                    if (not self._pxm_bal_alerted
                            or ts - self._pxm_bal_last_remind >= 4 * 3600.0):
                        alerts.append({
                            "kind": "vendor_balance_low",
                            "balance": round(float(bal), 2),
                            "min_alert": min_bal,
                            "rate_key": "proxy_managed:vendor_balance",
                        })
                        self._pxm_bal_alerted = True
                        self._pxm_bal_last_remind = ts
                elif bal is not None:
                    self._pxm_bal_alerted = False
        except Exception:
            logger.debug("托管代理上游余额巡检失败（忽略）", exc_info=True)

        # 低库存：签名去重 + 4h 重提；补货后签名清空自动静默（无恢复通知——
        # 「货补上了」在 ops 卡可见，不值得占一条推送）。
        low = summary.get("low_stock") or {}
        if low:
            low_sig = ",".join(f"{k}:{v}" for k, v in sorted(low.items()))
            if (low_sig != self._pxm_low_sig
                    or ts - self._pxm_low_last_remind >= 4 * 3600.0):
                alerts.append({
                    "kind": "low_stock", "low": low,
                    "min_stock": int(life.get("min_stock", 2) or 0),
                    "rate_key": "proxy_managed:low_stock",
                })
                self._pxm_low_last_remind = ts
            self._pxm_low_sig = low_sig
        else:
            self._pxm_low_sig = ""

        if summary.get("renewed") or summary.get("expired"):
            logger.info(
                "[proxy_managed] 生命周期：续期 %s（未入账 %s）/ 到期回收 %s / 预警 %s",
                summary.get("renewed"), summary.get("renew_unbilled"),
                summary.get("expired"), summary.get("warned"))
        if not alerts:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus

            bus = get_event_bus()
            for payload in alerts:
                bus.publish("proxy_managed_alert", payload)
                self.total_proxy_managed_alerts += 1
        except Exception:
            logger.debug("proxy_managed 告警发布失败（忽略）", exc_info=True)

    def _check_label_leak(self, *, now: Optional[float] = None) -> None:
        """AI 出站正文带系统标签（「[我方语音消息] …」）→ 主动告警（2026-09-12 事故沉淀）。

        事故：normalize_history 把「[我方发出的语音]」写进 assistant 内容，LLM 连续十轮后
        照抄/改写/翻译进正文，文本直发客户、语音被念出，7 分钟 4 例，全靠坐席截图才发现。
        出稿口守卫（outbound_text_guard.strip_system_labels）负责拦，本巡检负责**看见**：
        按落库的出站行扫（判定单源 ``label_leak_scan``），守卫漏了 / 别的生成链没过守卫 /
        老进程没装载新代码，都在这里现形。只有高置信 ``system_label`` 才触发；
        ``bracket_prefix``（AI 出站以其它方括号开头）随附计数不单独响。

        配置 ``health_watchdog.label_leak_remind.{enabled,lookback_hours,interval_min,limit}``
        （默认开：回看 24h，4h 重提，同一批行一天最多一次，窗口清零补恢复通知）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("label_leak_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        store = self._inbox()
        if store is None or not hasattr(store, "list_outbound_bracket_rows"):
            return
        ts = float(now if now is not None else time.time())
        lookback_h = max(1.0, float(br.get("lookback_hours", 24) or 24))
        try:
            from src.inbox.label_leak_scan import (
                KIND_SYSTEM_LABEL, scan_store, summarize,
            )
            findings = scan_store(
                store, lookback_hours=lookback_h,
                limit=int(br.get("limit", 500) or 500), now=ts)
        except Exception:
            logger.debug("标签泄漏巡检取数失败（忽略）", exc_info=True)
            return
        hard = [f for f in findings if f.get("kind") == KIND_SYSTEM_LABEL]
        if not hard:
            if self._remind.resolve(self._RK_LABEL):
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("label_leak_alert", {
                        "recovered": True,
                        "lookback_hours": lookback_h,
                        "rate_key": "label_leak:recovered",
                    })
                    logger.info("HealthWatchdog 发出标签泄漏恢复通知")
                except Exception:
                    logger.debug("label_leak recovery 发布失败（忽略）", exc_info=True)
            return

        interval_sec = max(600.0, float(br.get("interval_min", 240) or 240) * 60.0)
        from src.inbox.remind_ledger import HOLD as _RL_HOLD, REMIND as _RL_REMIND, fingerprint as _rl_fp
        fp = _rl_fp(sorted(str(f.get("message_id") or "") for f in hard))
        prev_fp = str(self._remind.get(self._RK_LABEL).get("fingerprint") or "")
        since = float(hard[0].get("ts") or 0.0) or None
        verdict = self._remind.decide(
            self._RK_LABEL, now=ts, interval_sec=interval_sec, fp=fp, since=since,
            unchanged_interval_sec=self._unchanged_interval_sec(br, interval_sec, 1440))
        if verdict == _RL_HOLD:
            return
        is_reminder = verdict == _RL_REMIND
        summ = summarize(findings)
        latest = hard[-1]
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("label_leak_alert", {
                "remind_key": self._RK_LABEL,
                "count": int(summ.get("count") or 0),
                "system_label": int(summ.get("system_label") or 0),
                "bracket_prefix": int(summ.get("bracket_prefix") or 0),
                "conversations": int(summ.get("conversations") or 0),
                "top_conversations": [list(kv) for kv in (summ.get("top_conversations") or [])],
                "sample_tags": list(summ.get("sample_tags") or []),
                "lookback_hours": lookback_h,
                "latest_ts": float(latest.get("ts") or 0.0),
                "latest_text": str(latest.get("text") or "")[:80],
                "latest_media": str(latest.get("media_type") or ""),
                "reminder": is_reminder,
                "unchanged": bool(is_reminder and prev_fp and prev_fp == fp),
                "since_ts": self._remind.first_seen(self._RK_LABEL),
                "rate_key": "label_leak:remind",
            })
        except Exception:
            logger.debug("label_leak alert 发布失败（忽略）", exc_info=True)
            return
        self._remind.mark_sent(
            self._RK_LABEL, now=ts, fp=fp,
            summary=f"AI 出站带系统标签 {len(hard)} 条（{summ.get('conversations')} 个会话）")
        self.total_label_leak_alerts += 1
        logger.warning(
            "标签泄漏巡检：近 %.0fh AI 出站正文带系统标签 %d 条（会话 %d 个，样本 %s；"
            "另 %d 条以其它方括号开头）最近一条=%r",
            lookback_h, len(hard), summ.get("conversations"),
            "、".join(summ.get("sample_tags") or [])[:80], summ.get("bracket_prefix"),
            str(latest.get("text") or "")[:60])

    def _check_frontend_errors(self, *, now: Optional[float] = None) -> None:
        """前端脚本 bug beacon（ReferenceError / SyntaxError）→ 主动告警（2026-09-15 事故沉淀）。

        事故：`personas.html` 一行 `_psnArRender(p, bnd)` 落盘、函数没写，模板热更新直上
        生产 → 新建 / 编辑人设一律 ReferenceError，人设工坊整体不可用 3.5 天。
        `_boot_error_guard` 早就把这类错误 beacon 到 `frontend_error_stats`、ops 卡也在展示
        ——但没有任何东西会把人叫醒。本巡检读同一份计数，按 **(page, type, fn) 三元组**
        判「有坏符号在被反复踩」：

        - 触发：任一三元组进程内累计 ≥ ``min_count``（默认 2——一次可能是坐席开着旧标签页，
          两次起就是真的有人在踩）；
        - 指纹＝达标三元组集合：新坏符号出现 → 指纹变 → 按 ``interval_min`` 重提；同一批
          → 24h 一次（``unchanged_interval_min``）；
        - 恢复：最近一次脚本 bug beacon 距今 ≥ ``quiet_hours``（默认 12h）且此前告过警
          → 发恢复通知（计数是进程级不会回落，「安静」就是修好了的唯一证据）。
        配置 ``health_watchdog.frontend_error_remind.{enabled,min_count,interval_min,
        unchanged_interval_min,quiet_hours}``（默认开）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("frontend_error_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        try:
            from src.web.frontend_error_stats import get_frontend_error_stats
            snap = get_frontend_error_stats().script_bugs_snapshot()
        except Exception:
            logger.debug("前端脚本错误巡检取数失败（忽略）", exc_info=True)
            return
        ts = float(now if now is not None else time.time())
        min_count = max(1, int(br.get("min_count", 2) or 2))
        quiet_sec = max(3600.0, float(br.get("quiet_hours", 12) or 12) * 3600.0)
        items = {k: int(v) for k, v in (snap.get("items") or {}).items() if int(v) >= min_count}
        last_ts = float(snap.get("last_ts") or 0.0)
        quiet = (not items) or (last_ts and ts - last_ts >= quiet_sec)
        if quiet:
            if self._remind.resolve(self._RK_FE):
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("frontend_error_alert", {
                        "recovered": True,
                        "quiet_hours": quiet_sec / 3600.0,
                        "rate_key": "frontend_error:recovered",
                    })
                    logger.info("HealthWatchdog 发出前端脚本错误恢复通知")
                except Exception:
                    logger.debug("frontend_error recovery 发布失败（忽略）", exc_info=True)
            return

        interval_sec = max(600.0, float(br.get("interval_min", 30) or 30) * 60.0)
        from src.inbox.remind_ledger import HOLD as _RL_HOLD, REMIND as _RL_REMIND, fingerprint as _rl_fp
        fp = _rl_fp(sorted(items.keys()))
        prev_fp = str(self._remind.get(self._RK_FE).get("fingerprint") or "")
        verdict = self._remind.decide(
            self._RK_FE, now=ts, interval_sec=interval_sec, fp=fp,
            unchanged_interval_sec=self._unchanged_interval_sec(br, interval_sec, 1440))
        if verdict == _RL_HOLD:
            return
        is_reminder = verdict == _RL_REMIND
        top = sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        pages = sorted({k.split(" ", 1)[0] for k in items})
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("frontend_error_alert", {
                "remind_key": self._RK_FE,
                "symbols": int(len(items)),
                "hits": int(sum(items.values())),
                "pages": pages,
                "top": [[k, v] for k, v in top],
                "last_ts": last_ts,
                "min_count": min_count,
                "reminder": is_reminder,
                "unchanged": bool(is_reminder and prev_fp and prev_fp == fp),
                "since_ts": self._remind.first_seen(self._RK_FE),
                "rate_key": "frontend_error:remind",
            })
        except Exception:
            logger.debug("frontend_error alert 发布失败（忽略）", exc_info=True)
            return
        self._remind.mark_sent(
            self._RK_FE, now=ts, fp=fp,
            summary=f"前端脚本错误 {len(items)} 个符号 / {sum(items.values())} 次（{'、'.join(pages)[:60]}）")
        self.total_frontend_error_alerts += 1
        logger.warning(
            "前端脚本错误巡检：%d 个坏符号被踩 %d 次，Top=%s",
            len(items), sum(items.values()),
            "; ".join(f"{k}×{v}" for k, v in top[:3]))

    _DIAG_OUTBOX_INTERVAL_SEC = 3600.0

    def _check_diag_outbox(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """报障 outbox 补传（每小时一次，有暂存件才动）——所有版本都跑的兜底。

        `hosted_gateway.refresh_once` 里那条补传只对托管/桌面版生效（刷新守护由
        `_wants_hosted` 闸住），源码态实例的暂存件只能等「下一次成功上传顺手补传」。
        这里读同一份 outbox、调同一个 `flush_diag_outbox`（单一实现），只补「谁来触发」。
        目录为空零开销；任何异常吞掉。返回 flush 结果（测试/观测用）。
        """
        ts = float(now if now is not None else time.time())
        last = float(getattr(self, "_diag_outbox_last_ts", 0.0) or 0.0)
        if ts - last < self._DIAG_OUTBOX_INTERVAL_SEC:
            return {}
        self._diag_outbox_last_ts = ts
        try:
            from src.utils.diag_upload import flush_diag_outbox, list_staged, resolve_diag_dirs
            _, logs_dir = resolve_diag_dirs(self._config_manager)
            if not list_staged(logs_dir):
                return {"sent": 0, "dropped": 0, "remaining": 0}
            out = flush_diag_outbox(self._config_manager)
        except Exception:
            logger.debug("报障 outbox 补传失败（忽略）", exc_info=True)
            return {}
        if out.get("sent") or out.get("dropped"):
            logger.info("[diag] 看门狗 outbox 补传：sent=%s dropped=%s remaining=%s",
                        out.get("sent"), out.get("dropped"), out.get("remaining"))
        return out

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
        interval_min,work_resume_grace_hours}``（默认开：≥3 条超 24h 触发，每 4h
        重提，清空自动补恢复通知；班表开启时休息期扣留稿不计入、复班给 2h 宽限，
        见 _filter_backlog_by_schedule）。
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

        # 工作时间班表感知（2026-08-04）：班表开启时，休息中账号的积压是
        # **刻意扣留**（AutosendWorker 复班会接续/补觉重拟），此时轰「无人
        # 处理」=狼来了；复班后再给宽限（work_resume_grace_hours 默认 2h）
        # 让补觉链/坐席消化隔夜积压——宽限只豁免**休息期攒下的**稿
        # （created < 本班次开始），复班后新拖延照常告警。
        aged, off_hours_held = self._filter_backlog_by_schedule(
            aged, cfg, br, now_ts=ts)

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
            # 恢复通知的诚实前提：队列真清空。仅因休息期扣留而「暂时看不见」
            # 不算处理完（off_hours_held>0 时不发恢复，复班后见真章）。
            if not stale and not off_hours_held and self._remind.resolve(self._RK_DRAFT):
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("draft_backlog_alert", {
                        "recovered": True,
                        "rate_key": "draft_backlog:recovered",
                    })
                    logger.info("HealthWatchdog 发出草稿积压恢复通知")
                except Exception:
                    logger.debug("draft_backlog recovery 发布失败（忽略）", exc_info=True)
            return

        interval_sec = max(600.0, float(br.get("interval_min", 240) or 240) * 60.0)
        # 内容指纹 = 还在等的那批稿的 id 集合：同样那几条 → 一天最多提一次；
        # 有新稿加入 / 老稿被处置 → 按常规间隔重提（账本跨重启，见 remind_ledger）。
        from src.inbox.remind_ledger import HOLD as _RL_HOLD, REMIND as _RL_REMIND, fingerprint as _rl_fp
        fp = _rl_fp(sorted(str(d.get("draft_id") or d.get("source_id") or d.get("created_ts") or "")
                           for d in stale))
        prev_fp = str(self._remind.get(self._RK_DRAFT).get("fingerprint") or "")
        # 条件真实成立时刻 = 第 min_count 老的那条稿满 min_age_h 的那一刻（账本「已开 X」口径，
        # 不再从账本首见起算——账本 09-10 才上线，积压是 8 月的）
        _created = sorted((float(d.get("created_ts") or 0.0) for d in stale
                           if float(d.get("created_ts") or 0.0) > 0), reverse=False)
        since = (_created[min_count - 1] + min_age_h * 3600.0) if len(_created) >= min_count else None
        verdict = self._remind.decide(
            self._RK_DRAFT, now=ts, interval_sec=interval_sec, fp=fp, since=since,
            unchanged_interval_sec=self._unchanged_interval_sec(br, interval_sec, 1440))
        if verdict == _RL_HOLD:
            return
        is_reminder = verdict == _RL_REMIND

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
                "remind_key": self._RK_DRAFT,
                "stale_count": len(stale),
                "min_age_hours": min_age_h,
                "oldest_hours": round(oldest_h, 1),
                "by_level": by_level,
                "sla_uncovered": uncovered,
                # 账目残留（内容已人工回过、草稿行没人处置）——与「客户在等」分开报
                "already_replied": already_replied,
                # 班表扣留/复班宽限中的条数（刻意延后，不算无人处理）
                "off_hours_held": off_hours_held,
                "reminder": is_reminder,
                # 与上一次外发相比内容没变（同样那批稿）——formatter 可据此标「情况未变」
                "unchanged": bool(is_reminder and prev_fp and prev_fp == fp),
                "since_ts": self._remind.first_seen(self._RK_DRAFT),
                "rate_key": "draft_backlog:remind",
            })
        except Exception:
            logger.debug("draft_backlog alert 发布失败（忽略）", exc_info=True)
            return
        self._remind.mark_sent(
            self._RK_DRAFT, now=ts, fp=fp,
            summary=f"待审草稿 {len(stale)} 条无人处理（最久 {self._fmt_hours(oldest_h)}）")
        self.total_draft_backlog_alerts += 1
        logger.warning(
            "待审草稿积压：%d 条超过 %.0fh 客户仍在等（最老 %.0fh，分级 %s；"
            "其中 %d 条不在 SLA 逐条告警覆盖内；另有 %d 条已人工回过仅账目残留；"
            "%d 条处于班表扣留/复班宽限）",
            len(stale), min_age_h, oldest_h, by_level, uncovered,
            already_replied, off_hours_held)

    def _check_accounts_truth(self, *, now: Optional[float] = None) -> None:
        """会话库有、注册表没有的真幽灵账号 → 主动轰人（P4b 2026-08-17）。

        原方案「已登出账号未读积压」被否决：``_accounts_summary_list`` 对
        logged_out/removed **强制 unread=0**（不可回复的未读不亮灯），那条告警
        永远不会响。桌面镜像（tg-desktop）在注册表、有自己的 tag，也不该当幽灵。

        本检查只盯**真泄漏**：``directory_ghost_keys``（目录 − 注册表 − web 工作台）。
        ≥ min_count（默认 1）即报——一条绕过注册表写会话就是接入链缺口。
        配置 ``health_watchdog.accounts_truth_remind.{enabled,min_count,interval_min}``。
        订阅别名 ``accounts_truth``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("accounts_truth_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        store = self._inbox()
        if store is None or not hasattr(store, "account_directory"):
            return
        min_count = max(1, int(br.get("min_count", 1) or 1))
        ts = float(now if now is not None else time.time())
        try:
            directory = store.account_directory() or {}
            from src.integrations.account_registry import get_account_registry
            rows = get_account_registry().list(include_removed=True) or []
            reg_keys = {(str(r.get("platform") or ""), str(r.get("account_id") or ""))
                        for r in rows}
            from src.web.routes.unified_inbox_aggregate import directory_ghost_keys
            ghosts = directory_ghost_keys(directory, reg_keys)
        except Exception:
            logger.debug("账号真相巡检取数失败（忽略）", exc_info=True)
            return

        if len(ghosts) < min_count:
            if self._at_alerted and not ghosts:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("accounts_truth_alert", {
                        "recovered": True,
                        "rate_key": "accounts_truth:recovered",
                    })
                    logger.info("HealthWatchdog 发出账号真相幽灵恢复通知")
                except Exception:
                    logger.debug("accounts_truth recovery 发布失败（忽略）", exc_info=True)
                self._at_alerted = False
                self._at_last_remind = 0.0
            return

        interval_sec = max(600.0, float(br.get("interval_min", 240) or 240) * 60.0)
        if self._at_alerted and ts - self._at_last_remind < interval_sec:
            return

        samples = [f"{p}:{a}" for p, a in ghosts[:8]]
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("accounts_truth_alert", {
                "ghost_count": len(ghosts),
                "samples": samples,
                "reminder": bool(self._at_alerted),
                "rate_key": "accounts_truth:remind",
            })
        except Exception:
            logger.debug("accounts_truth alert 发布失败（忽略）", exc_info=True)
            return
        self._at_alerted = True
        self._at_last_remind = ts
        self.total_accounts_truth_alerts += 1
        logger.warning(
            "账号真相：%d 个会话库账号不在注册表（样本 %s）——有接入链在绕过登记写会话",
            len(ghosts), samples,
        )

    def _check_reply_budget(self, *, now: Optional[float] = None) -> None:
        """回复额度触顶聚合巡检（P1 2026-08-12，webhook 别名 ``reply_budget``）。

        逐会话的 ``bot_peer_alert`` 在**首次拦截**时已各响一次（每会话每日至多
        一次）；本检查补「面」的信号：**多个**会话同日触顶＝系统性状况——预算
        配小了 / 撞上 bot 波次 / 某账号被围攻，属运维调参/排班问题，与逐会话
        事件互补而非重复。payload 随带 ``near_count``（≥80% 接近额度的会话数，
        与设置页「接近」chip / 收件箱预警横幅同源 ``budget_flags.near``）作提前
        量，但 near 本身**不触发**告警（预警不该比事故更响）。

        判定与设置页「今日额度状态」同一数据源（``list_reply_budget_today`` ×
        ``budget_flags``）——看板显示什么、告警就数什么，永不分叉。
        配置 ``health_watchdog.reply_budget_remind.{enabled,min_count,
        interval_min}``（默认开：≥3 会话触顶才响、4h 重提、触顶清零补恢复
        通知）。守卫整体关闭/额度=0 → **静默复位**不发恢复——「把守卫关了」
        不是「处理完了」，谎报恢复比不报更糟。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("reply_budget_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        store = self._inbox()
        if store is None or not hasattr(store, "list_reply_budget_today"):
            return
        from src.inbox.peer_bot_guard import budget_flags, parse_cfg, today_key
        guard = parse_cfg(cfg)
        ts = float(now if now is not None else time.time())
        if not (guard.get("enabled")
                and int(guard.get("daily_reply_budget") or 0) > 0):
            self._rb_alerted = False
            self._rb_last_remind = 0.0
            return
        try:
            rows = store.list_reply_budget_today(today_key(ts), limit=200) or []
        except Exception:
            logger.debug("回复额度巡检取数失败（忽略）", exc_info=True)
            return
        exhausted: List[Dict[str, Any]] = []
        hard_count = 0
        near_count = 0
        for r in rows:
            flags = budget_flags(r.get("used"), r.get("relieved"), guard)
            if flags["exhausted"]:
                exhausted.append({
                    "title": str(r.get("display_name") or r.get("chat_key")
                                 or r.get("conversation_id") or ""),
                    "used": flags["used"],
                })
                if flags["hard_stopped"]:
                    hard_count += 1
            elif flags["near"]:
                near_count += 1

        min_count = max(1, int(br.get("min_count", 3) or 3))
        if len(exhausted) < min_count:
            # 恢复通知的诚实前提：触顶真清零（豁免/跨日）。仅降到阈值以下不发。
            if self._rb_alerted and not exhausted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("reply_budget_alert", {
                        "recovered": True,
                        "rate_key": "reply_budget:recovered",
                    })
                    logger.info("HealthWatchdog 发出回复额度恢复通知")
                except Exception:
                    logger.debug("reply_budget recovery 发布失败（忽略）",
                                 exc_info=True)
                self._rb_alerted = False
                self._rb_last_remind = 0.0
            return

        interval_sec = max(600.0, float(br.get("interval_min", 240) or 240) * 60.0)
        if self._rb_alerted and ts - self._rb_last_remind < interval_sec:
            return

        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("reply_budget_alert", {
                "exhausted_count": len(exhausted),
                "hard_count": hard_count,
                "near_count": near_count,
                "budget_limit": int(guard.get("daily_reply_budget") or 0),
                # rows 本就按 used 降序 → 前 3 个就是烧得最凶的
                "samples": exhausted[:3],
                "reminder": bool(self._rb_alerted),
                "rate_key": "reply_budget:remind",
            })
        except Exception:
            logger.debug("reply_budget alert 发布失败（忽略）", exc_info=True)
            return
        self._rb_alerted = True
        self._rb_last_remind = ts
        self.total_reply_budget_alerts += 1
        logger.warning(
            "回复额度触顶聚合：%d 个会话今日烧穿预算 %d 轮（硬停 %d、"
            "接近额度另有 %d）",
            len(exhausted), int(guard.get("daily_reply_budget") or 0),
            hard_count, near_count)

    def _check_agent_quota(self, *, now: Optional[float] = None,
                           user_store: Any = None) -> None:
        """坐席月度字符额度水位聚合巡检（2026-08-16，webhook 别名 ``agent_quota``）。

        为什么需要它：路由层的 enforce 硬闸本批**默认关**（软提醒先行）——软提醒
        期运营对「谁快用满 / 谁已超额」没有任何主动信号，只能等坐席自己去「我的
        用量」看（和 draft_backlog「看板要有人开才有用」同病）。本检查把水位升级
        为主动外发：达到用户行 ``quota_alert_pct``（缺省 80）进 **warn 桶**、超额
        （>100%）进 **over 桶**，任一桶非空发一条聚合事件（逐用户告警＝噪音；
        「N 人接近/超额」才是运维要的排额度信号）。

        判定口径与 users 页 / 我的用量完全同源（``agent_quota_status`` 单一纯函数
        + ``month_totals`` 同一账本），看板显示什么、告警就数什么。全部回落后补
        一次恢复通知（照抄 draft_backlog 家族的 recovered 语义；月初账本自然清零
        即自动恢复）。配置 ``health_watchdog.agent_quota_remind.{enabled,
        interval_min}``（默认开、6h 重提）——父开关默认开可接受：整个检查先被
        ``agent_chars_enabled`` 闸住，未开计量的部署恒静默零噪音。

        ``user_store`` 形参供单测注入；生产走 ``app.state.user_store``（admin.py
        已暴露，与 draft_service 同取法），拿不到 → 静默（旧装配/裸测试 app）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("agent_quota_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        from src.utils.agent_char_usage import (
            agent_chars_enabled,
            agent_quota_status,
            ensure_store_for_read,
        )
        if not agent_chars_enabled(cfg if isinstance(cfg, dict) else None):
            return  # 未开坐席计量 → 恒静默（本检查存在的前提是账本在记）
        us = user_store
        if us is None:
            us = getattr(getattr(self._app, "state", self._app), "user_store", None)
        if us is None or not hasattr(us, "list_users"):
            return  # user_store 未暴露（旧装配/测试 app）→ 静默不猜
        store = ensure_store_for_read(cfg)
        if store is None:
            return
        ts = float(now if now is not None else time.time())
        try:
            totals = store.month_totals() or {}
            users = us.list_users() or []
        except Exception:
            logger.debug("坐席额度巡检取数失败（忽略）", exc_info=True)
            return

        warn: List[Dict[str, Any]] = []
        over: List[Dict[str, Any]] = []
        for u in users:
            try:
                quota = int(u.get("monthly_char_quota") or 0)
            except (TypeError, ValueError):
                quota = 0
            if quota <= 0:
                continue  # 不限额的用户无水位语义（master 通常在此列）
            uname = str(u.get("username") or "")
            if not uname:
                continue
            used = int((totals.get(uname) or {}).get("total") or 0)
            st = agent_quota_status(used, quota)
            try:
                alert_pct = int(u.get("quota_alert_pct") or 80)
            except (TypeError, ValueError):
                alert_pct = 80
            alert_pct = min(100, max(1, alert_pct))
            entry = {"username": uname, "used": st["used"],
                     "quota": st["quota"], "pct": st["pct"]}
            if st["level"] == "over":
                over.append(entry)
            elif st["pct"] >= alert_pct:
                warn.append(entry)

        if not warn and not over:
            # 恢复通知：全部回落（调额/月初账本自然清零）才发，且只发给告过警的
            if self._aq_alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("agent_quota_alert", {
                        "recovered": True,
                        "rate_key": "agent_quota:recovered",
                    })
                    logger.info("HealthWatchdog 发出坐席字符额度恢复通知")
                except Exception:
                    logger.debug("agent_quota recovery 发布失败（忽略）",
                                 exc_info=True)
                self._aq_alerted = False
                self._aq_last_remind = 0.0
            return

        interval_sec = max(600.0, float(br.get("interval_min", 360) or 360) * 60.0)
        if self._aq_alerted and ts - self._aq_last_remind < interval_sec:
            return

        # 桶内按用量占比降序（formatter 取前几个当 Top 明细），上限 8 防 payload 膨胀
        warn.sort(key=lambda e: e["pct"], reverse=True)
        over.sort(key=lambda e: e["pct"], reverse=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            from src.utils.agent_char_usage import _month_str
            get_event_bus().publish("agent_quota_alert", {
                "month": _month_str(ts),
                "warn_count": len(warn),
                "over_count": len(over),
                "warn": warn[:8],
                "over": over[:8],
                "reminder": bool(self._aq_alerted),
                "rate_key": "agent_quota:remind",
            })
        except Exception:
            logger.debug("agent_quota alert 发布失败（忽略）", exc_info=True)
            return
        self._aq_alerted = True
        self._aq_last_remind = ts
        self.total_agent_quota_alerts += 1
        # 日志=本机唯一保证在的持久告警通道（webhook 可能 0 通道），与
        # draft_backlog 同款落一行供实弹验证与事后追溯。
        logger.warning(
            "坐席字符额度水位：%d 人已超额、%d 人达到提醒线（enforce 硬闸当前%s，"
            "超额明细 %s）",
            len(over), len(warn),
            "开启" if bool((((cfg.get("usage") or {}).get("agent_chars") or {})
                            if isinstance(cfg, dict) else {}).get("enforce")) else "关闭（软提醒）",
            [(e["username"], e["pct"]) for e in over[:3]])

    def _startup_hygiene(self) -> None:
        """进程启动后跑一次的状态卫生（#207 / #170）：把「界面上还亮着、现场早已过去」
        的残标清掉，并落 INFO 让值守知道清了什么。

        - #207：带「需人工」且客户消息已被消化（末条为出站且晚于打标）→ 自动摘
          （只摘 ``dup_guard_blocked`` 等「AI 没能回」类系统标，crisis/人工标不动）；
          Loki 会话（dup 拦截 02:57 打标，AI 11:24 回上，红标挂到次日）装载后即消。
        - #170：已删除（有墓碑）的会话仍带 unread 计数＝孤儿未读——徽标口径早已
          剔除它们，但旧口径/第三方读数仍会算进去，归零一次并落日志。
        store 未就绪 → 保持未完成，下 tick 再试。
        """
        store = self._inbox()
        if store is None:
            return
        self._hygiene_done = True
        try:
            from src.integrations.protocol_autoreply import sweep_stale_needs_human
            res = sweep_stale_needs_human(store)
            if res.get("cleared"):
                logger.info("启动卫生扫描：「需人工」已消化自动摘标 %d 个（保留 %d）",
                            res.get("cleared", 0), res.get("kept", 0))
        except Exception:
            logger.debug("启动卫生扫描（需人工）失败（已忽略）", exc_info=True)
        try:
            from src.inbox.unread_aggregate import purge_orphan_unread
            n = purge_orphan_unread(store)
            if n:
                logger.info("启动卫生扫描：已删会话残留的孤儿未读归零 %d 个会话", n)
        except Exception:
            logger.debug("启动卫生扫描（孤儿未读）失败（已忽略）", exc_info=True)
        # #222 修法 3：存量被埋一次性清理——「人工归档 + 无入站 > 72h」的被埋未读
        # 标已读（坐席明示收尾、客户此后再没开口，横幅挂着只剩噪音；skuio 机
        # 7→8→9 个 / 100h 就是这批）。<72h 的仍留给横幅；自动归档的不动。
        # 配置 health_watchdog.buried_conv_remind.sweep_hours（默认 72；0=关）。
        try:
            hours = self._buried_sweep_hours()
            if hours > 0:
                from src.inbox.unread_aggregate import sweep_buried_unread
                n = sweep_buried_unread(store, min_idle_sec=hours * 3600.0,
                                        manual_only=True, reason="startup_sweep")
                if n:
                    logger.info("启动卫生扫描：[buried] startup_sweep cleared %d"
                                "（人工归档且无入站 > %.0fh 的被埋未读已标已读）", n, hours)
        except Exception:
            logger.debug("启动卫生扫描（被埋存量）失败（已忽略）", exc_info=True)

    def _buried_sweep_hours(self) -> float:
        """启动清扫的「无入站」门槛小时数（``buried_conv_remind.sweep_hours``，默认 72）。"""
        try:
            cfg = getattr(self._config_manager, "config", None) or {}
            br = (((cfg.get("health_watchdog") or {}).get("buried_conv_remind"))
                  or {}) if isinstance(cfg, dict) else {}
            return max(0.0, float(br.get("sweep_hours", 72) or 0))
        except Exception:
            return 72.0

    def _ui_surface_enabled(self) -> bool:
        """surface_in_ui 生效值：构造参数为底，配置 ``health_watchdog.surface_in_ui`` 可覆写。"""
        try:
            cfg = getattr(self._config_manager, "config", None) or {}
            hw = (cfg.get("health_watchdog") or {}) if isinstance(cfg, dict) else {}
            if isinstance(hw, dict) and "surface_in_ui" in hw:
                return bool(hw.get("surface_in_ui"))
        except Exception:
            pass
        # 测试/旧路径经 __new__ 绕过 __init__ 构造 → 缺属性按默认开
        return bool(getattr(self, "_surface_in_ui", True))

    def _surface_ui_status(self, sid: str, text: str, *, clear: bool = False) -> bool:
        """把巡检结论写进工作台通知中心（#170「上屏」路径）。

        与 ``POST /api/workspace/notifications/sys-status`` 同一队列同一合并语义
        （``app.state.notif_queue``，type=sys_status，按 ``data.id`` 只留最新一条；
        上限 200）。``clear=True`` ＝状态归零，撤掉该 id 的条目。页面刷新 / SSE 重连
        经 GET /api/workspace/notifications 回放 → 铃铛可见，不依赖 webhook。
        任何异常吞掉（这是可见性增益路径，不能反噬巡检）。
        """
        if not sid or not self._ui_surface_enabled():
            return False
        surfaced = getattr(self, "_ui_surfaced", None)
        if surfaced is None:
            surfaced = self._ui_surfaced = {}
        try:
            state = getattr(self._app, "state", self._app)
            nq = getattr(state, "notif_queue", None)
            if nq is None:
                if clear:
                    return False
                nq = []
                state.notif_queue = nq
            nq[:] = [
                n for n in nq
                if not ((n or {}).get("type") == "sys_status"
                        and str(((n or {}).get("data") or {}).get("id") or "") == sid)
            ]
            if clear:
                surfaced.pop(sid, None)
                return True
            nq.append({
                "type": "sys_status",
                "data": {"id": sid, "text": str(text or "")[:300],
                         "source": "health_watchdog"},
                "_notif_ts": int(time.time() * 1000),
            })
            if len(nq) > 200:
                del nq[:-200]
            surfaced[sid] = True
            return True
        except Exception:
            logger.debug("巡检结论写通知中心失败（已忽略）", exc_info=True)
            return False

    def _check_buried_conversations(self, *, now: Optional[float] = None) -> None:
        """归档着、却有未读入站的会话 → 主动轰人（P0-198）。

        事故：一条 33 条消息的**活跃**会话在坐席聊天途中从工作台彻底消失。归档在实现上
        是永久的（所有默认视图过滤 ``archived=1``），入站链路从不复位该标记，于是客户
        之后无论说多少句话都不回来、也不产生任何提示——**没有任何信号会响**。

        入站自动复活（``InboxStore._unarchive_on_inbound``）已经堵住新发生的这类事故，
        但它**够不着存量**：``archived_at`` 是那次才加的列，存量已归档行只能回填「升级
        时刻」（真实归档时刻不可追溯），客户是在升级之前开口的 → ts 早于回填值 → 判据
        天然不成立。本检查用一条**与时间戳无关**的信号兜住这批历史损失：未读只可能由
        入站消息产生，而「没人读过」本身就是「没人看得见」的直接证据。

        它同时是复活链路的**反向哨兵**：复活正常工作时这个清单应恒为空；一旦复活断了
        （接线丢失/判据回归），这里就会响。故即便存量清完也不该拆。

        聚合而非逐条（与 ``_check_draft_backlog`` 同哲学）：这是「有人被埋着」的排班/
        注意力问题，逐条＝噪音。配置 ``health_watchdog.buried_conv_remind.
        {enabled,min_count,min_unread,interval_min}``（默认开：≥1 条即触发——被埋一条
        就是一个客户在等，不像草稿积压那样存在「日常队列深度」的正常态）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("buried_conv_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        store = self._inbox()
        if store is None or not hasattr(store, "list_buried_archived"):
            return
        min_count = max(1, int(br.get("min_count", 1) or 1))
        min_unread = max(1, int(br.get("min_unread", 1) or 1))
        ts = float(now if now is not None else time.time())
        # #222 口径统一：优先走 unread_aggregate.buried_conversations（私聊 + 有效
        # 未读 + 可见消息，与工作台横幅 / 主徽标同一份 WHERE）——旧口径 raw
        # unread>=1 把 skip_groups 只进不出的群 / 频道也算成「客户在等」，被埋 9 个
        # 与横幅 N 条各说各话。None（旧 store / 查询没跑成）才回落 list_buried_archived。
        rows = None
        try:
            from src.inbox.unread_aggregate import buried_conversations
            rows = buried_conversations(store, min_unread=min_unread, limit=100)
        except Exception:
            rows = None
        if rows is None:
            try:
                rows = store.list_buried_archived(min_unread=min_unread, limit=100) or []
            except Exception:
                logger.debug("被埋会话巡检取数失败（忽略）", exc_info=True)
                return

        if len(rows) < min_count:
            # #170：归零即撤通知中心条目（不论 webhook 那边有没有报过）
            if not rows and (getattr(self, "_ui_surfaced", None) or {}).get("buried_conv"):
                self._surface_ui_status("buried_conv", "", clear=True)
            if self._bc_alerted and not rows:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("buried_conv_alert", {
                        "recovered": True,
                        "rate_key": "buried_conv:recovered",
                    })
                    logger.info("HealthWatchdog 发出被埋会话恢复通知")
                except Exception:
                    logger.debug("buried_conv recovery 发布失败（忽略）", exc_info=True)
                self._bc_alerted = False
                self._bc_last_remind = 0.0
            return

        total_unread = 0
        oldest_h = 0.0
        auto_n = 0
        for r in rows:
            try:
                total_unread += int(r.get("unread") or 0)
            except (TypeError, ValueError):
                pass
            try:
                last_ts = float(r.get("last_ts") or 0)
                if last_ts > 0:
                    oldest_h = max(oldest_h, (ts - last_ts) / 3600.0)
            except (TypeError, ValueError):
                pass
            try:
                if float(r.get("auto_archived_at") or 0) > 0:
                    auto_n += 1
            except (TypeError, ValueError):
                pass
        # #170 上屏：每 tick 刷新通知中心条目（按 id 合并＝常驻一条、持续时长跟着走），
        # 与下方 webhook 的 4h 重提节流**无关**——用户在工作台里随时能看到现状；
        # 处置入口指向收件箱筛选条「归档中还有 N 条未读 → 取消归档这 N 条」。
        self._surface_ui_status(
            "buried_conv",
            f"有 {total_unread} 条未读在 {len(rows)} 个已归档会话里（最久 {oldest_h:.0f} 小时）"
            "——收件箱筛选条「归档中还有 N 条未读」可一键取消归档或逐条查看")

        interval_sec = max(600.0, float(br.get("interval_min", 240) or 240) * 60.0)
        if self._bc_alerted and ts - self._bc_last_remind < interval_sec:
            return

        # 人工/自动分开报：处置完全不同——人工归档要找那个人问清楚是不是误操作，
        # 自动归档说明策略把活跃会话判死了（该调 idle_hours 或干脆关掉）。
        samples = [str(r.get("conversation_id") or "") for r in rows[:5]]
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("buried_conv_alert", {
                "buried_count": len(rows),
                "total_unread": total_unread,
                "oldest_hours": round(oldest_h, 1),
                "auto_archived": auto_n,
                "manual_archived": len(rows) - auto_n,
                "samples": samples,
                "reminder": bool(self._bc_alerted),
                "rate_key": "buried_conv:remind",
            })
        except Exception:
            logger.debug("buried_conv alert 发布失败（忽略）", exc_info=True)
            return
        self._bc_alerted = True
        self._bc_last_remind = ts
        self.total_buried_conv_alerts += 1
        logger.warning(
            "被埋会话：%d 个会话已归档却有未读入站（共 %d 条未读，最近活动距今 %.0fh；"
            "人工归档 %d / 自动归档 %d）——客户在等，但工作台默认视图看不见它们",
            len(rows), total_unread, oldest_h, len(rows) - auto_n, auto_n,
        )

    def _check_phantom_unread(self, *, now: Optional[float] = None) -> None:
        """账号栏未读徽标与清单口径的**差额** → 主动轰人（#159，2026-09-03）。

        事故形态：steven 号三处显示 7 条待处理、清单一条都没有（证据包
        G4BYWH）。1.0.72 起徽标改按清单口径算（``inbox.unread_aggregate``），
        差额即「被新闸门剔掉的幽灵未读」——墓碑会话、一条可见消息都不剩的
        会话、协议号纯占位残值。

        为什么要主动报：#145件④ 从 0817 挂到 0903 才等到一份带现场的诊断包，
        整整两周里值守只能等客户截图。差额是台机器自己就能算出来的数，不该靠
        用户发现。**差额 > 0 不等于故障**（旧残值本来就会存在一段时间），它是
        「这台机器上有多少未读是幽灵」的量化——超阈值才吵人。

        配置 ``health_watchdog.phantom_unread_remind.{enabled,min_phantom,
        interval_min}``（默认开，阈值 5）。取数失败/旧 store → 静默跳过。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        pr = (((cfg.get("health_watchdog") or {}).get("phantom_unread_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not pr.get("enabled", True):
            return
        store = self._inbox()
        if store is None:
            return
        ts = float(now if now is not None else time.time())
        try:
            from src.inbox.unread_aggregate import phantom_unread_report
            rep = phantom_unread_report(store) or {}
        except Exception:
            logger.debug("幽灵未读巡检取数失败（忽略）", exc_info=True)
            return
        phantom = int(rep.get("phantom") or 0)
        min_phantom = max(1, int(pr.get("min_phantom", 5) or 5))

        if phantom < min_phantom:
            # 清零＝存量残值被清干净了（用户清了未读/会话被真正删掉）→ 报喜一次
            if self._pu_alerted and phantom <= 0:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("phantom_unread_alert", {
                        "recovered": True,
                        "rate_key": "phantom_unread:recovered",
                    })
                    logger.info("HealthWatchdog 发出幽灵未读清零通知")
                except Exception:
                    logger.debug("phantom_unread recovery 发布失败（忽略）",
                                 exc_info=True)
                self._pu_alerted = False
                self._pu_last_remind = 0.0
            return

        interval_sec = max(600.0, float(pr.get("interval_min", 360) or 360) * 60.0)
        if self._pu_alerted and ts - self._pu_last_remind < interval_sec:
            return

        by_acct = rep.get("by_account") or {}
        worst = sorted(by_acct.items(), key=lambda kv: -int(kv[1] or 0))[:5]
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("phantom_unread_alert", {
                "phantom": phantom,
                "badge_total": int(rep.get("badge") or 0),
                "store_total": int(rep.get("store") or 0),
                "worst_accounts": [{"account": k, "phantom": int(v or 0)}
                                   for k, v in worst],
                "reminder": bool(self._pu_alerted),
                "rate_key": "phantom_unread:remind",
            })
        except Exception:
            logger.debug("phantom_unread alert 发布失败（忽略）", exc_info=True)
            return
        self._pu_alerted = True
        self._pu_last_remind = ts
        self.total_phantom_unread_alerts += 1
        logger.warning(
            "幽灵未读：全库有 %d 条未读算在旧口径里、但清单上点不开（徽标口径 %d / "
            "旧口径 %d）——最多的账号：%s。这些多来自已删会话残值、消息被全部撤回的"
            "会话、协议号手机端未读残值；1.0.72 起账号栏已不再显示它们",
            phantom, int(rep.get("badge") or 0), int(rep.get("store") or 0),
            "、".join(f"{k}({v})" for k, v in worst) or "-",
        )

    def _filter_backlog_by_schedule(
        self, aged: List[Dict[str, Any]], cfg: Any, br: Any,
        *, now_ts: float,
    ) -> "tuple[List[Dict[str, Any]], int]":
        """按账号工作时间班表剔除「刻意扣留/复班宽限中」的积压稿。

        返回（仍算积压的列表, 豁免条数）。班表未启用 = 原样返回（零行为变更）；
        账号状态按 (platform, account) 记忆化（≤500 条稿也只探几次班表）；
        单条判定异常按「在等」计（宁可多报不漏报，与本检查其他路径同哲学）。
        """
        try:
            from src.inbox.work_hours_gate import (
                schedule_state,
                work_schedule_cfg,
            )
            ws = work_schedule_cfg(cfg if isinstance(cfg, dict) else {})
            if not ws.get("enabled"):
                return aged, 0
            try:
                grace_sec = max(0.0, float(
                    (br or {}).get("work_resume_grace_hours", 2) or 0)) * 3600.0
            except (TypeError, ValueError):
                grace_sec = 2 * 3600.0
            states: Dict[str, Dict[str, Any]] = {}
            kept: List[Dict[str, Any]] = []
            held = 0
            for d in aged:
                try:
                    plat = str(d.get("platform") or "")
                    acct = str(d.get("account_id") or "default")
                    key = f"{plat}:{acct}"
                    st = states.get(key)
                    if st is None:
                        st = schedule_state(ws, plat, acct, now_ts)
                        states[key] = st
                    if st.get("gated"):
                        if not st.get("in_hours"):
                            held += 1  # 休息中：刻意扣留，不算无人处理
                            continue
                        shift_start = float(st.get("shift_started_ts") or 0)
                        if (grace_sec > 0 and shift_start > 0
                                and now_ts - shift_start < grace_sec
                                and float(d.get("created_ts") or 0)
                                < shift_start):
                            held += 1  # 复班宽限：隔夜稿给补觉/坐席消化窗口
                            continue
                except Exception:
                    logger.debug("积压巡检班表判定失败（按在等计）", exc_info=True)
                kept.append(d)
            return kept, held
        except Exception:
            logger.debug("积压巡检班表过滤失败（忽略）", exc_info=True)
            return aged, 0

    def _case_ctx_store(self) -> Any:
        """案例巡检/周报共用的 ContextStore 发现（skill_manager 优先，tg 回落）。"""
        state = getattr(self._app, "state", self._app)
        sm = getattr(state, "skill_manager", None)
        if sm is None:
            tg = getattr(state, "telegram_client", None)
            sm = getattr(tg, "skill_manager", None) if tg is not None else None
        return getattr(sm, "_context_store", None) if sm is not None else None

    def _check_case_backlog(self, *, now: Optional[float] = None) -> None:
        """案例中心积压巡检：AI 立了案却无人认领/处理 → 主动轰人。

        存在理由与草稿积压同构：案例告警（case_alert）只在**开案那一刻**响一声
        （铃铛/webhook），之后没人接手是静默的——「案例跟进」页要有人开才有用。
        三档判据（都只数**未认领且未结案**的案例；已认领=有人在跟，不吵）：

        - **危机档**：severity 3 无人认领超 ``urgent_min``（默认 30 分钟）→ 立即报
          （数量不设门槛——危机一条就够格）；
        - **媒体档**（P5）：media_complaint 无人认领超 ``media_stale_hours``（默认
          随 case_center.DEFAULT_MEDIA_STALE_HOURS=4h）达 ``media_min_count``
          （默认 **1**）条 → 报。媒体质疑=穿帮风险，客户在等解释——severity 2
          的常规档要凑满 3 条才响，单条超龄的穿帮不该等凑数；
        - **常规档**：severity ≤2 无人认领超 ``min_age_hours``（默认 4h）达
          ``min_count``（默认 3）条 → 聚合报。

        重提间隔 ``interval_min``（默认 4h）；三档同时清零时补恢复通知。
        配置 ``health_watchdog.case_backlog_remind.{enabled,urgent_min,min_age_hours,
        min_count,media_stale_hours,media_min_count,interval_min}``
        （默认开——零案例天然静默）。事件仍是 ``case_backlog_alert``（``cases``
        别名一次订阅全收，不新增订阅面）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("case_backlog_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not br.get("enabled", True):
            return
        ctx_store = self._case_ctx_store()
        if ctx_store is None:
            return

        ts = float(now if now is not None else time.time())
        urgent_min = max(5.0, float(br.get("urgent_min", 30) or 30))
        min_age_h = max(0.5, float(br.get("min_age_hours", 4) or 4))
        min_count = max(1, int(br.get("min_count", 3) or 3))
        try:
            from src.utils.case_center import (
                DEFAULT_MEDIA_STALE_HOURS, collect_case_rows,
            )
            rows = collect_case_rows(ctx_store, now=ts)
        except Exception:
            logger.debug("案例积压巡检取数失败（忽略）", exc_info=True)
            return
        media_stale_h = max(0.5, float(
            br.get("media_stale_hours", DEFAULT_MEDIA_STALE_HOURS)
            or DEFAULT_MEDIA_STALE_HOURS))
        media_min_count = max(1, int(br.get("media_min_count", 1) or 1))

        urgent: List[Dict[str, Any]] = []
        stale: List[Dict[str, Any]] = []
        media_stale: List[Dict[str, Any]] = []
        for r in rows:
            if r.get("closed") or r.get("claimed_by"):
                continue
            created = float(r.get("created_at") or 0)
            if created <= 0:
                continue
            age_min = (ts - created) / 60.0
            if int(r.get("severity") or 1) >= 3:
                if age_min >= urgent_min:
                    urgent.append(r)
            else:
                if age_min >= min_age_h * 60.0:
                    stale.append(r)
                if (str(r.get("source") or "") == "media_complaint"
                        and age_min >= media_stale_h * 60.0):
                    media_stale.append(r)

        media_hit = len(media_stale) >= media_min_count
        if not urgent and not media_hit and len(stale) < min_count:
            if (not urgent and not stale and not media_stale
                    and self._remind.resolve(self._RK_CASE)):
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("case_backlog_alert", {
                        "recovered": True,
                        "rate_key": "case_backlog:recovered",
                    })
                    logger.info("HealthWatchdog 发出案例积压恢复通知")
                except Exception:
                    logger.debug("case_backlog recovery 发布失败（忽略）", exc_info=True)
            return

        interval_sec = max(600.0, float(br.get("interval_min", 240) or 240) * 60.0)
        # 内容指纹 = 三档里还挂着的案例 id 集合（危机档单独进指纹：新增危机必须立刻响）
        from src.inbox.remind_ledger import HOLD as _RL_HOLD, REMIND as _RL_REMIND, fingerprint as _rl_fp
        fp = _rl_fp(sorted(str(r.get("case_id") or r.get("created_at") or "") for r in urgent),
                    sorted(str(r.get("case_id") or r.get("created_at") or "")
                           for r in stale + media_stale))
        prev_fp = str(self._remind.get(self._RK_CASE).get("fingerprint") or "")
        # 条件真实成立时刻：三档各自算「第 N 老的那条满龄」的时刻，取最早成立的那档
        since_cands: List[float] = []
        _u = sorted(float(r.get("created_at") or 0.0) for r in urgent if float(r.get("created_at") or 0.0) > 0)
        if _u:
            since_cands.append(_u[0])
        _m = sorted(float(r.get("created_at") or 0.0) for r in media_stale if float(r.get("created_at") or 0.0) > 0)
        if media_hit and len(_m) >= media_min_count:
            since_cands.append(_m[media_min_count - 1] + media_stale_h * 3600.0)
        _s = sorted(float(r.get("created_at") or 0.0) for r in stale if float(r.get("created_at") or 0.0) > 0)
        if len(_s) >= min_count:
            since_cands.append(_s[min_count - 1] + min_age_h * 3600.0)
        since = min(since_cands) if since_cands else None
        verdict = self._remind.decide(
            self._RK_CASE, now=ts, interval_sec=interval_sec, fp=fp, since=since,
            unchanged_interval_sec=self._unchanged_interval_sec(br, interval_sec, 1440))
        if verdict == _RL_HOLD:
            return
        is_reminder = verdict == _RL_REMIND

        by_source: Dict[str, int] = {}
        oldest_h = 0.0
        for r in urgent + stale:
            src = str(r.get("source") or "unknown")
            by_source[src] = by_source.get(src, 0) + 1
            created = float(r.get("created_at") or 0)
            if created > 0:
                oldest_h = max(oldest_h, (ts - created) / 3600.0)
        media_oldest_h = 0.0
        for r in media_stale:
            created = float(r.get("created_at") or 0)
            if created > 0:
                media_oldest_h = max(media_oldest_h, (ts - created) / 3600.0)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("case_backlog_alert", {
                "remind_key": self._RK_CASE,
                "urgent_count": len(urgent),
                "stale_count": len(stale),
                "oldest_hours": round(oldest_h, 1),
                "by_source": by_source,
                "min_age_hours": min_age_h,
                # P5 媒体档（穿帮风险 SLA）：0 条时字段仍在，formatter 按需渲染
                "media_stale_count": len(media_stale),
                "media_stale_hours": media_stale_h,
                "media_oldest_hours": round(media_oldest_h, 1),
                "reminder": is_reminder,
                "unchanged": bool(is_reminder and prev_fp and prev_fp == fp),
                "since_ts": self._remind.first_seen(self._RK_CASE),
                "rate_key": "case_backlog:remind",
            })
        except Exception:
            logger.debug("case_backlog alert 发布失败（忽略）", exc_info=True)
            return
        self._remind.mark_sent(
            self._RK_CASE, now=ts, fp=fp,
            summary=(f"案例待跟进：危机级 {len(urgent)} 条、超龄 {len(stale)} 条"
                     + (f"、媒体质疑 {len(media_stale)} 条" if media_stale else "")
                     + f"（最久 {self._fmt_hours(oldest_h)}）"))
        self.total_case_backlog_alerts += 1
        logger.warning(
            "案例积压：危机级无人认领 %d 条、媒体质疑超龄 %d 条、常规超龄未认领 %d 条"
            "（最老 %.1fh，来源 %s）",
            len(urgent), len(media_stale), len(stale), oldest_h, by_source)

    def _check_drill_hygiene(self, *, now: Optional[float] = None) -> None:
        """演练残影自动清扫（P6）：最近活动超窗的未结 drill 案例自动结案归零。

        为什么需要它（2026-08-09 实锤）：duel_nightly 收尾会 close-drill，但白天
        各线 **ad-hoc** 跑对练（bubble pacing 等）不走那个脚本——上午刚清完 3 条、
        28 分钟后又积 3 条。残影只在 /cases「演练数据」筛选下可见、不进任何计数/
        告警，但「测试数据不得污染跟进面」应该是机制而不是人肉习惯。

        判据＝**最近活动**（末条信号 ts）早于 ``drill_autoclean_hours``（默认 6h，
        0=关）——正在跑的演练信号还在进、不会被中途搅局；仅动保留号段 uid，真实
        客户零误伤面。30min 节流；清扫是卫生动作不是事故，只记 INFO 不发告警。
        配置挂 ``health_watchdog.case_backlog_remind.drill_autoclean_hours``
        （与案例巡检同块——同一个 ctx_store、同一族关注点）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        br = (((cfg.get("health_watchdog") or {}).get("case_backlog_remind"))
              or {}) if isinstance(cfg, dict) else {}
        try:
            clean_h = float(br.get("drill_autoclean_hours", 6) or 0)
        except (TypeError, ValueError):
            clean_h = 6.0
        if clean_h <= 0:
            return
        ts = float(now if now is not None else time.time())
        if ts - getattr(self, "_drill_clean_last", 0.0) < 1800.0:
            return
        self._drill_clean_last = ts
        ctx_store = self._case_ctx_store()
        if ctx_store is None:
            return
        try:
            from src.utils.case_center import close_open_drill_cases
            n = close_open_drill_cases(
                ctx_store, resolution="drill auto-clean (watchdog)",
                now=ts, persist=True, min_age_hours=clean_h)
        except Exception:
            logger.debug("演练残影清扫失败（忽略）", exc_info=True)
            return
        if n:
            self.total_drill_autocleaned = (
                getattr(self, "total_drill_autocleaned", 0) + n)
            logger.info("演练残影自动清扫：结案 %d 条（最近活动早于 %.1fh 的 drill 案例）",
                        n, clean_h)

    def _check_login_funnel(self, *, now: Optional[float] = None) -> None:
        """某平台某接入方式「发起 N 次、成功 0 次」→ 主动轰人。

        为什么需要它（2026-07-25 事故的第二半）：LINE 协议扫码 100% 失败烂了多日，
        根因是**没有任何一层在看「发起了多少次、成功了几次」**。为此建了
        ``login_funnel_stats`` 并在 ops 卡上画了 stalled 标记——但那仍是**被动**的：
        看板要有人打开才有用，而登录链路坏掉时通常没人正盯着运营总览。
        这条巡检把同一判据变成推送，闭合最后一公里。

        **判据是「距上次成功以来」而非累计**：累计口径（``authorized == 0``）会让
        「上周成功过、这周彻底坏掉」的链路永久静默，而回归比从没通过的新链路更隐蔽。
        计数由 ``login_funnel_stats`` 侧维护（``started_since_success``，成功即清零），
        **与 ops 卡的 stalled 标记同一字段**——否则会出现「收到告警去看板却显示健康」，
        比没告警更糟。

        - ``started_since_success >= min_started`` → 报（成功过一次即自动清零闭嘴）；
        - **重提要求它又涨了**：夜里没人再试就不刷屏（比单纯按时间重提精确，也免掉
          「已知坏、修不了」的方式每 4 小时叫一次）；
        - 恢复通知要**正面证据**（累计 authorized 真的涨了）；计数回退（进程重启 /
          stats.reset）不算成功。

        零流量天然静默，故默认开。
        配置 ``health_watchdog.login_funnel_remind.{enabled,min_started,interval_min}``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        lf = (((cfg.get("health_watchdog") or {}).get("login_funnel_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not lf.get("enabled", True):
            return
        min_started = max(2, int(lf.get("min_started", 5) or 5))
        interval_sec = max(600.0, float(lf.get("interval_min", 240) or 240) * 60.0)
        ts = float(now if now is not None else time.time())
        try:
            from src.integrations.login_funnel_stats import get_login_funnel_stats
            rows = (get_login_funnel_stats().dump() or {}).get("rows") or []
        except Exception:
            logger.debug("接入漏斗取数失败（忽略）", exc_info=True)
            return

        for row in rows:
            key = str(row.get("key") or "")
            if not key or key == "__other__":
                continue
            try:
                started = int(row.get("started") or 0)
                authorized = int(row.get("authorized") or 0)
            except (TypeError, ValueError):
                continue

            # 旧快照（升级窗口内的进程/测试替身）没有 since 字段时退回累计口径：
            # 少报「回归」形态，但不会误报。
            d_started = int(row.get("started_since_success",
                                    started if authorized == 0 else 0) or 0)
            base_auth = self._lf_base.get(key, authorized)
            if authorized < base_auth:
                base_auth = authorized      # 计数回退＝进程重启，不是「成功了」
                self._lf_base[key] = authorized

            if authorized > base_auth:
                self._lf_base[key] = authorized
                if key in self._lf_alerted:
                    try:
                        from src.integrations.shared.event_bus import get_event_bus
                        get_event_bus().publish("login_funnel_alert", {
                            "recovered": True, "key": key,
                            "rate_key": f"login_funnel:{key}:recovered",
                        })
                        logger.info("HealthWatchdog 发出接入链路恢复通知（%s）", key)
                    except Exception:
                        logger.debug("login_funnel recovery 发布失败（忽略）", exc_info=True)
                    self._lf_alerted.pop(key, None)
                    self._lf_last_remind.pop(key, None)
                continue

            self._lf_base[key] = authorized
            if d_started < min_started:
                continue
            prev = self._lf_alerted.get(key)
            if prev is not None:
                # 没有新的失败尝试就不重提——「坏着但没人再试」不值得每 4h 叫一次
                if d_started <= prev or ts - self._lf_last_remind.get(key, 0.0) < interval_sec:
                    continue

            plat, _, mode = key.partition(":")
            reasons = row.get("reasons") or {}
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("login_funnel_alert", {
                    "key": key, "platform": plat, "mode": mode,
                    "started": d_started,
                    "qr_shown": int(row.get("qr_shown_since_success",
                                            row.get("qr_shown") or 0) or 0),
                    "failed": int(row.get("failed_since_success",
                                          row.get("failed") or 0) or 0),
                    # 归因分布是「代码坏了 vs 账号被风控」的分水岭：全是 checkpoint
                    # 就别去查代码了。（累计口径，够定性；精确到窗口不值得为它建时序表）
                    "reasons": dict(reasons),
                    "reminder": prev is not None,
                    "rate_key": f"login_funnel:{key}",
                })
            except Exception:
                logger.debug("login_funnel alert 发布失败（忽略）", exc_info=True)
                continue
            self._lf_alerted[key] = d_started
            self._lf_last_remind[key] = ts
            self.total_login_funnel_alerts += 1
            logger.warning("账号接入链路停摆：%s 近期发起 %d 次、成功 0 次（原因分布 %s）",
                           key, d_started, reasons or "—")

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

    def _check_scan_loop_stall(self, *, now: Optional[float] = None) -> None:
        """常备扫描循环停摆巡检（P4 2026-08-09）。

        事故背景：目标结算/提醒扫描与工作链推进都曾挂在 ``report.enabled``
        闸死的调度器上**静默从未运行**（三轮开发后才被生产探针拆穿）。两者已
        迁常备循环并挂心跳（``app.state.goal_scan_state`` /
        ``workflow_autorun_state``），本巡检把「心跳停走」变成主动告警——
        「没跑」和「没货」必须分得出来，且不能再靠人想起来去看。

        判据（每循环独立）：心跳 dict 缺失（挂载失败/被移除）或
        ``last_tick_ts`` 停走超 ``stall_min``（tick=60s → 默认 10 分钟=缺 10 拍）
        → 首提 + ``interval_min`` 重提 + 心跳恢复补发恢复通知。循环内部被
        配置闸住（gated 非空）**不算停摆**——闸住时心跳照跳，这正是二者的
        判别面。配置 ``health_watchdog.scan_loop_remind.{enabled,stall_min,
        interval_min}``（默认开——这是巡检不是新行为）。"""
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        sc = ((cfg.get("health_watchdog") or {}).get("scan_loop_remind")
              or {})
        if not isinstance(sc, dict):
            sc = {}
        if not bool(sc.get("enabled", True)):
            return
        try:
            stall_min = float(sc.get("stall_min", 10) or 10)
        except (TypeError, ValueError):
            stall_min = 10.0
        try:
            interval_min = float(sc.get("interval_min", 240) or 240)
        except (TypeError, ValueError):
            interval_min = 240.0
        ts = float(now if now is not None else time.time())
        if not hasattr(self, "_scanloop_alerted"):
            self._scanloop_alerted: Dict[str, float] = {}
            self._scanloop_watch_since = ts
        state_root = getattr(self._app, "state", self._app)
        # D1b P0-5（2026-09-05）：冲刺推进器 / care 派发循环也进同一张停摆表——
        # 「自动推进」真开的两条腿：ticker 排拍、dispatcher 真发，任一停走目标卡
        # 照样写着「下一主动拍≈HH:MM」而一条都不会出手。两者是对象型循环
        # （有 last_tick_ts/_interval/is_running），经 _loop_heartbeat 折成与
        # dict 心跳同构。dispatcher 被 bootstrap 刻意跳过（ai_missing）不算停摆。
        care_eng = getattr(state_root, "care_engine", None)
        care_eng = care_eng if isinstance(care_eng, dict) else {}
        loops = [
            ("goal_scan", getattr(state_root, "goal_scan_state", None)),
            ("workflow_autorun",
             getattr(state_root, "workflow_autorun_state", None)),
            ("goal_sprint", _loop_heartbeat(
                getattr(state_root, "goal_sprint_ticker", None)
                or care_eng.get("sprint_ticker"))),
        ]
        if not care_eng.get("dispatcher_skip"):
            loops.append(("care_dispatch",
                          _loop_heartbeat(care_eng.get("dispatcher"))))
        from src.integrations.shared.event_bus import get_event_bus
        for name, st in loops:
            last_tick = float((st or {}).get("last_tick_ts") or 0.0)
            # 循环自带节拍（ticker 默认 120s、可热调到更长）时，停摆阈值至少
            # 放宽到 3 拍——否则运营把 interval 调到 10 分钟就天天误报。
            loop_itv = float((st or {}).get("interval_sec") or 0.0)
            stall_sec = max(stall_min * 60.0, loop_itv * 3.0)
            if last_tick <= 0:
                # 心跳从未出现：挂载失败或旧进程。给足启动宽限（watch_since
                # 起算），宽限外仍无心跳 → 与停摆同罪。
                stalled = (ts - float(self._scanloop_watch_since)) \
                    >= stall_sec
                stalled_min = ((ts - float(self._scanloop_watch_since)) / 60.0
                               if stalled else 0.0)
            else:
                stalled = (ts - last_tick) >= stall_sec
                stalled_min = (ts - last_tick) / 60.0
            alerted_at = float(self._scanloop_alerted.get(name) or 0.0)
            if stalled:
                if alerted_at and (ts - alerted_at) < interval_min * 60.0:
                    continue
                payload = {
                    "loop": name,
                    "stalled_min": round(stalled_min, 1),
                    "ticks": int((st or {}).get("ticks") or 0),
                    "last_tick_ts": last_tick,
                    "mounted": st is not None,
                    # 对象型循环带 asyncio task 活性：False=task 已退出（崩了）
                    # 而非「慢」；dict 心跳没有此字段 → None
                    "running": (st or {}).get("running"),
                    "reminder": bool(alerted_at),
                    "rate_key": f"scan_stall:{name}",
                }
                try:
                    get_event_bus().publish("scan_loop_stall_alert", payload)
                    self._scanloop_alerted[name] = ts
                    logger.warning(
                        "[scan-stall] 常备循环 %s 心跳停走 %.1f 分钟"
                        "（mounted=%s ticks=%s）", name, stalled_min,
                        st is not None, (st or {}).get("ticks"))
                except Exception:
                    logger.debug("scan stall publish failed", exc_info=True)
            elif alerted_at:
                # 心跳恢复 → 补发恢复通知并清标记（只发给告过警的，防噪）
                try:
                    get_event_bus().publish("scan_loop_stall_alert", {
                        "loop": name, "recovered": True,
                        "rate_key": f"scan_stall:{name}:recovered",
                    })
                except Exception:
                    logger.debug("scan stall recover publish failed",
                                 exc_info=True)
                self._scanloop_alerted.pop(name, None)

    def _check_image_models(self, *, now: Optional[float] = None) -> None:
        """出图模型「被删/失踪」哨兵（2026-08-22 事故防再犯）。

        事故：176 的 ``D:\\ComfyUI\\models`` 被整树清空，checkpoint 清单变空 →
        所有现场生图 400——从删除到被人发现隔了数小时，唯一暴露面是坐席面板
        里一段被截断的乱码。本巡检把「模型清单从有到无」变成主动告警：

        - 探测复用 ``image_gen_routes.probe_comfy_models``（服务端 /object_info
          清单）+ ``engine_deploy_status``（flux_pulid 部署判定）；
        - **只在「见过 deployed=true」之后 true→false 才告警**——冷启动/从未
          部署的机器天然静默零误报；ComfyUI 整机不可达也不由本检查管
          （probe=None → 静默，服务活性归 176 侧 ComfyWatchdog）；
        - 首提即发（模型没了=生图链全灭，不设 after 宽限），仍未恢复每
          ``interval_min``（默认 240）重提；恢复补发恢复通知。
        配置 ``health_watchdog.image_models_watch.{enabled,interval_min}``（默认开）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        watch = (((cfg.get("health_watchdog") or {}).get("image_models_watch"))
                 or {}) if isinstance(cfg, dict) else {}
        if not watch.get("enabled", True):
            return
        scfg = ((cfg.get("companion") or {}).get("selfie") or {}) \
            if isinstance(cfg, dict) else {}
        if not bool(scfg.get("enabled", False)):
            return
        try:
            from src.web.routes.image_gen_routes import (
                _comfy_url_from_args, engine_deploy_status, probe_comfy_models)
        except Exception:
            return
        url = _comfy_url_from_args(((scfg.get("provider") or {}).get("command_args")))
        if not url:
            return
        models = probe_comfy_models(url)
        if models is None:
            return  # 服务不可达 ≠ 模型被删（活性另有看门狗）；不猜不报
        ts = float(now if now is not None else time.time())
        deployed = bool(engine_deploy_status(models, ["flux_pulid"]).get("flux_pulid"))
        seen_ok = bool(getattr(self, "_imgm_seen_ok", False))
        alerted = bool(getattr(self, "_imgm_alerted", False))
        last_remind = float(getattr(self, "_imgm_last_remind", 0.0))

        if deployed:
            if alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("image_models_alert", {
                        "recovered": True, "url": url,
                        "rate_key": "image_models:recovered",
                    })
                    logger.info("HealthWatchdog 发出出图模型恢复通知")
                except Exception:
                    logger.debug("image_models recovery 发布失败（已忽略）",
                                 exc_info=True)
            self._imgm_seen_ok = True
            self._imgm_alerted = False
            self._imgm_last_remind = 0.0
            return

        if not seen_ok:
            return  # 从未见过好状态：新装机/未部署环境不误报
        interval_sec = max(600.0, float(watch.get("interval_min", 240) or 240) * 60.0)
        if alerted and ts - last_remind < interval_sec:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("image_models_alert", {
                "url": url,
                "ckpts": len(models.get("ckpts") or []),
                "unets": len(models.get("unets") or []),
                "reminder": alerted,
                "rate_key": "image_models:remind",
            })
            logger.warning(
                "HealthWatchdog 出图模型失踪告警 url=%s ckpts=%d（此前曾部署）",
                url, len(models.get("ckpts") or []))
        except Exception:
            logger.debug("image_models alert 发布失败（已忽略）", exc_info=True)
        self._imgm_alerted = True
        self._imgm_last_remind = ts

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
        # 账本判定（跨重启：down_since / alerted / last_remind 都是持久态）；指纹 = 故障形态，
        # 形态没变默认仍按 interval 重提（硬件掉线不该因「没变化」而沉默），可配 unchanged_interval_min 放宽
        from src.inbox.remind_ledger import HOLD as _RL_HOLD, fingerprint as _rl_fp
        fp = _rl_fp(kind, bool(probe.get("reachable")), bool(probe.get("models_loaded")),
                    str(probe.get("error") or "")[:80])
        verdict = self._remind.decide(
            self._RK_AVATAR, now=ts, after_sec=after_sec, interval_sec=interval_sec, fp=fp,
            unchanged_interval_sec=self._unchanged_interval_sec(ar, interval_sec))
        if verdict == _RL_HOLD:
            return
        # ── 救援链活性（2026-08-14 事故：7852 掉线 9h 无自愈——Boot/Watchdog
        # 两个计划任务都 Disabled，而告警只说「掉线」，运维默认看门狗会拉）。
        # 只在 kind=down 且真的要发告警时才探（subprocess ×2，已被 due 限流）；
        # 只读不 /Change——恢复任务是运维决策（代码模式期间停用可能是刻意的）。
        # 仅本机目标才探（2026-08-18）：救援任务是本机 schtasks，目标迁远端（140）
        # 后再点名本机任务＝把退役刻意态谎报成救援链断裂。
        rescue_broken: list = []
        if kind == "down" and avatar_probe_host_is_local(probe.get("url")):
            try:
                from src.ai.avatar_voice_rescue import (
                    broken_rescue_tasks, probe_rescue_tasks,
                    resolve_rescue_task_names)
                rescue_broken = broken_rescue_tasks(
                    probe_rescue_tasks(resolve_rescue_task_names(
                        cfg if isinstance(cfg, dict) else {})))
            except Exception:
                rescue_broken = []
            if rescue_broken:
                logger.warning(
                    "AvatarHub 7852 掉线且救援计划任务已停用/缺失：%s"
                    "——自动拉起不会发生，需人工恢复任务",
                    ", ".join(rescue_broken))
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("avatar_voice_alert", {
                "remind_key": self._RK_AVATAR,
                "reachable": bool(probe.get("reachable")),
                "models_loaded": bool(probe.get("models_loaded")),
                "url": str(probe.get("url") or ""),
                "error": str(probe.get("error") or ""),
                "hang": kind == "hang",
                "fail_streak": int(sig.get("fail_streak") or 0),
                "down_minutes": int(down_sec // 60),
                "reminder": bool(self._avatar_alerted),
                # 救援链断裂清单（空=正常/未知）：formatter 据此把
                # 「等看门狗」的默认指引换成「救援链已死，需人工恢复」
                "rescue_broken": rescue_broken,
                # 独立限流键：首提/重提不与其他事件挤 1h 窗
                "rate_key": "avatar_voice:remind",
            })
        except Exception:
            logger.debug("avatar_voice alert 发布失败（已忽略）", exc_info=True)
            return
        self._remind.mark_sent(
            self._RK_AVATAR, now=ts, fp=fp,
            summary=("语音合成服务 7852 " + ("卡死无响应" if kind == "hang" else "不可达")
                     + (f"（{str(probe.get('error') or '')[:60]}）" if probe.get("error") else "")))
        self.total_avatar_voice_reminders += 1

    def _check_hub_engine(self, *, now: Optional[float] = None) -> None:
        """hub 引擎目录离线哨兵（2026-08-22「fish 冒充 IndexTTS-2」事故沉淀）。

        事故形态：配置钉死 ``hub_fish.tts_engine=index_tts``，但 hub 的
        ``/api/engines`` 目录把它标成 ``available:false``（显存吃紧被泊车/掉登记），
        **hub 的路由只认目录这一票**——于是静默换成 fish_speech 合成，客户听到的
        是另一个人的声音。引擎进程自身 ``/health`` 200、``model_loaded:true``，
        所以既有的 7852 探针、hang 检测、语音断档台账**三条线全绿**。

        为什么不并进 ``_check_avatar_voice``：那组盯的是本机 7852（另一台机、另一
        套失败域、另一套救援链），共用计时器会让两边的首提/重提互相顶掉，且恢复
        语义不同（这里有确定性的目录读数，不需要 hang 那种「正面证据」推断）。

        为什么不等流量：本哨兵的**全部价值**就是零流量也能响。既有的
        ``_check_voice_outage`` 要「有人要语音且全在失败」才判——夜里没人说话时
        引擎掉线，要等第二天第一批客户先听到别人的声音（lenient）或先收不到语音
        （strict）才会有人知道。目录是**合成前**就能读到的确定信号。

        判据（零误报优先）：
          - 只在 ``hub_fish.enabled`` 且**显式钉了** ``tts_engine`` 时才检
            （没钉引擎＝接受 hub 自选，顶包无从谈起）；
          - 只认目录明说的 ``available:false``（``offline``）。目录拉不到
            （``unknown``，hub 整体挂了）→ 静默，那是 hub 可达性的活，本哨兵不抢；
            目录里查无此名（``unlisted``）→ 也静默，配置写错该由预检/首次合成报，
            让夜间告警去猜配置笔误只会造噪音；
          - 离线 ≥ ``after_min``（默认 20min，给正常的泊车/唤醒循环留窗）→ 首提，
            此后每 ``interval_min``（默认 240）重提；转 ``available:true`` → 恢复通知。
        配置 ``health_watchdog.hub_engine_remind.{enabled,after_min,interval_min}``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        hr = ((cfg.get("health_watchdog") or {}).get("hub_engine_remind")) or {}
        if not hr.get("enabled", True):
            return
        av = (cfg.get("avatar_voice") or {})
        if not isinstance(av, dict) or not av.get("enabled", False):
            return
        hf = av.get("hub_fish") or {}
        if not isinstance(hf, dict) or not hf.get("enabled", False):
            return
        engine = str(hf.get("tts_engine") or "").strip()
        if not engine:
            return  # 没钉引擎＝接受 hub 自选，不存在「被顶包」
        base_url = hf.get("base_url") or ""

        ts = float(now if now is not None else time.time())
        try:
            from src.ai.avatar_voice import hub_engine_directory_status
            snap = hub_engine_directory_status(base_url, engine)
        except Exception:
            logger.debug("hub 引擎目录读取失败（已忽略）", exc_info=True)
            return
        state = str(snap.get("state") or "unknown")
        if state != "offline":
            if state == "ok" and self._hub_engine_alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("hub_engine_alert", {
                        "recovered": True,
                        "engine": snap.get("engine") or engine,
                        "rate_key": "hub_engine:recovered",
                    })
                    logger.info("HealthWatchdog 发出 hub 引擎恢复通知 engine=%s",
                                snap.get("engine") or engine)
                except Exception:
                    logger.debug("hub_engine recovery 发布失败（已忽略）",
                                 exc_info=True)
            if state == "ok":
                self._hub_engine_down_since = 0.0
                self._hub_engine_alerted = False
                self._hub_engine_last_remind = 0.0
            # unknown/unlisted：判不了，保持现状（不清零也不推进计时）
            return

        if not self._hub_engine_down_since:
            self._hub_engine_down_since = ts
            return
        down_sec = ts - self._hub_engine_down_since
        after_sec = max(60.0, float(hr.get("after_min", 20) or 20) * 60.0)
        interval_sec = max(600.0, float(hr.get("interval_min", 240) or 240) * 60.0)
        due = (
            (not self._hub_engine_alerted and down_sec >= after_sec)
            or (self._hub_engine_alerted
                and ts - self._hub_engine_last_remind >= interval_sec)
        )
        if not due:
            return
        # strict 决定后果性质：strict＝拒发（客户收不到语音，看得见的缺失）；
        # lenient＝顶包（客户听到别人的声音，看不见的破绽——更该修）
        strict = str(av.get("voice_consistency") or "lenient").strip().lower() == "strict"

        # 先自救再轰人：既然已经到了要叫醒人的地步，那就先替他按一下那个他到了
        # 也只会按的按钮（``POST /api/services/ensure``＝把我们钉的引擎拉起来）。
        # 挂在告警节奏上而非每 tick——最多 4h 一次、幂等、只点名自己钉的引擎，
        # **绝不代运维泊车别人**（谁让路属跨条线的显存策略，hub 自有 lease 机制）。
        # 成了的话下一 tick 就是恢复通知，人可能根本不用起床。
        # 开关刻意与合成路径**共用一个键**（``avatar_voice.hub_fish.auto_wake``，
        # 就在 ``tts_engine`` 旁边＝它作用的对象旁边）。两处各设一个键会让运营
        # 关了一个以为关全了，而另一条还在替他动生产引擎。
        wake_state = ""
        if hf.get("auto_wake", True):
            try:
                from src.ai.avatar_voice import hub_engine_wake
                ok, detail = hub_engine_wake(base_url, engine)
                wake_state = ("accepted:" + detail) if ok else ("failed:" + detail)
                logger.info("hub 引擎离线 → 已尝试唤醒 engine=%s 结果=%s",
                            engine, wake_state)
            except Exception:
                logger.debug("hub 引擎唤醒尝试异常（已忽略）", exc_info=True)
                wake_state = "failed:exception"

        # 「谁占着显存」几乎总是这类事故的答案（2026-08-22 实测：唱歌工作室在线
        # 占 5.7G，质量轨 index_tts 需 ~8.7G 而空闲仅 6.4G）——不指名道姓的话，
        # 运维只知道「引擎离线了」，还得自己去翻 GPU 面板才知道该让谁让路。
        blockers: List[Dict[str, Any]] = []
        try:
            from src.ai.avatar_voice import (
                hub_engine_service_name, hub_vram_blockers,
            )
            # 排掉自己：候选表里出现「泊掉 index_tts 可让出 8.5G」时，那正是我们要
            # 救的引擎——自指建议会把运维直接引向这次故障本身（2026-08-22）。
            blockers = list((hub_vram_blockers(
                base_url,
                exclude=hub_engine_service_name(base_url, engine),
            ) or {}).get("hosts") or [])
        except Exception:
            logger.debug("hub 显存归因读取失败（已忽略）", exc_info=True)

        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("hub_engine_alert", {
                "engine": snap.get("engine") or engine,
                "url": str(base_url or ""),
                "strict": strict,
                "available_engines": list(snap.get("available_engines") or [])[:8],
                "down_minutes": int(down_sec // 60),
                "reminder": bool(self._hub_engine_alerted),
                "wake": wake_state,
                "vram_hosts": blockers[:3],
                "rate_key": "hub_engine:remind",
            })
            logger.warning(
                "HealthWatchdog hub 引擎离线告警 engine=%s down=%dmin strict=%s",
                snap.get("engine") or engine, int(down_sec // 60), strict)
        except Exception:
            logger.debug("hub_engine alert 发布失败（已忽略）", exc_info=True)
            return
        self._hub_engine_alerted = True
        self._hub_engine_last_remind = ts
        self.total_hub_engine_reminders += 1

    def _check_voice_outage(self, *, now: Optional[float] = None) -> None:
        """语音出站断档巡检（2026-08-02）：滚动窗内「尝试 ≥N 次且 0 成功」告警。

        为什么 ``_check_avatar_voice`` 不够：zhiliao 语音链断 5 天（hub 音色档
        404 → tts_pipeline voice_consistency=strict 全部拒发）零告警——hang 检测
        要求「7852 探针绿 + 失败 streak ≥3 且最近失败 ≤20min 新鲜」，低流量下
        零星请求隔几小时失败一次，永远不新鲜；探针又一直绿。本巡检改用
        ``voice_outage`` 台账（A 线 sender / B 线 autosend 的最终成败账），只看
        滚动窗聚合：**只要有人要语音且全在失败**，无论频率多低都会响。

        判据（零误报优先，信息不足一律静默）：
          - 窗口内 ``attempts ≥ min_attempts``（默认 3，防单条巧合）且 **0 成功**
            → 首提（EventBus ``voice_outage_alert``），此后每 ``interval_min``
            （默认 240）重提；
          - 告警过之后窗口内出现**任一成功**（正面证据）→ 补发恢复通知一次；
            无流量导致样本掉出窗口 ≠ 恢复，保持已告警态等证据；
          - 台账空/读取异常/快照坏形 → 静默。
        配置 ``health_watchdog.voice_outage_remind.{enabled,min_attempts,
        window_hours,interval_min}``（默认开 / 3 / 24 / 240）。

        **两条臂共用这一个开关与这套阈值**：本函数是全局臂（所有链一起看），
        ``_check_voice_outage_per_source`` 是单链臂（逐 source 看）。后者必须在
        全局臂的 ``ok_n > 0`` 早退**之前**跑——那个早退恰好覆盖了单链臂唯一
        有价值的场景（一条链全灭、被另一条健康链的成功数盖住）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        vr = (((cfg.get("health_watchdog") or {}).get("voice_outage_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not vr.get("enabled", True):
            return
        ts = float(now if now is not None else time.time())
        try:
            window_hours = max(1.0, float(vr.get("window_hours", 24) or 24))
        except (TypeError, ValueError):
            window_hours = 24.0
        try:
            from src.ai.voice_outage import get_voice_outage
            snap = get_voice_outage().outage_snapshot(
                now=ts, window_hours=window_hours)
        except Exception:
            return  # 台账不可用=信息不足，宁可漏报不误报
        if not isinstance(snap, dict):
            return
        attempts = int(snap.get("attempts_24h") or 0)
        ok_n = int(snap.get("ok_24h") or 0)

        # 单链臂先跑：全局臂在「窗内有成功」时会 return，而那恰恰是单链臂唯一
        # 有价值的场景（一条链全灭、被另一条健康链的成功数盖住）。
        try:
            self._check_voice_outage_per_source(
                snap, ts=ts, cfg=vr, window_hours=window_hours)
        except Exception:
            logger.debug("语音单链断档巡检异常（已忽略）", exc_info=True)

        if ok_n > 0:
            # 窗口内有成功=链路活着；从「已告警」恢复 → 补发恢复通知一次
            # （未告警过的正常态不发，防噪）。
            if self._vo_alerted:
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("voice_outage_alert", {
                        "recovered": True,
                        "rate_key": "voice_outage:recovered",
                    })
                    logger.info("HealthWatchdog 发出语音出站恢复通知")
                except Exception:
                    logger.debug(
                        "voice_outage recovery 发布失败（已忽略）", exc_info=True)
            self._vo_alerted = False
            self._vo_last_remind = 0.0
            return

        min_attempts = max(1, int(vr.get("min_attempts", 3) or 3))
        if attempts < min_attempts:
            # 样本不足不判：不告警、也不无凭据发恢复（已告警态保持，等正面证据）
            return

        interval_sec = max(600.0, float(vr.get("interval_min", 240) or 240) * 60.0)
        due = ((not self._vo_alerted)
               or (ts - self._vo_last_remind >= interval_sec))
        if not due:
            return
        reasons = snap.get("fail_reasons") or {}
        top_reasons = dict(sorted(
            reasons.items(), key=lambda kv: (-int(kv[1] or 0), kv[0]))[:3])
        last_ok_ts = float(snap.get("last_ok_ts") or 0.0)
        last_ok_hours = (round((ts - last_ok_ts) / 3600.0, 1)
                         if last_ok_ts > 0 else -1.0)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("voice_outage_alert", {
                "attempts": attempts,
                "window_hours": int(window_hours),
                # 台账事件挤满时名义窗是假的（更早的失败已被挤掉），如实随告警
                # 带上真实覆盖——运维照 24h 读会低估断档时长、也会以为「早上没事」
                "window_truncated": bool(snap.get("truncated")),
                "effective_window_hours": float(
                    snap.get("effective_window_hours") or window_hours),
                "consecutive_fails": int(snap.get("consecutive_fails") or 0),
                "top_reasons": top_reasons,
                "last_ok_hours": last_ok_hours,
                "by_source": dict(snap.get("by_source") or {}),
                # 原因指向 hub 时随告警带显存归因（谁占着、能不能让路）——
                # 这一档的根因几乎总是显存，而告警响的时间几乎总是凌晨
                "vram_hosts": self._vram_hosts_for_reasons(top_reasons),
                "reminder": bool(self._vo_alerted),
                # 独立限流键：首提/重提不与其他事件挤 1h 窗
                "rate_key": "voice_outage:remind",
            })
        except Exception:
            logger.debug("voice_outage alert 发布失败（已忽略）", exc_info=True)
            return
        self._vo_alerted = True
        self._vo_last_remind = ts
        self.total_voice_outage_alerts += 1
        logger.warning(
            "语音出站断档：%s 小时窗内 %d 次尝试 0 成功（连败 %d，原因 Top %s）"
            "——该发语音的回复全部回落文字",
            int(window_hours), attempts,
            int(snap.get("consecutive_fails") or 0), top_reasons)

    # 显存归因只对「共用合成层」那几种原因有意义（`hub_` 前缀）。设备端的
    # share intent 失败与显存无关，为它去打一次 hub GPU 面板纯属浪费。
    _HUB_STARVED_MARKERS = ("hub_synth_timing_out", "hub_engine_offline",
                            "hub_engine_mismatch")

    def _vram_hosts_for_reasons(self, reasons: Any) -> List[Dict[str, Any]]:
        """失败原因指向 hub 时，随告警带上「谁占着显存、能不能让路」。

        为什么长在这里而不是让 formatter 自己去查：formatter 跑在 webhook 投递
        线程上、且同一份 payload 可能投多个渠道，在那儿发 HTTP 会把「通知」变成
        「可能超时的通知」。watchdog 本就在巡检线程、本就已经为 ``hub_engine``
        那族告警调过同一个函数——**唯一的缺口是这族告警从没调过它**
        （2026-08-22 事故里真正响的恰好是这半边，正文只写了「自己去看 GPU 面板」）。

        只在原因确实指向 hub 时才调（``_HUB_STARVED_MARKERS``）：设备端 share
        intent 失败与显存无关，为它多打一次 hub 是白付延迟。读不到一律返空列表
        ——归因缺失只让告警少一段，绝不能让告警发不出去。
        """
        try:
            keys = list(reasons.keys()) if isinstance(reasons, dict) else []
        except Exception:
            return []
        if not any(m in str(k) for k in keys for m in self._HUB_STARVED_MARKERS):
            return []
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return []
        hf = ((cfg.get("avatar_voice") or {}).get("hub_fish")) or {}
        if not isinstance(hf, dict):
            hf = {}
        base_url = hf.get("base_url") or ""
        try:
            from src.ai.avatar_voice import (
                hub_engine_service_name, hub_vram_blockers,
            )
            # 同上：质量轨引擎自己不算「可让路的」（它就是被饿死的那个）
            skip = hub_engine_service_name(base_url, hf.get("tts_engine"))
            return list((hub_vram_blockers(
                base_url, exclude=skip) or {}).get("hosts") or [])[:3]
        except Exception:
            logger.debug("语音断档显存归因读取失败（已忽略）", exc_info=True)
            return []

    def _check_voice_outage_per_source(
        self, snap: Dict[str, Any], *, ts: float,
        cfg: Dict[str, Any], window_hours: float,
    ) -> None:
        """单链断档臂：某一条语音链窗内全灭，即便别的链健康也要报。

        为什么值得单独一臂（2026-08-22 接线 wa_rpa/mr_rpa 时想清楚的）：全局臂
        判「窗内 0 成功」，**一条健康的链就能把另一条全灭的链盖住**——台账里
        有 5 个 source，各自走不同的 TTS 后端/投递通道（A 线原生 voice、B 线
        autosend、坐席手动、WhatsApp share-sheet、Messenger share-sheet），
        「share intent 那条彻底不通而 Telegram 一切正常」不是边角情形而是常态。
        当晚事故的另一半正是这个形态：hub 引擎被挤爆时 WhatsApp 语音全灭，
        而台账（就算当时接了线）也会因为其他链有成功而全局绿灯。

        判据沿用全局臂的保守口径，只把分母换成单链：该 source 窗内
        ``attempts ≥ min_attempts`` 且 ``ok == 0`` → 首提，此后每
        ``interval_min`` 重提；出现任一成功 → 补发恢复通知一次。样本不足
        （新链刚上线只发过一两次）一律静默。source 数天然低基数（每条链一个
        字面量），状态字典不会无界增长。
        """
        by_source = snap.get("by_source") or {}
        if not isinstance(by_source, dict):
            return
        min_attempts = max(1, int(cfg.get("min_attempts", 3) or 3))
        interval_sec = max(600.0, float(cfg.get("interval_min", 240) or 240) * 60.0)
        # 全局臂已在报「所有链都灭了」——此时逐链重报一遍纯属刷屏（同一件事
        # 说 5 遍），单链臂只负责它独有的那个形态：**有链活着**却有链全灭。
        global_all_dead = int(snap.get("ok_24h") or 0) == 0

        for src, st in sorted(by_source.items()):
            if not isinstance(st, dict):
                continue
            src = str(src or "unknown")
            attempts = int(st.get("attempts") or 0)
            ok_n = int(st.get("ok") or 0)
            if ok_n > 0:
                if self._vo_src_alerted.get(src):
                    try:
                        from src.integrations.shared.event_bus import get_event_bus
                        get_event_bus().publish("voice_outage_alert", {
                            "recovered": True,
                            "source": src,
                            "rate_key": f"voice_outage:src:{src}:recovered",
                        })
                        logger.info("HealthWatchdog 发出语音单链恢复通知 source=%s",
                                    src)
                    except Exception:
                        logger.debug("voice_outage(src) recovery 发布失败（已忽略）",
                                     exc_info=True)
                self._vo_src_alerted.pop(src, None)
                self._vo_src_last_remind.pop(src, None)
                continue
            if attempts < min_attempts or global_all_dead:
                continue
            alerted = bool(self._vo_src_alerted.get(src))
            last = float(self._vo_src_last_remind.get(src) or 0.0)
            if alerted and ts - last < interval_sec:
                continue
            reasons = {}
            try:
                # 单链的失败原因：全局 fail_reasons 是混在一起的，逐链报错时
                # 混着别的链的原因等于误导（「share_skip」和「hub 超时」是两种活）
                reasons = dict(sorted(
                    (st.get("fail_reasons") or {}).items(),
                    key=lambda kv: (-int(kv[1] or 0), kv[0]))[:3])
            except Exception:
                reasons = {}
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("voice_outage_alert", {
                    "source": src,
                    "attempts": attempts,
                    "window_hours": int(window_hours),
                    "window_truncated": bool(snap.get("truncated")),
                    "effective_window_hours": float(
                        snap.get("effective_window_hours") or window_hours),
                    "top_reasons": reasons,
                    "vram_hosts": self._vram_hosts_for_reasons(reasons),
                    "reminder": alerted,
                    "rate_key": f"voice_outage:src:{src}",
                })
            except Exception:
                logger.debug("voice_outage(src) alert 发布失败（已忽略）",
                             exc_info=True)
                continue
            self._vo_src_alerted[src] = True
            self._vo_src_last_remind[src] = ts
            self.total_voice_outage_alerts += 1
            logger.warning(
                "语音单链断档：source=%s %s 小时窗内 %d 次尝试 0 成功"
                "（原因 Top %s）——其他链正常，所以全局告警不会响",
                src, int(window_hours), attempts, reasons)

    # 入站漏球巡检每轮最多审视的活跃会话数（list_conversations 按活跃度取近段）
    _UNANSWERED_SCAN_LIMIT = 400

    def _check_unanswered_inbound(self, *, now: Optional[float] = None) -> None:
        """「客户说了最后一句、系统既没回也没拟稿」的漏球巡检（P0 2026-08-05）。

        实锤：telegram 客户 22:27 连发两条（含「以后给你介绍做你老公」这类
        高意向社交信号），整晚零出站、零草稿，次日 07:10 只等来一条不接茬的
        通用晨安。现有告警对这形态全部沉默：draft_backlog 只看「草稿行存在」
        的积压、SLA 只看 L3/L4 草稿——**根本没拟稿**的丢球没有任何信号。

        判据（逐条都要满足，零误报优先）：
          - 会话最后一条是**入站**（last_message_dirs 批量口径）；
          - 悬空时长在 [min_age_hours, max_age_hours]（默认 2h..72h——太新给
            拟稿/拟人延迟链留时间，太旧属回访语义不是漏球）；
          - 私聊（chat_type 白名单 + telegram 负 ID 兜底排群）且未归档；
          - 非 bot/业务号（复用 peer_bot_guard 的排除口径）；
          - automation_mode 允许自动拟稿（manual=坐席显式接管，客户在等的是
            人，工作台未读就是它的信号，不进本告警防噪）；
          - 该会话**无 pending 草稿**（有稿未处理是 draft_backlog 的辖区，不双报）。

        聚合外发（EventBus ``unanswered_inbound_alert``，订阅别名
        ``unanswered_inbound``）：默认 ≥1 条即报（本检查的价值就在单条也不放过），
        4h 重提；候选清零补恢复通知（部分消化不发，防谎报）。任何取数异常一律
        静默。配置 ``health_watchdog.unanswered_inbound_remind.{enabled,
        min_age_hours,max_age_hours,min_count,interval_min}``（默认开/2/72/1/240）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        ur = (((cfg.get("health_watchdog") or {}).get("unanswered_inbound_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not ur.get("enabled", True):
            return
        store = self._inbox()
        if store is None or not (hasattr(store, "list_conversations")
                                 and hasattr(store, "last_message_dirs")):
            return
        ts = float(now if now is not None else time.time())
        try:
            min_age_h = float(ur.get("min_age_hours", 2) or 2)
            max_age_h = float(ur.get("max_age_hours", 72) or 72)
            min_count = max(1, int(ur.get("min_count", 1) or 1))
            interval_sec = max(
                600.0, float(ur.get("interval_min", 240) or 240) * 60.0)
        except (TypeError, ValueError):
            return
        try:
            rows = store.list_conversations(
                limit=self._UNANSWERED_SCAN_LIMIT) or []
        except Exception:
            return
        # 第一遍：便宜过滤（类型/年龄窗），把要做批量查询的候选收窄
        cand: List[Dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ct = str(r.get("chat_type") or "").strip().lower()
            if ct and ct not in ("private", "user"):
                continue  # 群/频道/bot 会话不属「客户在等」语义
            cid = str(r.get("conversation_id") or "")
            if not cid:
                continue
            try:
                last_ts = float(r.get("last_ts") or 0.0)
            except (TypeError, ValueError):
                continue
            if last_ts <= 0:
                continue
            age_h = (ts - last_ts) / 3600.0
            if age_h < min_age_h or age_h > max_age_h:
                continue
            if str(r.get("platform") or "") == "telegram":
                try:
                    if int(str(r.get("chat_key") or "")) < 0:
                        continue  # 负 ID=群/频道（chat_type 缺失时兜底）
                except (TypeError, ValueError):
                    pass
            try:
                from src.inbox.peer_bot_guard import proactive_exclude_row
                if proactive_exclude_row(r, cfg if isinstance(cfg, dict) else {}):
                    continue  # bot/接码/业务号：不是真客户在等
            except Exception:
                pass
            cand.append(r)
        stale: List[Dict[str, Any]] = []
        if cand:
            cids = [str(r.get("conversation_id")) for r in cand]
            try:
                dirs = store.last_message_dirs(cids) or {}
            except Exception:
                return  # 末条方向查不到＝没法判，宁可漏报不误报
            tags: Dict[str, Any] = {}
            if hasattr(store, "list_conv_tags_map"):
                try:
                    tags = store.list_conv_tags_map(cids) or {}
                except Exception:
                    tags = {}
            pending_cids = self._pending_draft_conversations()
            for r in cand:
                cid = str(r.get("conversation_id"))
                if str((dirs.get(cid) or {}).get("direction") or "") != "in":
                    continue  # 最后一条是我方发的 → 球不在我们这边
                if bool((tags.get(cid) or {}).get("archived")):
                    continue  # 归档会话由 buried_conv 巡检负责
                if cid in pending_cids:
                    continue  # 有稿在队 → draft_backlog 的辖区，不双报
                if not self._automation_allows_autodraft(cid, cfg):
                    continue  # manual=坐席显式接管，不属机器漏球
                try:
                    age_h = (ts - float(r.get("last_ts") or 0.0)) / 3600.0
                except (TypeError, ValueError):
                    age_h = 0.0
                stale.append({
                    "conversation_id": cid,
                    "platform": str(r.get("platform") or ""),
                    "account_id": str(r.get("account_id") or ""),
                    "age_hours": round(age_h, 1),
                })
        count = len(stale)
        if count < min_count:
            # 只有**清零**才算恢复（部分消化不发恢复，防谎报「已处理完」）
            if count == 0 and self._remind.resolve(self._RK_UNANSWERED):
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("unanswered_inbound_alert", {
                        "recovered": True,
                        "rate_key": "unanswered_inbound:recovered",
                    })
                    logger.info("HealthWatchdog 发出入站漏球恢复通知")
                except Exception:
                    logger.debug(
                        "unanswered_inbound recovery 发布失败（已忽略）",
                        exc_info=True)
            return
        # 内容指纹 = 还在等的会话 id 集合：同样那几个会话 → 一天最多提一次；新会话掉球立刻提
        from src.inbox.remind_ledger import HOLD as _RL_HOLD, REMIND as _RL_REMIND, fingerprint as _rl_fp
        fp = _rl_fp(sorted(str(s.get("conversation_id") or "") for s in stale))
        prev_fp = str(self._remind.get(self._RK_UNANSWERED).get("fingerprint") or "")
        # 条件真实成立时刻 = 第 min_count 老的那个会话满 min_age_h 的那一刻
        _ages = sorted((float(s.get("age_hours") or 0.0) for s in stale), reverse=True)
        since = (ts - (_ages[min_count - 1] - min_age_h) * 3600.0) if len(_ages) >= min_count else None
        verdict = self._remind.decide(
            self._RK_UNANSWERED, now=ts, interval_sec=interval_sec, fp=fp, since=since,
            unchanged_interval_sec=self._unchanged_interval_sec(ur, interval_sec, 1440))
        if verdict == _RL_HOLD:
            return
        is_reminder = verdict == _RL_REMIND
        stale.sort(key=lambda s: -float(s.get("age_hours") or 0.0))
        oldest = float(stale[0].get("age_hours") or 0.0)
        samples = stale[:5]
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("unanswered_inbound_alert", {
                "remind_key": self._RK_UNANSWERED,
                "count": count,
                "min_age_hours": min_age_h,
                "oldest_hours": round(oldest, 1),
                "samples": samples,
                "reminder": is_reminder,
                "unchanged": bool(is_reminder and prev_fp and prev_fp == fp),
                "since_ts": self._remind.first_seen(self._RK_UNANSWERED),
                # 独立限流键：首提/重提不与其他事件挤 1h 窗
                "rate_key": "unanswered_inbound:remind",
            })
        except Exception:
            logger.debug("unanswered_inbound alert 发布失败（已忽略）", exc_info=True)
            return
        self._remind.mark_sent(
            self._RK_UNANSWERED, now=ts, fp=fp,
            summary=f"客户消息 {count} 条超 {min_age_h:.0f}h 没人回（最久 {self._fmt_hours(oldest)}）")
        self.total_unanswered_inbound_alerts += 1
        # 日志=本机唯一保证在的持久告警通道（webhook 可能 0 通道），与
        # draft_backlog/lan_gpu 同款落一行供实弹验证与事后追溯。
        logger.warning(
            "入站漏球：%d 个会话最后一条是客户消息、超 %.0fh 无回复且无待审稿"
            "（最老 %.1fh；样例 %s）——拟稿链可能把球掉了",
            count, min_age_h, oldest,
            ", ".join(str(s.get("conversation_id")) for s in samples[:3]))

    def _pending_draft_conversations(self) -> set:
        """当前 pending 草稿覆盖的会话集合（漏球巡检的「有稿」排除口径）。"""
        svc = getattr(getattr(self._app, "state", self._app), "draft_service", None)
        if svc is None or not hasattr(svc, "list_drafts"):
            return set()
        try:
            rows = svc.list_drafts(status="pending", limit=1000) or []
        except Exception:
            return set()
        out: set = set()
        for d in rows:
            k = str((d or {}).get("conversation_id") or "")
            if k:
                out.add(k)
        return out

    def _automation_allows_autodraft(self, cid: str, cfg: Any) -> bool:
        """该会话的 automation_mode 是否属自动拟稿范畴（manual=人接管 → False）。

        判定异常按 True（计入告警）：本机默认档是 auto_ai，判不出更可能是
        瞬时故障——与 draft_backlog「宁可多报不漏报（漏报＝客户真的没人回）」
        同一取向。
        """
        try:
            from src.inbox.automation_mode import resolve_automation_mode
            mode = str(resolve_automation_mode(
                self._inbox(), cid, cfg if isinstance(cfg, dict) else {}) or "")
            return mode != "manual"
        except Exception:
            return True

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
        配置 ``health_watchdog.lan_gpu_remind.{enabled,after_min,interval_min}``（服务器
        默认开；桌面客户机默认关，见 ``lan_gpu_remind_enabled``）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        lr = (((cfg.get("health_watchdog") or {}).get("lan_gpu_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not lan_gpu_remind_enabled(cfg if isinstance(cfg, dict) else {}):
            return
        targets = lan_gpu_probe_targets(cfg if isinstance(cfg, dict) else {})
        if not targets:
            return
        ts = float(now if now is not None else time.time())
        after_sec = max(60.0, float(lr.get("after_min", 30) or 30) * 60.0)
        interval_sec = max(600.0, float(lr.get("interval_min", 240) or 240) * 60.0)
        for root in targets:
            st = self._lan_gpu_host_state(root)
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
                self._lan_gpu_persist(root, st)
                continue
            if not st["down_since"]:
                st["down_since"] = ts    # 首见不可达：只记时点，给抖动一个窗口
                self._lan_gpu_persist(root, st)
                continue
            down_sec = ts - st["down_since"]
            # 指纹 = 错误形态：默认形态不变仍按 interval 重提（可配 unchanged_interval_min 放宽）
            from src.inbox.remind_ledger import fingerprint as _rl_fp
            fp = _rl_fp(str(probe.get("error") or "")[:80])
            prev_fp = str(self._remind.meta(self._RK_LAN_GPU + root, "fp", "") or "")
            eff_interval = interval_sec
            if st["alerted"] and prev_fp and prev_fp == fp:
                eff_interval = self._unchanged_interval_sec(lr, interval_sec)
            due = ((not st["alerted"] and down_sec >= after_sec)
                   or (st["alerted"] and ts - st["last_remind"] >= eff_interval))
            # 人工闭嘴（P1.3）：静音期内 / 已认领且错误形态未变 → 不外发（恢复通知不受影响）
            _rk = self._RK_LAN_GPU + root
            if due and (self._remind.is_muted(_rk, now=ts)
                        or str(self._remind.meta(_rk, "acked_fp", "") or "") == fp):
                due = False
            if not due:
                continue
            was_reminder = bool(st["alerted"])
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("lan_gpu_alert", {
                    "remind_key": self._RK_LAN_GPU + root,
                    "host": host, "url": root,
                    "error": str(probe.get("error") or ""),
                    "down_minutes": int(down_sec // 60),
                    "reminder": was_reminder,
                    "unchanged": bool(was_reminder and prev_fp and prev_fp == fp),
                    # 每主机独立限流键：176 与 140 同时出事要各自能报
                    "rate_key": f"lan_gpu:{host}",
                })
            except Exception:
                logger.debug("lan_gpu alert 发布失败（已忽略）", exc_info=True)
                continue
            st["alerted"] = 1.0
            st["last_remind"] = ts
            self._lan_gpu_persist(root, st, fp=fp,
                                  summary=f"LAN GPU 主机 {host} 推理服务不可达")
            self.total_lan_gpu_reminders += 1
            # 日志=本机唯一保证在的持久告警通道（webhook 可能 0 通道、SSE 有白名单
            # 且只对在线页面直播）——与 draft_backlog 同款落一行，实弹验证/事后
            # 追溯都靠它（2026-08-01 首次验收时发现只发总线没落日志的盲区）。
            logger.warning(
                "LAN GPU 主机不可达：%s 已 %d 分钟（嵌入/视觉/兜底 LLM/本地 MT "
                "已静默转移备点，本地冗余归零）%s",
                host, int(down_sec // 60), "（重提）" if was_reminder else "")

    def _check_compute_lanes(self, *, now: Optional[float] = None) -> None:
        """三路算力（173 / DeepSeek / 硅基）欠费或故障：立刻报运维群，每 3 分钟催一次。

        停催条件只有两个：运维群卡片点「已处理 / 静音」，或探活恢复（发恢复通知）。
        内容不变也按 3 分钟重提——这是费用/算力事故，不是草稿积压那种「一天一次」。
        """
        from src.ai import compute_lanes as cl
        cfg = getattr(self._config_manager, "config", None) or {}
        if not cl.remind_enabled(cfg if isinstance(cfg, dict) else {}):
            return
        ts = float(now if now is not None else time.time())
        if now is None and self._last_compute_lane_ts and (ts - self._last_compute_lane_ts) < 45.0:
            return
        self._last_compute_lane_ts = ts
        interval = cl.nag_interval_sec(cfg if isinstance(cfg, dict) else {})
        for lane in cl.LANES:
            try:
                result = cl.probe_lane(lane, cfg if isinstance(cfg, dict) else {})
            except Exception:
                result = {"ok": False, "kind": "other", "detail": "探针异常"}
            cl.apply_probe(lane, result, now=ts)
            key = cl.remind_key(lane)
            if result.get("ok"):
                if self._remind.resolve(key):
                    try:
                        from src.integrations.shared.event_bus import get_event_bus
                        get_event_bus().publish("compute_lane_alert", {
                            "recovered": True,
                            "lane": lane,
                            "label": cl.LANE_LABELS.get(lane, lane),
                            "rate_key": f"compute_lane:{lane}:recovered",
                        })
                        logger.info("算力三路已恢复：%s", cl.LANE_LABELS.get(lane, lane))
                    except Exception:
                        logger.debug("compute_lane recovery 发布失败（已忽略）", exc_info=True)
                continue
            kind = str(result.get("kind") or "other")
            from src.inbox.remind_ledger import fingerprint as _rl_fp
            fp = _rl_fp(lane, kind, str(result.get("detail") or "")[:80])
            decision = self._remind.decide(
                key, now=ts, after_sec=0.0, interval_sec=interval, fp=fp,
                unchanged_interval_sec=interval,
            )
            if decision == "hold":
                continue
            standins = [x for x in cl.healthy_labels(now=ts)
                        if x != cl.LANE_LABELS.get(lane, lane)]
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("compute_lane_alert", {
                    "remind_key": key,
                    "lane": lane,
                    "label": cl.LANE_LABELS.get(lane, lane),
                    "kind": kind,
                    "kind_zh": cl.kind_zh(kind),
                    "detail": str(result.get("detail") or "")[:160],
                    "standins": standins,
                    "down_minutes": int(self._remind.active_seconds(key, now=ts) // 60),
                    "reminder": decision == "remind",
                    "unchanged": decision == "remind",
                    "rate_key": f"compute_lane:{lane}:{int(ts // interval)}",
                })
            except Exception:
                logger.debug("compute_lane alert 发布失败（已忽略）", exc_info=True)
                continue
            self._remind.mark_sent(
                key, now=ts, fp=fp,
                summary=f"{cl.LANE_LABELS.get(lane, lane)} {cl.kind_zh(kind)}")
            self.total_compute_lane_reminders += 1
            logger.warning(
                "算力三路异常：%s %s（%s）standins=%s%s",
                cl.LANE_LABELS.get(lane, lane), cl.kind_zh(kind),
                str(result.get("detail") or "")[:80],
                "、".join(standins) or "无",
                "（重提）" if decision == "remind" else "")

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

    def _probe_local_primary(self, base_url: str, timeout_sec: float) -> bool:
        """GET ``<base>/v1/models`` 探本地主链（vLLM/OpenAI 兼容）。True=活。"""
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            return False
        if not base.endswith("/v1"):
            base = base + "/v1"
        try:
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(base + "/models")
            with opener.open(req, timeout=max(1.0, float(timeout_sec))) as r:
                return int(getattr(r, "status", 0) or 0) == 200
        except Exception:
            return False

    def _check_true_probes(self, *, now: Optional[float] = None) -> None:
        """四域真活探针（2026-08-17 无兜底纪律规则 8）。

        对 tts/translate/vision/asr 周期性**真干活**（真合成含 magic bytes 校验 /
        真翻译 / 真识图 / 真转写），连续 ``fail_strikes``（默认 2）次失败 →
        notify_host（主机弹窗 + EventBus host_alert → webhook + ERROR 日志），
        恢复补绿窗。health 200 不算活——8/16 index worker CPU 爬行全天、健康
        探针全绿零告警的机制化收口。

        配置 ``health_watchdog.true_probe.{enabled,interval_min,fail_strikes}``
        （example 默认关——探针打真推理，Lite/无 LAN 引擎的部署会假阳；
        本机 overlay 开）。tick 在线程池里跑，阻塞 HTTP 安全；四域串行、
        单域超时 tts 90s（须大于 hub 内部换引擎重试预算，否则探针放弃后
        在途请求会让 hub 把副本判「挂死」并规避——探针反伤业务，
        见 true_probe._TTS_PROBE_TIMEOUT_DEFAULT）/ 其余 ≤45s，
        interval_min 控制真实探测频率（默认 10min）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        tp = ((cfg.get("health_watchdog") or {}).get("true_probe")) or {}
        if not isinstance(tp, dict) or not tp.get("enabled", False):
            return
        ts = float(now if now is not None else time.time())
        interval_sec = max(120.0, float(tp.get("interval_min", 10) or 10) * 60.0)
        if ts - getattr(self, "_tp_last_run", 0.0) < interval_sec:
            return
        self._tp_last_run = ts
        fail_strikes = max(1, int(tp.get("fail_strikes", 2) or 2))
        # 滞回（2026-09-10）：连败还要持续 min_fail_min（默认 15min）才首报；恢复要连续
        # recover_oks（默认 2）次成功才补绿窗——吸掉「冷加载超时一轮、下一轮就好」的抖动对。
        try:
            min_fail_sec = max(0.0, float(tp.get("min_fail_min", 15) or 0) * 60.0)
        except (TypeError, ValueError):
            min_fail_sec = 900.0
        try:
            recover_oks = max(1, int(tp.get("recover_oks", 2) or 1))
        except (TypeError, ValueError):
            recover_oks = 2
        try:
            from src.ops.true_probe import (
                build_probe_specs,
                next_strike_state,
                probe_gap_reasons,
                restore_strike_state,
                run_probe,
                state_path,
                write_state,
            )
        except Exception:
            return
        specs = build_probe_specs(cfg)
        if not specs:
            return
        from pathlib import Path as _TPPath  # 本模块无顶层 pathlib（同 _check_identity_shadow）
        _tp_dir = _TPPath(str(getattr(self._config_manager, "config_path", "")
                              or "config/config.yaml")).parent
        # 冷启动从状态文件恢复连败态（2026-08-27：识图红了一整天，三次重启把内存
        # alerted 清零 → 同一个问题弹了三次窗）。太旧的状态由 restore 自己丢弃。
        state = getattr(self, "_tp_state", None)
        if state is None:
            state = restore_strike_state(state_path(_tp_dir), now=ts)
        _round: List[str] = []
        _obs: Dict[str, Dict[str, Any]] = {}
        for spec in specs:
            domain = str(spec.get("domain") or "")
            ok, detail = run_probe(spec)
            _round.append(f"{domain}={'ok' if ok else 'FAIL'}")
            _obs[domain] = {"ok": ok, "detail": detail,
                            "url": str(spec.get("url") or ""),
                            "alerting": bool(spec.get("alert", True))}
            state, action = next_strike_state(
                state, domain, ok, fail_strikes=fail_strikes, now=ts,
                min_fail_sec=min_fail_sec, recover_oks=recover_oks)
            # 备胎类规格（vision_backup*）只观测不弹窗：它坏了不影响当下出话，
            # 半夜弹窗是纯噪音；连败仍记进状态文件，切主之前看板上就能看见。
            if not spec.get("alert", True):
                action = ""
            if ok:
                logger.debug("[true_probe] %s ok (%s)", domain, detail)
            else:
                logger.warning("[true_probe] %s FAIL (%s) strike=%s",
                               domain, detail,
                               (state.get(domain) or {}).get("fails"))
            if action == "alert":
                try:
                    from src.utils.host_alert import notify_host
                    _d = state.get(domain) or {}
                    _lasted_min = int(max(0.0, ts - float(_d.get("first_fail") or ts)) // 60)
                    notify_host(
                        f"真活探针失败｜{domain}",
                        (f"{domain} 连续 {int(_d.get('fails') or fail_strikes)} 次、"
                         f"持续 {_lasted_min} 分钟真活探针失败"
                         f"（{detail}）。该域链路按无兜底纪律将拒发并弹窗，"
                         f"请立即检查对应引擎。"),
                        key=f"probe:{domain}", cooldown_sec=600.0)
                except Exception:
                    logger.debug("[true_probe] 告警外发失败", exc_info=True)
            elif action == "recovered":
                try:
                    from src.utils.host_alert import notify_host
                    notify_host(
                        f"真活探针恢复｜{domain}",
                        f"{domain} 真活探针已恢复正常（{detail}）。",
                        key=f"probe_ok:{domain}", cooldown_sec=60.0)
                except Exception:
                    logger.debug("[true_probe] 恢复通知失败", exc_info=True)
        self._tp_state = state
        # 状态落盘＝跨重启去抖基准 + 唯一观测数据源（/api/workspace/metrics 的
        # true_probe 段读它，绝不在 web 请求里现场探针）。写失败不影响本轮结论。
        try:
            write_state(state_path(_tp_dir), state, observations=_obs,
                        gaps=probe_gap_reasons(cfg), now=ts)
        except Exception:
            logger.debug("[true_probe] 状态落盘异常（已忽略）", exc_info=True)
        # 轮次摘要恒 INFO：探针本身也要可观测——全 ok 时若只有 DEBUG，
        # 运维无法从 INFO 日志区分「都健康」与「探针根本没跑」（首夜实测盲区）。
        logger.info("[true_probe] 轮次完成 %s", " ".join(_round))

    def _check_probe_stalled(self, *, now: Optional[float] = None) -> None:
        """真活探针**自己**停摆时外发（2026-08-27 事故的自省闭环）。

        兜底机制必须自己有兜底：当天一个漏掉的局部 import 让 `_check_true_probes`
        整段抛异常，被 tick 的 except 吞掉，探针从重启起不跑而日志零正面痕迹。本检查
        独立成一条（那个方法整段挂掉时它照样跑），判据＝状态文件**存在且陈旧**
        （见 true_probe.stalled_verdict：从没跑过刻意不报，防「未启用」误报）。

        随 `health_watchdog.true_probe.enabled` 生效；恢复自动清零并补绿窗。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        tp = ((cfg.get("health_watchdog") or {}).get("true_probe")) or {}
        if not isinstance(tp, dict) or not tp.get("enabled", False):
            return
        try:
            from pathlib import Path as _PSPath

            from src.ops.true_probe import stalled_verdict
        except Exception:
            return
        ts = float(now if now is not None else time.time())
        cfg_dir = _PSPath(str(getattr(self._config_manager, "config_path", "")
                              or "config/config.yaml")).parent
        verdict = stalled_verdict(
            cfg_dir, interval_min=float(tp.get("interval_min", 10) or 10), now=ts)
        alerted = bool(getattr(self, "_probe_stall_alerted", False))
        if verdict is None:
            if alerted:
                self._probe_stall_alerted = False
                try:
                    from src.utils.host_alert import notify_host
                    notify_host("真活探针已恢复运行",
                                "真活探针恢复按周期落状态文件，探针链路已复活。",
                                key="probe_stalled_ok", cooldown_sec=60.0)
                except Exception:
                    logger.debug("[true_probe] 停摆恢复通知失败", exc_info=True)
            return
        logger.warning("[true_probe] 探针停摆：状态文件 %s 秒未更新（阈值 %s 秒，"
                       "最后一轮 %s）", verdict["stale_sec"], verdict["threshold_sec"],
                       verdict["updated_at"])
        if alerted:
            return
        self._probe_stall_alerted = True
        try:
            from src.utils.host_alert import notify_host
            notify_host(
                "真活探针停摆",
                (f"真活探针已 {verdict['stale_sec'] // 60} 分钟没有落状态文件"
                 f"（最后一轮 {verdict['updated_at']}）。四域链路当前**无人巡检**，"
                 f"请查 app 日志里的「真活探针巡检异常」并用 "
                 f"tools/true_probe_selfcheck.py 手动补位。"),
                key="probe_stalled", cooldown_sec=1800.0)
        except Exception:
            logger.debug("[true_probe] 停摆告警外发失败", exc_info=True)

    def _check_ai_primary_guard(self, *, now: Optional[float] = None) -> None:
        """本地主链保险（2026-08-15，`REPLY_from_zhongshu_20260815` 约定的我方半边）。

        语义：``ai.primary ∈ {local, local_only}`` 时探测本地主链端点
        （``ai.fallback.base_url`` 的 /v1/models）；同 tick 双探皆失败计一次 fail，
        连续 ``fail_streak``（默认 2）次且首败距今 ≥ ``min_span_sec``（默认 240s）
        → 写 overlay ``ai.primary=cloud``（set_overlay_flag 保注释）+ 尽力热重建
        AI 运行时 + EventBus ``ai_primary_guard_alert``。**单向只降不升**：切回
        local_only 归中枢执行器（起 :8001+暖机后切回）；端点恢复只发一次恢复
        通知提示可切回，绝不自动升。与执行器轮换不打架：他们停 :8001 **前**已把
        智聊切 cloud → 本检查在 cloud 档天然不动作。fail-open：任何内部异常绝不
        改配置。配置 ``health_watchdog.ai_primary_guard.{enabled,fail_streak,
        min_span_sec,probe_timeout_sec}``（默认 开/2/240/4）。

        **老板锁（``ai.primary_lock``）优先（2026-09-17 R88 解锁后补）**：锁值为
        ``local``/``local_only`` 时本保险**不再写 ``ai.primary=cloud``**——写了也会
        在 AIClient 装载点被锁强制打回并发「越权改档」告警，只会在运维群制造
        「已热切 cloud → 锁强制纠正」的成对噪音，让人误以为主链仍在 cloud 体制。
        锁在场时达到触发阈值只发 ``kind=probe_fail_locked`` 告警（端点连败、档位
        未动、请查 173），端点恢复发一次 ``kind=probe_recovered_locked``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        gcfg = ((cfg.get("health_watchdog") or {}).get("ai_primary_guard")) or {}
        if not gcfg.get("enabled", True):
            return
        ai_cfg = cfg.get("ai") or {}
        primary = str(ai_cfg.get("primary") or "cloud").strip().lower()
        fb = ai_cfg.get("fallback") or {}
        base_url = str(fb.get("base_url") or "").strip()
        ts = float(now if now is not None else time.time())
        probe_to = float(gcfg.get("probe_timeout_sec", 4) or 4)
        try:
            from src.ai.ai_primary_audit import resolve_lock as _resolve_lock
            lock = _resolve_lock(ai_cfg)
        except Exception:
            lock = ""
        # 锁在本地档：保险改档必被锁打回 → 只报不切
        lock_holds_local = lock in ("local", "local_only")
        locked_alerted = bool(getattr(self, "_apg_locked_alerted", False))

        if primary not in ("local", "local_only") or not base_url:
            # cloud 档 / 未配本地端点：只负责「已降级后的恢复通知」，其余状态归零
            if (self._apg_switched and base_url
                    and self._probe_local_primary(base_url, probe_to)):
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("ai_primary_guard_alert", {
                        "recovered": True,
                        "base_url": base_url,
                        "lock": lock or None,
                        "rate_key": "ai_primary_guard:recovered",
                    })
                    logger.info(
                        "本地主链已恢复可用（保险早前已切 cloud）——恢复本地档需按 "
                        "ai.primary_lock 治理经治理接口切换")
                except Exception:
                    logger.debug("ai_primary_guard recovery 发布失败（已忽略）",
                                 exc_info=True)
                self._apg_switched = False
            self._apg_fail_count = 0
            self._apg_first_fail_ts = 0.0
            return

        ok = self._probe_local_primary(base_url, probe_to)
        if not ok:
            # 同 tick 复核一次：单次 TCP 抖动/瞬时重启不算失败
            ok = self._probe_local_primary(base_url, probe_to)
        if ok:
            self._apg_fail_count = 0
            self._apg_first_fail_ts = 0.0
            if locked_alerted:
                # 锁在场期间报过「连败未切档」→ 端点回来补一次恢复通知（只一次）
                self._apg_locked_alerted = False
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("ai_primary_guard_alert", {
                        "recovered": True,
                        "kind": "probe_recovered_locked",
                        "base_url": base_url,
                        "lock": lock or None,
                        "effective": primary,
                        "rate_key": "ai_primary_guard:recovered",
                    })
                    logger.info("本地主链端点已恢复可达（锁 %s 在场，档位 %s 未曾变动）",
                                lock, primary)
                except Exception:
                    logger.debug("ai_primary_guard locked-recovery 发布失败（已忽略）",
                                 exc_info=True)
            return

        self._apg_fail_count += 1
        if self._apg_first_fail_ts <= 0.0:
            self._apg_first_fail_ts = ts
        streak_need = max(1, int(gcfg.get("fail_streak", 2) or 2))
        span_need = max(0.0, float(gcfg.get("min_span_sec", 240) or 240))
        if (self._apg_fail_count < streak_need
                or (ts - self._apg_first_fail_ts) < span_need):
            return

        if lock_holds_local:
            # 锁在本地档：不写配置、不热重建；只报一次「端点连败、档位未动」
            if locked_alerted:
                return
            self._apg_locked_alerted = True
            down_min = int((ts - self._apg_first_fail_ts) // 60)
            try:
                from src.ai.ai_primary_audit import append_event as _pa_append
                _pa_append(
                    "guard_probe_fail_locked", mode=primary, lock=lock,
                    base_url=base_url, fail_count=int(self._apg_fail_count),
                    via="health_watchdog")
            except Exception:
                pass
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("ai_primary_guard_alert", {
                    "kind": "probe_fail_locked",
                    "from_mode": primary,
                    "lock": lock,
                    "base_url": base_url,
                    "fail_count": int(self._apg_fail_count),
                    "down_minutes": down_min,
                    "rate_key": "ai_primary_guard:probe_fail_locked",
                })
            except Exception:
                logger.debug("ai_primary_guard locked alert 发布失败（已忽略）",
                             exc_info=True)
            logger.warning(
                "本地主链端点 %s 连续 %d 次探测失败（档位 %s，锁 %s 在场）→ 不自动切 cloud"
                "（锁会打回；要降级需老板改 ai.primary_lock），请查 173 vLLM :8001",
                base_url, self._apg_fail_count, primary, lock)
            return

        cm = self._config_manager
        if cm is None or not hasattr(cm, "set_overlay_flag"):
            return
        try:
            ok_w, msg = cm.set_overlay_flag("ai.primary", "cloud")
        except Exception:
            logger.warning("ai_primary_guard 写 overlay 异常（本轮放弃）", exc_info=True)
            return
        if not ok_w:
            logger.warning("ai_primary_guard 写 overlay 失败：%s", msg)
            return
        # 尽力热重建（失败不致命：config 热重载 ~30s 也会把 cloud 档装进活体）
        try:
            import asyncio as _aio
            loop = _aio.get_running_loop()
            from src.web.routes.unified_inbox_setup_routes import reload_ai_runtime
            loop.create_task(reload_ai_runtime(self._app, cm))
        except Exception:
            logger.debug("ai_primary_guard 热重建调度失败（等待配置热重载兜底）",
                         exc_info=True)
        down_min = int((ts - self._apg_first_fail_ts) // 60)
        # 切换审计（2026-08-22）：保险自动切档同样留痕——「谁改的」永远可查
        try:
            from src.ai.ai_primary_audit import append_event as _pa_append
            _pa_append(
                "guard_auto_cloud", mode_from=primary, mode_to="cloud",
                base_url=base_url, fail_count=int(self._apg_fail_count),
                via="health_watchdog")
        except Exception:
            pass
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ai_primary_guard_alert", {
                "from_mode": primary,
                "base_url": base_url,
                "lock": lock or None,
                "fail_count": int(self._apg_fail_count),
                "down_minutes": down_min,
                "rate_key": "ai_primary_guard:switched",
            })
        except Exception:
            logger.debug("ai_primary_guard alert 发布失败（已忽略）", exc_info=True)
        logger.warning(
            "本地主链保险触发：%s 连续 %d 次探测失败（原档 %s）→ ai.primary 已切 cloud"
            "（单向降级保服务；主链档位由 ai.primary_lock 治理，勿自动切回）",
            base_url, self._apg_fail_count, primary)
        self._apg_switched = True
        self._apg_fail_count = 0
        self._apg_first_fail_ts = 0.0
        self.total_ai_primary_guard_switches += 1

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

    def _check_tg_history_autosync(self, *, now: Optional[float] = None) -> None:
        """Telegram 历史自动补缺口（2026-08-02）：到期账号自动触发账号级云端同步。

        配置 ``inbox.tg_history_autosync.{enabled,interval_hours,dialogs,per_chat,
        max_accounts_per_tick}``（默认关——新子系统约定，生产经 overlay 开）。
        本检查仅做 15min 节流 + 开关判定；到期挑选/冷却/单飞/client 把关全部在
        ``maybe_autostart_tg_history_sync`` 里（与手动同步共用同一触发核心）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        acfg = ((cfg.get("inbox") or {}).get("tg_history_autosync") or {}) \
            if isinstance(cfg, dict) else {}
        if not acfg.get("enabled", False):
            return
        ts = float(now if now is not None else time.time())
        if self._last_tg_autosync_ts and (ts - self._last_tg_autosync_ts) < 900.0:
            return
        self._last_tg_autosync_ts = ts
        if self._app is None:
            return
        from src.web.routes.unified_inbox_account_routes import (
            maybe_autostart_tg_history_sync,
        )
        started = maybe_autostart_tg_history_sync(self._app, cfg)
        if started:
            self.total_tg_autosyncs += len(started)

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

    def _check_goal_sprint_liveness(self, *, now: Optional[float] = None) -> None:
        """自动推进有货零真发（D1b P0-5，2026-09-05）。

        与 ``_check_scan_loop_stall`` 正交：那个盯 ticker/dispatcher **心跳停走**；
        这个盯「配置能发 + 库里有够老的 auto 目标 + 24h 零 ``care_sent`` /
        ``beat_sent``」——线程活着但一条都没出去（会话全是人审档 / 排拍后被吞 /
        代码没装载）。引擎没开、目标太新、已经有真发 → 静默。
        配置 ``health_watchdog.goal_sprint_liveness.{enabled,min_active,
        min_age_hours,interval_min,unchanged_interval_min}``（默认开 / 2 / 4 /
        240 / 1440）。

        2026-09-16：节流位迁 ``remind_ledger``（跨重启不重发）；结构性
        ``beat_blocked`` 已在 liveness 层剔除。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return
        lr = ((cfg.get("health_watchdog") or {}).get("goal_sprint_liveness")
              or {})
        if not isinstance(lr, dict):
            lr = {}
        if not bool(lr.get("enabled", True)):
            return
        from src.companion.goals.service import (
            get_configured_store,
            sprint_engine_status,
        )
        from src.companion.goals.liveness import (
            collect_send_liveness,
            stall_verdict,
        )
        es = sprint_engine_status(cfg)
        if not es.get("sprint_effective"):
            return
        ts = float(now if now is not None else time.time())
        try:
            min_active = int(lr.get("min_active", 2) or 2)
        except (TypeError, ValueError):
            min_active = 2
        try:
            min_age_h = float(lr.get("min_age_hours", 4) or 4)
        except (TypeError, ValueError):
            min_age_h = 4.0
        try:
            interval_min = float(lr.get("interval_min", 240) or 240)
        except (TypeError, ValueError):
            interval_min = 240.0
        interval_sec = max(60.0, interval_min * 60.0)
        store = get_configured_store(
            cfg, getattr(self._config_manager, "config_path", None))
        snap = collect_send_liveness(store, now=ts)
        # M-7 D（#236）：单目标粒度点名——全局判据要 ≥2 个 auto 目标才响，BABY BEAR
        # 一个目标 4 天 0 拍永远够不着它。liveness.goal_stall_verdict 已逐目标判过
        # （建了 ≥24h、期限未到、近 24h 零 beat_sent），这里按目标节流落 INFO + 事件
        # （带 goal_id / 会话 / 标题），卡片同步红字（sprint_live.stalled）。
        try:
            self._note_goal_stalled(snap.get("stalled_goals") or [], now=ts,
                                    interval_min=interval_min, block=lr)
        except Exception:
            logger.debug("goal stalled 点名异常（已忽略）", exc_info=True)
        kind = stall_verdict(
            es, snap, min_active=min_active,
            min_age_sec=max(0.0, min_age_h) * 3600.0)
        rk = self._RK_GOAL_SENDS
        from src.inbox.remind_ledger import (
            HOLD as _RL_HOLD, REMIND as _RL_REMIND, fingerprint as _rl_fp,
        )
        if not kind:
            if self._remind.resolve(rk):
                logger.info(
                    "goal_sprint_liveness recovered: auto=%s sent_24h=%s "
                    "structural=%s",
                    snap.get("active_auto"), snap.get("sent_24h"),
                    snap.get("structural_blocked"))
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("scan_loop_stall_alert", {
                        "loop": "goal_sprint_sends",
                        "recovered": True,
                        "remind_key": rk,
                        "rate_key": "scan_stall:goal_sprint_sends:recovered",
                    })
                except Exception:
                    logger.debug("goal_sprint_liveness recovery 发布失败",
                                 exc_info=True)
            return
        stalled_ids = sorted(
            str(s.get("goal_id") or "")
            for s in (snap.get("stalled_goals") or [])
            if isinstance(s, dict) and s.get("goal_id"))
        fp = _rl_fp(stalled_ids or ["fleet"])
        verdict = self._remind.decide(
            rk, now=ts, interval_sec=interval_sec, fp=fp,
            unchanged_interval_sec=self._unchanged_interval_sec(
                lr, interval_sec, 1440))
        if verdict == _RL_HOLD:
            return
        self.total_goal_sprint_stall_alerts += 1
        logger.info(
            "goal_sprint_liveness stalled: auto=%s sent_24h=%s oldest_h=%.1f "
            "structural=%s ——引擎能发但 24h 零真发，卡片可能仍写着下一拍",
            snap.get("active_auto"), snap.get("sent_24h"),
            float(snap.get("oldest_age_sec") or 0) / 3600.0,
            snap.get("structural_blocked"))
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("scan_loop_stall_alert", {
                "loop": "goal_sprint_sends",
                "active_auto": snap.get("active_auto"),
                "sent_24h": snap.get("sent_24h"),
                "structural_blocked": snap.get("structural_blocked"),
                "oldest_hours": round(
                    float(snap.get("oldest_age_sec") or 0) / 3600.0, 1),
                "reminder": verdict == _RL_REMIND,
                "remind_key": rk,
                "rate_key": "scan_stall:goal_sprint_sends",
            })
        except Exception:
            logger.debug("goal_sprint_liveness 告警发布失败", exc_info=True)
            return
        self._remind.mark_sent(
            rk, now=ts, fp=fp,
            summary=(f"自动推进有货零真发 auto={snap.get('active_auto')} "
                     f"sent_24h={snap.get('sent_24h')}"))

    def _note_goal_stalled(self, stalled: list, *, now: float,
                           interval_min: float = 240.0,
                           block: Optional[Dict[str, Any]] = None) -> None:
        """单目标 stalled 点名（M-7 D #236）：每目标一条 INFO + 一条
        ``scan_loop_stall_alert``（loop=goal_sprint_goal，带 goal_id/会话/标题），
        按目标经 ``remind_ledger`` 节流（跨重启）；目标恢复时清账本并发 recovered。"""
        from src.inbox.remind_ledger import (
            HOLD as _RL_HOLD, REMIND as _RL_REMIND, fingerprint as _rl_fp,
        )
        interval_sec = max(60.0, float(interval_min) * 60.0)
        br = block if isinstance(block, dict) else {}
        unchanged = self._unchanged_interval_sec(br, interval_sec, 1440)
        prefix = self._RK_GOAL_STALL
        cur = {str(s.get("goal_id") or ""): s for s in (stalled or [])
               if isinstance(s, dict) and s.get("goal_id")}
        # 恢复：账本里点过名、这次不在名单 → resolve + recovered
        for key in list(self._remind.keys()):
            if not str(key).startswith(prefix):
                continue
            gid = str(key)[len(prefix):]
            if gid in cur:
                continue
            if self._remind.resolve(key):
                logger.info("goal_sprint_liveness goal_recovered: goal=%s", gid)
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("scan_loop_stall_alert", {
                        "loop": "goal_sprint_goal",
                        "goal_id": gid,
                        "recovered": True,
                        "remind_key": key,
                        "rate_key": f"scan_stall:goal:{gid}:recovered",
                    })
                except Exception:
                    logger.debug("goal_stalled recovery 发布失败", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            bus = get_event_bus()
        except Exception:
            bus = None
        for gid, s in cur.items():
            key = f"{prefix}{gid}"
            fp = _rl_fp(gid, str(s.get("conversation_id") or ""))
            verdict = self._remind.decide(
                key, now=now, interval_sec=interval_sec, fp=fp,
                unchanged_interval_sec=unchanged)
            if verdict == _RL_HOLD:
                continue
            conv = str(s.get("conversation_id") or "")
            title = str(s.get("title") or "")
            logger.info(
                "goal_sprint_liveness goal_stalled: goal=%s conv=%s title=%r "
                "——有排期但 24h 零真发，卡片已标红",
                gid, conv, title[:30])
            if bus is not None:
                try:
                    bus.publish("scan_loop_stall_alert", {
                        "loop": "goal_sprint_goal",
                        "goal_id": gid,
                        "conversation_id": conv,
                        "title": title,
                        "reminder": verdict == _RL_REMIND,
                        "remind_key": key,
                        "rate_key": f"scan_stall:goal:{gid}",
                    })
                except Exception:
                    logger.debug("goal_stalled 告警发布失败", exc_info=True)
                    continue
            self._remind.mark_sent(
                key, now=now, fp=fp,
                summary=f"目标 {gid[:12]} 24h 零真发（{title[:20]}）")
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

    def _check_token_wallet(self, *, now: Optional[float] = None) -> None:
        """Token 钱包水位巡检（quotawall v2 P2-4）：耗尽点名、临近提醒、回充报平安。

        耗尽判定**同源复用** ``quota_state.resolve_quota_state`` 的 tok 裁决
        （enabled+enforce+funded+balance<=0 才算 tok_out——funded 守卫防「从未
        注资的部署被谎报用尽」，与前端额度墙 tok 变体同一判据，绝不各算一套）。
        影子记账（enforce=False）期间余额只是观测数字，不告警——那个阶段的观测
        面是 ops 影子卡；临近提醒只在有月度含量分母时做（消耗 ≥ warn_pct%，即
        balance <= monthly*(100-warn_pct)%），纯充值包部署（monthly=0）没有稳定
        分母，不做百分比预警防误报。配置
        ``health_watchdog.token_wallet_remind.{enabled,warn_pct,interval_min}``
        （默认开/85/360，与 quota_remind 同族——未启用 token_ledger 的部署
        wallet.enabled=False 天然静默，零误报零开销）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        tw = (((cfg.get("health_watchdog") or {}).get("token_wallet_remind"))
              or {}) if isinstance(cfg, dict) else {}
        if not tw.get("enabled", True):
            return
        ts = float(now if now is not None else time.time())
        if self._last_token_wallet_ts and (ts - self._last_token_wallet_ts) < 600.0:
            return  # 与授权额度巡检同款 10min 稀疏节流
        self._last_token_wallet_ts = ts
        from src.licensing.token_ledger import wallet_snapshot
        w = wallet_snapshot()
        if not w.get("enabled") or not w.get("enforce"):
            self._token_wallet_alerted = False
            return
        from src.licensing.quota_state import resolve_quota_state
        tok = resolve_quota_state(quota=None, level="ok", wallet=w)["tok"]
        balance = int(tok.get("balance") or 0)
        monthly = int(tok.get("monthly") or 0)
        warn_pct = float(tw.get("warn_pct", 85) or 85)
        remind_sec = max(600.0, float(tw.get("interval_min", 360) or 360) * 60.0)
        near = bool(
            monthly > 0
            and balance <= monthly * max(0.0, 100.0 - warn_pct) / 100.0
        )
        from src.utils.host_alert import notify_host
        if tok.get("out"):
            if notify_host(
                "Token 钱包已耗尽",
                (f"Token 钱包余额已用尽（本月含量 {monthly:,}）。AI 增强功能已"
                 "自动降级为免费路径（服务不断线，体验降档）。请到 会员中心 → "
                 "Token 钱包 充值，或等待下月含量补发。"),
                key="token_wallet:exceeded",
                cooldown_sec=remind_sec,
            ):
                self.total_token_wallet_alerts += 1
            self._token_wallet_alerted = True
        elif near:
            pct_left = balance * 100.0 / monthly
            if notify_host(
                "Token 钱包即将耗尽",
                (f"Token 钱包余额仅剩 {balance:,}（本月含量 {monthly:,} 的 "
                 f"{pct_left:.0f}%）。耗尽后 AI 增强功能将降级为免费路径，"
                 "建议提前到 会员中心 充值。"),
                key="token_wallet:warn",
                cooldown_sec=remind_sec,
            ):
                self.total_token_wallet_alerts += 1
            self._token_wallet_alerted = True
        elif self._token_wallet_alerted:
            notify_host(
                "Token 钱包已恢复",
                f"Token 钱包余额回升至 {balance:,}。充值/月度补发已生效。",
                key="token_wallet:recover",
                cooldown_sec=0.0,
            )
            self._token_wallet_alerted = False

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
        代价只是下次重新起算 after_min）。``ai.primary=local*`` 时本地就是指定主链，
        文案「云端主模型不可用」不成立，整段静默（2026-09-18 误报：本地主链出话被
        当成顶班）。配置
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
            stats = ai.get_stats() or {}
            calls = int(stats.get("local_fallback_calls") or 0)
            primary_mode = str(stats.get("primary_mode") or "cloud").strip().lower()
        except Exception:
            return
        if primary_mode in ("local", "local_only"):
            self._fb_duty_last_calls = calls
            self._fb_duty_since_ts = 0.0
            self._fb_duty_idle_ticks = 0
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
        status: Dict[str, Any] = {}
        try:
            from src.integrations.account_orchestrator import (
                get_orchestrator_if_running,
            )
            from src.utils.ops_overview import orchestrator_worker_problems
            orch = get_orchestrator_if_running()
            if orch is not None:
                status = orch.status() or {}
        except Exception:
            status = {}
        # 实施97 线 B：桥接驱动（个人微信 PC 副驾）心跳过期也是「该账号投递链断了」——并入同一
        # 问题列表走同一告警/恢复/事件表（编排器没起来的部署也要能报，故不再因无编排器早退）
        bridge_problems = self._bridge_driver_problems()
        # 无受管账号且无桥接问题：若此前报过异常，静默 reconcile 掉遗留 open 事件
        if int(status.get("total") or 0) <= 0 and not bridge_problems:
            if self._last_orch_worker_sig:
                self._emit_orchestrator_worker_recovery()
                self._last_orch_worker_sig = None
            return

        problems = (orchestrator_worker_problems(status) if status else []) + bridge_problems
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

    def _bridge_driver_problems(self) -> List[Dict[str, Any]]:
        """桥接驱动心跳过期的账号（注册表 ``meta.bridge_heartbeat``）；读不到一律空表。

        只读**已初始化**的注册表单例（读路径不隐式建库）：单例还没建，说明本进程没见过任何桥接账号。
        """
        try:
            from src.integrations import account_registry as _ar
            reg = getattr(_ar, "_registry", None)
            if reg is None:
                return []
            from src.web.desktop_bridge_presence import bridge_driver_problems
            return bridge_driver_problems(reg.list() or [])
        except Exception:
            logger.debug("桥接驱动心跳巡检失败（已忽略）", exc_info=True)
            return []

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

    def _crisis_bridge(self):
        """危机升级桥（#185）：懒建单例，store 延迟解析（bootstrap 顺序无关）。"""
        br = getattr(self, "_crisis_escalation_bridge", None)
        if br is None:
            from src.companion.wellbeing_escalation_bridge import CrisisEscalationBridge
            br = CrisisEscalationBridge(self._inbox)
            self._crisis_escalation_bridge = br
        return br

    def _check_crisis_escalation_bridge(self) -> None:
        """R8 升级 → 工作台徽标/置顶/案例（#185 D7）。

        - 首 tick 装 ``crisis_event_store`` 落库监听器（推路径，零延迟）；
        - 每 tick 补扫 skill_manager 危机库中水位以上的升级事件（扫路径，
          兜「事件先于监听器」窄窗；首扫只立水位，不追溯历史）。
        随 ``companion.wellbeing.enabled``（默认开）；无 inbox store 时监听器照装
        （事件来时再解析），扫描跳过。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        wb = (((cfg.get("companion") or {}).get("wellbeing") or {})
              if isinstance(cfg, dict) else {})
        if not wb.get("enabled", True):
            return
        bridge = self._crisis_bridge()
        if bridge.install():
            # 首装心跳（一次性 INFO）：审计工具据此判「该巡检活着」
            logger.info("危机升级桥已装载：severe+escalated 事件 → 工作台徽标/置顶/案例")
        sm = self._skill_manager()
        if sm is None:
            tg = getattr(getattr(self._app, "state", self._app), "telegram_client", None)
            sm = getattr(tg, "skill_manager", None) if tg is not None else None
        crisis_store = getattr(sm, "_crisis_store", None) if sm is not None else None
        if crisis_store is None or self._inbox() is None:
            return
        results = bridge.sweep(crisis_store)
        bridged = sum(1 for r in (results or []) if r.get("bridged"))
        # 累计计数（metrics 可读；静默死掉时此数停涨即为信号）
        self.total_crisis_bridge_swept = getattr(self, "total_crisis_bridge_swept", 0) + len(results or [])
        self.total_crisis_bridge_applied = getattr(self, "total_crisis_bridge_applied", 0) + bridged
        if bridged:
            logger.info("危机升级桥补扫：%d 条升级事件已点亮工作台（累计 %d）",
                        bridged, self.total_crisis_bridge_applied)

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

    def _check_persona_retired_conflicts(self, *, now: Optional[float] = None) -> None:
        """撤销设定「复活」巡检（2026-08-04 P2 收尾）。

        档案内容与 ``boundaries.retired_facts`` 锚词打架 = 被运营删除的设定又被
        写回来了（2026-08-02 批量丰富实锤这一类）。Studio 保存路径已有同款落库前
        检测（persona_routes → ``retired_conflicts``），但**直改 profiles_runtime.yaml
        的写入方**（agent 批量丰富 / 运维手改）完全绕过 API——本巡检以进程内存态
        为准（profiles_runtime 热重载会在几秒内把文件编辑拉进内存），关掉最后盲区。

        配置 ``health_watchdog.persona_retired_remind.{enabled,interval_min,remind_min}``
        （默认开 / 60min 一轮 / 24h 重提；告警只减不增行为，enabled 缺省 True 与
        *_remind 家族一致）。去抖＝冲突指纹（persona×锚词×路径）变化立即再报，
        不变按重提间隔；清零且**报过**才补恢复通知（没报过的抖动恢复不发）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        rc = ((cfg.get("health_watchdog") or {}).get("persona_retired_remind")
              or {}) if isinstance(cfg, dict) else {}
        if not rc.get("enabled", True):
            return
        ts = float(now if now is not None else time.time())
        interval = max(5.0, float(rc.get("interval_min", 60) or 60)) * 60.0
        if self._retired_scan_ts and (ts - self._retired_scan_ts) < interval:
            return
        self._retired_scan_ts = ts
        try:
            from src.utils.persona_manager import PersonaManager
            from src.utils.persona_retired import retired_conflicts
            pm = PersonaManager.get_instance()
            try:
                pm.maybe_reload_runtime_profiles()
            except Exception:
                pass
            profiles = dict(getattr(pm, "_profile_personas", {}) or {})
        except Exception:
            logger.debug("撤销设定巡检取档案失败（已忽略）", exc_info=True)
            return
        conflicts: Dict[str, List[Dict[str, str]]] = {}
        for pid, p in profiles.items():
            try:
                hits = retired_conflicts(p)
            except Exception:
                continue
            if hits:
                conflicts[str(pid)] = [
                    {"term": str(h.get("term") or ""),
                     "path": str(h.get("path") or "")}
                    for h in hits[:6]
                ]
        if not conflicts:
            if self._retired_alerted:
                self._emit_persona_retired_recovery()
            self._retired_alerted = False
            self._retired_fp = ""
            self._retired_last_remind = 0.0
            return
        fp = "|".join(
            f"{pid}:{h['term']}@{h['path']}"
            for pid in sorted(conflicts)
            for h in conflicts[pid]
        )
        remind = max(10.0, float(rc.get("remind_min", 1440) or 1440)) * 60.0
        first = not self._retired_alerted
        changed = fp != self._retired_fp
        due = (ts - self._retired_last_remind) >= remind
        if not (first or changed or due):
            return
        self._retired_fp = fp
        self._retired_alerted = True
        self._retired_last_remind = ts
        self.total_persona_retired_alerts += 1
        self._emit_persona_retired_alert(conflicts, reminder=not (first or changed))

    def _emit_persona_retired_alert(
        self, conflicts: Dict[str, List[Dict[str, str]]], *, reminder: bool,
    ) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            total = sum(len(v) for v in conflicts.values())
            get_event_bus().publish("persona_retired_alert", {
                "conflicts": conflicts,
                "personas": sorted(conflicts.keys()),
                "total": int(total),
                "reminder": bool(reminder),
                "rate_key": "persona_retired:remind",
            })
            logger.warning(
                "撤销设定复活告警：%d 个人设 %d 处冲突（被删设定被写回档案）",
                len(conflicts), total)
        except Exception:
            logger.debug("persona_retired 告警发布失败（已忽略）", exc_info=True)

    def _emit_persona_retired_recovery(self) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("persona_retired_alert", {
                "recovered": True,
                "rate_key": "persona_retired:recovered",
            })
            logger.info("HealthWatchdog 发出撤销设定冲突恢复通知")
        except Exception:
            logger.debug("persona_retired recovery 发布失败（已忽略）", exc_info=True)

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
        report = build_ops_report(days=days, incident_stats=cur_inc, roi=cur_roi,
                                  billing=billing, compare=compare)
        # AI 价值总账行（2026-08-06）：与 /api/report/weekly.value / ops「AI 价值周报」卡
        # 同源（src/ops/value_report）。此前 ops_report 推送只有「运维+自动化+计费」，
        # 「AI 本周替你干了什么」（拟稿/触达回复率/出站量）恰好从缺——而 F4 legacy
        # 周报循环虽带价值行，却只走 config.yaml::webhook 旧栈（默认关）；接通推荐
        # 通道（notify_webhooks.json）的运营收到的是本条 ops_report。软失败不阻断主体。
        try:
            from src.ops.value_report import build_weekly_value
            vl = (build_weekly_value(inbox, now=ts) or {}).get("text_lines") or []
            if vl:
                report["value_lines"] = list(vl)
        except Exception:
            logger.debug("ops_report 价值行装配失败（已忽略）", exc_info=True)
        # P4：案例跟进待办行（紧急未结 / 媒体质疑超龄 / 演练残影）——与
        # /api/cases/active.summary 同源纯函数，软失败不阻断周报主体。
        # P7：处置质量行（媒体结案后复发率超阈的分桶）——老板看到的不只是
        # 「处理了多少」，还有「处理得管不管用」。
        try:
            ctx_store = self._case_ctx_store()
            if ctx_store is not None:
                from src.utils.case_center import (
                    case_board_text_lines, collect_case_rows,
                    effectiveness_text_lines, media_resolution_effectiveness,
                    summarize_case_board,
                )
                rows = collect_case_rows(ctx_store, now=ts, include_drill=True)
                clines = case_board_text_lines(summarize_case_board(rows, now=ts))
                clines.extend(effectiveness_text_lines(
                    media_resolution_effectiveness(ctx_store, now=ts)))
                if clines:
                    report.setdefault("value_lines", []).extend(clines)
        except Exception:
            logger.debug("ops_report 案例跟进行装配失败（已忽略）", exc_info=True)
        return report

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

    # ── WP-3 老板日报推送（2026-08-17；数据面=value_report 日窗，通道=ops_report）──

    def _build_daily_report(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """日报数据面：AI 价值日账（build_daily_value）+ 省时头条。

        与周报同通道、``period="daily"`` 标记（formatter 据此换「日报」标题）。
        store 缺席 / 装配失败 / **零流量日**一律 None——半夜刚装的实例天天推
        「全 0」日报＝老板直接屏蔽告警群，空报比没报更伤。
        """
        store = self._inbox()
        if store is None:
            return None
        try:
            from src.ops.value_report import (
                build_daily_value, estimate_saved_minutes,
                resolve_minutes_per_reply)
            v = build_daily_value(store, now=now)
        except Exception:
            logger.debug("日报价值段装配失败（已忽略）", exc_info=True)
            return None
        if not v:
            return None
        today = v.get("today") or {}
        d = today.get("drafts") or {}
        o = today.get("outreach") or {}
        tr = today.get("traffic") or {}
        if not any((int(tr.get("messages_out") or 0),
                    int(tr.get("messages_in") or 0),
                    int(d.get("created") or 0),
                    int(o.get("sent") or 0))):
            return None
        cm = self._config_manager
        cfg = getattr(cm, "config", None)
        if not isinstance(cfg, dict):
            cfg = cm if isinstance(cm, dict) else {}
        headline: List[str] = []
        sent = int(d.get("sent") or 0)
        if sent:
            saved_h = round(
                estimate_saved_minutes(today, resolve_minutes_per_reply(cfg))
                / 60.0, 1)
            headline.append(
                f"AI 经审核发出 {sent} 条回复，折算省约 {saved_h} 小时人工")
        return {
            "period": "daily",
            "days": 1,
            "headline": headline,
            "value_lines": list(v.get("text_lines") or [])[:6],
        }

    def _maybe_daily_report(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self._daily_enabled:
            return None
        ts = float(now if now is not None else time.time())
        if self._last_daily_ts and (ts - self._last_daily_ts) < self._daily_interval:
            return None
        # 节流戳在构建**之前**推进（与周报刻意不同）：零流量日若不推进，
        # 每个 5min 巡检 tick 都会重扫消息表白算一次日账——日报非告警，
        # 偶发失败等明天补班即可，不值得每 tick 重试。
        self._last_daily_ts = ts
        report = self._build_daily_report(now=ts)
        if report is None:
            return None
        self.total_daily_reports += 1
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ops_report", report)
            logger.info("HealthWatchdog 发出运营日报")
        except Exception:
            logger.debug("ops_report(daily) 发布失败（已忽略）", exc_info=True)
        return report

    # ── 每日运维摘要（2026-09-10 运维群降噪 P1.2）──────────────────────────────
    # 慢性积压（待审草稿 / 客户在等 / 案例跟进）与算力黄灯一天重提一遍已经够；
    # 本摘要把「此刻仍未处理的事、开了多久」+ 真活探针 + 昨日花费收成**一张卡**，
    # 固定钟点发，运维早上一眼看完。数据面直接取提醒账本 open_items（各巡检外发时
    # 已登记一行人话），不重跑巡检。配置 ``health_watchdog.daily_digest.{enabled,hour,minute}``
    # （遵循新子系统默认关）；「今天发过没」记在账本里，重启不重发。

    _RK_DIGEST = "_daily_digest"
    _DIGEST_LABELS = (
        ("draft_backlog", "待审草稿"),
        ("case_backlog", "案例跟进"),
        ("unanswered_inbound", "客户在等"),
        ("avatar_voice", "语音服务"),
        ("lan_gpu:", "LAN GPU"),
    )

    def _digest_cfg(self) -> Dict[str, Any]:
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return {}
        blk = (cfg.get("health_watchdog") or {}).get("daily_digest")
        return dict(blk) if isinstance(blk, dict) else {}

    # ── 公网入口可达性（2026-09-10 运维群降噪 P2.2）────────────────────────────
    # 卡片链接拼在渠道 base_url（katie.bd2026.cc）上，那条链（VPS nginx → SSH 隧道 → 本机）
    # 一断，卡照发、链接全 502。**告警**归 deploy/instances/prod_edge_watchdog.ps1（两击 +
    # 自愈拉隧道 + 通知），这里不重复报；只探一下、把状态交给 src.ops.public_link，
    # 让 webhook_notifier 铸链接时换成内网地址并在卡上说明。配置
    # ``health_watchdog.public_link_probe.{enabled,interval_min,strikes}``（默认开——没配
    # base_url 的部署天然零开销）；内网地址 ``web_admin.lan_base_url``，缺省按 web_admin.port 推导。

    def _check_public_link(self, *, now: Optional[float] = None) -> None:
        cfg = getattr(self._config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        blk = (cfg.get("health_watchdog") or {}).get("public_link_probe")
        blk = dict(blk) if isinstance(blk, dict) else {}
        if blk.get("enabled", True) is False:
            return
        ts = float(now if now is not None else time.time())
        try:
            interval_sec = max(60.0, float(blk.get("interval_min", 5) or 5) * 60.0)
        except (TypeError, ValueError):
            interval_sec = 300.0
        if ts - float(getattr(self, "_pl_last_run", 0.0) or 0.0) < interval_sec:
            return
        self._pl_last_run = ts
        from src.integrations.notify_webhooks_store import effective_webhooks
        from src.ops import public_link
        wa = cfg.get("web_admin") or {}
        lan = str(wa.get("lan_base_url") or "").strip()
        if not lan:
            try:
                lan = public_link.lan_base(int(wa.get("port") or 0))
            except (TypeError, ValueError):
                lan = ""
        public_link.set_lan_base(lan)
        bases: List[str] = []
        for wh in effective_webhooks(cfg) or []:
            if not isinstance(wh, dict) or wh.get("enabled", True) is False:
                continue
            if str(wh.get("format") or "") not in ("telegram", "whatsapp", "messenger"):
                continue
            if str(wh.get("links") or "login").strip().lower() == "off":
                continue
            base = str(wh.get("base_url") or "").strip().rstrip("/")
            if base.startswith("http") and base not in bases:
                bases.append(base)
        try:
            strikes = max(1, int(blk.get("strikes", public_link.DEFAULT_STRIKES) or 1))
        except (TypeError, ValueError):
            strikes = public_link.DEFAULT_STRIKES
        for base in bases:
            ok, detail = public_link.probe(base)
            flip = public_link.record(base, ok, detail, now=ts, strikes=strikes)
            if flip is True:
                logger.warning("公网入口不可达：%s（%s）——运维群卡片链接改用内网地址 %s 直到恢复",
                               base, detail, lan or "（推不出内网地址，仅在卡上说明）")
            elif flip is False:
                logger.info("公网入口已恢复：%s（%s），卡片链接换回公网地址", base, detail)

    def _build_daily_digest(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """摘要数据面（纯装配、绝不抛）：未处理项 / 真活探针 / 昨日花费。"""
        ts = float(now if now is not None else time.time())
        open_items: List[Dict[str, Any]] = []
        for it in self._remind.open_items(now=ts):
            key = str(it.get("key") or "")
            if key.startswith("_"):
                continue
            label = next((lbl for pre, lbl in self._DIGEST_LABELS if key.startswith(pre)), key)
            since = float(it.get("first_seen") or 0.0)
            open_items.append({
                "key": key, "label": label,
                "summary": str(it.get("summary") or ""),
                "since_ts": since,
                "hours": round(max(0.0, ts - since) / 3600.0, 1) if since > 0 else 0.0,
            })
        probes: Optional[Dict[str, Any]] = None
        tp = getattr(self, "_tp_state", None)
        if isinstance(tp, dict) and tp:
            bad = sorted(d for d, st in tp.items() if isinstance(st, dict) and st.get("alerted"))
            probes = {"total": len(tp), "ok": len(tp) - len(bad), "bad": bad}
        cost: Optional[Dict[str, Any]] = None
        try:
            from src.ai.cost_ledger import get_cost_ledger
            from src.web.routes.cost_routes import build_summary
            cfg = getattr(self._config_manager, "config", None) or {}
            s = build_summary(get_cost_ledger(), cfg if isinstance(cfg, dict) else {}, now=ts)
            if s.get("available"):
                y = s.get("yesterday") or {}
                cost = {
                    "provider": s.get("provider"), "currency": s.get("currency") or "CNY",
                    "yesterday_cost": y.get("cost"), "yesterday_calls": y.get("calls"),
                    "yesterday_truth": y.get("truth"),
                    "month_total": s.get("month_total"),
                    "budget_daily": (s.get("budget") or {}).get("daily"),
                    "balance": s.get("balance"), "runway_days": s.get("runway_days"),
                    "recon_verdict": str((s.get("last_recon") or {}).get("verdict") or ""),
                }
        except Exception:
            logger.debug("每日摘要成本段装配失败（已忽略）", exc_info=True)
        public: Optional[Dict[str, Any]] = None
        try:
            from src.ops import public_link
            snap = public_link.snapshot()
            if snap:
                public = {base: {"down": bool(st.get("down")),
                                 "down_hours": (round(max(0.0, ts - float(st.get("since") or 0.0)) / 3600.0, 1)
                                                if st.get("down") and st.get("since") else 0.0),
                                 "detail": str(st.get("detail") or "")}
                          for base, st in snap.items()}
        except Exception:
            public = None
        # 主链档位一行（2026-09-17）：单一口径 build_summary，effective 取活体 AIClient
        primary: Optional[Dict[str, Any]] = None
        try:
            from src.ai.ai_primary_summary import build_summary as _pm_summary
            cfg = getattr(self._config_manager, "config", None) or {}
            _cli = getattr(getattr(self._app, "state", None), "ai_client", None)
            _eff = getattr(_cli, "_primary_mode", None) if _cli is not None else None
            _lock = getattr(_cli, "_primary_lock", None) if _cli is not None else None
            s = _pm_summary(cfg if isinstance(cfg, dict) else {}, effective=_eff,
                            lock=(_lock if _lock is not None else None))
            primary = {k: s.get(k) for k in ("effective", "lock", "primary_text",
                                             "chain_text", "billing_provider", "mode_label")}
        except Exception:
            logger.debug("每日摘要主链段装配失败（已忽略）", exc_info=True)
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        return {"day": day, "open": open_items, "probes": probes, "cost": cost,
                "public_link": public, "primary": primary, "rate_key": f"ops_digest:{day}"}

    def _maybe_daily_digest(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        dc = self._digest_cfg()
        if not dc.get("enabled", False):
            return None
        ts = float(now if now is not None else time.time())
        lt = time.localtime(ts)
        try:
            hour = int(dc.get("hour", 9))
            minute = int(dc.get("minute", 0))
        except (TypeError, ValueError):
            hour, minute = 9, 0
        if (lt.tm_hour, lt.tm_min) < (hour, minute):
            return None
        day = time.strftime("%Y-%m-%d", lt)
        if str(self._remind.meta(self._RK_DIGEST, "last_day", "") or "") == day:
            return None
        # 先记「今天已发」再装配（与老板日报同理：装配失败等明天，不值得每 tick 重试）
        self._remind.set_meta(self._RK_DIGEST, "last_day", day)
        report = self._build_daily_digest(now=ts)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ops_digest_report", report)
            logger.info("HealthWatchdog 发出每日运维摘要（未处理 %d 项）", len(report["open"]))
        except Exception:
            logger.debug("ops_digest_report 发布失败（已忽略）", exc_info=True)
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
            "surface_in_ui": self._ui_surface_enabled(),
            "total_alerts": self.total_alerts,
            "total_recoveries": self.total_recoveries,
            "total_billing_alerts": self.total_billing_alerts,
            "total_draft_quality_alerts": self.total_draft_quality_alerts,
            "total_ai_quality_alerts": self.total_ai_quality_alerts,
            "total_realtime_voice_alerts": self.total_realtime_voice_alerts,
            "total_orchestrator_worker_alerts": self.total_orchestrator_worker_alerts,
            "total_memory_key_drift_alerts": self.total_memory_key_drift_alerts,
            "total_weekly_reports": self.total_weekly_reports,
            "total_daily_reports": self.total_daily_reports,
            "total_cloud_balance_alerts": self.total_cloud_balance_alerts,
            "total_license_quota_alerts": self.total_license_quota_alerts,
            "total_token_wallet_alerts": self.total_token_wallet_alerts,
            "total_fallback_duty_reminders": self.total_fallback_duty_reminders,
            "total_avatar_voice_reminders": self.total_avatar_voice_reminders,
            "total_media_promise_alerts": self.total_media_promise_alerts,
            "total_colloquial_llm_reminders": self.total_colloquial_llm_reminders,
            "total_identity_shadow_scans": self.total_identity_shadow_scans,
            # P4：入站半死升级提醒次数（此前只累加未出网 → 漏斗盲区）
            "total_inbox_read_stall_reminders": self.total_inbox_read_stall_reminders,
            # P0 2026-08-05：入站漏球（客户最后一句无回复且无草稿）告警次数
            "total_unanswered_inbound_alerts": self.total_unanswered_inbound_alerts,
            "total_accounts_truth_alerts": self.total_accounts_truth_alerts,
            "last_check_ts": self.last_check_ts,
            "last_light": self.last_light,
        }
