"""Singing capability admin routes (实施58 P1「歌房」).

Endpoints (all Depends(api_auth); responses key-based via tr(), zero CJK):
  GET  /api/singing/overview                 config + voices + templates + stock matrix + stats
  GET  /api/singing/audio/{pid}/{tid}        stream a stocked song for admin listening
  GET  /api/singing/anchor/{voice_key}       stream a voice anchor take
  POST /api/singing/config                   {key, value} -> overlay companion.singing.* (whitelist)
  POST /api/singing/template-enable          {id, enabled} -> manifest.json toggle

设计不变量：
- 只读面（overview/audio）绝不触发合成——供给一律走 song_factory CLI（GPU 编排
  在 CLI 内，web 进程零 GPU 依赖）；页面给的是「看/听/开关」。
- 开关写入走 config_manager.set_overlay_flag（保注释 overlay 单入口），键白名单
  + 类型钳制，防任意键注入。
- manifest 编辑只翻 enabled 位，其余字段原样保留（供给台账 source 字段不可丢）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# 开关白名单：key -> (类型, 下限, 上限)；bool 无钳制
_CONFIG_SPEC: Dict[str, tuple] = {
    "enabled": ("bool", None, None),
    "daily_cap": ("int", 0, 10),
    "cooldown_hours": ("float", 0.0, 48.0),
    "repeat_window_days": ("int", 0, 30),
    "allow_lang_fallback": ("bool", None, None),
}


def _coerce(key: str, value: Any):
    kind, lo, hi = _CONFIG_SPEC[key]
    if kind == "bool":
        return bool(value)
    num = float(value)
    if kind == "int":
        num = int(num)
    if lo is not None:
        num = max(lo, num)
    if hi is not None:
        num = min(hi, num)
    return num


def _persona_names() -> Dict[str, str]:
    """pid -> 显示名（best-effort：persona_manager 单例的档案摘要；
    任何异常回空表——页面回落显示 pid，绝不因命名美化打挂管理面）。"""
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        return {str(s.get("id")): str(s.get("name") or s.get("id"))
                for s in (pm.list_profiles_summary() or [])}
    except Exception:
        return {}


def register_singing_routes(app, api_auth, config_manager=None):
    from src.companion.song_stock import (
        find_stock_file, load_song_manifest, resolve_singing_cfg,
        singing_status_snapshot, stock_root, templates_dir,
    )
    from scripts.song_factory import load_voices

    def _cfg() -> Dict[str, Any]:
        try:
            return dict(getattr(config_manager, "config", None) or {})
        except Exception:
            return {}

    @app.get("/api/singing/overview")
    async def api_singing_overview(request: Request, _=Depends(api_auth)):
        cfg = _cfg()
        scfg = resolve_singing_cfg(cfg)
        tdir = templates_dir(scfg)
        sroot = stock_root(scfg)
        voices = load_voices(tdir)
        tpls = load_song_manifest(tdir)
        names = _persona_names()
        pids = []
        for v in voices:
            for pid in v["personas"]:
                if pid not in pids:
                    pids.append(pid)
        matrix: Dict[str, Dict[str, Any]] = {}
        for pid in pids:
            row: Dict[str, Any] = {}
            for t in tpls:
                p = find_stock_file(pid, t.id, sroot=sroot)
                if p is None:
                    continue
                cell: Dict[str, Any] = {"ext": p.suffix.lstrip(".")}
                try:
                    meta = json.loads(
                        (p.parent / f"{t.id}.json").read_text(
                            encoding="utf-8")) or {}
                    cell.update(
                        dur=meta.get("duration_sec"),
                        voice=meta.get("casting_voice"),
                        sim=meta.get("sim_vs_anchor",
                                     meta.get("sim_vs_approved_sample")),
                        voice_match=meta.get("voice_match"),
                        supply=meta.get("supply"),
                        rendered_at=meta.get("rendered_at"),
                    )
                except Exception:
                    pass
                row[t.id] = cell
            matrix[pid] = row
        return {
            "ok": True,
            "config": {
                "enabled": bool(scfg.get("enabled")),
                "daily_cap": scfg.get("daily_cap"),
                "cooldown_hours": scfg.get("cooldown_hours"),
                "repeat_window_days": scfg.get("repeat_window_days"),
                "allow_lang_fallback": bool(scfg.get("allow_lang_fallback")),
            },
            "voices": [{
                "key": v["key"], "label": v["label"],
                "canvas_s": v["canvas_s"], "strength": v["strength"],
                "personas": v["personas"],
                "anchor_ok": (tdir / v["anchor"]).is_file(),
            } for v in voices],
            "templates": [{
                "id": t.id, "title": t.title, "lyrics": t.lyrics,
                "lang": t.lang, "scene": t.scene, "source": t.source,
                "enabled": t.enabled,
            } for t in tpls],
            "persona_names": {pid: names.get(pid, pid) for pid in pids},
            "matrix": matrix,
            "stats": singing_status_snapshot(cfg),
        }

    def _serve_audio(path: Path):
        media = "audio/ogg" if path.suffix == ".ogg" else "audio/wav"
        return FileResponse(str(path), media_type=media, filename=path.name)

    @app.get("/api/singing/audio/{persona_id}/{template_id}")
    async def api_singing_audio(persona_id: str, template_id: str,
                                request: Request, _=Depends(api_auth)):
        if not _ID_RE.match(persona_id or "") or not _ID_RE.match(
                template_id or ""):
            raise HTTPException(400, tr(request, "err.singing.bad_id",
                                        "invalid id"))
        scfg = resolve_singing_cfg(_cfg())
        p = find_stock_file(persona_id, template_id,
                            sroot=stock_root(scfg))
        if p is None:
            raise HTTPException(404, tr(request, "err.singing.no_stock",
                                        "no stock"))
        return _serve_audio(p)

    @app.get("/api/singing/anchor/{voice_key}")
    async def api_singing_anchor(voice_key: str, request: Request,
                                 _=Depends(api_auth)):
        if not _ID_RE.match(voice_key or ""):
            raise HTTPException(400, tr(request, "err.singing.bad_id",
                                        "invalid id"))
        tdir = templates_dir(resolve_singing_cfg(_cfg()))
        for v in load_voices(tdir):
            if v["key"] == voice_key:
                p = tdir / v["anchor"]
                if not p.is_file():
                    raise HTTPException(404, tr(
                        request, "err.singing.no_anchor", "anchor missing"))
                return _serve_audio(p)
        raise HTTPException(404, tr(request, "err.singing.no_voice",
                                    "voice not found"))

    @app.post("/api/singing/config")
    async def api_singing_config(request: Request, _=Depends(api_auth)):
        if config_manager is None:
            raise HTTPException(503, tr(request, "err.singing.no_cfgmgr",
                                        "config manager unavailable"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        key = str((body or {}).get("key") or "")
        if key not in _CONFIG_SPEC:
            raise HTTPException(400, tr(request, "err.singing.bad_key",
                                        "key not allowed"))
        try:
            value = _coerce(key, (body or {}).get("value"))
        except (TypeError, ValueError):
            raise HTTPException(400, tr(request, "err.singing.bad_value",
                                        "bad value"))
        ok, msg = config_manager.set_overlay_flag(
            f"companion.singing.{key}", value)
        if not ok:
            raise HTTPException(500, str(msg)[:200])
        try:
            actor = str(request.session.get("username") or "?")
        except Exception:                      # 无 SessionMiddleware（测试装配）
            actor = "?"
        logger.info("[singing] config %s=%r by %s", key, value, actor)
        return {"ok": True, "key": key, "value": value}

    @app.post("/api/singing/template-enable")
    async def api_singing_template_enable(request: Request,
                                          _=Depends(api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        tid = str((body or {}).get("id") or "")
        enabled = bool((body or {}).get("enabled"))
        if not _ID_RE.match(tid):
            raise HTTPException(400, tr(request, "err.singing.bad_id",
                                        "invalid id"))
        tdir = templates_dir(resolve_singing_cfg(_cfg()))
        mf = tdir / "manifest.json"
        try:
            data = json.loads(mf.read_text(encoding="utf-8")) or {}
        except Exception:
            raise HTTPException(404, tr(request, "err.singing.no_manifest",
                                        "manifest missing"))
        hit = False
        for row in data.get("templates") or []:
            if isinstance(row, dict) and str(row.get("id")) == tid:
                row["enabled"] = enabled
                hit = True
        if not hit:
            raise HTTPException(404, tr(request, "err.singing.no_template",
                                        "template not found"))
        mf.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                      encoding="utf-8")
        logger.info("[singing] template %s enabled=%s", tid, enabled)
        return {"ok": True, "id": tid, "enabled": enabled}
