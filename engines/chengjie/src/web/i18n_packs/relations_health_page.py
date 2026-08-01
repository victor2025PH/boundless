# -*- coding: utf-8 -*-
"""流失预警页 relations_health.html 改版词条（RH-P1，2026-08-01）。

新键一律 ``rh2_`` 前缀（存量 ``rh_s*/rh_js*`` 在 web_i18n.py 单体，禁止撞 key）。
约定：整句参数化（``{n}`` 经 window.Tf 插值），不做片段拼接。
"""

ZH = {
    # ── 标签页 / 过滤器 ──────────────────────────────────────────────
    "rh2_tab_full": "全量榜 · 跨平台旅程",
    "rh2_tab_lite": "轻量榜 · 收件箱信号",
    "rh2_lite_days": "沉默 ≥ (天)",
    "rh2_lite_note": "轻量榜直接读收件箱信号（沉默时长 / 投诉·流失关键词 / 质检分），即开即用；"
                     "跨平台身份归并、亲密度与付费信号在全量榜。",
    # ── KPI 卡 ───────────────────────────────────────────────────────
    "rh2_kpi_scanned": "扫描关系",
    "rh2_kpi_listed": "上榜",
    "rh2_kpi_atrisk": "危机+风险",
    "rh2_kpi_payer_risk": "付费流失中",
    "rh2_kpi_lapsed": "掉付费",
    "rh2_kpi_lite_high": "高风险会话",
    "rh2_kpi_lite_med": "中风险会话",
    # ── 今日必挽 ─────────────────────────────────────────────────────
    "rh2_focus_title": "今日必挽",
    "rh2_focus_empty": "暂无必挽客户",
    # ── 三态 / 引导 ──────────────────────────────────────────────────
    "rh2_probe_fail": "能力探测失败，页面暂时无法判断可用数据源",
    "rh2_retry": "重试",
    "rh2_guide_title": "全量流失预警未启用",
    "rh2_guide_body": "本页的全量榜依赖「客户旅程 (contacts)」子系统——跨平台身份归并、"
                      "亲密度引擎与付费信号都由它记账。当前实例未开启 contacts.enabled，"
                      "请管理员在实例配置 overlay 中开启后重启实例生效。",
    "rh2_pending_restart": "配置 contacts.enabled 已开启但尚未生效（子系统在进程启动时装配），"
                           "下一次实例重启后全量榜自动可用；先用轻量榜。",
    "rh2_coldstart": "全量榜数据积累中：contacts 子系统刚开启，跨平台旅程随新消息逐步建档"
                     "（当前已建档 {n} 位客户）。数据长起来前建议先看轻量榜。",
    "rh2_lite_only_hint": "当前运行在轻量榜（收件箱信号）。开启 contacts.enabled 并重启实例后，"
                          "可解锁跨平台全量榜与「生成话术→标记已发」挽回闭环。",
    "rh2_err_load": "榜单加载失败",
    "rh2_empty_full": "没有符合条件的风险客户——关系都还算健康",
    "rh2_empty_lite": "没有符合条件的风险会话",
    # ── 表格 / 行操作 ────────────────────────────────────────────────
    "rh2_col_customer": "客户",
    "rh2_col_signal": "信号",
    "rh2_col_risk_advice": "风险原因与建议",
    "rh2_col_last_reason": "最近消息 / 原因",
    "rh2_open_conv": "打开会话",
    "rh2_detail": "详情",
    "rh2_detail_fail": "详情加载失败",
    "rh2_score_word": "风险分",
    "rh2_no_name": "(未命名)",
    "rh2_draft_na": "生成话术不可用（需 reactivation 组件在线）",
    "rh2_loaded_meta": "已加载 {n} 条 · 扫描 {m} 段关系",
    "rh2_lite_loaded_meta": "已加载 {n} 条（沉默 ≥ {d} 天）",
    "rh2_cache_tag": "（缓存）",
    # ── 图例 ─────────────────────────────────────────────────────────
    "rh2_legend_payvar": "紫底 = 付费客户正在流失（最优先挽回）",
    "rh2_legend_var": "红底 = 高价值关系正在流失",
    # ── 话术弹窗 / 管理工具 ──────────────────────────────────────────
    "rh2_draft_hint": "复制话术后到对应渠道发送；发出后点「标记已发」记录冷却，避免重复打扰。",
    "rh2_copied": "已复制到剪贴板",
    "rh2_copy_fail": "复制失败，请手动全选复制",
    "rh2_admin_tools": "管理工具",
    # ── RH-P2：挽回率 KPI + 页内直发 ─────────────────────────────────
    "rh2_kpi_winback": "7日挽回率",
    "rh2_winback_tip": "近{d}天发送 {s} · 挽回 {r} · 待观察 {p}",
    "rh2_send_direct": "直接发送",
    "rh2_sending": "发送中…",
    "rh2_sent_ok": "已发送，冷却已记录",
    "rh2_send_fail": "发送失败",
    "rh2_send_na": "未关联到可发送会话，请复制后到渠道手动发送",
}

