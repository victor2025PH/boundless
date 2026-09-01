# -*- coding: utf-8 -*-
"""小智「页面控件锚点」注册表（实施88 P0，2026-08-30）——DOM 动作总线的白名单。

定位：让「替我做」能替用户**点开/填入/带看**自家页面上的控件。与动作注册表
（actions.py，服务端写面）互补——这里登记的是**前端 DOM 手势**的合法目标。

设计铁律（改前先读）：
1. **只收「揭示类」目标**：click 只许点「打开抽屉/表单/向导」这类零副作用
   控件；fill 只许填「搜索/筛选框」（纯过滤，不落库）；show 是纯聚光灯。
   **发送/提交/删除类按钮永远不进本表**——真正的状态变更必须走
   actions.py 的 L2 确认卡链（服务端校验+审计+撤销），DOM 总线不开第二条
   写通道。门禁 ``tests/test_assistant_ui_anchors.py`` 用「锚点清单钉死 +
   手势/等级白名单」双保险。
2. **选择器是服务端策展的**：LLM 只能提名锚点 id，plan_action 按本表换出
   sel——前端永远不执行 LLM 生出的选择器（与「LLM 输出永不被直接执行」
   同一条链的延伸）。信任模型与 help_kb 条目的 anchor 字段（带我去聚光灯）
   一致。
3. **热区模板零改动**：unified_inbox.html 等多线活跃热区只用**既有稳定
   选择器**（id/属性），不新增 data-anchor 属性；非热区页（knowledge 等）
   优先补 data-anchor（教学模式一级精确匹配顺带受益）。每条锚点在门禁里有
   「模板证据」断言——模板重构把控件改没了，门禁先红，不留幽灵锚点。
4. **页面必须在 CORE_NAV_PATHS 内**：规划器会在用户不在目标页时自动插一步
   goto_page（agent_planner._maybe_insert_home_nav），nav 白名单回落集必须
   覆盖到，否则跨页任务断链。

字段：page（所在页，goto 落点）/ sel（CSS 选择器）/ gesture（click|fill|show）
/ level（当前全部 L1——与 goto_page 同信任级，无确认卡）/ 双语 label+desc。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 手势白名单：click=点开揭示类控件；fill=填入搜索/筛选框（带 text）；
# show=滚动到目标并聚光（零交互）。
GESTURES = ("click", "fill", "show")

FILL_TEXT_MAXLEN = 80

UI_ANCHORS: Dict[str, Dict[str, Any]] = {
    "open_account_drawer": {
        "page": "/workspace",
        "sel": "#acct-nav-btn",
        "gesture": "click",
        "level": "L1",
        "label_zh": "打开「账号与平台管理」抽屉",
        "label_en": "Open the accounts & platforms drawer",
        "desc_zh": "在坐席工作台点开账号抽屉（接入/管理各平台账号的入口）",
        "desc_en": "Opens the account drawer in the workspace (connect / "
                   "manage platform accounts)",
    },
    "inbox_search": {
        "page": "/workspace",
        "sel": "#search-input",
        "gesture": "fill",
        "level": "L1",
        "label_zh": "在会话列表里搜索联系人",
        "label_en": "Search contacts in the conversation list",
        "desc_zh": "把搜索词填进工作台的联系人搜索框（纯过滤，不改数据）",
        "desc_en": "Types the query into the workspace contact search box "
                   "(pure filter, no writes)",
    },
    "kb_new_entry": {
        "page": "/knowledge",
        "sel": '[data-anchor="kb-new-entry"]',
        "gesture": "click",
        "level": "L1",
        "label_zh": "打开「新建知识条目」表单",
        "label_en": "Open the new knowledge-entry form",
        "desc_zh": "在知识库页点开新建条目抽屉（内容仍由用户填写并保存）",
        "desc_en": "Opens the new-entry drawer on the knowledge page (the "
                   "user still fills and saves it)",
    },
    "kb_search": {
        "page": "/knowledge",
        "sel": "#entry-search",
        "gesture": "fill",
        "level": "L1",
        "label_zh": "搜索知识库条目",
        "label_en": "Search knowledge-base entries",
        "desc_zh": "把关键词填进知识库搜索框（标题/触发词过滤）",
        "desc_en": "Types the keyword into the knowledge search box "
                   "(title / trigger filter)",
    },
    "personas_new": {
        "page": "/personas",
        "sel": '[onclick^="openCreateFlow"]',
        "gesture": "click",
        "level": "L1",
        "label_zh": "打开「新建人设」向导",
        "label_en": "Open the new-persona wizard",
        "desc_zh": "在人设工作室点开新建人设流程（后续步骤仍由用户确认）",
        "desc_en": "Opens the create-persona flow in Persona Studio (later "
                   "steps stay with the user)",
    },
    "show_reply_budget_guard": {
        "page": "/reply-settings",
        "sel": 'h3[data-help="rps_peer_budget"]',
        "gesture": "show",
        "level": "L1",
        "label_zh": "带看「单会话额度保险丝」设置卡",
        "label_en": "Show the per-chat reply budget card",
        "desc_zh": "滚动到回复设置页的额度保险丝卡并聚光（只看不动）",
        "desc_en": "Scrolls to and spotlights the reply-budget card on the "
                   "reply-settings page (read-only)",
    },
}


def get_anchor(anchor_id: str) -> Optional[Dict[str, Any]]:
    return UI_ANCHORS.get(str(anchor_id or "").strip())


def anchor_ids() -> List[str]:
    return list(UI_ANCHORS.keys())


def prompt_lines(lang: str = "zh") -> List[str]:
    """规划器 prompt 的控件白名单块（每行：id (页 · 手势) 标签）。"""
    zh = not str(lang or "").lower().startswith("en")
    out: List[str] = []
    for aid, a in UI_ANCHORS.items():
        label = a["label_zh" if zh else "label_en"]
        out.append(f"- {aid} ({a['page']} · {a['gesture']}) {label}")
    return out
