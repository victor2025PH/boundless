"""AIChatAssistant 启动期辅助任务(Stage4,从 main.py 整方法原样迁出,仅 self->assistant)。

主动关怀/复活循环/延迟发件箱/嵌入预热/商业化初始化/情节回填——均由 start() 调度,
side-effect 式装配到 assistant 上,失败各自 try/except 兜底,绝不挡主启动。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.licensing.data_paths import plugin_dir, runtime_dir, runtime_file


async def maybe_start_proactive_care(assistant, web_app=None) -> None:
    """Phase O：主动关怀引擎——常备接线 + 配置热闸（P0 2026-08-01 改造）。

    旧行为：``companion.proactive_care.enabled=false`` 时整段早退——运营在 overlay
    开了开关也要**再吃一次重启**才真生效（捕获回调没注册、派发循环没启动），
    「一键开启」名存实亡。新行为：

    - **捕获**：回调无条件注册（``make_care_inbound_cb`` 内部本就逐条消息读实时
      配置判断 enabled/capture，关闸时每条入站只多一次 dict 查找，零副作用）。
    - **派发**：循环无条件启动，经 ``cfg_provider`` 每 tick 读实时配置——关闸时
      空转（不碰 store 不烧 LLM）。开/关经 config.local.yaml 热重载 ~30s 生效，
      免重启。``interval_sec`` 仍为启动期绑定（改它需重启，看板有注明）。
    - 依赖缺失（ai_client 为 None）才真跳过派发循环，原因落
      ``web_app.state.care_engine.dispatcher_skip`` 供 /api/care/health 展示。
      messenger runner 缺失不再挡整个循环（telegram/line/whatsapp 走多平台
      deferred 队列，与 messenger 无关）——send 回调内部对 messenger 分支判空。

    「新子系统默认 enabled:false」约定不变：默认仍不捕获、不派发，变的只是
    **机制常备、开关热生效**。
    """
    try:
        cfg = ((assistant.config.config.get("companion") or {}).get("proactive_care") or {})
        from src.contacts.care_schedule import (
            default_care_db_path, get_care_schedule_store,
        )

        care_store = get_care_schedule_store(
            default_care_db_path(assistant.config.config_path))
        engine_state = {"capture_wired": False, "dispatcher_skip": "",
                        "messenger_rpa": assistant.messenger_rpa_service is not None}
        if web_app is not None:
            web_app.state.care_schedule_store = care_store
            web_app.state.care_engine = engine_state
        # 启动清理只在已开闸时做（关闸期保持对库零写入）
        if cfg.get("enabled", False):
            try:
                care_store.expire_overdue(grace_days=float(cfg.get("grace_days", 1)))
            except Exception:
                pass

        # 对方机器人/自家账号守卫（2026-08-18 从派发层提前到这里构造）：捕获与
        # 派发共用同一谓词——垃圾捕获事故（老板↔自有账号互聊的运维报告被抓成
        # 「检查」关怀）从源头收口，而不是等派发时才拦。
        _care_peer_filter = None
        try:
            from src.companion.proactive_peer_hygiene import build_peer_filter
            from src.integrations.account_registry import get_account_registry
            _care_peer_filter = build_peer_filter(
                assistant.inbox_store, assistant.config.config or {},
                registry=get_account_registry())
        except Exception:
            assistant.logger.debug("care peer_filter 构造失败（不拦）", exc_info=True)

        # 捕获接线：无条件注册（回调内部按实时配置逐条闸门）
        if assistant.inbox_store is not None:
            try:
                from src.contacts.care_capture import make_care_inbound_cb
                assistant.inbox_store.register_new_inbound_cb(
                    make_care_inbound_cb(care_store, assistant.config,
                                         peer_filter=_care_peer_filter))
                engine_state["capture_wired"] = True
                assistant.logger.info("✅ proactive_care 捕获已常备接线（配置热闸，enabled=%s）",
                                      bool(cfg.get("enabled", False)))
            except Exception:
                assistant.logger.warning("proactive_care 捕获接线跳过", exc_info=True)

        # 派发循环：仅 ai_client 缺失才跳过（messenger runner 缺失不挡非 messenger 平台）
        if assistant.ai_client is None:
            engine_state["dispatcher_skip"] = "ai_missing"
            assistant.logger.info("proactive_care 派发循环跳过（ai 未就绪），仅捕获")
            return
        from src.contacts.care_dispatcher import CareDispatcher

        async def _care_send(channel, account_id, chat_name, reply, defer_until,
                             reason, staleness_sec, extra):
            if channel != "messenger":
                # 非 messenger → 多平台 deferred 队列（关/不可用则返回 0，零破坏）
                return assistant._enqueue_deferred_outbox(
                    channel, account_id, chat_name, reply, defer_until,
                    reason, staleness_sec, extra)
            if assistant.messenger_rpa_service is None:
                return 0  # messenger runner 未起 → 留 pending 待自愈/过期，不误标已发
            return await assistant.messenger_rpa_service.enqueue_reactivation_deferred(
                account_id=account_id, chat_name=chat_name, reply_text=reply,
                defer_until=defer_until, defer_reason=reason,
                staleness_sec=staleness_sec, extra=extra)

        def _care_context(contact_key: str) -> str:
            # 最近若干条消息文本作 prompt 可引用要点（best-effort）。
            # P2 修：原用 list_messages（取**最旧** limit 条）——「最近对话要点」
            # 实际喂的是几个月前的开场白；改 list_recent_messages（最近 8 条，ts 升序）。
            try:
                msgs = assistant.inbox_store.list_recent_messages(contact_key, limit=8) \
                    if assistant.inbox_store else []
                lines = [str(m.get("text") or "").strip() for m in (msgs or [])]
                return "\n".join(t for t in lines if t)[:800]
            except Exception:
                return ""

        def _care_already_discussed(contact_key: str, topic: str) -> bool:
            """O3 改进① 正式接线（实施84 P0-2；构造参数一直在、此前从未注入）：
            近 N 小时（already_discussed_hours，默认 48，0=关）**出站**消息里
            已经提过该主题 → 跳过（防「刚聊完面试，晚上机器又来打卡问面试」）。
            只认出站：客户自己反复提不算「我们已关心过」。异常按未聊过（放行）。"""
            import time as _t
            try:
                hours = float(_live_care_cfg().get("already_discussed_hours", 48) or 0)
                t = str(topic or "").strip()
                if hours <= 0 or len(t) < 2 or assistant.inbox_store is None:
                    return False
                msgs = assistant.inbox_store.list_recent_messages(
                    contact_key, limit=30) or []
                cutoff = _t.time() - hours * 3600.0
                for m in msgs:
                    if str(m.get("direction") or "") != "out":
                        continue
                    if float(m.get("ts") or 0) < cutoff:
                        continue
                    if t in str(m.get("text") or ""):
                        return True
                return False
            except Exception:
                return False

        def _care_prompt_extras(item: dict) -> dict:
            """拟稿增强块（实施84 P0-2）：人设口吻 / episodic 记忆要点 / 工作目标
            背景。全部 best-effort——任一源失败该块为空，prompt 退回旧口径。"""
            out = {"persona_line": "", "memory_block": "", "goal_block": ""}
            cfg_live = _live_care_cfg()
            platform = str(item.get("platform") or "")
            account_id = str(item.get("account_id") or "default")
            chat_key = str(item.get("chat_key") or "")
            contact_key = str(item.get("contact_key") or "")
            # ① 人设口吻（与主动触达 _persona_style 同源解析——七个人设不该写出同一句关怀）
            try:
                from src.ai.persona_voice import resolve_effective_persona_id
                from src.utils.persona_manager import PersonaManager
                pid = resolve_effective_persona_id(
                    assistant.config.config or {}, platform, account_id, chat_key)
                if pid:
                    p = PersonaManager.get_instance().get_persona_by_id(pid) or {}
                    pers = p.get("personality")
                    style = (str(pers.get("style") or "")
                             if isinstance(pers, dict) else "")
                    hint = " ".join(str(p.get("style_hint") or "").split())
                    seg = "；".join(s for s in (style, hint) if s)
                    out["persona_line"] = seg[:160]
            except Exception:
                assistant.logger.debug("care persona 增强失败（忽略）", exc_info=True)
            # ② episodic 记忆要点（按约定主题重排相关性；memory_in_prompt 默认开）。
            # P2 #341：关怀是我方先开口，只拿 user_stated（memory_stated_only 默认开）——
            # AI 推断条目不配被主动陈述，与 proactive_topic._eligible_facts 同纪律。
            try:
                if bool(cfg_live.get("memory_in_prompt", True)):
                    sm = assistant.skill_manager
                    epi = getattr(sm, "_episodic_store", None) if sm else None
                    if epi is not None and sm is not None:
                        mkey = sm._episodic_storage_key(
                            chat_key, "", platform, account_id)
                        topic = str(item.get("topic") or "").strip()
                        out["memory_block"] = (epi.get_bullets_for_prompt(
                            mkey, max_items=5, max_chars=400,
                            query_text=(topic or None),
                            rerank_keywords=bool(topic),
                            stated_only=bool(cfg_live.get(
                                "memory_stated_only", True))) or "").strip()
            except Exception:
                assistant.logger.debug("care 记忆增强失败（忽略）", exc_info=True)
            # ③ 工作目标背景（实施84 care×goal 打通的 P0 面；goal_hint 默认开，
            # 且 goals.enabled 关闭时该函数自身返回空）
            try:
                if bool(cfg_live.get("goal_hint", True)):
                    from src.contacts.care_goal_link import care_goal_hint
                    out["goal_block"] = care_goal_hint(
                        assistant.config, conversation_id=contact_key,
                        platform=platform, account_id=account_id,
                        chat_key=chat_key)
            except Exception:
                assistant.logger.debug("care 目标增强失败（忽略）", exc_info=True)
            return out

        def _care_user_clock(item: dict):
            """客户时钟（实施84 P0-6）：``companion.user_clock`` enabled+schedule
            双开且解析信任档 ∈ replace/narrow 才接管 care 安静窗（与主动触达
            调度接管同一准入）。解析不出/异常 → None（服务器钟，旧行为）。"""
            try:
                comp2 = (assistant.config.config.get("companion") or {})
                uc = dict(comp2.get("user_clock") or {})
                if not (uc.get("enabled", False) and uc.get("schedule", False)):
                    return None
                from src.companion.user_clock_resolver import (
                    resolve_for_conversation,
                )
                sm = assistant.skill_manager
                epi = getattr(assistant, "_episodic_store", None)
                if epi is None and sm is not None:
                    epi = getattr(sm, "_episodic_store", None)
                mkey = ""
                try:
                    if sm is not None:
                        mkey = sm._episodic_storage_key(
                            str(item.get("chat_key") or ""), "",
                            str(item.get("platform") or ""),
                            str(item.get("account_id") or ""))
                except Exception:
                    mkey = ""
                clock = resolve_for_conversation(
                    str(item.get("contact_key") or ""),
                    inbox_store=assistant.inbox_store,
                    episodic_store=epi, memory_key=mkey, cfg=uc)
                if clock is not None and getattr(clock, "trust", "") in (
                        "replace", "narrow"):
                    return clock
                return None
            except Exception:
                return None

        def _care_schedule_clock(item: dict):
            """Q-37：关怀安静窗与 proactive 同源读 ``resolve_schedule_clock``。
            返回 ``(UserClock|None, source)``；异常 → ``(None, "server")``。"""
            try:
                from src.companion.user_clock_resolver import (
                    resolve_schedule_user_clock,
                )
                sm = assistant.skill_manager
                epi = getattr(assistant, "_episodic_store", None)
                if epi is None and sm is not None:
                    epi = getattr(sm, "_episodic_store", None)
                mkey = ""
                try:
                    if sm is not None:
                        mkey = sm._episodic_storage_key(
                            str(item.get("chat_key") or ""), "",
                            str(item.get("platform") or ""),
                            str(item.get("account_id") or ""))
                except Exception:
                    mkey = ""
                persona = None
                try:
                    from src.ai.persona_voice import resolve_effective_persona_id
                    from src.utils.persona_manager import PersonaManager
                    pid = resolve_effective_persona_id(
                        assistant.config.config or {},
                        str(item.get("platform") or ""),
                        str(item.get("account_id") or "default"),
                        str(item.get("chat_key") or ""))
                    if pid:
                        persona = PersonaManager.get_instance().get_persona_by_id(pid) or {}
                except Exception:
                    persona = None
                comp2 = (assistant.config.config.get("companion") or {})
                uc = dict(comp2.get("user_clock") or {})
                return resolve_schedule_user_clock(
                    str(item.get("contact_key") or ""),
                    inbox_store=assistant.inbox_store,
                    episodic_store=epi, memory_key=mkey, cfg=uc,
                    persona=persona)
            except Exception:
                return None, "server"

        ai_name = "她"
        try:
            ai_name = str((assistant.config.get_ai_config() or {}).get("ai_name") or "她")
        except Exception:
            ai_name = "她"

        # K2b：变现配额门控回调（仅当变现 gate 开启才注入；否则 None=不拦，零破坏）
        proactive_paywall = assistant._build_care_paywall(care_store)

        def _live_care_cfg() -> dict:
            """实时 proactive_care 配置（enabled/dry_run/max_per_tick 每 tick 消费）。"""
            try:
                return dict((assistant.config.config.get("companion") or {})
                            .get("proactive_care") or {})
            except Exception:
                return {}

        # P3：每联系人主动预算——读侧=既有 outreach_log 共享账本（proactive_topic
        # 真发本就落账），写侧=下方 _care_sent_hook。判定纯函数在 care_budget，
        # 阈值每次实时读（热闸）。inbox_store 缺失 → 恒放行（fail-open）。
        def _care_budget_gate(contact_key: str) -> bool:
            import time as _t

            from src.contacts.care_budget import (
                budget_allows, local_midnight_ts, parse_contact_budget_cfg,
            )
            store = assistant.inbox_store
            if store is None:
                return True
            bcfg = parse_contact_budget_cfg(_live_care_cfg())
            if not bcfg.enabled:
                return True
            now = _t.time()
            try:
                last = float(store.last_outreach_ts(contact_key) or 0)
                today = int(store.count_outreach_since(
                    contact_key, local_midnight_ts(now)))
            except Exception:
                return True
            return budget_allows(cfg=bcfg, last_touch_ts=last,
                                 touches_today=today, now=now)

        def _care_sent_hook(item: dict) -> None:
            """care 真发成功 → outreach_log 落账（batch_id=care:<topic>，note=care）。

            让 care 触达对「预算读侧 / proactive_review 周报 / 将来任何消费方」可见。
            实施84 P1-2 增量：目标推进型行（topic_norm=goal:*）同时把「已发」回执
            写进目标事件时间线（坐席在目标卡看得到这次触达）。
            """
            store = assistant.inbox_store
            if store is None:
                return
            topic = str(item.get("topic") or "")[:24]
            store.record_outreach(
                str(item.get("contact_key") or ""),
                batch_id=f"care:{topic}",
                platform=str(item.get("platform") or ""),
                account_id=str(item.get("account_id") or "default"),
                note="care",
            )
            try:
                from src.companion.goals.sprint_ticker import (
                    parse_goal_care_kind,
                    record_natural_beat_sent,
                    record_sprint_beat_sent,
                )
                kind, gid, arg = parse_goal_care_kind(item.get("topic_norm"))
                if gid:
                    from src.companion.goals.store import peek_goal_store
                    gstore = peek_goal_store()
                    if gstore is not None:
                        # M-7 A（#236）：beat_sent 带会话 + care 行 id——拍清单据 care#
                        # 反查话术快照（care 表 sent_text，hook 之后才写）与 deferred
                        # 投递真相（note=deferred:<row>）；此处拿不到正文，不硬塞。
                        _conv = str(item.get("contact_key") or "")
                        _cid = item.get("id") or item.get("care_id") or 0
                        if kind == "sprint":
                            # 冲刺主动拍（P0 2026-08-30）：落拍行+beat_sent+计数
                            record_sprint_beat_sent(
                                gstore, gid, int(arg),
                                conversation_id=_conv, care_id=_cid)
                            gstore.add_event(
                                gid, "care_sent",
                                f"冲刺拍 p{int(arg)} 已发出：{topic}",
                                conversation_id=_conv)
                        elif kind == "daily":
                            # 自然档每日主动拍（D1b P0-3）：今日拍行落 sent
                            record_natural_beat_sent(
                                gstore, gid, str(arg),
                                conversation_id=_conv, care_id=_cid)
                            gstore.add_event(
                                gid, "care_sent",
                                f"每日主动拍已发出：{topic}",
                                conversation_id=_conv)
                        else:
                            # impl84 goal_link 到期关怀：也是目标驱动的一次主动出手——
                            # 记一条 beat_sent（detail=care:link）让看门狗/拍清单只数
                            # beat_sent 一种 kind 就够（care_sent 保留为人话时间线）
                            gstore.add_event(
                                gid, "beat_sent",
                                f"care:link care#{int(_cid or 0)}"
                                if _cid else "care:link",
                                conversation_id=_conv)
                            gstore.add_event(
                                gid, "care_sent", f"到期关怀已发出：{topic}",
                                conversation_id=_conv)
            except Exception:
                assistant.logger.debug("care→goal 发出回执失败（忽略）", exc_info=True)

        # 对方机器人/自家账号守卫（P1 2026-08-03）：care 派发前统一卫生闸——
        # 谓词已在捕获接线前构造（2026-08-18 捕获/派发共用 _care_peer_filter）。

        def _goal_row_policy(item: dict) -> dict:
            """冲刺行豁免策略（P0 2026-08-30）：goals.sprint 实时配置 →
            {exempt_budget, ignore_quiet, jitter, live}。非相位行/关闸/异常 → {}。

            ``live``（D1b P0-1 2026-09-05）＝goals 开 + sprint 开 + sprint 自己的
            dry_run 关：该行绕过 care 的 enabled/dry_run 灰度门真发。冲刺借 care
            管线只是为了拟稿/投递/护栏复用，不该继承 care 的业务开关。
            自然档每日拍行（``:d…``，D1b P0-3）同样 live，但**不豁免**预算/安静
            时段——日常节奏就该吃日常刹车；只有冲刺是用户拍板的全力档。
            """
            try:
                from src.companion.goals.service import resolve_goals_cfg
                from src.companion.goals.sprint_ticker import (
                    parse_goal_care_kind,
                    parse_sprint_cfg,
                )
                kind, gid, _arg = parse_goal_care_kind(item.get("topic_norm"))
                if not gid or kind not in ("sprint", "daily"):
                    return {}
                gcfg = resolve_goals_cfg(assistant.config.config or {})
                scfg = parse_sprint_cfg(gcfg)
                if not scfg.get("enabled"):
                    return {}
                live = (bool(gcfg.get("enabled", False))
                        and not bool(scfg.get("dry_run")))
                if kind == "daily":
                    return {"exempt_budget": False, "ignore_quiet": False,
                            "jitter": scfg.get("jitter_sec"), "live": live}
                return {
                    "exempt_budget": bool(scfg.get("exempt_contact_budget")),
                    "ignore_quiet": bool(scfg.get("ignore_quiet_hours")),
                    "jitter": scfg.get("jitter_sec"),
                    "live": live,
                }
            except Exception:
                return {}

        # M-5 A（#217 / D-M6 2026-09-06）：目标行出站硬规则套在 send_callback 外——
        # 无产品目标禁报价/开户/支付话术（goal_no_product 拦截）+ 目标首条真发进
        # L1 草稿预览（reason=goal_first_send）。非 goal 行原样透传；包装失败退回
        # 裸 _care_send（守卫自身故障不该让 care 全链哑火）。
        _care_send_guarded = _care_send
        try:
            from src.companion.goals.product_guard import wrap_care_send

            def _catalog_probe() -> bool:
                """官网产品目录里是否真有货（acquire/retention 空 product_id ＝
                按画像自动选品，目录空则是空壳）。"""
                try:
                    from src.companion.goals import site_catalog as _sc
                    cat = _sc.load_catalog(_sc.catalog_path(
                        assistant.config.config or {},
                        getattr(assistant.config, "config_path", None)))
                    return bool((cat or {}).get("products"))
                except Exception:
                    return False

            def _goal_store_getter():
                from src.companion.goals.store import peek_goal_store
                return peek_goal_store()

            _care_send_guarded = wrap_care_send(
                _care_send, care_store=care_store,
                goal_store_getter=_goal_store_getter,
                inbox_store_getter=lambda: assistant.inbox_store,
                catalog_probe=_catalog_probe)
        except Exception:
            assistant.logger.debug("goal product_guard 接线失败（退回裸 send）",
                                   exc_info=True)

        def _care_profile(item: dict) -> dict:
            """客户档案（M-1 A #218）：inbox 会话 / conv_meta 用户时钟国家 / contacts 档案
            → {country, residence, language, known_since, known_days, display_name}。
            全部 best-effort，任一源缺失即该字段为空。"""
            from src.contacts.care_profile import build_customer_profile
            try:
                cstore = getattr(getattr(assistant, "contacts", None), "store", None)
            except Exception:
                cstore = None
            return build_customer_profile(
                item, inbox_store=assistant.inbox_store, contacts_store=cstore)

        async def _care_deliver_now(row_id: int, platform: str) -> dict:
            """N-1 A（#243）：「立即发」入队后当场投这一行——接多平台 deferred 队列的
            ``deliver_now``（同一 sender / 同一终态落库）。messenger 走浏览器 RPA 队列，
            没有同步路径 → 如实回「排队中」。"""
            if str(platform) == "messenger":
                return {"delivered": False, "status": "pending",
                        "reason": "messenger_rpa_queue"}
            disp = getattr(assistant, "_deferred_outbox_dispatcher", None)
            if disp is None or not hasattr(disp, "deliver_now"):
                return {"delivered": False, "status": "pending", "reason": "no_sync_path"}
            return await disp.deliver_now(int(row_id))

        def _care_tz_fallback(item: dict) -> dict:
            """Q-4（#267 D）：客户钟解析不出时，安静窗按 人设所在地 → 账号班表时区 判，
            不再默认服务器本地钟。返回 {"tz_name", "basis"}；两者皆无 → {}（服务器钟）。"""
            plat = str(item.get("platform") or "")
            acct = str(item.get("account_id") or "default")
            try:
                from src.ai.persona_voice import resolve_effective_persona_id
                from src.companion.persona_location import resolve_place_with_fallback
                from src.utils.persona_manager import PersonaManager
                pid = resolve_effective_persona_id(
                    assistant.config.config or {}, plat, acct,
                    str(item.get("chat_key") or ""))
                if pid:
                    p = PersonaManager.get_instance().get_persona_by_id(pid) or {}
                    place = resolve_place_with_fallback(p)
                    if place is not None and getattr(place, "tz_name", ""):
                        return {"tz_name": str(place.tz_name), "basis": "persona"}
            except Exception:
                assistant.logger.debug("care tz_fallback 人设时区解析失败", exc_info=True)
            try:
                from src.inbox.work_hours_gate import resolve_entry, work_schedule_cfg
                ws = work_schedule_cfg(assistant.config.config or {})
                tz_name = str(resolve_entry(ws, plat, acct).get("tz_name") or "").strip()
                if tz_name:
                    return {"tz_name": tz_name, "basis": "account"}
            except Exception:
                assistant.logger.debug("care tz_fallback 账号时区解析失败", exc_info=True)
            return {}

        dispatcher = CareDispatcher(
            store=care_store, ai_client=assistant.ai_client,
            send_callback=_care_send_guarded,
            tz_fallback_provider=_care_tz_fallback,
            context_provider=_care_context, proactive_allowed=proactive_paywall,
            already_discussed=_care_already_discussed,
            ai_name=ai_name,
            max_per_tick=int(cfg.get("max_per_tick", 3)),
            interval_sec=float(cfg.get("interval_sec", 600)),
            skip_if_no_context=bool(cfg.get("skip_if_no_context", True)),
            quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
            quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
            dry_run=bool(cfg.get("dry_run", False)),
            cfg_provider=_live_care_cfg,
            budget_gate=_care_budget_gate,
            sent_hook=_care_sent_hook,
            peer_filter=_care_peer_filter,
            prompt_extras_provider=_care_prompt_extras,
            user_clock_provider=_care_user_clock,
            schedule_clock_provider=_care_schedule_clock,
            goal_row_policy=_goal_row_policy,
            profile_provider=_care_profile,
            deliver_now=_care_deliver_now,
        )
        await dispatcher.start()
        assistant._care_dispatcher = dispatcher
        if web_app is not None:
            engine_state["dispatcher"] = dispatcher
            # N-1 A：派发器/投递 worker 都活在这条（主）loop 上；web 线程的路由
            # 要「立即发」得把协程投回来（run_coroutine_threadsafe），不能跨 loop await。
            try:
                engine_state["loop"] = asyncio.get_running_loop()
            except RuntimeError:
                pass
        # M-1 A #214：这行只是**启动快照**；dry_run 唯一真值 = dispatcher.effective_dry_run()
        # （实时配置），翻转时 care_dispatcher 另打一行「dry_run 实时值变化」。
        assistant.logger.info(
            "✅ proactive_care 派发循环已常备（interval=%ss, enabled=%s, dry_run=%s ← 启动快照，"
            "实时值以 /api/care/health 与「dry_run 实时值变化」日志为准）",
            cfg.get("interval_sec", 600), bool(cfg.get("enabled", False)),
            bool(cfg.get("dry_run", False)))

        # 实施84 P1-2：目标到期 → care 排期扫描器（常备接线 + 配置热闸：
        # proactive_care.goal_link.enabled 默认关，关闸时每 tick 空转零副作用）。
        try:
            from src.contacts.care_goal_link import CareGoalScanner
            goal_scanner = CareGoalScanner(
                care_store=care_store, config_obj=assistant.config,
                interval_sec=max(900.0, float(cfg.get("interval_sec", 600)) * 2))
            await goal_scanner.start()
            assistant._care_goal_scanner = goal_scanner
            if web_app is not None:
                engine_state["goal_scanner"] = goal_scanner
            assistant.logger.info(
                "✅ care×goal 到期排期扫描已常备（goal_link.enabled=%s）",
                bool((cfg.get("goal_link") or {}).get("enabled", False)))
        except Exception:
            assistant.logger.warning("care goal 扫描器启动跳过", exc_info=True)

        # P0 2026-08-30：冲刺推进器——限时目标（today/session）的时间驱动主动拍。
        # 常备接线 + 配置热闸（companion.goals.sprint.enabled 默认关，关闸时
        # 每 tick 空转零副作用）；排期进 care 管线，发送享 care 全套护栏。
        try:
            from src.companion.goals.sprint_ticker import SprintGoalTicker

            def _sprint_emotion_gate(goal: dict, meta: dict) -> str:
                """冲刺主动拍的情绪/危机档位：复用 skill_manager 同一判定
                （能查 crisis_event_store，block 真正可达）。失败 → ""。"""
                try:
                    sm = assistant.skill_manager
                    if sm is None:
                        return ""
                    mkey = sm._episodic_storage_key(
                        str(goal.get("chat_key") or ""), "",
                        str(goal.get("platform") or ""),
                        str(goal.get("account_id") or ""))
                    _i = meta.get("last_emotion_intensity")
                    return str(sm._proactive_emotion_gate(
                        mkey, str(meta.get("last_emotion") or ""),
                        float(_i) if _i is not None else -1.0) or "")
                except Exception:
                    return ""

            sprint_ticker = SprintGoalTicker(
                care_store=care_store, config_obj=assistant.config,
                inbox_store_getter=lambda: assistant.inbox_store,
                emotion_gate=_sprint_emotion_gate)
            await sprint_ticker.start()
            assistant._goal_sprint_ticker = sprint_ticker
            if web_app is not None:
                engine_state["sprint_ticker"] = sprint_ticker
                try:
                    web_app.state.goal_sprint_ticker = sprint_ticker
                except Exception:
                    pass
            # #166（2026-09-05）：此前 enabled=False 也打 ✅——skuio 机三次重启都
            # 「✅ 冲刺推进器已常备（enabled=False）」，目标卡却写着「自动推进」。
            # 引擎真相单一出口 goals.service.sprint_engine_status：关闸 / 派发终点
            # 被 care 关闸或 dry_run 拦住 → WARNING 直说「自动推进档不会主动出手」。
            try:
                from src.companion.goals.service import sprint_engine_status
                _es = sprint_engine_status(assistant.config.config or {})
            except Exception:
                _es = {}
            if _es.get("sprint_effective"):
                assistant.logger.info(
                    "✅ 冲刺推进器已常备（goals.sprint.enabled=True，platforms=%s，"
                    "natural_daily=%s）",
                    ",".join(_es.get("sprint_platforms") or ()),
                    bool(_es.get("natural_daily", True)))
                # D1b P0-2：白名单没显式配就是吃出厂默认——提醒运营看一眼，
                # 别再出现「sprint 开着、付费主力平台却静默不出手」
                if not _es.get("sprint_platforms_explicit"):
                    assistant.logger.warning(
                        "⚠ goals.sprint.platforms 未显式配置，按出厂默认 %s 出手；"
                        "要收窄/放宽请在 config.local.yaml 写明",
                        ",".join(_es.get("sprint_platforms") or ()))
            elif not _es.get("sprint_enabled"):
                assistant.logger.warning(
                    "⚠ 冲刺推进器未启用（companion.goals.sprint.enabled=False）："
                    "目标「自动推进」档不会主动出手，只在对方来消息时带方向")
            else:
                assistant.logger.warning(
                    "⚠ 冲刺推进器已开但派发被拦（%s）：限时目标的主动拍只拟稿不真发",
                    ",".join(_es.get("sprint_blockers") or ()) or "unknown")
        except Exception:
            assistant.logger.warning("冲刺推进器启动跳过", exc_info=True)

        # P2 2026-08-01：LLM 抽取影子扫描——与真实捕获同一入站事件源（第二个
        # inbound 回调，内部自带配置闸+廉价门），LLM 对照在异步 drain 循环限批限
        # 预算地跑，产物只有 JSONL + 计数（绝不入库不发送）。默认关
        # （proactive_care.llm_extract.shadow），常备接线 + 热闸同派发器哲学。
        try:
            from src.contacts.care_shadow_scan import CareShadowScanner
            # 与 plugin_dir 同一分裂规则：同树落 <数据根>/logs，VPS 离开 /etc。
            _shadow_log_dir = (
                plugin_dir(assistant.config.config_path).parent / "logs" / "care_shadow")
            shadow_scanner = CareShadowScanner(
                ai_client=assistant.ai_client,
                cfg_provider=_live_care_cfg,
                log_dir=_shadow_log_dir,
                interval_sec=float(cfg.get("interval_sec", 600)),
                # P4：真实捕获模式的写侧（llm_extract.enabled=true 才会写；
                # 纯影子期注入无副作用）
                care_store=care_store,
            )
            if assistant.inbox_store is not None:
                assistant.inbox_store.register_new_inbound_cb(shadow_scanner.inbound_cb)
            await shadow_scanner.start()
            assistant._care_shadow_scanner = shadow_scanner
            if web_app is not None:
                engine_state["shadow_scanner"] = shadow_scanner
            assistant.logger.info(
                "✅ care LLM 影子扫描已常备（shadow=%s）",
                bool((cfg.get("llm_extract") or {}).get("shadow", False)))
        except Exception:
            assistant.logger.warning("care 影子扫描启动跳过", exc_info=True)
    except Exception as ex:
        assistant.logger.warning("proactive_care 启动跳过: %s", ex)
        assistant.logger.debug("proactive_care 启动异常", exc_info=True)


# 自号互聊安全语料（P1 dry/go_live 都用，中性寒暄——go_live 只发到本号收藏消息 'me'）
_NURTURE_SELF_CHAT_CORPUS = (
    "今天也要加油鸭", "记个事情：晚点回复几个朋友", "备忘：下午整理一下资料",
    "喝口水休息一下", "随手记一笔", "提醒自己早点休息", "今天天气不错",
    "待办：回消息 / 看看动态", "小结一下今天", "记得多喝水",
)


async def maybe_start_nurture_engine(assistant, web_app=None) -> None:
    """智能养号执行引擎——常备接线 + 配置热闸（镜像 proactive_care）。

    默认全关（``ops.nurture.enabled=false``）：循环常备启动但每 tick 空转，零副作用。
    dry_run（enabled+dry_run）：算计划 + 影子记录，绝不碰账号。go_live（enabled+!dry_run）：
    **仅金丝雀白名单**账号真动作（read=标记已读 / self_chat=发到本号收藏消息），动作前查
    kill-switch。开关经 config.local.yaml 热重载 ~30s 生效，免重启（interval_sec 除外）。
    """
    try:
        from src.nurture.nurture_engine import NurtureEngine
        from src.nurture.nurture_ledger import get_nurture_ledger

        cfg = ((assistant.config.config.get("ops") or {}).get("nurture") or {})
        ledger = get_nurture_ledger(
            runtime_file("nurture_ledger.json", assistant.config.config_path))
        engine_state = {"skip": ""}
        if web_app is not None:
            web_app.state.nurture_ledger = ledger

        def _live_nurture_cfg() -> dict:
            try:
                return dict((assistant.config.config.get("ops") or {}).get("nurture") or {})
            except Exception:
                return {}

        def _accounts_provider():
            """从 ops.nurture.accounts 配置 × 注册表在册号，产出调度器输入。"""
            live = _live_nurture_cfg()
            accts_cfg = live.get("accounts") or {}
            if not isinstance(accts_cfg, dict) or not accts_cfg:
                return []
            try:
                from src.integrations.account_registry import get_account_registry
                reg = get_account_registry()
                known = {f"{r.get('platform')}:{r.get('account_id')}" for r in reg.list()}
            except Exception:
                known = set()
            out = []
            for key, plan in accts_cfg.items():
                key = str(key)
                if known and key not in known:
                    continue  # 只养在册号，跳过陈旧配置键
                if ":" not in key:
                    continue
                plat, acct = key.split(":", 1)
                out.append({"key": key, "platform": plat, "account_id": acct,
                            "plan": plan if isinstance(plan, dict) else {}})
            return out

        def _signals_provider(accounts):
            out = {}
            try:
                from src.integrations.account_registry import get_account_registry
                from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
                from src.integrations.protocol_autoreply_settings import cfg_with_settings
                from src.skills.account_signals import build_account_signals, lifecycle_stage
                reg = get_account_registry()
                try:
                    lim = get_autoreply_limiter(cfg_with_settings(assistant.config.config or {}))
                except Exception:
                    lim = None
                status_map = {f"{r.get('platform')}:{r.get('account_id')}": str(r.get("status") or "")
                              for r in reg.list()}
                for a in accounts or []:
                    plat, acct, key = a["platform"], a["account_id"], a["key"]
                    try:
                        sig = build_account_signals(plat, acct, registry=reg, limiter=lim)
                        stage = lifecycle_stage(sig, status_map.get(key, ""))
                        out[key] = {"stage": stage, "age_days": sig.get("age_days"),
                                    "banned": bool(sig.get("banned")),
                                    "circuit_open": bool(sig.get("_circuit_open")),
                                    # 风险退避信号（risk_backoff 消费：正被平台节流/报错的号不养）
                                    "flood_waits_24h": int(sig.get("flood_waits_24h") or 0),
                                    "errors_24h": int(sig.get("errors_24h") or 0)}
                    except Exception:
                        out[key] = {"stage": "offline"}  # 查不清=保守跳过（scheduler 剔 offline）
            except Exception:
                assistant.logger.debug("nurture signals provider 异常", exc_info=True)
            return out

        async def _run_on_web_loop(coro_factory):
            _wl = getattr(assistant, "_web_loop", None)
            if _wl is not None and _wl.is_running():
                fut = asyncio.run_coroutine_threadsafe(coro_factory(), _wl)
                return await asyncio.wrap_future(fut)
            return await coro_factory()

        async def _block_check(platform, account_id):
            try:
                from src.ops.kill_switch import is_blocked
                on, _scope, _reason = is_blocked(platform, account_id)
                return bool(on)
            except Exception:
                return True  # 查不清 kill-switch 一律保守拦

        async def _read_cb(platform, account_id):
            """read=给该号一条现有私聊标记已读（纯行为信号，不过 send-gate）。无会话→False。"""
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator()
                convs = []
                if assistant.inbox_store is not None:
                    convs = assistant.inbox_store.list_conversations(
                        limit=8, platform=platform, account_id=account_id,
                        chat_type="private") or []
                # 优先挑有未读的；否则挑最近一条
                target = ""
                for c in convs:
                    if int(c.get("unread") or 0) > 0:
                        target = str(c.get("chat_key") or "")
                        break
                if not target and convs:
                    target = str(convs[0].get("chat_key") or "")
                if not target:
                    return False
                return bool(await _run_on_web_loop(
                    lambda: orch.mark_read(platform, account_id, target)))
            except Exception:
                assistant.logger.debug("nurture read_cb 异常", exc_info=True)
                return False

        async def _self_chat_cb(platform, account_id):
            """self_chat=发一条中性寒暄到**本号收藏消息 'me'**（零外部足迹的安全发送信号）。"""
            try:
                import time as _t
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator()
                idx = int(_t.time() // 3600) % len(_NURTURE_SELF_CHAT_CORPUS)
                text = _NURTURE_SELF_CHAT_CORPUS[idx]
                res = await _run_on_web_loop(
                    lambda: orch.send(platform, account_id, "me", text, origin="auto"))
                if isinstance(res, dict):
                    return bool(res.get("delivered")) and not res.get("blocked")
                return bool(res)
            except Exception:
                assistant.logger.debug("nurture self_chat_cb 异常", exc_info=True)
                return False

        engine = NurtureEngine(
            ledger, cfg_provider=_live_nurture_cfg,
            accounts_provider=_accounts_provider, signals_provider=_signals_provider,
            read_cb=_read_cb, self_chat_cb=_self_chat_cb, block_check=_block_check,
            interval_sec=float(cfg.get("interval_sec", 900)),
            max_actions_per_tick=int(cfg.get("max_actions_per_tick", 20)),
        )
        await engine.start()
        assistant._nurture_engine = engine
        if web_app is not None:
            web_app.state.nurture_engine = engine
        assistant.logger.info(
            "✅ nurture_engine 已常备（interval=%ss, enabled=%s, dry_run=%s）",
            cfg.get("interval_sec", 900), bool(cfg.get("enabled", False)),
            bool(cfg.get("dry_run", True)))
    except Exception as ex:  # noqa: BLE001
        assistant.logger.warning("nurture_engine 启动跳过: %s", ex)
        assistant.logger.debug("nurture_engine 启动异常", exc_info=True)


async def maybe_start_reactivation_loop(assistant) -> None:
    """W2-D4.2/4.3：启动 reactivation 主动唤醒循环（陪护核心）。

    条件：contacts 子系统已启用 + messenger_rpa_service 已起 + 配置 reactivation.enabled
    """
    try:
        cfg_react = (assistant.config.config.get("reactivation") or {})
        if not cfg_react.get("enabled", False):
            assistant.logger.info("reactivation_loop 未启用（reactivation.enabled=false）")
            return
        if assistant.contacts is None or assistant.messenger_rpa_service is None:
            assistant.logger.info(
                "reactivation_loop 跳过（contacts=%s messenger_rpa=%s）",
                assistant.contacts is not None, assistant.messenger_rpa_service is not None,
            )
            return
        from src.contacts.reactivation_loop import ReactivationLoop

        # send_callback：把 reply 入 messenger 的 deferred 队列
        async def _send_to_messenger(channel, account_id, chat_name, reply,
                                     defer_until, reason, staleness_sec, extra):
            if channel != "messenger":
                # 非 messenger → 多平台 deferred 队列（关/不可用则返回 0，零破坏）
                return assistant._enqueue_deferred_outbox(
                    channel, account_id, chat_name, reply, defer_until,
                    reason, staleness_sec, extra)
            return await assistant.messenger_rpa_service.enqueue_reactivation_deferred(
                account_id=account_id,
                chat_name=chat_name,
                reply_text=reply,
                defer_until=defer_until,
                defer_reason=reason,
                staleness_sec=staleness_sec,
                extra=extra,
            )

        # last_activity_provider：从统一收件箱取该会话真实最近互动 ts（权威事实源），
        # 用于校正 journey.updated_at 陈旧导致的「刚聊过还发好久不见」。取不到 → 0（不拦，
        # 退回旧行为）。chat_key 对 telegram 是数字 id、对其他平台是各自 external_id。
        def _last_activity_provider(channel, account_id, chat_key) -> float:
            try:
                store = getattr(assistant, "inbox_store", None)
                if store is None:
                    return 0.0
                from src.inbox.normalizer import conv_id as _cid
                conv = store.get_conversation(
                    _cid(str(channel), str(account_id), str(chat_key)))
                if not conv:
                    return 0.0
                return float(conv.get("last_ts") or 0.0)
            except Exception:
                return 0.0

        # episodic_provider：拿 journey 对象 → 渲染画像 block 给 reactivation prompt
        def _episodic_provider(journey) -> str:
            try:
                if journey is None:
                    return ""
                snap = (getattr(journey, "context_snapshot_json", "") or "").strip()
                if snap:
                    from src.contacts.portrait_extractor import render_block
                    return render_block(snap) or ""
                return ""
            except Exception:
                return ""

        ai_name = ""
        try:
            ai_name = str((assistant.config.get_ai_config() or {}).get("ai_name") or "她")
        except Exception:
            ai_name = "她"

        # 真实近期活跃阈值：与 scheduler 的 min_silent_days 同源（contacts 段），
        # 保证「选候选」与「发送前复核」用同一把尺。
        _min_silent_days = float(
            ((assistant.config.config.get("contacts") or {}).get("min_silent_days")) or 3)

        # 对方机器人/自家账号守卫（P1 2026-08-03）：reactivation 派发前统一卫生闸
        # （与 care / proactive_topic 复用 build_peer_filter 同一入口）。
        _react_peer_filter = None
        try:
            from src.companion.proactive_peer_hygiene import build_peer_filter
            from src.integrations.account_registry import get_account_registry
            _react_peer_filter = build_peer_filter(
                assistant.inbox_store, assistant.config.config or {},
                registry=get_account_registry())
        except Exception:
            assistant.logger.debug(
                "reactivation peer_filter 构造失败（不拦）", exc_info=True)

        # 实施84 P0-6：reactivation 真发落 outreach_log 共享账本——此前只写
        # journey_events，逃逸在每联系人打扰预算（care contact_budget 读侧）与
        # 统一触达时间线之外；同一个客户可能同日吃「召回+关怀+问候」三连。
        def _react_sent_hook(info: dict) -> None:
            store = assistant.inbox_store
            if store is None:
                return
            try:
                from src.inbox.normalizer import conv_id as _cid
                cid = _cid(str(info.get("channel") or ""),
                           str(info.get("account_id") or "default"),
                           str(info.get("chat_name") or ""))
                store.record_outreach(
                    cid,
                    batch_id=f"reactivation:silent_"
                             f"{int(float(info.get('silent_days') or 0))}d",
                    platform=str(info.get("channel") or ""),
                    account_id=str(info.get("account_id") or "default"),
                    note="reactivation",
                )
            except Exception:
                assistant.logger.debug(
                    "reactivation outreach 落账失败（忽略）", exc_info=True)

        loop = ReactivationLoop(
            scheduler=assistant.contacts.reactivation,
            store=assistant.contacts.store,
            ai_client=assistant.ai_client,
            send_callback=_send_to_messenger,
            episodic_provider=_episodic_provider,
            last_activity_provider=_last_activity_provider,
            min_silent_sec=_min_silent_days * 86400.0,
            ai_name=ai_name,
            peer_filter=_react_peer_filter,
            sent_hook=_react_sent_hook,
            max_per_tick=int(cfg_react.get("max_per_tick", 3)),
            interval_sec=float(cfg_react.get("interval_sec", 600)),
            skip_if_no_episodic=bool(cfg_react.get("skip_if_no_episodic", True)),
            dry_run=bool(cfg_react.get("dry_run", False)),
            first_run_grace_minutes=float(
                cfg_react.get("first_run_grace_minutes", 60),
            ),
            first_run_max_per_tick=int(
                cfg_react.get("first_run_max_per_tick", 1),
            ),
            platform_priority=(
                cfg_react.get("platform_priority")
                or ["messenger", "telegram", "line", "whatsapp"]
            ),
        )
        await loop.start()
        assistant._reactivation_loop = loop
        assistant.logger.info(
            "✅ reactivation_loop 已启动（interval=%ss max_per_tick=%s）",
            cfg_react.get("interval_sec", 600), cfg_react.get("max_per_tick", 3),
        )
    except Exception as ex:
        assistant.logger.warning("reactivation_loop 启动跳过: %s", ex)
        assistant.logger.debug("reactivation_loop 启动异常", exc_info=True)


def deferred_outbox_accepting(assistant) -> bool:
    """多平台 deferred 队列**是否接收入队**（``companion.multiplatform_deferred.enabled``
    实时值，N-1 D #243）。开关只管「收不收新消息」，不再管队列建不建 / drain loop 起不起。"""
    try:
        comp = (assistant.config.config.get("companion") or {})
        return bool((comp.get("multiplatform_deferred") or {}).get("enabled", False))
    except Exception:
        return False


