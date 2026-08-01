"""Phase O4：主动关怀待办 Web API。

把 `CareScheduleStore` 暴露给后台：看「待关怀/已发/跳过(含原因)/过期」+ 手动加/取消。
读写都过 `api_auth`（后台管理面）。store 经 app.state 注入，缺则按 config 目录懒建单例。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import Depends, Request

logger = logging.getLogger(__name__)


def register_care_routes(app, *, api_auth, config_manager=None) -> None:
    def _store(request: Request):
        st = getattr(request.app.state, "care_schedule_store", None)
        if st is not None:
            return st
        from src.contacts.care_schedule import get_care_schedule_store
        db_path = ":memory:"
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            base = Path(getattr(cm, "config_path", "") or "").parent
            if str(base):
                db_path = base / "care_schedule.db"
        except Exception:
            db_path = ":memory:"
        st = get_care_schedule_store(db_path)
        request.app.state.care_schedule_store = st
        return st

    def _summary(store) -> dict:
        return {s: store.count(status=s)
                for s in ("pending", "sent", "skipped", "expired", "cancelled")}

    def _cm(request: Request):
        return getattr(request.app.state, "config_manager", None) or config_manager

    def _care_cfg(request: Request) -> dict:
        cm = _cm(request)
        conf = getattr(cm, "config", None) or {}
        return dict((conf.get("companion") or {}).get("proactive_care") or {})

    # ── P2：48h 回复率（效果回流）。逐条查会话消息 → 60s 进程内缓存防健康轮询打库 ──
    _effect_memo = {"ts": 0.0, "data": {}}

    def _compute_effect(request: Request, store, now: float) -> dict:
        """近 7 天真发关怀（note=deferred:*，dry_run 不算）的 48h 回复率。

        逐条判定：sent_at 后 48h 窗内该会话有无入站。未满窗且尚未回复的条目
        不进分母（immature），已回复的提前计入——与主动触达 outreach 判定同哲学。
        inbox store 不可用 → 返回 {}（页面隐藏该行，不装数据）。
        """
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            return {}
        try:
            rows = store.list_recent(status="sent", limit=300)
        except Exception:
            return {}
        week = [r for r in rows
                if float(r.get("sent_at") or 0) >= now - 7 * 86400.0
                and str(r.get("note") or "").startswith("deferred:")]
        matured = replied = immature = 0
        for r in week:
            sat = float(r.get("sent_at") or 0)
            cid = str(r.get("contact_key") or "")
            got = False
            try:
                msgs = inbox.list_recent_messages(cid, limit=100) or []
                got = any(
                    str(m.get("direction") or "") == "in"
                    and sat < float(m.get("ts") or 0) <= sat + 48 * 3600.0
                    for m in msgs)
            except Exception:
                got = False
            if got:
                matured += 1
                replied += 1
            elif now - sat >= 48 * 3600.0:
                matured += 1
            else:
                immature += 1
        return {
            "window_days": 7,
            "sent_7d": len(week),
            "matured": matured,
            "replied": replied,
            "immature": immature,
            "rate": (round(replied / matured, 3) if matured else None),
        }

    def _effect_cached(request: Request, store) -> dict:
        now = time.time()
        if now - float(_effect_memo["ts"]) < 60.0:
            return dict(_effect_memo["data"])
        data = _compute_effect(request, store, now)
        _effect_memo["ts"] = now
        _effect_memo["data"] = data
        return dict(data)

    # ── P0 2026-08-01：链路自检 + 一键开闸（配合 background_tasks 常备接线）──
    @app.get("/api/care/health")
    async def api_care_health(request: Request, _=Depends(api_auth)):
        """关怀引擎四灯自检：引擎开关 / 入站捕获 / 到期派发 / 发送通道 + 活动读数。

        响应只出结构化状态码（无文案），措辞由前端 i18n 渲染。
        """
        cm = _cm(request)
        conf = getattr(cm, "config", None) or {}
        comp = (conf.get("companion") or {})
        care_cfg = dict(comp.get("proactive_care") or {})
        enabled = bool(care_cfg.get("enabled", False))
        engine = getattr(request.app.state, "care_engine", None) or {}
        dispatcher = engine.get("dispatcher")
        dispatch = {"running": False, "last_tick_ts": 0.0,
                    "skip": str(engine.get("dispatcher_skip") or "")}
        if dispatcher is not None:
            try:
                dispatch.update(dispatcher.health_snapshot())
            except Exception:
                logger.debug("care dispatcher snapshot 失败", exc_info=True)
        store = _store(request)
        now = time.time()
        mdef = dict(comp.get("multiplatform_deferred") or {})
        # P2：LLM 影子抽取快照（未接线/未启用 → {}，前端隐藏该行）
        shadow = {}
        sc = engine.get("shadow_scanner")
        if sc is not None:
            try:
                shadow = sc.snapshot()
            except Exception:
                logger.debug("care shadow snapshot 失败", exc_info=True)
        return {
            "ok": True,
            "enabled": enabled,
            "dry_run": bool(care_cfg.get("dry_run", False)),
            "capture": {
                "config_on": enabled and bool(care_cfg.get("capture", True)),
                "wired": bool(engine.get("capture_wired", False)),
            },
            "dispatch": dispatch,
            "delivery": {
                "multiplatform_deferred": bool(mdef.get("enabled", False)),
                "messenger_rpa": bool(engine.get("messenger_rpa", False)),
            },
            "activity": {
                "captured_24h": store.count_created_since(now - 86400.0),
                "last_captured_ts": store.last_created_at(),
                "summary": _summary(store),
            },
            "shadow": shadow,
            "effect": _effect_cached(request, store),
        }

    _ENGINE_ACTIONS = ("enable_dry", "go_live", "pause")

    def _engine_audit(cm, *, actor: str, action: str, applied: list) -> None:
        """开闸动作追加到 companion_capability_audit.jsonl（与能力开关同一台账，best-effort）。"""
        try:
            import json as _json
            base = Path(getattr(cm, "config_path", "") or "").parent
            if not str(base):
                return
            rec = {"ts": round(time.time(), 3), "actor": actor,
                   "key": "proactive_care.engine", "field": action,
                   "value": True, "path": ";".join(applied), "reason": "care_engine_api"}
            with open(base / "companion_capability_audit.jsonl", "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("care engine 审计写入失败（忽略）", exc_info=True)

    @app.post("/api/care/engine")
    async def api_care_engine(request: Request, _=Depends(api_auth)):
        """一键开/关关怀引擎（写 config.local.yaml overlay，热重载 ~30s 生效，免重启）。

        body: {action: enable_dry|go_live|pause, actor?}。路径为**硬编码白名单**（非用户输入）：
        - enable_dry → proactive_care.enabled=true + dry_run=true + multiplatform_deferred.enabled=true
          （灰度档：捕获+到期拟稿全开，但只记样本不真发）
        - go_live → 前提当前 enabled，否则拒绝（强制先走灰度）→ dry_run=false
        - pause → proactive_care.enabled=false（捕获与派发一起停）
        """
        cm = _cm(request)
        if cm is None or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "reason": "config_unavailable"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        action = str(body.get("action") or "").strip()
        actor = (str(body.get("actor") or "").strip() or "web-admin")
        if action not in _ENGINE_ACTIONS:
            return {"ok": False, "reason": "bad_action", "actions": list(_ENGINE_ACTIONS)}

        care_cfg = _care_cfg(request)
        if action == "go_live" and not bool(care_cfg.get("enabled", False)):
            return {"ok": False, "reason": "not_enabled"}

        flags: list = []
        if action == "enable_dry":
            flags = [("companion.proactive_care.enabled", True),
                     ("companion.proactive_care.dry_run", True),
                     ("companion.multiplatform_deferred.enabled", True)]
        elif action == "go_live":
            flags = [("companion.proactive_care.dry_run", False),
                     ("companion.multiplatform_deferred.enabled", True)]
        elif action == "pause":
            flags = [("companion.proactive_care.enabled", False)]

        applied, failed = [], []
        for path, value in flags:
            ok, msg = cm.set_overlay_flag(path, value)
            if ok:
                applied.append(path)
            else:
                failed.append({"path": path, "reason": str(msg)})
        if applied:
            _engine_audit(cm, actor=actor, action=action, applied=applied)
        eff = _care_cfg(request)
        return {
            "ok": not failed,
            "action": action,
            "applied": applied,
            "failed": failed,
            "effective": {"enabled": bool(eff.get("enabled", False)),
                          "dry_run": bool(eff.get("dry_run", False))},
            "hot_reload_sec": 30,
        }

    @app.get("/api/care/schedule")
    async def api_care_schedule_list(
        request: Request, status: str = "", limit: int = 100, _=Depends(api_auth),
    ):
        """关怀待办列表 + 各状态计数。status 空=全部。"""
        store = _store(request)
        lim = max(1, min(int(limit or 100), 500))
        items = store.list_recent(status=status.strip(), limit=lim)
        return {"ok": True, "items": items, "count": len(items),
                "summary": _summary(store)}

    @app.get("/api/care/schedule/due")
    async def api_care_schedule_due(request: Request, limit: int = 100, _=Depends(api_auth)):
        """当前到期且仍 pending 的待办（预览到点会发什么）。"""
        store = _store(request)
        items = store.list_due(limit=max(1, min(int(limit or 100), 500)))
        return {"ok": True, "items": items, "count": len(items)}

    @app.post("/api/care/schedule")
    async def api_care_schedule_add(request: Request, _=Depends(api_auth)):
        """运营手动加一条关怀（AI 没抽到的约定）。body：
        {contact_key, platform, account_id, chat_key, topic, due_at?|due_in_hours?,
         source_text?, sentiment?}。手动可信 → confidence=1.0，不受阈值/去重拦截。"""
        from src.contacts.care_commitment import CareCommitment

        body = await request.json()
        contact_key = str(body.get("contact_key") or "").strip()
        topic = str(body.get("topic") or "").strip()
        if not contact_key or not topic:
            return {"ok": False, "reason": "missing", "message": "contact_key 和 topic 必填"}
        now = time.time()
        if body.get("due_at"):
            try:
                due_at = float(body["due_at"])
            except Exception:
                return {"ok": False, "reason": "bad_due_at", "message": "due_at 非法"}
        else:
            try:
                due_at = now + float(body.get("due_in_hours", 24)) * 3600.0
            except Exception:
                due_at = now + 86400.0
        if due_at <= now:
            return {"ok": False, "reason": "due_in_past", "message": "到期时间须在未来"}

        commitment = CareCommitment(
            due_at=due_at, event_at=due_at, topic=topic,
            sentiment=str(body.get("sentiment") or "neutral"),
            anchor_text="manual", source_text=str(body.get("source_text") or "")[:160],
            confidence=1.0,
        )
        store = _store(request)
        rid = store.add_commitment(
            commitment, contact_key=contact_key,
            platform=str(body.get("platform") or ""),
            account_id=str(body.get("account_id") or "default"),
            chat_key=str(body.get("chat_key") or ""),
            min_confidence=0.0, dedup_window_days=0.0,
        )
        if not rid:
            return {"ok": False, "reason": "add_failed", "message": "写入失败（可能重复）"}
        return {"ok": True, "id": rid}

    @app.post("/api/care/schedule/{sid}/cancel")
    async def api_care_schedule_cancel(sid: int, request: Request, _=Depends(api_auth)):
        """取消一条 pending 待办。"""
        store = _store(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok = store.cancel(int(sid), note=str(body.get("note") or "")[:200])
        if not ok:
            return {"ok": False, "reason": "not_pending", "message": "待办不存在或非 pending"}
        return {"ok": True, "cancelled": int(sid)}

    @app.post("/api/care/schedule/{sid}/send-now")
    async def api_care_schedule_send_now(sid: int, request: Request, _=Depends(api_auth)):
        """立即发：把 due_at 提前到当前 → 下个派发 tick 即到期处理（仍走全套发送护栏）。"""
        store = _store(request)
        ok = store.bring_forward(int(sid))
        if not ok:
            return {"ok": False, "reason": "not_pending", "message": "待办不存在或非 pending"}
        return {"ok": True, "due_now": int(sid)}

    @app.post("/api/care/schedule/{sid}/preview")
    async def api_care_schedule_preview(sid: int, request: Request, _=Depends(api_auth)):
        """P2 预览：这条待办到点 AI 会说什么——与派发共用 ``build_care_prompt``
        同一句 prompt 口径（「先看后发」看到的就是真发的话术风格），只生成、
        不落任何状态、不占试运行样本。响应只出结构化 reason，文案由前端 i18n。"""
        store = _store(request)
        item = store.get(int(sid))
        if not item or str(item.get("status")) != "pending":
            return {"ok": False, "reason": "not_pending"}
        ai = getattr(request.app.state, "ai_client", None)
        if ai is None:
            return {"ok": False, "reason": "ai_missing"}
        from src.contacts.care_dispatcher import build_care_prompt

        context_block = ""
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is not None:
            try:
                msgs = inbox.list_recent_messages(
                    str(item.get("contact_key") or ""), limit=8) or []
                context_block = "\n".join(
                    t for t in (str(m.get("text") or "").strip() for m in msgs) if t
                )[:800]
            except Exception:
                context_block = ""
        cm = _cm(request)
        ai_name = "她"
        try:
            ai_name = str((cm.get_ai_config() or {}).get("ai_name") or "她")
        except Exception:
            ai_name = "她"
        prompt = build_care_prompt(item, context_block=context_block, ai_name=ai_name)
        try:
            text = (await ai.chat(prompt) or "").strip()
        except Exception:
            logger.debug("care preview LLM 失败 sid=%s", sid, exc_info=True)
            return {"ok": False, "reason": "llm_error"}
        if not text:
            return {"ok": False, "reason": "llm_empty"}
        return {"ok": True, "id": int(sid), "preview": text}

    # ── Phase O 质量闭环：care dry_run 样本审核（与 reactivation 同范式）────────
    @app.get("/api/care/dry-run-samples")
    async def api_care_dry_samples(
        request: Request, limit: int = 50, before_ts: float = 0, _=Depends(api_auth),
    ):
        """care_dispatcher dry_run 模式下最近生成的关怀话术样本（供运营审核）。"""
        try:
            from src.monitoring.metrics_store import get_metrics_store
            samples = get_metrics_store().care_dry_samples(
                limit=max(1, min(int(limit or 50), 200)),
                before_ts=before_ts if before_ts > 0 else None,
            )
            return {"ok": True, "count": len(samples), "samples": samples}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}:{ex}"}

    @app.post("/api/care/dry-run-feedback")
    async def api_care_dry_feedback(request: Request, _=Depends(api_auth)):
        """对 care dry_run 样本的人工反馈。body：{sample_ts, verdict:like|dislike}。
        dislike → reply_text 进**共享** dislike 黑名单（care/reactivation 都会规避）。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        verdict = str(body.get("verdict", "")).strip().lower()
        if verdict not in ("like", "dislike"):
            return {"ok": False, "reason": "bad_verdict", "message": "verdict 须为 like/dislike"}
        sample_ts = float(body.get("sample_ts") or 0)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            ms.record_care_feedback(verdict)  # O·P 联动质量看板计数
            if verdict == "dislike" and sample_ts > 0:
                for s in ms.care_dry_samples(limit=200):
                    if abs(float(s.get("ts") or 0) - sample_ts) < 1.0:
                        ms.add_disliked_reply(s.get("reply_text", ""))
                        break
        except Exception:
            pass
        return {"ok": True, "verdict": verdict, "sample_ts": sample_ts}


__all__ = ["register_care_routes"]
