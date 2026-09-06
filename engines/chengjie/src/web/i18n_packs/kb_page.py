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
    # ── 条目来源隔离（2026-09-05 J-9 #184：厂商预置产品说明 vs 用户知识）──
    "kb2_sat_none": "暂无反馈",
    "kb2_src_tip": "按条目来源筛选",
    "kb2_src_all": "全部来源",
    "kb2_src_user": "我建的",
    "kb2_src_import": "批量导入",
    "kb2_src_system": "系统话术/示例",
    "kb2_src_vendor": "系统预置·厂商产品",
    # L-4 F（#201）：学习队列审核通过入库的条目单列一档，可筛可回滚
    "kb2_src_learner": "学习队列学来的",
    "kb2_src_vendor_badge": "厂商预置",
    "kb2_src_vendor_tip": "随安装包预置的厂商自家产品说明，不是你的知识；桌面模式下对客回复不会用到",
    "kb2_vendor_lead": "检测到",
    "kb2_vendor_lead2": "条厂商随包预置的产品说明（不是你的知识）。",
    "kb2_vendor_excluded": "对客回复已排除它们。",
    "kb2_vendor_not_excluded": "当前部署对客回复仍会用到它们。",
    "kb2_vendor_view": "查看",
    "kb2_vendor_purge": "一键清空",
    "kb2_vendor_later": "稍后",
    "kb2_vendor_none": "没有厂商预置条目",
    "kb2_vendor_purge_confirm": "确定删除全部 {n} 条厂商预置条目？只删 source=厂商预置 的条目，你自建/导入的知识不受影响。",
    "kb2_vendor_purged": "已清空 {n} 条厂商预置条目",
    "kb2_vendor_purge_fail": "清空失败，请稍后重试",
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
    # Source isolation (2026-09-05 J-9 #184)
    "kb2_sat_none": "No feedback yet",
    "kb2_src_tip": "Filter by entry source",
    "kb2_src_all": "All sources",
    "kb2_src_user": "Created by me",
    "kb2_src_import": "Bulk import",
    "kb2_src_system": "System scripts / examples",
    "kb2_src_vendor": "Preset · vendor products",
    "kb2_src_learner": "Learned via learning queue",
    "kb2_src_vendor_badge": "Vendor preset",
    "kb2_src_vendor_tip": "Vendor product notes bundled with the installer — not your knowledge; excluded from customer replies in desktop mode",
    "kb2_vendor_lead": "Found",
    "kb2_vendor_lead2": "vendor product entries bundled with the installer (not your knowledge).",
    "kb2_vendor_excluded": "They are already excluded from customer replies.",
    "kb2_vendor_not_excluded": "This deployment still uses them in customer replies.",
    "kb2_vendor_view": "View",
    "kb2_vendor_purge": "Clear all",
    "kb2_vendor_later": "Later",
    "kb2_vendor_none": "No vendor preset entries",
    "kb2_vendor_purge_confirm": "Delete all {n} vendor preset entries? Only source=vendor entries are removed; your own / imported knowledge is untouched.",
    "kb2_vendor_purged": "Cleared {n} vendor preset entries",
    "kb2_vendor_purge_fail": "Clear failed, please retry later",
}
