# -*- coding: utf-8 -*-
"""ops-overview「🎭 人设覆写」卡词条（2026-07-26 方案 A P3 观测）。

独立成 pack（而非并入 ops_overview_page.py）：该大包正被并行工作流编辑，
按 i18n_packs 治理机制（自动发现、跨 pack 同 key 即抛错）拆小文件零冲突。
卡片消费方：src/web/templates/ops_overview.html::renderPersonaOverride。
"""

ZH = {
    "ov2_s_po": "人设覆写（会话级换绑用量与 legacy 债）",
    "ov2_po_sub": (
        "出站解析按档位分布 + 治理动作累计。决策「conv_override 要不要默认开」"
        "与「legacy 清理收官时点」都看这里。"
    ),
    "ov2_po_resolves": "出站解析",
    "ov2_po_conv": "会话覆写命中",
    "ov2_po_acct": "账号人设命中",
    "ov2_po_legacy_sup": "legacy 被压制",
    "ov2_po_debt": "遗留债余量",
    "ov2_po_actions": "治理动作",
    "ov2_po_a_bind": "会话换绑",
    "ov2_po_a_unbind": "解除覆写",
    "ov2_po_a_acct": "整号切换",
    "ov2_po_a_lg_up": "legacy 升级",
    "ov2_po_a_lg_rm": "legacy 清除",
    "ov2_po_by_platform": "覆写命中按平台",
    "ov2_po_col_platform": "平台",
    "ov2_po_col_count": "次数",
    "ov2_po_actions_bd": "治理动作明细",
    "ov2_po_col_action": "动作",
    "ov2_po_fails": "换绑被拒（业务层）",
    "ov2_po_csrf_blocked": "换绑被拦（安全层）",
    "ov2_po_fails_bd": "被拒原因明细",
    "ov2_po_col_fail": "操作:原因",
    "ov2_po_hint": (
        "计数自实例启动累计（重启清零）；「遗留债余量」是实时盘点的活水位"
        "（不随重启清零，Messenger RPA 托管键不计入）。债清零后回升＝有路径在"
        "重新制造旧式绑定——去 人设工作室 › 遗留绑定清理 处置。"
    ),
}

EN = {
    "ov2_s_po": "Persona overrides (per-conversation switching & legacy debt)",
    "ov2_po_sub": (
        "Outbound persona resolutions by winning tier, plus governance action "
        "counters. Read this to decide whether conv_override should default on "
        "and when legacy cleanup is finished."
    ),
    "ov2_po_resolves": "Resolutions",
    "ov2_po_conv": "Conv-override wins",
    "ov2_po_acct": "Account-persona wins",
    "ov2_po_legacy_sup": "Legacy suppressed",
    "ov2_po_debt": "Legacy debt left",
    "ov2_po_actions": "Governance actions",
    "ov2_po_a_bind": "Bind conv override",
    "ov2_po_a_unbind": "Clear conv override",
    "ov2_po_a_acct": "Account persona set",
    "ov2_po_a_lg_up": "Legacy upgraded",
    "ov2_po_a_lg_rm": "Legacy removed",
    "ov2_po_by_platform": "Override wins by platform",
    "ov2_po_col_platform": "Platform",
    "ov2_po_col_count": "Count",
    "ov2_po_actions_bd": "Action breakdown",
    "ov2_po_col_action": "Action",
    "ov2_po_fails": "Switches rejected (business)",
    "ov2_po_csrf_blocked": "Switches blocked (security)",
    "ov2_po_fails_bd": "Rejection breakdown",
    "ov2_po_col_fail": "Op:reason",
    "ov2_po_hint": (
        "Counters accumulate since instance start (reset on restart); "
        "\u201cLegacy debt left\u201d is a live gauge (survives restarts; "
        "Messenger-RPA-managed keys excluded). If it climbs back above zero "
        "after cleanup, something is creating peer-global bindings again \u2014 "
        "handle them in Persona Studio \u203a Legacy binding cleanup."
    ),
}