def ensure_deferred_outbox(assistant):
    """建多平台 deferred 队列（非 messenger 主动消息走此队列）并保证 drain loop 在跑。

    N-1 D（#243，2026-09-08）改造：**不再以 ``multiplatform_deferred.enabled`` 决定建不建**
    ——旧行为「关闸时返回 None、开闸后首次入队才懒建」有两个洞：① 启动期关闸 →
    ``_maybe_start_deferred_outbox`` 直接 return，运行时开闸后没人再 start；② 懒建路径
    只建不 start。skuio 09-07 实录：13:14 启动 enabled=False，15:27 页面开真发，16:34:52
    首条关怀入队同秒队列「已就绪」，预计 16:47:52 出队——**没有任何协程在 drain**，直到
    进程重启。现在：store + dispatcher 随后端启动即建（幂等），``enabled`` 退化为
    ``_enqueue_deferred_outbox`` 的**入队热闸**（关＝不收新消息、已入队的照常 drain）；
    懒建路径也 ``ensure_started()``。返回 dispatcher，或 None（初始化异常）。
    sender 用编排器 `orch.send(platform,account,chat_key,text)` 统一投递（编排器已
    路由到对应平台 worker 并回写收件箱出站镜像）；worker 未就绪 → 抛 NotReady 推后重试。
    messenger 不走此队列（保留既有 runner deferred 路径）。
    """
    if assistant._deferred_outbox_dispatcher is not None:
        disp = assistant._deferred_outbox_dispatcher
        try:
            if hasattr(disp, "ensure_started"):
                disp.ensure_started()
        except Exception:
            assistant.logger.debug("deferred_outbox ensure_started 异常", exc_info=True)
        return disp
    try:
        comp = (assistant.config.config.get("companion") or {})
        cfg = (comp.get("multiplatform_deferred") or {})
        from src.integrations.shared.deferred_outbox import (
            DeferredDispatcher, DeferredOutboxStore, DeferredSenderNotReady,
        )

        store = DeferredOutboxStore(
            runtime_file("deferred_outbox.db", assistant.config.config_path))

        async def _universal_send(account_id, chat_key, text, *, platform):
            # 出站自动翻译/语言硬闸（主动触达：care/reactivation 等经 deferred 队列的
            # 非 messenger 主动消息）。care 默认按 zh 生成 → 真译成客户语言；
            # reactivation 本就按客户语言生成 → 检测护栏自动跳过（no-op）。
            # translate 关闭时 lang_gate（默认开）gate-only 只拦 CJK 实质冲突。
            text = await assistant._maybe_translate_outbound(
                platform, account_id, chat_key, text)
            if text is None:
                # 语言硬闸 HOLD（P3-198）：文本语言与客户语言实质冲突且翻译不可用
                # → 按暂态推后重试（翻译引擎恢复后自动补投），绝不原样发出。
                raise DeferredSenderNotReady("lang_gate_hold")
            # WP-4 rider ② 系统级披露（compliance.disclosure.notice，基线关）：
            # 本函数是 care/reactivation 等 deferred 主动家族的**单一投递收口**
            # （5 平台同点）——首触即 AI 的会话在此前置披露语。放在出站翻译之后
            # （披露语按会话语言取，绝不再过翻译层）；防重键与回复三线同键空间。
            try:
                from src.compliance.disclosure import apply_disclosure_for
                from src.inbox.normalizer import conv_id as _dc_cid
                text, _ = apply_disclosure_for(
                    _dc_cid(str(platform), str(account_id), str(chat_key)),
                    text, store=getattr(assistant, "inbox_store", None))
            except Exception:
                assistant.logger.debug(
                    "[deferred_outbox] 披露注入异常（原样发送）", exc_info=True)
            # 1) 编排器受管 worker（telegram/whatsapp/line… 任一暴露 send 的）
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(assistant.config.config or {})
                if orch.owns(platform, account_id):
                    res = await orch.send(platform, account_id, chat_key, text)
                    return bool((res or {}).get("delivered", True))
            except DeferredSenderNotReady:
                raise
            except Exception:
                assistant.logger.debug("[deferred_outbox] 编排器发送异常 %s:%s",
                                  platform, account_id, exc_info=True)
            # 2) 回落：主 A 线客户端（仅 telegram default）
            if platform == "telegram" and assistant.telegram_client is not None:
                try:
                    target = int(chat_key)
                except (TypeError, ValueError):
                    target = chat_key
                try:
                    return bool(await assistant.telegram_client.send_message(target, text))
                except Exception:
                    assistant.logger.debug("[deferred_outbox] 主客户端发送失败", exc_info=True)
                    return False
            # 3) 该账号此刻无可用 worker → 暂态，推后重试（不丢、不标失败）
            raise DeferredSenderNotReady(f"no worker for {platform}:{account_id}")

        def _make_sender(platform):
            async def _s(account_id, chat_key, text):
                return await _universal_send(account_id, chat_key, text,
                                             platform=platform)
            return _s

        # 主动消息发送前活跃复核（defer 窗口内对方开口 → 取消过时「好久不见」）：
        # 权威事实源=统一收件箱 conversations.last_ts。阈值与召回 min_silent 同源。
        def _deferred_activity_provider(platform, account_id, chat_key) -> float:
            try:
                istore = getattr(assistant, "inbox_store", None)
                if istore is None:
                    return 0.0
                from src.inbox.normalizer import conv_id as _cid
                conv = istore.get_conversation(
                    _cid(str(platform), str(account_id), str(chat_key)))
                return float((conv or {}).get("last_ts") or 0.0)
            except Exception:
                return 0.0

        _guard_days = float(
            ((assistant.config.config.get("contacts") or {}).get("min_silent_days")) or 3)

        dispatcher = DeferredDispatcher(
            store=store,
            quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
            quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
            min_gap_sec=float(cfg.get("min_gap_sec", 45)),
            max_per_tick=int(cfg.get("max_per_tick", 3)),
            interval_sec=float(cfg.get("interval_sec", 120)),
            activity_provider=_deferred_activity_provider,
            recent_activity_guard_sec=_guard_days * 86400.0,
        )
        platforms = cfg.get("platforms") or [
            "telegram", "line", "whatsapp", "instagram", "zalo",
            # QQ 协议登录（个人号，Milky）可主动触达；QQ 机器人（qqbot）刻意不在缺省表——
            # 官方被动窗口外发不出去，进队列只会被账本拦成 window_expired
            "qq",
        ]
        for p in platforms:
            dispatcher.register_sender(str(p), _make_sender(str(p)))

        assistant._deferred_outbox_dispatcher = dispatcher
        if assistant._web_app is not None:
            assistant._web_app.state.deferred_outbox_store = store
            assistant._web_app.state.deferred_outbox_dispatcher = dispatcher
        # 懒建路径（首次入队时才走到这里）也必须让 drain loop 跑起来；启动期由
        # ``_maybe_start_deferred_outbox`` await start()，这里同步 ensure 是幂等兜底。
        started = False
        try:
            started = bool(dispatcher.ensure_started())
        except Exception:
            assistant.logger.debug("deferred_outbox ensure_started 异常", exc_info=True)
        assistant.logger.info(
            "✅ 多平台 deferred 队列已就绪（platforms=%s interval=%ss drain_loop=%s "
            "accepting=%s ← 入队热闸 companion.multiplatform_deferred.enabled 实时读）",
            platforms, cfg.get("interval_sec", 120),
            "running" if started else "pending_start", bool(cfg.get("enabled", False)))
        return dispatcher
    except Exception:
        assistant.logger.warning("多平台 deferred 队列初始化失败（非 messenger 主动消息将被丢弃）",
                             exc_info=True)
        return None


