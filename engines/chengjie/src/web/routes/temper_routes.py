"""被骂回怼治理 API（``companion.temper``，2026-08-12）。

挂 ``/api/companion/temper/*``。三个端点服务「看清楚 → 调档 → 自查」闭环：

  GET  /api/companion/temper/status   —— 治理配置 + 每人设生效档位表 + 观测快照
  POST /api/companion/temper/config   —— 写 enabled/force_level/default_level/
       profanity（config overlay，ruamel 保注释；复用陪伴能力审计账本）
  POST /api/companion/temper/dry-run  —— 回怼诊断器：一句话 + 人设/会话 →
       判定链（词表命中/敌意/缓和/生效档位来源/将注入的指令预览）。
       2026-08-12 阿龙事故复盘的直接产物：「为什么没怼」从翻库两小时
       变成运营 10 秒自查。只读零副作用（不写观测计数、不动粘性窗）。

人设级档位的唯一事实源是人设文件 ``temper`` 字段（Studio 编辑）——本 API
刻意不提供 per-persona 写入口，防与人设编辑器双头写。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.temper_routes")

_ROLE_VIEWER = "viewer"

# POST /config 允许写的键 → overlay 路径（白名单，其余一律拒绝）
_CONFIG_KEYS = {
    "enabled": "companion.temper.enabled",
    "force_level": "companion.temper.force_level",
    "default_level": "companion.temper.default_level",
    "profanity": "companion.temper.profanity",
    "max_rounds": "companion.temper.max_rounds",
    "taunt_response": "companion.temper.taunt_response",
}


def register_temper_routes(app, api_auth, config_manager=None):
    """挂载被骂回怼治理 API。``api_auth``＝登录校验依赖。"""

    def _cm(request: Request):
        cm = config_manager
        if cm is None:
            cm = getattr(request.app.state, "config_manager", None)
        return cm

    def _full_cfg(request: Request) -> Dict[str, Any]:
        cm = _cm(request)
        cfg = getattr(cm, "config", None) if cm is not None else None
        return cfg if isinstance(cfg, dict) else {}

    def _require_write(request: Request) -> None:
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.temper.readonly"))

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web-admin")
        except Exception:
            return "web-admin"

    def _persona_rows(temper_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """每人设生效档位表（含来源），供治理面板 chips / 诊断下拉。"""
        from src.companion.temper import resolve_temper_level
        rows: List[Dict[str, Any]] = []
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            for meta in pm.list_profiles_summary():
                pid = str(meta.get("id") or "").strip()
                if not pid:
                    continue
                persona = pm.get_persona_by_id(pid)
                lv = resolve_temper_level(temper_cfg, persona)
                rows.append({
                    "id": pid,
                    "name": str(meta.get("name") or pid),
                    "level": lv["level"],
                    "source": lv["source"],
                    "profanity_capped": bool(lv["profanity_capped"]),
                })
        except Exception:
            logger.debug("temper persona rows 汇总失败", exc_info=True)
        return rows

    @app.get("/api/companion/temper/status")
    async def api_temper_status(request: Request, _=Depends(api_auth)):
        """治理配置 + 每人设生效档位 + 进程观测快照（只读）。"""
        from src.companion.temper import (
            parse_temper_cfg, temper_stats_snapshot,
        )
        cfg = _full_cfg(request)
        tcfg = parse_temper_cfg(cfg)
        return {
            "ok": True,
            "config": tcfg,
            "personas": _persona_rows(tcfg),
            "stats": temper_stats_snapshot(),
        }

    @app.post("/api/companion/temper/config")
    async def api_temper_config(request: Request, _=Depends(api_auth)):
        """写治理键（白名单）到 config overlay；复用陪伴能力审计账本。

        body: ``{enabled?, force_level?, default_level?, profanity?}``——
        只写出现的键。档位值经 temper 模块校验（force 允许空串=按人设）。
        """
        from src.companion.temper import TEMPER_LEVELS, parse_temper_cfg
        _require_write(request)
        cm = _cm(request)
        if cm is None or not isinstance(getattr(cm, "config", None), dict):
            raise HTTPException(503, tr(request, "err.temper.config_na"))
        if not hasattr(cm, "set_overlay_flag"):
            raise HTTPException(503, tr(request, "err.temper.overlay_na"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}

        writes: List[Dict[str, Any]] = []
        for key, path in _CONFIG_KEYS.items():
            if key not in body:
                continue
            raw = body.get(key)
            if key in ("enabled", "profanity", "taunt_response"):
                value: Any = bool(raw)
            elif key == "max_rounds":
                try:
                    value = max(0, min(20, int(raw)))
                except Exception:
                    raise HTTPException(400, tr(
                        request, "err.temper.bad_rounds", value=str(raw)))
            else:
                value = str(raw or "").strip().lower()
                if value and value not in TEMPER_LEVELS:
                    raise HTTPException(400, tr(
                        request, "err.temper.bad_level", level=value))
                if key == "default_level" and not value:
                    value = "gentle"
            writes.append({"key": key, "path": path, "value": value})
        if not writes:
            raise HTTPException(400, tr(request, "err.temper.no_keys"))

        actor = _actor(request)
        applied = []
        for w in writes:
            ok, msg = cm.set_overlay_flag(w["path"], w["value"])
            if not ok:
                raise HTTPException(500, tr(
                    request, "err.temper.write_failed",
                    name=w["key"], detail=str(msg or "")))
            applied.append(w["key"])
            try:
                from src.web.routes.companion_capability_routes import (
                    _audit_toggle,
                )
                _audit_toggle(
                    cm, actor=actor, key="temper_comeback",
                    field=w["key"], value=w["value"], path=w["path"],
                    reason="temper-governance")
            except Exception:
                logger.debug("temper 审计写入失败（忽略）", exc_info=True)
        logger.info("[temper] 治理配置变更 actor=%s keys=%s", actor, applied)

        tcfg = parse_temper_cfg(_full_cfg(request))
        return {"ok": True, "applied": applied, "config": tcfg,
                "personas": _persona_rows(tcfg)}

    @app.post("/api/companion/temper/dry-run")
    async def api_temper_dry_run(request: Request, _=Depends(api_auth)):
        """回怼诊断器（只读，零副作用——不写计数不动粘性窗）。

        body: ``{text, persona_id?, conversation_id?, lang?}``。
        conversation_id（``platform:account:chat_key`` 3 段）优先——按出站链
        同一条 ``resolve_effective_persona`` 解析生效人设 + 首段平台参与
        platform_caps 封顶判定，回答「这条会话为什么怼/没怼」；否则用显式
        persona_id（无平台上下文不判封顶）；都没有＝只看词表判定。
        返回 verdict 枚举供前端渲染：inject / inject_if_sticky /
        clear_window / suppressed_off / taunt_banter / none / disabled。
        """
        from src.companion.temper import (
            apply_platform_cap, build_taunt_hint, build_temper_hint,
            detect_insult, detect_taunt, is_de_escalation, looks_hostile,
            parse_temper_cfg, resolve_temper_level,
        )
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, tr(request, "err.temper.no_text"))
        lang = str(body.get("lang") or "zh").strip().lower() or "zh"

        cfg = _full_cfg(request)
        tcfg = parse_temper_cfg(cfg)

        # 生效人设：conversation_id（出站链同源解析）> persona_id 显式指定
        pid = str(body.get("persona_id") or "").strip()
        persona_tier = "explicit" if pid else ""
        platform = ""
        conv_id = str(body.get("conversation_id") or "").strip()
        if conv_id:
            parts = conv_id.split(":", 2)
            if len(parts) == 3 and all(p.strip() for p in parts):
                platform = parts[0].strip().lower()
                if not pid:
                    try:
                        from src.ai.persona_voice import (
                            resolve_effective_persona,
                        )
                        pid, persona_tier = resolve_effective_persona(
                            cfg, parts[0], parts[1], parts[2])
                    except Exception:
                        logger.debug(
                            "dry-run 会话人设解析失败", exc_info=True)
            else:
                raise HTTPException(400, tr(request, "err.temper.bad_conv"))

        persona: Optional[Dict[str, Any]] = None
        persona_name = ""
        if pid:
            try:
                from src.utils.persona_manager import PersonaManager
                persona = PersonaManager.get_instance().get_persona_by_id(pid)
                persona_name = str((persona or {}).get("name") or pid)
            except Exception:
                persona = None

        insult = detect_insult(text)
        hostile = looks_hostile(text)
        deesc = is_de_escalation(text)
        taunt = detect_taunt(text)
        lv = resolve_temper_level(tcfg, persona)
        cap = apply_platform_cap(lv["level"], platform, tcfg["platform_caps"])
        eff_level = cap["level"]
        hint = build_temper_hint(eff_level, lang)

        if not tcfg["enabled"]:
            verdict = "disabled"
        elif insult:
            verdict = "inject" if hint else "suppressed_off"
        elif deesc:
            verdict = "clear_window"
        elif hostile:
            verdict = "inject_if_sticky" if hint else "suppressed_off"
        elif taunt and tcfg["taunt_response"]:
            hint = build_taunt_hint(eff_level, lang)
            verdict = "taunt_banter" if hint else "suppressed_off"
        else:
            verdict = "none"

        return {
            "ok": True,
            "verdict": verdict,
            "checks": {
                "insult": insult, "hostile": hostile,
                "deescalation": deesc, "taunt": taunt,
            },
            "persona": {
                "id": pid, "name": persona_name, "tier": persona_tier,
                "found": persona is not None,
            },
            "level": eff_level,
            "source": lv["source"],
            "profanity_capped": bool(lv["profanity_capped"]),
            "platform_capped": bool(cap["capped"]),
            "sticky_window_sec": tcfg["sticky_window_sec"],
            "hint_preview": hint,
        }
