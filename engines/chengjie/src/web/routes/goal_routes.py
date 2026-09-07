"""工作目标（work goals；1.0.77 前叫「营销目标」，N-3 #241 统一）后台 API。

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

# #166：ledger.settle_goal 里有**专属信号分支**的模板（进度=里程碑/信号驱动）；
# 其余（custom / 未知模板）走 else 分支——进度只跟日历爬，卡上「89%」是时间不是
# 推进。右栏卡按 beats.progress_kind 标注口径。与 ledger 分支表同步（门禁钉住）。
_SIGNAL_PROGRESS_TEMPLATES = frozenset((
    "conversion_unlock", "conversion_subscribe", "relationship_stage",
    "relationship_intimacy", "engagement_reactivate", "acquire_and_convert",
    "retention_expand", "profile_discovery",
))


def _split_conversation_id(conversation_id: str):
    """``platform:account_id:chat_key`` → 三元组（chat_key 可含冒号，split 限 2 刀）。"""
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) == 3 and all(p.strip() for p in parts[:2]):
        return parts[0].strip(), parts[1].strip(), parts[2].strip()
    return "", "", ""


def _find_beat_action(store: Any, goal: Dict[str, Any], body: Any = None):
    """拍反馈查找：显式 day（前端 ``g.today.day``）→ 当前槽 → 最近一行。

    限时档的槽不是日历日，不能再只查 ``day_key()``。
    """
    from src.companion.goals.pace import resolve_pace, slot_key
    gid = str((goal or {}).get("goal_id") or "")
    explicit = str((body or {}).get("day") or "").strip()
    if explicit:
        a = store.get_action(gid, explicit)
        if a is not None:
            return a
    pace = resolve_pace(goal)
    sk = slot_key(pace, None, 0.0)
    a = store.get_action(gid, sk)
    if a is not None:
        return a
    rows = store.list_actions(gid, limit=8)
    return rows[0] if rows else None


def register_goal_routes(app, auth_dep, config_manager=None):
    """注册工作目标路由。``config_manager`` 供配置段/库路径解析。"""

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

    def _client_hide(request: Request) -> bool:
        """「用户版隐藏」是否生效（M-5 A / #217 / D-M6）：与模板全局
        ``ui_client_hide`` 同一口径＝client 形态且未开开发者模式（L-4 A）。
        任何异常按不藏（fail-open：内部机器少藏一项只是噪音，藏错让坐席缺入口）。"""
        try:
            from src.web.ui_visibility import (
                client_hide_active,
                resolve_developer_mode,
            )
            try:
                sess = request.session
            except Exception:
                sess = None
            return bool(client_hide_active(_cfg_root(), resolve_developer_mode(sess)))
        except Exception:
            return False

    def _business_domain() -> str:
        """业务域单一真值（N-3 #241 / D-N1）：companion / sales。读配置（显式键或
        部署形态推导），异常按 sales（＝旧行为，不多藏）。"""
        try:
            from src.utils.business_domain import resolve_business_domain
            return resolve_business_domain(_cfg_root())
        except Exception:
            return "sales"

    def _template_hidden(request: Request, template_id: str) -> bool:
        """模板在「当前形态 + 业务域」下是否被藏：M-5 用户版隐藏 ∪ N-3 陪伴域隐藏。"""
        from src.companion.goals.templates import is_hidden_template
        return is_hidden_template(
            template_id, client_hide=_client_hide(request),
            business_domain=_business_domain())

    def _deny_hidden_template(request: Request, template_id: str) -> None:
        """用户版 / 陪伴域不建「转化成交」类目目标（#217 / #241）：藏了入口还放 API
        建＝旧前端「上次用过」/ 脚本仍能把 AI 拉去推销智聊。销售域的 partner /
        internal / 开发者模式不拦；存量目标不受影响（只闸新建）。"""
        if _template_hidden(request, template_id):
            raise HTTPException(
                403, tr(request, "err.goals.template_retired_client"))

    def _attach_tier(view, request: Request):
        """用户版 / 陪伴域：存量「转化成交」目标卡标 ``template_retired=True``（#217，
        「该模板已下线，建议改用自定义目标」）。其他类目不加键（前端缺键＝不渲染）；
        best-effort 绝不抛。"""
        if not isinstance(view, dict):
            return view
        try:
            if _template_hidden(request, str(view.get("template") or "")):
                view["template_retired"] = True
        except Exception:
            logger.debug("attach tier flag skipped", exc_info=True)
        return view

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
                slot_is_stale,
                slot_label,
                slot_src,
                slot_value,
            )
            sel = parse_selected_slots((view.get("params") or {}).get("slots"))
            if not sel:
                return view
            prof = store.get_customer_profile(
                str(view.get("platform") or ""),
                str(view.get("chat_key") or ""))
            fields = dict((prof or {}).get("fields") or {})
            # src=值来源（auto 正则/llm 抽取/agent 人工）——卡上打勾清单据此
            # 标注「AI 猜的还是人核实的」（P3 2026-08-18，行业 handoff 惯例）；
            # stale=值超 90 天未更新（P1 2026-08-29，提醒顺口再确认而非硬信旧值）
            view["slots_progress"] = [
                {
                    "key": k,
                    "label": slot_label(k, lang),
                    "filled": bool(slot_value(fields, k)),
                    "value": slot_value(fields, k)[:20],
                    "src": slot_src(fields, k),
                    "stale": slot_is_stale(fields, k),
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
            # 人设过滤同注入链（#145③）：刻意复用注入侧同一个解析器，预览与
            # 真推口径不得分叉——坐席照着预览说话，预览多出一条真推不出来的货
            # 比没有预览更坑。pick_products 是 fail-closed，不传＝绑定货全黑。
            pid = _svc()._account_persona(
                _cfg_root(), str(view.get("platform") or ""),
                str(view.get("account_id") or ""))
            view["products"] = [{
                "id": str(p.get("id") or ""),
                "name": str(p.get("name_en" if en else "name_zh")
                            or p.get("name_zh") or p.get("id") or ""),
                "pitch": str(p.get("pitch_en" if en else "pitch_zh")
                             or p.get("pitch_zh") or ""),
                "price_from": str(p.get("price_from") or ""),
                "url": sc.product_link(site, p),
            } for p in sc.pick_products(
                cat, fields, pinned=pinned, limit=2, sold=sold,
                persona_id=pid)]
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
        # N-3 #241：按业务域给表（销售 relation+bant / 陪伴 relation+personal）+ 自定义
        # 标签；sensitive 随行下发（前端默认不勾 + 锁标 + 提示「只多轮自然带出」）。
        try:
            out["discovery_slots"] = _discovery_slots()
        except Exception:
            logger.debug("pickers: discovery_slots failed", exc_info=True)
        return out

    def _discovery_slots() -> list:
        from src.companion.goals.profile_slots import slots_for_domain
        return [
            {"key": str(s.get("key") or ""),
             "track": str(s.get("track") or ""),
             "label_zh": str(s.get("label_zh") or s.get("key") or ""),
             "label_en": str(s.get("label_en") or s.get("label_zh") or s.get("key") or ""),
             "sensitive": bool(s.get("sensitive")),
             "custom": bool(s.get("custom"))}
            for s in slots_for_domain(_business_domain(), include_lifecycle=False)
            if str(s.get("key") or "")
        ]

    # N-3 #241：自定义标签从配置登记（进程级；service 注入链与本路由共用同一注册表）
    try:
        from src.companion.goals.profile_slots import load_custom_slots_from_config
        load_custom_slots_from_config(_cfg_root())
    except Exception:
        logger.debug("custom slots load skipped", exc_info=True)

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
        # #166（2026-09-05）：引擎真相单一出口 service.sprint_engine_status——
        # 旧三键（bridge_enabled/proactive_enabled/sprint_enabled）原样保留给旧前端；
        # 新增「有效性 + 阻塞点」（派发终点 care 关闸/dry_run、平台白名单），
        # 前端据此把「自动推进」灰掉并说清为什么，不再兜售引擎不执行的事。
        es = _svc().sprint_engine_status(
            _cfg_root(), platform=str(request.query_params.get("platform") or ""))
        bd = _business_domain()
        return {
            # M-5 A（#217 / D-M6）：用户版不下发「转化成交」类目（四张预置），
            # partner / internal / 开发者模式照旧——与 L-4 ui_client_hide 同口径；
            # N-3 A（#241 / D-N1）：陪伴业务域一律不下发（域级规则，形态无关）
            "templates": list_templates(
                client_hide=_client_hide(request), business_domain=bd),
            # 业务域随模板库下发：前端画像分组 / 「标成交」字段 / 摸底 chips 据此渲染
            "business_domain": bd,
            "autonomy_levels": list(AUTONOMY_LEVELS),
            "statuses": list(GOAL_STATUSES),
            "caps": {
                "bridge_enabled": bool(es.get("bridge_enabled")),
                "proactive_enabled": bool(es.get("proactive_enabled")),
                # P3 2026-08-30：冲刺推进器开关——前端据此如实描述限时档行为
                # （开=「AI 按时间表主动出击」；关=「对方开口才推进」）
                "sprint_enabled": bool(es.get("sprint_enabled")),
                "sprint_effective": bool(es.get("sprint_effective")),
                "sprint_blockers": list(es.get("sprint_blockers") or []),
                "sprint_platforms": list(es.get("sprint_platforms") or []),
                "natural_auto_effective": bool(es.get("natural_auto_effective")),
                "natural_auto_blockers": list(es.get("natural_auto_blockers") or []),
            },
            "engine": es,
            # P24 建目标向导：参数枚举源（解锁项/会员档/官网产品/阶段词表）
            "pickers": _pickers(),
        }

    @app.get("/api/goals/engine-status")
    async def goals_engine_status(
        request: Request, platform: str = "", _auth=Depends(auth_dep)
    ):
        """目标引擎真相（#166）：``{enabled, inject_enabled, sprint_enabled,
        sprint_effective, sprint_blockers, sprint_platforms, care_enabled,
        care_dry_run, bridge_enabled, proactive_enabled, natural_auto_effective,
        natural_auto_blockers}``。goals 关也 200（readiness 同哲学：关着也要能
        说明为什么）；``platform`` 给了就把白名单判定计入 blockers。"""
        return _svc().sprint_engine_status(_cfg_root(), platform=platform)

    def _attach_notified(view, store):
        """done 终局视图附「完成提醒已发出」回执（P2 2026-08-18）——读通知幂等
        标记（goal_events.completed_notified，与扫描器同一事实源），终局卡据此
        渲染「✓ 已推送提醒」。非 done / 查询异常不附键（前端缺键=不渲染）。"""
        if not isinstance(view, dict) or str(view.get("status")) != "done":
            return view
        try:
            from src.companion.goals.notify import NOTIFIED_EVENT_KIND
            view["notified"] = any(
                str(e.get("kind") or "") == NOTIFIED_EVENT_KIND
                for e in store.list_events(
                    str(view.get("goal_id") or ""), limit=80))
        except Exception:
            logger.debug("attach notified skipped", exc_info=True)
        return view

    def _attach_sprint_live(view, store):
        """活跃目标 → 调度透明化字段（P3 2026-08-30；D1b 自然档同挂）：
        ``sprint_live: {ticker_on, beats_used, next_phase_ts, nudgeable}``——
        卡上「下一次跟进≈xx:xx / 引擎未开」状态行的数据源。
        非活跃零开销直通；立即推进按钮只属冲刺；best-effort 绝不抛。"""
        try:
            if not isinstance(view, dict):
                return view
            if str(view.get("status")) != "active":
                return view
            from src.companion.goals.pace import is_sprint
            from src.companion.goals.service import (
                resolve_goals_cfg,
                sprint_engine_status,
            )
            from src.companion.goals.sprint_ticker import (
                next_daily_ts,
                next_phase_ts,
                parse_sprint_cfg,
            )
            pace = str(view.get("pace") or "natural")
            sprint = is_sprint(pace)
            scfg = parse_sprint_cfg(resolve_goals_cfg(_cfg_root()))
            goal_like = {
                "start_ts": view.get("start_ts"),
                "deadline_ts": view.get("deadline_ts"),
            }
            beats = store.list_actions(str(view.get("goal_id") or ""),
                                       limit=120)
            # #166：ticker_on 必须是**整条链有效**，不是 sprint.enabled 一个开关。
            # D1b P0-3：自然档走同一字段（daily 拍），nudgeable 仍只属冲刺。
            es = sprint_engine_status(
                _cfg_root(), platform=str(view.get("platform") or ""))
            if sprint:
                ticker_on = bool(es.get("sprint_effective"))
                blockers = list(es.get("sprint_blockers") or [])
            else:
                ticker_on = bool(es.get("natural_auto_effective"))
                blockers = list(es.get("natural_auto_blockers") or [])
            # D1b P0-4：卡片只在**运行时闸也全过**时承诺「下一主动拍≈HH:MM」——
            # 引擎配置全绿但会话在 review 档 / 对方 opt-out / 危机窗内，ticker 照样
            # 一条都不排，此前卡上却一直挂着时间承诺。运行时闸与 ticker 同一函数。
            runtime: Dict[str, Any] = {}
            if ticker_on and str(view.get("autonomy") or "") == "auto":
                try:
                    from src.companion.goals.sprint_ticker import (
                        RUNTIME_GATE_ORDER,
                        goal_runtime_gates,
                    )
                    from src.companion.goals.sprint_ticker import (
                        load_optout_mutes,
                    )
                    mutes = load_optout_mutes(_config_path())
                    rg = goal_runtime_gates(
                        dict(view), cfg=scfg, inbox=_inbox_store(),
                        mutes=mutes, stop_on_fail=False)
                    runtime = dict(rg.get("gates") or {})
                    blockers += [k for k in RUNTIME_GATE_ORDER
                                 if k in runtime and not runtime[k]
                                 and k not in ("autonomy",)]
                except Exception:
                    logger.debug("sprint runtime gates skipped", exc_info=True)
            live_ok = ticker_on and not any(
                b in blockers for b in ("platform", "no_conversation",
                                        "no_inbox", "automation_mode",
                                        "crisis", "optout"))
            if live_ok:
                nxt = (next_phase_ts(goal_like, scfg) if sprint
                       else next_daily_ts(scfg))
            else:
                nxt = 0
            # M-7 A（#236）：卡片「已推进 N 拍」与看门狗 sent_24h 同一口径——都数
            # gstore 事件 beat_sent（主动真发），不再数 goal_actions 行（那里
            # consumed=回复链顺势带方向，用户在会话里看不到一条独立消息，却被
            # 写成「已推进 2 拍」）。trace 摘要（sent/injected/blocked/today）给
            # 卡片并排展示「主动 N · 顺势 M · 今天被拦 Z」，不用二次请求。
            from src.companion.goals.service import build_beats_trace
            import time as _t
            _now = _t.time()
            trace = build_beats_trace(store, dict(view), now=_now)
            tsum = trace.get("summary") or {}
            tbeats = trace.get("beats") or []
            sent_24h = sum(
                1 for b in tbeats
                if b.get("kind") == "sent" and float(b.get("ts") or 0) >= _now - 86400)
            last_block = None
            for b in reversed(tbeats):
                if b.get("kind") == "blocked":
                    last_block = {"reason": str(b.get("reason") or ""),
                                  "ts": float(b.get("ts") or 0)}
                    break
            # M-7 D（#236）：卡片「自动推进中」只在 **近 24h 真发 ≥1** 或 **24h 内有排期
            # 且运行时闸全过** 时成立；否则三选一说真相：今日已达上限（明日 HH:MM）/
            # 等待首拍（预计 HH:MM）/ 被 X 拦住（原因）。单目标 stalled（建了 ≥24h、
            # 有排期、零真发）红字点名。auto_state 与 blockers 同源，前端不再自己猜。
            from src.companion.goals.liveness import goal_stall_verdict
            is_auto = str(view.get("autonomy") or "") == "auto"
            stalled = bool(is_auto and ticker_on and live_ok and goal_stall_verdict(
                dict(view), sent_in_window=sent_24h, now=_now) == "stalled")
            nxt_f = float(nxt or 0)
            if not is_auto:
                auto_state = "manual"
            elif not ticker_on:
                auto_state = "off"
            elif not live_ok:
                auto_state = "blocked"
            elif sent_24h >= 1:
                auto_state = "active"
            elif (last_block and last_block["reason"] == "pace_cap"
                    and last_block["ts"] >= _now - 86400):
                auto_state = "cap_reached"
            elif stalled:
                auto_state = "stalled"
            elif nxt_f > _now and nxt_f <= _now + 86400:
                auto_state = "waiting_first"
            elif last_block and last_block["ts"] >= _now - 86400:
                auto_state = "blocked_recent"
            else:
                auto_state = "waiting_first"
            view["sprint_live"] = {
                "ticker_on": ticker_on,
                "ticker_enabled": bool(scfg.get("enabled")),
                "blockers": blockers,
                "runtime": runtime,
                "beats_used": int(tsum.get("sent") or 0),
                "beats_actions": len(beats),
                "trace": tsum,
                "sent_24h": int(sent_24h),
                "last_block": last_block,
                "stalled": stalled,
                "auto_state": auto_state,
                "next_phase_ts": round(nxt_f, 1) if live_ok else 0,
                # 立即推进按钮只属冲刺：auto 档 + 引擎开 + 运行时闸全过
                "nudgeable": live_ok
                and sprint
                and str(view.get("autonomy") or "") == "auto",
            }
        except Exception:
            logger.debug("sprint live attach skipped", exc_info=True)
        return view

    def _attach_beats(view, store):
        """进行中目标 → 「动作 N/M 拍」进度口径（#166）：卡上那个 89% 是**时间进度**
        （自定义/自然档目标的 progress 只跟日历爬），坐席把它读成「推进了 89%」——
        并排给出真正出过手的拍数：N＝consumed/sent 且力度非 none 的拍（与
        refresh_goal 的 engaged_beats 同口径）；M＝限时档生效封顶 / 自然档＝总天数
        （一天一拍）。非活跃零开销；best-effort 绝不抛。"""
        try:
            if not isinstance(view, dict) or str(view.get("status")) != "active":
                return view
            from src.companion.goals.pace import (
                effective_beat_cap,
                is_sprint,
                resolve_pace,
            )
            from src.companion.goals.service import resolve_goals_cfg
            from src.companion.goals.sprint_ticker import parse_sprint_cfg
            acts = store.list_actions(str(view.get("goal_id") or ""), limit=120)
            used = sum(
                1 for a in acts
                if str(a.get("status")) in ("consumed", "sent")
                and str(a.get("push_level") or "") != "none")
            pace = resolve_pace({"template": view.get("template"),
                                 "params": view.get("params") or {},
                                 "start_ts": view.get("start_ts"),
                                 "deadline_ts": view.get("deadline_ts")})
            tid = str(view.get("template") or "")
            if is_sprint(pace):
                scfg = parse_sprint_cfg(resolve_goals_cfg(_cfg_root()))
                cap = effective_beat_cap(
                    pace, overrides=scfg,
                    mode=str((view.get("params") or {}).get("sprint_mode") or ""))
                kind = ("milestone" if tid in _SIGNAL_PROGRESS_TEMPLATES
                        else "beats")
            else:
                cap = int(view.get("total_days") or 0)
                kind = ("milestone" if tid in _SIGNAL_PROGRESS_TEMPLATES
                        else "time")
            tot = float(view.get("total_sec") or 0.0)
            rem = float(view.get("remaining_sec") or 0.0)
            time_pct = int(round(max(0.0, min(1.0, (tot - rem) / tot)) * 100)) \
                if tot > 0 else 0
            view["beats"] = {
                "used": int(used), "cap": int(cap),
                # progress 口径：time＝只跟日历爬（ledger custom/未知模板分支，
                # 「89%」是时间不是推进）；beats＝限时档按已出手拍爬；
                # milestone＝模板里程碑信号驱动
                "progress_kind": kind,
                "time_pct": time_pct,
            }
        except Exception:
            logger.debug("beats attach skipped", exc_info=True)
        return view

    def _attach_sprint_recap(view, store):
        """终态限时目标 → 复盘字段：用了几拍 + 最后一拍意图（终局卡
        「停在哪」一眼可见）。natural / 非终态零开销直通。"""
        try:
            if not isinstance(view, dict):
                return view
            if str(view.get("pace") or "natural") == "natural":
                return view
            if str(view.get("status")) not in (
                    "done", "failed", "expired", "cancelled"):
                return view
            acts = store.list_actions(
                str(view.get("goal_id") or ""), limit=120)
            view["beats_used"] = len(acts)
            if acts:
                # list_actions 按 day DESC：session 槽 s:{ts} 定长数字、
                # today 槽 YYYY-MM-DDTHH——字典序即时间序，首行=最后一拍
                view["last_beat_intent"] = str(
                    acts[0].get("intent") or "")[:80]
        except Exception:
            logger.debug("sprint recap attach skipped", exc_info=True)
        return view

    def _attach_settlement(view, store, *, now=None):
        """终态目标 → ``settlement``（M-7 C #236：到期那一刻落库的结算摘要）+
        ``settled_recent``（done_at 在 7 天内：卡片显「已到期 · 查看结算」）。
        非终态 / 无摘要零开销直通；best-effort 绝不抛。"""
        try:
            if not isinstance(view, dict):
                return view
            if str(view.get("status")) not in ("done", "failed", "expired"):
                return view
            import time as _t
            from src.companion.goals.notify import (
                SETTLEMENT_VISIBLE_SEC,
                read_settlement,
            )
            n = float(now if now is not None else _t.time())
            done_at = float(view.get("done_at") or 0)
            view["settled_recent"] = bool(
                done_at > 0 and (n - done_at) <= SETTLEMENT_VISIBLE_SEC)
            s = read_settlement(store, str(view.get("goal_id") or ""))
            if s is not None:
                view["settlement"] = s
        except Exception:
            logger.debug("settlement attach skipped", exc_info=True)
        return view

    def _recent_terminal_for_conv(store, conv: str, *, exclude_gid: str = ""):
        """同会话最近一条终态目标（可排除当前目标）；无 → None。"""
        try:
            for g in store.list_goals(limit=30) or []:
                if str(g.get("conversation_id") or "") != conv:
                    continue
                if exclude_gid and str(g.get("goal_id") or "") == exclude_gid:
                    continue
                if str(g.get("status") or "") in ("done", "failed", "expired"):
                    return g
        except Exception:
            return None
        return None

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
            out = {
                "goal": None,
                "last": _attach_settlement(_attach_sprint_recap(_attach_notified(
                    svc.goal_view(last, lang=_lang(request)), store), store), store)
                if last else None,
            }
            # P1（2026-08-29）买家信号：无目标的会话近窗在问价/要购买方式
            # → 卡上提示「趁热开今天收口」。只提示不动作，拍板权在坐席。
            try:
                from src.companion.goals.buying_signal import (
                    scan_recent_inbound,
                )
                from src.integrations.protocol_bridge import get_inbox_store
                inbox = get_inbox_store()
                if inbox is not None:
                    hint = scan_recent_inbound(
                        inbox.list_recent_messages(conv, limit=12))
                    if hint:
                        out["signal_hint"] = hint
            except Exception:
                logger.debug("buying signal hint skipped", exc_info=True)
            return out
        lang = _lang(request)
        view = _attach_notified(
            _attach_slots_progress(
                _attach_products(
                    _refreshed_view(svc, store, goal, lang=lang),
                    store, lang=lang),
                store, lang=lang),
            store)
        # settle-on-read 可能本轮刚转终态（限时目标到期）——复盘字段同样要附
        view = _attach_tier(_attach_beats(_attach_sprint_live(
            _attach_sprint_recap(view, store), store), store), request)
        # M-7 C（#236）：新目标刚建、上一个同会话目标 7 天内刚到期 → 带上它的结算
        # （卡片一行「上一个目标 X 拍 / 结果，已结算」——用户 02:2x 重建后卡片回
        # 「起步」，以为引擎重置了）
        prev_settled = None
        try:
            prev = _recent_terminal_for_conv(
                store, conv, exclude_gid=str(view.get("goal_id") or ""))
            if prev is not None:
                pv = _attach_settlement(
                    svc.goal_view(prev, lang=lang), store)
                if pv.get("settled_recent"):
                    prev_settled = {
                        "goal_id": str(prev.get("goal_id") or ""),
                        "title": str(prev.get("title") or pv.get("template_name") or ""),
                        "status": str(prev.get("status") or ""),
                        "done_at": float(prev.get("done_at") or 0),
                        "settlement": pv.get("settlement"),
                    }
        except Exception:
            logger.debug("prev_settled attach skipped", exc_info=True)
        # #166：卡片带引擎真相（按本会话平台判白名单）——「自动推进」档在引擎
        # 不能真出手时卡上要说清，不能只在建目标表单里说一次
        return {"goal": view, "last": None, "prev_settled": prev_settled,
                # N-3 #241：卡片按业务域渲染画像分组 / 「标成交」字段（陪伴 = 「标记达成」
                # 达成结果 + 备注，不出现产品 / 金额）
                "business_domain": _business_domain(),
                "engine": svc.sprint_engine_status(
                    _cfg_root(), platform=str(view.get("platform") or platform))}

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

    @app.get("/api/goals/report/accounts")
    async def goals_report_accounts(
        request: Request, days: int = 30, _auth=Depends(auth_dep)
    ):
        """账号视角矩阵（P0 2026-08-09）：每（平台×账号）的 进行中/完成/未达成/
        完成率/赢单数+金额/平均达成天数/主力模板 + 顶层合计——「哪个号在出成绩」。
        只读；viewer 照常可读。"""
        svc = _require_enabled(request)
        store = _store(svc)
        from src.companion.goals.report import matrix_report
        d = max(1, min(int(days or 30), 365))
        out = matrix_report(store, days=d, lang=_lang(request))
        out["ok"] = True
        # N-3 #241（VAQGZY ④）：报表页按域收起金额口径（陪伴域没有「赢单金额」）
        out["business_domain"] = _business_domain()
        return out

    @app.get("/api/goals/report/contacts")
    async def goals_report_contacts(
        request: Request,
        days: int = 30,
        status: str = "done",
        platform: str = "",
        account_id: str = "",
        template: str = "",
        page: int = 1,
        page_size: int = 25,
        _auth=Depends(auth_dep),
    ):
        """账号×客户明细（P0 2026-08-09）：完成客户清单＝销售线索列表——
        每行带客户展示名、完成方式（订单/人工/自动结算）、金额、用时、
        **完成后是否已跟进**（done 后有出站消息＝已跟进，客观推导零打点）、
        推荐后续链。分页 + 状态/平台/账号/模板筛选。只读；viewer 照常可读。"""
        svc = _require_enabled(request)
        store = _store(svc)
        from src.companion.goals.report import contacts_report
        st = str(status or "").strip().lower()
        if st and st not in ("done", "active", "failed", "expired",
                             "cancelled", "ended", "all"):
            raise HTTPException(400, tr(request, "err.goals.bad_state"))
        out = contacts_report(
            store, _inbox_store(),
            days=days, status="" if st == "all" else st,
            platform=platform, account_id=account_id, template=template,
            page=page, page_size=page_size, lang=_lang(request))
        out["ok"] = True
        return out

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
        # P3 2026-08-09：常备扫描循环心跳（bootstrap 挂 app.state）——「没跑」
        # 和「没货」必须从外面分得出来（P0 扫描器挂死调度器上静默从未运行的
        # 教训）。空 dict=循环没挂载（旧进程/测试 app），如实外露。
        snap["scan_loop"] = dict(
            getattr(request.app.state, "goal_scan_state", None) or {})
        # P4 2026-08-30：冲刺推进器心跳（同一教训同一疗法）——running/enabled/
        # 上轮排入数/各跳过原因。空 dict=推进器没挂载。
        try:
            _tk = getattr(request.app.state, "goal_sprint_ticker", None)
            snap["sprint_loop"] = dict(_tk.snapshot()) if _tk else {}
        except Exception:
            snap["sprint_loop"] = {}
        return snap

    @app.get("/api/goals/notify-status")
    async def goals_notify_status(request: Request, _auth=Depends(auth_dep)):
        """完成通知链路状态（P0 2026-08-18）：目标卡「达成后会不会有人收到推送」
        的可见化数据源——此前 notify 扫描器（YAML overlay）与渠道订阅
        （notify_webhooks.json）都是黑盒，链路半接通（扫描在跑、渠道没订
        goal_complete）对运营完全隐形，本机实测就这么静默了 9 天。

        口径＝**文件真相**（``effective_webhooks``，mtime 失效缓存，与告警渠道
        面板同源）；进程侧是否已热更由 alert-link-status 的分歧检测负责，这里
        不重复造。零密钥响应（布尔/计数/通道名）；任意登录角色可读——坐席看到
        「达成会通知管理员」本身就是信息。goals 总闸关时随全家 403（前端隐藏）。"""
        _require_enabled(request)
        cfg = _cfg_root()
        from src.companion.goals.notify import resolve_notify_cfg
        ncfg = resolve_notify_cfg(cfg)
        out: Dict[str, Any] = {
            "ok": True,
            # 扫描器开关（发现完成→发事件；铃铛/toast 随事件天然到位）
            "notify_enabled": bool(ncfg.get("enabled")),
            "miss_digest": bool(ncfg.get("miss_digest")),
            # 外发推送：goal_complete 别名是否有启用通道在订阅
            "push_covered": False,
            "push_channels": [],
            # P2：坐席定向副本开关 + 当前登录人是否已绑定通知号（面板据此把
            # 「推送给管理员」升格成「管理员 + 你」；未绑定不出错误只如实不升格）
            "push_agent": bool(ncfg.get("push_agent")),
            "self_bound": False,
        }
        try:
            from src.integrations.alert_link_audit import audit_alert_link
            from src.integrations.notify_webhooks_store import effective_webhooks
            audit = audit_alert_link(effective_webhooks(cfg), ["goal_complete"])
            chans = (audit.get("per_alias") or {}).get("goal_complete") or []
            out["push_channels"] = [str(c) for c in chans][:5]
            out["push_covered"] = bool(chans)
        except Exception:
            logger.debug("goal notify-status audit failed", exc_info=True)
        if out["push_agent"]:
            try:
                from src.companion.goals.notify import user_store_for
                me = str(request.session.get("username")
                         or request.session.get("user", "") or "").strip()
                us = user_store_for(_config_path())
                if me and us is not None:
                    u = us.get_user(me)
                    out["self_bound"] = bool(
                        isinstance(u, dict)
                        and str(u.get("notify_tg_chat_id") or "").strip())
            except Exception:
                logger.debug("notify-status self_bound skipped", exc_info=True)
        # P3：摸底要点出境开关回显（接通面板据此渲染当前档位）
        out["include_profile"] = bool(ncfg.get("include_profile"))
        # J-4 G（2026-09-05）：「达成推送未接通 / 扫描未开」是只有内部运维能处置的
        # 配置 nag——桌面包（client 形态）用户与非管理角色都无处下手，卡上常驻黄字
        # 只制造焦虑。服务端给出抑制旗标（形态判定单源 ui_visibility.is_client_flavor
        # + 角色与 notify-config 写权限同集），cp-goal 据此不渲染 warn 行；push_covered
        # 的绿字仍照常（那是正面信息）。放在 API 里而非模板属性：cp-goal 也跑在桌面壳
        # 的静态 copilot/app.html（无 Jinja），只有接口能把形态真相送到那儿。
        try:
            from src.web.ui_visibility import is_client_flavor
            _client = bool(is_client_flavor(cfg))
        except Exception:
            _client = False
        try:
            _role = str(request.session.get("role", "") or "")
        except Exception:
            _role = ""
        out["nag_suppressed"] = bool(_client or _role in _NOTIFY_CFG_DENY_ROLES)
        return out

    # 完成推送设置的可写白名单（P3 2026-08-18）：路径硬编码防任意键注入——
    # 与 care 引擎 /api/care/engine 同范式（set_overlay_flag 保注释写 overlay，
    # 热重载 ~30s 生效免重启）。扫描器开关此前只能改 YAML，是「设置面碎在
    # 四处」的最后一块无 UI 死角。
    _NOTIFY_FLAG_PATHS = {
        "enabled": "companion.goals.notify.enabled",
        "push_agent": "companion.goals.notify.push_agent",
        "miss_digest": "companion.goals.notify.miss_digest",
        "include_profile": "companion.goals.notify.include_profile",
    }
    # 与告警渠道面板同一排除法（拒 agent/viewer）：完成推送改的是全实例
    # 通知行为，属主管动作；agent 本被 _agent_guard 前置拦（纵深冗余）。
    _NOTIFY_CFG_DENY_ROLES = ("agent", "viewer")

    @app.post("/api/goals/notify-settings")
    async def goals_notify_settings(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        """完成推送开关面板写口：body 里给到哪个键就写哪个（布尔），其余不动。

        goals 总闸关时随全家 403（与 notify-status 的 _require_enabled 同口径
        ——总闸都没开，改推送开关是空转）；写失败逐键回报不整体装死。"""
        _require_enabled(request)
        try:
            role = str(request.session.get("role", "") or "")
        except Exception:
            role = ""
        if role in _NOTIFY_CFG_DENY_ROLES:
            raise HTTPException(
                403, tr(request, "err.perm.supervisor_required"))
        if config_manager is None or not hasattr(
                config_manager, "set_overlay_flag"):
            return {"ok": False, "reason": "config_unavailable"}
        body = payload if isinstance(payload, dict) else {}
        applied, failed = [], []
        for key, path in _NOTIFY_FLAG_PATHS.items():
            if key not in body:
                continue
            ok, msg = config_manager.set_overlay_flag(path, bool(body[key]))
            if ok:
                applied.append(key)
            else:
                failed.append({"key": key, "reason": str(msg)})
        if not applied and not failed:
            return {"ok": False, "reason": "nothing_to_update"}
        if applied:
            try:
                actor = str(request.session.get("username")
                            or request.session.get("user", "") or "")[:60]
            except Exception:
                actor = ""
            logger.info("[goal-notify] 推送设置变更 by=%s keys=%s",
                        actor or "?", ",".join(applied))
        from src.companion.goals.notify import resolve_notify_cfg as _rncfg
        eff = _rncfg(_cfg_root())
        return {
            "ok": not failed,
            "applied": applied,
            "failed": failed,
            "effective": {
                "notify_enabled": bool(eff.get("enabled")),
                "push_agent": bool(eff.get("push_agent")),
                "miss_digest": bool(eff.get("miss_digest")),
                "include_profile": bool(eff.get("include_profile")),
            },
            "hot_reload_sec": 30,
        }

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
        _deny_hidden_template(request, template_id)
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
        # 缺省档＝auto（2026-08-12 运营方针：建目标以全自动推进为主；
        # observe 是每目标的显式选择，不作缺省）。UI 总是显式带 autonomy，
        # 这里兜的是裸 API/脚本调用。
        autonomy = str(body.get("autonomy") or "auto")
        try:
            priority = int(body.get("priority") or 1)
        except (TypeError, ValueError):
            priority = 1
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        params = dict(params)
        params["pace"] = "natural"
        title = str(body.get("title") or "")[:120]
        try:
            # 生产 session 键是 username（登录只写它）；"user" 是历史误键——
            # 修前人建目标 created_by 恒空串（P3 2026-08-18 实锤），定向推送的
            # 「创建人」解析链整条落空。保留 "user" 回落兼容测试桩。
            actor = str(request.session.get("username")
                        or request.session.get("user", "") or "")[:52]
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
        """画像视图（GET 与 POST 共用同一形状，前端保存后免二次拉取）。

        N-3 #241 / TN736F：画像字段集与摸底 chips **同一份域 schema**
        （``profile_slots.slots_for_domain``，一处定义两处渲染）。陪伴域 = 关系 +
        个人情况（+ 自定义），不渲染商机区；**存量不丢**——不在当前域表里但已有值的槽
        （升级前填的「预算档」等）以 ``extra=True`` 附在末尾，前端归「其他已记录」。
        """
        from src.companion.goals.profile_slots import (
            ALL_SLOTS,
            fill_rates,
            missing_slots,
            secondary_track,
            slots_for_domain,
            tracks_for,
        )
        bd = _business_domain()
        store = _store(svc)
        prof = store.get_customer_profile(pf, ck)
        fields = dict((prof or {}).get("fields") or {})
        lab = "label_en" if lang.startswith("en") else "label_zh"

        def _row(s, extra=False):
            key = s["key"]
            cell = fields.get(key) if isinstance(fields.get(key), dict) else {}
            row = {
                "key": key,
                "track": s["track"],
                "label": str(s.get(lab) or key),
                "value": str((cell or {}).get("v") or ""),
                "src": str((cell or {}).get("src") or ""),
                "ts": float((cell or {}).get("ts") or 0),
            }
            if s.get("sensitive"):
                row["sensitive"] = True
            if s.get("custom"):
                row["custom"] = True
            if extra:
                row["extra"] = True
            return row

        domain_slots = slots_for_domain(bd)
        seen = {s["key"] for s in domain_slots}
        slots = [_row(s) for s in domain_slots]
        for s in ALL_SLOTS:
            if s["key"] in seen:
                continue
            cell = fields.get(s["key"])
            if isinstance(cell, dict) and str(cell.get("v") or "").strip():
                slots.append(_row(s, extra=True))
        sec = secondary_track(bd)
        # 画像卡缺口 chips（坐席「拟稿去问」入口）：敏感槽不进——那等于直接问；
        # 目标勾选了敏感槽走注入链的「多轮自然带出」路径，不经这里
        missing = [m["key"] for m in
                   missing_slots(fields, track=sec, limit=8, business_domain=bd)
                   if not m.get("sensitive")][:6]
        return {
            "platform": pf, "chat_key": ck,
            "business_domain": bd,
            "tracks": list(tracks_for(bd)),
            "secondary_track": sec,
            "slots": slots,
            "fill": fill_rates(fields, business_domain=bd),
            "missing": missing,
            # 旧前端兼容键：销售域 = 商机缺口；陪伴域没有商机轨 → 空
            "missing_bant": missing if sec == "bant" else [],
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

    # ── 自定义摸底标签（N-3 #241 「+ 自定义标签」；TN736F ③ 同步出现在画像卡）──
    _CUSTOM_SLOTS_PATH = "companion.goals.custom_slots"

    @app.get("/api/goals/custom-slots")
    async def goals_custom_slots_get(request: Request, _auth=Depends(auth_dep)):
        _require_enabled(request)
        from src.companion.goals.profile_slots import custom_slots
        return {"ok": True, "custom_slots": [dict(s) for s in custom_slots()],
                "discovery_slots": _discovery_slots()}

    @app.post("/api/goals/custom-slots")
    async def goals_custom_slots_set(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        """加 / 删一个自定义标签。``{"label": "家乡"}`` 加；``{"remove": "x_…"}`` 删。
        持久化到 overlay ``companion.goals.custom_slots``（标签列表，配置是唯一事实源），
        同时刷进程内注册表——摸底 chips / 画像卡 / 注入链下一次读取即见。"""
        _require_enabled(request)
        _deny_viewer(request)
        from src.companion.goals.profile_slots import (
            custom_slot_key,
            custom_slot_labels,
            custom_slots,
            get_slot,
            is_custom_slot_key,
            register_custom_slots,
        )
        body = payload or {}
        label = str(body.get("label") or "").strip()[:24]
        remove = str(body.get("remove") or "").strip().lower()
        labels = custom_slot_labels()
        if label:
            if get_slot(custom_slot_key(label)) is not None or any(
                    l.casefold() == label.casefold() for l in labels):
                pass  # 已有：幂等
            elif len(labels) >= 12:
                raise HTTPException(400, tr(request, "err.goals.custom_slot_limit", n=12))
            else:
                labels.append(label)
        elif remove and is_custom_slot_key(remove):
            labels = [l for l in labels if custom_slot_key(l) != remove]
        else:
            raise HTTPException(400, tr(request, "err.goals.custom_slot_label_required"))
        setter = getattr(config_manager, "set_overlay_flag", None)
        if callable(setter):
            ok, msg = setter(_CUSTOM_SLOTS_PATH, list(labels))
            if not ok:
                logger.warning("custom_slots overlay write failed: %s", msg)
                raise HTTPException(500, tr(request, "err.goals.update_failed"))
        register_custom_slots(list(labels))
        actor = ""
        try:
            actor = request.session.get("username", "")
        except Exception:
            pass
        logger.info("goals custom_slots = %s (by %s)", labels, actor or "?")
        return {"ok": True, "custom_slots": [dict(s) for s in custom_slots()],
                "discovery_slots": _discovery_slots(),
                "added_key": custom_slot_key(label) if label else ""}

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
                views.append(_attach_tier(
                    _refreshed_view(svc, store, g, lang=lang), request))
            else:
                views.append(_attach_tier(svc.goal_view(g, lang=lang), request))
        return {"goals": views, "summary": store.summary()}

    @app.post("/api/goals")
    async def goals_create(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        svc = _require_enabled(request)
        _deny_viewer(request)
        from src.companion.goals.templates import get_template
        from src.companion.goals.pace import (
            clamp_deadline_days,
            is_sprint,
            normalize_pace,
            pace_allowed,
        )
        body = payload or {}
        template_id = str(body.get("template") or "").strip()
        tmpl = get_template(template_id)
        if tmpl is None:
            raise HTTPException(400, tr(request, "err.goals.template_unknown"))
        _deny_hidden_template(request, template_id)

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
        params_in = body.get("params") if isinstance(
            body.get("params"), dict) else {}
        params_in = dict(params_in)
        _explicit_pace = body.get("pace") or params_in.get("pace")
        pace = normalize_pace(_explicit_pace)
        if not pace_allowed(template_id, pace):
            raise HTTPException(400, tr(request, "err.goals.pace_not_allowed"))
        if not str(_explicit_pace or "").strip():
            # #65 C1（2026-09-04）：客户端没给 pace → 按期限跨度自动落档
            # （≤2.5h session / <20h today）。此前一律 natural，下一行
            # clamp 把 60 分钟目标夹成 1 天——子日跨度进不了库，节奏档形同虚设。
            from src.companion.goals.pace import default_pace_for_deadline
            pace = default_pace_for_deadline(template_id, deadline_days)
        deadline_days = clamp_deadline_days(
            pace, deadline_days,
            default_days=float(tmpl.get("default_days") or 14))
        params_in["pace"] = pace
        autonomy = str(body.get("autonomy") or "auto")
        if is_sprint(pace) and autonomy == "observe":
            autonomy = "suggest"

        try:
            # username 为生产真键（见批量口同款注释）；"user" 回落兼容测试桩
            actor = str(request.session.get("username")
                        or request.session.get("user", "") or "")[:60]
        except Exception:
            actor = ""
        # notify_extra 入库前消毒（P3：逐目标「达成后通知谁」，写读同一清洗）
        if "notify_extra" in params_in:
            from src.companion.goals.notify import sanitize_notify_extra
            cleaned = sanitize_notify_extra(params_in.get("notify_extra"))
            if cleaned:
                params_in["notify_extra"] = cleaned
            else:
                params_in.pop("notify_extra", None)
        goal = store.create_goal(
            conversation_id=conv, platform=platform, account_id=account_id,
            chat_key=chat_key, template=template_id,
            title=str(body.get("title") or "")[:120],
            params=params_in,
            # 缺省档＝auto（与批量建目标同口径，2026-08-12 全自动为主）
            autonomy=autonomy,
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
        # P3 2026-08-13 建目标即挂推荐链（inbox.workflows.goal_auto_attach 默认关；
        # 五重闸在 helper 内：auto 档 × 模板有推荐 × 链在且启用 × 无在途）。
        # best-effort：挂链失败绝不影响建目标本身；成功 → 目标时间线 chain_started
        # 回写（与手动 start-chain 路由同口径）+ 响应带 auto_attached_chain
        # （cp-goal 据此把推荐行直接渲染成「已挂上 ✓」，不再出按钮）。
        attached = None
        try:
            from src.inbox.workflow_starter import maybe_auto_attach_chain
            attached = maybe_auto_attach_chain(
                getattr(request.app.state, "inbox_store", None),
                _cfg_root(),
                conversation_id=str(goal.get("conversation_id") or ""),
                goal_id=str(goal.get("goal_id") or ""),
                goal_template=str(goal.get("template") or ""),
                goal_autonomy=str(goal.get("autonomy") or ""),
            )
            if attached:
                svc.record_chain_event(
                    _cfg_root(), _config_path(),
                    str(goal.get("conversation_id") or ""),
                    "chain_started", str(attached.get("name") or ""))
        except Exception:
            logger.debug("goal auto-attach skipped", exc_info=True)
        out = {"ok": True,
               "goal": _attach_tier(
                   _refreshed_view(svc, store, goal, lang=_lang(request)),
                   request)}
        if attached:
            out["auto_attached_chain"] = attached
        return out

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
        view = _attach_settlement(_attach_tier(
            (_refreshed_view(svc, store, goal, lang=lang)
             if str(goal.get("status")) == "active"
             else svc.goal_view(goal, lang=lang)), request), store)
        actions = store.list_actions(goal_id, limit=30)
        # i18n P0：拍史逐行附英文展示态（同池反查；匹配不到=""=前端回落中文）。
        # 「What AI did」时间线读的就是这批行。
        try:
            from src.companion.goals.templates import (
                get_template as _gt, intent_en_for as _ien,
            )
            _tpl = _gt(str(goal.get("template") or "")) or {}
            _prm = goal.get("params") or {}
            for _a in actions:
                if isinstance(_a, dict):
                    _a["intent_en"] = _ien(_tpl, _prm, str(_a.get("intent") or ""))
        except Exception:
            logger.debug("actions intent_en enrich skipped", exc_info=True)
        return {
            "goal": view,
            "actions": actions,
            "events": store.list_events(goal_id, limit=50),
        }

    @app.get("/api/goals/{goal_id}/beats")
    async def goals_beats(
        request: Request, goal_id: str, _auth=Depends(auth_dep)
    ):
        """M-7 A（#236 = #166 族第五次）：目标「每一拍」清单——第 N 拍 / 时刻 /
        会话 / 说了什么 / 相位 / 状态（已投递 · 排队中 · 失败 · 被拦 + 原因 ·
        首拍待预览）+ 可跳转的平台消息行 id。数据源只有目标事件表（与看门狗
        ``goal_sprint_liveness`` 同一表同一 kind），卡片「已推进 N 拍」点开看的就是
        这份。只读；viewer 照常可读。"""
        svc = _require_enabled(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        try:
            care_store = getattr(app.state, "care_schedule_store", None)
        except Exception:
            care_store = None
        try:
            outbox = getattr(app.state, "deferred_outbox_store", None)
        except Exception:
            outbox = None
        out = svc.build_beats_trace(
            store, goal, care_store=care_store, outbox_store=outbox,
            inbox_store=_inbox_store())
        out["title"] = str(goal.get("title") or "")
        out["status"] = str(goal.get("status") or "")
        out["conversation_id"] = str(goal.get("conversation_id") or "")
        return out

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
            # notify_extra 消毒与建目标同口径；显式传空=清除点名收件人
            if "notify_extra" in body["params"]:
                from src.companion.goals.notify import sanitize_notify_extra
                cleaned = sanitize_notify_extra(
                    body["params"].get("notify_extra"))
                if cleaned:
                    merged["notify_extra"] = cleaned
                else:
                    merged.pop("notify_extra", None)
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
                for a in store.list_actions(goal_id, limit=120):
                    d = str((a or {}).get("day") or "")
                    if d:
                        store.delete_planned_action(goal_id, d)
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
        action = _find_beat_action(store, goal, body)
        if action is None:
            raise HTTPException(409, tr(request, "err.goals.no_beat_today"))
        reason = str(body.get("reason") or "agent").strip()[:120]
        aid = str(action.get("action_id") or "")
        slot = str(action.get("day") or "")
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
        if slot:
            action = store.get_action(goal_id, slot) or action
        return {"ok": True,
                "goal": svc.goal_view(goal, action, lang=_lang(request))}

    @app.post("/api/goals/{goal_id}/sprint/nudge")
    async def goals_sprint_nudge(
        request: Request, goal_id: str, _auth=Depends(auth_dep),
    ):
        """坐席「立即推进」（P3 2026-08-30）：冲刺目标当场排一条主动拍。

        人拍板绕过**节奏闸**（沉默阈/出站间隔），**安全闸原样过**：
        危机 block / opt-out 静默 / 会话档位（仅 auto_ai 自发）——按钮是
        入口不是授权。全相位已排过 → 409 exhausted（引擎已在路上，连点
        不会双发：相位 topic_norm 去重是同一道闸）。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        if str(goal.get("status")) != "active":
            raise HTTPException(409, tr(request, "err.goals.not_active"))
        from src.companion.goals.pace import is_sprint, resolve_pace
        from src.companion.goals.sprint_ticker import (
            load_optout_mutes,
            parse_sprint_cfg,
            schedule_nudge,
        )
        if not is_sprint(resolve_pace(goal)):
            raise HTTPException(400, tr(request, "err.goals.not_sprint"))
        scfg = parse_sprint_cfg(svc.resolve_goals_cfg(_cfg_root()))
        if not scfg.get("enabled"):
            raise HTTPException(
                409, tr(request, "err.goals.sprint_disabled"))
        if str(goal.get("autonomy") or "") != "auto":
            raise HTTPException(
                409, tr(request, "err.goals.nudge_not_auto"))
        conv = str(goal.get("conversation_id") or "")
        inbox = _inbox_store()
        # 会话档位：人审会话不自发（与推进器同闸；读不到=fail-closed）
        try:
            mode = str(inbox.get_automation_mode(conv) or "") \
                if inbox is not None else ""
        except Exception:
            mode = ""
        if mode != "auto_ai":
            raise HTTPException(
                409, tr(request, "err.goals.nudge_not_auto"))
        # 危机 block（meta 口径；skill_manager 级危机库判定由推进器/回复链兜）
        try:
            from src.utils.wellbeing_guard import proactive_emotion_gate
            import time as _t
            meta = (inbox.get_conv_meta(conv) or {}) if inbox else {}
            _i = meta.get("last_emotion_intensity")
            if proactive_emotion_gate(
                    None, now=_t.time(),
                    last_emotion=str(meta.get("last_emotion") or ""),
                    last_emotion_intensity=(
                        float(_i) if _i is not None else None)) == "block":
                raise HTTPException(
                    409, tr(request, "err.goals.nudge_blocked"))
        except HTTPException:
            raise
        except Exception:
            pass
        # opt-out 静默（客户说过别再发——人工加速也不越）
        try:
            mutes = load_optout_mutes(_config_path())
            if conv in mutes:
                from src.utils.proactive_optout import optout_active
                import time as _t2
                if optout_active(mutes.get(conv), now=_t2.time()):
                    raise HTTPException(
                        409, tr(request, "err.goals.nudge_muted"))
        except HTTPException:
            raise
        except Exception:
            pass
        from src.contacts.care_schedule import get_care_schedule_store
        res = schedule_nudge(get_care_schedule_store(), goal, cfg=scfg)
        if res is None:
            raise HTTPException(
                409, tr(request, "err.goals.nudge_exhausted"))
        phase, rid = res
        store.add_event(goal_id, "sprint_nudge", f"p{phase} care#{rid}")
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_sprint_nudge()
        except Exception:
            pass
        logger.info("[goal-sprint] 坐席手动加速 goal=%s p%s care#%s",
                    goal_id, phase, rid)
        return {"ok": True, "phase": int(phase), "care_id": int(rid)}

    @app.get("/api/goals/{goal_id}/preflight")
    async def goals_preflight(
        request: Request, goal_id: str, _auth=Depends(auth_dep),
    ):
        """「这条目标现在能不能真的自动出手」一次说全（D1b P0-4 2026-09-05）。

        三层合一（引擎配置 / 进程活性 / 目标运行时闸）→ ``{ok, blockers[],
        engine, process, runtime, due_now, next_due, pending_rows}``。
        运行时闸与 ticker ``run_once`` 同一函数（``goal_runtime_gates``），
        preflight 说「能」ticker 就一定会排——此前卡片只看引擎配置，会话在
        review 档 / opt-out / 危机窗时照样挂着「下一主动拍≈HH:MM」。
        blockers 用配置键名口径（与 ``sprint_engine_status`` 同一词表），
        前端按 ``inbox.goal.engine.blk.*`` 直接翻译。只读，绝不抛 5xx。"""
        svc = _require_enabled(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        from src.companion.goals.sprint_ticker import (
            load_optout_mutes,
            preflight_goal,
        )
        tk = getattr(app.state, "goal_sprint_ticker", None)
        ticker_running = None
        if tk is not None:
            try:
                ticker_running = bool(tk.is_running())
            except Exception:
                ticker_running = None
        dispatcher_running = None
        try:
            eng = getattr(app.state, "care_engine", None) or {}
            disp = eng.get("dispatcher") if isinstance(eng, dict) else None
            if disp is not None:
                dispatcher_running = bool(disp.is_running())
        except Exception:
            dispatcher_running = None
        care_store = None
        try:
            care_store = getattr(app.state, "care_schedule_store", None)
        except Exception:
            care_store = None
        try:
            mutes = load_optout_mutes(_config_path())
        except Exception:
            mutes = {}
        out = preflight_goal(
            dict(goal), cfg_root=_cfg_root(),
            inbox=_inbox_store(), care_store=care_store, mutes=mutes,
            ticker_running=ticker_running,
            dispatcher_running=dispatcher_running,
        )
        # 「下一拍」给前端 HH:MM 友好串（本地时区）
        try:
            import time as _t3
            nd = float(out.get("next_due") or 0)
            out["next_due_hhmm"] = (
                _t3.strftime("%H:%M", _t3.localtime(nd)) if nd > 0 else "")
        except Exception:
            out["next_due_hhmm"] = ""
        return out

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
            # P2 2026-08-30 补确认：到期时检出过达成信号但没人点（冲刺窗短，
            # 绿条常等到过期）→ 终局卡「疑似达成→补确认」走这条迁移。与迟到
            # 订单复活（settle_order_ref 的 late 分支）同一哲学：达成是硬事实，
            # 到期只是跟踪窗先关了。cancelled 仍不可复活（人工叫停是明示决定）。
            ("expired", "done"): "done",
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
        # M-7 C（#236）：人工标终态也留一份结算摘要（卡片「查看结算」同源），
        # 但不推铃铛——人自己点的，不是引擎静默到期
        if new_status == "done" and goal is not None:
            try:
                from src.companion.goals.notify import settle_and_notify
                settle_and_notify(store, goal, cfg_root=_cfg_root(),
                                  inbox_store=_inbox_store(), notify=False)
            except Exception:
                logger.debug("manual settle summary skipped", exc_info=True)
        # P5 留存环：手动标成交（线下收款等）与订单回流同权——成交即续期
        # （manual=True：只有带 catalog 能力的模板才有「续费」语义）
        if new_status == "done" and goal is not None:
            svc.maybe_create_retention_goal(
                store, _cfg_root(), goal, manual=True,
                plan=str((goal.get("params") or {}).get("last_plan") or ""))
            # P7 回流再转化：坐席手动把挽回目标标 done（对方回来了）与
            # settle-on-read 同权（内部门控 created_by=winback_auto，普通目标零影响）
            svc.maybe_spawn_reconvert(store, _cfg_root(), goal)
        # 冲刺插队回程票（P2/P3 2026-08-30）：人工终局（成交/取消）同样恢复
        # 被暂停的长线目标（settle-on-read 路径在 refresh_goal 内已挂同钩）
        if new_status in ("done", "cancelled") and goal is not None:
            try:
                svc.maybe_resume_linked_goal(store, goal)
            except Exception:
                logger.debug("linked resume skipped", exc_info=True)
        return {"ok": True,
                "goal": svc.goal_view(goal, lang=_lang(request))}

    logger.info("工作目标路由已注册（/api/goals*）")


def register_goal_report_page(app, *, page_auth, templates,
                              config_manager=None):
    """目标达成报表页（P0 2026-08-09；与 agent_perf 同族：主管专属工作台页）。

    GET /workspace/goal-report —— 账号×客户完成情况 + 完成客户跟进动作面。
    数据走 ``/api/goals/report/accounts`` + ``/api/goals/report/contacts``；
    发消息复用 ``/api/unified-inbox/send``（幂等键 ``goalrpt-*``）。
    goals 未启用时页面照常可开（前端按 403 显示未启用引导，与 ops 卡同语义）。
    """
    from fastapi import Depends
    from fastapi.responses import HTMLResponse, RedirectResponse

    def _session_role(request: Request) -> str:
        try:
            return str(request.session.get("role", "") or "")
        except Exception:
            return ""

    def _ctx(request) -> dict:
        try:
            sess = request.session
        except (AttributeError, AssertionError):
            sess = {}
        ctx: dict = {
            "user_name": sess.get("username") or "",
            "user_display_name": (
                sess.get("display_name") or sess.get("username") or ""),
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa["site_name"]
        except Exception:
            pass
        return ctx

    @app.get("/workspace/goal-report", response_class=HTMLResponse)
    async def workspace_goal_report_page(
        request: Request, _=Depends(page_auth),
    ):
        """目标达成报表（主管专属；非主管重定向到工作台）。"""
        # 与 drafts_routes._SUPERVISOR_ROLES 同口径（master/admin=主管）
        if _session_role(request) not in ("master", "admin"):
            return RedirectResponse(url="/workspace", status_code=302)
        return templates.TemplateResponse(
            request, "goal_report.html", _ctx(request))

    logger.info("目标达成报表页已注册（/workspace/goal-report）")
