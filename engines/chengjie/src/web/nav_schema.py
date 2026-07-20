# -*- coding: utf-8 -*-
"""侧栏导航单一数据源(NAV_SCHEMA)。

base.html 的完整/简洁两种模式、命令面板(Ctrl+K)页面项、渠道状态点都从这里渲染,
改一个菜单项只需改这一个文件(TERM_DICT 悬浮词条仍在 base.html,key 由 item.help 关联)。

结构:
- NAV_ICONS: 图标名 → 内联 SVG(品牌图标仅用于渠道组,其余为线性图标)
- NAV_ITEMS: 菜单项定义(label_key 指向 web_i18n,label_zh 为兜底文案)
- NAV_GROUPS_FULL: 完整模式分组(按任务流:工作台/渠道/AI/洞察/合规/系统/支持)
- SIMPLE_CORE / SIMPLE_MORE: 简洁模式主区与折叠区(引用 item id)
- "__domain_pages__" 哨兵: 模板在该位置内联渲染域动态页(domain_web_pages)

约定:
- key      = active 高亮键(与 admin.py _PATH_TO_ACTIVE 的值一致)
- badge    = 徽标 span 的 DOM id(简洁/完整互斥渲染,可共用同一 id)
- dot      = 渠道在线状态点的 data-chan 值(base.html JS 轮询填充)
- help     = base.html TERM_DICT 的悬浮词条 key
- master_only(item/group)= 仅 master 角色渲染
- cmd_keys = 命令面板搜索别名(含旧菜单名,保证改名后老用户仍搜得到)
"""

_STROKE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">%s</svg>'
_BRAND = '<svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18">%s</svg>'

NAV_ICONS = {
    "inbox": _STROKE % '<path d="M4 4h16v16H4z"/><path d="M4 9l8 5 8-5"/>',
    "file-text": _STROKE % '<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "heart": _STROKE % '<path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/>',
    "pulse": _STROKE % '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "radar": _STROKE % '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.2 19.1 19.1"/>',
    "telegram": _BRAND % '<path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/>',
    "line": _BRAND % '<path d="M19.365 9.863c.349 0 .63.285.63.631 0 .345-.281.63-.63.63H17.61v1.125h1.755c.349 0 .63.283.63.63 0 .344-.281.629-.63.629h-2.386c-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.63-.63h2.386c.346 0 .627.285.627.63 0 .349-.281.63-.63.63H17.61v1.125h1.755zm-3.855 3.016c0 .27-.174.51-.432.596-.064.021-.133.031-.199.031-.211 0-.391-.09-.51-.25l-2.443-3.317v2.94c0 .344-.279.629-.631.629-.346 0-.626-.285-.626-.629V8.108c0-.27.173-.51.43-.595.06-.023.136-.033.194-.033.195 0 .375.104.495.254l2.462 3.33V8.108c0-.345.282-.63.63-.63.345 0 .63.285.63.63v4.771zm-5.741 0c0 .344-.282.629-.631.629-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.63-.63.346 0 .628.285.628.63v4.771zm-2.466.629H4.917c-.345 0-.63-.285-.63-.629V8.108c0-.345.285-.63.63-.63.348 0 .63.285.63.63v4.141h1.756c.348 0 .629.283.629.63 0 .344-.282.629-.629.629M24 10.314C24 4.943 18.615.572 12 .572S0 4.943 0 10.314c0 4.811 4.27 8.842 10.035 9.608.391.082.923.258 1.058.59.12.301.079.766.038 1.08l-.164 1.02c-.045.301-.24 1.186 1.049.645 1.291-.539 6.916-4.078 9.436-6.975C23.176 14.393 24 12.458 24 10.314"/>',
    "messenger": _BRAND % '<path d="M12 2C6.477 2 2 6.145 2 11.259c0 2.913 1.454 5.512 3.726 7.21V22l3.405-1.869c.91.252 1.872.388 2.869.388 5.523 0 10-4.145 10-9.259S17.523 2 12 2zm.997 12.467l-2.546-2.715-4.97 2.715 5.467-5.804 2.61 2.715 4.905-2.715-5.466 5.804z"/>',
    "whatsapp": _BRAND % '<path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884"/>',
    "plus-circle": _STROKE % '<path d="M12 2a10 10 0 0110 10 10 10 0 01-10 10A10 10 0 012 12 10 10 0 0112 2z"/><path d="M12 8v8M8 12h8"/>',
    "persona": _STROKE % '<circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/><circle cx="18.5" cy="7" r="2.5" fill="currentColor" opacity=".45"/>',
    "book": _STROKE % '<path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/>',
    "book-open": _STROKE % '<path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2z"/><path d="M22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z"/>',
    "brain": _STROKE % '<path d="M12 4.5a2.5 2.5 0 00-2.5 2.5v.5A3.5 3.5 0 006 11v.5a3.5 3.5 0 00.5 6.9A2.6 2.6 0 009 21h6a2.6 2.6 0 002.5-2.6 3.5 3.5 0 00.5-6.9V11a3.5 3.5 0 00-3.5-3.5V7A2.5 2.5 0 0012 4.5z"/><path d="M12 4.5V21"/>',
    "sliders": _STROKE % '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    "target": _STROKE % '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    "grid": _STROKE % '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "bar-chart": _STROKE % '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
    "funnel": _STROKE % '<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>',
    "dollar": _STROKE % '<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/>',
    "alert-triangle": _STROKE % '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "clock": _STROKE % '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "users": _STROKE % '<path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/>',
    "gear": _STROKE % '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>',
    "git": _STROKE % '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 009 9"/>',
    "terminal": _STROKE % '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    "code": _STROKE % '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    "help": _STROKE % '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "phone": _STROKE % '<path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72 12.84 12.84 0 00.7 2.81 2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45 12.84 12.84 0 002.81.7A2 2 0 0122 16.92z"/>',
    # 域动态页图标(manifest.yaml web.pages[].icon)
    "globe": _STROKE % '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/>',
    "package": _STROKE % '<line x1="16.5" y1="9.4" x2="7.5" y2="4.21"/><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
    "wallet": _STROKE % '<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>',
    "truck": _STROKE % '<rect x="1" y="3" width="15" height="13"/><polygon points="16 8 20 8 23 11 23 16 16 16 16 8"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/>',
    "info": _STROKE % '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
}

