# -*- coding: utf-8 -*-
"""知识库管理页增量词条（2026-08-04 白天模式可读性专项随批）。

存量 kb_s* / kb_js_* 键仍在 web_i18n.py 单体（未迁移域）；本 pack 只收
本批新增键，前缀 kb2_ 与单体命名空间隔离（防 pack×单体同 key 门禁红）。
"""

ZH = {
    "kb2_disabled": "已停用",
    # ── 空态三件套（2026-08-22 P5：真空 vs 筛选无结果分开说话）──
    "kb2_empty_lead": "还没有知识条目——播种一个起步包，AI 立刻能答产品常见问题；也可以手动新建。",
    "kb2_empty_seed": "一键播种起步包",
    "kb2_nomatch": "没有匹配当前筛选的条目",
    "kb2_clearfilter": "清除筛选",
    "kb2_health_chip": "健康分",
    "kb2_health_tip": "知识库健康分（点击查看诊断建议与修复入口）",
    "kb2_sandbox_try": "沙盒试一句",
    "kb2_sandbox_try_tip": "保存前先到沙盒用触发词试一次命中效果（不发消息）",
    "kb2_entry_gone": "这条知识已被删除或改名，无法定位",
}

EN = {
    "kb2_disabled": "Disabled",
    "kb2_empty_lead": "No knowledge entries yet — seed a starter pack so AI can answer common product questions right away, or add entries manually.",
    "kb2_empty_seed": "Seed a starter pack",
    "kb2_nomatch": "No entries match the current filters",
    "kb2_clearfilter": "Clear filters",
    "kb2_health_chip": "Health",
    "kb2_health_tip": "Knowledge base health score (click for diagnosis and fixes)",
    "kb2_sandbox_try": "Try in sandbox",
    "kb2_sandbox_try_tip": "Dry-run the triggers in the sandbox before saving (no message is sent)",
    "kb2_entry_gone": "This entry was deleted or renamed; cannot locate it",
}