async def warmup_embeddings(assistant):
    """后台批量向量化无 embedding 的知识库条目"""
    try:
        await asyncio.sleep(5)
        if not assistant.ai_client or not assistant.ai_client.client:
            return
        if hasattr(assistant.config, "config_path"):
            cfg_dir = runtime_dir(assistant.config.config_path)
        else:
            cfg_dir = Path("config")
        kb_path = (cfg_dir / "knowledge_base.db").resolve()
        if not kb_path.exists():
            assistant.logger.info("向量预热: 知识库文件不存在，跳过 (%s)", kb_path)
            return
        from src.utils.kb_store import KnowledgeBaseStore
        kb = KnowledgeBaseStore(kb_path)
        pending = kb.get_entries_without_embedding()
        if not pending:
            assistant.logger.info("向量预热: 所有条目已向量化 (%d 条)", kb._vindex.count())
            return
        assistant.logger.info("向量预热: 发现 %d 条待向量化条目，开始批量处理...", len(pending))
        batch_size = 20
        done = 0
        for i in range(0, len(pending), batch_size):
            if not assistant.running:
                break
            batch = pending[i:i + batch_size]
            texts = []
            for e in batch:
                parts = [e.get("title", "")]
                trigs = e.get("triggers", "")
                if trigs:
                    try:
                        import json as _j
                        tl = _j.loads(trigs) if isinstance(trigs, str) else trigs
                        if isinstance(tl, list):
                            parts.append(" ".join(tl))
                    except Exception:
                        pass
                for f in ("scenario", "steps", "principles"):
                    if e.get(f):
                        parts.append(e[f][:200])
                texts.append(" ".join(parts)[:500])
            try:
                vecs = await assistant.ai_client.embed_with_fallback(texts)
                if vecs and len(vecs) == len(batch):
                    n_ok = 0
                    for entry, vec in zip(batch, vecs):
                        if not vec:
                            continue
                        kb.set_single_embedding(entry["id"], vec)
                        n_ok += 1
                    done += n_ok
                    assistant.logger.debug("向量预热: 已处理 %d/%d (本批成功 %d)", done, len(pending), n_ok)
                else:
                    assistant.logger.warning(
                        "向量预热: 批次返回数量仍不匹配 (%s vs %s)",
                        len(vecs) if vecs else 0, len(batch),
                    )
            except Exception as e:
                assistant.logger.warning("向量预热: 批次失败: %s", e)
            await asyncio.sleep(1.5)
        cov = kb.embedding_coverage()
        assistant.logger.info("向量预热完成: %d 条新增向量化, 总覆盖率 %s%%", done, cov.get("pct", 0))
    except Exception:
        assistant.logger.exception("向量预热异常")