# ── 菜单项 ───────────────────────────────────────────────────────────────────
NAV_ITEMS = {
    "workspace": dict(key="", path="/workspace", icon="inbox", target="_blank",
                      external=True, label_key="workspace_inbox", label_zh="坐席工作台",
                      help="nav_unified_inbox",
                      cmd_keys="unified inbox 统一 收件箱 消息 聊天 多平台 坐席 工作台 workspace"),
    "cases": dict(key="cases", path="/cases", icon="file-text", badge="badge-cases",
                  label_key="cases", label_zh="案例跟进", help="nav_cases",
                  cmd_keys="cases 案例 待处理 待处理案例 跟进 case"),
    "care": dict(key="care", path="/care-schedule", icon="heart",
                 label_key="care", label_zh="主动关怀", help="nav_care",
                 cmd_keys="care 关怀 主动 问候"),
    "relations_health": dict(key="relations_health", path="/relations-health", icon="pulse",
                             label_key="relations_health", label_zh="流失预警",
                             help="nav_relations_health",
                             cmd_keys="churn 流失 预警 关系 健康 relations"),
    "rpa_overview": dict(key="rpa_overview", path="/rpa-overview", icon="radar",
                         badge="badge-rpa-ov", label_key="rpa_overview", label_zh="渠道总览",
                         help="nav_rpa_overview",
                         cmd_keys="rpa overview 总览 跨平台 渠道 渠道总览 RPA跨平台总览 telegram line messenger whatsapp"),
    "telegram": dict(key="telegram", path="/telegram", icon="telegram", dot="telegram",
                     label_key="telegram_settings", label_zh="Telegram 自动化",
                     help="nav_telegram",
                     cmd_keys="telegram tg 电报 自动化 设置 主号 telegram设置"),
    "line_rpa": dict(key="line_rpa", path="/line-rpa", icon="line", dot="line",
                     label_key="line_rpa", label_zh="LINE 自动化", help="nav_line_rpa",
                     cmd_keys="line rpa 自动化 自动聊天 真机"),
    "messenger_rpa": dict(key="messenger_rpa", path="/messenger-rpa", icon="messenger",
                          dot="messenger", label_key="messenger_rpa",
                          label_zh="Messenger 自动化", help="nav_messenger_rpa",
                          cmd_keys="messenger facebook fb rpa 自动化 线索"),
    "whatsapp_rpa": dict(key="whatsapp_rpa", path="/whatsapp-rpa", icon="whatsapp",
                         dot="whatsapp", label_key="whatsapp_rpa",
                         label_zh="WhatsApp 自动化", help="nav_whatsapp_rpa",
                         cmd_keys="whatsapp wa rpa 自动化 自动聊天 模板"),
    "ai_studio": dict(key="ai_studio", path="/ai-studio", icon="plus-circle",
                      featured=True, strong=True, label_key="ai_studio",
                      label_zh="AI 工作室", help="nav_ai_studio",
                      cmd_keys="ai studio 工作室 中枢 hub"),
    "personas": dict(key="personas", path="/personas", icon="persona",
                     label_key="personas", label_zh="人设工作室", help="nav_personas",
                     cmd_keys="personas persona 人设 角色 工作室"),
    "knowledge": dict(key="knowledge", path="/knowledge", icon="book",
                      label_key="knowledge", label_zh="知识库", help="nav_knowledge",
                      cmd_keys="knowledge 知识库 话术"),
    "learner": dict(key="learner", path="/learner", icon="book-open", badge="badge-learner",
                    label_key="learner", label_zh="学习队列", help="nav_learner",
                    cmd_keys="learner 学习 审核 AI 学习审核 学习队列 队列"),
    "episodic": dict(key="episodic", path="/episodic-memory", icon="brain",
                     label_key="episodic", label_zh="AI 记忆", help="nav_episodic",
                     cmd_keys="episodic memory 记忆 情景 情景记忆"),
    "strategies": dict(key="strategies", path="/strategies", icon="sliders",
                       label_key="strategies", label_zh="回复策略", help="nav_strategies",
                       cmd_keys="strategies 策略 配置 策略配置 回复策略 参数"),
    "strategy_analytics": dict(key="strategy-analytics", path="/strategy-analytics",
                               icon="target", label_key="strategy_analytics",
                               label_zh="策略效果", help="nav_strategy_analytics",
                               cmd_keys="strategy analytics 策略 效果"),
    "dash": dict(key="dash", path="/", icon="grid", label_key="dashboard",
                 label_zh="数据概览", help="nav_dashboard",
                 cmd_keys="dashboard home 首页 概览 仪表盘"),
    "analytics": dict(key="analytics", path="/analytics", icon="bar-chart",
                      label_key="analytics", label_zh="运营分析", help="nav_analytics",
                      cmd_keys="analytics 运营 分析 数据"),
    "funnel": dict(key="funnel", path="/funnel", icon="funnel",
                   label_key="rpa_fn_title", label_zh="运营漏斗", help="nav_funnel",
                   cmd_keys="funnel 漏斗 转化 运营漏斗 conversion journey"),
    "monetization": dict(key="monetization", path="/monetization", icon="dollar",
                         label_key="monetization", label_zh="变现营收",
                         help="nav_monetization",
                         cmd_keys="monetization revenue 变现 营收 订阅"),
    "crisis_audit": dict(key="crisis_audit", path="/crisis-audit", icon="alert-triangle",
                         badge="badge-crisis", label_key="crisis_audit", label_zh="危机审计",
                         help="nav_crisis_audit", cmd_keys="crisis 危机 审计 风险"),
    "audit": dict(key="audit", path="/audit", icon="clock", label_key="audit",
                  label_zh="操作记录", help="nav_audit", cmd_keys="audit 审计 记录 操作"),
    "users": dict(key="users", path="/users", icon="users", label_key="users",
                  label_zh="用户管理", help="nav_users", cmd_keys="users 用户"),
    "settings": dict(key="settings", path="/settings", icon="gear",
                     label_key="system_settings", label_zh="系统设置", help="nav_settings",
                     cmd_keys="settings 系统 设置 品牌 授权 system"),
    "diff": dict(key="diff", path="/diff", icon="git", label_key="diff", label_zh="版本对比",
                 help="nav_diff", cmd_keys="diff 对比 版本"),
    "logs": dict(key="logs", path="/logs", icon="terminal", label_key="logs",
                 label_zh="实时日志", help="nav_logs", cmd_keys="logs 日志 终端"),
    "developer": dict(key="developer", path="/developer", icon="code", label_key="developer",
                      label_zh="开发者工具", help="nav_developer",
                      cmd_keys="developer 开发者 API key 密钥 接口 私聊 process_private"),
    "help": dict(key="help", path="/help", icon="help", label_key="help_center",
                 label_zh="帮助中心", help="nav_help", cmd_keys="help 帮助"),
    # 简洁模式专属:人工转接(深链到系统设置页的人工转接卡片)
    "escalation": dict(key="settings", path="/settings#escalation", icon="phone",
                       master_only=True, label_key="escalation", label_zh="人工转接",
                       help="nav_escalation", cmd_keys="escalation 人工 转接 客服 handoff"),
}

