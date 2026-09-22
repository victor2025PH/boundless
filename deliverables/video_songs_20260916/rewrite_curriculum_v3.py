#!/usr/bin/env python3
"""按五视角方案重写 E1–E11 footage：完整功能闭环，BOUNDLESS 不再默认舞台。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
p = ROOT / "curriculum.json"
cur = json.loads(p.read_text(encoding="utf-8"))

cur["_doc"] = (
    "智聊 ChatX 教学。规则：①每集完整功能闭环（进入→操作→结果）②主舞台按功能页/"
    "真实控件，BOUNDLESS 仅 E3 英文互译可选③指针大红圈④不露脸。"
)

FOOTAGE = {
    "E1": [
        {
            "clip": "a_overview",
            "desc": "多平台会话列表 + 账号坞（不绑死 BOUNDLESS）",
            "steps": [
                ["caption", "【统一收件箱】左边是所有平台的真实会话", 2400],
                ["point_css", "#plat-icons, #nav-rail, .nav-plat", "左侧平台轨：TG / LINE / WA…"],
                ["wait", 1400],
                ["point_css", ".conv-item", "每一行＝一个真实客户"],
                ["wait", 1200],
                ["point_css", "#acct-dock, .adk-item", "顶栏【账号坞】切换工作号"],
                ["wait", 1600],
            ],
        },
        {
            "clip": "b_filters",
            "desc": "筛选闭环",
            "steps": [
                ["caption", "【筛选】先看该看的", 2000],
                ["click_css_text", ".ftab", "群组", "点【群组】"],
                ["wait", 1400],
                ["click_css_text", ".ftab", "私聊", "点【私聊】"],
                ["wait", 1400],
                ["click_css_text", ".ftab", "未读", "点【未读】"],
                ["wait", 1400],
                ["click_css_text", ".ftab", "全部", "回到【全部】"],
                ["wait", 1000],
            ],
        },
        {
            "clip": "c_threads",
            "desc": "打开两个不同真实会话证明多平台",
            "steps": [
                ["caption", "打开不同平台的真实会话——不是假数据", 2200],
                ["open_nth_chat", 0, "打开列表第 1 个真实会话"],
                ["wait", 2200],
                ["scroll_messages", -200],
                ["wait", 1400],
                ["open_nth_chat", 2, "再打开另一个真实会话"],
                ["wait", 2400],
                ["caption", "一个收件箱，接住多个平台", 2200],
                ["wait", 1600],
            ],
        },
        {
            "clip": "d_copilot",
            "desc": "助手三页签导览",
            "steps": [
                ["ensure_assist", "打开右侧【业务助手】"],
                ["wait", 800],
                ["cp_tab", "客户关系", "【客户关系】"],
                ["wait", 1600],
                ["cp_tab", "工具箱", "【工具箱】"],
                ["wait", 1600],
                ["cp_tab", "回复台", "【回复台】"],
                ["wait", 1600],
            ],
        },
    ],
    "E2": [
        {
            "clip": "a_drawer",
            "desc": "账号管理抽屉完整路径",
            "steps": [
                ["caption", "【渠道接入】从账号管理进入", 2200],
                ["click_css", "#acct-nav-btn", "点【账号管理】"],
                ["wait", 2000],
                ["point_css", "#account-drawer, .drawer-body, .acct-plat-header", "各平台接入状态"],
                ["wait", 1800],
                ["point_css", ".acct-add-btn", "【＋新增账号】扫码/授权"],
                ["wait", 1200],
                ["click_css", ".acct-add-btn", "打开新增"],
                ["wait", 3500],
                ["caption", "扫码完成后账号出现在顶栏坞", 2400],
                ["wait", 1600],
                ["press", "Escape"],
                ["wait", 800],
            ],
        },
        {
            "clip": "b_health",
            "desc": "账号坞健康灯",
            "steps": [
                ["point_css", ".adk-item", "已接入账号一号一格"],
                ["wait", 1400],
                ["point_css", ".adk-st.on, .adk-st", "绿灯＝在线"],
                ["wait", 2000],
                ["point_css", "#acct-dock", "四个平台都挂在这一条坞上"],
                ["wait", 2000],
            ],
        },
    ],
    "E3": [
        {
            "clip": "a_open",
            "desc": "英文会话 + 译文顶栏",
            "steps": [
                ["open_chat", "BOUNDLESS", "打开英文客户【BOUNDLESS】（本集专用）"],
                ["wait", 1600],
                ["caption", "客户原文是英文——先打开显示译文", 2400],
                ["point_css", "#xl-show-toggle, #xl-topbar", "顶栏【显示译文】"],
                ["wait", 1200],
                ["click_css", "#xl-show-toggle", "勾选显示译文"],
                ["wait", 2800],
            ],
        },
        {
            "clip": "b_translate",
            "desc": "翻译按钮与弹出层",
            "steps": [
                ["open_chat", "BOUNDLESS"],
                ["wait", 1000],
                ["point_css", "#xlate-toggle-btn", "回复台旁【翻译】"],
                ["wait", 1000],
                ["click_css", "#xlate-toggle-btn", "打开翻译菜单"],
                ["wait", 2200],
                ["caption", "入站可读中文；出站自动成客户语言", 2600],
                ["point_css", "#reply-ta", "用中文写，发出去是对方母语"],
                ["wait", 2000],
            ],
        },
        {
            "clip": "c_multilang",
            "desc": "多语演示会话",
            "steps": [
                ["open_chat", "ZH_DEMO", "切到【智聊支持】看多语句子"],
                ["wait", 1800],
                ["scroll_messages", -240],
                ["wait", 2000],
                ["caption", "粤语 / 日文 / 韩文也能进同一收件箱", 2400],
                ["wait", 2000],
            ],
        },
    ],
    "E4": [
        {
            "clip": "a_modes",
            "desc": "档位选择闭环",
            "steps": [
                ["open_nth_chat", 1, "打开一个真实会话看档位"],
                ["wait", 1400],
                ["caption", "本会话档位：手动 / AI草稿·我审 / 全自动", 2400],
                ["point_css", "#mode-select", "顶栏【档位】胶囊"],
                ["wait", 1400],
                ["click_css", "#mode-select", "展开档位"],
                ["wait", 2200],
                ["press", "Escape"],
                ["wait", 600],
            ],
        },
        {
            "clip": "b_draft",
            "desc": "底部 AI回复（产品主入口）→ 草稿结果",
            "steps": [
                ["open_nth_chat", 1, "同一会话准备拟稿"],
                ["wait", 1200],
                ["caption", "点【AI回复】生成人设化草稿", 2400],
                ["point_css", "#ai-reply-btn", "回复台工具条【AI回复】"],
                ["wait", 1400],
                ["click_css", "#ai-reply-btn", "生成 AI 草稿"],
                ["wait", 5000],
                ["point_css", "#draft-pick, .draft-pick, [data-st], #cdraft-bar, .dpick", "草稿出来后可勾选发送或去工坊"],
                ["wait", 2400],
                ["point_css", "#tko-btn", "随时【接管】变人工"],
                ["wait", 2000],
            ],
        },
    ],
    "E5": [
        {
            "clip": "a_studio",
            "desc": "人设工作室主舞台",
            "steps": [
                ["goto", "/personas"],
                ["caption", "【人设工作室】语气、边界、背景一次配好", 2400],
                ["wait", 2000],
                ["click_css", "#tabn-profiles", "打开【人设池】"],
                ["wait", 2200],
                ["point_text", "JSON", "可从 JSON 导入字段"],
                ["wait", 1800],
                ["scroll_messages", 350],
                ["wait", 1800],
            ],
        },
        {
            "clip": "b_bind",
            "desc": "回到会话做绑定（工具箱）",
            "steps": [
                ["goto", "/workspace"],
                ["open_nth_chat", 0, "选一个会话准备绑定人设"],
                ["wait", 1200],
                ["ensure_assist"],
                ["cp_tab", "工具箱", "人设绑定在【工具箱】"],
                ["wait", 1200],
                ["expand_card", "persona", "展开【人设绑定】"],
                ["wait", 2500],
                ["caption", "试聊通过再绑到会话", 2200],
                ["wait", 1800],
            ],
        },
    ],
    "E6": [
        {
            "clip": "a_kb",
            "desc": "知识库管理页",
            "steps": [
                ["goto", "/knowledge"],
                ["caption", "【知识库】条目有标题、触发词、示例回复", 2600],
                ["wait", 2200],
                ["point_text", "新建条目", "【新建条目】"],
                ["wait", 1600],
                ["point_css", ".entry-card, .kb-tab", "已有知识条目可检索编辑"],
                ["wait", 2200],
            ],
        },
        {
            "clip": "b_slash",
            "desc": "会话里用 / 引用知识",
            "steps": [
                ["goto", "/workspace"],
                ["open_nth_chat", 0],
                ["wait", 1200],
                ["caption", "回复台输入 / 可插入知识与模板", 2400],
                ["click_css", "#reply-ta", "聚焦回复台"],
                ["wait", 600],
                ["type", "#reply-ta", "/"],
                ["wait", 2500],
                ["point_css", "#slash-panel, .slash-panel", "【/ 指令】知识库与模板"],
                ["wait", 2200],
                ["press", "Escape"],
                ["wait", 800],
            ],
        },
    ],
    "E7": [
        {
            "clip": "a_voice",
            "desc": "工具箱语音卡完整展开",
            "steps": [
                ["open_nth_chat", 0, "选会话后才能用语音工具"],
                ["wait", 1000],
                ["ensure_assist"],
                ["cp_tab", "工具箱", "语音在【工具箱】"],
                ["wait", 1200],
                ["expand_card", "voice", "展开【语音克隆 / 发送】"],
                ["wait", 2000],
                ["point_text", "生成语音", "【生成语音】试听"],
                ["wait", 1800],
                ["caption", "试听通过＝所听即所发", 2400],
                ["wait", 2000],
            ],
        },
    ],
    "E8": [
        {
            "clip": "a_goals",
            "desc": "工作目标 / 今日拍",
            "steps": [
                ["open_nth_chat", 0],
                ["wait", 1000],
                ["ensure_assist"],
                ["cp_tab", "客户关系", "打开【客户关系】"],
                ["wait", 1600],
                ["expand_card", "goal", "展开【工作目标】"],
                ["wait", 2200],
                ["point_text", "今日", "【今日拍】十秒反馈"],
                ["wait", 1800],
                ["point_css", "[data-cp-card=goal], #ws-cp-goal", "下一步英雄卡在这里"],
                ["wait", 2200],
            ],
        },
    ],
    "E9": [
        {
            "clip": "a_care",
            "desc": "关怀日程独立页",
            "steps": [
                ["goto", "/care-schedule"],
                ["caption", "【关怀日程】主动跟进，也守安静时段", 2600],
                ["wait", 2800],
                ["point_text", "关怀", "关怀规则与日程"],
                ["wait", 2000],
            ],
        },
        {
            "clip": "b_memory",
            "desc": "AI 记忆页",
            "steps": [
                ["goto", "/episodic-memory"],
                ["caption", "【AI 记忆】记得偏好，回答更贴人", 2600],
                ["wait", 3000],
                ["scroll_messages", 300],
                ["wait", 2000],
            ],
        },
    ],
    "E10": [
        {
            "clip": "a_ball",
            "desc": "小智球打开面板",
            "steps": [
                ["caption", "右下角【小智】：问怎么用、带我去", 2400],
                ["point_css", ".asb-ball", "得点【小智球】"],
                ["wait", 1000],
                ["click_css", ".asb-ball", "打开小智面板"],
                ["wait", 2800],
                ["point_css", ".asb-panel, .asb-wrap", "在面板里提问或切教学"],
                ["wait", 2500],
            ],
        },
    ],
    "E11": [
        {
            "clip": "a_risk",
            "desc": "风险会话主镜（不发教学水贴）",
            "steps": [
                ["caption", "【风险标签】该转人就不乱答", 2400],
                ["point_css", ".conv-tag-chip", "列表上的风险标记"],
                ["wait", 1400],
                ["open_chat", "风险会话", "打开真实风险会话"],
                ["wait", 2800],
                ["point_css", "#cs-band, #ai-diag-btn, #mode-select", "看 AI 是否让位/转人工"],
                ["wait", 2200],
            ],
        },
        {
            "clip": "b_settings",
            "desc": "风控设置页",
            "steps": [
                ["goto", "/reply-settings"],
                ["caption", "设置里可配【自动化与风控】", 2400],
                ["wait", 2200],
                ["point_text", "风控", "风控分级与策略"],
                ["wait", 1800],
                ["click_text", "风控", "进入风控相关区"],
                ["wait", 2800],
            ],
        },
    ],
}

for ep in cur["episodes"]:
    eid = ep.get("id")
    if eid in FOOTAGE:
        ep["footage_actions"] = FOOTAGE[eid]
        ep["status"] = "footage_rewritten_v3"

p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
print("rewrote", list(FOOTAGE))