def maybe_init_monetization(assistant, web_app=None) -> None:
    """Phase K2：C 端变现（默认关，monetization.enabled 开）。

    开启时建 EntitlementStore 单例（落运行时目录 entitlements.db）→ 挂 app.state 供路由用，
    并按 catalog 注入价目；启动可选清理过期订阅。关时不建库（路由会按需懒建只读单例）。
    """
    try:
        mon = (assistant.config.config.get("monetization") or {})
        if not mon.get("enabled", False):
            assistant.logger.info("C 端变现未启用（monetization.enabled=false）")
            return
        from src.utils.entitlement_store import get_entitlement_store
        from src.utils.monetization import merge_catalog

        catalog = merge_catalog(mon.get("catalog"))
        _rt = runtime_dir(assistant.config.config_path)
        store = get_entitlement_store(_rt / "entitlements.db", catalog=catalog)
        if web_app is not None:
            web_app.state.entitlement_store = store
        # Stage 1：把真实权益接进对话路径——注册进程级 resolver，让付费剧情闸
        # （story_engine.require_unlock）据端用户真实拥有判准入。仅在变现就绪时注册，
        # 故未启用时 resolve_entitlement 恒 None → 付费场景仍对所有人锁（零回归）。
        try:
            from src.utils.companion_context import set_relationship_providers
            set_relationship_providers(
                entitlement_resolver=lambda ck: store.get_entitlement(ck))
            assistant.logger.info("✅ 对话剧情付费闸已接入真实权益（entitlement resolver 已注册）")
        except Exception:
            assistant.logger.debug("entitlement resolver 注册失败", exc_info=True)
        if mon.get("expire_on_startup", True):
            try:
                store.expire_subscriptions()
            except Exception:
                pass
        # Stage 3：付费预告转化漏斗埋点库（teaser 发出 → tx_ledger 归因）。
        try:
            from src.utils.companion_funnel_store import (
                get_companion_funnel_store,
            )
            funnel = get_companion_funnel_store(_rt / "companion_funnel.db")
            assistant._companion_funnel_store = funnel
            if web_app is not None:
                web_app.state.companion_funnel_store = funnel
            assistant.logger.info("✅ 付费预告转化漏斗埋点已就绪")
        except Exception:
            assistant.logger.debug("companion funnel store 初始化跳过", exc_info=True)
        assistant.logger.info("✅ C 端变现已就绪（EntitlementStore 已挂载）")
    except Exception:
        assistant.logger.warning("C 端变现初始化跳过", exc_info=True)