# 仅命令面板可达(无侧栏入口)的页面
CMD_EXTRA_ITEMS = {
    "templates": dict(key="tpl", path="/templates", icon="file-text",
                      label_key="templates", label_zh="话术模板",
                      cmd_keys="templates 模板 话术"),
    "import": dict(key="import", path="/import", icon="package",
                   label_key="import_page", label_zh="导入配置", cmd_keys="import 导入"),
}

DOMAIN_SENTINEL = "__domain_pages__"

# ── 完整模式分组 ─────────────────────────────────────────────────────────────
NAV_GROUPS_FULL = [
    dict(label_key="section_workbench", label_zh="工作台",
         items=["workspace", "cases", "care", "relations_health"]),
    dict(label_key="section_channels", label_zh="渠道自动化",
         items=["rpa_overview", "telegram", "line_rpa", "messenger_rpa",
                "whatsapp_rpa", DOMAIN_SENTINEL]),
    dict(label_key="section_ai_kb", label_zh="AI 与知识",
         items=["ai_studio", "personas", "knowledge", "learner", "episodic",
                "strategies", "strategy_analytics"]),
    dict(label_key="section_insights", label_zh="数据洞察",
         items=["dash", "analytics", "funnel", "monetization"]),
    dict(label_key="section_compliance", label_zh="安全合规",
         items=["crisis_audit", "audit"]),
    dict(label_key="section_system", label_zh="系统管理", master_only=True,
         items=["users", "settings", "diff", "logs", "developer"]),
    dict(label_key="section_support", label_zh="支持", items=["help"]),
]

