"""工作链出厂模板（种子包，B1）——统一收件箱工作链的冷启动与演示备货。

四条行业通用跟进 SOP，`ensure_starter_chains(store)` 幂等导入：按**固定 chain_id**
判重，已存在（含运营改过的）一律跳过，绝不覆盖——「导入示例」不能变成「重置我的链」。

安全默认：种子链全部**手动启动**（``trigger_conditions={}``）。自动触发（沉默天数/
高流失）会让导入动作立刻对沉默会话批量开链，属惊吓默认值；运营看过步骤内容后在
/workflows 编辑器里自己开。template 步骤只产建议话术（经 ``workflow_step`` 事件进
坐席 Copilot 预填），不会自动发送。

步骤 schema 与 /workflows 链编辑器完全一致：``{action_type, note, delay_hours}``
（delay_hours=该步执行前的等待；task 步骤的 delay_hours 同时是任务到期窗口——
WorkflowRunner 现状语义，种子取值两义下都合理）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 话术类步骤的 note 写成「方向指令+示例」而非逐字稿：消费端是 LLM Copilot 预填
# （reply-suggest 拿 workflow_text 当生成种子），指令式文本对不同客户泛化更好。
STARTER_CHAINS: List[Dict[str, Any]] = [
    {
        "chain_id": "starter_icebreak_7d",
        "name": "新客破冰 · 7日跟进",
        "steps": [
            {"action_type": "template", "delay_hours": 0,
             "note": "破冰开场：自然打招呼+轻话题（今天过得怎么样/最近在忙什么），只聊天不推销"},
            {"action_type": "template", "delay_hours": 48,
             "note": "第3天：接上次话题延伸，分享一件自己的日常小事，用开放式问题收尾"},
            {"action_type": "template", "delay_hours": 48,
             "note": "第5天：给一次轻价值（实用小建议/有趣内容），与对方兴趣相关，自然不推销"},
            {"action_type": "task", "delay_hours": 48,
             "note": "第7天复盘：互动质量如何？决定推进方向（继续培养/介绍产品/降频观察）"},
        ],
        "trigger_conditions": {},
        "enabled": True,
    },
    {
        "chain_id": "starter_reactivate_3step",
        "name": "沉默唤回 · 3步",
        "steps": [
            {"action_type": "template", "delay_hours": 0,
             "note": "轻唤回：提一件对方之前亲口说过的具体小事，只关心不追问（例：上次你说在忙搬家，安顿好了吗）"},
            {"action_type": "template", "delay_hours": 48,
             "note": "仍没回则换角度：分享一件与对方兴趣相关的新鲜事，零压力结尾，不问「在吗」"},
            {"action_type": "task", "delay_hours": 72,
             "note": "第5天仍未回：人工评估此客户（换渠道/降频/暂停跟进），避免继续连发骚扰"},
        ],
        "trigger_conditions": {},
        # 实施92：唤回链的使命就是等回复——客户开口＝目标达成，链按完成收束
        # （其余种子链缺省 ''＝随全局 reply_yield 默认「暂停让人接管」）。
        "on_reply": "complete",
        "enabled": True,
    },
    {
        "chain_id": "starter_postsale_care",
        "name": "成交后关怀 · 72小时",
        "steps": [
            {"action_type": "template", "delay_hours": 0,
             "note": "成交感谢：确认订单/权益已到位，告知有任何问题随时找我"},
            {"action_type": "tag", "tag": "成交客户", "delay_hours": 0, "note": ""},
            {"action_type": "template", "delay_hours": 24,
             "note": "24小时回访：使用/体验还顺利吗？有问题第一时间帮忙解决"},
            {"action_type": "task", "delay_hours": 48,
             "note": "72小时后：满意客户建「复购/转介绍」跟进任务，不满意客户升级处理"},
        ],
        "trigger_conditions": {},
        "enabled": True,
    },
    {
        "chain_id": "starter_quote_followup",
        "name": "报价跟单 · 3步",
        "steps": [
            {"action_type": "template", "delay_hours": 0,
             "note": "报价后确认：对方是否收到并看懂报价，主动问还有什么疑虑"},
            {"action_type": "template", "delay_hours": 24,
             "note": "24小时未定：补充一条价值信息（客户案例/对比优势/售后保障），不催单"},
            {"action_type": "template", "delay_hours": 24,
             "note": "48小时：给一个现在行动的理由（限时优惠/库存/名额），语气软推进"},
            {"action_type": "task", "delay_hours": 24,
             "note": "72小时仍未定：建任务人工跟单收尾（电话/换决策人/让步空间评估）"},
        ],
        "trigger_conditions": {},
        "enabled": True,
    },
]

# C1/J：目标模板 → 推荐种子链。前端单源在 shared/copilot/sidebar-chrome.js 的
# CopilotShared.GOAL_CHAIN_REC（cp-goal / cp-chain-exec 两组件消费）；本表是后端
# 消费拷贝（漏斗「推荐跟随率」按 goal.template 反查），**两份必须逐项相等**，
# 由 tests/test_workflows_feature_flag.py::test_rec_map_js_equals_python 钉住。
# 只登记高置信对应；relationship_* / custom 诚实不推荐（不进跟随率分母）。
GOAL_CHAIN_REC: Dict[str, str] = {
    "engagement_reactivate": "starter_reactivate_3step",   # 沉默唤回 ↔ 唤回链
    "acquire_and_convert": "starter_icebreak_7d",          # 获客转化 ↔ 新客破冰
    "retention_expand": "starter_postsale_care",           # 留存增购 ↔ 成交后关怀
    "conversion_unlock": "starter_quote_followup",         # 付费解锁 ↔ 报价跟单
    "conversion_subscribe": "starter_quote_followup",      # 会员订阅 ↔ 报价跟单
}

# 实施92 P0-5：旅程阶段 → 推荐自动挂链（成交引擎预设的接线表）。刻意只登记
# 高置信两对：进报价阶段 → 报价跟单、成交 → 售后关怀。破冰/唤回**不进预设**
# ——破冰挂在「每个新会话」上是噪音制造机；沉默客户的触达本部署由
# proactive_topic（全自动、有退避/变体/频控）负责，链再叠一层＝双打扰。
STAGE_CHAIN_REC: Dict[str, str] = {
    "quoting": "starter_quote_followup",
    "deal": "starter_postsale_care",
}

# 实施93：guide 风味接线表（quoting 位=已引导 → 点击促发；deal 位=已转化 →
# 转化后跟进）。阶段内部 id 不变，见 journey_stage.resolve_journey_cfg.flavor。
GUIDE_STAGE_CHAIN_REC: Dict[str, str] = {
    "quoting": "guide_click_nudge",
    "deal": "guide_post_convert",
}


def stage_chain_rec_for(flavor: str) -> Dict[str, str]:
    """按旅程风味取「阶段→推荐链」表（预设接线/右栏建议/漏斗跟随共用口径）。"""
    return (GUIDE_STAGE_CHAIN_REC if str(flavor or "") == "guide"
            else STAGE_CHAIN_REC)


# 实施93：引导转化行业链包（导流→点击促发→转化后跟进）。话术步的 ``cta`` 字段
# ＝发送时确定性追加该会话的追踪短链（``@primary``＝第一个启用的转化目标）；
# 铸链失败整步降级提醒坐席（fail-closed，见 workflow_auto_step）。
GUIDE_CHAINS: List[Dict[str, Any]] = [
    {
        # 单一职责：铺垫+发链就收工——发链进「已引导」后，催点/复盘由
        # guide_click_nudge（stage_enter 自动挂）接手，两链绝不对同一客户叠加催促
        "chain_id": "guide_warmup_3",
        "name": "导流铺垫 · 2步",
        "steps": [
            {"action_type": "template", "delay_hours": 0,
             "note": "价值铺垫：围绕对方最近聊的话题给一条真实有用的信息/结论，"
                     "自然带出「我们有个地方能看到更多」，本步不发链接不推销"},
            {"action_type": "template", "delay_hours": 24, "cta": "@primary",
             "note": "顺着昨天的话题给出去处：一句话说清点开能得到什么"
                     "（消息末尾会自动带上追踪链接，文案别自己写网址）"},
        ],
        "trigger_conditions": {},
        "enabled": True,
    },
    {
        "chain_id": "guide_click_nudge",
        "name": "点击促发 · 2步",
        "steps": [
            {"action_type": "template", "delay_hours": 24, "cta": "@primary",
             "note": "链接发出 24 小时没点开：换个说法讲点开的价值，给一个"
                     "现在看的理由（链接自动附带；对方一点开本链自动收工）"},
            {"action_type": "task", "delay_hours": 48,
             "note": "72 小时仍未点：人工复盘该客户（换目标/换钩子/降频）"},
        ],
        "trigger_conditions": {},
        "enabled": True,
    },
    {
        "chain_id": "guide_post_convert",
        "name": "转化后跟进 · 3步",
        "steps": [
            {"action_type": "template", "delay_hours": 0,
             "note": "对方刚点开/完成动作：确认体验（打得开吗/找得到吗），"
                     "给一条上手小提示"},
            {"action_type": "template", "delay_hours": 24,
             "note": "24小时回访：用得怎么样？有没有卡住的地方，顺手答疑"},
            {"action_type": "template", "delay_hours": 72,
             "note": "引导下一步动作：回访/更深入的功能/推荐给朋友（按业务目标），"
                     "自然不硬推"},
        ],
        "trigger_conditions": {},
        "enabled": True,
    },
]

_VALID_STEP_TYPES = {"template", "task", "tag", "note", "escalate"}


def goal_auto_attach_enabled(cfg_root: Any) -> bool:
    """``inbox.workflows.goal_auto_attach``（默认关）——建目标即自动挂推荐链。"""
    try:
        if not isinstance(cfg_root, dict):
            return False
        wf = ((cfg_root.get("inbox") or {}).get("workflows") or {})
        return bool(wf.get("goal_auto_attach", False))
    except Exception:
        return False


def maybe_auto_attach_chain(
    store: Any,
    cfg_root: Any,
    *,
    conversation_id: str,
    goal_id: str,
    goal_template: str,
    goal_autonomy: str,
) -> Optional[Dict[str, str]]:
    """P3 2026-08-13：建目标即自动挂上模板推荐的跟进 SOP 链（目标定方向 →
    SOP 保执行，一步到位）。返回 ``{"chain_id","name"}``；未挂 → None。绝不抛。

    五重闸（全过才挂——挂链会产生提醒/自动步，绝不能变成建目标的隐形副作用）：
    1. ``inbox.workflows.goal_auto_attach``（默认关）+ workflows 模块可用；
    2. 目标 autonomy == auto（suggest/observe 是「人主导」的明示选择，不代挂）；
    3. 模板在 GOAL_CHAIN_REC 有高置信推荐（relationship_*/custom 诚实无推荐）；
    4. 推荐链真实存在且 enabled（种子未导入/被停用 → 不挂不造）；
    5. 同会话同链无在途执行（has_running_chain 含 paused）。
    归因与手动「推荐」入口同口径：context 带 goal_id → 漏斗推荐跟随率/归因组
    回复率自动计入。goal_events 的 chain_started 回写由调用方（路由层）负责——
    与 start-chain 路由同一 helper，本函数保持零 goals 域依赖。
    """
    try:
        if not goal_auto_attach_enabled(cfg_root):
            return None
        from src.web.routes.unified_inbox_workflow_routes import (
            workflows_disabled_reason_cfg,
        )
        if workflows_disabled_reason_cfg(cfg_root):
            return None
        if str(goal_autonomy or "") != "auto":
            return None
        chain_id = GOAL_CHAIN_REC.get(str(goal_template or ""), "")
        if not chain_id or store is None:
            return None
        chain = store.get_workflow_chain(chain_id)
        if not chain or not chain.get("enabled"):
            return None
        conv = str(conversation_id or "").strip()
        if not conv or store.has_running_chain(conv, chain_id):
            return None
        store.start_chain_execution(
            chain_id, conv,
            {"agent": "goal_auto_attach", "goal_id": str(goal_id or "")},
            schedule_first_step=True,
        )
        logger.info("[goal-auto-attach] 目标 %s 自动挂上推荐链 %s conv=%s",
                    goal_id, chain_id, conv)
        return {"chain_id": chain_id,
                "name": str(chain.get("name") or chain_id)}
    except Exception:
        logger.debug("maybe_auto_attach_chain failed", exc_info=True)
        return None


def _ensure_pack(store: Any, pack: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    imported: List[str] = []
    skipped: List[str] = []
    for chain in pack:
        cid = chain["chain_id"]
        try:
            if store.get_workflow_chain(cid):
                skipped.append(cid)
                continue
            store.upsert_workflow_chain({
                "chain_id": cid,
                "name": chain["name"],
                "steps": chain["steps"],
                "trigger_conditions": chain.get("trigger_conditions") or {},
                "enabled": chain.get("enabled", True),
                "on_reply": chain.get("on_reply") or "",
            })
            imported.append(cid)
        except Exception:
            skipped.append(cid)
    return {"imported": imported, "skipped": skipped}


def ensure_starter_chains(store: Any) -> Dict[str, List[str]]:
    """幂等导入销售种子链，返回 ``{"imported": [...], "skipped": [...]}``。

    判重按 chain_id：存在即跳过（不比对内容、不覆盖）——运营对种子链的任何修改
    （改名/改步骤/开自动触发）都不会被再次导入抹掉。单条失败不影响其余。
    """
    return _ensure_pack(store, STARTER_CHAINS)


def ensure_guide_chains(store: Any) -> Dict[str, List[str]]:
    """实施93：幂等导入引导转化链包（判重/不覆盖语义同 ensure_starter_chains）。"""
    return _ensure_pack(store, GUIDE_CHAINS)


# ── 实施92 P0-5：「成交引擎」一键预设 ────────────────────────────────────────
# 把「五个默认关的开关要逐个找到并打开」收敛成一个运营决策：
#   开 = journey.enabled overlay 置 true + 两条种子链接上 stage_enter 触发
#       （报价→跟单、成交→关怀）+ 唤回链 on_reply 补 complete 语义 +
#       存量会话阶段一次性回填；
#   关 = journey.enabled 置 false + 摘掉两条链的 stage_enter（其余条件不动）。
# reply_yield（回复即让路）默认常开不在预设里翻动；auto_advance（AI 自动发）
# 刻意**不进预设**——那是另一个量级的授权，保持独立显式开关。


def deal_engine_status(store: Any, cfg_root: Any) -> Dict[str, Any]:
    """成交引擎当前状态（workflows 页卡片与预设端点共用口径）。绝不抛。
    实施93：接线表随 journey.flavor（sales/guide）取，响应回带 flavor。"""
    import json as _j
    from src.inbox.journey_stage import resolve_journey_cfg
    from src.inbox.workflow_auto_step import resolve_auto_advance_cfg
    from src.inbox.workflow_reply_yield import resolve_reply_yield_cfg
    j = resolve_journey_cfg(cfg_root)
    ry = resolve_reply_yield_cfg(cfg_root)
    aa = resolve_auto_advance_cfg(cfg_root)
    chains: List[Dict[str, Any]] = []
    all_wired = True
    all_auto = True
    for stage, cid in stage_chain_rec_for(j["flavor"]).items():
        chain = None
        try:
            chain = store.get_workflow_chain(cid) if store is not None else None
        except Exception:
            chain = None
        wired = False
        if chain and chain.get("enabled"):
            try:
                conds = _j.loads(chain.get("trigger_conditions") or "{}")
            except Exception:
                conds = {}
            wired = str(conds.get("stage_enter") or "") == stage
        exec_mode = str((chain or {}).get("exec_mode") or "remind")
        chains.append({
            "chain_id": cid,
            "name": str((chain or {}).get("name") or cid),
            "stage": stage,
            "exists": chain is not None,
            "wired": wired,
            "exec_mode": exec_mode,
        })
        all_wired = all_wired and wired
        all_auto = all_auto and (exec_mode == "auto")
    # 自动发送档（实施92b）：auto_advance 总闸 + 两条预设链 exec_mode=auto 才算开。
    # deliver 是下游 L2 autosend 的真发闸（本预设**不代改**——那是既有独立授权）；
    # 关着时自动稿会落人审队列而非直发，状态如实回带给前端提示。
    l2 = {}
    try:
        l2 = ((cfg_root or {}).get("inbox") or {}).get("l2_autosend") or {}
        if not isinstance(l2, dict):
            l2 = {}
    except Exception:
        l2 = {}
    auto_send_on = bool(aa["enabled"] and all_wired and all_auto)
    return {
        "journey_enabled": bool(j["enabled"]),
        "reply_yield_enabled": bool(ry["enabled"]),
        "flavor": j["flavor"],
        "chains": chains,
        "engine_on": bool(j["enabled"] and all_wired),
        "auto_send": {
            "on": auto_send_on,
            "auto_advance_enabled": bool(aa["enabled"]),
            "chains_auto": bool(all_auto),
            "deliver_wired": bool(l2.get("enabled", False)) and bool(
                l2.get("deliver", False)),
            "quiet_start": int(aa["quiet_start"]),
            "quiet_end": int(aa["quiet_end"]),
            "max_per_day": int(aa["max_per_day"]),
        },
    }


def apply_deal_engine(
    store: Any, config_manager: Any, enable: bool,
    auto_send: Any = None,
) -> Dict[str, Any]:
    """应用/撤销成交引擎预设。返回摘要（含每步结果，绝不抛）。

    幂等：重复开/关安全。链数据改动只动 ``stage_enter``、唤回链的空
    ``on_reply``、以及（显式传 ``auto_send`` 时）两条预设链的 ``exec_mode``；
    运营对链的其他修改（步骤/名称/enabled）原样保留。

    ``auto_send``（实施92b，「替坐席发消息自动推进」档）：
      - True  → overlay 开 ``inbox.workflows.auto_advance.enabled`` + 两条预设链
                exec_mode=auto——话术步到点自动拟稿投给 L2 autosend 管线
                （静默时段顺延 / 每 tick·每日预算 / 会话须全自动档 / 生成失败
                降级提醒，四重闸全在既有 workflow_auto_step 内）；
      - False → 反向（auto_advance 关 + exec_mode=remind）；
      - None  → 不碰（保持运营现状；主开关按钮只传 enable 时走这档）。
    ``enable=False`` 时强制视同 ``auto_send=False``——关引擎必须回到最安全档，
    不许留一个「引擎关了链还在自动发」的半开态。
    """
    import json as _j
    out: Dict[str, Any] = {"ok": True, "enable": bool(enable),
                           "chains": [], "backfilled": 0}
    if store is None:
        return {"ok": False, "error": "store_unavailable"}
    if not enable:
        auto_send = False
    target_exec: Any = None
    if auto_send is True:
        target_exec = "auto"
    elif auto_send is False:
        target_exec = "remind"
    from src.inbox.journey_stage import resolve_journey_cfg
    flavor = resolve_journey_cfg(
        getattr(config_manager, "config", None) or {})["flavor"]
    rec = stage_chain_rec_for(flavor)
    if enable:
        try:
            if flavor == "guide":
                ensure_guide_chains(store)
            else:
                ensure_starter_chains(store)
        except Exception:
            logger.debug("deal_engine: ensure seeds failed", exc_info=True)
    # 0) 换风味互斥：另一风味接线表上的链若还挂着 stage_enter，一并摘除
    #（否则 sales/guide 两套链对同一阶段同时开火＝双打扰）
    other = (STAGE_CHAIN_REC if flavor == "guide" else GUIDE_STAGE_CHAIN_REC)
    for _stage, cid in other.items():
        if cid in rec.values():
            continue
        try:
            chain = store.get_workflow_chain(cid)
            if not chain:
                continue
            conds = _j.loads(chain.get("trigger_conditions") or "{}")
            if not isinstance(conds, dict) or "stage_enter" not in conds:
                continue
            conds.pop("stage_enter", None)
            store.upsert_workflow_chain({
                "chain_id": cid,
                "name": chain.get("name") or cid,
                "steps": _j.loads(chain.get("steps_json") or "[]"),
                "trigger_conditions": conds,
                "enabled": bool(chain.get("enabled", True)),
                "exec_mode": chain.get("exec_mode") or "remind",
                "on_reply": chain.get("on_reply") or "",
                "created_at": chain.get("created_at"),
            })
        except Exception:
            logger.debug("deal_engine: unwire other-flavor chain failed",
                         exc_info=True)
    # 1) 当前风味推荐链接/摘 stage_enter（+ 显式 auto_send 时切执行档位）
    for stage, cid in rec.items():
        try:
            chain = store.get_workflow_chain(cid)
            if not chain:
                out["chains"].append({"chain_id": cid, "ok": False,
                                      "error": "missing"})
                continue
            try:
                conds = _j.loads(chain.get("trigger_conditions") or "{}")
            except Exception:
                conds = {}
            if not isinstance(conds, dict):
                conds = {}
            if enable:
                conds["stage_enter"] = stage
            else:
                conds.pop("stage_enter", None)
            try:
                steps = _j.loads(chain.get("steps_json") or "[]")
            except Exception:
                steps = []
            store.upsert_workflow_chain({
                "chain_id": cid,
                "name": chain.get("name") or cid,
                "steps": steps,
                "trigger_conditions": conds,
                "enabled": bool(chain.get("enabled", True)),
                "exec_mode": (target_exec if target_exec is not None
                              else (chain.get("exec_mode") or "remind")),
                "on_reply": chain.get("on_reply") or "",
                "created_at": chain.get("created_at"),
            })
            out["chains"].append({"chain_id": cid, "ok": True,
                                  "stage": stage if enable else ""})
        except Exception as exc:
            out["chains"].append({"chain_id": cid, "ok": False,
                                  "error": str(exc)[:80]})
    # 2) 唤回链 on_reply 语义补齐（仅空值时补，运营显式配置不覆盖）
    if enable:
        try:
            re_chain = store.get_workflow_chain("starter_reactivate_3step")
            if re_chain and not str(re_chain.get("on_reply") or ""):
                try:
                    steps = _j.loads(re_chain.get("steps_json") or "[]")
                except Exception:
                    steps = []
                try:
                    conds = _j.loads(re_chain.get("trigger_conditions") or "{}")
                except Exception:
                    conds = {}
                store.upsert_workflow_chain({
                    "chain_id": "starter_reactivate_3step",
                    "name": re_chain.get("name") or "",
                    "steps": steps,
                    "trigger_conditions": conds,
                    "enabled": bool(re_chain.get("enabled", True)),
                    "exec_mode": re_chain.get("exec_mode") or "remind",
                    "on_reply": "complete",
                    "created_at": re_chain.get("created_at"),
                })
        except Exception:
            logger.debug("deal_engine: reactivate on_reply fix failed",
                         exc_info=True)
    # 3) overlay 开关（保注释写入；config_manager 缺席如实报告不装成功）
    flag_ok, flag_msg = False, "config_manager_unavailable"
    try:
        if config_manager is not None and hasattr(config_manager, "set_overlay_flag"):
            flag_ok, flag_msg = config_manager.set_overlay_flag(
                "inbox.workflows.journey.enabled", bool(enable))
    except Exception as exc:
        flag_ok, flag_msg = False, str(exc)[:80]
    out["flag_ok"] = bool(flag_ok)
    out["flag_msg"] = str(flag_msg or "")
    if not flag_ok:
        out["ok"] = False
    # 3b) 自动发送总闸（仅显式请求时写；enable=False 已在上方强制 False）
    if auto_send is not None:
        aa_ok, aa_msg = False, "config_manager_unavailable"
        try:
            if (config_manager is not None
                    and hasattr(config_manager, "set_overlay_flag")):
                aa_ok, aa_msg = config_manager.set_overlay_flag(
                    "inbox.workflows.auto_advance.enabled", bool(auto_send))
        except Exception as exc:
            aa_ok, aa_msg = False, str(exc)[:80]
        out["auto_send"] = bool(auto_send)
        out["auto_flag_ok"] = bool(aa_ok)
        out["auto_flag_msg"] = str(aa_msg or "")
        if not aa_ok:
            out["ok"] = False
    # 4) 开启时对存量会话做一次阶段回填（拿当下配置直接跑，不等 overlay 热重载）
    if enable and flag_ok:
        try:
            from src.inbox.journey_stage import backfill_stages
            cfg_now = dict(getattr(config_manager, "config", None) or {})
            out["backfilled"] = backfill_stages(store, cfg_now, limit=300)
        except Exception:
            logger.debug("deal_engine: backfill failed", exc_info=True)
    return out
