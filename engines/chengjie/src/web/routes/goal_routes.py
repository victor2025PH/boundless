"""营销目标（marketing goals）后台 API。

端点（挂 ``/api/goals*``；静态路径先于 ``{goal_id}`` 注册防吞路由）：
- ``GET  /api/goals/templates``        —— 模板库（建目标表单用；含里程碑/参数 schema）
- ``GET  /api/goals/for-conversation`` —— 会话活跃目标视图（右栏卡；settle-on-read）
- ``GET  /api/goals/report``           —— 结果闭环聚合（P2：模板×终态成功率/天数/反馈）
- ``GET  /api/goals/readiness``        —— 开闸就绪度（P10：开关/人设/账号绑定/订单通道）
- ``POST /api/goals/batch``            —— 批量 campaign（P2：多会话同款目标，逐条护栏）
- ``GET  /api/goals/profile``          —— 客户画像卡（P1：双轨槽位+填充率+缺口）
- ``POST /api/goals/profile``          —— 坐席手录画像（覆盖 auto；空串=清槽）
- ``POST /api/goals/order-hook``       —— 官网订单回流（token 鉴权；按 ref 结算 done）
- ``GET  /api/goals/agenda``           —— 今日工作清单（P17：把计数变成点名单；
  ``?names=1`` 时信封顶层附 {conversation_id: 客户展示名} 映射）
- ``GET  /api/goals``                  —— 目标列表 + 状态聚合（看板；逐条 settle）
- ``POST /api/goals``                  —— 建目标（viewer 只读拦截；每会话活跃数上限）
- ``GET  /api/goals/{goal_id}``        —— 详情（视图 + 拍时间线 + 事件台账）
- ``POST /api/goals/{goal_id}/update`` —— 改字段（title/autonomy/priority/deadline/params）
- ``POST /api/goals/{goal_id}/status`` —— 生命周期操作（pause/resume/cancel/done 手动成交，
  done 可带可选 ``meta`` 成交归因）
- ``POST /api/goals/{goal_id}/beat/feedback`` —— 坐席采纳/驳回/撤销今日拍（P2 回流 planner）

口径唯一性：读路径一律经 ``service.refresh_goal``（settle + 当日拍）再出
``service.goal_view``——右栏卡、看板、prompt 注入三个消费面读同一份结算结果。
功能未启用（``companion.goals.enabled=false``）时全部端点 403，不装死也不 500。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.goal_routes")

_ROLE_VIEWER = "viewer"

# list 接口逐条 settle（每条=进程内 DB 读），上限护住最坏情况
_LIST_SETTLE_CAP = 100


def _split_conversation_id(conversation_id: str):
    """``platform:account_id:chat_key`` → 三元组（chat_key 可含冒号，split 限 2 刀）。"""
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) == 3 and all(p.strip() for p in parts[:2]):
        return parts[0].strip(), parts[1].strip(), parts[2].strip()
    return "", "", ""


def register_goal_routes(app, auth_dep, config_manager=None):
    """注册营销目标路由。``config_manager`` 供配置段/库路径解析。"""

    def _cfg_root() -> Dict[str, Any]:
        try:
            cfg = getattr(config_manager, "config", None)
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _config_path():
        return getattr(config_manager, "config_path", None)

    def _svc():
        from src.companion.goals import service as goal_service
        return goal_service

    def _require_enabled(request: Request):
        svc = _svc()
        if not svc.goals_enabled(_cfg_root()):
            raise HTTPException(403, tr(request, "err.goals.disabled"))
        return svc

    def _deny_viewer(request: Request):
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.goals.readonly"))

    def _store(svc):
        return svc.get_configured_store(_cfg_root(), _config_path())

    def _inbox_store():
        try:
            from src.integrations.protocol_bridge import get_inbox_store
            return get_inbox_store()
        except Exception:
            return None

    def _lang(request: Request) -> str:
        try:
            return str(getattr(request.state, "ui_lang", "") or "zh")
        except Exception:
            return "zh"

    def _refreshed_view(svc, store, goal, *, lang: str) -> Dict[str, Any]:
        res = svc.refresh_goal(store, _cfg_root(), goal, inbox_store=_inbox_store())
        return svc.goal_view(
            res["goal"], res.get("action"), res.get("hold"), lang=lang)

    def _attach_slots_progress(view, store, *, lang: str):
        """摸底类目标（params.slots 勾选）→ 视图附逐槽位清单
        ``slots_progress: [{key,label,filled,value}]``——目标卡「打勾清单」
        数据源（客户答了职业下轮刷新即打勾）。非摸底目标零改动；
        best-effort 绝不抛（清单缺失只影响 UI 细节不影响卡片主体）。"""
        if not isinstance(view, dict):
            return view
        try:
            from src.companion.goals.profile_slots import (
                parse_selected_slots,
                slot_label,
                slot_value,
            )
            sel = parse_selected_slots((view.get("params") or {}).get("slots"))
            if not sel:
                return view
            prof = store.get_customer_profile(
                str(view.get("platform") or ""),
                str(view.get("chat_key") or ""))
            fields = dict((prof or {}).get("fields") or {})
            view["slots_progress"] = [
                {
                    "key": k,
                    "label": slot_label(k, lang),
                    "filled": bool(slot_value(fields, k)),
                    "value": slot_value(fields, k)[:20],
                }
                for k in sel
            ]
        except Exception:
            logger.debug("slots_progress attach skipped", exc_info=True)
        return view

    def _attach_products(view, store, *, lang: str):
        """catalog 模板的活跃目标 → 附「系统会推什么」预览（坐席人工回复
        对齐口径用；与 prompt 注入同一套选品纯函数，读数零写入）。"""
        if not isinstance(view, dict) or not view.get("catalog"):
            return view
        try:
            from src.companion.goals import site_catalog as sc
            cat = sc.load_catalog(sc.catalog_path(_cfg_root(), _config_path()))
            prof = store.get_customer_profile(
                str(view.get("platform") or ""), str(view.get("chat_key") or ""))
            fields = dict((prof or {}).get("fields") or {})
            pinned = str((view.get("params") or {}).get("product_id") or "")
            site = (cat or {}).get("site") or {}
            en = str(lang).lower().startswith("en")
            # 与 prompt 注入同口径：近窗成交量做痛点同分裁决（P4 反哺）
            sold = sc.sold_boost_map(
                cat, _svc().sold_plan_counts_cached(store))
            view["products"] = [{
                "id": str(p.get("id") or ""),
                "name": str(p.get("name_en" if en else "name_zh")
                            or p.get("name_zh") or p.get("id") or ""),
                "pitch": str(p.get("pitch_en" if en else "pitch_zh")
                             or p.get("pitch_zh") or ""),
                "price_from": str(p.get("price_from") or ""),
                "url": sc.product_link(site, p),
            } for p in sc.pick_products(
                cat, fields, pinned=pinned, limit=2, sold=sold)]
        except Exception:   # 目录层软失败：不出产品行即可
            logger.debug("attach products failed", exc_info=True)
        return view

    def _pickers() -> Dict[str, Any]:
        """建目标向导第二步的枚举源（P24）：解锁项/会员档（变现价目表）+
        官网产品（site_catalog）+ 漏斗阶段词表。逐段软失败——哪段挂了就缺
        哪段，前端对缺失段自动回落通用输入框，绝不 500。"""
        from src.companion.goals.templates import STAGE_ORDER
        out: Dict[str, Any] = {"stages": list(STAGE_ORDER)}
        cfg = _cfg_root()
        mon = cfg.get("monetization") if isinstance(cfg.get("monetization"), dict) else {}
        try:
            from src.utils.monetization import merge_catalog
            cat = merge_catalog((mon or {}).get("catalog"))
            out["currency"] = str(cat.get("currency") or "USD")
            out["unlock_items"] = [
                {"id": str(k), "label": str((v or {}).get("label") or k),
                 "price": float((v or {}).get("price") or 0)}
                for k, v in (cat.get("items") or {}).items()]
            out["tiers"] = [
                {"id": str(k), "label": str((v or {}).get("label") or k),
                 "monthly": float((v or {}).get("monthly") or 0)}
                for k, v in (cat.get("tiers") or {}).items() if str(k) != "free"]
        except Exception:
            logger.debug("pickers: monetize catalog failed", exc_info=True)
        try:
            from src.companion.goals import site_catalog as sc
            scat = sc.load_catalog(sc.catalog_path(_cfg_root(), _config_path()))
            prods = (scat or {}).get("products")
            out["site_products"] = [
                {"id": str(p.get("id") or ""),
                 "name_zh": str(p.get("name_zh") or p.get("id") or ""),
                 "name_en": str(p.get("name_en") or p.get("name_zh") or p.get("id") or ""),
                 "price_from": str(p.get("price_from") or "")}
                for p in prods if isinstance(p, dict)] if isinstance(prods, list) else []
        except Exception:
            logger.debug("pickers: site catalog failed", exc_info=True)
        # P28：摸底模板 slots chips 枚举（与 profile_slots 登记表同源）
        try:
            from src.companion.goals.profile_slots import SLOTS
            out["discovery_slots"] = [
                {"key": str(s.get("key") or ""),
                 "track": str(s.get("track") or ""),
                 "label_zh": str(s.get("label_zh") or s.get("key") or ""),
                 "label_en": str(s.get("label_en") or s.get("label_zh") or s.get("key") or "")}
                for s in SLOTS
                if str(s.get("track") or "") in ("relation", "bant")
                and str(s.get("key") or "")
            ]
        except Exception:
            logger.debug("pickers: discovery_slots failed", exc_info=True)
        return out

    # ── 静态路径（先注册）────────────────────────────────────────────────────
    @app.get("/api/goals/templates")
    async def goals_templates(request: Request, _auth=Depends(auth_dep)):
        _require_enabled(request)
        from src.companion.goals.templates import (
            AUTONOMY_LEVELS,
            GOAL_STATUSES,
            list_templates,
        )
        # caps：让前端把 auto 档说明对齐真实行为——bridge 关着时「自动推进」
        # 不会主动发消息，表单如实注明（文案与行为一致是硬原则）。
        cfg = _cfg_root()
        companion = cfg.get("companion") if isinstance(cfg.get("companion"), dict) else {}
        goals_cfg = companion.get("goals") if isinstance(companion.get("goals"), dict) else {}
        bridge_cfg = goals_cfg.get("bridge") if isinstance(goals_cfg.get("bridge"), dict) else {}
        proactive_cfg = (companion.get("proactive_topic")
                         if isinstance(companion.get("proactive_topic"), dict) else {})
        return {
            "templates": list_templates(),
            "autonomy_levels": list(AUTONOMY_LEVELS),
            "statuses": list(GOAL_STATUSES),
            "caps": {
                "bridge_enabled": bool(bridge_cfg.get("enabled", False)),
                "proactive_enabled": bool(proactive_cfg.get("enabled", False)),
            },
            # P24 建目标向导：参数枚举源（解锁项/会员档/官网产品/阶段词表）
            "pickers": _pickers(),
        }

    @app.get("/api/goals/for-conversation")
    async def goals_for_conversation(
        request: Request, conversation_id: str = "", _auth=Depends(auth_dep)
    ):
        svc = _require_enabled(request)
        conv = str(conversation_id or "").strip()
        if not conv:
            raise HTTPException(400, tr(request, "err.goals.conversation_required"))
        store = _store(svc)
        platform, account_id, chat_key = _split_conversation_id(conv)
        goal = store.find_active_goal(
            conversation_id=conv, platform=platform, chat_key=chat_key,
            account_id=account_id)
        if goal is None:
            # 无活跃目标 → 给最近一条终态目标（卡片显示「上一个目标的结果」）
            recent = [g for g in store.list_goals(limit=20)
                      if str(g.get("conversation_id") or "") == conv]
            last = recent[0] if recent else None
            return {
                "goal": None,
                "last": svc.goal_view(last, lang=_lang(request)) if last else None,
            }
        lang = _lang(request)
        view = _attach_slots_progress(
            _attach_products(
                _refreshed_view(svc, store, goal, lang=lang),
                store, lang=lang),
            store, lang=lang)
        return {"goal": view, "last": None}

    @app.get("/api/goals/report")
    async def goals_report(
        request: Request, days: int = 30, _auth=Depends(auth_dep)
    ):
        """结果闭环读数面（P2）：窗口期终态聚合（模板×结果、成功率、
        平均达成天数）+ 拍/坐席反馈计数 + 最近终态列表。周审据此调模板/曲线。"""
        svc = _require_enabled(request)
        store = _store(svc)
        import time as _t
        d = max(1, min(int(days or 30), 180))
        report = store.outcome_report(_t.time() - d * 86400.0)
        report["window_days"] = d
        return report

    @app.get("/api/goals/instr-samples")
    async def goals_instr_samples(
        request: Request, limit: int = 20, _auth=Depends(auth_dep)
    ):
        """P23 质量抽检：坐席指令 → 拟稿产出 留样尾窗（新→旧，无客户原文）。

        「模型有没有照指令做」没法当门禁断言，周审用这 20 条人耳抽检替代
        拍脑袋。样本由回复台指令链产生（desktop smart-reply 落盘），不依赖
        goals 开关 → 与 readiness 同豁免，**不走** ``_require_enabled``。"""
        from src.inbox.instr_samples import read_instr_samples
        rows = read_instr_samples(limit=max(1, min(int(limit or 20), 100)))
        return {"ok": True, "n": len(rows), "samples": rows}

    @app.get("/api/goals/readiness")
    async def goals_readiness(request: Request, _auth=Depends(auth_dep)):
        """开闸就绪度（P10）+ 校准建议（P12）。

        总闸/自动建/人设/绑定/目录/订单/留存环一次看清；并附
        ``calibration``（priority/hints/personas_href）与可选 30 天
        churn_outcomes 对症提示。goals 关也 200（正是「为什么跑不起来」），
        不走 ``_require_enabled``。"""
        from src.companion.goals.calibration import growth_calibration
        from src.companion.goals.readiness import growth_readiness
        from src.companion.goals.stats import get_goal_stats
        snap = growth_readiness(_cfg_root())
        report = None
        try:
            if (snap.get("checks") or {}).get("goals_enabled"):
                import time as _t
                report = _store(_svc()).outcome_report(
                    _t.time() - 30 * 86400.0)
        except Exception:
            report = None
        stats_dump: Dict[str, Any] = {}
        try:
            stats_dump = get_goal_stats().dump()
        except Exception:
            stats_dump = {}
        try:
            snap["calibration"] = growth_calibration(
                readiness=snap, report=report, stats=stats_dump)
        except Exception:
            snap["calibration"] = {
                "priority": "idle", "hints": [], "focus": "",
                "personas_href": snap.get("personas_href") or "/personas",
            }
        # P16：进程漏斗计数随就绪快照一起出（周审 CLI 单靠本路由即可读全
        # 「拍/注入/目录/画像/守卫剥离」——workspace metrics 是坐席会话口径，
        # admin bearer 读不到）。内容为计数+守卫片段，无客户原文。
        snap["stats"] = stats_dump
        return snap

    @app.post("/api/goals/batch")
    async def goals_batch(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        """批量 campaign（P2）：一次给多个会话建同款目标。逐目标沿用单建的
        全部护栏（模板校验/每会话活跃上限），单条失败只记 skipped 不整批中断。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        from src.companion.goals.templates import get_template
        body = payload or {}
        template_id = str(body.get("template") or "").strip()
        tmpl = get_template(template_id)
        if tmpl is None:
            raise HTTPException(400, tr(request, "err.goals.template_unknown"))
        raw_targets = body.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise HTTPException(400, tr(request, "err.goals.batch_targets_required"))

        cfg = svc.resolve_goals_cfg(_cfg_root())
        batch_cfg = cfg.get("batch") or {}
        try:
            max_per_call = int(batch_cfg.get("max_per_call", 50) or 50)
        except (TypeError, ValueError):
            max_per_call = 50
        max_per_call = max(1, min(max_per_call, 200))
        if len(raw_targets) > max_per_call:
            raise HTTPException(400, tr(
                request, "err.goals.batch_too_many", n=max_per_call))

        try:
            cap = int(cfg.get("max_active_per_conversation", 1) or 1)
        except (TypeError, ValueError):
            cap = 1
        from src.companion.goals.store import MAX_ACTIVE_PER_CONVERSATION
        cap = max(1, min(cap, MAX_ACTIVE_PER_CONVERSATION))

        try:
            deadline_days = float(body.get("deadline_days") or 0)
        except (TypeError, ValueError):
            deadline_days = 0.0
        if deadline_days <= 0:
            deadline_days = float(tmpl.get("default_days") or 14)
        deadline_days = max(1.0, min(deadline_days, 180.0))
        autonomy = str(body.get("autonomy") or "suggest")
        try:
            priority = int(body.get("priority") or 1)
        except (TypeError, ValueError):
            priority = 1
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        title = str(body.get("title") or "")[:120]
        try:
            actor = str(request.session.get("user", "") or "")[:52]
        except Exception:
            actor = ""

        store = _store(svc)
        created = 0
        skipped = []
        seen = set()
        for raw in raw_targets:
            if isinstance(raw, str):
                conv = raw.strip()
                platform, account_id, chat_key = _split_conversation_id(conv)
            elif isinstance(raw, dict):
                platform = str(raw.get("platform") or "").strip()
                account_id = str(raw.get("account_id") or "").strip()
                chat_key = str(raw.get("chat_key") or "").strip()
                conv = str(raw.get("conversation_id") or "").strip()
                if conv and not (platform and chat_key):
                    platform, account_id, chat_key = _split_conversation_id(conv)
                elif not conv and platform and account_id and chat_key:
                    conv = f"{platform}:{account_id}:{chat_key}"
            else:
                skipped.append({"target": str(raw)[:80], "reason": "bad_target"})
                continue
            if not conv or not platform or not chat_key:
                skipped.append({"target": conv or str(raw)[:80],
                                "reason": "bad_target"})
                continue
            if conv in seen:                     # 同批重复 → 只建一条
                skipped.append({"target": conv, "reason": "duplicate"})
                continue
            seen.add(conv)
            if store.count_active_for_conversation(conv) >= cap:
                skipped.append({"target": conv, "reason": "active_limit"})
                continue
            goal = store.create_goal(
                conversation_id=conv, platform=platform, account_id=account_id,
                chat_key=chat_key, template=template_id, title=title,
                params=params, autonomy=autonomy, priority=priority,
                deadline_days=deadline_days,
                created_by=(actor + ":batch") if actor else "batch",
            )
            if goal is None:
                skipped.append({"target": conv, "reason": "create_failed"})
                continue
            created += 1
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_created()
            except Exception:
                pass
        return {"ok": True, "requested": len(raw_targets),
                "created": created, "skipped": skipped}

    def _profile_view(svc, pf: str, ck: str, lang: str) -> Dict[str, Any]:
        """画像视图（GET 与 POST 共用同一形状，前端保存后免二次拉取）。"""
        from src.companion.goals.profile_slots import (
            SLOTS,
            fill_rates,
            missing_slots,
        )
        store = _store(svc)
        prof = store.get_customer_profile(pf, ck)
        fields = dict((prof or {}).get("fields") or {})
        slots = []
        for s in SLOTS:
            key = s["key"]
            cell = fields.get(key) if isinstance(fields.get(key), dict) else {}
            slots.append({
                "key": key,
                "track": s["track"],
                "label": str(s.get(
                    "label_en" if lang.startswith("en") else "label_zh") or key),
                "value": str((cell or {}).get("v") or ""),
                "src": str((cell or {}).get("src") or ""),
                "ts": float((cell or {}).get("ts") or 0),
            })
        return {
            "platform": pf, "chat_key": ck,
            "slots": slots,
            "fill": fill_rates(fields),
            "missing_bant": [m["key"] for m in
                             missing_slots(fields, track="bant", limit=6)],
            "updated_at": float((prof or {}).get("updated_at") or 0),
        }

    @app.get("/api/goals/profile")
    async def goals_profile_get(
        request: Request,
        platform: str = "",
        chat_key: str = "",
        conversation_id: str = "",
        _auth=Depends(auth_dep),
    ):
        """客户画像卡（P1）：双轨槽位 + 值/来源/时间 + 填充率 + 缺口。
        坐席右栏「客户」页消费；``conversation_id`` 可替代 platform+chat_key。"""
        svc = _require_enabled(request)
        pf = str(platform or "").strip()
        ck = str(chat_key or "").strip()
        if not (pf and ck) and conversation_id:
            pf2, _acct, ck2 = _split_conversation_id(conversation_id)
            pf, ck = pf or pf2, ck or ck2
        if not pf or not ck:
            raise HTTPException(400, tr(request, "err.goals.conversation_required"))
        return _profile_view(svc, pf, ck, _lang(request))

    @app.post("/api/goals/profile")
    async def goals_profile_set(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        """坐席手录画像槽位（overwrite 语义：传值覆盖、传空串清除；
        未知槽位键忽略）。auto 采集永不覆盖坐席手录，此处反向覆盖一切。
        返回保存后的完整画像视图（与 GET 同形，免二次拉取）。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        body = payload or {}
        pf = str(body.get("platform") or "").strip()
        ck = str(body.get("chat_key") or "").strip()
        conv = str(body.get("conversation_id") or "").strip()
        if not (pf and ck) and conv:
            pf2, _acct, ck2 = _split_conversation_id(conv)
            pf, ck = pf or pf2, ck or ck2
        if not pf or not ck:
            raise HTTPException(400, tr(request, "err.goals.conversation_required"))
        fields = body.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise HTTPException(400, tr(request, "err.goals.profile_fields_required"))
        store = _store(svc)
        store.upsert_customer_profile(
            pf, ck, fields, source="agent", overwrite=True)
        view = _profile_view(svc, pf, ck, _lang(request))
        view["ok"] = True
        return view

    @app.post("/api/goals/order-hook")
    async def goals_order_hook(request: Request, payload: Dict[str, Any]):
        """官网订单回流（P2 成交闭环）：下单页 ``?ref=<conversation_id>`` 提交后
        官网后端 POST 回本端点 → 按 ref 反查活跃目标 → 自动结算 done。

        **无会话 auth**（外部 webhook）——独立 token 鉴权（``Authorization:
        Bearer <token>`` / ``X-Goals-Token`` 头 / body.token 三选一，与
        ``companion.goals.order_hook.token`` 恒时比较）。推荐 Bearer 头：
        它同时天然通过全站 CSRF 中间件的 Bearer 豁免口（本路径亦已加显式
        豁免，X-Goals-Token/body 形式同样可达——CSRF 防的是浏览器 ambient
        session，本端点不认 session，token 即唯一边界）。
        幂等：同 order_id 已回流过 → 直接返回 dup，不重复结算。
        没匹配到目标也返回 200（订单本就可能来自非目标流量），只记 stats。
        """
        svc = _svc()
        cfg = svc.resolve_goals_cfg(_cfg_root())
        hook = cfg.get("order_hook") or {}
        if not (cfg.get("enabled") and isinstance(hook, dict)
                and hook.get("enabled")):
            raise HTTPException(403, tr(request, "err.goals.disabled"))
        import hmac as _hmac
        token = str(hook.get("token") or "")
        auth_h = str(request.headers.get("authorization") or "")
        bearer = auth_h[7:].strip() if auth_h.startswith("Bearer ") else ""
        given = str(bearer
                    or request.headers.get("x-goals-token")
                    or (payload or {}).get("token") or "")
        if not token or not given or not _hmac.compare_digest(token, given):
            raise HTTPException(403, tr(request, "err.goals.bad_hook_token"))
        body = payload or {}
        ref = str(body.get("ref") or "").strip()
        if ref.lower().startswith("chat:"):
            ref = ref[5:]
        if not ref:
            raise HTTPException(400, tr(request, "err.goals.ref_required"))
        # 结算核心与 order_pull（引擎拉单）共用 service.settle_order_ref 单一入口
        res = svc.settle_order_ref(
            _store(svc), ref=ref,
            order_id=str(body.get("order_id") or ""),
            plan=str(body.get("plan") or ""),
            cfg_root=_cfg_root())
        if not res.get("matched"):
            return {"ok": True, "matched": False}
        if res.get("dup"):
            return {"ok": True, "matched": True,
                    "goal_id": res.get("goal_id"), "dup": True}
        if not res.get("updated"):
            raise HTTPException(500, tr(request, "err.goals.update_failed"))
        return {"ok": True, "matched": True, "goal_id": res.get("goal_id")}

    def _agenda_peer_names(items) -> Dict[str, str]:
        """B3 ops 抽屉：把清单行的 conversation_id 批量解析成客户展示名。

        读 inbox 主表 ``get_conversations_for_ids``（一次 IN 批查，不逐行打库），
        只收非空名（优先 ``display_name``，兼容 ``name``）；无 store／任何异常
        → 软失败返空 map，绝不拖垮清单本体。
        """
        try:
            ids = [c for c in (str(it.get("conversation_id") or "")
                               for it in items or []) if c]
            if not ids:
                return {}
            inbox = _inbox_store()
            if inbox is None:
                return {}
            out: Dict[str, str] = {}
            for cid, row in (inbox.get_conversations_for_ids(ids) or {}).items():
                row = row or {}
                name = str(row.get("display_name") or row.get("name") or "").strip()
                if name:
                    out[str(cid)] = name
            return out
        except Exception:
            logger.debug("agenda names enrichment failed", exc_info=True)
            return {}

    @app.get("/api/goals/agenda")
    async def goals_agenda(
        request: Request,
        scope: str = "today",
        limit: int = 100,
        state: str = "",
        names: str = "",
        _auth=Depends(auth_dep),
    ):
        """今日工作清单（P17）：看板给的是「计数」，坐席要的是「点名单」。

        逐条走 ``GET /api/goals`` 同一条 settle-on-read（``_refreshed_view``）——
        不另开第二条结算路，清单里的力度/反馈态与右栏卡逐字一致。
        取数上限受 ``_LIST_SETTLE_CAP`` 护（逐条 settle=逐条 DB 读）。

        - ``scope``：目前只有 ``today``（别的值 400，不装死返空清单）
        - ``state``：可选筛选 push/pending/hold/adopted/rejected（``all``/空=不筛）
        - ``names``：``1``/``true``（大小写不限）＝信封顶层附
          ``names: {conversation_id: 展示名}`` 映射（B3 ops 抽屉直显客户名用；
          只收非空名，软失败返 ``{}``）；其他值/缺省＝完全不带 ``names`` 键
        - ``counts``：**过滤前**的真实清单规模（``total``）+ 各态计数；
          ``shown``＝过滤后条数，前端「筛掉了多少」直接可显

        只读端点，viewer 照常可读。清单行本身仍不联表客户昵称（``ITEM_KEYS``
        行契约不动）——展示名走可选 ``?names=1`` 的顶层 map 按需 opt-in，
        默认路径不反向依赖 inbox store。
        """
        svc = _require_enabled(request)
        sc = str(scope or "").strip().lower() or "today"
        if sc != "today":
            raise HTTPException(400, tr(request, "err.goals.bad_scope"))
        st = str(state or "").strip().lower()
        if st in ("", "all"):
            st = ""
        elif st not in svc.AGENDA_STATES:
            raise HTTPException(400, tr(request, "err.goals.bad_state"))
        try:
            lim = int(limit or 100)
        except (TypeError, ValueError):
            lim = 100
        lim = max(1, min(lim, 200))

        from src.companion.goals.planner import day_key
        store = _store(svc)
        lang = _lang(request)
        items = []
        for g in store.list_goals(
                status="active", limit=min(lim, _LIST_SETTLE_CAP)):
            try:
                view = _refreshed_view(svc, store, g, lang=lang)
                # settle-on-read 可能当场把目标结算成终态（到期/已达成）——
                # 那它今天已不是待办，不该占清单名额（也不该虚增 total）
                if str(view.get("status") or "") != "active":
                    continue
                items.append(svc.agenda_item(view))
            except Exception:      # 单条坏数据不该让整张清单 500
                logger.debug("agenda item skipped", exc_info=True)
        items.sort(key=svc.agenda_sort_key)
        counts = svc.agenda_counts(items)
        if st:
            items = [it for it in items if svc.agenda_state_match(it, st)]
        counts["shown"] = len(items)
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_agenda_read()
        except Exception:
            pass
        out = {"ok": True, "scope": "today", "day": day_key(),
               "counts": counts, "items": items}
        if str(names or "").strip().lower() in ("1", "true"):
            out["names"] = _agenda_peer_names(items)
        return out

    @app.get("/api/goals")
    async def goals_list(
        request: Request,
        status: str = "",
        platform: str = "",
        account_id: str = "",
        limit: int = 50,
        _auth=Depends(auth_dep),
    ):
        svc = _require_enabled(request)
        store = _store(svc)
        lang = _lang(request)
        goals = store.list_goals(
            status=status, platform=platform, account_id=account_id,
            limit=min(int(limit or 50), _LIST_SETTLE_CAP))
        views = []
        for g in goals:
            if str(g.get("status")) == "active":
                views.append(_refreshed_view(svc, store, g, lang=lang))
            else:
                views.append(svc.goal_view(g, lang=lang))
        return {"goals": views, "summary": store.summary()}

    @app.post("/api/goals")
    async def goals_create(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        svc = _require_enabled(request)
        _deny_viewer(request)
        from src.companion.goals.templates import get_template
        body = payload or {}
        template_id = str(body.get("template") or "").strip()
        if get_template(template_id) is None:
            raise HTTPException(400, tr(request, "err.goals.template_unknown"))

        conv = str(body.get("conversation_id") or "").strip()
        platform = str(body.get("platform") or "").strip()
        account_id = str(body.get("account_id") or "").strip()
        chat_key = str(body.get("chat_key") or "").strip()
        if conv and not (platform and chat_key):
            platform, account_id, chat_key = _split_conversation_id(conv)
        elif not conv and platform and account_id and chat_key:
            conv = f"{platform}:{account_id}:{chat_key}"
        if not conv or not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.goals.conversation_required"))

        store = _store(svc)
        cfg = svc.resolve_goals_cfg(_cfg_root())
        try:
            cap = int(cfg.get("max_active_per_conversation", 1) or 1)
        except (TypeError, ValueError):
            cap = 1
        from src.companion.goals.store import MAX_ACTIVE_PER_CONVERSATION
        cap = max(1, min(cap, MAX_ACTIVE_PER_CONVERSATION))
        if store.count_active_for_conversation(conv) >= cap:
            raise HTTPException(409, tr(request, "err.goals.active_limit", n=cap))

        try:
            deadline_days = float(body.get("deadline_days") or 0)
        except (TypeError, ValueError):
            deadline_days = 0.0
        if deadline_days <= 0:
            tmpl = get_template(template_id) or {}
            deadline_days = float(tmpl.get("default_days") or 14)
        deadline_days = max(1.0, min(deadline_days, 180.0))

        try:
            actor = str(request.session.get("user", "") or "")[:60]
        except Exception:
            actor = ""
        goal = store.create_goal(
            conversation_id=conv, platform=platform, account_id=account_id,
            chat_key=chat_key, template=template_id,
            title=str(body.get("title") or "")[:120],
            params=body.get("params") if isinstance(body.get("params"), dict) else {},
            autonomy=str(body.get("autonomy") or "suggest"),
            priority=int(body.get("priority") or 1),
            deadline_days=deadline_days,
            created_by=actor,
        )
        if goal is None:
            raise HTTPException(500, tr(request, "err.goals.create_failed"))
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_created()
        except Exception:
            pass
        return {"ok": True,
                "goal": _refreshed_view(svc, store, goal, lang=_lang(request))}

    # ── 动态路径（后注册）────────────────────────────────────────────────────
    @app.get("/api/goals/{goal_id}")
    async def goals_detail(
        request: Request, goal_id: str, _auth=Depends(auth_dep)
    ):
        svc = _require_enabled(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        lang = _lang(request)
        view = (_refreshed_view(svc, store, goal, lang=lang)
                if str(goal.get("status")) == "active"
                else svc.goal_view(goal, lang=lang))
        return {
            "goal": view,
            "actions": store.list_actions(goal_id, limit=30),
            "events": store.list_events(goal_id, limit=50),
        }

    @app.post("/api/goals/{goal_id}/update")
    async def goals_update(
        request: Request, goal_id: str, payload: Dict[str, Any],
        _auth=Depends(auth_dep),
    ):
        """改字段。``deadline_days`` 即「调节奏」（坐席 UI 的改期限入口）：

        - 截止时间恒从 ``start_ts`` 锚定重算（与建目标/「第X/Y天」显示同一坐标系）；
        - 护栏①：终态目标（done/failed/expired/cancelled）的期限是死数据，改了
          也不会被结算读到 → 409（静默接受＝让人误以为改了有用）；
        - 护栏②：新截止时间落在过去（已进行 2 天还改成 1 天）→ 400 并告知
          最小可改天数——那是「立即判死」不是「加速」，想立即结束该走
          status 路由的 done/cancel；
        - 当日拍重排：只对 ``planned`` 且无坐席反馈的今日拍生效（consumed/sent
          是既成事实、adopted/rejected 是人的决定，均不动）——「加速」当天
          生效而不是明天；
        - 事件台账记差值（``deadline_days:3->1``），复盘时间线能看出节奏为何变。
        active 目标返回 settle-on-read 后的完整视图（带 today），与 detail 同口径。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        body = payload or {}
        fields: Dict[str, Any] = {}
        if isinstance(body.get("title"), str):
            fields["title"] = body["title"].strip()[:120]
        if body.get("autonomy") is not None:
            fields["autonomy"] = str(body.get("autonomy") or "")
        if body.get("priority") is not None:
            try:
                fields["priority"] = int(body.get("priority"))
            except (TypeError, ValueError):
                pass
        deadline_detail = ""
        deadline_direction = ""
        if body.get("deadline_days") is not None:
            try:
                dd = float(body.get("deadline_days"))
            except (TypeError, ValueError):
                dd = 0.0
            if dd > 0:
                if str(goal.get("status") or "") not in ("active", "paused"):
                    raise HTTPException(
                        409, tr(request, "err.goals.deadline_terminal"))
                import time as _t
                now = _t.time()
                start = float(goal.get("start_ts") or now)
                dd = min(dd, 180.0)
                new_dl = start + dd * 86400.0
                if new_dl <= now:
                    day_idx = int((now - start) // 86400.0) + 1
                    raise HTTPException(400, tr(
                        request, "err.goals.deadline_past", day=day_idx))
                fields["deadline_ts"] = new_dl
                old_dl = float(goal.get("deadline_ts") or 0.0)
                old_days = ((old_dl - start) / 86400.0
                            if old_dl > start else 0.0)

                def _fmt_d(x: float) -> str:
                    r = round(float(x), 1)
                    return str(int(r)) if float(r).is_integer() else str(r)

                deadline_detail = (
                    f"deadline_days:{_fmt_d(old_days)}->{_fmt_d(dd)}")
                # 加急/延期方向（进程脉搏；耐久口径在事件明细）——等值改动不计
                if dd < old_days - 0.05:
                    deadline_direction = "shorten"
                elif dd > old_days + 0.05:
                    deadline_direction = "extend"
        if isinstance(body.get("params"), dict):
            merged = dict(goal.get("params") or {})
            merged.update(body["params"])
            fields["params"] = merged
        if not fields:
            raise HTTPException(400, tr(request, "err.goals.nothing_to_update"))
        if not store.update_goal_fields(goal_id, **fields):
            raise HTTPException(500, tr(request, "err.goals.update_failed"))
        parts = sorted(k for k in fields.keys() if k != "deadline_ts")
        if deadline_detail:
            parts.append(deadline_detail)
        store.add_event(goal_id, "updated", ",".join(parts))
        if deadline_direction:
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_deadline_edit(deadline_direction)
            except Exception:
                pass
        goal = store.get_goal(goal_id)
        lang = _lang(request)
        if goal is not None and str(goal.get("status") or "") == "active":
            if deadline_detail:
                from src.companion.goals.planner import day_key
                store.delete_planned_action(goal_id, day_key())
            return {"ok": True,
                    "goal": _refreshed_view(svc, store, goal, lang=lang)}
        return {"ok": True,
                "goal": svc.goal_view(goal, lang=lang)}

    @app.post("/api/goals/{goal_id}/beat/feedback")
    async def goals_beat_feedback(
        request: Request, goal_id: str, payload: Dict[str, Any],
        _auth=Depends(auth_dep),
    ):
        """suggest 档坐席闭环（P2）：对「今日拍」采纳/驳回/撤销。
        - adopt：正向信号入事件台账（不改拍状态；detail 标记供卡片显示 ✓）。
        - reject：今日拍置 skipped（当天立即停注入/停主动带意图），事件回流
          planner——近窗驳回 1 次力度封顶 soft、≥2 次退避陪伴日。
        - undo（P17）：撤销上一次反馈，**真撤**不是擦标记——
          驳回撤销把拍打回 planned（``build_block_for_chat`` 的 skipped/blocked
          闸门随之放开，当天恢复注入）并落 ``beat_reject_undone``，后者在
          ``refresh_goal`` 里逐一抵消近窗 ``beat_rejected``（见
          ``planner.effective_rejects``），明天不再被无故降档；
          采纳撤销只清 detail 标记（采纳本就没改状态）。
          没有既有反馈时是**幂等空操作**：照常 200 出视图，不落事件——
          连点两下撤销不该在台账里刷出两行。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        if str(goal.get("status")) != "active":
            raise HTTPException(409, tr(request, "err.goals.not_active"))
        body = payload or {}
        verdict = str(body.get("verdict") or "").strip().lower()
        if verdict not in ("adopt", "reject", "undo"):
            raise HTTPException(400, tr(request, "err.goals.bad_verdict"))
        from src.companion.goals.planner import day_key
        action = store.get_action(goal_id, day_key())
        if action is None:
            raise HTTPException(409, tr(request, "err.goals.no_beat_today"))
        reason = str(body.get("reason") or "agent").strip()[:120]
        aid = str(action.get("action_id") or "")
        if verdict == "undo":
            # 撤销哪一种，由拍现状说话（与清单渲染同一个判定函数）
            prev = svc.beat_feedback_state(action)
            if prev == "rejected":
                store.mark_action(aid, "planned", detail="")
                store.add_event(goal_id, "beat_reject_undone", reason)
            elif prev == "adopted":
                # 采纳没动过状态 → 原状态原样写回，只清标记
                store.mark_action(
                    aid, str(action.get("status") or "planned"), detail="")
                store.add_event(goal_id, "beat_adopt_undone", reason)
            if prev:
                try:
                    from src.companion.goals.stats import get_goal_stats
                    get_goal_stats().record_feedback_undo()
                except Exception:
                    pass
        elif verdict == "reject":
            store.mark_action(aid, "skipped", detail=f"rejected:{reason}")
            store.add_event(goal_id, "beat_rejected", reason)
        else:
            # 采纳保持拍状态（planned 后续照常 consumed），只落 detail + 事件
            store.mark_action(
                aid, str(action.get("status") or "planned"), detail="adopted")
            store.add_event(goal_id, "beat_adopted", reason)
        if verdict in ("adopt", "reject"):
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_feedback(verdict)
            except Exception:
                pass
        goal = store.get_goal(goal_id) or goal
        action = store.get_action(goal_id, day_key())
        return {"ok": True,
                "goal": svc.goal_view(goal, action, lang=_lang(request))}

    @app.post("/api/goals/{goal_id}/status")
    async def goals_status(
        request: Request, goal_id: str, payload: Dict[str, Any],
        _auth=Depends(auth_dep),
    ):
        """生命周期操作（pause/resume/cancel/done）。

        ``action="done"`` 可附**可选** ``meta``＝成交归因
        ``{product?, amount?, note?}``（``sanitize_won_meta`` 消毒截断，未知键丢弃，
        金额非必填——录成交不该有摩擦）。落 ``goal_events(kind="won_meta")`` 的
        紧凑 JSON：零 schema 迁移，且 ``result`` 保持 ``manual:agent`` 原样，
        ``outcome_report`` 的 won 判定（``manual:`` 前缀）与 ``sold_plan_counts``
        的 ``order:`` 口径逐字不变。其余 action 传了 meta 一律忽略。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        action = str((payload or {}).get("action") or "").strip().lower()
        cur = str(goal.get("status") or "")
        transitions = {
            ("active", "pause"): "paused",
            ("paused", "resume"): "active",
            ("active", "cancel"): "cancelled",
            ("paused", "cancel"): "cancelled",
            # 坐席拍板「已成交/已达成」——线下收单、口头成交等 webhook 看不见的
            # 终局，人工闭环（P2 成交回流的保底路径）。
            ("active", "done"): "done",
            ("paused", "done"): "done",
        }
        new_status = transitions.get((cur, action))
        if new_status is None:
            raise HTTPException(400, tr(request, "err.goals.bad_transition",
                                        status=cur, action=action or "?"))
        import time as _t
        fields: Dict[str, Any] = {"status": new_status}
        if new_status == "cancelled":
            fields["done_at"] = _t.time()
        elif new_status == "done":
            fields["done_at"] = _t.time()
            fields["progress"] = 1.0
            fields["result"] = "manual:agent"
            try:
                from src.companion.goals.templates import get_template
                tmpl = get_template(str(goal.get("template") or "")) or {}
                ms = tmpl.get("milestones") or []
                if ms:
                    fields["milestone_idx"] = len(ms) - 1
            except Exception:
                pass
        if not store.update_goal_fields(goal_id, **fields):
            raise HTTPException(500, tr(request, "err.goals.update_failed"))
        store.add_event(goal_id, "status", f"{cur}->{new_status}:manual")
        # 成交归因（P17）：只在 done 收，全空则连事件都不落（不留噪声行）
        if new_status == "done":
            won_meta = svc.sanitize_won_meta((payload or {}).get("meta"))
            if won_meta:
                import json as _json
                store.add_event(goal_id, "won_meta", _json.dumps(
                    won_meta, ensure_ascii=False, separators=(",", ":")))
        try:
            from src.companion.goals.stats import get_goal_stats
            if new_status == "cancelled":
                get_goal_stats().record_terminal("cancelled")
            elif new_status == "done":
                get_goal_stats().record_terminal("done")
            elif new_status == "paused":
                get_goal_stats().record_paused()
        except Exception:
            pass
        goal = store.get_goal(goal_id)
        # P5 留存环：手动标成交（线下收款等）与订单回流同权——成交即续期
        # （manual=True：只有带 catalog 能力的模板才有「续费」语义）
        if new_status == "done" and goal is not None:
            svc.maybe_create_retention_goal(
                store, _cfg_root(), goal, manual=True,
                plan=str((goal.get("params") or {}).get("last_plan") or ""))
            # P7 回流再转化：坐席手动把挽回目标标 done（对方回来了）与
            # settle-on-read 同权（内部门控 created_by=winback_auto，普通目标零影响）
            svc.maybe_spawn_reconvert(store, _cfg_root(), goal)
        return {"ok": True,
                "goal": svc.goal_view(goal, lang=_lang(request))}

    logger.info("营销目标路由已注册（/api/goals*）")