# ── 简洁模式 ────────────────────────────────────────────────────────────────
SIMPLE_CORE = ["workspace", "cases", "care", "knowledge", DOMAIN_SENTINEL,
               "learner", "personas", "rpa_overview", "telegram", "line_rpa",
               "messenger_rpa", "whatsapp_rpa", "escalation"]
SIMPLE_MORE = ["dash", "analytics", "audit", "episodic", "crisis_audit",
               "relations_health", "monetization", "help"]


def _resolve(ids):
    return [i if i == DOMAIN_SENTINEL else NAV_ITEMS[i] for i in ids]


def _cmd_items():
    """命令面板页面项:完整模式顺序 + 简洁专属项 + 面板专属页。"""
    simple_ids = set(SIMPLE_CORE) | set(SIMPLE_MORE)
    seen, out = set(), []

    def add(item_id, item):
        if item_id in seen:
            return
        seen.add(item_id)
        d = dict(item)
        d["simple"] = item_id in simple_ids
        out.append(d)

    for grp in NAV_GROUPS_FULL:
        for i in grp["items"]:
            if i != DOMAIN_SENTINEL:
                add(i, NAV_ITEMS[i])
    for i in SIMPLE_CORE + SIMPLE_MORE:
        if i != DOMAIN_SENTINEL:
            add(i, NAV_ITEMS[i])
    for i, item in CMD_EXTRA_ITEMS.items():
        add(i, item)
    return out


_NAV_CONTEXT = dict(
    nav_icons=NAV_ICONS,
    nav_groups=[dict(g, items=_resolve(g["items"])) for g in NAV_GROUPS_FULL],
    nav_simple_core=_resolve(SIMPLE_CORE),
    nav_simple_more=_resolve(SIMPLE_MORE),
    nav_cmd_items=_cmd_items(),
)


def get_nav_context() -> dict:
    """供 admin.py _enrich_context 与渲染类测试注入模板上下文(静态数据,进程内单例)。"""
    return _NAV_CONTEXT