EN = {
    "rh2_tab_full": "Full board · journeys",
    "rh2_tab_lite": "Lite board · inbox signals",
    "rh2_lite_days": "Silent ≥ (days)",
    "rh2_lite_note": "The lite board reads inbox signals directly (silence, complaint/churn "
                     "keywords, QA score) and works out of the box; cross-platform identity "
                     "merge, intimacy and payment signals live on the full board.",
    "rh2_kpi_scanned": "Scanned",
    "rh2_kpi_listed": "Listed",
    "rh2_kpi_atrisk": "Crisis + at-risk",
    "rh2_kpi_payer_risk": "Paying at risk",
    "rh2_kpi_lapsed": "Lapsed payers",
    "rh2_kpi_lite_high": "High-risk convs",
    "rh2_kpi_lite_med": "Medium-risk convs",
    "rh2_focus_title": "Save-first today",
    "rh2_focus_empty": "Nothing urgent right now",
    "rh2_probe_fail": "Capability probe failed; cannot determine available data sources",
    "rh2_retry": "Retry",
    "rh2_guide_title": "Full churn board not enabled",
    "rh2_guide_body": "The full board depends on the contacts subsystem — cross-platform "
                      "identity merge, the intimacy engine and payment signals are all "
                      "recorded by it. contacts.enabled is off on this instance; ask an "
                      "admin to enable it in the config overlay and restart the instance.",
    "rh2_pending_restart": "contacts.enabled is already on but not loaded yet (the subsystem "
                           "is assembled at process start). The full board becomes available "
                           "after the next instance restart; use the lite board meanwhile.",
    "rh2_coldstart": "Full board is warming up: the contacts subsystem was just enabled and "
                     "journeys accumulate as new messages arrive ({n} contacts so far). "
                     "Use the lite board until data builds up.",
    "rh2_lite_only_hint": "Running on the lite board (inbox signals). Enable contacts.enabled "
                          "and restart to unlock the cross-platform full board and the "
                          "script-generation win-back loop.",
    "rh2_err_load": "Board failed to load",
    "rh2_empty_full": "No at-risk customers matched — relations look healthy",
    "rh2_empty_lite": "No risky conversations matched",
    "rh2_col_customer": "Customer",
    "rh2_col_signal": "Signals",
    "rh2_col_risk_advice": "Risk & advice",
    "rh2_col_last_reason": "Last message / reasons",
    "rh2_open_conv": "Open chat",
    "rh2_detail": "Details",
    "rh2_detail_fail": "Failed to load details",
    "rh2_score_word": "Risk score",
    "rh2_no_name": "(unnamed)",
    "rh2_draft_na": "Script generation unavailable (needs the reactivation component)",
    "rh2_loaded_meta": "Loaded {n} rows · scanned {m} relationships",
    "rh2_lite_loaded_meta": "Loaded {n} rows (silent ≥ {d} days)",
    "rh2_cache_tag": "(cached)",
    "rh2_legend_payvar": "Purple = paying customer at risk (save first)",
    "rh2_legend_var": "Red = high-value relation at risk",
    "rh2_draft_hint": "Copy the script and send it in the customer's channel, then click "
                      "\"Mark as sent\" to record the cooldown and avoid double-pinging.",
    "rh2_copied": "Copied to clipboard",
    "rh2_copy_fail": "Copy failed — select the text manually",
    "rh2_admin_tools": "Admin tools",
    # ── RH-P2: winback KPI + in-page direct send ─────────────────────
    "rh2_kpi_winback": "7-day winback",
    "rh2_winback_tip": "Last {d}d: sent {s} · won back {r} · pending {p}",
    "rh2_send_direct": "Send now",
    "rh2_sending": "Sending…",
    "rh2_sent_ok": "Sent; cooldown recorded",
    "rh2_send_fail": "Send failed",
    "rh2_send_na": "No sendable conversation linked — copy and send manually",
}
