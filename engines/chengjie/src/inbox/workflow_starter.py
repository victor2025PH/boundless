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


def ensure_starter_chains(store: Any) -> Dict[str, List[str]]:
    """幂等导入种子链，返回 ``{"imported": [...], "skipped": [...]}``。

    判重按 chain_id：存在即跳过（不比对内容、不覆盖）——运营对种子链的任何修改
    （改名/改步骤/开自动触发）都不会被再次导入抹掉。单条失败不影响其余。
    """
    imported: List[str] = []
    skipped: List[str] = []
    for chain in STARTER_CHAINS:
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
            })
            imported.append(cid)
        except Exception:
            skipped.append(cid)
    return {"imported": imported, "skipped": skipped}
