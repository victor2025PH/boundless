# -*- coding: utf-8 -*-
"""全局悬浮提示词典单一数据源(原 base.html 内联 TERM_DICT)。

base.html 的 tooltip 引擎经 `const TERM_DICT={{ help_terms|tojson }};` 消费本表;
admin.py `_enrich_context` 注入模板上下文。词条结构:
    key → {zh, en, desc, desc_en, usage?, usage_en?}
zh/en=标题,desc*=功能描述,usage*=典型操作路径(可选)。
导航项的 key(nav_*)由 src/web/nav_schema.py 的 item.help 关联。
"bot_admin" 品牌词条此处存默认品牌名;线上 zh/en 随 site_name 动态,
由 base.html 注入 TERM_DICT 后覆写(与原内联 Jinja 表达式行为一致)。
"""

HELP_TERMS: dict = {
    # ── 导航页面 ───
    "nav_dashboard": {
        "zh": "仪表盘",
        "en": "Dashboard",
        "desc": "系统总览页面，展示 Bot 运行状态、通道健康度、最近操作等关键指标",
        "desc_en": "System overview page showing bot status, channel health, recent activity and other key metrics",
        "usage": "打开即可查看全局状态，点击各卡片的快捷链接可跳转到对应功能模块",
        "usage_en": "Open it to see global status; click a card's quick link to jump to the matching module"
    },
    "nav_templates": {
        "zh": "话术模板（已迁移）",
        "en": "Reply Templates (moved)",
        "desc": "话术模板已统一迁移到知识库的「系统话术」分类，可在知识库页面进行编辑",
        "desc_en": "Reply templates have moved to the Knowledge Base's \"System Scripts\" category and are edited there",
        "usage": "打开知识库 → 筛选「系统话术」分类 → 编辑对应的话术条目",
        "usage_en": "Open Knowledge Base → filter the \"System Scripts\" category → edit the matching entry"
    },
    "nav_channels": {
        "zh": "通道管理",
        "en": "Channel Management",
        "desc": "配置支付通道的费率、金额限制、状态开关，实时监控成功率",
        "desc_en": "Configure payment-channel rates, amount limits and status toggles, and monitor success rate in real time",
        "usage": "点击通道卡片展开详情 → 调整参数 → 保存。可切换状态为 启用/维护中/波动",
        "usage_en": "Click a channel card to expand → adjust parameters → save. Status can be set to Enabled / Maintenance / Fluctuating"
    },
    "nav_strategies": {
        "zh": "回复策略",
        "en": "Reply Strategies",
        "desc": "为不同用户意图配置 AI 回复参数（温度、长度、上下文轮数），支持 A/B 灰度测试。注意：这里是 AI 回复参数，不是营销策略",
        "desc_en": "Configure AI reply parameters (temperature, length, context rounds) per user intent, with A/B testing. Note: these are AI reply parameters, not marketing strategy",
        "usage": "选择策略 → 调整 Temperature/Max Tokens 等参数 → 保存 → 在意图映射中绑定",
        "usage_en": "Pick a strategy → adjust Temperature/Max Tokens → save → bind it in the intent mapping"
    },
    "nav_strategy_analytics": {
        "zh": "策略效果",
        "en": "Strategy Performance",
        "desc": "追踪各策略的质量评分、追问率、响应时间等指标，自动识别最优策略",
        "desc_en": "Track each strategy's quality score, follow-up rate, response time and more, auto-spotting the best one",
        "usage": "选择时间范围查看趋势图，点击策略卡片查看详细指标",
        "usage_en": "Pick a time range to view trends; click a strategy card for detailed metrics"
    },
    "nav_knowledge": {
        "zh": "知识库",
        "en": "Knowledge Base",
        "desc": "管理 AI 的知识条目、触发词、错误码字典，支持 BM25 + 语义搜索",
        "desc_en": "Manage the AI's knowledge entries, trigger words and error-code dictionary, with BM25 + semantic search",
        "usage": "搜索/浏览条目 → 编辑内容和触发词 → 用沙盒测试验证效果",
        "usage_en": "Search/browse entries → edit content and triggers → verify with the sandbox test"
    },
    "nav_users": {
        "zh": "用户管理",
        "en": "User Management",
        "desc": "添加/管理后台管理员账户，设置角色权限（主帐号/管理员/观察员）",
        "desc_en": "Add and manage admin accounts and assign role permissions (Master / Admin / Viewer)",
        "usage": "点击「创建子帐号」→ 填写信息 → 分配角色。可随时禁用/删除账户",
        "usage_en": "Click \"Create sub-account\" → fill in details → assign a role. Accounts can be disabled/deleted anytime"
    },
    "nav_settings": {
        "zh": "系统设置",
        "en": "System Settings",
        "desc": "配置 AI 提示词和人工转接设置",
        "desc_en": "Configure the AI system prompt and human-handoff settings",
        "usage": "编辑提示词 → 保存。底层 API/安全配置请进入开发者工具",
        "usage_en": "Edit the prompt → save. For low-level API/security config, use Developer Tools"
    },
    "nav_developer": {
        "zh": "开发者工具",
        "en": "Developer Tools",
        "desc": "API 密钥、底层参数、安全令牌、Bot 行为（含是否处理私聊）、回复逻辑、意图路由等高级配置（需密码）",
        "desc_en": "Advanced config — API keys, low-level params, security tokens, bot behavior (incl. private chats), reply logic, intent routing (password required)",
        "usage": "输入开发者密码进入 → 「Bot 行为配置」中勾选「私聊消息」可允许私聊 → 保存",
        "usage_en": "Enter the developer password → tick \"Private messages\" under Bot Behavior to allow DMs → save"
    },
    "nav_audit": {
        "zh": "操作记录",
        "en": "Activity Log",
        "desc": "查看所有后台操作的审计日志，支持按操作人/类型/时间筛选",
        "desc_en": "View the audit log of all admin actions, filterable by operator / type / time",
        "usage": "使用顶部筛选条件缩小范围 → 点击导出按钮下载 CSV",
        "usage_en": "Use the top filters to narrow down → click Export to download CSV"
    },
    "nav_diff": {
        "zh": "版本对比",
        "en": "Version Diff",
        "desc": "对比配置的不同版本快照，查看变更详情，支持一键回滚",
        "desc_en": "Compare configuration version snapshots, inspect change details, and roll back in one click",
        "usage": "选择两个版本 → 查看差异 → 确认后可回滚到旧版本",
        "usage_en": "Pick two versions → review the diff → confirm to roll back to the older one"
    },
    "nav_logs": {
        "zh": "实时日志",
        "en": "Live Logs",
        "desc": "实时查看系统运行日志流，支持按级别过滤（DEBUG/INFO/WARN/ERROR）和关键词搜索",
        "desc_en": "Stream system logs in real time, filterable by level (DEBUG/INFO/WARN/ERROR) and keyword",
        "usage": "选择日志级别按钮过滤 → 输入关键词进一步筛选 → 点击暂停冻结画面",
        "usage_en": "Pick a level button to filter → type a keyword to narrow further → click Pause to freeze the view"
    },
    "nav_analytics": {
        "zh": "运营分析",
        "en": "Analytics",
        "desc": "查看运营数据图表、KB 命中率、响应时间趋势，内置 AI Copilot 自然语言查询",
        "desc_en": "View operations charts, KB hit rate and response-time trends, with a built-in AI Copilot for natural-language queries",
        "usage": "切换时间范围查看趋势 → 在 Copilot 输入框用中文提问查询数据",
        "usage_en": "Switch the time range to view trends → ask the Copilot a plain-language question to query data"
    },
    "nav_cases": {
        "zh": "案例跟进",
        "en": "Case Follow-ups",
        "desc": "追踪用户意图链产生的案例（如咨询→投诉→退款），监控满意度，支持添加备注和结案",
        "desc_en": "Track cases formed by user intent chains (e.g. inquiry → complaint → refund), monitor satisfaction, add notes and close cases",
        "usage": "查看活跃 Case 列表 → 关注高风险标记 → 添加备注或结案",
        "usage_en": "Review the active case list → watch high-risk flags → add notes or close the case"
    },
    "nav_escalation": {
        "zh": "人工转接",
        "en": "Agent Handoff",
        "desc": "配置客服用户名、转接触发规则、排班与话术，让 AI 在需要时自动 @ 人工客服",
        "desc_en": "Configure agent usernames, handoff trigger rules, schedules and scripts so the AI auto-@s a human agent when needed",
        "usage": "启用 → 填写客服用户名 → 设置触发次数和冷却时间 → 保存",
        "usage_en": "Enable → fill in agent usernames → set trigger count and cooldown → save"
    },
    "nav_help": {
        "zh": "帮助",
        "en": "Help",
        "desc": "查看所有可用的 Bot 指令和操作说明，支持关键词搜索",
        "desc_en": "Browse all available bot commands and how-to guides, with keyword search",
        "usage": "输入关键词搜索 → 点击展开查看指令详情和用法",
        "usage_en": "Type a keyword to search → expand an item to see command details and usage"
    },
    "nav_import": {
        "zh": "导入/导出",
        "en": "Import / Export",
        "desc": "导入/导出配置文件（YAML/JSON/ZIP），管理 Webhook 变更通知",
        "desc_en": "Import/export configuration files (YAML/JSON/ZIP) and manage Webhook change notifications",
        "usage": "拖拽文件到上传区 → 选择覆盖或合并模式 → 确认导入",
        "usage_en": "Drag a file onto the upload area → choose Overwrite or Merge mode → confirm the import"
    },
    "nav_unified_inbox": {
        "zh": "坐席工作台",
        "en": "Agent Workspace",
        "desc": "多平台统一收件箱：坐席在此接待所有渠道的客户对话，支持人工/AI 协作回复（新窗口打开）",
        "desc_en": "Unified multi-platform inbox where agents handle customer chats from every channel, with human/AI co-reply (opens in a new window)",
        "usage": "点击在新窗口打开 → 扫码接入账号 → 认领会话开始接待",
        "usage_en": "Click to open in a new window → link accounts via QR → claim a conversation to start"
    },
    "nav_learner": {
        "zh": "学习队列",
        "en": "Learning Queue",
        "desc": "AI 从未命中对话中挖掘的新知识草稿，人工审核通过后自动写入知识库",
        "desc_en": "Knowledge drafts the AI mines from unanswered conversations; approved items are written into the Knowledge Base",
        "usage": "查看待审草稿 → 修改并通过 / 拒绝 → 通过项自动入库",
        "usage_en": "Review pending drafts → edit & approve / reject → approved entries auto-enter the KB"
    },
    "nav_personas": {
        "zh": "人设工作室",
        "en": "Persona Studio",
        "desc": "管理 AI 人设池、会话绑定与全局人设规则，决定 AI 用什么身份和口吻说话",
        "desc_en": "Manage the persona pool, session bindings and global persona rules — deciding the AI's identity and tone",
        "usage": "创建/编辑人设 → 设为默认或绑定到指定会话 → 配置全局规则",
        "usage_en": "Create/edit a persona → set as default or bind to sessions → configure global rules"
    },
    "nav_ai_studio": {
        "zh": "AI 工作室",
        "en": "AI Studio",
        "desc": "AI 能力聚合入口：人设指挥、情景记忆、学习审核、关系与身份绑定一站式管理",
        "desc_en": "Aggregated AI hub: persona command, episodic memory, learning review, relations and identity binding in one place",
        "usage": "从各功能卡片进入对应模块，或用顶部标签页切换",
        "usage_en": "Enter each module from its card, or switch with the top tabs"
    },
    "nav_rpa_overview": {
        "zh": "渠道总览",
        "en": "Channel Overview",
        "desc": "Telegram / LINE / Messenger / WhatsApp 四大渠道的运行状态、待审、告警与漏斗聚合看板",
        "desc_en": "Aggregate board for Telegram / LINE / Messenger / WhatsApp: runtime status, pending reviews, alerts and funnels",
        "usage": "先看告警和待审数 → 点击渠道卡片跳到对应渠道页处理",
        "usage_en": "Check alerts and pending counts first → click a channel card to jump to that channel's page"
    },
    "nav_telegram": {
        "zh": "Telegram 自动化",
        "en": "Telegram Automation",
        "desc": "配置 Telegram 主号的 AI 自动回复：接收范围、回复逻辑、屏蔽名单、语音与运营漏斗。坐席扫码登录的账号请在坐席工作台管理，不在此页",
        "desc_en": "Configure the Telegram main account's AI auto-reply: scope, reply logic, block list, voice and funnel. QR-linked agent accounts are managed in the Agent Workspace, not here",
        "usage": "选择消息处理范围 → 调整回复逻辑 → 保存后对主号自动回复生效",
        "usage_en": "Choose the message scope → tune reply logic → save to apply to the main account's auto-replies"
    },
    "nav_line_rpa": {
        "zh": "LINE 自动化",
        "en": "LINE Automation",
        "desc": "LINE 真机自动化运营台：设备监控、聊天设置、运维工具与运营漏斗",
        "desc_en": "LINE device-automation console: device monitoring, chat settings, ops tools and funnel",
        "usage": "监控页看设备状态 → 设置页调聊天行为 → 运维页处理异常",
        "usage_en": "Watch device status on Monitor → tune chat behavior in Settings → handle issues in Ops"
    },
    "nav_messenger_rpa": {
        "zh": "Messenger 自动化",
        "en": "Messenger Automation",
        "desc": "Messenger 网页自动化运营台：客户线索、人设策略、账号设备、审批质检与数据中心",
        "desc_en": "Messenger web-automation console: leads, persona strategy, accounts & devices, review QA and data center",
        "usage": "总览看运行态 → 客户线索跟进 → 审批质检处理待审消息",
        "usage_en": "Check the overview → follow up leads → clear pending items in Review QA"
    },
    "nav_whatsapp_rpa": {
        "zh": "WhatsApp 自动化",
        "en": "WhatsApp Automation",
        "desc": "WhatsApp 协议自动化：对话管理、待审队列、模板分析、配置与运维",
        "desc_en": "WhatsApp protocol automation: conversations, review queue, template analytics, config and ops",
        "usage": "对话页看会话 → 待审页处理草稿 → 模板分析优化话术",
        "usage_en": "Browse chats → clear the review queue → optimize scripts via template analytics"
    },
    "nav_episodic": {
        "zh": "AI 记忆",
        "en": "AI Memory",
        "desc": "查看与校正 AI 对每个用户的情景记忆，保证长期对话不「失忆」、不记错",
        "desc_en": "Inspect and correct the AI's episodic memory about each user, so long-running chats stay consistent",
        "usage": "搜索用户 → 查看记忆条目 → 修正错误记忆或删除过期信息",
        "usage_en": "Search a user → review memory entries → fix wrong memories or delete stale ones"
    },
    "nav_crisis_audit": {
        "zh": "危机审计",
        "en": "Crisis Audit",
        "desc": "危机安全链事件留痕：高危消息的识别、处理过程与复盘记录",
        "desc_en": "Audit trail of the crisis-safety chain: how high-risk messages were detected, handled and reviewed",
        "usage": "关注未处理事件 → 查看上下文 → 标记处理结果",
        "usage_en": "Watch unhandled events → inspect context → mark the outcome"
    },
    "nav_care": {
        "zh": "主动关怀",
        "en": "Proactive Care",
        "desc": "到点主动问候客户的关怀任务：排期、话术与发送状态一目了然",
        "desc_en": "Scheduled proactive check-ins: timing, scripts and delivery status at a glance",
        "usage": "查看今日待发关怀 → 调整排期或话术 → 跟踪发送结果",
        "usage_en": "See today's due check-ins → adjust schedule or script → track delivery"
    },
    "nav_relations_health": {
        "zh": "流失预警",
        "en": "Churn Alerts",
        "desc": "按关系健康度给用户分级，提前发现可能流失的客户并及时干预",
        "desc_en": "Grades users by relationship health, surfacing customers at churn risk early for intervention",
        "usage": "从高风险分组看起 → 打开用户详情 → 安排关怀或人工跟进",
        "usage_en": "Start with the high-risk group → open a user's detail → schedule care or manual follow-up"
    },
    "nav_monetization": {
        "zh": "变现营收",
        "en": "Monetization",
        "desc": "订阅、解锁、打赏等 C 端营收与转化漏斗报表",
        "desc_en": "Reports on consumer revenue (subscriptions, unlocks, tips) and conversion funnels",
        "usage": "切换时间范围看营收趋势 → 用漏斗定位转化瓶颈",
        "usage_en": "Switch the time range to view revenue trends → use the funnel to find conversion bottlenecks"
    },
    "nav_funnel": {
        "zh": "运营漏斗",
        "en": "Ops Funnel",
        "desc": "跨平台客户旅程漏斗:新客→活跃→转化各阶段人数、转化率与漏斗告警,支持按渠道筛选",
        "desc_en": "Cross-platform customer-journey funnel: stage counts, conversion rates and funnel alerts, filterable by channel",
        "usage": "看整体漏斗 → 用渠道筛选片定位掉量渠道 → 处理漏斗告警",
        "usage_en": "Review the overall funnel → use channel chips to find the leaking channel → handle funnel alerts"
    },
    # ── 通用术语 ───
    "buy_rate": {
        "zh": "买入费率",
        "en": "Buy-in Rate",
        "desc": "用户向该通道充值时平台收取的费率百分比",
        "desc_en": "The percentage fee the platform charges when a user tops up via this channel"
    },
    "sell_rate": {
        "zh": "卖出费率",
        "en": "Sell-out Rate",
        "desc": "用户从该通道提现时的费率",
        "desc_en": "The fee charged when a user withdraws from this channel"
    },
    "fee_rate": {
        "zh": "手续费率",
        "en": "Fee Rate",
        "desc": "该通道的交易手续费百分比。例如 0.5% 表示每笔扣除 0.5%",
        "desc_en": "The channel's transaction fee percentage. E.g. 0.5% deducts 0.5% per transaction"
    },
    "circuit_breaker": {
        "zh": "熔断器",
        "en": "Circuit Breaker",
        "desc": "当 AI 接口连续失败超过阈值时自动停止调用一段时间，防止雪崩",
        "desc_en": "Automatically halts calls for a while when the AI API fails consecutively beyond a threshold, preventing cascading failures"
    },
    "cooldown": {
        "zh": "冷却时间",
        "en": "Cooldown",
        "desc": "两次回复之间的最小间隔（秒），防止刷屏",
        "desc_en": "Minimum interval (seconds) between two replies, to prevent flooding"
    },
    "temperature": {
        "zh": "回复温度（Temperature）",
        "en": "Reply Temperature",
        "desc": "控制 AI 回复的随机性。0=最确定稳定，1=最多样创意。业务场景建议 0.3-0.5",
        "desc_en": "Controls how random the AI's replies are. 0 = most deterministic, 1 = most creative. 0.3-0.5 recommended for business",
        "usage": "拖动滑块调整，保存后对该策略下所有新回复生效",
        "usage_en": "Drag the slider; saving applies it to all new replies under this strategy"
    },
    "max_tokens": {
        "zh": "最大字数（Max Tokens）",
        "en": "Max Tokens",
        "desc": "AI 单次回复的最大 token 数。1 token ≈ 1.5 个中文字。256=简短，512=正常，1024=详细",
        "desc_en": "Max tokens per AI reply. 1 token ≈ 1.5 Chinese characters. 256 = short, 512 = normal, 1024 = detailed",
        "usage": "输入数值后保存。值越大回复越长，但速度变慢、费用增加",
        "usage_en": "Enter a value and save. Larger = longer replies, but slower and pricier"
    },
    "context_rounds": {
        "zh": "上下文轮数",
        "en": "Context Rounds",
        "desc": "传入 AI 的历史对话轮数。轮数越多上下文越丰富，但速度更慢费用更高",
        "desc_en": "Number of past dialogue rounds fed to the AI. More rounds = richer context but slower and pricier",
        "usage": "通常 3-5 轮即可。复杂业务场景可增至 8-10 轮",
        "usage_en": "3-5 rounds is usually enough; raise to 8-10 for complex scenarios"
    },
    "split_send": {
        "zh": "分条发送",
        "en": "Split Send",
        "desc": "长回复自动拆成多条消息发送，模拟真人打字节奏",
        "desc_en": "Long replies are auto-split into several messages, mimicking a human typing rhythm"
    },
    "reply_probability": {
        "zh": "回复概率",
        "en": "Reply Probability",
        "desc": "收到此类消息时实际回复的概率。0.1=只有10%的消息会回复",
        "desc_en": "Probability of actually replying to such a message. 0.1 = only 10% of messages get a reply",
        "usage": "设为 1.0 表示 100% 回复。水群场景可调低避免刷屏",
        "usage_en": "Set 1.0 for a 100% reply rate. Lower it in busy group chats to avoid flooding"
    },
    "skip_ai": {
        "zh": "跳过 AI",
        "en": "Skip AI",
        "desc": "启用后直接使用模板回复，不调用 AI 接口，响应极快（<1秒）",
        "desc_en": "When enabled, replies straight from templates without calling the AI API — extremely fast (<1s)",
        "usage": "适用于固定话术场景（如问候语），开启后节省 API 费用",
        "usage_en": "Good for fixed scripts (e.g. greetings); saves API cost"
    },
    "success_rate": {
        "zh": "成功率",
        "en": "Success Rate",
        "desc": "该通道交易成功的比例。低于 80% 建议排查或暂停",
        "desc_en": "Share of successful transactions on this channel. Below 80% warrants investigation or pausing"
    },
    "alert_threshold": {
        "zh": "告警阈值",
        "en": "Alert Threshold",
        "desc": "成功率低于此值时通道卡片显示红色告警",
        "desc_en": "When the success rate drops below this, the channel card shows a red alert"
    },
    "P50": {
        "zh": "响应中位数（P50）",
        "en": "Response Median (P50)",
        "desc": "50% 的请求在此时间内完成。反映典型用户体验",
        "desc_en": "50% of requests finish within this time. Reflects typical user experience"
    },
    "P90": {
        "zh": "响应 P90",
        "en": "Response P90",
        "desc": "90% 的请求在此时间内完成。反映大多数用户的最慢体验",
        "desc_en": "90% of requests finish within this time. Reflects most users' slowest experience"
    },
    "P99": {
        "zh": "响应 P99",
        "en": "Response P99",
        "desc": "99% 的请求在此时间内完成。用于发现极端慢请求",
        "desc_en": "99% of requests finish within this time. Used to surface extreme slow requests"
    },
    "SSE": {
        "zh": "服务端推送（SSE）",
        "en": "Server-Sent Events (SSE)",
        "desc": "Server-Sent Events，服务器实时推送日志到浏览器，无需手动刷新",
        "desc_en": "Server-Sent Events: the server pushes logs to the browser in real time, no manual refresh needed"
    },
    "minimum_amount": {
        "zh": "最小金额",
        "en": "Minimum Amount",
        "desc": "该通道允许的单笔最小交易金额",
        "desc_en": "The smallest single-transaction amount this channel allows"
    },
    "maximum_amount": {
        "zh": "最大金额",
        "en": "Maximum Amount",
        "desc": "该通道允许的单笔最大交易金额",
        "desc_en": "The largest single-transaction amount this channel allows"
    },
    "processing_time": {
        "zh": "处理时间",
        "en": "Processing Time",
        "desc": "从提交到完成的平均处理耗时",
        "desc_en": "Average time from submission to completion"
    },
    "per_user": {
        "zh": "用户冷却",
        "en": "Per-User Cooldown",
        "desc": "同一用户两次触发回复的最小间隔",
        "desc_en": "Minimum interval between two reply triggers from the same user"
    },
    "per_content": {
        "zh": "内容冷却",
        "en": "Per-Content Cooldown",
        "desc": "相同内容重复发送时的冷却时间",
        "desc_en": "Cooldown applied when identical content is sent repeatedly"
    },
    "global": {
        "zh": "全局冷却",
        "en": "Global Cooldown",
        "desc": "所有回复之间的最小间隔",
        "desc_en": "Minimum interval between all replies"
    },
    "by_intent": {
        "zh": "意图冷却",
        "en": "Per-Intent Cooldown",
        "desc": "按意图类型分别设置的冷却时间",
        "desc_en": "Cooldown configured separately by intent type"
    },
    "enabled": {
        "zh": "已启用",
        "en": "Enabled",
        "desc": "此功能/通道当前处于开启状态",
        "desc_en": "This feature/channel is currently switched on"
    },
    "rate_limit": {
        "zh": "限流",
        "en": "Rate Limiting",
        "desc": "防止短时间内过多请求的保护机制，基于令牌桶算法",
        "desc_en": "A protection mechanism preventing too many requests in a short time, based on the token-bucket algorithm"
    },
    "webhook": {
        "zh": "Webhook 推送通知",
        "en": "Webhook Notifications",
        "desc": "配置变更时自动推送通知到外部系统（Slack/微信/Telegram）",
        "desc_en": "Auto-push notifications to external systems (Slack/WeChat/Telegram) when config changes",
        "usage": "填写推送地址 → 选择监听事件 → 保存 → 发送测试验证",
        "usage_en": "Enter the push URL → choose events to watch → save → send a test to verify"
    },
    "intent": {
        "zh": "意图",
        "en": "Intent",
        "desc": "用户消息被 AI 识别为的类型，如 greeting（问候）、order_query（查单）、complaint（投诉）等",
        "desc_en": "The type the AI classifies a user message as, e.g. greeting, order_query, complaint, etc."
    },
    "strategy": {
        "zh": "回复策略",
        "en": "Reply Strategy",
        "desc": "根据消息意图选择的 AI 参数组合（温度、输出长度、上下文轮数等）",
        "desc_en": "The AI parameter set (temperature, output length, context rounds, etc.) chosen by message intent"
    },
    "follow_up_rate": {
        "zh": "追问率",
        "en": "Follow-up Rate",
        "desc": "用户在收到 AI 回复后 5 分钟内再次发送消息的比例；高追问率可能表示回复质量不足",
        "desc_en": "Share of users who message again within 5 minutes of an AI reply; a high rate may signal insufficient reply quality"
    },
    "same_intent_rate": {
        "zh": "同意图追问率",
        "en": "Same-Intent Follow-up Rate",
        "desc": "追问消息与原始消息具有相同意图的比例；越高说明 AI 可能没解决用户问题",
        "desc_en": "Share of follow-ups carrying the same intent as the original; higher means the AI likely didn't solve the problem"
    },
    "silence_rate": {
        "zh": "静默率",
        "en": "Silence Rate",
        "desc": "回复后用户无追问的比例；高静默率通常表示一次解决",
        "desc_en": "Share of replies with no follow-up; a high rate usually means one-shot resolution"
    },
    "template_hit_rate": {
        "zh": "模板命中率",
        "en": "Template Hit Rate",
        "desc": "使用预设模板直接回复（未调用 AI API）的比例；可节省 API 调用",
        "desc_en": "Share of replies served directly from preset templates (no AI API call); saves API calls"
    },
    "backfill": {
        "zh": "回填",
        "en": "Backfill",
        "desc": "当用户新消息到达时，系统回溯标记前一条 AI 回复的追问状态",
        "desc_en": "When a new user message arrives, the system retroactively marks the previous AI reply's follow-up status"
    },
    "strategy_event": {
        "zh": "策略事件",
        "en": "Strategy Event",
        "desc": "每次 AI 通过某个策略回复用户时记录的一条追踪数据",
        "desc_en": "A tracking record logged each time the AI replies via a given strategy"
    },
    "quality_score": {
        "zh": "质量评分",
        "en": "Quality Score",
        "desc": "综合评分 0-100，融合响应速度(20%)、一次解决率(35%)、同意图追问惩罚(30%)、API 效率(15%)",
        "desc_en": "Composite score 0-100, blending response speed (20%), one-shot resolution (35%), same-intent follow-up penalty (30%) and API efficiency (15%)"
    },
    "ab_test": {
        "zh": "A/B 灰度测试",
        "en": "A/B Test",
        "desc": "对同一意图分流多个策略，用一致性哈希保证同一用户始终进入同一分桶，实现对照实验",
        "desc_en": "Splits one intent across multiple strategies, using consistent hashing so a user always lands in the same bucket — a controlled experiment",
        "usage": "创建两个策略 → 在意图映射中同时绑定 → 系统自动分流 → 查看策略效果对比",
        "usage_en": "Create two strategies → bind both in the intent mapping → the system auto-splits → compare in Strategy Performance"
    },
    "data_retention": {
        "zh": "数据保留",
        "en": "Data Retention",
        "desc": "策略追踪事件和通用事件的自动清理周期（天数），防止数据库无限膨胀",
        "desc_en": "Auto-cleanup period (days) for strategy-tracking and generic events, preventing unbounded database growth"
    },
    "advisor": {
        "zh": "智能诊断",
        "en": "Smart Diagnostics",
        "desc": "系统自动分析策略指标，检测异常（高追问率、慢响应等）并给出参数调整建议",
        "desc_en": "Auto-analyzes strategy metrics, detects anomalies (high follow-up rate, slow response, etc.) and suggests parameter tweaks"
    },
    "purge": {
        "zh": "数据清理",
        "en": "Data Purge",
        "desc": "删除超过保留期限的旧追踪数据并回收磁盘空间（VACUUM）",
        "desc_en": "Deletes tracking data past its retention period and reclaims disk space (VACUUM)"
    },
    "autopilot": {
        "zh": "自动驾驶（Auto-Pilot）",
        "en": "Auto-Pilot",
        "desc": "系统持续监测策略质量评分，当某策略评分低于阈值时自动将其映射的意图切换到更优策略",
        "desc_en": "Continuously monitors strategy quality scores and auto-switches an intent's mapping to a better strategy when a score drops below threshold"
    },
    "session": {
        "zh": "会话",
        "en": "Session",
        "desc": "同一用户在 30 分钟内的连续交互归为一个会话，用于追踪整体对话解决率",
        "desc_en": "A user's continuous interactions within 30 minutes count as one session, used to track overall resolution"
    },
    "resolve_rate": {
        "zh": "解决率",
        "en": "Resolution Rate",
        "desc": "会话中最后一条回复后用户未再追问的比例；高解决率意味着问题一次性解决",
        "desc_en": "Share of sessions with no follow-up after the last reply; high means problems were solved in one go"
    },
    "param_suggestion": {
        "zh": "参数微调建议",
        "en": "Parameter Tuning Suggestion",
        "desc": "系统根据策略指标异常自动生成具体参数调整建议（如增加 context_rounds），可一键应用",
        "desc_en": "Concrete parameter-adjustment suggestions auto-generated from metric anomalies (e.g. raise context_rounds), applicable in one click"
    },
    "model_id": {
        "zh": "模型 ID",
        "en": "Model ID",
        "desc": "当前策略使用的 AI 模型名称，可在策略中配置以实现模型级 A/B 测试",
        "desc_en": "The AI model name used by the current strategy; configurable per strategy for model-level A/B testing"
    },
    "model_ab": {
        "zh": "模型 A/B 对比",
        "en": "Model A/B Comparison",
        "desc": "将相同策略配置不同模型，通过 A/B 灰度分流对比模型效果差异",
        "desc_en": "Configure different models for the same strategy and compare their effect via A/B traffic splitting"
    },
    "user_segment": {
        "zh": "用户分群",
        "en": "User Segments",
        "desc": "按活跃度分群：高频(≥10条)、中频(3-9条)、低频(1-2条)，分析各群体对策略的响应差异",
        "desc_en": "Segments by activity: heavy (≥10 msgs), moderate (3-9), light (1-2), to analyze how each group responds to strategies"
    },
    "heavy": {
        "zh": "高频用户",
        "en": "Heavy Users",
        "desc": "分析窗口内产生 ≥10 条交互的用户群体，通常是核心用户",
        "desc_en": "Users with ≥10 interactions in the analysis window — usually core users"
    },
    "moderate": {
        "zh": "中频用户",
        "en": "Moderate Users",
        "desc": "分析窗口内产生 3-9 条交互的用户群体",
        "desc_en": "Users with 3-9 interactions in the analysis window"
    },
    "light": {
        "zh": "低频用户",
        "en": "Light Users",
        "desc": "分析窗口内仅 1-2 条交互的用户群体，可能是新用户或偶尔使用者",
        "desc_en": "Users with only 1-2 interactions in the analysis window — possibly new or occasional users"
    },
    # ── AI / 配置术语 ───
    "api_key": {
        "zh": "API 密钥（API Key）",
        "en": "API Key",
        "desc": "调用 AI 服务的身份验证密钥，类似密码，请妥善保管不要泄露",
        "desc_en": "Authentication key for calling the AI service, like a password — keep it safe and never leak it",
        "usage": "从 AI 服务商后台复制密钥 → 粘贴到此处 → 点击测试连接验证",
        "usage_en": "Copy the key from your AI provider's console → paste it here → click Test Connection to verify"
    },
    "base_url": {
        "zh": "接口地址（Base URL）",
        "en": "Base URL",
        "desc": "AI 服务的 API 访问地址。不同服务商地址不同",
        "desc_en": "The AI service's API endpoint. It differs by provider",
        "usage": "填写完整 URL（含 https://），如 https://generativelanguage.googleapis.com",
        "usage_en": "Enter the full URL (incl. https://), e.g. https://generativelanguage.googleapis.com"
    },
    "embedding_model": {
        "zh": "向量模型（Embedding）",
        "en": "Embedding Model",
        "desc": "将文本转换为数学向量的模型，用于知识库语义搜索（理解意思而非精确匹配）",
        "desc_en": "A model that turns text into mathematical vectors, used for Knowledge Base semantic search (understanding meaning, not exact matching)",
        "usage": "通常使用默认模型即可。如需更换，确保模型支持中文",
        "usage_en": "The default model is usually fine. If you change it, make sure it supports Chinese"
    },
    "system_prompt": {
        "zh": "系统提示词（System Prompt）",
        "en": "System Prompt",
        "desc": "AI 的核心指令，定义 AI 的角色、风格、回复规则。所有回复都受此约束",
        "desc_en": "The AI's core instruction, defining its role, style and reply rules. Every reply is bound by it",
        "usage": "编辑提示词 → 保存即生效。可用右侧快捷按钮跳转到不同段落",
        "usage_en": "Edit the prompt → save to apply. Use the side buttons to jump between sections"
    },
    "thinking_budget": {
        "zh": "思考预算（Thinking Budget）",
        "en": "Thinking Budget",
        "desc": "AI 深度推理的 token 预算，越高推理越深入但速度越慢、费用更高",
        "desc_en": "Token budget for the AI's deep reasoning; higher = deeper reasoning but slower and pricier",
        "usage": "简单问答设 0（关闭推理），复杂业务场景设 1024-4096",
        "usage_en": "Set 0 for simple Q&A (reasoning off); 1024-4096 for complex scenarios"
    },
    "session_key": {
        "zh": "会话密钥（Session Key）",
        "en": "Session Key",
        "desc": "Web 管理面板的登录会话加密密钥，用于保护 Cookie 安全",
        "desc_en": "Encryption key for the web admin login session, protecting cookie security",
        "usage": "初始化时自动生成，无需手动修改。如需重置登录状态可更换",
        "usage_en": "Auto-generated at init; no manual change needed. Rotate it to reset all login states"
    },
    # ── 知识库术语 ───
    "kb_entry": {
        "zh": "知识条目",
        "en": "Knowledge Entry",
        "desc": "知识库中的一条记录，包含标题、触发词、处理步骤、示例回复等字段",
        "desc_en": "A Knowledge Base record with fields like title, triggers, handling steps and a sample reply",
        "usage": "创建条目 → 设置触发词 → 用沙盒测试命中效果",
        "usage_en": "Create an entry → set triggers → verify hits with the sandbox test"
    },
    "triggers": {
        "zh": "触发词",
        "en": "Trigger Words",
        "desc": "用户消息匹配这些关键词时会命中对应的知识条目。支持多个词，用逗号分隔",
        "desc_en": "When a user message matches these keywords, the matching entry is hit. Multiple words allowed, comma-separated",
        "usage": "添加常见的用户提问关键词，如「查单」「订单号」「到账」",
        "usage_en": "Add common user-question keywords, e.g. \"track order\", \"order number\", \"received\""
    },
    "sandbox": {
        "zh": "沙盒测试",
        "en": "Sandbox Test",
        "desc": "在安全环境中测试 KB 搜索和 AI 回复效果，不影响真实用户",
        "desc_en": "Test KB search and AI replies in a safe environment without affecting real users",
        "usage": "输入模拟用户消息 → 查看命中的条目和 AI 生成的回复",
        "usage_en": "Enter a simulated user message → see the matched entry and the AI-generated reply"
    },
    "vectorize": {
        "zh": "向量化",
        "en": "Vectorize",
        "desc": "将所有知识条目转为数学向量，启用语义搜索（理解含义而非仅关键词匹配）",
        "desc_en": "Convert all knowledge entries into vectors to enable semantic search (meaning, not just keyword matching)",
        "usage": "点击后等待处理完成。新增条目后建议重新向量化",
        "usage_en": "Click and wait for processing. Re-vectorize after adding new entries"
    },
    "batch_translate": {
        "zh": "批量翻译",
        "en": "Batch Translate",
        "desc": "将所有未翻译的 KB 条目自动翻译为英语/乌尔都语/葡萄牙语/阿拉伯语",
        "desc_en": "Auto-translate all untranslated KB entries into English/Urdu/Portuguese/Arabic",
        "usage": "点击后自动翻译，翻译结果可在「翻译审核」标签页中审核",
        "usage_en": "Click to auto-translate; review the results in the \"Translation Review\" tab"
    },
    "hit_rate": {
        "zh": "命中率",
        "en": "Hit Rate",
        "desc": "用户消息能匹配到 KB 知识条目的比例。命中率越高，AI 回复质量越好",
        "desc_en": "Share of user messages that match a KB entry. Higher hit rate = better AI reply quality",
        "usage": "命中率低于 60% 时建议扩充知识库或优化触发词",
        "usage_en": "Below 60%, consider expanding the KB or refining triggers"
    },
    "miss_log": {
        "zh": "未命中记录",
        "en": "Miss Log",
        "desc": "用户消息没有匹配到任何 KB 条目时的记录，用于发现知识盲区",
        "desc_en": "Records of user messages that matched no KB entry, used to find knowledge gaps",
        "usage": "定期查看未命中记录 → 为高频未命中创建新的知识条目",
        "usage_en": "Review misses regularly → create new entries for frequent misses"
    },
    "kb_feedback": {
        "zh": "效果反馈",
        "en": "Feedback",
        "desc": "对 AI 回复的评价记录（好评/差评），用于持续优化知识库内容",
        "desc_en": "Records of ratings (thumbs up/down) on AI replies, used to continuously improve KB content"
    },
    "bm25": {
        "zh": "BM25 搜索",
        "en": "BM25 Search",
        "desc": "基于关键词频率的传统搜索算法，速度快但无法理解语义",
        "desc_en": "A traditional keyword-frequency search algorithm — fast but unable to understand semantics"
    },
    "semantic_search": {
        "zh": "语义搜索",
        "en": "Semantic Search",
        "desc": "基于向量的搜索，能理解同义词和相似表达（如「退钱」=「退款」）",
        "desc_en": "Vector-based search that understands synonyms and similar phrasing (e.g. \"give my money back\" = \"refund\")"
    },
    # ── 日志级别 ───
    "DEBUG": {
        "zh": "调试日志（DEBUG）",
        "en": "Debug Log (DEBUG)",
        "desc": "最详细的日志级别，包含开发调试信息。通常仅开发人员使用",
        "desc_en": "The most detailed log level, including dev-debug info. Usually for developers only",
        "usage": "选中此按钮可查看所有级别的日志，信息量最大",
        "usage_en": "Select this to see logs of all levels — the most verbose"
    },
    "INFO": {
        "zh": "信息日志（INFO）",
        "en": "Info Log (INFO)",
        "desc": "常规运行信息，如消息收发、回复生成、KB 查询等正常操作",
        "desc_en": "Routine runtime info such as message send/receive, reply generation and KB queries — normal operations",
        "usage": "日常监控推荐使用此级别",
        "usage_en": "Recommended for day-to-day monitoring"
    },
    "WARNING": {
        "zh": "警告日志（WARNING）",
        "en": "Warning Log (WARNING)",
        "desc": "潜在问题警告，如成功率下降、KB 未命中、接口超时等",
        "desc_en": "Warnings of potential issues, e.g. dropping success rate, KB misses, API timeouts",
        "usage": "重点关注此级别，可提前发现问题",
        "usage_en": "Watch this level closely to catch problems early"
    },
    "ERROR": {
        "zh": "错误日志（ERROR）",
        "en": "Error Log (ERROR)",
        "desc": "运行错误，需要关注但系统仍可运行。如 API 调用失败、数据异常等",
        "desc_en": "Runtime errors needing attention while the system still runs, e.g. API call failures, data anomalies",
        "usage": "出现 ERROR 时应检查原因并尽快修复",
        "usage_en": "On ERROR, investigate the cause and fix it promptly"
    },
    "CRITICAL": {
        "zh": "严重错误（CRITICAL）",
        "en": "Critical Error (CRITICAL)",
        "desc": "系统级故障，如数据库连接断开、服务崩溃等，需要立即处理",
        "desc_en": "System-level failures such as a dropped database connection or a crashed service — handle immediately",
        "usage": "CRITICAL 日志出现时应立即排查，可能影响所有用户",
        "usage_en": "When CRITICAL appears, investigate at once; it may affect all users"
    },
    # ── Case / 意图链 ───
    "case_id": {
        "zh": "案例 ID",
        "en": "Case ID",
        "desc": "系统自动为识别到的意图链模式分配的唯一追踪编号",
        "desc_en": "A unique tracking number the system assigns to each detected intent-chain pattern"
    },
    "intent_chain": {
        "zh": "意图链",
        "en": "Intent Chain",
        "desc": "用户在对话中的意图变化轨迹，如「查单 → 投诉 → 退款」",
        "desc_en": "The trajectory of a user's intent changes in a conversation, e.g. \"track order → complaint → refund\"",
        "usage": "通过意图链可判断用户问题是否在升级，及时干预",
        "usage_en": "An intent chain shows whether a user's issue is escalating, so you can step in early"
    },
    "satisfaction": {
        "zh": "满意度",
        "en": "Satisfaction",
        "desc": "基于用户行为（追问、投诉、催促等）实时计算的满意度评分，范围 0-100",
        "desc_en": "A real-time satisfaction score (0-100) computed from user behavior (follow-ups, complaints, prompting, etc.)",
        "usage": "低于 40 分标记为高风险，建议人工介入",
        "usage_en": "Below 40 is flagged high-risk; human intervention is advised"
    },
    "at_risk": {
        "zh": "高风险",
        "en": "At Risk",
        "desc": "满意度评分低于阈值的用户，可能即将流失或投诉",
        "desc_en": "Users whose satisfaction score is below threshold, who may churn or complain soon",
        "usage": "在 Case 列表中带红色标记的即为高风险用户",
        "usage_en": "Users flagged red in the case list are high-risk"
    },
    "escalation": {
        "zh": "人工升级",
        "en": "Escalation",
        "desc": "AI 判断无法解决用户问题时，自动标记为需要人工客服介入",
        "desc_en": "When the AI judges it can't solve the issue, it auto-flags the case for a human agent"
    },
    "close_case": {
        "zh": "结案",
        "en": "Close Case",
        "desc": "标记案例为已解决，并记录解决方案",
        "desc_en": "Mark a case as resolved and record the solution",
        "usage": "点击结案按钮 → 输入解决说明 → 确认",
        "usage_en": "Click Close → enter the resolution note → confirm"
    },
    # ── 数据分析 ───
    "copilot": {
        "zh": "运营 Copilot",
        "en": "Operations Copilot",
        "desc": "AI 助手，用自然语言查询内部运营数据。支持问「今天知识库命中率多少？」等问题",
        "desc_en": "An AI assistant for querying internal operations data in natural language. Ask things like \"What's today's KB hit rate?\"",
        "usage": "在输入框输入中文问题 → 按回车 → 等待 AI 分析并返回结果",
        "usage_en": "Type a question in the box → press Enter → wait for the AI to analyze and return results"
    },
    # ── 按钮操作 ───
    "btn_refresh": {
        "zh": "刷新",
        "en": "Refresh",
        "desc": "重新从服务器加载最新数据",
        "desc_en": "Reload the latest data from the server",
        "usage": "点击后等待数据更新。页面数据通常每 30 秒自动刷新",
        "usage_en": "Click and wait for the update. Page data usually auto-refreshes every 30s"
    },
    "btn_save": {
        "zh": "保存",
        "en": "Save",
        "desc": "将当前修改保存到服务器，立即生效",
        "desc_en": "Save the current changes to the server, effective immediately",
        "usage": "修改完成后点击保存。也可使用快捷键 Ctrl+S",
        "usage_en": "Click Save when done. You can also use the Ctrl+S shortcut"
    },
    "btn_export": {
        "zh": "导出",
        "en": "Export",
        "desc": "将当前数据导出为文件（CSV/JSON/YAML）下载到本地",
        "desc_en": "Export the current data as a file (CSV/JSON/YAML) to download locally",
        "usage": "点击后浏览器自动下载文件",
        "usage_en": "Click and the browser downloads the file automatically"
    },
    "btn_import": {
        "zh": "导入",
        "en": "Import",
        "desc": "从本地文件导入数据到系统",
        "desc_en": "Import data into the system from a local file",
        "usage": "选择文件 → 确认导入模式（覆盖/合并） → 确认",
        "usage_en": "Choose a file → confirm the import mode (overwrite/merge) → confirm"
    },
    "btn_test": {
        "zh": "测试",
        "en": "Test",
        "desc": "验证当前配置是否能正常工作",
        "desc_en": "Verify whether the current configuration works",
        "usage": "点击后等待测试结果，成功会显示绿色提示",
        "usage_en": "Click and wait for the result; success shows a green prompt"
    },
    "btn_delete": {
        "zh": "删除",
        "en": "Delete",
        "desc": "永久删除此项，操作不可恢复",
        "desc_en": "Permanently delete this item; the action cannot be undone",
        "usage": "点击后需要二次确认才会执行",
        "usage_en": "A second confirmation is required before it executes"
    },
    "btn_pause": {
        "zh": "暂停/继续",
        "en": "Pause / Resume",
        "desc": "暂停实时数据流，方便查看当前内容",
        "desc_en": "Pause the live data stream to inspect current content",
        "usage": "暂停后数据不再滚动，再次点击恢复",
        "usage_en": "When paused, data stops scrolling; click again to resume"
    },
    # ── 导入/导出格式 ───
    "JSON": {
        "zh": "JSON 格式",
        "en": "JSON Format",
        "desc": "通用数据格式，结构化存储，适合程序处理和 API 传输",
        "desc_en": "A universal, structured data format suited to programmatic processing and API transport"
    },
    "CSV": {
        "zh": "CSV 格式",
        "en": "CSV Format",
        "desc": "逗号分隔表格格式，可直接用 Excel/WPS 打开编辑",
        "desc_en": "Comma-separated tabular format, openable and editable directly in Excel/WPS"
    },
    "YAML": {
        "zh": "YAML 格式",
        "en": "YAML Format",
        "desc": "人类友好的配置文件格式，层级清晰易读",
        "desc_en": "A human-friendly config file format with a clear, readable hierarchy"
    },
    "ZIP": {
        "zh": "ZIP 压缩包",
        "en": "ZIP Archive",
        "desc": "将多个文件打包压缩为一个文件，方便传输和备份",
        "desc_en": "Bundles multiple files into one compressed file for easy transfer and backup"
    },
    "HMAC": {
        "zh": "HMAC-SHA256 签名",
        "en": "HMAC-SHA256 Signature",
        "desc": "消息认证码算法，用于验证 Webhook 推送消息的真实性，防止伪造",
        "desc_en": "A message-authentication-code algorithm used to verify the authenticity of Webhook push messages and prevent forgery"
    },
    # ── 通道状态 ───
    "ch_enabled": {
        "zh": "启用",
        "en": "Enabled",
        "desc": "通道正常运行中，可以处理交易",
        "desc_en": "The channel is running normally and can process transactions"
    },
    "ch_maintenance": {
        "zh": "维护中",
        "en": "Under Maintenance",
        "desc": "通道暂停服务，正在维护。AI 会告知用户通道维护状态",
        "desc_en": "The channel is paused for maintenance. The AI will tell users about the maintenance status"
    },
    "ch_fluctuation": {
        "zh": "波动",
        "en": "Fluctuating",
        "desc": "通道成功率不稳定，可能影响交易。AI 会提醒用户注意风险",
        "desc_en": "The channel's success rate is unstable and may affect transactions. The AI will warn users of the risk"
    },
    # ── 用户角色 ───
    "role_master": {
        "zh": "主帐号",
        "en": "Master",
        "desc": "拥有全部权限，可管理其他用户、修改系统设置、查看所有数据",
        "desc_en": "Has full permissions: manage other users, change system settings, and view all data"
    },
    "role_admin": {
        "zh": "管理员",
        "en": "Admin",
        "desc": "拥有编辑权限，可管理模板、通道、知识库，但不能管理用户和系统设置",
        "desc_en": "Has edit permissions: manage templates, channels and the knowledge base, but not users or system settings"
    },
    "role_viewer": {
        "zh": "观察员",
        "en": "Viewer",
        "desc": "只读权限，可查看所有数据但不能修改任何配置",
        "desc_en": "Read-only: can view all data but cannot modify any configuration"
    },
    # ── brand / site_name ───
    "bot_admin": {
        "zh": "无界科技 · 智聊",
        "en": "Boundless · ChatX",
        "desc": "面向 Telegram 等渠道的 AI 客服与人工协同后台，用于知识库、案例、转接与监控",
        "desc_en": "An AI customer-service and human-collaboration backend for channels like Telegram, covering knowledge base, cases, handoff and monitoring"
    },
    # ── 其他 ───
    "token": {
        "zh": "Token（令牌）",
        "en": "Token",
        "desc": "AI 处理文本的基本单位。1 个 token 约等于 1.5 个中文字或 0.75 个英文单词",
        "desc_en": "The basic unit the AI processes text in. 1 token ≈ 1.5 Chinese characters or 0.75 English words"
    },
    "Bot": {
        "zh": "Bot（机器人）",
        "en": "Bot",
        "desc": "Telegram 上的 AI 客服机器人，自动接收并回复用户消息",
        "desc_en": "The AI customer-service bot on Telegram that automatically receives and replies to user messages"
    },
    "Case": {
        "zh": "Case（案例/工单）",
        "en": "Case",
        "desc": "由系统自动识别的用户对话升级事件，需要运营关注和跟进",
        "desc_en": "A user-conversation escalation event auto-detected by the system, needing ops attention and follow-up"
    }
}


def get_help_terms() -> dict:
    """供 admin.py 与渲染类测试注入模板上下文(静态数据,进程内单例)。"""
    return HELP_TERMS
