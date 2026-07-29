"""陪伴主动话题调度·可观测预览 API（dry-run 观测，无副作用）。

端点：
  GET /api/companion/proactive/preview?limit=  本轮"会主动联系谁、引用哪条记忆、
      带哪些背景事实、是否被 care 去重让路"——不发送、不写冷却，即便功能未启用也可看。

预览能力由 main 在启动时挂到 ``app.state.companion_proactive_preview``（一个无参/可选
limit 的可调用对象）。未就绪（inbox/skill 未起）时该属性缺失，端点返回 available=false，
不报错——与本仓"子系统未就绪软降级"约定一致。
"""
from __future__ import annotations

import logging

from fastapi import Depends, Request

logger = logging.getLogger(__name__)


def register_companion_proactive_routes(app, *, api_auth) -> None:
    @app.get("/api/companion/proactive/preview")
    async def api_companion_proactive_preview(
        request: Request, limit: int = 50, _=Depends(api_auth),
    ):
        """主动话题本轮候选预览（dry-run）。不触发发送。"""
        fn = getattr(request.app.state, "companion_proactive_preview", None)
        if fn is None:
            return {
                "ok": True, "available": False, "enabled": False,
                "plans": [], "candidates": 0,
                "message": "主动话题预览未就绪（inbox/skill_manager 未起或未配置 companion）",
            }
        try:
            data = fn(limit=max(1, min(int(limit or 50), 200)))
        except Exception:
            logger.warning("companion proactive preview 失败", exc_info=True)
            return {"ok": False, "available": True, "plans": [],
                    "message": "预览计算失败"}
        out = {"ok": True, "available": True}
        out.update(data if isinstance(data, dict) else {})
        return out

    @app.post("/api/companion/proactive/sample")
    async def api_companion_proactive_sample(request: Request, _=Depends(api_auth)):
        """试发采样：对某会话生成 AI 实际会发的那句话，但**不发送**（开闸前先读文案）。

        body: ``{conversation_id}``。会真实调用一次 AI（有 token 成本），无发送/无写冷却。
        """
        fn = getattr(request.app.state, "companion_proactive_generate", None)
        if fn is None:
            return {"ok": True, "available": False, "generated": False,
                    "message": "试发未就绪（inbox/skill_manager 未起或 ai 未就绪）"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = str((body or {}).get("conversation_id") or "").strip()
        if not cid:
            return {"ok": False, "generated": False, "message": "缺 conversation_id"}
        # Stage O：slot ∈ {morning,night} → 试发每日仪式问候（晨/晚安）；空 → 沉默回访开场。
        slot = str((body or {}).get("slot") or "").strip().lower()
        try:
            data = await fn(cid, slot) if slot else await fn(cid)
        except Exception:
            logger.warning("companion proactive sample 失败", exc_info=True)
            return {"ok": False, "generated": False, "message": "生成失败"}
        out = {"ok": True, "available": True}
        out.update(data if isinstance(data, dict) else {})
        return out

    def _sample_store(request: Request):
        return getattr(request.app.state, "companion_sample_store", None)

    @app.post("/api/companion/proactive/sample/{sid}/rate")
    async def api_companion_proactive_rate(sid: int, request: Request, _=Depends(api_auth)):
        """对一条试发采样评分（质量回流）。body: {rating: up|down, edited_text?, note?}。"""
        store = _sample_store(request)
        if store is None:
            return {"ok": False, "message": "评分存储未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        rating = str((body or {}).get("rating") or "").strip().lower()
        if rating not in ("up", "down"):
            return {"ok": False, "message": "rating 须为 up 或 down"}
        ok = store.rate(
            int(sid), rating,
            edited_text=str((body or {}).get("edited_text") or "")[:500],
            note=str((body or {}).get("note") or "")[:500],
        )
        if not ok:
            return {"ok": False, "message": "采样不存在或评分失败"}
        return {"ok": True, "rated": int(sid), "rating": rating}

    @app.get("/api/companion/proactive/samples")
    async def api_companion_proactive_samples(
        request: Request, limit: int = 50, rating: str = "", _=Depends(api_auth),
    ):
        """采样列表 + 聚合统计（好评率 / 按 mode 分），供调 prompt/阈值。"""
        store = _sample_store(request)
        if store is None:
            return {"ok": True, "available": False, "items": [], "stats": {}}
        items = store.list_recent(limit=max(1, min(int(limit or 50), 500)),
                                  rating=str(rating or ""))
        return {"ok": True, "available": True, "items": items, "stats": store.stats()}

    @app.get("/api/companion/proactive/tuning-advice")
    async def api_companion_proactive_tuning_advice(
        request: Request, _=Depends(api_auth),
    ):
        """基于采样评分给"调参建议"（只读、人审）：按 mode 好评率 + 针对性建议 +
        few-shot 候选（高赞文案 / 差评改写）。绝不自动改配置。"""
        from src.integrations.companion_sample_store import build_tuning_advice

        store = _sample_store(request)
        if store is None:
            return {"ok": True, "available": False, "advice": {}}
        rated = store.list_recent(limit=200, rating="up") \
            + store.list_recent(limit=200, rating="down")
        advice = build_tuning_advice(store.stats(), rated)
        return {"ok": True, "available": True, "advice": advice}

    @app.get("/api/companion/proactive/status")
    async def api_companion_proactive_status(
        request: Request, _=Depends(api_auth),
    ):
        """主动触达运行态（观测用）：tick 统计 + bootstrap + 配置 + 候选预览摘要。"""
        out: dict = {"ok": True}
        try:
            from src.companion.proactive_stats import metrics_snapshot as _ps
            out["proactive"] = _ps()
        except Exception:
            out["proactive"] = {}
        try:
            from src.inbox.automation_mode_stats import metrics_snapshot as _bs
            out["automation_bootstrap"] = _bs()
        except Exception:
            out["automation_bootstrap"] = {}
        try:
            cm = getattr(request.app.state, "config_manager", None)
            cfg = getattr(cm, "config", None) or {}
            pt = ((cfg.get("companion") or {}).get("proactive_topic") or {})
            from src.utils.proactive_pacing import (
                parse_adaptive_pacing_cfg,
                parse_no_reply_backoff_cfg,
                parse_response_pacing_cfg,
            )
            _vr = pt.get("variety") if isinstance(pt.get("variety"), dict) else {}
            _oo = pt.get("optout") if isinstance(pt.get("optout"), dict) else {}
            out["config"] = {
                "enabled": bool(pt.get("enabled", False)),
                "dry_run": bool(pt.get("dry_run", False)),
                "min_silent_hours": pt.get("min_silent_hours"),
                "cooldown_hours": pt.get("cooldown_hours"),
                "voice_enabled": bool((pt.get("voice") or {}).get("enabled", False)),
                "foreign_voice": bool(
                    ((pt.get("voice") or {}).get("foreign") or {}).get("enabled", False)),
                "photo_enabled": bool((pt.get("photo") or {}).get("enabled", False)),
                "adaptive_pacing": parse_adaptive_pacing_cfg(pt),
                # P1：退避/变体/退订配置回显（看板一眼看开关与阈值是否生效）
                "no_reply_backoff": parse_no_reply_backoff_cfg(pt),
                "variety": {
                    "enabled": bool(_vr.get("enabled", True)),
                    "similarity_threshold": float(
                        _vr.get("similarity_threshold", 0.6) or 0.6),
                },
                "optout": {
                    "enabled": bool(_oo.get("enabled", True)),
                    "mute_days": float(_oo.get("mute_days", 30) or 30),
                },
                # P2：回复率反哺配置回显
                "response_pacing": parse_response_pacing_cfg(pt),
            }
            # P3：媒体形态反哺——三臂（text 基线/voice/photo）回复率与生效倍率。
            # 与编排器同一纯函数口径（collect_media_feedback），看板读数即真值。
            try:
                from src.companion.proactive_media_feedback import (
                    collect_media_feedback,
                    parse_media_feedback_cfg,
                )
                _mfb = parse_media_feedback_cfg(pt)
                out["config"]["media_feedback"] = _mfb
                if _mfb.get("enabled"):
                    _st_fb = getattr(request.app.state, "inbox_store", None)
                    out["media_feedback"] = collect_media_feedback(_st_fb, _mfb)
            except Exception:
                logger.debug("proactive status media_feedback 段失败", exc_info=True)
            # P5：checkin 效能门控——两臂回复率与本日跳过概率（与引擎同口径）。
            try:
                from src.companion.proactive_mode_gate import (
                    collect_mode_gate,
                    parse_mode_gate_cfg,
                )
                _mg = parse_mode_gate_cfg(pt)
                out["config"]["mode_gate"] = _mg
                if _mg.get("enabled"):
                    _st_mg = getattr(request.app.state, "inbox_store", None)
                    out["mode_gate"] = collect_mode_gate(_st_mg, _mg)
            except Exception:
                logger.debug("proactive status mode_gate 段失败", exc_info=True)
        except Exception:
            out["config"] = {}
        # Phase17：photo/voice/text 分形态回复率（A/B 评估）。数据源=outreach_log
        # （_send 成功即落 batch_id=proactive_topic:<kind>），复用 P61 回执统计口径：
        # 触达后 72h 内该会话有入站=已回复。纯查询，无写放大。
        try:
            st = getattr(request.app.state, "inbox_store", None)
            if st is not None and hasattr(st, "outreach_response_stats"):
                ab = {}
                for kind in ("photo", "voice", "text"):
                    r = st.outreach_response_stats(
                        f"proactive_topic:{kind}", response_window_days=3.0)
                    ab[kind] = {
                        "sent": int(r.get("sent") or 0),
                        "responded": int(r.get("responded") or 0),
                        "response_rate": r.get("response_rate"),
                        "avg_response_minutes": r.get("avg_response_minutes"),
                    }
                out["kind_ab"] = ab
        except Exception:
            out["kind_ab"] = {}
        # P2：mode 分布落库口径（近 14 天 outreach_log note=mode）——进程计数
        # 重启即清零（本机重启频繁），DB 口径重启后照样能回看「checkin 是否还
        # 占 100%」。进程计数保留作 fallback（DB 无数据/旧库）。
        try:
            st = getattr(request.app.state, "inbox_store", None)
            if st is not None and hasattr(st, "outreach_mode_histogram"):
                out["sent_modes_14d"] = st.outreach_mode_histogram(
                    "proactive_topic:", days=14.0)
        except Exception:
            out["sent_modes_14d"] = {}
        # P4：mode 级回复率（近 14 天，触达后 72h 有入站=回复）——「哪种开场
        # 客户真的会回」读数化；P5 若做 mode 门控/选型反哺，判据就从这里来。
        try:
            st = getattr(request.app.state, "inbox_store", None)
            if st is not None and hasattr(st, "outreach_note_response_stats"):
                out["mode_ab"] = st.outreach_note_response_stats(
                    "proactive_topic:", response_window_days=3.0,
                    lookback_days=14.0)
        except Exception:
            out["mode_ab"] = {}
        # P4：few_shot 就绪度——评分样本攒到阈值前开开关是空转；看板给出
        # 「已评 n/阈值」提示，运营知道差多少。阈值只是提示口径，非硬闸。
        try:
            _ss = getattr(request.app.state, "companion_sample_store", None)
            if _ss is not None and hasattr(_ss, "stats"):
                _sst = _ss.stats() or {}
                _fs_cfg = (pt.get("few_shot") or {}) if isinstance(
                    pt.get("few_shot"), dict) else {}
                out["few_shot"] = {
                    "enabled": bool(_fs_cfg.get("enabled", False)),
                    "total": int(_sst.get("total") or 0),
                    "rated": int(_sst.get("rated") or 0),
                    "up": int(_sst.get("up") or 0),
                    "down": int(_sst.get("down") or 0),
                    "ready_threshold": 15,
                }
        except Exception:
            logger.debug("proactive status few_shot 段失败", exc_info=True)
        # P1（2026-07-29）：未回退避账本直方图 + opt-out 静默数——「死联系人还剩
        # 多少在退避中 / 有多少人明确说了别发」从翻 JSON 文件变成看板读数。
        # 直读落盘文件（与调度进程零耦合，路由侧无状态）。
        try:
            import json as _json
            import time as _time
            from pathlib import Path as _Path

            from src.integrations.companion_proactive import JsonProactiveLedger
            cm = getattr(request.app.state, "config_manager", None)
            _cfgp = getattr(cm, "config_path", None)
            if _cfgp:
                _dir = _Path(_cfgp).parent
                led = JsonProactiveLedger(
                    _dir / "companion_proactive_cooldown.json").snapshot()
                hist = {"entries": len(led),
                        "streak1": 0, "streak2": 0, "streak3plus": 0}
                for v in led.values():
                    s = int((v or {}).get("streak") or 0)
                    if s == 1:
                        hist["streak1"] += 1
                    elif s == 2:
                        hist["streak2"] += 1
                    elif s >= 3:
                        hist["streak3plus"] += 1
                out["backoff_ledger"] = hist
                # P6：回复率阈值生效值（auto_thresholds 开 → 账本人群分位数校准；
                # 人群不足/同质/未开 → 配置固定值）。与规划器同一纯函数同一数据。
                try:
                    from src.utils.proactive_pacing import (
                        calibrate_response_thresholds,
                        parse_response_pacing_cfg as _prp,
                    )
                    _rp = _prp(pt)
                    out["response_thresholds"] = calibrate_response_thresholds(
                        [(e.get("obs_n", 0), e.get("obs_replied", 0))
                         for e in led.values()], _rp)
                except Exception:
                    logger.debug("proactive status 阈值校准段失败", exc_info=True)
                _muted = 0
                _mp = _dir / "companion_optout_mute.json"
                if _mp.exists():
                    _mm = _json.loads(_mp.read_text("utf-8")) or {}
                    _nowt = _time.time()
                    _muted = sum(
                        1 for v in _mm.values()
                        if isinstance(v, dict)
                        and float(v.get("until") or 0) > _nowt)
                out["optout"] = {"muted": _muted}
        except Exception:
            logger.debug("proactive status ledger 段失败", exc_info=True)
        fn = getattr(request.app.state, "companion_proactive_preview", None)
        if fn is not None:
            try:
                prev = fn(limit=20)
                if isinstance(prev, dict):
                    out["preview"] = {
                        "candidates": prev.get("candidates", 0),
                        "scanned": prev.get("scanned", 0),
                        "enabled": prev.get("enabled", False),
                    }
                    _plans = prev.get("plans") or []
                    out["preview"]["sample"] = [
                        {
                            "conversation_id": p.get("conversation_id"),
                            "stage": p.get("stage"),
                            "intimacy": p.get("intimacy"),
                            "effective_min_silent_hours": p.get(
                                "effective_min_silent_hours"),
                            "silent_hours": p.get("silent_hours"),
                            "mode": p.get("mode"),
                            # P0/P1 观测：未回连发次数 + 沉默分档（看板一眼可见）
                            "unanswered_streak": p.get("unanswered_streak"),
                            "gap_bucket": p.get("gap_bucket"),
                        }
                        for p in _plans[:5]
                        if isinstance(p, dict)
                    ]
            except Exception:
                out["preview"] = {"candidates": -1}
        else:
            out["preview"] = {"available": False}
        return out


__all__ = ["register_companion_proactive_routes"]
