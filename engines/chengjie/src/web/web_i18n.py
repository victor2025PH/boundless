"""Web 面板多语言 — 翻译字典 v2

热加载（2026-07，前端开发免重启闭环的最后一块）：本文件是纯数据字典 + 3 个消费函数，
改键此前必须整进程重启（~30s 全站不可用窗口）。现 ``get_translations``/``t``/``tr``
入口带 mtime 探测（2s 节流）——文件变化时在 fresh namespace 里 exec 自身源码、原子替换
``_TRANSLATIONS``。配合模板 auto_reload（admin.py），**模板 + i18n 键的前端改动刷新
浏览器即生效**。安全性：``_TRANSLATIONS`` 仅本模块函数引用（无外部直接 import 数据）；
坏保存态（语法错/缺语言）保留旧字典并告警，绝不崩服务；替换是单引用赋值（原子）。
"""

import logging as _logging
import threading as _threading
import time as _time
from pathlib import Path as _Path

_logger = _logging.getLogger(__name__)

_TRANSLATIONS = {
    "zh": {
        # ── 品牌 & 导航 ──────────────────────────────
        "brand": "无界科技 · 智聊",
        "brand.product": "智聊",
        "brand.login_line": "智聊 · 管理控制台",
        "brand.setup_line": "智聊 · 初始化向导",
        "brand.sidebar": "无界 · 智聊",
        "brand.company": "无界科技",
        "brand.login_subtitle_default": "管理控制台",
        "dashboard": "数据概览",
        "templates": "话术模板",
        "channels": "通道管理",
        "channels_status": "通道状态",
        "strategies": "回复策略",
        "strategy_analytics": "策略效果",
        "audit": "操作记录",
        "diff": "版本对比",
        "analytics": "运营分析",
        "cases": "案例跟进",
        "logs": "实时日志",
        "help": "帮助",
        "help_center": "帮助中心",
        "escalation": "人工转接",
        "developer": "开发者工具",
        "import_page": "导入配置",
        "users": "用户管理",
        # ── 侧栏导航补全（③-S3：原硬编码 span 收口为 key）──
        "personas": "人设工作室",
        "workspace_inbox": "坐席工作台",
        # rpa_* → i18n_packs/rpa_shared.py(P5 迁移)
        "telegram_settings": "Telegram 自动化",
        "line_rpa": "LINE 自动化",
        "messenger_rpa": "Messenger 自动化",
        "whatsapp_rpa": "WhatsApp 自动化",
        "episodic": "AI 记忆",
        "crisis_audit": "危机审计",
        "care": "主动关怀",
        "relations_health": "流失预警",
        "monetization": "变现营收",
        "ai_studio": "AI 工作室",
        "viewer_badge": "只读",
        "viewer_badge_title": "当前账号为只读观察员，无法修改配置",
        # ── 分区标签 ─────────────────────────────────
        "section_daily": "日常工作",
        "section_ops": "运营中心",
        "section_ai": "AI 与策略",
        "section_data": "数据分析",
        "section_data_records": "数据 & 记录",
        "section_system": "系统管理",
        # ── 账户 & 权限 ───────────────────────────────
        "logout": "退出",
        "logout_full": "退出登录",
        "change_pwd": "修改密码",
        "role_master": "主帐号（全部权限）",
        "role_admin": "管理员（编辑权限）",
        "role_viewer": "观察员（只读）",
        # ── 密码弹窗 ──────────────────────────────────
        "pwd_title": "修改密码",
        "pwd_old": "当前密码",
        "pwd_new": "新密码（至少 6 位）",
        "pwd_confirm": "确认新密码",
        "pwd_submit": "确认修改",
        # ── 通用操作 ──────────────────────────────────
        "save": "保存",
        "cancel": "取消",
        "confirm": "确认",
        "delete": "删除",
        "edit": "编辑",
        "add": "添加",
        "search": "搜索",
        "search_placeholder": "搜索页面、功能…",
        "export": "导出",
        "import": "导入",
        "refresh": "刷新",
        "back": "返回",
        "close": "关闭",
        "enable": "启用",
        "disable": "禁用",
        "yes": "是",
        "no": "否",
        # ── 状态 & 标签 ───────────────────────────────
        "status_running": "运行中",
        "status": "状态",
        "time": "时间",
        "action": "操作",
        "target": "目标",
        "operator": "操作人",
        "no_data": "暂无数据",
        "loading": "加载中…",
        # ── 仪表盘 ────────────────────────────────────
        "uptime": "运行时长（小时）",
        "template_count": "话术模板",
        "channel_count": "通道数量",
        "sys_status": "系统状态",
        "quick_actions": "快速操作",
        "export_config": "导出配置",
        "import_config": "导入配置",
        "view_audit": "操作记录",
        "diff_page": "版本对比",
        "channel_health": "通道健康度",
        "manage_channels": "管理通道",
        "recent_ops": "最近操作",
        "view_all": "查看全部",
        # ── 通知 ──────────────────────────────────────
        "notifications": "通知",
        "notif_empty": "暂无通知",
        "notif_mark_all": "全部已读",
        "notif_strategy": "策略告警",
        "notif_system": "系统事件",
        # ── 登录 ──────────────────────────────────────
        "login_title": "登录 智聊",
        "login_btn": "登录",
        "token_error": "Token 错误",
        # ── 统计 ─────────────────────────────────────
        "total_events": "总事件数",
        "avg_response": "平均响应时间",
        # ── 语言切换 ──────────────────────────────────
        "lang_switch": "English",
        "lang_current": "简体中文",
        # ── 新手引导 ──────────────────────────────────
        "tour_skip": "跳过引导",
        "tour_next": "下一步",
        "tour_done": "完成",
        "tour_prev": "上一步",
        "tour_step": "步骤",
        "tour_of": "/",
        # ── 快捷键 ────────────────────────────────────
        "shortcuts_title": "键盘快捷键",
        "shortcuts_nav": "导航",
        "shortcuts_actions": "操作",
        # ── UI 模式 ──────────────────────────────────
        "mode_simple": "简洁模式",
        "mode_full": "完整模式",
        "mode_simple_short": "简洁",
        "mode_full_short": "完整",
        "switch_mode": "切换模式",
        "switch_to_full": "切换完整模式",
        "switch_to_simple": "切换简洁模式",
        "click_switch_full": "点击切换到完整模式",
        "click_switch_simple": "点击切换到简洁模式",
        "knowledge": "知识库",
        "learner": "学习队列",
        "system_settings": "系统设置",
        "lang_switch_label": "切换语言",
        # ── 命令面板（③-S3：Ctrl+K 面板 JS 文案）──────
        "cmd_reload": "刷新当前页面",
        "switch_theme": "切换主题",
        "cmd_no_match": "没有匹配的结果",
        # ── 快捷键弹窗（③-S3：sc-desc 行 + 侧栏折叠提示）──
        "sidebar_toggle": "折叠/展开侧栏",
        "cmd_palette": "命令面板",
        "sc_save": "保存（模板页）",
        "sc_close_modal": "关闭弹窗",
        "sc_show_help": "显示快捷键帮助",
        # ── 术语提示开关（③-S4：全局 tooltip 引擎 chrome）──
        "tip_toggle_title": "左键=开关术语提示 · 右键=隐藏按钮 · 可拖动",
        "tip_hidden_toast": "术语按钮已隐藏（刷新页面恢复）",
        # ── 坐席工作台·数据看板（P1 i18n 收口 step①）────
        # dash.* → i18n_packs/dashboard_shell.py(P5 迁移)
        # ── 数据看板·静态骨架（P1 i18n step③-K）────────────
        # ── 数据看板·JS 动态串（P1 i18n step③-K2）──────────
        # 共用状态/单位
        # 客户阶段（沿用看板既有口径：留资/引流）
        # SLA 口径
        # 风险分级
        # 明细弹窗 / 生成跟进
        # 升级面板
        # 风险草稿分布（loadRisk）
        # 系统指标 / 趋势 / 我的绩效 / 排行榜（K2b）
        # 负荷 / 质量&KB / 工作区 / 简报 / 在线状态（K2c）
        # 计数助手 / 引导 / 健康 / 跨语言 / 一键发(K2d)
        # ── 收件箱·顶栏（P1 i18n step②-B）────────────────
        # inbox.* → i18n_packs/inbox_workspace.py(P5 迁移)
        # ── 草稿审批工作台（P1 i18n step③）──────────────
        # draft.* → i18n_packs/draft_review_page.py(P5 迁移)
        # ── 草稿审批·JS 动态串（P1 i18n step③-L）──────────
        # 共用
        # 风险分级（审批口径）
        # 批量栏 / 快捷键提示（静态）
        # 卡片正文
        # 面板标题
        # 编辑 / 发送
        # 处置
        # Copilot
        # 对话上下文
        # 翻译面板
        # 批量操作
        # 键盘快捷键 alert
        # 对话智能徽章
        # 客户画像
        # 模板库
        # 模板语言筛选 chip 是「语言身份」标记，按惯例两套 locale 都用母语原文（中文恒「中文」、
        # 日文恒「日本語」），与 EN 同理；走 T() 仅为统一过 i18n 门禁，非随界面语言变。
        # 快捷键面板
        # 质量评分明细
        # KB 归档
        # SSE 事件 toast
        # ── 收件箱·业务助手面板 + 骨架态（P1 i18n step③-B）──
        # ── P1 按需拉取更早历史（从手机补拉）+ 通讯录/好友名单面板 ──────────
        # ── 收件箱·右栏内容行（P1 i18n step③-C）──────────
        # ── 收件箱·toast/confirm 海（P1 i18n step③-D）─────
        # ── 收件箱·关系阶段 / 工作链通知（P1 i18n step③-E）──
        # ── 收件箱·媒体/档位/接管/草稿/NBA/slash 引用（P1 i18n step③-F）──
        # ── 收件箱·平台自动化降级提示条（P0-5 可见化）──
        # ── 收件箱·批量/默认语言/账号/群@/历史/视图（P1 i18n step③-G）──
        # ── 收件箱·语音克隆管理（P1 i18n step③-H · 登记/改绑/试听）──
        # 对账/回收（reconcile/purge）
        # ── 收件箱·静态壳余项 + 主题 + 平台/漏斗/时间（P1 i18n step③-M1/M2）──
        # ── 收件箱·会话列表/账号条/群组动态（P1 i18n step③-M3）──
        # ── 收件箱·批量条/会话头/安全条/输入区/翻译弹层（P1 i18n step③-M4）──
        # ── 二期：Composer 一体化 AI 草稿条 ──
        # ── 二期：会话行「谁在处理」微标 ──
        # ── 扫码接入 · Telegram 两步验证（云密码）──
        # ── 收件箱·账号抽屉 + 扫码接入向导（P1 i18n step③-M5）──
        # ── 收件箱·会话头/消息媒体/标签下拉/确认弹窗（P1 i18n step③-M6）──
        # ── 收件箱·只读/能力/消息搜索/注解/消息气泡/发送 toast（P1 i18n step③-M7）──
        # ── 收件箱·全自动记录/草稿小卡/认领/语音（P1 i18n step③-M8）──
        # ── 收件箱·默认译文/回复语言管理器（P1 i18n step③-M9）──
        # ── 收件箱·翻译引擎徽标/多线路对照（P1 i18n step③-M10）──
        # ── P0-2：翻译置信度暴露（对照候选分档徽标 + 单条低置信提示）──
        # ── 收件箱·手动单条翻译/会话内媒体翻译（P1 i18n step③-M11）──
        # ── 收件箱·图片/语音/文档翻译面板（P1 i18n step③-M12）──
        # ── 收件箱·快捷模板/知识库/账号抽屉/编排器/登录方式（P1 i18n step③-M13）──
        # ── 收件箱·译文状态胶囊/实时@提醒/兜底（P1 i18n step③-M14）──
        # ── 工作台页面标题（<title>，服务端渲染首屏即对，P1 i18n step③-N）──
        # base.* → i18n_packs/workspace_shell.py(P5 迁移)
        # ── 工作台共享壳·顶栏/菜单/用户区（P1 i18n step③-I）──
        # ── 工作台壳·SLA 下钻面板 / 确认弹框 / reason 映射（P1 i18n step③-J）──
        # ── 工作台壳·告警/通知偏好弹窗（P1 i18n step③-J）──
        "crmw.unit.sec": "{n}秒",
        "crmw.unit.min": "{n}分",
        "crmw.unit.min_dec": "{n}分",
        "crmw.unit.hour": "{n}时",
        "crmw.unit.hour_dec": "{n}时",
        "crmw.unit.day": "{n}天",
        # ── 实时语音试拨页 voice_call.html（P33 静态层）──
        "rvc.title": "实时语音通话 · 陪伴",
        "rvc.h1": "实时语音通话",
        "rvc.engine_pill": "MiniCPM-o · 全双工",
        "rvc.help_t": "使用说明",
        "rvc.step_pick": "选人设",
        "rvc.step_engine": "引擎就绪",
        "rvc.step_call": "接通",
        "rvc.lbl_pick": "① 选择人设",
        "rvc.loading_personas": "加载人设中…",
        "rvc.btn_listen": "🔊 试听",
        "rvc.hint_pick": "选一个人设即可试听其声音",
        "rvc.btn_upload": "🎙️ 上传真人声",
        "rvc.btn_remove": "移除",
        "rvc.lbl_lang": "语言",
        "rvc.lang_zh": "中文",
        "rvc.adv_toggle": "⚙ 高级选项（会话记忆 / 访问口令）",
        "rvc.lbl_chatkey": "会话标识（选最近会话带入 TA 的长期记忆，或保持全新对话）",
        "rvc.chatkey_fresh": "✨ 全新对话（不带入记忆）",
        "rvc.chatkey_ph": "如 tg:8244899900",
        "rvc.lbl_token": "访问口令（access_token · 仅公网部署需要）",
        "rvc.token_ph": "服务器要求时填写",
        "rvc.lbl_engine": "语音引擎 · 显存按需",
        "rvc.engine_detecting": "检测中…",
        "rvc.btn_engine_start": "启动引擎",
        "rvc.status_idle": "未连接",
        "rvc.guide_title": "3 步开始通话 🎧",
        "rvc.guide_s1_t": "选人设",
        "rvc.guide_s1_b": "挑一个头像，就是要陪你聊天的 TA",
        "rvc.guide_s2_t": "启动引擎",
        "rvc.guide_s2_b": "点「启动引擎」按需载入显存（约 10–60 秒）",
        "rvc.guide_s3_t": "接通通话",
        "rvc.guide_s3_b": "就绪后点「和 TA 通话」，开口即可对话",
        "rvc.guide_ok": "开始使用",
        "rvc.btn_call": "📞 接通",
        "rvc.btn_hang": "挂断",
        # ── 工作台壳·通知中心 + SSE 事件文案 + 全局搜索（P1 i18n step③-J）──
        # ── 工作台壳·剩余 JS 块（漏斗/在线/合并待审/授权/L4 草稿，J4）──
        "lang_toggle": "English",
        # ── Case 面板正文（③-S5：cases.html 静态 + JS 动态串）──
        "cases_title": "案例跟进",
        "cases_guide_title": "操作说明",
        "cases_guide_body": "每个案例卡片显示用户消息和 AI 回复。你可以添加<strong>备注</strong>标记重要信息，处理完毕后点击<strong style=\"color:var(--green)\">结案</strong>。列表每 30 秒自动刷新。",
        "cases_guide_dismiss": "知道了，不再显示",
        "cases_stat_total": "总 Case",
        "cases_stat_active": "活跃",
        "cases_risk": "高风险",
        "cases_empty_title": "当前没有待处理的案例",
        "cases_empty_hint": "系统每 30 秒自动检查新案例，也可以点击「刷新」手动检查",
        "cases_empty_full": "暂无活跃 Case",
        "cases_escalated": "已升级",
        "cases_closed": "已结案",
        "cases_satisfaction": "满意度",
        "cases_consecutive": "连续追问",
        "cases_user": "用户",
        "cases_group": "群",
        "cases_note": "备注",
        "cases_note_ph": "添加备注…",
        "cases_save_note": "保存备注",
        "cases_close": "结案",
        "cases_load_fail": "加载失败",
        "cases_load_fail_hint": "请稍后点击「刷新」重试。如果问题持续，请联系技术支持。",
        "cases_close_prompt": "请输入结案说明（可为空）：",
        # ── 简洁模式·新手引导弹窗（③-S5：base.html simple onboard-modal，原仅 full 态密封）──
        "onb_welcome_title": "欢迎使用简洁模式",
        "onb_welcome_desc": "系统已为你精简了操作界面，专注于最常用的功能：",
        "onb_cases_label": "案例跟进",
        "onb_cases_desc": "查看和处理客户案例",
        "onb_kb_label": "知识库",
        "onb_kb_desc": "浏览和搜索知识条目",
        "onb_review_label": "学习队列",
        "onb_review_desc": "审核 AI 生成的草稿",
        "onb_footer": "你随时可以在侧边栏底部切换到完整模式",
        "onb_start": "开始使用",
        # ── 实时日志页正文（③-S6：logs.html 工具栏 + 终端 + SSE 重连）──
        "logs_adv_title": "高级功能",
        "logs_adv_desc": "实时日志供技术排查使用。",
        "logs_back_cases": "返回案例",
        "logs_search_ph": "关键词过滤…",
        "logs_clear_filter": "清除过滤",
        "logs_live": "实时流",
        "logs_count_init": "0 行",
        "logs_line_count": "{n} 行",
        "logs_pause": "暂停",
        "logs_resume": "继续",
        "logs_paused": "已暂停",
        "logs_clear_title": "清空当前显示的日志（不影响服务器日志文件）",
        "logs_clear": "清空",
        "logs_download": "下载",
        "logs_follow": "跟随",
        "logs_no_match": "无匹配日志",
        "logs_reconnect": "重连中…（第 {n} 次 / {s}s）",
        # ── 运营分析页正文（③-S7：刷新栏/时间段/统计卡/图表/运营 Copilot）──
        # analytics_* → i18n_packs/analytics_page.py(P5 迁移)
        # ── 人设工作室 personas.html（③-S8a-1：topbar/tabs/dashboard/profiles 静态层）──
        # psn_* → i18n_packs/persona_studio.py(P5 迁移)
        # ── 人设工作室 personas.html（③-S8a-2：bindings/rules/drawer/default/picker 静态层）──
        # ── 人设工作室 P0 改版（卡片头像/就绪清单/试听/撤销）──
        # ── 人设工作室 P1（应用到/新建向导/试聊）──
        # ── 人设工作室 P2（活跃统计/体检台账）──
        # err.* → i18n_packs/errors_stock.py(P5 迁移)
        # ── personas.html JS 层（③-S8b 自动迁移）──
        # ov_* → i18n_packs/rpa_overview_page.py(P5 迁移)
        # msg_* → i18n_packs/messenger_page.py(P5 迁移)
        # msg_tab_voice → i18n_packs/messenger.py(P4 拆分)
        # wa_* → i18n_packs/whatsapp_page.py(P5 迁移)
        # ln_* → i18n_packs/line_page.py(P5 迁移)
        # tg_s/tg_js_* → i18n_packs/telegram_page.py(P5 迁移)
        # tg_gate_* → i18n_packs/telegram_gate.py;fn_export_csv 等 → i18n_packs/funnel.py(P4 拆分)
        # db_* → i18n_packs/dashboard_page.py(P5 迁移)
        # set_* → i18n_packs/settings_page.py(P5 迁移)
        # ── P0-4/C4 授权激活卡 ──
        # kb_* → i18n_packs/knowledge_page.py(P5 迁移)
        # ap_* → i18n_packs/agent_perf_page.py(P5 迁移)
        # ops.* → i18n_packs/ops_shared.py(P5 迁移)
        # as_* → i18n_packs/ai_studio_page.py(P5 迁移)
        # em_* → i18n_packs/episodic_page.py(P5 迁移)
        # lr_* → i18n_packs/learner_page.py(P5 迁移)
        # rh_* → i18n_packs/relations_health_page.py(P5 迁移)
        # mo_* → i18n_packs/monetization_page.py(P5 迁移)
        # c3_* → i18n_packs/contact360_page.py(P5 迁移)
        # sa_* → i18n_packs/strategy_analytics_page.py(P5 迁移)
        # ov2_* → i18n_packs/ops_overview_page.py(P5 迁移)
        # ── P0-3/B9：入站自动译量 KPI（成本护栏观测）──
        # tp_* → i18n_packs/templates_page.py(P5 迁移)
        "tm_s001": "模板库管理",
        "tm_s002": "新建模板",
        "tm_s003": "搜索模板标题或内容…",
        "tm_s004": "全部场景",
        "tm_s005": "订单",
        "tm_s006": "物流",
        "tm_s007": "退款",
        "tm_s008": "投诉",
        "tm_s009": "结束语",
        "tm_s010": "使用次数",
        "tm_s011": "简短描述（如：中文订单确认）",
        "tm_s012": "模板正文。使用",
        "tm_s013": "{变量}",
        "tm_s014": "标记占位符，如",
        "tm_s015": "{订单号}",
        "tm_s016": "物流查询",
        "tm_s017": "产品咨询",
        "tm_s018": "平台（留空",
        "tm_s019": "全平台）",
        "tm_js001": "条模板",
        "tm_js002": "产品",
        "tm_js003": "标题和内容不能为空",
        "tm_js004": "确定删除模板",
        "tm_js005": "此操作不可恢复。",
        "tm_js006": "删除失败（可能需要主管权限）",
        # im_* → i18n_packs/import_page.py(P5 迁移)
        # dv_* → i18n_packs/developer_page.py(P5 迁移)
        # ── 云端凭证体检 / 备用 Key 池卡（P-KeyPool）──────────────
        # wf_* → i18n_packs/workflows_page.py(P5 迁移)
        # st_* → i18n_packs/strategies_page.py(P5 迁移)
        "us_s001": "用户管理",
        "us_s002": "此页面为高级管理功能。",
        "us_s003": "管理后台帐号、角色与权限",
        "us_s004": "观察员",
        "us_s005": "主帐号不可删除或降级",
        "us_s006": "添加子帐号",
        "us_s007": "当前登录中的所有设备，可踢出指定会话",
        "us_s008": "踢出所有其他设备",
        "us_s009": "创建子帐号",
        "us_s010": "英文字母",
        "us_s011": "数字",
        "us_s012": "密码",
        "us_s013": "至少 6 位",
        "us_s014": "只读观察员",
        "us_js001": "角色已更新",
        "us_js002": "确认删除用户",
        "us_js003": "用户已创建",
        "us_js004": "暂无活跃会话",
        "us_js005": "移动设备",
        "us_js006": "浏览器",
        "us_js010": "最后活跃：",
        "us_js009": "踢出",
        "us_js007": "已踢出该设备",
        "us_js008": "确认踢出所有其他设备？",
        "us_kicked_n": "已踢出 {n} 个设备",
        "role_agent": "坐席（仅聊天工作台）",
        "au_s002": "操作活动热力图",
        "au_s003": "最近 84 天",
        "au_s004": "每格代表一天，颜色越深表示操作次数越多",
        "au_s005": "一",
        "au_s006": "二",
        "au_s007": "三",
        "au_s008": "四",
        "au_s009": "五",
        "au_s010": "六",
        "au_s011": "快捷筛选",
        "au_s012": "点击一次过滤；再点",
        "au_s013": "复位",
        "au_s014": "筛选条件",
        "au_s015": "导出当前筛选结果为",
        "au_s016": "导出全部记录为",
        "au_s017": "导出筛选结果",
        "au_s018": "导出全部",
        "au_s019": "操作类型",
        "au_s020": "目标 / 关键词",
        "au_s021": "通道名 / 模板名 / 关键词",
        "au_s022": "起始日期",
        "au_s023": "结束日期",
        "au_s024": "操作时间轴",
        "au_s025": "快照",
        "au_s026": "查看变更",
        "au_s027": "旧值：",
        "au_s028": "新值：",
        "au_preset_1": "🛡 大 body 攻击 (413)",
        "au_preset_2": "⚡ 限流命中 (429)",
        "au_preset_3": "📝 字典写入",
        "au_preset_4": "↺ 字典恢复",
        "au_preset_5": "🔄 字典 reload",
        "au_js001": "收起变更",
        "au_js002": "次操作",
        "au_js004": "个活跃日",
        "au_js005": "最高单日",
        "au_js003": "热力图加载失败",
        "au_s001": "操作记录",
        "wr_s001": "经营 ROI 看板",
        "wr_s002": "返回今日概览",
        "wr_s003": "自动应答 vs 人工接管",
        "wr_s004": "拦截未发",
        "wr_s005": "每日 AI（蓝）/ 人工（橙）发送量",
        "wr_s006": "配置健康度",
        "wr_tf_sticky_n": "{n} 个跨天回访（越高越好）",
        "wr_tf_cohort": "新关系 {n} 个的 7 日回访",
        "wr_tf_total_replies": "共 {n} 条回复",
        "wr_tf_est_per": "按每条 {n}s 估算",
        "wr_tf_hour_cost": "按 ¥{n}/人时",
        "wr_tf_conv_rate": "转化率 {n}%",
        "wr_tf_responded": "已响应 {n} 会话",
        "wr_tf_lead_rate": "留资率 {n}%",
        "wr_tf_spark": "{day}：AI {ai} / 人工 {hu}",
        "wr_js001": "活跃关系",
        "wr_js002": "期内有用户互动的关系数",
        "wr_js003": "关系黏性",
        "wr_js004": "人均往来轮次",
        "wr_js005": "关系深度（越高越深）",
        "wr_js006": "留存",
        "wr_js007": "自动应答占比",
        "wr_js008": "节省人力（人时）",
        "wr_js009": "人工接管",
        "wr_js010": "坐席处置后发送",
        "wr_js011": "引流成功",
        "wr_js012": "首响达标率",
        "wr_js013": "新增客户",
        "wr_js014": "配置自检不可用",
        "wr_js015": "警告",
        "wr_js016": "配置正常",
        "wr_js017": "未发现问题",
        "wr_js018": "查看详情",
        "wu_s001": "用量计量",
        "wu_s002": "暂无用量数据（接入渠道并开始收发消息后将逐步积累）。",
        "wu_s003": "每日用量趋势",
        "wu_s004": "消息量",
        "wu_s005": "调用数",
        "wu_s006": "口径：消息量",
        "wu_s007": "收发消息条数；AI 调用数",
        "wu_s008": "生成的 AI 草稿数；活跃坐席",
        "wu_s009": "窗口内有处置记录的去重坐席数。",
        "wu_s010": "计费对账单",
        "wu_s011": "数量",
        "wu_s012": "应收金额",
        "wu_s013": "该账期暂无用量数据。",
        "wu_tf_spark": "{day}：消息 {m} / AI {ai}",
        "wu_tf_meta1": "账期 {period} · 套餐 {plan}",
        "wu_tf_meta_cust": " · 客户 {c}",
        "wu_tf_meta_seats": " · 席位 {seats}（活跃 {active}）",
        "wu_tf_meta_over": " · ⚠ 超额 {n}",
        "wu_tf_total": "合计 {cur} {n}",
        "wu_js001": "无环比",
        "wu_js002": "（社区模式）",
        "wu_js003": "超额",
        "wu_js004": "消息总量",
        "wu_js005": "本月消息配额",
        "wu_js006": "暂无趋势",
        "aq_s001": "AI 回复质量",
        "aq_s002": "回复质量",
        "aq_s003": "暂无 AI 草稿处置数据（接入渠道",
        "aq_s004": "开启 AI 自动应答后将逐步积累）。",
        "aq_s005": "处置构成（窗口内全部 AI 草稿）",
        "aq_s006": "自动通过率趋势",
        "aq_s007": "每日 autosend / 当日处置总数",
        "aq_s008": "知识库改进候选（AI 答错被改写/拒绝的会话）",
        "aq_s009": "把这些「人工纠正过」的问答一键沉淀为知识，AI 下次就会了。",
        "aq_js001": "批准发",
        "aq_js002": "改写后发",
        "aq_js003": "强制放行",
        "aq_js004": "拦截",
        "aq_js005": "暂无处置",
        "aq_js006": "自动通过率",
        "aq_js007": "人工改写率",
        "aq_js008": "拒绝率",
        "aq_js009": "高风险占比",
        "aq_js010": "总处置数",
        "aq_js011": "暂无候选（近期无被改写/拒绝的草稿）。",
        "aq_js012": "坐席答案：",
        "aq_js013": "无改写答案（转入后由 AI 自动填充初稿，再人工校对）",
        "aq_js014": "转为知识条目",
        "aq_js015": "转入中…",
        "aq_js016": "已建条目",
        "aq_js017": "（AI 正在填充初稿）",
        "aq_js018": "已转入",
        "aq_tf_spark": "{day}：{rate}%（{a}/{t}）",
        "qm_s001": "实时队列看板",
        "qm_s002": "实时运营队列看板",
        "qm_s003": "手动刷新",
        "qm_s004": "待处理会话",
        "qm_s005": "未读消息",
        "qm_s006": "严重超时",
        "qm_s007": "平均等待",
        "qm_s008": "卡片视图",
        "qm_s009": "表格视图",
        "qm_s010": "最长等待",
        "qm_s011": "负载",
        "qm_s012": "重新分配会话",
        "qm_s013": "的会话分配给：",
        "qm_s014": "确认分配",
        "qm_s015": "将",
        "qm_tf_will_assign": "将分配 {n} 个会话",
        "qm_tf_pending_opt": " ({n} 个待处理)",
        "qm_tf_assigned_to": "已分配 {n} 个会话给 {agent}",
        "qm_js001": "暂无坐席在线",
        "qm_js002": "均等待",
        "qm_js003": "重新分配",
        "qm_js004": "暂无其他在线坐席可分配",
        "qm_js005": "没有找到可分配的会话",
        "qm_js006": "分配失败：",
        "qm_js_min": "分",
        "cs_s001": "主动关怀",
        "cs_s002": "主动关怀待办",
        "cs_s003": "记住对方说的事，到点主动关心 · 需开启",
        "cs_s004": "关怀主题",
        "cs_s005": "面试 / 复查 …",
        "cs_s006": "多少小时后",
        "cs_s007": "手动加一条",
        "cs_s008": "待关怀",
        "cs_s009": "主题",
        "cs_s010": "原话摘要",
        "cs_s011": "到期",
        "cs_tf_count": "共 {n} 条（按到期升序）",
        "cs_js001": "立即发",
        "cs_js002": "联系人 key 和关怀主题必填",
        "cs_js003": "立即发：将提前到现在，下个派发轮次即处理（仍走发送护栏）。继续？",
        "cs_js004": "取消这条关怀待办？",
        "cs_js005": "取消失败：",
        "ca_s001": "危机审计",
        "ca_s002": "危机事件审计",
        "ca_s003": "安全链留痕 · 需开启",
        "ca_s004": "筛选用户",
        "ca_s005": "前缀",
        "ca_s006": "仅未处理",
        "ca_s007": "连击",
        "ca_s008": "标记",
        "ca_s009": "标记已处理",
        "ca_s010": "处置备注（可选）：已电话联系 / 已转人工 / …",
        "ca_s011": "确认处置",
        "ca_tf_evt_user": "事件 #{id} · 用户 {user}",
        "ca_js001": "本页",
        "ca_js002": "未处理合计",
        "ca_js003": "最近危机事件（按时间倒序）",
        "ca_js004": "未升级",
        "ca_js005": "处置失败",
        "ca_js006": "已升级",
        "su_s001": "首次使用 · 初始化向导",
        "su_s002": "账户",
        "su_s003": "创建管理员账户",
        "su_s004": "确认密码",
        "su_s005": "再次输入",
        "su_s006": "配置 AI API 后，知识库语义搜索和自动翻译功能将自动启用。稍后也可在配置文件中修改。",
        "su_s007": "完成初始化",
        "su_s008": "初始化完成！",
        "su_s009": "账户已创建，正在跳转到登录页…",
        "su_s010": "前往登录",
        "su_s011": "深色 / 浅色",
        "su_s000": "初始化设置",
        "su_js_001": "请先填写",
        "su_js_002": "请填写用户名",
        "su_js_003": "密码至少 6 位",
        "su_js_004": "两次输入的密码不一致",
        "su_js_005": "初始化中…",
        "su_js_006": "初始化失败",
        "su_js_reqfail": "请求失败：",
        "su_tf_created": "账户「{u}」已创建，3 秒后跳转…",
        "sw_js_001": "已就绪",
        "sw_js_002": "已填，待启用/登录",
        "sw_js_003": "已填，留空保持不变",
        "sw_js_004": "填好凭证保存后，前往",
        "sw_js_005": "扫码 / 验证码登录该平台账号。",
        "sw_js_006": "无需凭证，开启即用。",
        "sw_js_007": "保存并启用",
        "sw_js_008": "已保存生效",
        "sw_s001": "渠道接入向导",
        "sw_s002": "选择渠道 → 填入凭证（即时校验）→ 保存生效。凭证写入",
        "sw_s003": "，不改动主配置注释。",
        "sw_s006": "已就绪",
        "sw_s007": "渠道接好后，别忘了给 AI 喂知识",
        "sw_s008": "知识库冷启动（一键播种起步话术）",
        "sw_js_opt": "（可选）",
        "sw_tf_miss": "缺 {n} 项",
        "sw_tf_cur": "当前：{v}（留空则不变）",
        "sw_tf_ready": "已就绪 {r} / {t}",
        "el_js_001": "无人认领",
        "el_js_002": "坐席离线",
        "el_js_003": "坐席静音/免打扰",
        "el_js_004": "升级总数",
        "el_js_005": "已接管",
        "el_js_006": "平均接管时延",
        "el_js_007": "该窗口暂无升级记录",
        "el_js_008": "未接管",
        "el_js_009": "收件箱未启用",
        "el_s001": "升级历史",
        "el_s002": "升级历史 / 接管时延",
        "el_tf_taken": "接管 {d}",
        "el_tf_wait": "等待 {d}",
        "el_tf_holder": "原认领 {h}",
        "lg_s001": "管理控制台",
        "lg_s002": "使用 Token 登录",
        "lg_s003": "请输入访问令牌",
        "lg_s004": "使用帐号密码登录",
        "lg_s005": "切换深色 / 浅色",
        "lg_s000": "登录",
        "lg_s006": "管理密码",
        "da_s001": "草稿审计日志",
        "da_s002": "返回草稿工作台",
        "da_s003": "草稿 ID（可选）",
        "da_s004": "坐席 ID（可选）",
        "da_s005": "动作",
        "da_s006": "原因",
        "da_o_all": "全部动作",
        "da_o_blocked": "blocked（拦截）",
        "da_o_force": "force_override（强制放行）",
        "da_o_autosend": "autosend（自动发送）",
        "da_o_approved": "approved（批准）",
        "da_o_rejected": "rejected（拒绝）",
        "da_tf_count": "共 {n} 条记录",
        "diff_js_001": "配额",
        "diff_js_002": "加载失败，请刷新",
        "diff_js_003": "暂无快照",
        "diff_js_004": "快速对比：",
        "diff_js_005": "该配置文件快照不足两个",
        "diff_js_006": "暂无该配置快照",
        "diff_js_007": "确认回滚",
        "diff_s001": "版本对比供管理员追溯配置变更。",
        "diff_s002": "快照时间轴",
        "diff_s003": "旧版本",
        "diff_s004": "新版本",
        "diff_s005": "点击选择",
        "diff_s006": "快速对比：",
        "diff_s007": "选择快照",
        "diff_s008": "当前配置",
        "diff_s009": "对比",
        "diff_s010": "回滚到旧版本",
        "diff_s011": "清除选择",
        "diff_s012": "对比结果",
        "diff_s013": "两个版本内容完全相同，无差异",
        "diff_s014": "行新增",
        "diff_s015": "行删除",
        "diff_s016": "请选择两个快照版本进行对比",
        "diff_s_lblA": "旧版本（A — Before）",
        "diff_s_lblB": "新版本（B — After）",
        "diff_tf_latest2": "{label}：最新两版本",
        "diff_tf_latestcur": "{label}：最新 vs 当前",
        "diff_js_rollback_confirm": "回滚确认",
        "diff_tf_rollback_q": "确定回滚到快照「{id}」？<br>当前配置将被覆盖（系统已自动备份）。",
        "diff_js_rolledback": "已回滚: ",
        "ks_js_001": "知识库不可用",
        "ks_js_002": "偏冷，建议播种",
        "ks_js_003": "已有知识",
        "ks_js_004": "一键播种",
        "ks_js_005": "播种中…",
        "ks_js_006": "播种失败",
        "ks_js_007": "再次播种",
        "ks_s001": "知识库冷启动",
        "ks_s002": "返回接入向导",
        "ks_s003": "渠道接好了，但 AI 还没有可答的私域知识。选一个场景一键播种起步话术，随后到知识库微调。",
        "ks_s004": "已启用知识条目",
        "ks_s005": "覆盖分类",
        "ks_s006": "播种后建议：",
        "ks_s010": "① 到",
        "ks_s011": "② 用",
        "ks_s008": "按自家业务修改话术；",
        "ks_s009": "输入客户问法，确认 AI 能命中作答。",
        "ks_tf_packcount": "{n} 条起步话术",
        "ks_tf_added": "新增 {added} 条",
        "ks_tf_added_skip": "新增 {added} 条，跳过 {skipped} 条已存在",
        "gl_js_001": "可以上线",
        "gl_js_002": "所有硬性条件已满足，开张吧！",
        "gl_js_003": "可上线，但有建议项",
        "gl_js_004": "硬性条件已满足，黄色项建议尽快完善。",
        "gl_js_005": "暂不可上线",
        "gl_js_006": "存在必须修复的硬性问题，见下方红色项。",
        "gl_tf_sum": "<b style=\"color:var(--tk-ok-ink);\">{ok}</b> 通过　<b style=\"color:var(--tk-warn-ink);\">{warn}</b> 建议　<b style=\"color:var(--tk-danger-ink);\">{fail}</b> 待修",
        "gl_js_action": "处理",
        "tk_js_001": "无期限",
        "tk_js_002": "没有待办任务",
        "tk_js_003": "逾期",
        "tk_js_004": "联系人子系统未启用",
        "tk_s001": "我的待办",
        "tk_s002": "工作台",
        "tk_s003": "跟进待办",
        "tk_s004": "全部坐席",
        "tk_s005": "今天及逾期",
        "tk_s006": "仅逾期",
        "tk_s007": "全部未完成",
        "tk_tf_count": "共 {n} 条 · 到期(我的/全部) {mine}/{all}",
        "tk_tf_snooze1": "+1天",
        "tk_tf_snooze3": "+3天",
        "tk_tf_snooze7": "+1周",
        "cl_js_001": "无匹配客户",
        "cl_js_002": "已留资",
        "cl_s001": "客户列表",
        "cl_s002": "搜索：名称 / ID / 渠道号码…",
        "cl_s003": "全部客户",
        "cl_s004": "未留资",
        "cl_s005": "全部跟进",
        "cl_s006": "有跟进计划",
        "cl_s007": "上一页",
        "cl_s008": "下一页",
        "cl_o_due": "待跟进(到期)",
        "cl_tf_all_n": "全部 {n}",
        "cl_tf_pageinfo": "{pg} / {pages} 页 · 共 {total}",
        "atd_js_001": "暂无文件",
        "atd_js_002": "清理中…",
        "atd_js_003": "清理失败",
        "atd_s001": "总体统计",
        "atd_s002": "文件数",
        "atd_s003": "总大小",
        "atd_s004": "文件时效",
        "atd_s005": "最早文件",
        "atd_s006": "最新文件",
        "atd_s007": "超过 24h 的文件建议清理",
        "atd_s008": "前缀分布",
        "atd_s009": "运维操作",
        "atd_s000": "TTS 预览文件仪表盘",
        "atd_s_h1": "预览文件仪表盘",
        "atd_s_back": "返回首页",
        "atd_s014": "自动刷新: 30s |",
        "atd_s_disk": "磁盘占用估算 (假设 1GB 阈值)",
        "atd_s015": "tts-*: 通用 | line-tts-*: LINE | wa-tts-*: WhatsApp",
        "atd_s016": "清理 >1h",
        "atd_s017": "清理 >24h",
        "atd_s018": "清理 >7d",
        "atd_tf_cleaned": "已清理 {n} 个文件",
        # hp_* → i18n_packs/help_page.py(P5 迁移)
        # rvc_* → i18n_packs/voice_clone_page.py(P5 迁移)
        # ── 后端 API 错误文案（P36：请求级本地化，前端 d.detail/d.error 直显）──
        # ── 统一收件箱 发送写路径（P37）──
        # ── 平台扫码登录（P37）──
        # err.login.password_* / err.enable.* → src/web/i18n_packs/errors.py(P4 拆分)
        # ── 跨路由共享错误词表（P38：权限/服务就绪/请求体，全站复用）──
        # ── 草稿审核域（P38）──
        # ── ops_overview 草稿积压治理卡（P38b：恢复被外部改动打破的 seal）──
        # ── 登录/用户管理域（P39；密码类复用 su_js_003 / base.shell.pwd_min_len / token_error）──
        # ── 系统设置域（P39；X格式错误/必须是JSON 参数化收敛）──
        # ── 授权/字符额度域（P0-4 / C4）──
        # ── 情景记忆/跨平台身份域（P40）──
        # ── RPA 跨平台共享错误词表（P40；{platform}/{op} 参数化，line+whatsapp 共用）──
        # ── P43b：messenger_rpa 收口（{dep}/{field}/{key} 参数化 + 复用 op_failed/service_not_started 等）──
        # ── inbox 工作台域（P41；{field} 参数化 + 复用 err.svc.inbox_not_ready）──
        # ── P0-1 桌面首启向导：AI 凭证 overlay 保存 + workspace 引导条 ──
        "setup.ai.saved": "AI 配置已保存",
        # ── 备用 Key 池（P-KeyPool，2026-07-12）────────────────────
        "setup.pool.saved": "备用 Key 池已保存并生效",
        "ws.aiguide.text": "AI 大模型还没配置：填好 API Key 后，翻译和智能回复才能用。",
        "ws.aiguide.cta": "去配置",
        "ws.aiguide.dismiss": "先不管",
        # ── 云端 AI 降级状态条（P-KeyPool D）───────────────────────
        "ws.aidegrade.text": "云端 AI 降级中，回复可能稍慢；系统已自动切换备用通道，无需操作。",
        "ws.aidegrade.mode_pool": "（备用 Key 顶班中）",
        "ws.aidegrade.mode_local": "（本地模型顶班中）",
        "setup.ai.card_title": "AI 大模型",
        "setup.ai.card_sub": "翻译 / 智能回复的引擎。只需一个 OpenAI 兼容 API Key（如 DeepSeek）。",
        "setup.ai.f_key": "API Key",
        "setup.ai.f_base": "接口地址（base_url）",
        "setup.ai.f_model": "模型",
        "setup.ai.btn_test": "测试连接",
        "setup.ai.btn_save": "保存并生效",
        "setup.ai.testing": "正在连接 AI 服务…",
        "setup.ai.test_ok": "连接成功，Key 有效",
        "setup.ai.saving": "正在保存…",
        "setup.ai.saved_ready": "已保存，翻译就绪 ✓",
        "setup.ai.saved_not_ready": "已保存，但连接自检未通过（请核对 Key / 接口地址 / 网络）",
        "setup.ai.st_configured": "已配置",
        "setup.ai.st_missing": "未配置",
        # ── inbox 余部批量（P42；大量复用 err.svc.inbox_not_ready / field_required）──
        # ── P43a：非 inbox 中小文件批量（svc/rpa/voice/persona/ec/tg/case/cp/ca/page）──
        # pma_* → i18n_packs/persona_apply_modal.py(P5 迁移)
    },
    "en": {
        # ── Brand & Nav ──────────────────────────────
        "brand": "Boundless · ChatX",
        "brand.product": "ChatX",
        "brand.login_line": "ChatX · Admin Console",
        "brand.setup_line": "ChatX · Setup Wizard",
        "brand.sidebar": "Boundless · ChatX",
        "brand.company": "Boundless",
        "brand.login_subtitle_default": "Admin Console",
        "dashboard": "Overview",
        "templates": "Templates",
        "channels": "Channels",
        "channels_status": "Channel Status",
        "strategies": "Reply Strategies",
        "strategy_analytics": "Strategy Performance",
        "audit": "Activity Log",
        "diff": "Version Diff",
        "analytics": "Analytics",
        "cases": "Case Follow-ups",
        "logs": "Live Logs",
        "help": "Help",
        "help_center": "Help Center",
        "escalation": "Agent Handoff",
        "developer": "Developer Tools",
        "import_page": "Import Config",
        "users": "User Management",
        # ── Sidebar nav fill-in (③-S3: ex-hardcoded spans → keys) ──
        "personas": "Persona Studio",
        "workspace_inbox": "Agent Workspace",
        # rpa_* → i18n_packs/rpa_shared.py (P5 split)
        "telegram_settings": "Telegram Automation",
        "line_rpa": "LINE Automation",
        "messenger_rpa": "Messenger Automation",
        "whatsapp_rpa": "WhatsApp Automation",
        "episodic": "AI Memory",
        "crisis_audit": "Crisis Audit",
        "care": "Proactive Care",
        "relations_health": "Churn Alerts",
        "monetization": "Monetization",
        "ai_studio": "AI Studio",
        "viewer_badge": "Read-only",
        "viewer_badge_title": "This account is a read-only viewer and cannot modify configuration",
        # ── Sections ─────────────────────────────────
        "section_daily": "Daily Work",
        "section_ops": "Operations",
        "section_ai": "AI & Strategy",
        "section_data": "Data & Analytics",
        "section_data_records": "Data & Records",
        "section_system": "System Admin",
        # ── Account ───────────────────────────────────
        "logout": "Logout",
        "logout_full": "Sign Out",
        "change_pwd": "Change Password",
        "role_master": "Master (Full Access)",
        "role_admin": "Admin (Edit Access)",
        "role_viewer": "Viewer (Read Only)",
        # ── Password modal ────────────────────────────
        "pwd_title": "Change Password",
        "pwd_old": "Current Password",
        "pwd_new": "New Password (min 6 chars)",
        "pwd_confirm": "Confirm New Password",
        "pwd_submit": "Update Password",
        # ── Common actions ────────────────────────────
        "save": "Save",
        "cancel": "Cancel",
        "confirm": "Confirm",
        "delete": "Delete",
        "edit": "Edit",
        "add": "Add",
        "search": "Search",
        "search_placeholder": "Search pages, features…",
        "export": "Export",
        "import": "Import",
        "refresh": "Refresh",
        "back": "Back",
        "close": "Close",
        "enable": "Enable",
        "disable": "Disable",
        "yes": "Yes",
        "no": "No",
        # ── Status & Labels ───────────────────────────
        "status_running": "Running",
        "status": "Status",
        "time": "Time",
        "action": "Action",
        "target": "Target",
        "operator": "Operator",
        "no_data": "No data",
        "loading": "Loading…",
        # ── Dashboard ─────────────────────────────────
        "uptime": "Uptime (hours)",
        "template_count": "Templates",
        "channel_count": "Channels",
        "sys_status": "System Status",
        "quick_actions": "Quick Actions",
        "export_config": "Export Config",
        "import_config": "Import Config",
        "view_audit": "Audit Log",
        "diff_page": "Version Diff",
        "channel_health": "Channel Health",
        "manage_channels": "Manage Channels",
        "recent_ops": "Recent Operations",
        "view_all": "View All",
        # ── Notifications ─────────────────────────────
        "notifications": "Notifications",
        "notif_empty": "No notifications",
        "notif_mark_all": "Mark all read",
        "notif_strategy": "Strategy Alert",
        "notif_system": "System Event",
        # ── Login ─────────────────────────────────────
        "login_title": "Sign in — ChatX",
        "login_btn": "Login",
        "token_error": "Invalid token",
        # ── Stats ─────────────────────────────────────
        "total_events": "Total Events",
        "avg_response": "Avg Response",
        # ── Language ──────────────────────────────────
        "lang_switch": "中文",
        "lang_current": "English",
        # ── Onboarding ────────────────────────────────
        "tour_skip": "Skip Tour",
        "tour_next": "Next",
        "tour_done": "Done",
        "tour_prev": "Back",
        "tour_step": "Step",
        "tour_of": "of",
        # ── Shortcuts ─────────────────────────────────
        "shortcuts_title": "Keyboard Shortcuts",
        "shortcuts_nav": "Navigation",
        "shortcuts_actions": "Actions",
        # ── UI Mode ──────────────────────────────────
        "mode_simple": "Simple Mode",
        "mode_full": "Full Mode",
        "mode_simple_short": "Simple",
        "mode_full_short": "Full",
        "switch_mode": "Switch Mode",
        "switch_to_full": "Switch to Full Mode",
        "switch_to_simple": "Switch to Simple Mode",
        "click_switch_full": "Click to switch to Full Mode",
        "click_switch_simple": "Click to switch to Simple Mode",
        "knowledge": "Knowledge Base",
        "learner": "Learning Queue",
        "system_settings": "System Settings",
        "lang_switch_label": "Language",
        # ── Command palette (③-S3: Ctrl+K palette JS strings) ──
        "cmd_reload": "Reload current page",
        "switch_theme": "Toggle theme",
        "cmd_no_match": "No matching results",
        # ── Shortcuts modal (③-S3: sc-desc rows + sidebar toggle hint) ──
        "sidebar_toggle": "Collapse / expand sidebar",
        "cmd_palette": "Command palette",
        "sc_save": "Save (templates page)",
        "sc_close_modal": "Close dialog",
        "sc_show_help": "Show shortcuts help",
        # ── Term-tip toggle (③-S4: global tooltip engine chrome) ──
        "tip_toggle_title": "Left-click = toggle term tips · Right-click = hide · draggable",
        "tip_hidden_toast": "Term button hidden (refresh the page to restore)",
        # ── Agent Workspace · Dashboard (P1 i18n step①) ──
        # dash.* → i18n_packs/dashboard_shell.py (P5 split)
        # ── Dashboard · static skeleton (P1 i18n step③-K) ──
        # ── Dashboard · dynamic JS (P1 i18n step③-K2) ─────
        # ── Inbox · top bar (P1 i18n step②-B) ───────────
        # inbox.* → i18n_packs/inbox_workspace.py (P5 split)
        # ── Draft review workspace (P1 i18n step③) ──────
        # draft.* → i18n_packs/draft_review_page.py (P5 split)
        # ── Inbox · assistant panel + skeleton states (P1 i18n step③-B) ──
        # ── P1 on-demand older history (pull from phone) + contacts panel ──
        # ── Inbox · right-rail content rows (P1 i18n step③-C) ──
        # ── Inbox · toast/confirm sea (P1 i18n step③-D) ──────
        # ── Inbox · relationship-stage / workflow notices (P1 i18n step③-E) ──
        # ── Inbox · media/mode/takeover/draft/NBA/slash quote (P1 i18n step③-F) ──
        # ── Inbox · platform automation downgrade notice (P0-5 visibility) ──
        # ── Inbox · batch/default-lang/account/group@/history/view (P1 i18n step③-G) ──
        # ── Inbox · voice-clone management (P1 i18n step③-H · enroll/rebind/audition) ──
        # reconcile / purge
        # ── Inbox · static shell remainder + theme + platform/funnel/time (P1 i18n step③-M1/M2) ──
        # ── Inbox · conversation list / account chips / group activity (P1 i18n step③-M3) ──
        # ── Inbox · batch bar / chat header / safety bar / composer / translate popover (P1 i18n step③-M4) ──
        # ── Phase 2: composer-integrated AI draft bar ──
        # ── Phase 2: conversation-row handler badge ──
        # ── Connect · Telegram two-step verification (cloud password) ──
        # ── Inbox · account drawer + QR onboarding wizard (P1 i18n step③-M5) ──
        # ── Inbox · chat header / message media / tag dropdown / confirm dialog (P1 i18n step③-M6) ──
        # ── Inbox · read-only/caps/msg-search/notes/message bubble/send toasts (P1 i18n step③-M7) ──
        # ── Inbox · full-auto log / draft mini-card / claim / voice (P1 i18n step③-M8) ──
        # ── Inbox · default translate/reply language manager (P1 i18n step③-M9) ──
        # ── Inbox · translation engine badges / multi-engine compare (P1 i18n step③-M10) ──
        # ── P0-2: translation confidence exposure (compare-card tier badges + low-conf hint) ──
        # ── Inbox · manual single translate / in-conversation media translate (P1 i18n step③-M11) ──
        # ── Inbox · image/voice/document translate panels (P1 i18n step③-M12) ──
        # ── Inbox · quick templates / KB / account drawer / orchestrator / login modes (P1 i18n step③-M13) ──
        # ── Inbox · translation status pill / realtime @mention / fallback (P1 i18n step③-M14) ──
        # ── Workspace shared shell · top bar / menu / user area (P1 i18n step③-I) ──
        # base.* → i18n_packs/workspace_shell.py (P5 split)
        # ── Workspace shell · SLA drill panel / confirm dialog / reason map (P1 i18n step③-J) ──
        # ── Workspace shell · alert/notification preferences modal (P1 i18n step③-J) ──
        "crmw.unit.sec": "{n}s",
        "crmw.unit.min": "{n} min",
        "crmw.unit.min_dec": "{n} min",
        "crmw.unit.hour": "{n} h",
        "crmw.unit.hour_dec": "{n} h",
        "crmw.unit.day": "{n} d",
        # ── Realtime voice trial page voice_call.html (P33 static layer) ──
        "rvc.title": "Realtime Voice Call · Companion",
        "rvc.h1": "Realtime Voice Call",
        "rvc.engine_pill": "MiniCPM-o · Full-duplex",
        "rvc.help_t": "How to use",
        "rvc.step_pick": "Pick persona",
        "rvc.step_engine": "Engine ready",
        "rvc.step_call": "Connect",
        "rvc.lbl_pick": "① Choose a persona",
        "rvc.loading_personas": "Loading personas…",
        "rvc.btn_listen": "🔊 Preview",
        "rvc.hint_pick": "Select a persona to preview their voice",
        "rvc.btn_upload": "🎙️ Upload voice sample",
        "rvc.btn_remove": "Remove",
        "rvc.lbl_lang": "Language",
        "rvc.lang_zh": "Chinese",
        "rvc.adv_toggle": "⚙ Advanced (memory / access token)",
        "rvc.lbl_chatkey": "Conversation ID (pick a recent chat to load memory, or start fresh)",
        "rvc.chatkey_fresh": "✨ Fresh chat (no memory)",
        "rvc.chatkey_ph": "e.g. tg:8244899900",
        "rvc.lbl_token": "Access token (only for public deployments)",
        "rvc.token_ph": "Enter if required by server",
        "rvc.lbl_engine": "Voice engine · VRAM on demand",
        "rvc.engine_detecting": "Checking…",
        "rvc.btn_engine_start": "Start engine",
        "rvc.status_idle": "Not connected",
        "rvc.guide_title": "Start a call in 3 steps 🎧",
        "rvc.guide_s1_t": "Pick persona",
        "rvc.guide_s1_b": "Choose an avatar — that's who you'll talk to",
        "rvc.guide_s2_t": "Start engine",
        "rvc.guide_s2_b": "Click Start engine to load VRAM (~10–60s)",
        "rvc.guide_s3_t": "Connect",
        "rvc.guide_s3_b": "When ready, click Call and start talking",
        "rvc.guide_ok": "Get started",
        "rvc.btn_call": "📞 Connect",
        "rvc.btn_hang": "Hang up",
        # ── Workspace shell · notification center + SSE event copy + global search (P1 i18n step③-J) ──
        # ── Workspace shell · remaining JS blocks (funnel/online/merge-review/license/L4 drafts, J4) ──
        "lang_toggle": "中文",
        # ── Case panel body (③-S5: cases.html static + dynamic JS) ──
        "cases_title": "Case Follow-ups",
        "cases_guide_title": "How it works",
        "cases_guide_body": "Each case card shows the user message and the AI reply. Add a <strong>note</strong> to flag key info, then click <strong style=\"color:var(--green)\">Close</strong> when done. The list auto-refreshes every 30s.",
        "cases_guide_dismiss": "Got it, don't show again",
        "cases_stat_total": "Total cases",
        "cases_stat_active": "Active",
        "cases_risk": "At risk",
        "cases_empty_title": "No pending cases right now",
        "cases_empty_hint": "The system checks for new cases every 30s; you can also click Refresh to check manually.",
        "cases_empty_full": "No active cases",
        "cases_escalated": "Escalated",
        "cases_closed": "Closed",
        "cases_satisfaction": "Satisfaction",
        "cases_consecutive": "Consecutive follow-ups",
        "cases_user": "User",
        "cases_group": "Group",
        "cases_note": "Note",
        "cases_note_ph": "Add a note…",
        "cases_save_note": "Save note",
        "cases_close": "Close case",
        "cases_load_fail": "Failed to load",
        "cases_load_fail_hint": "Please click Refresh to retry. If it persists, contact technical support.",
        "cases_close_prompt": "Enter a resolution note (optional):",
        # ── Simple-mode onboarding modal (③-S5: base.html simple onboard-modal; was full-only sealed) ──
        "onb_welcome_title": "Welcome to Simple Mode",
        "onb_welcome_desc": "We've streamlined the interface to focus on the features you use most:",
        "onb_cases_label": "Case Follow-ups",
        "onb_cases_desc": "View and handle customer cases",
        "onb_kb_label": "Knowledge Base",
        "onb_kb_desc": "Browse and search knowledge entries",
        "onb_review_label": "Learning Queue",
        "onb_review_desc": "Review AI-generated drafts",
        "onb_footer": "You can switch to Full Mode anytime from the bottom of the sidebar",
        "onb_start": "Get started",
        # ── Live logs page body (③-S6: logs.html toolbar + terminal + SSE reconnect) ──
        "logs_adv_title": "Advanced feature",
        "logs_adv_desc": "Live logs are for technical troubleshooting.",
        "logs_back_cases": "Back to cases",
        "logs_search_ph": "Filter by keyword…",
        "logs_clear_filter": "Clear filters",
        "logs_live": "Live",
        "logs_count_init": "0 lines",
        "logs_line_count": "{n} lines",
        "logs_pause": "Pause",
        "logs_resume": "Resume",
        "logs_paused": "Paused",
        "logs_clear_title": "Clear the currently displayed logs (does not affect server log files)",
        "logs_clear": "Clear",
        "logs_download": "Download",
        "logs_follow": "Follow",
        "logs_no_match": "No matching logs",
        "logs_reconnect": "Reconnecting… (attempt {n} / {s}s)",
        # ── Analytics page body (③-S7: refresh bar / period / stat cards / charts / Ops Copilot) ──
        # analytics_* → i18n_packs/analytics_page.py (P5 split)
        # ── Persona Studio personas.html (③-S8a-1: topbar/tabs/dashboard/profiles static) ──
        # psn_* → i18n_packs/persona_studio.py (P5 split)
        # ── Persona Studio personas.html (③-S8a-2: bindings/rules/drawer/default/picker static) ──
        # ── Persona Studio P0 revamp (card avatar/readiness/preview/undo) ──
        # ── Persona Studio P1 (apply-to/create-wizard/chat-test) ──
        # ── Persona Studio P2 (usage stats/health ledger) ──
        # err.* → i18n_packs/errors_stock.py (P5 split)
        # ── personas.html JS layer (③-S8b auto-migrated) ──
        # ov_* → i18n_packs/rpa_overview_page.py (P5 split)
        # msg_* → i18n_packs/messenger_page.py (P5 split)
        # msg_tab_voice → i18n_packs/messenger.py (P4 split)
        # wa_* → i18n_packs/whatsapp_page.py (P5 split)
        # ln_* → i18n_packs/line_page.py (P5 split)
        # tg_s/tg_js_* → i18n_packs/telegram_page.py (P5 split)
        # tg_gate_* → i18n_packs/telegram_gate.py; fn_export_csv etc. → i18n_packs/funnel.py (P4 split)
        # db_* → i18n_packs/dashboard_page.py (P5 split)
        # set_* → i18n_packs/settings_page.py (P5 split)
        # ── P0-4/C4 license activation card ──
        # kb_* → i18n_packs/knowledge_page.py (P5 split)
        # ap_* → i18n_packs/agent_perf_page.py (P5 split)
        # ops.* → i18n_packs/ops_shared.py (P5 split)
        # as_* → i18n_packs/ai_studio_page.py (P5 split)
        # em_* → i18n_packs/episodic_page.py (P5 split)
        # lr_* → i18n_packs/learner_page.py (P5 split)
        # rh_* → i18n_packs/relations_health_page.py (P5 split)
        # mo_* → i18n_packs/monetization_page.py (P5 split)
        # c3_* → i18n_packs/contact360_page.py (P5 split)
        # sa_* → i18n_packs/strategy_analytics_page.py (P5 split)
        # ov2_* → i18n_packs/ops_overview_page.py (P5 split)
        # ── P0-3/B9: inbound auto-translate volume KPI (cost guardrail) ──
        # tp_* → i18n_packs/templates_page.py (P5 split)
        "tm_s001": "Template Library",
        "tm_s002": "New template",
        "tm_s003": "Search template title or content…",
        "tm_s004": "All scenes",
        "tm_s005": "Order",
        "tm_s006": "Logistics",
        "tm_s007": "Refund",
        "tm_s008": "Complaint",
        "tm_s009": "Closing",
        "tm_s010": "Usage count",
        "tm_s011": "Short description (e.g. Chinese order confirmation)",
        "tm_s012": "Template body. Use",
        "tm_s013": "{variable}",
        "tm_s014": "to mark placeholders, e.g.",
        "tm_s015": "{order number}",
        "tm_s016": "Logistics query",
        "tm_s017": "Product inquiry",
        "tm_s018": "Platform (empty",
        "tm_s019": "all platforms)",
        "tm_js001": "templates",
        "tm_js002": "Product",
        "tm_js003": "Title and content cannot be empty",
        "tm_js004": "Delete template",
        "tm_js005": "This cannot be undone.",
        "tm_js006": "Delete failed (may require supervisor permission)",
        # im_* → i18n_packs/import_page.py (P5 split)
        # dv_* → i18n_packs/developer_page.py (P5 split)
        # ── Cloud credentials / backup key pool card (P-KeyPool) ──
        # wf_* → i18n_packs/workflows_page.py (P5 split)
        # st_* → i18n_packs/strategies_page.py (P5 split)
        "us_s001": "User Management",
        "us_s002": "This page is an advanced admin feature.",
        "us_s003": "Manage backend accounts, roles and permissions",
        "us_s004": "Observer",
        "us_s005": "The primary account cannot be deleted or demoted",
        "us_s006": "Add sub-account",
        "us_s007": "All currently logged-in devices; you can kick out specific sessions",
        "us_s008": "Kick out all other devices",
        "us_s009": "Create sub-account",
        "us_s010": "letters",
        "us_s011": "digits",
        "us_s012": "Password",
        "us_s013": "at least 6 characters",
        "us_s014": "Read-only observer",
        "us_js001": "Role updated",
        "us_js002": "Delete user",
        "us_js003": "User created",
        "us_js004": "No active sessions",
        "us_js005": "Mobile device",
        "us_js006": "Browser",
        "us_js007": "Device kicked out",
        "us_js008": "Kick out all other devices?",
        "us_js009": "Kick out",
        "us_js010": "Last active:",
        "us_kicked_n": "Kicked out {n} devices",
        "role_agent": "Agent (Chat Workspace Only)",
        "au_s002": "Activity heatmap",
        "au_s003": "Last 84 days",
        "au_s004": "Each cell is one day; darker means more actions",
        "au_s005": "Mon",
        "au_s006": "Tue",
        "au_s007": "Wed",
        "au_s008": "Thu",
        "au_s009": "Fri",
        "au_s010": "Sat",
        "au_s011": "Quick filters",
        "au_s012": "Click once to filter; click again to",
        "au_s013": "reset",
        "au_s014": "Filters",
        "au_s015": "Export current filtered results as",
        "au_s016": "Export all records as",
        "au_s017": "Export filtered",
        "au_s018": "Export all",
        "au_s019": "Action type",
        "au_s020": "Target / keyword",
        "au_s021": "Channel / template / keyword",
        "au_s022": "Start date",
        "au_s023": "End date",
        "au_s024": "Action timeline",
        "au_s025": "Snapshot",
        "au_s026": "View changes",
        "au_s027": "Old:",
        "au_s028": "New:",
        "au_preset_1": "🛡 Large body attack (413)",
        "au_preset_2": "⚡ Rate-limit hit (429)",
        "au_preset_3": "📝 Dict write",
        "au_preset_4": "↺ Dict restore",
        "au_preset_5": "🔄 Dict reload",
        "au_js001": "Collapse changes",
        "au_js002": "actions",
        "au_js003": "Failed to load heatmap",
        "au_js004": "active days",
        "au_js005": "peak day",
        "au_s001": "Audit Log",
        "wr_s001": "Business ROI Dashboard",
        "wr_s002": "Back to Today Overview",
        "wr_s003": "Auto-reply vs Human takeover",
        "wr_s004": "Blocked (not sent)",
        "wr_s005": "Daily AI (blue) / Human (orange) send volume",
        "wr_s006": "Config health",
        "wr_tf_sticky_n": "{n} cross-day revisits (higher is better)",
        "wr_tf_cohort": "7-day revisits of {n} new relationships",
        "wr_tf_total_replies": "{n} replies in total",
        "wr_tf_est_per": "estimated at {n}s each",
        "wr_tf_hour_cost": "at ¥{n}/hour",
        "wr_tf_conv_rate": "conversion rate {n}%",
        "wr_tf_responded": "{n} sessions responded",
        "wr_tf_lead_rate": "lead rate {n}%",
        "wr_tf_spark": "{day}: AI {ai} / Human {hu}",
        "wr_js001": "Active relationships",
        "wr_js002": "Relationships with interaction this period",
        "wr_js003": "Relationship stickiness",
        "wr_js004": "Avg exchange rounds",
        "wr_js005": "Relationship depth (higher is deeper)",
        "wr_js006": "retention",
        "wr_js007": "auto-reply share",
        "wr_js008": "Labor saved (person-hours)",
        "wr_js009": "Human takeover",
        "wr_js010": "Sent after agent handling",
        "wr_js011": "Conversions",
        "wr_js012": "First-response SLA rate",
        "wr_js013": "New customers",
        "wr_js014": "Config self-check unavailable",
        "wr_js015": "warnings",
        "wr_js016": "Config healthy",
        "wr_js017": "No issues found",
        "wr_js018": "View details",
        "wu_s001": "Usage Metering",
        "wu_s002": "No usage data yet (accumulates once channels are connected and messages start flowing).",
        "wu_s003": "Daily usage trend",
        "wu_s004": "Messages",
        "wu_s005": "Calls",
        "wu_s006": "Definitions: Messages",
        "wu_s007": "messages sent/received; AI calls",
        "wu_s008": "AI drafts generated; Active agents",
        "wu_s009": "distinct agents active in the window.",
        "wu_s010": "Billing statement",
        "wu_s011": "Quantity",
        "wu_s012": "Amount due",
        "wu_s013": "No usage data for this period.",
        "wu_tf_spark": "{day}: Messages {m} / AI {ai}",
        "wu_tf_meta1": "Period {period} · Plan {plan}",
        "wu_tf_meta_cust": " · Customer {c}",
        "wu_tf_meta_seats": " · Seats {seats} (active {active})",
        "wu_tf_meta_over": " · ⚠ over {n}",
        "wu_tf_total": "Total {cur} {n}",
        "wu_js001": "no MoM",
        "wu_js002": "(Community mode)",
        "wu_js003": "Over",
        "wu_js004": "Total messages",
        "wu_js005": "Monthly message quota",
        "wu_js006": "No trend yet",
        "aq_s001": "AI Reply Quality",
        "aq_s002": "Reply quality",
        "aq_s003": "No AI draft disposition data yet (connect a channel",
        "aq_s004": "enable AI autosend, and it will accumulate over time).",
        "aq_s005": "Disposition breakdown (all AI drafts in window)",
        "aq_s006": "Auto-pass rate trend",
        "aq_s007": "Daily autosend / total dispositions that day",
        "aq_s008": "KB improvement candidates (sessions where AI was corrected/rejected)",
        "aq_s009": "Turn these human-corrected Q&As into knowledge in one click, and AI will know next time.",
        "aq_js001": "Approved & sent",
        "aq_js002": "Sent after rewrite",
        "aq_js003": "Force-passed",
        "aq_js004": "Blocked",
        "aq_js005": "No dispositions",
        "aq_js006": "Auto-pass rate",
        "aq_js007": "Manual rewrite rate",
        "aq_js008": "Rejection rate",
        "aq_js009": "High-risk share",
        "aq_js010": "Total dispositions",
        "aq_js011": "No candidates (no recently rewritten/rejected drafts).",
        "aq_js012": "Agent answer:",
        "aq_js013": "No rewritten answer (after import, AI drafts it, then a human proofreads)",
        "aq_js014": "Convert to KB entry",
        "aq_js015": "Importing...",
        "aq_js016": "Entry created",
        "aq_js017": " (AI is drafting it)",
        "aq_js018": "Imported",
        "aq_tf_spark": "{day}: {rate}% ({a}/{t})",
        "qm_s001": "Live Queue Dashboard",
        "qm_s002": "Live operations queue dashboard",
        "qm_s003": "Manual refresh",
        "qm_s004": "Pending sessions",
        "qm_s005": "Unread messages",
        "qm_s006": "Severe timeouts",
        "qm_s007": "Avg wait",
        "qm_s008": "Card view",
        "qm_s009": "Table view",
        "qm_s010": "Longest wait",
        "qm_s011": "Load",
        "qm_s012": "Reassign sessions",
        "qm_s013": "'s sessions to:",
        "qm_s014": "Confirm assignment",
        "qm_s015": "Reassign",
        "qm_tf_will_assign": "Will assign {n} sessions",
        "qm_tf_pending_opt": " ({n} pending)",
        "qm_tf_assigned_to": "Assigned {n} sessions to {agent}",
        "qm_js001": "No agents online",
        "qm_js002": "avg wait",
        "qm_js003": "Reassign",
        "qm_js004": "No other online agents to assign",
        "qm_js005": "No assignable sessions found",
        "qm_js006": "Assignment failed:",
        "qm_js_min": "m",
        "cs_s001": "Proactive Care",
        "cs_s002": "Proactive care to-do",
        "cs_s003": "Remember what they said and reach out on time · requires enabling",
        "cs_s004": "Care topic",
        "cs_s005": "Interview / follow-up ...",
        "cs_s006": "After how many hours",
        "cs_s007": "Add one manually",
        "cs_s008": "Pending care",
        "cs_s009": "Topic",
        "cs_s010": "Original summary",
        "cs_s011": "Due",
        "cs_tf_count": "{n} items (sorted by due date, ascending)",
        "cs_js001": "Send now",
        "cs_js002": "Contact key and care topic are required",
        "cs_js003": "Send now: bring it forward to now, processed in the next dispatch round (still subject to send guardrails). Continue?",
        "cs_js004": "Cancel this care to-do?",
        "cs_js005": "Cancel failed:",
        "ca_s001": "Crisis Audit",
        "ca_s002": "Crisis event audit",
        "ca_s003": "Safety-chain trail · requires enabling",
        "ca_s004": "Filter by user",
        "ca_s005": "prefix",
        "ca_s006": "Unhandled only",
        "ca_s007": "Repeats",
        "ca_s008": "Mark",
        "ca_s009": "Mark as handled",
        "ca_s010": "Disposition note (optional): called / escalated to human / ...",
        "ca_s011": "Confirm disposition",
        "ca_tf_evt_user": "Event #{id} · User {user}",
        "ca_js001": "This page",
        "ca_js002": "Total unhandled",
        "ca_js003": "Recent crisis events (newest first)",
        "ca_js004": "Not escalated",
        "ca_js005": "Disposition failed",
        "ca_js006": "Escalated",
        "su_s001": "First-time use · Setup wizard",
        "su_s002": "Account",
        "su_s003": "Create admin account",
        "su_s004": "Confirm password",
        "su_s005": "Re-enter",
        "su_s006": "After configuring the AI API, knowledge-base semantic search and auto-translation are enabled automatically. You can also change this later in the config file.",
        "su_s007": "Finish setup",
        "su_s008": "Setup complete!",
        "su_s009": "Account created. Redirecting to the login page…",
        "su_s010": "Go to login",
        "su_s011": "Dark / Light",
        "su_s000": "Initial Setup",
        "su_js_001": "Please enter the",
        "su_js_002": "Please enter a username",
        "su_js_003": "Password must be at least 6 characters",
        "su_js_004": "The two passwords do not match",
        "su_js_005": "Initializing…",
        "su_js_006": "Setup failed",
        "su_js_reqfail": "Request failed: ",
        "su_tf_created": "Account \"{u}\" created. Redirecting in 3s…",
        "sw_js_001": "Ready",
        "sw_js_002": "Filled, pending enable/login",
        "sw_js_003": "Filled; leave blank to keep unchanged",
        "sw_js_004": "After saving your credentials, go to",
        "sw_js_005": "to log in to this platform account via QR code / verification code.",
        "sw_js_006": "No credentials needed; ready to use.",
        "sw_js_007": "Save & enable",
        "sw_js_008": "Saved & applied",
        "sw_s001": "Channel setup wizard",
        "sw_s002": "Choose a channel → enter credentials (validated instantly) → save to apply. Credentials are written to",
        "sw_s003": ", without touching main-config comments.",
        "sw_s006": "Ready",
        "sw_s007": "Once channels are connected, don't forget to feed the AI knowledge",
        "sw_s008": "Knowledge-base cold start (one-click seed starter scripts)",
        "sw_js_opt": "(optional)",
        "sw_tf_miss": "{n} missing",
        "sw_tf_cur": "Current: {v} (blank keeps unchanged)",
        "sw_tf_ready": "Ready {r} / {t}",
        "el_js_001": "Unclaimed",
        "el_js_002": "Agent offline",
        "el_js_003": "Agent muted / DND",
        "el_js_004": "Total escalations",
        "el_js_005": "Taken over",
        "el_js_006": "Avg. takeover latency",
        "el_js_007": "No escalations in this window",
        "el_js_008": "Not taken over",
        "el_js_009": "Inbox not enabled",
        "el_s001": "Escalation history",
        "el_s002": "Escalation history / takeover latency",
        "el_tf_taken": "Taken over {d}",
        "el_tf_wait": "Waited {d}",
        "el_tf_holder": "Originally claimed by {h}",
        "lg_s001": "Admin console",
        "lg_s002": "Sign in with Token",
        "lg_s003": "Enter your access token",
        "lg_s004": "Sign in with username & password",
        "lg_s005": "Toggle dark / light",
        "lg_s000": "Sign in",
        "lg_s006": "Admin password",
        "da_s001": "Draft audit log",
        "da_s002": "Back to draft workspace",
        "da_s003": "Draft ID (optional)",
        "da_s004": "Agent ID (optional)",
        "da_s005": "Action",
        "da_s006": "Reason",
        "da_o_all": "All actions",
        "da_o_blocked": "Blocked",
        "da_o_force": "Force-passed",
        "da_o_autosend": "Auto-sent",
        "da_o_approved": "Approved",
        "da_o_rejected": "Rejected",
        "da_tf_count": "{n} records",
        "diff_js_001": "Quota",
        "diff_js_002": "Load failed, please refresh",
        "diff_js_003": "No snapshots yet",
        "diff_js_004": "Quick compare: ",
        "diff_js_005": "This config has fewer than two snapshots",
        "diff_js_006": "No snapshots for this config yet",
        "diff_js_007": "Confirm rollback",
        "diff_s001": "Version diff lets admins trace config changes.",
        "diff_s002": "Snapshot timeline",
        "diff_s003": "Old version",
        "diff_s004": "New version",
        "diff_s005": "Click to select",
        "diff_s006": "Quick compare: ",
        "diff_s007": "Select snapshot",
        "diff_s008": "Current config",
        "diff_s009": "Compare",
        "diff_s010": "Roll back to old version",
        "diff_s011": "Clear selection",
        "diff_s012": "Diff result",
        "diff_s013": "The two versions are identical — no differences",
        "diff_s014": "lines added",
        "diff_s015": "lines removed",
        "diff_s016": "Select two snapshot versions to compare",
        "diff_s_lblA": "Old version (A — Before)",
        "diff_s_lblB": "New version (B — After)",
        "diff_tf_latest2": "{label}: latest two versions",
        "diff_tf_latestcur": "{label}: latest vs current",
        "diff_js_rollback_confirm": "Rollback confirmation",
        "diff_tf_rollback_q": "Roll back to snapshot \"{id}\"?<br>The current config will be overwritten (auto-backed up).",
        "diff_js_rolledback": "Rolled back: ",
        "ks_js_001": "Knowledge base unavailable",
        "ks_js_002": "Cold — seeding recommended",
        "ks_js_003": "Knowledge available",
        "ks_js_004": "Seed in one click",
        "ks_js_005": "Seeding…",
        "ks_js_006": "Seeding failed",
        "ks_js_007": "Seed again",
        "ks_s001": "Knowledge-base cold start",
        "ks_s002": "Back to setup wizard",
        "ks_s003": "Channels are connected, but the AI has no private-domain knowledge yet. Pick a scenario to seed starter scripts, then fine-tune in the knowledge base.",
        "ks_s004": "Enabled KB entries",
        "ks_s005": "Categories covered",
        "ks_s006": "After seeding: ",
        "ks_s010": "① Go to",
        "ks_s011": "② Use",
        "ks_s008": "and tailor scripts to your business; ",
        "ks_s009": "enter sample customer questions and confirm the AI answers correctly.",
        "ks_tf_packcount": "{n} starter scripts",
        "ks_tf_added": "Added {added}",
        "ks_tf_added_skip": "Added {added}, skipped {skipped} already existing",
        "gl_js_001": "Ready to go live",
        "gl_js_002": "All hard requirements are met — you're good to launch!",
        "gl_js_003": "Can go live, with suggestions",
        "gl_js_004": "Hard requirements are met; address yellow items soon.",
        "gl_js_005": "Not ready to go live",
        "gl_js_006": "Must-fix issues remain — see red items below.",
        "gl_tf_sum": "<b style=\"color:var(--tk-ok-ink);\">{ok}</b> passed <b style=\"color:var(--tk-warn-ink);\">{warn}</b> suggested <b style=\"color:var(--tk-danger-ink);\">{fail}</b> to fix",
        "gl_js_action": "Fix",
        "tk_js_001": "No due date",
        "tk_js_002": "No tasks — you're all caught up",
        "tk_js_003": "Overdue",
        "tk_js_004": "Contacts subsystem not enabled",
        "tk_s001": "My tasks",
        "tk_s002": "Workspace",
        "tk_s003": "Follow-up tasks",
        "tk_s004": "All agents",
        "tk_s005": "Today & overdue",
        "tk_s006": "Overdue only",
        "tk_s007": "All open",
        "tk_tf_count": "{n} tasks · due (mine/all) {mine}/{all}",
        "tk_tf_snooze1": "+1 day",
        "tk_tf_snooze3": "+3 days",
        "tk_tf_snooze7": "+1 week",
        "cl_js_001": "No matching contacts",
        "cl_js_002": "Lead captured",
        "cl_s001": "Contacts",
        "cl_s002": "Search: name / ID / channel number…",
        "cl_s003": "All contacts",
        "cl_s004": "No lead yet",
        "cl_s005": "All follow-ups",
        "cl_s006": "Has follow-up plan",
        "cl_s007": "Previous",
        "cl_s008": "Next",
        "cl_o_due": "Follow-up due",
        "cl_tf_all_n": "All {n}",
        "cl_tf_pageinfo": "{pg} / {pages} · {total} total",
        "atd_js_001": "No files yet",
        "atd_js_002": "Cleaning…",
        "atd_js_003": "Cleanup failed",
        "atd_s001": "Overview",
        "atd_s002": "Files",
        "atd_s003": "Total size",
        "atd_s004": "File age",
        "atd_s005": "Oldest file",
        "atd_s006": "Newest file",
        "atd_s007": "Files older than 24h should be cleaned up",
        "atd_s008": "Prefix breakdown",
        "atd_s009": "Ops actions",
        "atd_s000": "TTS preview file dashboard",
        "atd_s_h1": "preview file dashboard",
        "atd_s_back": "Back to home",
        "atd_s014": "Auto-refresh: 30s |",
        "atd_s_disk": "Disk usage estimate (1GB threshold assumed)",
        "atd_s015": "tts-*: general | line-tts-*: LINE | wa-tts-*: WhatsApp",
        "atd_s016": "Clean >1h",
        "atd_s017": "Clean >24h",
        "atd_s018": "Clean >7d",
        "atd_tf_cleaned": "Removed {n} file(s)",
        # hp_* → i18n_packs/help_page.py (P5 split)
        # rvc_* → i18n_packs/voice_clone_page.py (P5 split)
        # ── Backend API error copy (P36: request-scoped localization) ──
        # ── Unified inbox send write-path (P37) ──
        # ── Platform QR login (P37) ──
        # err.login.password_* / err.enable.* → src/web/i18n_packs/errors.py (P4 split)
        # ── Cross-route shared error vocabulary (P38) ──
        # ── Draft review domain (P38) ──
        # ── ops_overview draft-backlog governance card (P38b: restore seal) ──
        # ── Login / user management (P39) ──
        # ── System settings (P39) ──
        # ── Licensing / character quota (P0-4 / C4) ──
        # ── Episodic memory / cross-platform identity (P40) ──
        # ── RPA shared error vocabulary (P40; {platform}/{op} parameterized) ──
        # ── P43b: messenger_rpa ──
        # ── Inbox workspace (P41) ──
        # ── P0-1 Desktop first-run wizard: AI credential overlay + workspace guide bar ──
        "setup.ai.saved": "AI configuration saved",
        # ── Backup key pool (P-KeyPool, 2026-07-12) ────────────────
        "setup.pool.saved": "Backup key pool saved and applied",
        "ws.aiguide.text": "AI model is not configured yet — translation and smart replies need an API key.",
        "ws.aiguide.cta": "Configure",
        "ws.aiguide.dismiss": "Not now",
        # ── Cloud AI degradation bar (P-KeyPool D) ─────────────────
        "ws.aidegrade.text": "Cloud AI is degraded; replies may be slower. A backup channel has taken over automatically — no action needed.",
        "ws.aidegrade.mode_pool": "(backup key serving)",
        "ws.aidegrade.mode_local": "(local model serving)",
        "setup.ai.card_title": "AI Model",
        "setup.ai.card_sub": "Powers translation / smart replies. One OpenAI-compatible API key (e.g. DeepSeek) is enough.",
        "setup.ai.f_key": "API Key",
        "setup.ai.f_base": "Endpoint (base_url)",
        "setup.ai.f_model": "Model",
        "setup.ai.btn_test": "Test connection",
        "setup.ai.btn_save": "Save & apply",
        "setup.ai.testing": "Connecting to AI service…",
        "setup.ai.test_ok": "Connected — key works",
        "setup.ai.saving": "Saving…",
        "setup.ai.saved_ready": "Saved — translation ready ✓",
        "setup.ai.saved_not_ready": "Saved, but the connection check failed (verify key / endpoint / network)",
        "setup.ai.st_configured": "Configured",
        "setup.ai.st_missing": "Not configured",
        # ── Inbox remainder batch (P42) ──
        # ── P43a: non-inbox mid/small files ──
        # pma_* → i18n_packs/persona_apply_modal.py (P5 split)
    },
}