async def reconcile_dead_peer_marks(assistant) -> None:
    """#88 存量 dead-peer 标记核销（启动一次性迁移，幂等；flag 关=no-op）。

    0830 skuio 实锤：#73 补齐「送达即清标」钩子之前累积的旧标记（Kate/Yhang
    『曾被对方拉黑』）没有任何核销机制——客户早已恢复可达（消息带勾送达）、
    黄条仍常驻且自动回复停摆。开机后延迟扫一遍登记表：标记时间之后 messages
    出站镜像（只在成功路径写入）里有任一出站行 → 清标。deactivated 恒不核销。
    """
    try:
        from src.ops.dead_peer_registry import (
            reconcile_stale_marks, registry_from_config,
        )
        reg = registry_from_config(assistant.config)
        if reg is None:
            return   # flag 关 / 配置不可解析：零行为
        store = getattr(assistant, "inbox_store", None)
        db_path = getattr(store, "_db_path", None) if store is not None else None
        if not db_path:
            return
        # 错开启动高峰（store 迁移/各链预热），核销不抢窗口
        await asyncio.sleep(30)
        out = await asyncio.to_thread(
            reconcile_stale_marks, reg, db_path, log=assistant.logger)
        if out.get("cleared"):
            assistant.logger.info(
                "✅ dead-peer 存量核销完成：检查 %s 条、解除 %s 条陈旧标记",
                out.get("checked", 0), out.get("cleared", 0))
    except Exception:
        assistant.logger.debug("dead-peer 存量核销跳过", exc_info=True)


