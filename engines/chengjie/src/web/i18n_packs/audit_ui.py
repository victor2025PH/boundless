# -*- coding: utf-8 -*-
"""审计展示层 UI 词条（首页「最近操作」卡 + /audit 增强，2026-08-05）。

键族：
- ``aud_ui_*``  模板静态词条（分组标题/对象前缀/徽章/摘要格式串）
- ``aud_js_*``  前端 JS 相对时间（经 window.T/Tf 消费）
- ``au_fam_*``  /audit 业务视角快捷筛选 chips
- ``au_retention_note`` /audit 保留期标注（{days}/{rows} 占位）

动作名人话标签在同目录 ``audit_actions.py``（aud_act_* 键族），勿混放。
格式串占位符用 ``{n}``/``{d}``/``{days}``/``{rows}``——模板侧 .replace / JS 侧
window.Tf 消费；按 i18n 施工约定，占位符名禁用 request/key/default。
"""

ZH = {
    # ── 首页「最近操作」卡 ──────────────────────────────────────────
    "aud_ui_today_fmt": "今日 {n} 次操作 · 含 {d} 次删除",
    "aud_ui_today_fmt0": "今日 {n} 次操作",
    "aud_ui_grp_today": "今天",
    "aud_ui_grp_yesterday": "昨天",
    "aud_ui_tgt_memory": "记忆",
    "aud_ui_tgt_keyword": "关键词",
    "aud_ui_tgt_identity": "身份",
    "aud_ui_rollback": "可回滚",
    "aud_ui_rollback_tip": "该操作留有配置快照，点击对比当前配置并可回滚",
    # ── 相对时间（JS） ─────────────────────────────────────────────
    "aud_js_now": "刚刚",
    "aud_js_min": "{n} 分钟前",
    "aud_js_hour": "{n} 小时前",
    # ── /audit 业务视角快捷筛选 ────────────────────────────────────
    "au_fam_danger": "⚠️ 高危操作",
    "au_fam_memory": "🧠 记忆操作",
    "au_fam_kb": "📚 知识库",
    "au_fam_persona": "🎭 人设",
    "au_fam_rpa": "🤖 RPA·渠道",
    # ── /audit 保留期标注 ──────────────────────────────────────────
    "au_retention_note": "记录保留 {days} 天 · 上限 {rows} 条",
}

EN = {
    # ── Dashboard "Recent Operations" card ─────────────────────────
    "aud_ui_today_fmt": "{n} ops today · {d} destructive",
    "aud_ui_today_fmt0": "{n} ops today",
    "aud_ui_grp_today": "Today",
    "aud_ui_grp_yesterday": "Yesterday",
    "aud_ui_tgt_memory": "Memory",
    "aud_ui_tgt_keyword": "Keyword",
    "aud_ui_tgt_identity": "Identity",
    "aud_ui_rollback": "Restorable",
    "aud_ui_rollback_tip": "A config snapshot exists — open diff vs current to restore",
    # ── Relative time (JS) ─────────────────────────────────────────
    "aud_js_now": "just now",
    "aud_js_min": "{n} min ago",
    "aud_js_hour": "{n} h ago",
    # ── /audit domain quick filters ────────────────────────────────
    "au_fam_danger": "⚠️ Destructive",
    "au_fam_memory": "🧠 Memory ops",
    "au_fam_kb": "📚 Knowledge",
    "au_fam_persona": "🎭 Personas",
    "au_fam_rpa": "🤖 RPA · channels",
    # ── /audit retention note ──────────────────────────────────────
    "au_retention_note": "Kept {days} days · up to {rows} rows",
}