DEFAULT_LANG = "zh"


# ── i18n packs 合并（P4 词条治理机制化,见 src/web/i18n_packs/__init__.py）────
# 新增词条一律进按域拆分的 pack 文件;本单体字典只承载存量。消费方(get_translations
# /t/tr)一律读合并视图 _MERGED,对模板/JS 词表/路由文案完全透明。
def _build_merged(base: dict) -> dict:
    """单体 + packs → 合并字典。packs 收集失败时 fail-safe 回落单体。"""
    try:
        from src.web.i18n_packs import collect_packs
        pzh, pen = collect_packs()
        return {"zh": {**base["zh"], **pzh}, "en": {**base["en"], **pen}}
    except Exception as exc:
        _logger.warning("i18n packs 合并失败（仅用单体字典）: %s", exc)
        return base


_MERGED = _build_merged(_TRANSLATIONS)

# ── 热加载状态（见模块 docstring）────────────────────────────────────────────
_SRC_PATH = _Path(__file__).resolve()
_RELOAD_LOCK = _threading.Lock()
# 基线必须在 **import 时**立即记录（import 装载的字典与此刻磁盘一致）——若推迟到
# 首次调用才记，「启动后、首个请求前」的改动会被当成基线吞掉（曾实测踩中）。
try:
    _loaded_mtime: float = _SRC_PATH.stat().st_mtime
