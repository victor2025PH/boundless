"""「自动回复设置」页路由（P0，2026-08-02）。

挂载点：
    GET  /reply-settings          — 设置页面（回复档位 / 速度 / 拟人链 / 语音）
    GET  /api/reply-settings      — 当前有效值 + 字段元数据 + 上游闸门状态
    POST /api/reply-settings      — 白名单校验后写 config.local.yaml overlay
                                    （保注释），deliver_delay 对活体 worker 热更

职责边界：
- 校验/快照/热生效判定全在纯函数核心 ``src.inbox.reply_pacing_settings``，
  本层只做 config 读写 + worker 热更 + 审计 + i18n。
- ``l2_autosend.enabled`` / ``deliver``（AI 真发主开关）**刻意不可写**——
  那是能力看板 critical 双重 opt-in 护栏的辖区，本页只读展示 + 深链。
- 响应体 0 硬编码 CJK（错误文案走 tr() + reply_settings_page pack）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_ERR_KEYS = {
    "unknown_field": "rps_err_unknown_field",
    "bad_bool": "rps_err_bad_value",
    "bad_enum": "rps_err_bad_value",
    "bad_number": "rps_err_bad_value",
    "out_of_range": "rps_err_out_of_range",
    "min_gt_max": "rps_err_min_gt_max",
    "bad_overrides": "rps_err_bad_value",
    "bad_persona_id": "rps_err_bad_persona",
    "bad_override_key": "rps_err_bad_value",
    "too_long": "rps_err_too_long",
    "bad_platform": "rps_err_bad_platform",
    "cap_unsupported": "rps_err_cap_unsupported",
    # 工作时间班表（P0-ws）
    "bad_time": "rps_err_bad_time",
    "bad_workdays": "rps_err_bad_value",
    "bad_timezone": "rps_err_bad_timezone",
    "bad_account_key": "rps_err_bad_account_key",
    "incomplete_window": "rps_err_incomplete_window",
}

_HUMANIZE_FLAGS = {
    "inbox.l2_autosend.mark_read_before_reply": "mark_read",
    "inbox.l2_autosend.typing_indicator": "typing",
}
_DELAY_PREFIX = "inbox.l2_autosend.deliver_delay."
_PLAT_HUMANIZE_PATH = "inbox.l2_autosend.platform_humanize"


def _audit_path(config_manager):
    base = getattr(config_manager, "config_path", None)
    return (Path(base).parent / "reply_settings_audit.jsonl") if base else None


def _append_audit(config_manager, *, actor, changes, old_values) -> None:
    """开关变更审计（best-effort，绝不影响保存结果）。"""
    try:
        p = _audit_path(config_manager)
        if p is None:
            return
        rec = {
            "ts": round(time.time(), 3), "actor": actor,
            "changes": {k: {"old": old_values.get(k), "new": v}
                        for k, v in changes.items()},
        }
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("写自动回复设置审计失败（忽略）", exc_info=True)


def register_reply_settings_routes(
    app, *, page_auth, api_auth, templates, config_manager,
) -> None:
    from src.inbox.reply_pacing_settings import (
        HUMANIZE_FLAG_KEYS, PRESETS, REPLACE_PATHS, build_snapshot,
        chain_pacing_coverage, cross_validate, effective_values,
        explain_pacing, match_preset, merged_delay_block,
        merged_persona_overrides, merged_scoped_overrides, nested_patch,
        sanitize_patch, split_hot_pending, validate_platform_flags_caps,
    )
    _OVERRIDES_PATH = _DELAY_PREFIX + "persona_overrides"
    _PLAT_OVERRIDES_PATH = _DELAY_PREFIX + "platform_overrides"

    def _runtime_readback(request: Request):
        """活体 worker 的实际生效值（P1-c 保存后回读对账；无 worker → None）。"""
        try:
            worker = getattr(getattr(request.app, "state", None),
                             "autosend_worker", None)
            fn = getattr(worker, "runtime_pacing_snapshot", None)
            return fn() if callable(fn) else None
        except Exception:
            logger.debug("读 worker 运行时快照失败（忽略）", exc_info=True)
            return None

    def _observed():
        """进程内实测观测（P2）：拟人延迟分布 + 已读/打字按平台成功率。

        数据源是 humanize_metrics 的进程级累计（autosend / native_tg 各路径都在，
        前端按前缀分组展示）——把「配置说 3–9 秒」与「线上实际多少」摆到同一页。
        采集缺席/异常 → None（前端整块隐藏，不谎报「尚无采样」）。"""
        try:
            from src.integrations.humanize_metrics import (
                pacing_snapshot as _pace,
                snapshot as _hum,
            )
            return {"pacing": _pace(), "humanize": _hum()}
        except Exception:
            logger.debug("读拟人观测快照失败（忽略）", exc_info=True)
            return None

    def _style_overrides():
        """人设「说话方式」显式覆写计数（P0-style：全局默认卡的分层提示）。

        让运营在改全局默认时看见「有 N 个人设自定义了，不吃这里的值」——
        没有这行提示，「改了却有人设没变」必然变成工单。异常 → None（UI 隐藏）。
        """
        try:
            from src.utils.persona_manager import PersonaManager
            return PersonaManager.get_instance().count_speaking_overrides()
        except Exception:
            logger.debug("读人设覆写计数失败（忽略）", exc_info=True)
            return None

    def _observed_reply_len():
        """近 200 条真实回复的长度分布（内容与风格卡「实测回读」；异常 → None）。"""
        try:
            from src.monitoring.metrics_store import get_metrics_store
            return get_metrics_store().reply_length_snapshot()
        except Exception:
            logger.debug("读回复长度分布失败（忽略）", exc_info=True)
            return None

    # 平台能力清单（P0-caps，2026-08-03）：mark_read / typing 按平台三态，
    # 事实源 = capability_matrix（与编排器运行时 hasattr 判据同源）——UI 渲染它，
    # 替掉模板里手写且已过时的「当前 Telegram 协议号支持」。判定要构造 5 个
    # probe worker（纯赋值无 IO，但有 import 开销）→ 进程内 TTL 缓存；能力只随
    # 代码/重启变，5 分钟窗口足够新鲜。异常 → None（前端整块隐藏，不谎报）。
    _caps_cache: dict = {"ts": 0.0, "val": None}

    def _platform_caps():
        try:
            now = time.time()
            if _caps_cache["val"] is not None and now - _caps_cache["ts"] < 300:
                return _caps_cache["val"]
            from src.integrations.platform_capabilities import (
                humanize_caps_by_platform,
            )
            val = humanize_caps_by_platform(
                getattr(config_manager, "config", None) or {})
            _caps_cache.update(ts=now, val=val)
            return val
        except Exception:
            logger.debug("读平台能力清单失败（忽略）", exc_info=True)
            return None

    def _snapshot_extras(snap: dict, request: Request) -> dict:
        """GET/POST 共用的快照增量（P1：预设匹配 + 实测长度；P0-caps：能力清单）。"""
        snap["style_overrides"] = _style_overrides()
        snap["presets"] = PRESETS
        snap["preset_match"] = match_preset(snap.get("values") or {})
        snap["observed_reply_len"] = _observed_reply_len()
        snap["runtime"] = _runtime_readback(request)
        snap["observed"] = _observed()
        snap["platform_caps"] = _platform_caps()
        # 链路生效自检（2026-08-07）：三条发送链各自的节奏来源/值，任何一条
        # 「active 且秒回」→ 前端顶部亮红——修「滑杆只管部分链、其余链 0 延迟
        # 却在页面上隐形」。异常 → None（前端整块隐藏，不谎报）。
        try:
            snap["chain_coverage"] = chain_pacing_coverage(
                getattr(config_manager, "config", None) or {})
        except Exception:
            logger.debug("读链路节奏自检失败（忽略）", exc_info=True)
            snap["chain_coverage"] = None
        return snap

    @app.get("/reply-settings", response_class=HTMLResponse)
    async def reply_settings_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "reply_settings.html", {})

    @app.get("/api/reply-settings")
    async def api_reply_settings_get(request: Request, _=Depends(api_auth)):
        cfg = getattr(config_manager, "config", None) or {}
        return _snapshot_extras(build_snapshot(cfg), request)

    @app.get("/api/reply-settings/explain")
    async def api_reply_settings_explain(
        request: Request, platform: str = "", account_id: str = "default",
        chat_key: str = "", _=Depends(api_auth),
    ):
        """生效值溯源（P1）：这个会话此刻会用什么档位/人设/节奏/风格、来自哪层。

        每个分区独立 best-effort（拿不到就 None，绝不 500）——溯源是排障辅助，
        任何子系统缺席（测试装配/store 未启）都不该让整个接口不可用。
        来源口径与真实行为共用同一批函数：档位=resolve_automation_mode 同源、
        人设=autosend worker 同一 resolver、节奏=explain_pacing（merge 语义与
        humanize 一致）、风格=explain_reply_style（与 prompt 格式化器同 precedence，
        有门禁对拍）。
        """
        cfg = getattr(config_manager, "config", None) or {}
        platform = str(platform or "").lower().strip()
        account_id = str(account_id or "default").strip() or "default"
        chat_key = str(chat_key or "").strip()
        if not platform or not chat_key:
            return {"ok": False, "errors": [{
                "field": "platform/chat_key", "code": "empty",
                "message": tr(request, "rps_err_ctx_required")}]}

        out: dict = {"ok": True, "platform": platform,
                     "account_id": account_id, "chat_key": chat_key}

        # 会话 id + 档位（显式设置 vs 全局默认，来源分开报）
        mode_val, mode_src, cid = None, None, ""
        try:
            from src.inbox.normalizer import conv_id
            cid = conv_id(platform, account_id, chat_key)
            out["conversation_id"] = cid
            from src.web.routes.unified_inbox_services import _inbox_store
            store = _inbox_store(request)
            explicit = None
            if store is not None:
                try:
                    explicit = store.get_automation_mode_if_set(cid)
                except Exception:
                    explicit = None
            if explicit:
                mode_val, mode_src = str(explicit), "conversation"
            else:
                from src.inbox.automation_mode import resolve_automation_mode
                if store is not None:
                    mode_val = resolve_automation_mode(store, cid, cfg)
                else:
                    mode_val = str((cfg.get("inbox", {}) or {}).get(
                        "auto_draft", {}).get("automation_mode") or "auto_ai")
                mode_src = "global"
        except Exception:
            logger.debug("explain: 档位解析失败", exc_info=True)
        out["mode"] = ({"value": mode_val, "source": mode_src}
                       if mode_val else None)

        # 生效人设（与 autosend worker 同一 resolver：含会话级覆写）
        persona_id, persona_name = "", ""
        try:
            from src.ai.persona_voice import resolve_effective_persona_id
            persona_id = str(resolve_effective_persona_id(
                cfg, platform, account_id, chat_key) or "")
        except Exception:
            logger.debug("explain: 人设解析失败", exc_info=True)
        persona_dict = None
        if persona_id:
            try:
                from src.utils.persona_manager import PersonaManager
                persona_dict = PersonaManager.get_instance(
                ).get_persona_by_id(persona_id)
                if isinstance(persona_dict, dict):
                    persona_name = str(persona_dict.get("name") or "")
            except Exception:
                logger.debug("explain: 人设档案读取失败", exc_info=True)
        out["persona"] = {"id": persona_id, "name": persona_name}

        # 节奏（键级来源：人设覆写 → 平台覆写 → 全局 → 缺省）
        try:
            out["pacing"] = explain_pacing(cfg, persona_id, platform=platform)
        except Exception:
            logger.debug("explain: 节奏溯源失败", exc_info=True)
            out["pacing"] = None

        # 内容与风格（与 prompt 格式化器同 precedence）
        try:
            from src.utils.persona_manager import (
                explain_reply_style, resolve_reply_defaults,
            )
            out["style"] = explain_reply_style(
                persona_dict if isinstance(persona_dict, dict) else {},
                resolve_reply_defaults(cfg))
        except Exception:
            logger.debug("explain: 风格溯源失败", exc_info=True)
            out["style"] = None

        # 工作时间班表（P0-ws，2026-08-04）：该账号此刻在班/休息、班表窗口与
        # 下次边界——「这个号为什么现在不自动回」必须在生效值卡一眼可见，
        # 与闸门共用 schedule_state 同一口径（预判≠另算一套）。
        try:
            from src.inbox.work_hours_gate import (
                schedule_state,
                work_schedule_cfg,
            )
            out["work_schedule"] = schedule_state(
                work_schedule_cfg(cfg), platform, account_id)
        except Exception:
            logger.debug("explain: 班表快照失败", exc_info=True)
            out["work_schedule"] = None
        return out

    @app.get("/api/reply-settings/budget-today")
    async def api_reply_settings_budget_today(
        request: Request, _=Depends(api_auth),
    ):
        """回复额度守卫「今日额度状态」列表（只读，P0-guard 2026-08-12）。

        rows 按当日自动链轮次降序（上限 50），逐行带 (platform, account_id,
        chat_key) 三元组——前端豁免按钮直接投 **收件箱既有** relief 端点
        （POST /api/unified-inbox/reply-budget/relief），本页不造第二个写入口。
        exhausted/hard_stopped 语义与收件箱横幅同源（peer_bot_guard.
        budget_flags）。``guard`` 段＝parse_cfg 全量解析值（高级参数只读展示）。
        store 缺席（收件箱持久化未启）/旧 store 无列表方法 → available=false
        + 空表，如实降级不装死。
        """
        from src.inbox.peer_bot_guard import budget_flags, parse_cfg, today_key
        guard = parse_cfg(getattr(config_manager, "config", None) or {})
        day = today_key()
        out = {"ok": True, "day": day, "available": False,
               "guard": guard, "rows": []}
        try:
            from src.web.routes.unified_inbox_services import _inbox_store
            store = _inbox_store(request)
        except Exception:
            store = None
        if store is None or not hasattr(store, "list_reply_budget_today"):
            return out
        try:
            raw = store.list_reply_budget_today(day, limit=50) or []
        except Exception:
            logger.debug("读当日预算台账失败（如实降级）", exc_info=True)
            return out
        out["available"] = True
        for r in raw:
            flags = budget_flags(r.get("used"), r.get("relieved"), guard)
            out["rows"].append({
                "conversation_id": str(r.get("conversation_id") or ""),
                "platform": str(r.get("platform") or ""),
                "account_id": str(r.get("account_id") or ""),
                "chat_key": str(r.get("chat_key") or ""),
                "title": str(r.get("display_name") or r.get("chat_key")
                             or r.get("conversation_id") or ""),
                **flags,
            })
        return out

    @app.post("/api/reply-settings")
    async def api_reply_settings_save(request: Request, _=Depends(api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        changes = body.get("changes") if isinstance(body, dict) else None
        if not isinstance(changes, dict) or not changes:
            return {"ok": False,
                    "errors": [{"field": "", "code": "empty",
                                "message": tr(request, "rps_err_empty")}]}

        cfg = getattr(config_manager, "config", None) or {}
        clean, errors = sanitize_patch(changes)
        errors += cross_validate(clean, cfg)
        # 能力护栏（P1）：显式开启的平台拟人开关必须该平台真支持——前端 disabled
        # 不算护栏，绕过 UI 直打 API 也要拦；探针不可用 → fail-open 放行。
        if _PLAT_HUMANIZE_PATH in clean:
            errors += validate_platform_flags_caps(
                clean[_PLAT_HUMANIZE_PATH], _platform_caps())
        if errors:
            for e in errors:
                e["message"] = tr(
                    request, _ERR_KEYS.get(e["code"], "rps_err_bad_value"),
                    field=e.get("field", ""))
            return {"ok": False, "errors": errors}

        # 覆写表：UI 管理键提交表 × 现存表（YAML 手调键按条目保留）→ 最终整表；
        # 落盘走 REPLACE_PATHS 整树替换（删掉的人设/平台真被删掉）。
        if _OVERRIDES_PATH in clean:
            clean[_OVERRIDES_PATH] = merged_persona_overrides(
                cfg, clean[_OVERRIDES_PATH])
        if _PLAT_OVERRIDES_PATH in clean:
            clean[_PLAT_OVERRIDES_PATH] = merged_scoped_overrides(
                cfg, _PLAT_OVERRIDES_PATH, clean[_PLAT_OVERRIDES_PATH])
        if _PLAT_HUMANIZE_PATH in clean:
            clean[_PLAT_HUMANIZE_PATH] = merged_scoped_overrides(
                cfg, _PLAT_HUMANIZE_PATH, clean[_PLAT_HUMANIZE_PATH],
                editable_keys=HUMANIZE_FLAG_KEYS)

        old_values = {k: v for k, v in effective_values(cfg).items() if k in clean}
        saver = getattr(config_manager, "save_overlay_patch", None)
        ok = (saver(nested_patch(clean), replace_paths=REPLACE_PATHS)
              if callable(saver) else False)
        if not ok:
            return {"ok": False,
                    "errors": [{"field": "", "code": "save_failed",
                                "message": tr(request, "rps_err_save_failed")}]}

        # hot=="worker" 的键对活体 worker 热更（构造期固化，写 overlay 传导不到）。
        # 两个独立入口独立成败：deliver_delay 族 / 拟人链开关。
        worker = getattr(getattr(request.app, "state", None),
                         "autosend_worker", None)
        worker_applied: set = set()
        delay_paths = [k for k in clean if k.startswith(_DELAY_PREFIX)]
        if delay_paths:
            apply_fn = getattr(worker, "apply_deliver_delay", None)
            if callable(apply_fn):
                try:
                    apply_fn(merged_delay_block(
                        getattr(config_manager, "config", None) or {}, clean))
                    worker_applied.update(delay_paths)
                except Exception:
                    logger.warning("deliver_delay 热更 worker 失败（重启后生效）",
                                   exc_info=True)
        flag_paths = [k for k in clean if k in _HUMANIZE_FLAGS]
        if flag_paths:
            apply_fn = getattr(worker, "apply_humanize_flags", None)
            if callable(apply_fn):
                try:
                    apply_fn(**{_HUMANIZE_FLAGS[k]: bool(clean[k])
                                for k in flag_paths})
                    worker_applied.update(flag_paths)
                except Exception:
                    logger.warning("拟人链开关热更 worker 失败（重启后生效）",
                                   exc_info=True)
        if _PLAT_HUMANIZE_PATH in clean:
            apply_fn = getattr(worker, "apply_platform_humanize", None)
            if callable(apply_fn):
                try:
                    apply_fn(clean[_PLAT_HUMANIZE_PATH])
                    worker_applied.add(_PLAT_HUMANIZE_PATH)
                except Exception:
                    logger.warning("平台拟人覆写热更 worker 失败（重启后生效）",
                                   exc_info=True)

        live, pending = split_hot_pending(clean, worker_applied=worker_applied)
        actor = str((body or {}).get("actor") or "").strip()
        if not actor:
            try:
                actor = str(request.session.get("username") or "web")
            except Exception:
                actor = "web"
        _append_audit(config_manager, actor=actor, changes=clean,
                      old_values=old_values)

        snap = build_snapshot(getattr(config_manager, "config", None) or {})
        snap.update({"applied_live": live, "needs_restart": pending})
        return _snapshot_extras(snap, request)

    # 一键「改为跟随滑杆」（P0，2026-08-12）：链路自检卡对「独立配置节奏」
    # （黄档 diverged）与「follow:false 秒回」（红档）的收敛动作——把该链独立键
    # 写成 {min_sec:0, max_sec:0, follow:true}，经 humanize.resolve_following_delay_block
    # 单一判定即恢复跟随本页滑杆。语义要点：
    # - 显式带 follow:true：overlay 与桌面种子是深合并，若存量有 follow:false，
    #   只写 0/0 会合并成「显式秒回」而不是跟随——三键一起写才是完整语义；
    # - 这是一次性迁移动作不是旋钮，刻意不进 FIELDS 白名单（进了就变成常驻表单项，
    #   与「减少旋钮」的页面方针相反）；审计走同一 reply_settings_audit.jsonl；
    # - 两个键运行时按消息现读 config → 保存后对新消息即时生效，无需重启。
    _FOLLOW_TARGETS = {
        "native_tg": "telegram.reply_humanize.thinking_delay",
        "protocol": "protocol_autoreply.delay",
    }

    @app.post("/api/reply-settings/follow-slider")
    async def api_reply_settings_follow_slider(
        request: Request, _=Depends(api_auth),
    ):
        try:
            body = await request.json()
        except Exception:
            body = {}
        chain = str((body or {}).get("chain") or "").strip()
        base_path = _FOLLOW_TARGETS.get(chain)
        if not base_path:
            return {"ok": False, "errors": [{
                "field": "chain", "code": "bad_enum",
                "message": tr(request, "rps_err_bad_value", field="chain")}]}

        cfg = getattr(config_manager, "config", None) or {}
        node = cfg
        for part in base_path.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        old_block = dict(node) if isinstance(node, dict) else {}

        flat = {base_path + ".min_sec": 0, base_path + ".max_sec": 0,
                base_path + ".follow": True}
        saver = getattr(config_manager, "save_overlay_patch", None)
        ok = saver(nested_patch(flat)) if callable(saver) else False
        if not ok:
            return {"ok": False, "errors": [{
                "field": "", "code": "save_failed",
                "message": tr(request, "rps_err_save_failed")}]}

        actor = str((body or {}).get("actor") or "").strip()
        if not actor:
            try:
                actor = str(request.session.get("username") or "web")
            except Exception:
                actor = "web"
        _append_audit(
            config_manager, actor=actor, changes=flat,
            old_values={base_path + "." + k: old_block.get(k)
                        for k in ("min_sec", "max_sec", "follow")})

        snap = build_snapshot(getattr(config_manager, "config", None) or {})
        snap.update({"applied_live": sorted(flat), "needs_restart": []})
        return _snapshot_extras(snap, request)
