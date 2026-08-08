# -*- coding: utf-8 -*-
"""侧栏导航单一数据源(NAV_SCHEMA)。

base.html 的完整/简洁两种模式、命令面板(Ctrl+K)页面项、渠道状态点都从这里渲染,
改一个菜单项只需改这一个文件(TERM_DICT 悬浮词条仍在 base.html,key 由 item.help 关联)。

结构:
- NAV_ICONS: 图标名 → 内联 SVG(品牌图标仅用于渠道组,其余为线性图标)
- NAV_ITEMS: 菜单项定义(label_key 指向 web_i18n,label_zh 为兜底文案)
- NAV_GROUPS_FULL: 完整模式分组(按任务流:工作台/真机矩阵/AI/洞察/合规/系统/支持)
- SIMPLE_CORE / SIMPLE_MORE: 简洁模式主区与折叠区(引用 item id;定位=值班/看店
  日常,运维/分析/矩阵类只在完整模式)
- MATRIX_ITEM_IDS → nav_matrix_items: 真机矩阵五页(总览+四渠道)。简洁模式侧栏
  不渲染它们;深链进矩阵页时两套侧栏(base.html/_ws_sidebar.html)按当前路径
  上下文渲染本组,保住组内互切与高亮
- "__domain_pages__" 哨兵: 模板在该位置内联渲染域动态页(domain_web_pages)

约定:
- key      = active 高亮键(与 admin.py _PATH_TO_ACTIVE 的值一致)
- badge    = 徽标 span 的 DOM id(简洁/完整互斥渲染,可共用同一 id)
- dot      = 渠道在线状态点的 data-chan 值(base.html JS 轮询填充)
- help     = base.html TERM_DICT 的悬浮词条 key
- master_only(item/group)= 仅 master 角色渲染
- feature  = 授权档位功能名(licensing/feature_gate.py 注册表;gate 默认关 = 全量渲染
             零变化;P3 起锁定项渲染为「锁标 + 跳会员中心」升级引导(locked=True 注解,
             base.html nav_item 宏消费);命令面板仍直接隐藏锁定项(跳转列表无升级语义)
- cmd_keys = 命令面板搜索别名(含旧菜单名/同义词,保证改名或术语分裂后老用户仍搜得到)
- simple   = 命令面板「简洁模式」可见性覆写(默认按是否在 SIMPLE_CORE/SIMPLE_MORE 推导;
             CMD_EXTRA_ITEMS 的项不在简洁清单里 → 不显式声明 simple=True 就只在完整
             模式的面板里出现,而全角色默认档位是简洁模式,见 web_user_store)
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
    # winname：入口走 _win_unique.html 的窗口唯一性 helper——点击复用/聚焦既有
    # 工作台窗口而非每次 _blank 新开（窗口堆积的头号入口）；target 保留作降级兜底。
    "workspace": dict(key="", path="/workspace", icon="inbox", target="_blank",
                      winname="workspace",
                      external=True, label_key="workspace_inbox", label_zh="坐席工作台",
                      help="nav_unified_inbox",
                      cmd_keys="unified inbox 统一 收件箱 消息 聊天 多平台 坐席 工作台 workspace"),
    "cases": dict(key="cases", path="/cases", icon="file-text", badge="badge-cases",
                  label_key="cases", label_zh="案例跟进", help="nav_cases",
                  cmd_keys="cases 案例 待处理 待处理案例 跟进 case"),
    "care": dict(feature="care", key="care", path="/care-schedule", icon="heart",
                 label_key="care", label_zh="主动关怀", help="nav_care",
                 cmd_keys="care 关怀 主动 问候"),
    "relations_health": dict(feature="care", key="relations_health", path="/relations-health", icon="pulse",
                             label_key="relations_health", label_zh="流失预警",
                             help="nav_relations_health",
                             cmd_keys="churn 流失 预警 关系 健康 relations"),
    # ── 真机矩阵（2026-08-03 更名迁移）：总览 + 四渠道从简洁模式撤出，只在完整
    #    模式「真机矩阵」组渲染（运维驾驶舱定位，普通用户日常不碰）。cmd_keys 保留
    #    旧名（渠道总览/XX 渠道/渠道中心），老用户 Ctrl+K 搜旧名仍命中；五项显式
    #    simple=True＝侧栏藏、命令面板仍可搜（藏而不废，URL/书签也不封）。简洁模式
    #    深链进矩阵页时，两套侧栏按当前路径上下文渲染本组（消费 nav_matrix_items）。
    "rpa_overview": dict(feature="rpa", key="rpa_overview", path="/rpa-overview",
                         icon="radar", simple=True,
                         badge="badge-rpa-ov", label_key="rpa_overview", label_zh="矩阵总览",
                         help="nav_rpa_overview",
                         cmd_keys="rpa overview 总览 跨平台 渠道 渠道总览 真机矩阵 矩阵 "
                                  "matrix RPA跨平台总览 telegram line messenger whatsapp"),
    # 渠道中心融合：四渠道设置页迁入工作台壳（/workspace/channels/*），
    # 旧路径仍 302 兜底；侧栏/命令面板直指新址。
    # Telegram 刻意不挂 feature="rpa"：主号走 MTProto 协议（基础产品面），API 前缀
    # 表也不含 telegram；挂档会把核心渠道锁进 flagship（门禁钉在
    # test_page_feature_mapping_pure：/workspace/channels/telegram 不守卫）。
    "telegram": dict(key="telegram", path="/workspace/channels/telegram",
                     icon="telegram", dot="telegram", simple=True,
                     label_key="telegram_settings", label_zh="Telegram",
                     help="nav_telegram",
                     cmd_keys="telegram tg 电报 自动化 设置 主号 telegram设置 渠道中心 "
                              "真机矩阵 matrix"),
    "line_rpa": dict(feature="rpa", key="line_rpa", path="/workspace/channels/line", icon="line",
                     dot="line", simple=True,
                     label_key="line_rpa", label_zh="LINE", help="nav_line_rpa",
                     cmd_keys="line rpa 自动化 自动聊天 真机 渠道中心 真机矩阵 matrix"),
    "messenger_rpa": dict(feature="rpa", key="messenger_rpa", path="/workspace/channels/messenger",
                          icon="messenger", simple=True,
                          dot="messenger", label_key="messenger_rpa",
                          label_zh="Messenger", help="nav_messenger_rpa",
                          cmd_keys="messenger facebook fb rpa 自动化 线索 渠道中心 "
                                   "真机矩阵 matrix"),
    "whatsapp_rpa": dict(feature="rpa", key="whatsapp_rpa", path="/workspace/channels/whatsapp",
                         icon="whatsapp", simple=True,
                         dot="whatsapp", label_key="whatsapp_rpa",
                         label_zh="WhatsApp", help="nav_whatsapp_rpa",
                         cmd_keys="whatsapp wa rpa 自动化 自动聊天 模板 渠道中心 "
                                  "真机矩阵 matrix"),
    # 群脉 CrowdX 导播台：多号群戏剧本库 + 离线排练（dry-run，不发真消息）
    "group_show": dict(key="group_show", path="/group-show", icon="users",
                       label_key="gs_nav", label_zh="群脉导播台",
                       cmd_keys="group show crowdx 群脉 导播 导播台 群戏 剧本 排练 炒群"),
    # ai_studio 已解散（2026-08-01）：5 个 tab 中 3 个是兄弟页面套壳，独有能力
    # 已迁回权威页（Prompt A/B→策略效果、合并批量/扫描→ops/merge-reviews、
    # 身份映射→ops/contacts、关系分析三卡→运营分析、聚合统计→数据概览）。
    # /ai-studio 路由 301 → /personas 兜底旧书签。
    "personas": dict(feature="personas", key="personas", path="/personas", icon="persona",
                     label_key="personas", label_zh="人设工作室", help="nav_personas",
                     cmd_keys="personas persona 人设 角色 工作室"),
    "knowledge": dict(feature="kb", key="knowledge", path="/knowledge", icon="book",
                      label_key="knowledge", label_zh="知识库", help="nav_knowledge",
                      cmd_keys="knowledge 知识库 话术"),
    "learner": dict(feature="kb", key="learner", path="/learner", icon="book-open", badge="badge-learner",
                    label_key="learner", label_zh="学习队列", help="nav_learner",
                    cmd_keys="learner 学习 审核 AI 学习审核 学习队列 队列"),
    "episodic": dict(key="episodic", path="/episodic-memory", icon="brain",
                     label_key="episodic", label_zh="AI 记忆", help="nav_episodic",
                     cmd_keys="episodic memory 记忆 情景 情景记忆"),
    "strategies": dict(feature="ai_autosend", key="strategies", path="/strategies", icon="sliders",
                       label_key="strategies", label_zh="回复策略", help="nav_strategies",
                       cmd_keys="strategies 策略 配置 策略配置 回复策略 参数"),
    # 自动回复设置：全自动档位/回复速度/拟人细节的集中入口（2026-08-02 P0）
    "reply_settings": dict(feature="ai_autosend", key="reply_settings", path="/reply-settings",
                           icon="clock",
                           label_key="rps_nav", label_zh="自动回复设置",
                           cmd_keys="reply settings 自动回复 回复速度 档位 延迟 打字 "
                                    "拟人 全自动 速度 autosend delay pacing mode "
                                    "长度 风格 内容 语气 emoji 句数 length style tone"),
    "strategy_analytics": dict(feature="ai_autosend", key="strategy-analytics", path="/strategy-analytics",
                               icon="target", label_key="strategy_analytics",
                               label_zh="策略效果", help="nav_strategy_analytics",
                               cmd_keys="strategy analytics 策略 效果"),
    "dash": dict(key="dash", path="/", icon="grid", label_key="dashboard",
                 label_zh="数据概览", help="nav_dashboard",
                 cmd_keys="dashboard home 首页 概览 仪表盘"),
    # 运营总览（/admin/ops）2026-08-02 补入侧栏：此前只有简洁模式仪表盘的快捷
    # 入口卡能到——信息密度最高的 ops 卡片页没有常驻导航，是历代内容被迫堆进
    # 仪表盘的根因之一。
    "ops": dict(key="ops", path="/admin/ops", icon="pulse",
                label_key="db2_nav_ops", label_zh="运营总览",
                help="nav_ops_overview",
                cmd_keys="ops overview 运营 总览 运营总览 运维 事件 roi 计费 "
                         "可靠性 老板 boss"),
    "analytics": dict(feature="analytics", key="analytics", path="/analytics", icon="bar-chart",
                      label_key="analytics", label_zh="运营分析", help="nav_analytics",
                      cmd_keys="analytics 运营 分析 数据"),
    "funnel": dict(feature="analytics", key="funnel", path="/funnel", icon="funnel",
                   label_key="rpa_fn_title", label_zh="运营漏斗", help="nav_funnel",
                   cmd_keys="funnel 漏斗 转化 运营漏斗 conversion journey"),
    "monetization": dict(feature="monetization", key="monetization", path="/monetization", icon="dollar",
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
    # 融合实例 P3：会员中心（档位/用量/功能矩阵/到期；nav 锁标与顶栏徽章的落点）
    "membership": dict(key="membership", path="/membership", icon="wallet",
                       master_only=True, label_key="mb_nav", label_zh="会员中心",
                       cmd_keys="membership plan 会员 档位 套餐 授权 升级 license"),
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
    # 个人设置(2026-08-04):坐席级外观个性化(主题/壁纸/夜间/字号/圆角/动画)。
    # 全角色可用;设置经 /api/workspace/prefs.appearance 漫游(本机缓存+服务端),
    # 收件箱左栏「主题配色」按钮弹出的快捷面板与本页共用同一渲染器(appearance.js)。
    "personal_settings": dict(key="personal_settings", path="/personal-settings",
                              icon="sliders",
                              label_key="nav_personal_settings", label_zh="个人设置",
                              cmd_keys="personal settings 个人 设置 个人设置 外观 主题 "
                                       "壁纸 夜间 暗色 字号 圆角 动画 表情 appearance "
                                       "theme wallpaper night dark emoji"),
}

# 仅命令面板可达(无侧栏入口)的页面
CMD_EXTRA_ITEMS = {
    "templates": dict(key="tpl", path="/templates", icon="file-text",
                      label_key="templates", label_zh="话术模板",
                      cmd_keys="templates 模板 话术"),
    "import": dict(key="import", path="/import", icon="package",
                   label_key="import_page", label_zh="导入配置", cmd_keys="import 导入"),
    # 坐席「工作目标」深链(/workspace?card=goal → 自动切「客户&关系」tab + 展开目标卡)。
    # 只进命令面板不进侧栏:侧栏已有 workspace 行,同一目的页不重复占位。
    # 术语三分裂(坐席端=工作目标 / 配置与 ops=营销目标 / 运营口语=工作计划)全塞 cmd_keys
    # ——面板按 name+keys 子串搜,搜任一说法都命中这一个入口。
    # simple=True 必须显式声明:全角色默认简洁模式,否则坐席根本搜不到(本项要解的正是「找不到」)。
    "work_goal": dict(key="", path="/workspace?card=goal", icon="target",
                      simple=True, label_key="nav_work_goal",
                      label_zh="工作目标（工作计划）", help="work_goal",
                      cmd_keys="工作目标 工作计划 营销目标 目标 计划 推进 里程碑 今日拍 "
                               "goal goals plan milestone agenda"),
}

DOMAIN_SENTINEL = "__domain_pages__"

# ── 完整模式分组 ─────────────────────────────────────────────────────────────
NAV_GROUPS_FULL = [
    # 域动态页哨兵在「工作台」组尾：支付域渠道/汇率等属日常业务面；且哨兵不能
    # 单独成组——无域包的部署会渲染出空分组标题。
    dict(label_key="section_workbench", label_zh="工作台",
         items=["workspace", "cases", "care", "relations_health", DOMAIN_SENTINEL]),
    # 真机矩阵（原「渠道自动化」，2026-08-03 更名）：矩阵总览 + 四渠道 + 群脉导播
    # ——群脉指挥的就是同一批矩阵账号，归组随矩阵。
    dict(label_key="section_channels", label_zh="真机矩阵",
         items=["rpa_overview", "telegram", "line_rpa", "messenger_rpa",
                "whatsapp_rpa", "group_show"]),
    dict(label_key="section_ai_kb", label_zh="AI 与知识",
         items=["personas", "reply_settings", "knowledge", "learner",
                "episodic", "strategies", "strategy_analytics"]),
    dict(label_key="section_insights", label_zh="数据洞察",
         items=["dash", "ops", "analytics", "funnel", "monetization"]),
    dict(label_key="section_compliance", label_zh="安全合规",
         items=["crisis_audit", "audit"]),
    dict(label_key="section_system", label_zh="系统管理", master_only=True,
         items=["users", "settings", "membership", "diff", "logs", "developer"]),
    dict(label_key="section_support", label_zh="支持",
         items=["personal_settings", "help"]),
]

# ── 简洁模式 ────────────────────────────────────────────────────────────────
# 定位（2026-08-03 精简）：简洁模式＝值班/看店视角（坐席+店主每天要碰的），
# 完整模式＝配置/运维/增长视角。真机矩阵五项、人设工作室与分析/审计/记账类页
# 只在完整模式渲染；URL 不封（书签/深链仍可达），命令面板按 simple 标注兜底可搜。
# 危机审计刻意留在折叠区：红色徽标是简洁模式用户唯一的危机可见通道，安全项不藏。
SIMPLE_CORE = ["workspace", "cases", "care", "knowledge", DOMAIN_SENTINEL,
               "reply_settings", "escalation"]
SIMPLE_MORE = ["dash", "learner", "crisis_audit", "personal_settings", "help"]

# 真机矩阵成员（简洁模式上下文导航用：深链进矩阵页时侧栏就地渲染本组，保住
# 组内互切与当前页高亮；base.html 与 _ws_sidebar.html 经 nav_matrix_items 消费）。
MATRIX_ITEM_IDS = ("rpa_overview", "telegram", "line_rpa", "messenger_rpa",
                   "whatsapp_rpa")


def _resolve(ids):
    return [i if i == DOMAIN_SENTINEL else NAV_ITEMS[i] for i in ids]


def _cmd_items():
    """命令面板页面项:完整模式顺序 + 简洁专属项 + 面板专属页。

    simple 默认按简洁清单推导,项内显式声明 simple 则以其为准(面板专属页无侧栏行,
    只能这样进简洁模式的面板)。
    """
    simple_ids = set(SIMPLE_CORE) | set(SIMPLE_MORE)
    seen, out = set(), []

    def add(item_id, item):
        if item_id in seen:
            return
        seen.add(item_id)
        d = dict(item)
        d["simple"] = bool(item.get("simple", item_id in simple_ids))
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
    nav_matrix_items=_resolve(list(MATRIX_ITEM_IDS)),
    nav_cmd_items=_cmd_items(),
)


def _drop_locked(items, locked):
    """过滤掉 feature 被锁定的菜单项(命令面板用;哨兵与无标签项原样保留)。"""
    return [
        it for it in items
        if it == DOMAIN_SENTINEL
        or not (isinstance(it, dict) and it.get("feature") in locked)
    ]


def _mark_locked(items, locked):
    """给 feature 被锁定的菜单项打 locked=True 注解(拷贝,绝不改单例)。

    P3 语义：侧栏锁定项不消失,渲染为「锁标 + 跳 /membership 升级引导」
    (base.html nav_item 宏消费 locked 字段)——市场面保留可见的升级面,
    也避免「点进被锁页面 → API 全 403」的死路体验。
    """
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("feature") in locked:
            out.append(dict(it, locked=True))
        else:
            out.append(it)
    return out


# ── E3：页面路径 → 授权功能族（中间件页面守卫用） ─────────────────────────────
# 与 nav 可见性同一事实源（NAV_ITEMS 的 feature 标注）：锁定项侧栏渲染锁标跳
# /membership，直连 URL 也一致 302 过去——nav 藏了但 URL 仍能打开半残页（API 全
# 403）的缝隙从此闭合，且两个面永不漂移（新页面挂进 NAV_ITEMS 带 feature 即自动
# 双面生效）。表按前缀长度降序（最长优先），匹配按整段边界（/knowledge 匹配
# /knowledge 与 /knowledge/*，不误伤 /knowledgebase）。
_PAGE_FEATURE_PREFIXES = tuple(sorted(
    ((str(it["path"]), str(it["feature"]))
     for it in NAV_ITEMS.values()
     if isinstance(it, dict) and it.get("feature") and it.get("path")),
    key=lambda pf: -len(pf[0]),
))


def feature_for_page_path(path: str):
    """页面路径 → 所属授权功能族；无归属 → None（不守卫）。纯函数零 IO。"""
    p = str(path or "")
    for prefix, feat in _PAGE_FEATURE_PREFIXES:
        if p == prefix or p.startswith(prefix + "/"):
            return feat
    return None


def get_nav_context(config: dict = None) -> dict:
    """供 admin.py _enrich_context 与渲染类测试注入模板上下文。

    不传 config / 档位闸门关 / 无锁定项 → 返回静态全量(进程内单例,零变化);
    传 config 且有锁定功能 → 侧栏视图锁定项带 locked=True 注解(锁标渲染),
    命令面板视图直接隐藏锁定项。视图每次重建(列表极小,开销可忽略),
    feature_gate 侧异常一律回落全量。
    """
    if config is None:
        return _NAV_CONTEXT
    try:
        from src.licensing.feature_gate import gate_enabled, locked_features
        if not gate_enabled(config):
            return _NAV_CONTEXT
        locked = set(locked_features(config))
    except Exception:
        return _NAV_CONTEXT
    if not locked:
        return _NAV_CONTEXT
    groups = [dict(g, items=_mark_locked(g["items"], locked))
              for g in _NAV_CONTEXT["nav_groups"]]
    return dict(
        _NAV_CONTEXT,
        nav_groups=groups,
        nav_simple_core=_mark_locked(_NAV_CONTEXT["nav_simple_core"], locked),
        nav_simple_more=_mark_locked(_NAV_CONTEXT["nav_simple_more"], locked),
        nav_matrix_items=_mark_locked(_NAV_CONTEXT["nav_matrix_items"], locked),
        nav_cmd_items=_drop_locked(_NAV_CONTEXT["nav_cmd_items"], locked),
    )
