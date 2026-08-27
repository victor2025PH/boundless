# -*- coding: utf-8 -*-
"""账号资产中心词条（账号资产保全 P1，2026-08-19）。

覆盖 workspace_assets.html 全部 UI（总览 KPI / 账号资产卡 / 可加回句柄堆叠条 /
快照·导出台账）+ nav 入口。按词条单源规则进 pack（勿往 web_i18n.py 单体加键）；
键前缀 ``ac_``，新建前已确认全库唯一。ZH/EN 键一一对应，由
``test_i18n_packs_bilingual_and_no_collision`` 门禁钉住。
"""

ZH = {
    # ── 入口 / 页头 ──
    "ac_nav": "账号资产中心",
    "ac_title": "账号资产中心",
    "ac_sub": "每个账号沉淀了多少会话、消息、联系人一目了然；账号被封时数据仍在这里，可随时导出、迁移。",
    "ac_refresh": "刷新",
    "ac_loading": "正在盘点资产…",
    "ac_load_fail": "加载失败，请稍后重试",
    # ── 总览 KPI ──
    "ac_kpi_accounts": "账号",
    "ac_kpi_banned": "封禁",
    "ac_kpi_convs": "会话",
    "ac_kpi_msgs": "消息",
    "ac_kpi_contacts": "联系人（各账号累计）",
    "ac_kpi_media": "媒体文件",
    "ac_backup_last": "最近备份",
    "ac_backup_none": "暂无备份记录",
    # ── 账号卡：状态徽章 ──
    "ac_st_banned": "已封禁",
    "ac_st_online": "在线",
    "ac_st_offline": "离线",
    "ac_st_pending": "待接入",
    "ac_st_removed": "已移除",
    "ac_st_history": "仅历史",
    # ── 账号卡：数字与元信息 ──
    "ac_n_convs": "会话",
    "ac_n_msgs": "消息",
    "ac_n_contacts": "联系人",
    "ac_last_active": "最近活跃",
    "ac_media_files": "媒体",
    # ── 可加回句柄 ──
    "ac_reach_title": "可加回句柄覆盖",
    "ac_reach_hint": "按会话档案身份列（用户名/手机号）统计的私聊客户覆盖——迁移建联清单的口径",
    "ac_lg_both": "双句柄",
    "ac_lg_user": "仅用户名",
    "ac_lg_phone": "仅手机号",
    "ac_lg_none": "无句柄",
    # ── CTA ──
    "ac_cta_export": "导出记录",
    "ac_cta_migrate": "导出迁移包",
    "ac_migrate_pending": "迁移包导出待上线（可先导出记录）",
    "ac_exporting": "导出中…",
    "ac_export_ok": "导出完成",
    "ac_export_fail": "导出失败",
    # ── 台账 ──
    "ac_ledger_title": "快照 / 导出台账",
    "ac_ledger_empty": "暂无导出、快照记录（导出记录或触发封号自动快照后此处留痕）",
    "ac_col_time": "时间",
    "ac_col_acct": "平台 / 账号",
    "ac_col_kind": "类型",
    "ac_col_result": "结果",
    "ac_col_detail": "明细",
    "ac_k_export": "历史导出",
    "ac_k_purge": "历史清除",
    "ac_k_migration": "迁移包导出",
    "ac_k_snapshot": "自动快照",
    # ── 空态 ──
    "ac_cards_empty": "暂无账号资产——接入渠道并开始收发消息后，这里会按账号呈现全部沉淀。",
}

EN = {
    # ── nav / header ──
    "ac_nav": "Account Assets",
    "ac_title": "Account Asset Center",
    "ac_sub": "See what each account has accumulated — chats, messages, contacts. If an account gets banned, the data stays here and can be exported or migrated at any time.",
    "ac_refresh": "Refresh",
    "ac_loading": "Taking inventory…",
    "ac_load_fail": "Failed to load, please retry later",
    # ── overview KPIs ──
    "ac_kpi_accounts": "Accounts",
    "ac_kpi_banned": "banned",
    "ac_kpi_convs": "Conversations",
    "ac_kpi_msgs": "Messages",
    "ac_kpi_contacts": "Contacts (sum per account)",
    "ac_kpi_media": "Media files",
    "ac_backup_last": "Last backup",
    "ac_backup_none": "No backups yet",
    # ── status badges ──
    "ac_st_banned": "Banned",
    "ac_st_online": "Online",
    "ac_st_offline": "Offline",
    "ac_st_pending": "Pending",
    "ac_st_removed": "Removed",
    "ac_st_history": "History only",
    # ── card numbers / meta ──
    "ac_n_convs": "Chats",
    "ac_n_msgs": "Messages",
    "ac_n_contacts": "Contacts",
    "ac_last_active": "Last active",
    "ac_media_files": "Media",
    # ── reachability ──
    "ac_reach_title": "Re-addable handle coverage",
    "ac_reach_hint": "Private-chat customers with username/phone on file (conversation identity columns) — the migration outreach list",
    "ac_lg_both": "Both",
    "ac_lg_user": "Username only",
    "ac_lg_phone": "Phone only",
    "ac_lg_none": "No handle",
    # ── CTAs ──
    "ac_cta_export": "Export history",
    "ac_cta_migrate": "Export migration kit",
    "ac_migrate_pending": "Migration export not deployed yet (history export works now)",
    "ac_exporting": "Exporting…",
    "ac_export_ok": "Export done",
    "ac_export_fail": "Export failed",
    # ── ledger ──
    "ac_ledger_title": "Snapshot / export ledger",
    "ac_ledger_empty": "No exports or snapshots yet — history exports and auto snapshots on ban will show up here.",
    "ac_col_time": "Time",
    "ac_col_acct": "Platform / account",
    "ac_col_kind": "Type",
    "ac_col_result": "Result",
    "ac_col_detail": "Detail",
    "ac_k_export": "History export",
    "ac_k_purge": "History purge",
    "ac_k_migration": "Migration export",
    "ac_k_snapshot": "Auto snapshot",
    # ── empty states ──
    "ac_cards_empty": "No account assets yet — connect a channel and start messaging; everything accumulates here per account.",
}
