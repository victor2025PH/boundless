# -*- coding: utf-8 -*-
"""学习队列页（learner.html）2026-08-02 改造新增词条。

存量 lr_* 键仍在 web_i18n.py 单体；本 pack 只收新键（lr2_* / err.learner.*），
遵守「新增词条一律进 pack」的单源规则。
"""

ZH = {
    # ── 路由错误文案 ──
    "err.learner.kb_unavailable": "知识库不可用，学习功能暂停",
    "err.learner.ai_unavailable": "AI 引擎不可用，无法生成草稿（已有草稿仍可正常审核）",
    "err.learner.feed_query_required": "请填写要学习的问题",
    "err.learner.feed_query_too_long": "问题过长（最多 200 字）",

    # ── 页面状态条 ──
    "lr2_status_last": "上次学习",
    "lr2_status_manual": "手动",
    "lr2_status_scheduled": "自动",
    "lr2_status_result": "收集 {c} → 生成 {g} → 入库 {s}",
    "lr2_status_misspool": "未命中池 {n} 条",
    "lr2_status_never": "尚未运行过学习任务（定时任务每 24 小时自动运行一次）",
    "lr2_funnel_detail": "上轮素材明细：达标 {q} 条 · 低于 {t} 次门槛 {b} 条 · 非问题样式 {nq} 条 · 占位符 {p} 条 · 已有草稿 {d} 条",
    "lr2_ai_off": "AI 引擎不可用：可审核已有草稿，暂无法生成新草稿",
    "lr2_svc_err": "学习服务暂不可用",
    "lr2_run_fail_detail": "学习失败",

    # ── 手动喂料 ──
    "lr2_feed_ph": "把客户问过而 AI 没答好的问题贴进来，直接生成学习草稿",
    "lr2_feed_btn": "入队学习",
    "lr2_feed_ok_gen": "已生成草稿，请在下方审核",
    "lr2_feed_ok_queued": "已入队，AI 恢复后自动生成",
    "lr2_feed_dup": "该问题已有草稿，无需重复学习",
    "lr2_feed_fail": "入队失败",
    "lr2_src_manual": "手动喂料",
}

EN = {
    "err.learner.kb_unavailable": "Knowledge base unavailable; learner is paused",
    "err.learner.ai_unavailable": "AI engine unavailable; cannot generate drafts (existing drafts can still be reviewed)",
    "err.learner.feed_query_required": "Please enter the question to learn",
    "err.learner.feed_query_too_long": "Question too long (200 characters max)",

    "lr2_status_last": "Last run",
    "lr2_status_manual": "manual",
    "lr2_status_scheduled": "scheduled",
    "lr2_status_result": "collected {c} → generated {g} → saved {s}",
    "lr2_status_misspool": "{n} missed queries in pool",
    "lr2_status_never": "Learner has not run yet (scheduled to run every 24h)",
    "lr2_funnel_detail": "Last funnel: qualified {q} · below {t}-hit threshold {b} · non-question {nq} · placeholders {p} · already drafted {d}",
    "lr2_ai_off": "AI engine unavailable: you can review existing drafts, but new drafts cannot be generated",
    "lr2_svc_err": "Learner service unavailable",
    "lr2_run_fail_detail": "Learning failed",

    "lr2_feed_ph": "Paste a question the AI failed to answer; a learning draft will be generated",
    "lr2_feed_btn": "Add to learning",
    "lr2_feed_ok_gen": "Draft generated — review it below",
    "lr2_feed_ok_queued": "Queued; it will be generated once AI is back",
    "lr2_feed_dup": "This question already has a draft",
    "lr2_feed_fail": "Failed to queue",
    "lr2_src_manual": "Manual",
}