except OSError:
    _loaded_mtime = 0.0
_next_check_ts: float = 0.0         # 节流：最多每 2s stat 一次
_last_err_mtime: float = -1.0       # 坏保存态告警去重（同一 mtime 只报一次）


def _packs_mtime() -> float:
    """pack 目录聚合 mtime（任一 pack 文件变化即变化;失败回 0 不触发重载）。"""
    try:
        from src.web.i18n_packs import pack_files
        return max((p.stat().st_mtime for p in pack_files()), default=0.0)
    except Exception:
        return 0.0


_packs_loaded_mtime: float = _packs_mtime()


def _maybe_reload() -> None:
    """单体或 pack 目录 mtime 变化时热重载翻译字典（fail-safe：任何异常保留旧字典）。

    快路径（节流窗口内 / mtime 未变）零锁零 IO 开销；仅真变化时锁内 exec 一次
    （15k 行 ~100ms，替换为原子引用赋值，并发读方要么旧字典要么新字典，无撕裂）。
    """
    global _next_check_ts, _loaded_mtime, _last_err_mtime, _TRANSLATIONS
    global _MERGED, _packs_loaded_mtime
    now = _time.monotonic()
    if now < _next_check_ts:
        return
    _next_check_ts = now + 2.0

    # ── pack 目录变化 → 仅重收集 packs 并重建合并视图（不 exec 单体）──
    pm = _packs_mtime()
    if pm != _packs_loaded_mtime:
        with _RELOAD_LOCK:
            if pm != _packs_loaded_mtime:
                try:
                    from src.web.i18n_packs import collect_packs
                    pzh, pen = collect_packs(force_reload=True)
                    _MERGED = {"zh": {**_TRANSLATIONS["zh"], **pzh},
                               "en": {**_TRANSLATIONS["en"], **pen}}
                    _packs_loaded_mtime = pm
                    _logger.info("i18n packs 热重载完成（+%d/+%d 键）", len(pzh), len(pen))
                except Exception as exc:
                    _logger.warning("i18n packs 热重载失败（保留旧字典）: %s", exc)

    try:
        mtime = _SRC_PATH.stat().st_mtime
    except OSError:
        return
    if mtime == _loaded_mtime:
        return
    with _RELOAD_LOCK:
        if mtime == _loaded_mtime:  # double-check：另一线程已重载
            return
        try:
            # utf-8-sig：编辑器/PowerShell 可能写出带 BOM 的保存态，裸 utf-8 会把
            # U+FEFF 混进源码首行 → compile SyntaxError → 热加载静默失效
            src = _SRC_PATH.read_text(encoding="utf-8-sig")
            ns: dict = {"__file__": str(_SRC_PATH)}
            exec(compile(src, str(_SRC_PATH), "exec"), ns)  # noqa: S102
            new = ns.get("_TRANSLATIONS")
            if isinstance(new, dict) and new.get("zh") and new.get("en"):
                _TRANSLATIONS = new
                _MERGED = _build_merged(new)
                _loaded_mtime = mtime
                _logger.info("web_i18n 热重载完成（zh=%d 键, en=%d 键）",
                             len(new["zh"]), len(new["en"]))
            else:
                if mtime != _last_err_mtime:
                    _last_err_mtime = mtime
                    _logger.warning("web_i18n 热重载被拒：_TRANSLATIONS 缺 zh/en（保留旧字典）")
        except Exception as exc:
            # 语法错误的中间保存态等：保留旧字典，同一 mtime 只报一次，修好后自动重试
            if mtime != _last_err_mtime:
                _last_err_mtime = mtime
                _logger.warning("web_i18n 热重载失败（保留旧字典）: %s", exc)


