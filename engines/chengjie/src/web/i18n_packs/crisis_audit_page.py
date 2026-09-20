# -*- coding: utf-8 -*-
"""客户安全预警页（原「危机审计」）增量词条。

存量 ca_* 键仍在 web_i18n 单体（词条单源规则：新增进 pack，存量待迁）。

2026-08-18 P1：「安全体检」空态三键（ca_js010/011/012）。
2026-09-05 #185（J-8 A）：空态分三档 + 人话化。
- ``audit_off``：留痕没开 → 红色横幅 + 一键开启。**绝不**再显示「这是好消息」——
  没开留痕时 0 条不是「没事」，是「看不见」。
- ``audit_on_empty``：留痕已开，近 N 天无事件 → 才是真正的好消息。
- 术语：R4/R6/R8 → 关注 / 已兜底 / 已升级（带一句解释）；配置键不再出现在页面上；
  「用户 ID 前缀」→「按客户搜索」。
"""

ZH = {
    # ── 2026-08-18 P1 空态（保留：audit_on_empty 档仍用 011/012）──
    "ca_js010": "没有待处理的危机事件——这是好消息",
    "ca_js011": "查看已处理历史",
    "ca_js012": "危机识别守卫在每条进站消息上运行；一旦命中会在这里留痕，并点亮侧栏红色徽标。",
    # ── 2026-09-05 #185 人话化：页名 / 副标题 / 筛选 / 表头 ──
    "ca_s020": "客户安全预警",
    "ca_s021": "AI 会在每条进站消息上识别自伤、绝望等危机信号；命中后记录在这里，需要时叫人接手。",
    "ca_s022": "按客户搜索",
    "ca_s023": "客户 ID（输入开头几位即可）",
    "ca_s024": "状态说明",
    "ca_s025": "关注",
    "ca_s026": "AI 识别到危机信号（严重 / 偏高）",
    "ca_s027": "已兜底",
    "ca_s028": "AI 原本的回复触到安全红线，已替换为安全回复",
    "ca_s029": "已升级",
    "ca_s030": "已叫人：工作台红色「需人工」徽标 + 会话置顶 + 案例跟进",
    "ca_s031": "状态",
    "ca_s032": "风险",
    "ca_s033": "连续命中",
    # ── #185 三档空态 ──
    "ca_js020": "危机留痕未开启",
    "ca_js021": "识别和安全回复仍在生效，但事件不会被记录、也不会通知任何人。开启后：命中即记录在这里，严重情况点亮工作台红色徽标并置顶会话。",
    "ca_js022": "一键开启留痕与人工升级",
    "ca_js023": "已开启，正在刷新…",
    "ca_js024": "开启失败",
    "ca_js025": "留痕已开启：近 {n} 天无危机事件",
    "ca_js026": "人工升级未开启：严重事件只会记录，不会叫人。",
    "ca_js027": "开启人工升级",
    "ca_js028": "留痕状态未知（后端未就绪）",
    # ── #185 表格内人话（等级 / 类别）──
    "ca_js030": "严重",
    "ca_js031": "偏高",
    "ca_js032": "自伤风险",
    "ca_js033": "绝望情绪",
    "ca_js034": "语音痛苦信号",
}

EN = {
    "ca_js010": "No unhandled crisis events — that's good news",
    "ca_js011": "Show handled history",
    "ca_js012": "The crisis-detection guard runs on every inbound message; any hit is recorded here and lights up the red sidebar badge.",
    "ca_s020": "Customer Safety Alerts",
    "ca_s021": "The AI screens every inbound message for self-harm and despair signals; hits are recorded here and a human is called in when needed.",
    "ca_s022": "Search by customer",
    "ca_s023": "Customer ID (first few characters are enough)",
    "ca_s024": "What the statuses mean",
    "ca_s025": "Concern",
    "ca_s026": "The AI detected a crisis signal (severe / elevated)",
    "ca_s027": "Safety net applied",
    "ca_s028": "The AI's original reply crossed a safety red line and was replaced with a safe reply",
    "ca_s029": "Escalated",
    "ca_s030": "A human was called: red \"needs human\" badge in the workbench + conversation pinned + case opened",
    "ca_s031": "Status",
    "ca_s032": "Risk",
    "ca_s033": "Hits in a row",
    "ca_js020": "Crisis logging is off",
    "ca_js021": "Detection and safe replies still work, but events are not recorded and nobody is notified. Once enabled, every hit is logged here and severe cases light up the red workbench badge and pin the conversation.",
    "ca_js022": "Enable logging & human escalation",
    "ca_js023": "Enabled — refreshing…",
    "ca_js024": "Failed to enable",
    "ca_js025": "Logging is on: no crisis events in the last {n} days",
    "ca_js026": "Human escalation is off: severe events are only recorded, nobody is called.",
    "ca_js027": "Enable human escalation",
    "ca_js028": "Logging status unknown (backend not ready)",
    "ca_js030": "Severe",
    "ca_js031": "Elevated",
    "ca_js032": "Self-harm risk",
    "ca_js033": "Despair",
    "ca_js034": "Distress in voice",
}
