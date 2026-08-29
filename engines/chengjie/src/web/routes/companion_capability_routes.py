"""陪伴能力就绪度看板 API（只读，无副作用）。

端点：
  GET /api/companion/capabilities  —— 全部陪伴能力的开/关/灰度/blocked 状态 +
      分阶段开启阶梯 + 行动建议。服务「看板→校准→分阶段开启」北极星第一步：先看清楚。

纯聚合在 ``src.companion.capability_status.collect_capability_status``；本层只把
config（``config_manager.config``）与运行时子系统挂载信号（``app.state``）喂进去。
子系统未就绪时对应能力判 blocked 而非报错——与本仓「软降级」约定一致。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import Depends, Request

logger = logging.getLogger(__name__)

# capability runtime_dep 键 → app.state 属性名（存在即视为子系统已挂载）
_RUNTIME_STATE_ATTRS = {
    "translation_service": "translation_service",
    "quality_trend_store": "quality_trend_store",
    "autosend_worker": "autosend_worker",
    "companion_proactive_preview": "companion_proactive_preview",
    "care_schedule_store": "care_schedule_store",
    "deferred_outbox_store": "deferred_outbox_store",
}


def _overlay_dict(cm) -> dict:
    """读实例 overlay 原文（config.local.yaml），不是合并后的 config。

    值守「运营关闸」判定必须看 overlay 是否**显式**写了
    ``companion_send_gate.enabled: false``——合并 config 的代码缺省也是 false，
    读合并值会把新装机误判成运营关闸。文件缺失 / 解析失败 → {}（=未显式关）。
    """
    try:
        raw = getattr(cm, "config_path", "") or ""
        if not raw:
            return {}
        ov = Path(raw).parent / "config.local.yaml"
        if not ov.is_file():
            return {}
        import yaml
        with open(ov, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("读 overlay 失败（值守 send-gate 决策回落为默认开闸）",
                     exc_info=True)
        return {}


async def _provision_media_backends(cm, flags) -> dict:
    """开启类预设的**供给**步骤：托管版把识图指向官网网关（幂等）。

    2026-07-31 修「按钮只对开关负责、不对结果负责」：此前本接口只写 ``vision.enabled``，
    而托管版的识图后端是启动时的一次性内存注入——令牌后到、或写 overlay 触发的热重载，
    都会让它缺席。于是用户点完「一键开齐入站识别」拿到的是一盏「开了但后端未就绪」的
    黄灯加一句「联系客服」。现在开启前先补供给，让按钮对**能不能用**负责。

    非托管态 / 用户自配后端 → ``ensure_hosted_vision`` 内部自行早返（单一事实源在
    hosted_gateway，此处不复制判断）。绝不抛：供给失败仍照常写开关（保留「先开开关
    后补后端」的既有语义），只把结果如实回给前端。走线程池——领令牌可能真发 HTTP。
    """
    out: dict = {}
    if not (flags or {}).get("vision.enabled"):
        return out
    import asyncio

    try:
        from src.ai.hosted_gateway import (
            ensure_hosted_ai, ensure_hosted_vision, vision_provision_reason)

        def _run() -> bool:
            ensure_hosted_ai(cm)  # 识图与聊天共用设备令牌：没令牌先补令牌
            return bool(ensure_hosted_vision(cm))

        out["vision"] = await asyncio.to_thread(_run)
        out["vision_reason"] = vision_provision_reason(getattr(cm, "config", None))
    except Exception:
        logger.debug("托管识图供给失败（忽略，仍写开关）", exc_info=True)
        out["vision"] = False
    return out


def _audit_path(cm):
    base = getattr(cm, "config_path", None)
    return (Path(base).parent / "companion_capability_audit.jsonl") if base else None


def _snapshot_path(cm):
    base = getattr(cm, "config_path", None)
    return (Path(base).parent / "companion_capability_last_snapshot.json") if base else None


def _audit_toggle(cm, *, actor, key, field, value, path, reason="") -> None:
    """把开关变更追加到 config 目录下 companion_capability_audit.jsonl（best-effort）。"""
    try:
        p = _audit_path(cm)
        if p is None:
            return
        rec = {"ts": round(time.time(), 3), "actor": actor, "key": key,
               "field": field, "value": value, "path": path, "reason": reason}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("写陪伴能力开关审计失败（忽略）", exc_info=True)


def _read_audit(path, limit) -> list:
    """读审计 JSONL，返回最新在前的最多 limit 条（坏行跳过）。"""
    if not path or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    cap = max(1, min(int(limit or 50), 500))
    out = []
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
        if len(out) >= cap:
            break
    return out


def _apply_one(cm, config, modes, key, field, value, *, actor, reason) -> dict:
    """单条意图过护栏 + 写 overlay + 审计，返回结果项（不抛）。"""
    from src.companion.capability_toggle import check_toggle

    item = {"key": key, "field": field, "value": value}
    chk = check_toggle(config, modes, key, field, value)
    if not chk.get("allowed"):
        item["status"] = "blocked"
        item["reason"] = chk.get("reason") or "护栏拒绝"
        return item
    path = chk.get("flag_path") or ""
    ok, msg = cm.set_overlay_flag(path, value)
    if not ok:
        item["status"] = "error"
        item["reason"] = msg
        return item
    _audit_toggle(cm, actor=actor, key=key, field=field, value=value,
                  path=path, reason=reason)
    item["status"] = "applied"
    item["path"] = path
    if chk.get("warn"):
        item["warn"] = chk.get("reason")
    return item


def _apply_plan(cm, config, modes, plan, *, actor, reason) -> dict:
    """逐条应用意图列表（config 被 set_overlay_flag 就地更新，后条能看到前条结果）。"""
    applied, blocked, warned = [], [], []
    for it in plan or []:
        res = _apply_one(cm, config, modes, it["key"], it["field"],
                         bool(it["value"]), actor=actor, reason=reason)
        if res["status"] == "applied":
            applied.append(res)
            if res.get("warn"):
                warned.append(res)
        else:
            blocked.append(res)
    return {"applied": applied, "blocked": blocked, "warned": warned}


def _apply_extra_flags(cm, flags, *, actor, reason) -> dict:
    """把注册表外原生 config 路径直写 overlay（如金丝雀开关）。

    路径只来自 ``capability_presets`` 的白名单声明（预设 extras / 快照 extra_flags），
    非用户任意输入，故不过 ``check_toggle``；逐条审计，失败不中断。
    """
    applied, failed = [], []
    for path, value in (flags or {}).items():
        ok, msg = cm.set_overlay_flag(path, value)
        if ok:
            _audit_toggle(cm, actor=actor, key=path, field="raw", value=value,
                          path=path, reason=reason)
            applied.append({"path": path, "value": value})
        else:
            failed.append({"path": path, "value": value, "reason": msg})
    return {"extras_applied": applied, "extras_failed": failed}


def _collect_status(state, config):
    from src.companion.capability_status import collect_capability_status
    runtime = {dep: (getattr(state, attr, None) is not None)
               for dep, attr in _RUNTIME_STATE_ATTRS.items()}
    return collect_capability_status(config, runtime=runtime)


def _try_rewire(state) -> dict:
    """开关写完 overlay 后 best-effort 热接线 autosend（P1 2026-08-22）。

    闭包由 bootstrap 注册（``make_autosend_rewire``）；旧进程/测试 app 无此键
    → 如实回 ``{"rewired": False, "reason": "not_wired"}``（重启后仍会生效，
    与旧行为一致——feature 探测，绝不抛）。
    """
    fn = getattr(state, "autosend_rewire", None)
    if not callable(fn):
        return {"rewired": False, "reason": "not_wired"}
    try:
        return dict(fn() or {})
    except Exception:
        logger.debug("autosend 热接线调用失败（忽略）", exc_info=True)
        return {"rewired": False, "reason": "error"}


def _auto_ai_count(state):
    store = getattr(state, "inbox_store", None)
    if store is None:
        return None
    try:
        return sum(1 for v in store.all_automation_modes().values() if v == "auto_ai")
    except Exception:
        logger.debug("auto_ai 计数失败", exc_info=True)
        return None


def _gather_readiness(state, window_hours, *, ref_summary=None):
    """从各运营数据源 best-effort 取数 → readiness_signals（signals/advice 共用）。"""
    from src.companion.readiness_signals import readiness_signals

    win = max(1.0, min(float(window_hours or 168), 720))
    since = time.time() - win * 3600.0

    text_quality = None
    store = getattr(state, "inbox_store", None)
    if store is not None:
        try:
            text_quality = store.get_quality_stats(since_ts=since)
        except Exception:
            logger.debug("get_quality_stats 失败", exc_info=True)

    proactive_quality = None
    try:
        from src.monitoring.metrics_store import get_metrics_store
        proactive_quality = get_metrics_store().companion_quality_overview(
            window_sec=win * 3600.0)
    except Exception:
        logger.debug("companion_quality_overview 失败", exc_info=True)

    sent = failed = None
    svc = getattr(state, "draft_service", None)
    if svc is not None:
        try:
            rows = svc.list_audit(since_ts=since, limit=2000)
            sent = sum(1 for r in rows if str(r.get("action")) == "autosend")
            failed = sum(1 for r in rows if str(r.get("action")) == "autosend_failed")
        except Exception:
            logger.debug("list_audit 计数失败", exc_info=True)

    return readiness_signals(
        text_quality=text_quality, proactive_quality=proactive_quality,
        delivery={"autosend": sent, "autosend_failed": failed},
        realtime_voice=_realtime_voice_stats_dump(),
        voice_ref_summary=ref_summary)


def _realtime_voice_stats_dump():
    try:
        from src.ai.realtime_voice_stats import get_realtime_voice_stats
        return get_realtime_voice_stats().dump()
    except Exception:
        logger.debug("realtime_voice stats 读取失败", exc_info=True)
        return None


def _voice_ref_summary():
    try:
        from src.web.routes.voice_live_routes import collect_voice_ref_readiness_summary
        return collect_voice_ref_readiness_summary()
    except Exception:
        logger.debug("voice ref summary 失败", exc_info=True)
        return None


def _memory_store_available(state) -> Optional[bool]:
    store = getattr(state, "inbox_store", None)
    if store is None:
        return None
    try:
        sm = getattr(state, "skill_manager", None)
        if sm is not None and getattr(sm, "_episodic_store", None) is not None:
            return True
        if getattr(state, "episodic_memory", None) is not None:
            return True
    except Exception:
        pass
    return False


def gather_companion_advice(state, config, window_hours: float = 168):
    """组合 能力档 × 信号 → build_advice（advice 端点与 ops-overview 共用，单一事实源）。"""
    from src.companion.capability_advisor import build_advice
    from src.companion.embedding_readiness import embedding_source_configured
    from src.companion.realtime_voice_readiness import realtime_voice_host_configured
    status = _collect_status(state, config)
    ref_summary = _voice_ref_summary()
    signals = _gather_readiness(state, window_hours, ref_summary=ref_summary)
    embed_ready = embedding_source_configured(config)
    rtv_configured = realtime_voice_host_configured(config)
    return build_advice(status, signals, auto_ai=_auto_ai_count(state),
                        embed_ready=embed_ready, rtv_configured=rtv_configured,
                        rtv_ref_summary=ref_summary)


def register_companion_capability_routes(app, *, api_auth) -> None:
    @app.get("/api/companion/capabilities")
    async def api_companion_capabilities(request: Request, _=Depends(api_auth)):
        """陪伴栈能力就绪度看板。"""
        from src.companion.capability_status import collect_capability_status

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False,
                    "message": "config 未就绪", "capabilities": []}

        runtime = {dep: (getattr(state, attr, None) is not None)
                   for dep, attr in _RUNTIME_STATE_ATTRS.items()}
        try:
            data = collect_capability_status(config, runtime=runtime)
        except Exception:
            logger.warning("companion capability status 计算失败", exc_info=True)
            return {"ok": False, "available": True, "capabilities": [],
                    "message": "聚合计算失败"}
        return {"ok": True, "available": True, **data}

    @app.get("/api/companion/capabilities/delivery-calibration")
    async def api_companion_delivery_calibration(
        request: Request, window_hours: float = 24, _=Depends(api_auth),
    ):
        """全自动「真发」主开关开闸前校准：auto_ai 会话分布 + 三开关 verdict + 近期真发/失败。"""
        from src.companion.delivery_calibration import delivery_calibration

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}

        # 会话档位分布（auto_ai 决定 deliver 是否对人真发）
        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        # 近窗口真发/失败计数（best-effort；审计不可用则 None=未知）
        sent = failed = None
        svc = getattr(state, "draft_service", None)
        if svc is not None:
            try:
                win = max(1.0, min(float(window_hours or 24), 720))
                rows = svc.list_audit(since_ts=time.time() - win * 3600.0, limit=1000)
                sent = sum(1 for r in rows if str(r.get("action")) == "autosend")
                failed = sum(1 for r in rows
                             if str(r.get("action")) == "autosend_failed")
            except Exception:
                logger.debug("list_audit 计数失败", exc_info=True)

        try:
            data = delivery_calibration(
                config, modes, recent_autosend=sent, recent_autosend_failed=failed)
        except Exception:
            logger.warning("delivery calibration 计算失败", exc_info=True)
            return {"ok": False, "available": True, "message": "校准计算失败"}
        return {"ok": True, "available": True, "window_hours": window_hours, **data}

    @app.get("/api/companion/capabilities/realtime-voice-calibration")
    async def api_companion_realtime_voice_calibration(request: Request, _=Depends(api_auth)):
        """实时语音开闸前校准：host 配置 + 参考音体检 + opener/字幕/记忆链 + 引擎载入。"""
        from src.ai.realtime_voice import RealtimeVoiceConfig
        from src.ai.realtime_voice_client import RealtimeVoiceClient
        from src.companion.realtime_voice_calibration import realtime_voice_calibration
        from src.web.routes.voice_live_routes import collect_voice_ref_readiness_summary

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        rvc = RealtimeVoiceConfig.from_config(config)
        ref_summary = collect_voice_ref_readiness_summary()
        engine_loaded = None
        if rvc.enabled:
            try:
                import asyncio
                st = await asyncio.to_thread(RealtimeVoiceClient(rvc).model_status)
                engine_loaded = bool(st.get("model_loaded"))
            except Exception:
                logger.debug("realtime voice engine status 失败", exc_info=True)
                engine_loaded = False
        mem = _memory_store_available(state)
        try:
            data = realtime_voice_calibration(
                config, ref_summary=ref_summary, engine_loaded=engine_loaded,
                memory_store=mem if mem is not None else None)
        except Exception:
            logger.warning("realtime voice calibration 计算失败", exc_info=True)
            return {"ok": False, "available": True, "message": "校准计算失败"}
        return {"ok": True, "available": True, **data}

    @app.post("/api/companion/capabilities/toggle")
    async def api_companion_capability_toggle(request: Request, _=Depends(api_auth)):
        """分阶段开启「开」一步：带服务端护栏地开/关单个能力（写 config overlay + 审计）。

        body: {key, field?("enabled"|"dry_run"), value:bool, actor?}。
        护栏拒绝时返回 ``{ok:False, blocked:True, message}``；放行但有风险时 ``warn`` 带提示。
        """
        from src.companion.capability_status import collect_capability_status
        from src.companion.capability_toggle import check_toggle

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        if not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "message": "配置写入能力不可用（请升级 ConfigManager）"}

        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        key = str(body.get("key") or "").strip()
        field = str(body.get("field") or "enabled").strip()
        value = bool(body.get("value"))
        actor = (str(body.get("actor") or "").strip() or "web-admin")
        if not key:
            return {"ok": False, "message": "缺少 key"}

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        chk = check_toggle(config, modes, key, field, value)
        if not chk.get("allowed"):
            return {"ok": False, "blocked": True,
                    "message": chk.get("reason") or "护栏拒绝此操作"}

        path = chk.get("flag_path") or ""
        ok, msg = cm.set_overlay_flag(path, value)
        if not ok:
            return {"ok": False, "message": msg}
        _audit_toggle(cm, actor=actor, key=key, field=field, value=value, path=path)

        # 回算该能力最新状态 + 概要，供前端就地刷新
        runtime = {dep: (getattr(state, attr, None) is not None)
                   for dep, attr in _RUNTIME_STATE_ATTRS.items()}
        capability = summary = None
        try:
            data = collect_capability_status(config, runtime=runtime)
            capability = next((c for c in data.get("capabilities", [])
                               if c.get("key") == key), None)
            summary = data.get("summary")
        except Exception:
            logger.debug("回算能力状态失败", exc_info=True)
        resp = {"ok": True, "message": "已生效", "path": path, "field": field,
                "value": value, "capability": capability, "summary": summary}
        if chk.get("warn"):
            resp["warn"] = chk.get("reason")
        # autosend 双开关（worker/deliver）即时生效：热接线运行中的 worker
        if key in ("l2_autosend_worker", "l2_autosend_deliver"):
            resp["rewire"] = _try_rewire(state)
        return resp

    @app.get("/api/companion/media-capabilities")
    async def api_media_capabilities(request: Request, _=Depends(api_auth)):
        """入站多媒体能力（识图/识别语音/识别视频/自动发自拍）就绪自检：每项开没开 + 后端配没配。

        只读。回 ``{capabilities:[{key,label,enabled,backend_ready,stage,hint}], summary}``。
        stage=off（未开）/ needs_backend（开了但没配后端）/ active（开了且后端就绪）。
        """
        from src.companion.media_capability import collect_media_status

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False,
                    "message": "config 未就绪", "capabilities": []}
        try:
            data = collect_media_status(config)
        except Exception:
            logger.warning("media capability status 计算失败", exc_info=True)
            return {"ok": False, "available": True, "capabilities": [],
                    "message": "聚合计算失败"}
        return {"ok": True, "available": True, **data}

    @app.post("/api/companion/media-capabilities/preset")
    async def api_media_capabilities_preset(request: Request, _=Depends(api_auth)):
        """一键预设多媒体能力（只翻 vision/voice_recognition/selfie 开关，写 overlay + 审计）。

        body: {name: understand_all|understand_and_selfie|media_off, actor?}。
        开启类预设会附带 ``warnings``（若将开启的能力后端未就绪，如实提示但仍写开关——
        运营可先开开关再补后端，与其它 overlay 开关同语义）。
        """
        from src.companion.media_capability import (
            MEDIA_PRESETS, build_media_preset, collect_media_status,
            preset_backend_warnings,
        )

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict) or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        name = str(body.get("name") or "").strip()
        actor = (str(body.get("actor") or "").strip() or "web-admin")
        spec = build_media_preset(name)
        if spec is None:
            return {"ok": False, "message": f"未知媒体预设: {name}",
                    "presets": {k: v["label"] for k, v in MEDIA_PRESETS.items()}}

        # 供给先于开关：warnings 必须在供给之后算，否则报的是「修好之前」的旧账
        provisioned = await _provision_media_backends(cm, spec.get("flags") or {})
        warnings = preset_backend_warnings(name, config)
        applied, failed = [], []
        for path, value in (spec.get("flags") or {}).items():
            ok, msg = cm.set_overlay_flag(path, bool(value))
            if ok:
                _audit_toggle(cm, actor=actor, key=path, field="enabled",
                              value=bool(value), path=path, reason=f"media_preset:{name}")
                applied.append({"path": path, "value": bool(value)})
            else:
                failed.append({"path": path, "value": bool(value), "reason": msg})
        status = None
        try:
            status = collect_media_status(config)
        except Exception:
            logger.debug("回算媒体能力状态失败", exc_info=True)
        return {"ok": True, "preset": name, "label": spec["label"],
                "applied": applied, "failed": failed, "warnings": warnings,
                "provisioned": provisioned, "status": status}

    @app.post("/api/companion/media-capabilities/provision")
    async def api_media_capabilities_provision(request: Request, _=Depends(api_auth)):
        """「重试接入」：重跑一次托管识图供给，回结构化原因码 + 最新自检状态。

        存在理由＝把托管态那句死路文案（「如未生效请联系客服开启」）换成用户点得动的
        动作：绝大多数「后端未就绪」只是供给没发生（令牌后到 / 热重载抹掉），重跑即好，
        根本不该开工单。真需要人工介入时也给出确定性原因码（``no_token`` 等），
        客服不必从零猜。
        """
        from src.ai.hosted_gateway import vision_provision_reason
        from src.companion.media_capability import collect_media_status

        cm = getattr(request.app.state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        provisioned = await _provision_media_backends(cm, {"vision.enabled": True})
        reason = vision_provision_reason(config)
        status = None
        try:
            status = collect_media_status(config)
        except Exception:
            logger.debug("回算媒体能力状态失败", exc_info=True)
        return {"ok": True, "provisioned": provisioned, "reason": reason,
                "status": status}

    @app.get("/api/companion/capabilities/toggle-audit")
    async def api_companion_toggle_audit(
        request: Request, limit: int = 50, _=Depends(api_auth),
    ):
        """最近的能力开关变更（谁/何时/改了啥/上下文 reason），最新在前。"""
        cm = getattr(request.app.state, "config_manager", None)
        if cm is None:
            return {"ok": False, "available": False, "entries": []}
        entries = _read_audit(_audit_path(cm), limit)
        return {"ok": True, "available": True,
                "count": len(entries), "entries": entries}

    @app.get("/api/companion/capabilities/preset-preview")
    async def api_companion_capability_preset_preview(
        request: Request, name: str = "", _=Depends(api_auth),
    ):
        """只读预览一键预设将写入的意图（含运营关闸时是否跳过 send-gate）。

        与 POST ``/preset`` 共用 ``preview_preset``；本端点零 overlay 写入、
        零快照、零 rewire。``name`` 未知 → ``error=unknown_preset``。
        """
        from src.companion.capability_presets import PRESETS, preview_preset

        cm = getattr(request.app.state, "config_manager", None)
        if cm is None:
            return {"ok": False, "available": False, "error": "config_not_ready"}
        preview = preview_preset(str(name or "").strip(), overlay=_overlay_dict(cm))
        if preview is None:
            return {"ok": False, "error": "unknown_preset",
                    "presets": {k: v["label"] for k, v in PRESETS.items()}}
        return {"ok": True, "available": True, **preview}

    @app.post("/api/companion/capabilities/preset")
    async def api_companion_capability_preset(request: Request, _=Depends(api_auth)):
        """一键预设档：按风险阶梯整档切换（每条仍逐项过护栏）；切换前自动存快照供回滚。

        body: {name: safe_default|dry_run_trial|full_auto, actor?}。
        """
        from src.companion.capability_presets import (
            PRESETS, preview_preset, capture_extra_flags, capture_snapshot,
            preset_extras,
        )
        from src.companion.capability_status import collect_capability_status

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict) or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        name = str(body.get("name") or "").strip()
        actor = (str(body.get("actor") or "").strip() or "web-admin")
        overlay = _overlay_dict(cm)
        preview = preview_preset(name, overlay=overlay)
        if preview is None:
            return {"ok": False, "message": f"未知预设: {name}",
                    "presets": {k: v["label"] for k, v in PRESETS.items()}}
        plan = preview["plan"]

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        # 切换前存快照（best-effort，供 rollback；extra_flags 覆盖金丝雀等注册表外路径）
        try:
            sp = _snapshot_path(cm)
            if sp is not None:
                snap = {"ts": round(time.time(), 3), "actor": actor,
                        "applied_preset": name, "snapshot": capture_snapshot(config),
                        "extra_flags": capture_extra_flags(config)}
                sp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.debug("存快照失败（忽略）", exc_info=True)

        result = _apply_plan(cm, config, modes, plan, actor=actor, reason=f"preset:{name}")
        extras = preset_extras(name)
        if extras:
            result.update(_apply_extra_flags(
                cm, {e["path"]: e["value"] for e in extras},
                actor=actor, reason=f"preset:{name}"))
        result["rewire"] = _try_rewire(state)
        summary = None
        try:
            runtime = {dep: (getattr(state, attr, None) is not None)
                       for dep, attr in _RUNTIME_STATE_ATTRS.items()}
            summary = collect_capability_status(config, runtime=runtime).get("summary")
        except Exception:
            logger.debug("回算概要失败", exc_info=True)
        if preview.get("send_gate_skipped"):
            result["send_gate_skipped"] = preview["send_gate_skipped"]
        return {"ok": True, "preset": name, "label": PRESETS[name]["label"],
                "summary": summary, **result}

    @app.post("/api/companion/capabilities/rollback")
    async def api_companion_capability_rollback(request: Request, _=Depends(api_auth)):
        """回滚到上一次预设切换前的快照（逐项过护栏；条件变了的项会被如实拦下）。"""
        from src.companion.capability_presets import snapshot_to_plan
        from src.companion.capability_status import collect_capability_status

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict) or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        actor = (str((body or {}).get("actor") or "").strip() or "web-admin")

        sp = _snapshot_path(cm)
        if sp is None or not sp.exists():
            return {"ok": False, "message": "无可回滚的快照（尚未做过一键切换）"}
        try:
            blob = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return {"ok": False, "message": "快照损坏，无法回滚"}
        plan = snapshot_to_plan((blob or {}).get("snapshot") or {})
        if not plan:
            return {"ok": False, "message": "快照为空"}

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        result = _apply_plan(cm, config, modes, plan, actor=actor, reason="rollback")
        extra_flags = (blob or {}).get("extra_flags") or {}
        if isinstance(extra_flags, dict) and extra_flags:
            result.update(_apply_extra_flags(cm, extra_flags,
                                             actor=actor, reason="rollback"))
        result["rewire"] = _try_rewire(state)
        summary = None
        try:
            runtime = {dep: (getattr(state, attr, None) is not None)
                       for dep, attr in _RUNTIME_STATE_ATTRS.items()}
            summary = collect_capability_status(config, runtime=runtime).get("summary")
        except Exception:
            logger.debug("回算概要失败", exc_info=True)
        return {"ok": True, "restored_from": (blob or {}).get("ts"),
                "undid_preset": (blob or {}).get("applied_preset"),
                "summary": summary, **result}

    @app.get("/api/companion/standby")
    async def api_companion_standby_get(request: Request, _=Depends(api_auth)):
        """AI 值守当前姿态（收件箱一键开关）：off|suggest|watching|custom + 切「值守中」就绪度。

        只读。``watching`` 段复用 ``check_toggle`` 权威判定告诉 UI：现在切值守中会不会被
        双重 opt-in 护栏拦下（如无 auto_ai 会话），以及是否裸奔（send-gate 未开）。
        """
        from src.companion.delivery_calibration import delivery_calibration
        from src.companion.standby_mode import (
            infer_standby_mode, split_state, standby_options,
            watching_send_gate_fields,
        )
        from src.companion.capability_toggle import check_toggle

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        cal = delivery_calibration(config, modes)
        chk = check_toggle(config, modes, "l2_autosend_deliver", "enabled", True)
        return {
            "ok": True, "available": True,
            "mode": infer_standby_mode(config),
            "options": standby_options(),
            # #12 拆开控制读侧（2026-08-30）：新会话默认档 / worker / 真发总闸 /
            # 显式全自动行数（关真发前的影响面披露）。旧前端不识此键零影响；
            # 新前端据此渲染「拆开控制」面板（键缺席=旧后端 → 面板隐藏）。
            "split": split_state(
                config, auto_ai_rows=cal["automation_modes"]["auto_ai"]),
            "watching": {
                "allowed": bool(chk.get("allowed")),
                "warn": bool(chk.get("warn")),
                "reason": chk.get("reason") or "",
                "auto_ai": cal["automation_modes"]["auto_ai"],
                # 全局默认档=auto_ai（新会话 bootstrap 即全自动）——前端据此
                # 不再对「显式 auto_ai=0」误报「不会对任何人真发」（B37 读侧）
                "global_auto_ai": bool(cal.get("global_auto_ai")),
                "worker": cal["switches"]["worker"],
                "send_gate": cal["switches"]["send_gate"],
                **watching_send_gate_fields(_overlay_dict(cm)),
            },
        }

    @app.post("/api/companion/standby")
    async def api_companion_standby_set(request: Request, _=Depends(api_auth)):
        """切换 AI 值守姿态（主管专属）。逐条过同一套护栏 + 写 overlay + 存快照供回滚。

        body: {mode: off|suggest|watching, actor?}。

        P1 2026-08-22 捆绑语义（「一键全自动」单写入口）：三档不再只管
        worker/deliver 两开关——**默认档位 extras 先落**（watching→auto_ai+
        bootstrap / suggest→review / off→manual，护栏因此看到「全局已全自动」，
        新装机零会话也不再死锁）→ 能力计划照旧逐条过护栏 → **存量系统落档
        会话批量对齐**（只动 bootstrap/standby 来源的行，人的显式设置绝不覆盖）
        → **热接线**运行中的 worker（真发当场生效，不再等重启）。
        """
        from src.web.routes.unified_inbox_auth import _require_supervisor
        from src.companion.standby_mode import (
            align_existing_conversations, build_standby_plan,
            infer_standby_mode, is_standby_mode, send_gate_operator_off,
            split_state, standby_align_target, standby_extras,
            watching_send_gate_fields, STANDBY_LABELS,
        )
        from src.companion.capability_presets import capture_extra_flags, capture_snapshot
        from src.companion.delivery_calibration import delivery_calibration
        from src.companion.capability_toggle import check_toggle

        _require_supervisor(request)  # 全局改 autosend 姿态是主管动作（收件箱对坐席也可见）

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict) or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        mode = str(body.get("mode") or "").strip()
        actor = (str(body.get("actor") or "").strip() or "web-admin")
        overlay = _overlay_dict(cm)

        # ── #12 拆开控制写侧（2026-08-30）：body {"set": {...}} 单键直写，与三档
        #    预设互斥（带 set 即不走捆绑路径）。「新会话默认档」只写
        #    inbox.auto_draft.automation_mode（不动真发/worker/存量会话）；「真发
        #    总闸」只走 l2_autosend_deliver 能力意图（check_toggle 护栏 + 审计 +
        #    热接线照旧）——修「切个默认档连真发一起翻、既有全自动 B 线全灭
        #    零提示」（钧 0830 01:57 审计行实锤）。
        setter = body.get("set") if isinstance(body.get("set"), dict) else None
        if setter:
            modes = None
            store = getattr(state, "inbox_store", None)
            if store is not None:
                try:
                    modes = store.all_automation_modes()
                except Exception:
                    logger.debug("all_automation_modes 失败", exc_info=True)
            out: dict = {"ok": True, "split_applied": {}}
            if "default_mode" in setter:
                dm = str(setter.get("default_mode") or "").strip().lower()
                from src.inbox.store import AUTOMATION_MODES as _AM
                if dm not in _AM:
                    return {"ok": False, "message": f"未知默认档: {dm}"}
                out.update(_apply_extra_flags(
                    cm, {"inbox.auto_draft.automation_mode": dm},
                    actor=actor, reason="standby-split:default_mode"))
                out["split_applied"]["default_mode"] = dm
            if "deliver" in setter:
                want = bool(setter.get("deliver"))
                from src.companion.capability_presets import (
                    CAP_BY_KEY, _intentions_for, _order,
                )
                cap = CAP_BY_KEY.get("l2_autosend_deliver")
                mini_plan = (_order(_intentions_for(
                    cap, "on" if want else "off")) if cap is not None else [])
                out.update(_apply_plan(cm, config, modes, mini_plan,
                                       actor=actor,
                                       reason="standby-split:deliver"))
                out["split_applied"]["deliver"] = want
                out["rewire"] = _try_rewire(state)
            cal = delivery_calibration(config, modes)
            out["mode"] = infer_standby_mode(config)
            out["split"] = split_state(
                config, auto_ai_rows=cal["automation_modes"]["auto_ai"])
            return out

        plan = build_standby_plan(mode, overlay=overlay)
        if plan is None or not is_standby_mode(mode):
            return {"ok": False, "message": f"未知值守档: {mode}",
                    "options": [{"mode": m, "label": lbl}
                                for m, lbl in STANDBY_LABELS.items()]}

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        # 切换前存快照（与预设共用单槽，供 /rollback 统一回滚；标 standby:<mode> 便于审计）
        try:
            sp = _snapshot_path(cm)
            if sp is not None:
                snap = {"ts": round(time.time(), 3), "actor": actor,
                        "applied_preset": f"standby:{mode}",
                        "snapshot": capture_snapshot(config),
                        "extra_flags": capture_extra_flags(config)}
                sp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.debug("存快照失败（忽略）", exc_info=True)

        result: dict = {}
        # ① 默认档位 extras **先于**能力计划——deliver 的护栏读全局档位，
        #    先写档位才轮到「全局已全自动」的免死锁语义（B37 修复的写侧）。
        extras = standby_extras(mode)
        if extras:
            result.update(_apply_extra_flags(
                cm, {e["path"]: e["value"] for e in extras},
                actor=actor, reason=f"standby:{mode}"))
        # ② 能力计划逐条过护栏（send-gate 先立 → worker → deliver 压最后）
        result.update(_apply_plan(cm, config, modes, plan,
                                  actor=actor, reason=f"standby:{mode}"))
        # ③ 存量系统落档会话批量对齐（走线程池：可能逐行写几百条）
        aligned = 0
        target = standby_align_target(mode)
        if target and store is not None:
            try:
                import asyncio as _aio
                aligned = await _aio.to_thread(
                    align_existing_conversations, store, target)
            except Exception:
                logger.debug("存量会话对齐失败（忽略）", exc_info=True)
        result["aligned_conversations"] = aligned
        # ④ 热接线运行中的 worker（真发当场生效；旧进程无闭包=如实回报）
        result["rewire"] = _try_rewire(state)
        cal = delivery_calibration(config, modes)
        chk = check_toggle(config, modes, "l2_autosend_deliver", "enabled", True)
        out = {
            "ok": True, "requested": mode, "mode": infer_standby_mode(config),
            "label": STANDBY_LABELS.get(mode, mode),
            "split": split_state(
                config, auto_ai_rows=cal["automation_modes"]["auto_ai"]),
            "watching": {
                "allowed": bool(chk.get("allowed")), "warn": bool(chk.get("warn")),
                "reason": chk.get("reason") or "",
                "auto_ai": cal["automation_modes"]["auto_ai"],
                "global_auto_ai": bool(cal.get("global_auto_ai")),
                "worker": cal["switches"]["worker"],
                "send_gate": cal["switches"]["send_gate"],
                **watching_send_gate_fields(overlay),
            },
            **result,
        }
        if mode == "watching" and send_gate_operator_off(overlay):
            out["send_gate_skipped"] = "operator_off"
        return out

    @app.get("/api/companion/capabilities/signals")
    async def api_companion_capability_signals(
        request: Request, window_hours: float = 168, _=Depends(api_auth),
    ):
        """决策信号：把真实运营指标翻成「该不该往上爬一档」的数据驱动建议。

        三路：文本自动回复质量(get_quality_stats) + 主动触达好评(companion_quality_overview)
        + 真发投递失败率(近窗口审计)。每路给 verdict + 一句下一步建议。
        """
        data = _gather_readiness(request.app.state, window_hours)
        return {"ok": True, "available": True, "window_hours": window_hours, **data}

    @app.get("/api/companion/capabilities/advice")
    async def api_companion_capability_advice(
        request: Request, window_hours: float = 168, _=Depends(api_auth),
    ):
        """能力档 × 决策信号 联动建议 + 配置一致性体检（闭环纠偏）。

        把每档当前状态与对应运营信号对齐成一条可执行建议（含一键 target），
        并查开关自洽性（真发开但 worker 关 / 无 auto_ai / 裸奔 / 语音孤悬 / blocked）。
        """
        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            advice = gather_companion_advice(state, config, window_hours)
        except Exception:
            logger.warning("companion advice 聚合失败", exc_info=True)
            return {"ok": False, "available": True, "message": "建议聚合失败"}
        return {"ok": True, "available": True, "window_hours": window_hours, **advice}


__all__ = ["register_companion_capability_routes"]