def get_translations(lang: str = "zh") -> dict:
    _maybe_reload()
    return _MERGED.get(lang, _MERGED[DEFAULT_LANG])


def t(key: str, lang: str = "zh") -> str:
    _maybe_reload()
    d = _MERGED.get(lang, _MERGED[DEFAULT_LANG])
    return d.get(key, key)


def tr(request, key: str, default: str = None, /, **fmt) -> str:
    """请求级本地化 — 给 API 的 detail/error 文案用。

    从 inject_i18n 中间件写入的 ``request.state.ui_lang`` 取当前语言，
    返回对应语种译文，使前端 verbatim 直显的 ``d.detail`` / ``d.error``
    随 UI 语言走，而非把硬编码中文泄漏给英文用户。
    未知键回落 ``default``（再回落键名本身）；``**fmt`` 尽力 format（异常不抛）。

    ``request`` / ``key`` / ``default`` 为**位置限定形参**（``/``）：这样占位符即便
    叫 ``key`` / ``default`` / ``request``（如 ``err.rpa.config_missing`` 曾用 ``{key}``），
    以关键字传入时也会落进 ``**fmt`` 而非与本函数形参撞名抛 ``TypeError``。
    另有门禁 ``test_i18n_placeholders_avoid_reserved_names`` 从源头禁用这些占位符名。
    """
    _maybe_reload()
    lang = DEFAULT_LANG
    try:
        lang = getattr(getattr(request, "state", None), "ui_lang", DEFAULT_LANG) or DEFAULT_LANG
    except Exception:
        lang = DEFAULT_LANG
    d = _MERGED.get(lang, _MERGED[DEFAULT_LANG])
    s = d.get(key)
    if s is None:
        s = default if default is not None else key
    if fmt:
        try:
            s = s.format(**fmt)
        except Exception:
            pass
    return s
