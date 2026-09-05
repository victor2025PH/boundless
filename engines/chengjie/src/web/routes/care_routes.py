"""Phase O4：主动关怀待办 Web API。

把 `CareScheduleStore` 暴露给后台：看「待关怀/已发/跳过(含原因)/过期」+ 手动加/取消。
读写都过 `api_auth`（后台管理面）。store 经 app.state 注入，缺则按 config 目录懒建单例。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import Depends, Request

logger = logging.getLogger(__name__)

_DEFERRED_NOTE_PREFIX = "deferred:"
_REPLY_WINDOW_SEC = 48 * 3600.0


def _deferred_id_from_note(note: str) -> int:
    """真发 sent 行的 note=``deferred:<row_id>`` → 队列行 id（其余形态返 0）。"""
    n = str(note or "")
    if not n.startswith(_DEFERRED_NOTE_PREFIX):
        return 0
    try:
        return int(n[len(_DEFERRED_NOTE_PREFIX):])
    except Exception:
        return 0


def _reply_state(inbox, contact_key: str, sent_at: float, now: float) -> str:
    """一条真发关怀的回复状态：``replied | no_reply | window_open``。

    聚合 48h 回复率与行级徽标共用本判定（单一真相；读消息异常按「没回」
    计——与旧 _compute_effect 行为一致，宁可低估不虚报）。
    """
    got = False
    try:
        msgs = inbox.list_recent_messages(str(contact_key), limit=100) or []
        got = any(
            str(m.get("direction") or "") == "in"
            and sent_at < float(m.get("ts") or 0) <= sent_at + _REPLY_WINDOW_SEC
            for m in msgs)
    except Exception:
        got = False
    if got:
        return "replied"
    return "no_reply" if now - sent_at >= _REPLY_WINDOW_SEC else "window_open"


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
            # list_history 最近在前——sent 行超过 limit 时优先保住近 7 天窗口
            rows = (store.list_history(status="sent", limit=300)
                    if hasattr(store, "list_history")
                    else store.list_recent(status="sent", limit=300))
        except Exception:
            return {}
        week = [r for r in rows
                if float(r.get("sent_at") or 0) >= now - 7 * 86400.0
                and str(r.get("note") or "").startswith(_DEFERRED_NOTE_PREFIX)]
        matured = replied = immature = 0
        by_topic: dict = {}
        for r in week:
            sat = float(r.get("sent_at") or 0)
            state = _reply_state(inbox, str(r.get("contact_key") or ""), sat, now)
            # P3：主题分桶（复用同一轮判定，零额外探针）——「哪类关怀有人回」；
            # window_open 不进分母，与总口径一致。
            topic = (str(r.get("topic") or "").strip() or "?")[:24]
            bucket = by_topic.setdefault(topic, {"n": 0, "replied": 0})
            if state == "replied":
                matured += 1
                replied += 1
                bucket["n"] += 1
                bucket["replied"] += 1
            elif state == "no_reply":
                matured += 1
                bucket["n"] += 1
            else:
                immature += 1
        top_topics = sorted(by_topic.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:5]
        return {
            "window_days": 7,
            "sent_7d": len(week),
            "matured": matured,
            "replied": replied,
            "immature": immature,
            "rate": (round(replied / matured, 3) if matured else None),
            "by_topic": [{"topic": k, "n": v["n"], "replied": v["replied"]}
                         for k, v in top_topics if v["n"] > 0],
        }

    def _effect_cached(request: Request, store) -> dict:
        now = time.time()
        if now - float(_effect_memo["ts"]) < 60.0:
            return dict(_effect_memo["data"])
        data = _compute_effect(request, store, now)
        _effect_memo["ts"] = now
        _effect_memo["data"] = data
        return dict(data)

    # ── P0 2026-08-03 历史可读性：显示名 join / 投递真相 / 行级回复徽标 ──────
    def _join_display_names(request: Request, rows: list) -> None:
        """批量补 display_name（与 /api/care/plan 同一 join 口径；失败静默）。"""
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not rows:
            return
        try:
            cids = [str(r.get("contact_key") or "") for r in rows]
            convs = inbox.get_conversations_for_ids([c for c in cids if c])
            names = {k: str(v.get("display_name") or "")
                     for k, v in (convs or {}).items()}
        except Exception:
            logger.debug("care history 名称 join 失败（忽略）", exc_info=True)
            return
        for r in rows:
            r["display_name"] = names.get(str(r.get("contact_key") or "")) or ""

    def _attach_delivery(request: Request, rows: list) -> None:
        """真发 sent 行反查 deferred 队列：投递真相 + 实际发出的话术。

        care 表的 sent 只代表「已入队」，队列里那条之后可能 pending/failed/
        expired——这里读时 join 出真状态（刻意不回写 care 表，免双写一致性）。
        隐私边界：只认队列行 extra 里 care=True 且 care_id 与本行对得上的
        （都是 care 入队时自己写的指纹）——话术是 AI 生成的出站关怀文案，
        与 dry-run 样本全文可见同一口径；通用队列总览（deferred_outbox_routes）
        只回长度的纪律不受影响。
        """
        dstore = getattr(request.app.state, "deferred_outbox_store", None)
        if dstore is None or not rows:
            return
        want: dict = {}
        for r in rows:
            if str(r.get("status") or "") != "sent":
                continue
            did = _deferred_id_from_note(str(r.get("note") or ""))
            if did > 0:
                want.setdefault(did, []).append(r)
        if not want:
            return
        try:
            drows = dstore.get_by_ids(list(want.keys())) or {}
        except Exception:
            logger.debug("care history deferred 反查失败（忽略）", exc_info=True)
            return
        for did, group in want.items():
            d = drows.get(did)
            if not d:
                continue
            try:
                extra = json.loads(str(d.get("extra") or "{}")) or {}
            except Exception:
                extra = {}
            for r in group:
                if not (bool(extra.get("care"))
                        and int(extra.get("care_id") or 0) == int(r.get("id") or 0)):
                    continue  # 指纹对不上：宁可不显示，绝不挂错话术
                r["delivery"] = {
                    "status": str(d.get("status") or ""),
                    "sent_at": float(d.get("sent_at") or 0),
                    "error": str(d.get("error") or "")[:200],
                }
                r["sent_text"] = str(d.get("reply_text") or "")

    _reply_memo: dict = {}  # care row id -> (checked_ts, state)
    _REPLY_PROBE_CAP = 50

    def _attach_reply_states(request: Request, rows: list, now: float) -> None:
        """真发 sent 行补 reply_state（48h 窗，与聚合回复率同一判定函数）。

        逐行要查一次会话消息 → 探针 50 行/次封顶 + 60s 记忆（replied 是终态
        永久缓存）；预算外的行不给字段（前端不显示，绝不猜）。dry_run 行不适用。
        """
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            return
        probes = 0
        for r in rows:
            if str(r.get("status") or "") != "sent":
                continue
            if not str(r.get("note") or "").startswith(_DEFERRED_NOTE_PREFIX):
                continue
            sat = float(r.get("sent_at") or 0)
            if sat <= 0:
                continue
            rid = int(r.get("id") or 0)
            memo = _reply_memo.get(rid)
            if memo and (memo[1] == "replied" or now - memo[0] < 60.0):
                r["reply_state"] = memo[1]
                continue
            if probes >= _REPLY_PROBE_CAP:
                continue
            probes += 1
            state = _reply_state(inbox, str(r.get("contact_key") or ""), sat, now)
            _reply_memo[rid] = (now, state)
            r["reply_state"] = state
        if len(_reply_memo) > 2000:  # 防长期运行无界增长
            _reply_memo.clear()

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

    # ── P7 2026-08-03：「AI 关怀管家」聚合方案（页面唯一主数据源）──────────
    @app.get("/api/care/plan")
    async def api_care_plan(request: Request, _=Depends(api_auth)):
        """一次请求出整份运行方案：引擎灯态 + 分组待办（联系人显示名服务端 join）
        + 读数摘要（效果/样本/捕获）+ 结构化建议。

        替代旧页面的 health+schedule+samples 三连拉。响应只出结构化码
        （advice=[{code,...}]），措辞由前端 i18n 渲染（CJK 收口纪律）。
        确定性核心在 ``src/contacts/care_advisor.py``；LLM 解说层属下一阶段，
        挂在本方案之上（缺席不影响本接口）。
        """
        from src.contacts.care_advisor import (
            build_advice, build_lights, engine_overall, group_plan_items,
        )

        cm = _cm(request)
        conf = getattr(cm, "config", None) or {}
        comp = (conf.get("companion") or {})
        care_cfg = dict(comp.get("proactive_care") or {})
        enabled = bool(care_cfg.get("enabled", False))
        dry_run = bool(care_cfg.get("dry_run", False))
        llm_cfg = dict(care_cfg.get("llm_extract") or {})
        mdef = dict(comp.get("multiplatform_deferred") or {})

        engine = getattr(request.app.state, "care_engine", None) or {}
        dispatch = {"running": False, "last_tick_ts": 0.0}
        dispatcher = engine.get("dispatcher")
        if dispatcher is not None:
            try:
                dispatch.update(dispatcher.health_snapshot())
            except Exception:
                logger.debug("care plan dispatcher snapshot 失败", exc_info=True)
        shadow = {}
        sc = engine.get("shadow_scanner")
        if sc is not None:
            try:
                shadow = sc.snapshot()
            except Exception:
                logger.debug("care plan shadow snapshot 失败", exc_info=True)

        store = _store(request)
        now = time.time()
        summary = _summary(store)
        pending = store.list_pending(limit=200)

        # 试运行拟稿 / 最近已发（recent 区块，dry 与 live 二选一的叙事）
        dry_samples = []
        samples_stats = {}
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            dry_samples = ms.care_dry_samples(limit=8)
            care_q = (ms.companion_quality_overview(window_sec=7 * 86400.0)
                      or {}).get("care") or {}
            fb = care_q.get("feedback") or {}
            like = int(fb.get("like") or 0)
            dislike = int(fb.get("dislike") or 0)
            samples_stats = {
                "recent_7d": int(care_q.get("dry_run") or 0),
                "like": like,
                "dislike": dislike,
                "reviewed": like + dislike,
                "like_rate_pct": fb.get("like_rate_pct"),
            }
        except Exception:
            logger.debug("care plan metrics 读取失败（忽略）", exc_info=True)

        # P2 2026-08-03：拟稿与已发**并存**（旧版按 dry/live 二选一——切档后
        # 另一侧历史就看不见了）。sent 块=真发行（note=deferred:*，dry 拟稿不算
        # 「已发」，≤20 最近在前，内联话术快照 + 投递真相 + 回复徽标）。
        # P3：dry 待审队列升级为**持久口径**（store 里未审且有快照的 dry 行，
        # 重启后待审拟稿不再消失、审过的不再重复出现）；老库无快照时代回落
        # 进程内 metrics 样本（行为与旧版一致）。前端按 kind 分区渲染。
        dry_store_rows = (store.list_unreviewed_drafts(limit=8)
                          if hasattr(store, "list_unreviewed_drafts") else [])
        rstats = store.review_stats() if hasattr(store, "review_stats") else {}
        if dry_store_rows or int(rstats.get("reviewed") or 0) > 0:
            dry_rows = [{
                "kind": "dry",
                "id": int(r.get("id") or 0),
                # 实施84 P0-4：新语义 dry 行是 pending（sent_at 空），时刻取
                # dry_sampled_at；旧语义行仍读 sent_at。
                "ts": float(r.get("sent_at") or r.get("dry_sampled_at") or 0),
                "topic": str(r.get("topic") or ""),
                "text": str(r.get("sent_text") or ""),
                "platform": str(r.get("platform") or ""),
                "chat_key": str(r.get("chat_key") or ""),
                "contact_key": str(r.get("contact_key") or ""),
            } for r in dry_store_rows]
        else:
            dry_rows = [{
                "kind": "dry",
                "ts": float(s.get("ts") or 0),
                "topic": str(s.get("topic") or ""),
                "text": str(s.get("reply_text") or ""),
                "platform": str(s.get("platform") or ""),
                "chat_key": str(s.get("chat_key") or ""),
                "contact_key": str(s.get("contact_key") or ""),
            } for s in dry_samples[:8]]
        # P3：审核进度改读持久口径（review 列）——「已审 n/15」重启不清零；
        # 进程内 metrics 口径仅在快照时代尚未开始时兜底（老库升级窗口）。
        if rstats and (dry_store_rows or int(rstats.get("reviewed") or 0) > 0):
            _like = int(rstats.get("like") or 0)
            _dislike = int(rstats.get("dislike") or 0)
            samples_stats = {
                "recent_7d": int(rstats.get("drafts_7d") or 0),
                "like": _like,
                "dislike": _dislike,
                "reviewed": _like + _dislike,
                "like_rate_pct": (round(_like / (_like + _dislike) * 100, 1)
                                  if (_like + _dislike) else None),
                "source": "store",
            }

        sent_src = (store.list_history(status="sent", limit=60)
                    if hasattr(store, "list_history")
                    else store.list_recent(status="sent", limit=60))
        real_sent = [r for r in sent_src
                     if str(r.get("note") or "").startswith(_DEFERRED_NOTE_PREFIX)]
        real_sent.sort(key=lambda r: float(r.get("sent_at") or 0), reverse=True)
        sent_recent = [{
            "kind": "sent",
            "status": "sent",  # 供 delivery/reply 富化复用同一判定
            "id": int(r.get("id") or 0),
            "ts": float(r.get("sent_at") or 0),
            "sent_at": float(r.get("sent_at") or 0),
            "topic": str(r.get("topic") or ""),
            "note": str(r.get("note") or ""),
            "platform": str(r.get("platform") or ""),
            "chat_key": str(r.get("chat_key") or ""),
            "contact_key": str(r.get("contact_key") or ""),
            "sent_text": str(r.get("sent_text") or ""),  # 快照＝持久口径
        } for r in real_sent[:20]]
        _attach_delivery(request, sent_recent)
        _attach_reply_states(request, sent_recent, now)
        recent_rows = (dry_rows + sent_recent) if dry_run else (sent_recent + dry_rows)

        # 联系人显示名：一次批量 join（inbox 不可用 → 前端回落 chat_key）
        names = {}
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is not None:
            try:
                cids = [str(it.get("contact_key") or "") for it in pending]
                cids += [str(r.get("contact_key") or "") for r in recent_rows]
                convs = inbox.get_conversations_for_ids([c for c in cids if c])
                names = {k: str(v.get("display_name") or "")
                         for k, v in (convs or {}).items()}
            except Exception:
                logger.debug("care plan 名称 join 失败（忽略）", exc_info=True)

        def _with_name(row: dict) -> dict:
            row["display_name"] = names.get(str(row.get("contact_key") or "")) or ""
            return row

        items = [_with_name({
            "id": int(it.get("id") or 0),
            "contact_key": str(it.get("contact_key") or ""),
            "platform": str(it.get("platform") or ""),
            "chat_key": str(it.get("chat_key") or ""),
            "topic": str(it.get("topic") or ""),
            "source_text": str(it.get("source_text") or ""),
            "due_at": float(it.get("due_at") or 0),
            "sentiment": str(it.get("sentiment") or "neutral"),
            "confidence": float(it.get("confidence") or 0),
        }) for it in pending]
        groups = group_plan_items(items, now)
        by_key = {g["key"]: len(g["items"]) for g in groups}
        recent_rows = [_with_name(r) for r in recent_rows]

        lights = build_lights(
            enabled=enabled, dry_run=dry_run,
            capture_wired=bool(engine.get("capture_wired", False)),
            capture_config_on=enabled and bool(care_cfg.get("capture", True)),
            dispatch_running=bool(dispatch.get("running", False)),
            dispatch_skip=str(engine.get("dispatcher_skip") or ""),
            multiplatform_deferred=bool(mdef.get("enabled", False)),
            messenger_rpa=bool(engine.get("messenger_rpa", False)),
        )
        effect = _effect_cached(request, store)
        captured_24h = store.count_created_since(now - 86400.0)
        last_captured_ts = store.last_created_at()
        advice = build_advice(
            enabled=enabled, dry_run=dry_run,
            reviewed=int(samples_stats.get("reviewed") or 0),
            like_rate_pct=samples_stats.get("like_rate_pct"),
            samples_7d=int(samples_stats.get("recent_7d") or 0),
            pending_total=int(summary.get("pending") or 0),
            captured_24h=captured_24h,
            last_captured_ts=last_captured_ts,
            shadow_llm_only=int(shadow.get("llm_only") or 0),
            llm_capture_enabled=bool(llm_cfg.get("enabled", False)),
            effect_rate=effect.get("rate"),
            effect_matured=int(effect.get("matured") or 0),
            effect_replied=int(effect.get("replied") or 0),
            now=now,
        )
        return {
            "ok": True,
            "now": now,
            "engine": {
                "enabled": enabled,
                "dry_run": dry_run,
                "lights": lights,
                "overall": engine_overall(lights, enabled=enabled, dry_run=dry_run),
                "last_tick_ts": float(dispatch.get("last_tick_ts") or 0),
                "dispatch_skip": str(engine.get("dispatcher_skip") or ""),
            },
            "digest": {
                "summary": summary,
                "pending_total": int(summary.get("pending") or 0),
                "overdue": int(by_key.get("overdue") or 0),
                "due_today": int(by_key.get("overdue") or 0) + int(by_key.get("today") or 0),
                "captured_24h": captured_24h,
                "last_captured_ts": last_captured_ts,
                "effect": effect,
                "samples": samples_stats,
            },
            "advice": advice,
            "groups": groups,
            "recent": {"mode": ("dry" if dry_run else "live"), "items": recent_rows},
            "shadow": shadow,
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
        request: Request, status: str = "", limit: int = 100,
        contact_key: str = "", _=Depends(api_auth),
    ):
        """关怀待办/历史列表 + 各状态计数。status 空=全部。

        P0 2026-08-03 历史可读性：排序分语义——pending 仍按到期升序（待办），
        其余（含「全部」）按「最近发生」降序（翻历史要的是最新在前），响应带
        ``order`` 供前端标注口径；行内联 display_name / 投递真相 / 实际话术 /
        48h 回复状态（依赖缺失 → 字段缺席，前端自动回落旧渲染）。
        P2：``contact_key`` 非空＝「TA 的关怀史」（恒按最近发生降序）。
        """
        store = _store(request)
        st = status.strip()
        ck = contact_key.strip()
        lim = max(1, min(int(limit or 100), 500))
        if ck and hasattr(store, "list_history"):
            items, order = (store.list_history(status=st, contact_key=ck, limit=lim),
                            "recent_desc")
        elif st == "pending" or not hasattr(store, "list_history"):
            items, order = store.list_recent(status=st, limit=lim), "due_asc"
        else:
            items, order = store.list_history(status=st, limit=lim), "recent_desc"
        _join_display_names(request, items)
        _attach_delivery(request, items)
        _attach_reply_states(request, items, time.time())
        return {"ok": True, "items": items, "count": len(items),
                "order": order, "summary": _summary(store)}

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
        from src.contacts.care_intent import detect_instruction_intent

        body = await request.json()
        contact_key = str(body.get("contact_key") or "").strip()
        topic = str(body.get("topic") or "").strip()
        # J-8 #182 两种类型：event＝客户的事（AI 自然关心，经 LLM）；
        # verbatim＝到点原文直发（零 LLM）。缺省 event 保旧调用方语义。
        mode = str(body.get("mode") or "event").strip().lower()
        if mode not in ("event", "verbatim"):
            return {"ok": False, "reason": "bad_mode"}
        if not contact_key or not topic:
            return {"ok": False, "reason": "missing", "message": "contact_key 和 topic 必填"}
        # 意图守卫：「什么事」填的是给 AI 的指令 / 要发的话（「主动问候对方早上好」）
        # → 不入队，回 reason=looks_like_instruction 让前端提示切换到原文直发；
        # 运营确认仍按客户的事入队时带 confirm_event=true 放行。
        if mode == "event" and not bool(body.get("confirm_event")):
            verdict = detect_instruction_intent(topic)
            if verdict.get("looks_like_instruction"):
                return {"ok": False, "reason": "looks_like_instruction",
                        "guard_reason": str(verdict.get("reason") or ""),
                        "suggest_mode": "verbatim"}
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

        store = _store(request)
        if mode == "verbatim":
            # 原文＝topic 栏填的整句话（前端同一个输入框）；source_text 可显式给全文
            text = str(body.get("text") or body.get("source_text") or topic).strip()
            if not hasattr(store, "add_verbatim"):
                return {"ok": False, "reason": "unsupported"}
            rid = store.add_verbatim(
                contact_key=contact_key, due_at=due_at, text=text,
                platform=str(body.get("platform") or ""),
                account_id=str(body.get("account_id") or "default"),
                chat_key=str(body.get("chat_key") or ""),
            )
            if not rid:
                return {"ok": False, "reason": "add_failed", "message": "写入失败"}
            return {"ok": True, "id": rid, "mode": "verbatim"}

        commitment = CareCommitment(
            due_at=due_at, event_at=due_at, topic=topic,
            sentiment=str(body.get("sentiment") or "neutral"),
            anchor_text="manual", source_text=str(body.get("source_text") or "")[:160],
            confidence=1.0,
        )
        rid = store.add_commitment(
            commitment, contact_key=contact_key,
            platform=str(body.get("platform") or ""),
            account_id=str(body.get("account_id") or "default"),
            chat_key=str(body.get("chat_key") or ""),
            min_confidence=0.0, dedup_window_days=0.0,
        )
        if not rid:
            return {"ok": False, "reason": "add_failed", "message": "写入失败（可能重复）"}
        return {"ok": True, "id": rid, "mode": "event"}

    @app.post("/api/care/intent-check")
    async def api_care_intent_check(request: Request, _=Depends(api_auth)):
        """J-8 #182 前端逐键守卫：「什么事」这栏像不像一条要发出去的话/指令。
        纯函数、零副作用；与 add 端点同一判定源（``detect_instruction_intent``）。"""
        from src.contacts.care_intent import detect_instruction_intent
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        v = detect_instruction_intent(str(body.get("topic") or ""))
        return {"ok": True, "looks_like_instruction": bool(v.get("looks_like_instruction")),
                "reason": str(v.get("reason") or "")}

    @app.post("/api/care/schedule/{sid}/send-text")
    async def api_care_schedule_send_text(sid: int, request: Request, _=Depends(api_auth)):
        """J-8 #182「改一改再发」：预览上手改的终稿直接送出站队列（不经 LLM）。

        body：{text}。经派发器 ``deliver_text``（与自动派发同一 ``_send``：deferred
        队列 gate / pacing / kill-switch / 安静时段顺延全享），成功即该行 mark_sent。
        结构化 reason：not_pending / empty_text / missing_route / gated / send_failed /
        dispatcher_missing。
        """
        store = _store(request)
        item = store.get(int(sid))
        if not item or str(item.get("status")) != "pending":
            return {"ok": False, "reason": "not_pending"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        text = str(body.get("text") or "").strip()
        if not text:
            return {"ok": False, "reason": "empty_text"}
        engine = getattr(request.app.state, "care_engine", None) or {}
        disp = engine.get("dispatcher") if isinstance(engine, dict) else None
        if disp is None or not hasattr(disp, "deliver_text"):
            return {"ok": False, "reason": "dispatcher_missing"}
        res = await disp.deliver_text(dict(item), text)
        res = dict(res or {})
        res.setdefault("ok", False)
        res["id"] = int(sid)
        return res

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

    @app.post("/api/care/schedule/{sid}/reschedule")
    async def api_care_schedule_reschedule(sid: int, request: Request, _=Depends(api_auth)):
        """P7 改期：pending 待办的 due_at 改到未来某刻（方案卡「改时间」）。

        body：{due_at?|due_in_hours?}——与手动添加同一时间语义；须在未来。
        响应只出结构化 reason（bad_due/due_in_past/not_pending），文案由前端 i18n。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        now = time.time()
        if body.get("due_at") is not None:
            try:
                due_at = float(body["due_at"])
            except Exception:
                return {"ok": False, "reason": "bad_due"}
        else:
            try:
                due_at = now + float(body.get("due_in_hours", 24)) * 3600.0
            except Exception:
                return {"ok": False, "reason": "bad_due"}
        if due_at <= now:
            return {"ok": False, "reason": "due_in_past"}
        store = _store(request)
        if not store.reschedule(int(sid), due_at):
            return {"ok": False, "reason": "not_pending"}
        return {"ok": True, "id": int(sid), "due_at": due_at}

    @app.post("/api/care/schedule/{sid}/preview")
    async def api_care_schedule_preview(sid: int, request: Request, _=Depends(api_auth)):
        """P2 预览：这条待办到点 AI 会说什么——与派发共用 ``build_care_prompt``
        同一句 prompt 口径（「先看后发」看到的就是真发的话术风格），只生成、
        不落任何状态、不占试运行样本。响应只出结构化 reason，文案由前端 i18n。"""
        store = _store(request)
        item = store.get(int(sid))
        if not item or str(item.get("status")) != "pending":
            return {"ok": False, "reason": "not_pending"}
        # J-8 #182：预览先回「我理解为」结构化字段（前端按 i18n 拼句）；
        # 原文直发行不经 LLM，预览就是原文。
        understanding: dict = {}
        try:
            from src.contacts.care_intent import care_understanding
            understanding = care_understanding(item)
        except Exception:
            understanding = {}
        if understanding.get("mode") == "verbatim":
            return {"ok": True, "id": int(sid), "mode": "verbatim",
                    "preview": str(understanding.get("source_text") or ""),
                    "understanding": understanding}
        ai = getattr(request.app.state, "ai_client", None)
        if ai is None:
            return {"ok": False, "reason": "ai_missing", "understanding": understanding}
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
        # B110④ 预览=派发同源：负样本块同样进预览 prompt（缺方法/读失败按空）
        _recent_sent = []
        try:
            _recent_sent = store.recent_sent_texts(
                str(item.get("contact_key") or ""), limit=4)
        except Exception:
            _recent_sent = []
        # 实施84 P0-2 预览=派发同源：增强块（人设/记忆/目标）经派发器公开方法
        # 取同一份 provider——预览看到的就是真发的口径。派发器缺席按空（旧口径）。
        _extras = {}
        try:
            engine = getattr(request.app.state, "care_engine", None) or {}
            disp = engine.get("dispatcher")
            if disp is not None and hasattr(disp, "prompt_extras"):
                _extras = disp.prompt_extras(item) or {}
        except Exception:
            _extras = {}
        prompt = build_care_prompt(
            item, context_block=context_block,
            recent_sent=_recent_sent, ai_name=ai_name,
            persona_line=str(_extras.get("persona_line") or ""),
            memory_block=str(_extras.get("memory_block") or ""),
            goal_block=str(_extras.get("goal_block") or ""))
        try:
            text = (await ai.chat(prompt) or "").strip()
        except Exception:
            logger.debug("care preview LLM 失败 sid=%s", sid, exc_info=True)
            return {"ok": False, "reason": "llm_error", "understanding": understanding}
        if not text:
            return {"ok": False, "reason": "llm_empty", "understanding": understanding}
        return {"ok": True, "id": int(sid), "mode": "event", "preview": text,
                "understanding": understanding}

    # ── 实施84 P0-5：全部主动消息统一时间线（来源标注）────────────────────────
    _TL_RITUAL_NOTES = ("ritual", "milestone")

    def _outreach_source(batch_id: str, note: str) -> str:
        """batch_id 前缀 → 管线来源码（前端 i18n 渲染人话）。

        proactive_topic 批次按 note（=开场 mode）再分仪式/回访两桶——ritual
        与 topic 走同一 _send/_log_outreach，批次前缀相同，mode 才是分界。
        """
        bid = str(batch_id or "")
        if bid.startswith("care:"):
            return "care"
        if bid.startswith("reactivation:"):
            return "reactivation"
        if bid.startswith("proactive_topic:"):
            n = str(note or "").lower()
            if any(k in n for k in _TL_RITUAL_NOTES):
                return "ritual"
            return "topic"
        return "other"

    @app.get("/api/care/outreach-timeline")
    async def api_care_outreach_timeline(
        request: Request, days: float = 7, limit: int = 120,
        conversation_id: str = "",
        _=Depends(api_auth),
    ):
        """近 N 天**全部管线**的主动消息时间线（outreach_log 单一账本读口）。

        修「坐席在关怀页看到的与客户实际收到的对不上」的感知裂缝：约定关怀 /
        沉默回访 / 仪式问候 / 召回各自为政地发，此前没有任何一处能回答
        「这位客户这周到底被主动打扰了几次、都是谁发的」。旧后端无
        ``list_outreach_recent`` → 前端按 reason=unavailable 隐藏区块。

        ``conversation_id``（实施84 P0-5b，可选）＝单会话过滤——收件箱消息流
        给出站消息标「来源 chip」用（客户端按时间就近匹配消息行）。过滤在
        全量行上做（读口 ≤1000 行，纯内存筛，不另写 SQL 分叉）。
        """
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not hasattr(inbox, "list_outreach_recent"):
            return {"ok": False, "reason": "unavailable"}
        d = max(1.0, min(float(days or 7), 30.0))
        rows = inbox.list_outreach_recent(
            days=d, limit=max(1, min(int(limit or 120), 500))) or []
        cid_filter = str(conversation_id or "").strip()
        if cid_filter:
            rows = [r for r in rows
                    if str(r.get("conversation_id") or "") == cid_filter]
        names = {}
        try:
            cids = [str(r.get("conversation_id") or "") for r in rows]
            convs = inbox.get_conversations_for_ids([c for c in cids if c])
            names = {k: str(v.get("display_name") or "")
                     for k, v in (convs or {}).items()}
        except Exception:
            logger.debug("outreach timeline 名称 join 失败（忽略）", exc_info=True)
        items = []
        by_source: dict = {}
        for r in rows:
            src = _outreach_source(str(r.get("batch_id") or ""),
                                   str(r.get("note") or ""))
            by_source[src] = int(by_source.get(src, 0)) + 1
            cid = str(r.get("conversation_id") or "")
            items.append({
                "ts": float(r.get("ts") or 0),
                "source": src,
                "batch_id": str(r.get("batch_id") or "")[:48],
                "note": str(r.get("note") or "")[:48],
                "platform": str(r.get("platform") or ""),
                "conversation_id": cid,
                "display_name": names.get(cid) or "",
            })
        return {"ok": True, "days": d, "count": len(items),
                "by_source": by_source, "items": items}

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
        """对 care 拟稿的人工反馈。body：{care_id?|sample_ts?, verdict:like|dislike}。

        P3：判定持久化到 care 行（review 列）——「已审 n/15」进度重启不丢，
        改判允许但不重复计数；dislike 话术仍进**会话级**共享黑名单（metrics
        侧刻意不持久化：dislike 是主观判断，重启后重审更健康——该设计不变）。
        legacy sample_ts 路径保留（旧页面/降级模式），样本携带 care_id 时同样落行。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        verdict = str(body.get("verdict", "")).strip().lower()
        if verdict not in ("like", "dislike"):
            return {"ok": False, "reason": "bad_verdict", "message": "verdict 须为 like/dislike"}
        care_id = int(body.get("care_id") or 0)
        sample_ts = float(body.get("sample_ts") or 0)
        store = _store(request)

        sample = None
        if sample_ts > 0:
            try:
                from src.monitoring.metrics_store import get_metrics_store
                for s in get_metrics_store().care_dry_samples(limit=200):
                    if abs(float(s.get("ts") or 0) - sample_ts) < 1.0:
                        sample = s
                        break
            except Exception:
                sample = None
        if not care_id and sample:
            care_id = int(sample.get("care_id") or 0)

        applied_row = newly = False
        reply_text = ""
        if care_id > 0 and hasattr(store, "set_review"):
            applied_row, newly = store.set_review(care_id, verdict)
            if applied_row and verdict == "dislike":
                row = store.get(care_id) or {}
                reply_text = str(row.get("sent_text") or "")
        if not applied_row and not sample and care_id > 0:
            return {"ok": False, "reason": "not_found"}
        if not reply_text and sample and verdict == "dislike":
            reply_text = str(sample.get("reply_text") or "")
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            if newly or not applied_row:
                ms.record_care_feedback(verdict)  # 首评 +1；改判不重复计数
            if verdict == "dislike" and reply_text:
                ms.add_disliked_reply(reply_text)
        except Exception:
            pass
        return {"ok": True, "verdict": verdict, "care_id": care_id,
                "sample_ts": sample_ts, "persisted": applied_row}


__all__ = ["register_care_routes"]