async def episodic_backfill_periodic(assistant):
    """可选：按间隔补全情景记忆缺失向量（memory.vector.backfill_periodic）。"""
    try:
        mcfg = (assistant.config.config or {}).get("memory") or {}
        vcfg = (mcfg.get("vector") or {})
        pcfg = vcfg.get("backfill_periodic") or {}
        if not pcfg.get("enabled", False):
            return
        init_delay = float(pcfg.get("initial_delay_seconds", 1800))
        await asyncio.sleep(max(0.0, init_delay))
    except Exception:
        assistant.logger.exception("情景记忆周期补全初始化失败")
        return

    while assistant.running:
        try:
            mcfg = (assistant.config.config or {}).get("memory") or {}
            vcfg = (mcfg.get("vector") or {})
            pcfg = vcfg.get("backfill_periodic") or {}
            if not pcfg.get("enabled", False):
                await asyncio.sleep(3600)
                continue
            if not vcfg.get("enabled", False):
                await asyncio.sleep(min(3600.0, float(pcfg.get("interval_hours", 6)) * 3600.0))
                continue
            limit = max(1, min(int(pcfg.get("limit", 20)), 100))
            sm = assistant.skill_manager
            if sm:
                out = await sm.episodic_backfill_embeddings(limit)
                if int(out.get("updated") or 0) > 0:
                    assistant.logger.info("情景记忆周期补全: %s", out)
                else:
                    assistant.logger.debug("情景记忆周期补全: %s", out)
        except Exception:
            assistant.logger.exception("情景记忆周期补全失败")
        try:
            hrs = float(
                ((assistant.config.config or {}).get("memory") or {})
                .get("vector", {})
                .get("backfill_periodic", {})
                .get("interval_hours", 6)
            )
        except (TypeError, ValueError):
            hrs = 6.0
        await asyncio.sleep(max(60.0, hrs * 3600.0))
