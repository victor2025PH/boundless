# -*- coding: utf-8 -*-
"""assistant 任务导向 how-to 语料包（第三源，2026-08-20 评审车道补齐）。

背景：20 问金标评测实锤 help_terms（名词解释）+ nav_schema（页面入口）
两源对「怎么做 X」类问题命中率仅 ~50%（DoD 线 80%）——用户问的是
操作步骤，语料里只有名词。本包补 30+ 条任务条目。

内容红线（与 seed_corpus 同约）：每条都必须是**产品当前真实行为**
（依据：代码/规则/已验证的 UI 事实），配置文件类操作一律注明
「请管理员操作」；禁止写「我以为有」的功能——助手编造功能是一票
否决项。新增条目时先确认功能真实存在。

id 稳定（howto:<slug>），重跑 = 幂等 upsert。path 供前端「带我去」。
"""
from __future__ import annotations

# (slug, title, title_en, content, content_en, keywords, path)
_HOWTO: list[tuple[str, str, str, str, str, str, str]] = [
    (
        "send-voice",
        "怎么给客户发语音消息",
        "How to send a voice message",
        "在坐席工作台打开会话，右栏「语音」组件输入要说的话 → 点「🎙️ 生成语音」"
        "试听 → 满意后点发送（发送的就是你试听的那条音频）。输入区语音按钮同样"
        "可生成并发送语音。文字改动后需重新生成，防止发出未试听的内容。",
        "Open the conversation in the workspace, use the Voice panel on the "
        "right: type the text, click Generate to preview, then Send (what you "
        "hear is exactly what is sent). Regenerate after editing the text.",
        "发语音 语音消息 语音 录音 voice 试听 克隆声 生成语音",
        "/workspace",
    ),
    (
        "voice-enroll",
        "怎么登记/克隆一个新音色",
        "How to enroll (clone) a new voice",
        "在坐席工作台右栏「语音」组件上传一段参考音频即可零样本登记克隆声"
        "（系统自动生成逐字稿并预热）。人设的默认音色在「人设工作室」的语音"
        "档位里配置，请管理员操作。",
        "Upload a reference audio clip in the workspace Voice panel to enroll "
        "a cloned voice (zero-shot). Default persona voices are configured in "
        "Persona Studio by an admin.",
        "音色 克隆 登记 声音 参考音 enroll clone voice 换声",
        "/workspace",
    ),
    (
        "change-password",
        "怎么修改登录密码",
        "How to change my login password",
        "点右上角头像打开用户菜单 → 「修改密码」，输入旧密码与新密码保存即可。"
        "忘记密码请联系管理员：在「用户管理」页你的帐号卡上点「🔑 重置密码」给你设一个新密码。",
        "Open the user menu (top-right avatar) → Change password. If you "
        "forgot it, ask an admin to click \"Reset password\" on your card in the Users page.",
        "改密码 修改密码 密码 password 登录",
        "",
    ),
    (
        "reset-user-password",
        "怎么给坐席/子帐号重置密码（忘记密码）",
        "How to reset a team member's password (forgotten password)",
        "「用户管理」页找到该帐号的卡片 → 点「🔑 重置密码」→ 输入新密码（至少 6 位，"
        "也可点「🎲 生成」随机生成）→ 「保存」。保存后会弹出一次性的「登录信息」卡"
        "（登录地址 / 用户名 / 新密码），点「复制全部」发给对方——关闭后密码不再显示。"
        "旧密码立即失效；对方已登录的设备不会被踢出，需要的话在页面下方「已登录的设备」"
        "里踢出。主帐号的密码只能由自己在「修改密码」里改，不能在这里重置。",
        "On the Users page find the account card → click Reset password → enter a new "
        "password (6+ characters, or click Generate) → Save. A one-time sign-in info card "
        "(address / username / new password) appears — click Copy all and send it to the "
        "person; the password is not shown again after closing. The old password stops "
        "working immediately; devices already signed in are not kicked out (use "
        "\"Signed-in devices\" below if needed). The master account can only change its "
        "own password via Change password.",
        "重置密码 忘记密码 坐席密码 子帐号密码 密码忘了 改坐席密码 重设密码 reset password "
        "登录信息 复制登录信息",
        "/users",
    ),
    (
        "logout-switch-account",
        "怎么退出登录 / 在桌面端换成子帐号登录",
        "How to sign out / switch to a sub-account in the desktop app",
        "点右上角头像 → 「退出登录」→ 确认。退出后会停在登录页，用你的帐号密码登录即可"
        "（系统管理员可切到「管理员令牌登录」）。在智聊桌面端，退出后自动登录会暂停，"
        "这样坐席才能用自己的子帐号登录；重新打开应用会恢复主帐号自动登录。坐席工作台"
        "里的退出入口在左上角用户菜单的「退出登录」。",
        "Click the avatar (top right) → Sign out → confirm. You land on the sign-in page; "
        "sign in with your username and password (system admins can switch to admin token "
        "sign-in). In the ChatX desktop app, automatic sign-in is paused after signing out "
        "so an agent can sign in with their own sub-account; reopening the app restores "
        "master auto sign-in. In the agent workspace the Sign out item is in the user menu.",
        "退出登录 退出 登出 注销 换帐号 切换帐号 换账号 子帐号登录 坐席登录 登录页 "
        "sign out logout switch account 自动登录",
        "",
    ),
    (
        "sub-account-cannot-login",
        "新建的子帐号登不上怎么办",
        "A newly created sub-account cannot sign in — what to check",
        "按顺序排查：① 桌面端要先退出主帐号（右上角头像 → 退出登录），停在登录页后再用"
        "子帐号登录；② 「用户管理」页头有「帐号所在后端」——子帐号只存在于创建它的那台"
        "后端，坐席机若连的是另一台后端要在那台上创建；③ 看帐号卡上是否有「已禁用」，"
        "有则在「⋯」菜单里点「启用」；④ 密码不确定就点「🔑 重置密码」重新设一个并把"
        "登录信息复制给对方。卡上「从未登录」表示该帐号还没成功登录过。",
        "Check in order: 1) in the desktop app sign out of the master account first "
        "(avatar → Sign out) and sign in with the sub-account from the sign-in page; "
        "2) the Users page header shows which backend the accounts live on — a sub-account "
        "exists only on the backend where it was created, so a seat pointing at another "
        "backend needs it created there; 3) if the card shows Disabled, enable it from the "
        "⋯ menu; 4) if the password is uncertain, click Reset password and copy the sign-in "
        "info to the person. \"Never signed in\" on the card means no successful sign-in yet.",
        "登不上 登录不了 无法登录 登录不进去 进不去 登不进 子帐号登录失败 新账号 新开的号 "
        "刚开的账号 刚建的账号 密码错误 从未登录 帐号所在后端 已禁用 cannot login sign-in failed",
        "/users",
    ),
    (
        "rewatch-tour",
        "新手引导在哪（已下线）",
        "Where is the onboarding tour (retired)",
        "新手引导已下线（2026-08-31 起）：产品改为装好即用，不再有首启向导和"
        "页面导览。想了解某个功能直接问小智（例如「怎么发语音」「工作链怎么用」），"
        "或问「这个软件怎么用」看整体上手主线。",
        "The onboarding tour was retired (2026-08-31): the product is now "
        "install-and-go with no first-run wizard or page tours. Just ask me "
        "about any feature (e.g. \"how to send a voice message\"), or ask "
        "\"how to use this system\" for the getting-started overview.",
        "新手引导 引导 教程 tour 上手 重看 向导 没有引导",
        "",
    ),
    (
        "shortcuts",
        "快捷键一览在哪看",
        "Where to see keyboard shortcuts",
        "在后台管理页按 ? 键打开快捷键帮助面板；按 Ctrl+K 打开命令面板可搜索"
        "页面与操作。收件箱里有键盘速查按钮。",
        "Press ? on admin pages for the shortcut overlay; Ctrl+K opens the "
        "command palette. The inbox has its own keyboard cheat-sheet button.",
        "快捷键 键盘 shortcuts hotkey 按键",
        "",
    ),
    (
        "cmd-palette",
        "命令面板怎么用",
        "How to use the command palette",
        "按 Ctrl+K（任意后台页面）打开命令面板，输入页面名或操作关键词回车直达，"
        "支持中英文搜索，还有刷新/主题/改密/退出等快捷动作。",
        "Press Ctrl+K on any admin page, type a page name or action keyword "
        "and hit Enter. Supports zh/en search plus quick actions.",
        "命令面板 Ctrl+K palette 搜索 快速 跳转",
        "",
    ),
    (
        "clear-filters",
        "收件箱筛选怎么清空",
        "How to clear inbox filters",
        "收件箱筛选区点「清空筛选」按钮，会重置全部条件（含面板里的状态/目标"
        "议程/标签/排序）。若列表看起来少了会话，多半是有筛选在生效，先清空。",
        "Click Clear Filters in the inbox filter bar; it resets every "
        "condition including status/goal-agenda/tags/sort. If conversations "
        "seem missing, clear filters first.",
        "筛选 清空 过滤 filter 重置 会话不见了 列表少了",
        "/workspace",
    ),
    (
        # 实施81：报障工单处置台（master 专属运维面；关键词刻意窄——「报障/提交
        # 问题」等提交侧词属 report-bug 条目，别抢）
        "bug-tickets-console",
        "报障工单在哪管理 / 处置",
        "Where to manage bug tickets",
        "「安全合规 → 报障工单」（/admin/bug-tickets，管理员专属）：左侧列表点任意"
        "工单看详情与截图，右侧可流转状态（标「已修复」时填一句修复说明）、"
        "一键回访 @报障人、直接群内回复（带快捷模板）；顶部「补发回访积压」"
        "可把离线期没发出去的回访一次补齐。",
        "Compliance → Bug tickets (/admin/bug-tickets, master only): pick a "
        "ticket to see details and screenshots; update status (add a fix note "
        "when marking fixed), notify the reporter, or reply into the group "
        "with quick templates. \u201cFlush pending notifies\u201d re-sends "
        "undelivered fix notifications.",
        "报障工单 工单处置 工单管理 处置台 工单在哪 回访 修复回访 "
        "已知问题 修复进展 tickets console 工单状态",
        "/admin/bug-tickets",
    ),
    (
        "report-bug",
        "在哪提交 bug / 报障",
        "How to report a bug",
        "本助手面板切到「报障」标签：一句话描述 + 可选截图（Ctrl+V 粘贴），"
        "系统会自动附上页面与版本信息，一键提交给开发方。也可在官方 Telegram "
        "报障群反馈。提交后在「我的」标签跟进进度。",
        "Switch to the Report tab in this assistant panel: describe the issue "
        "(optionally paste a screenshot); page and build info are attached "
        "automatically. Track progress in the My Tickets tab.",
        "报障 报bug bug 提交问题 反馈 故障 工单 report "
        "软件坏了 坏了 找谁 出问题 不能用 用不了 崩了",
        "",
    ),
    (
        "ticket-status",
        "报障之后怎么看处理进度",
        "How to track my bug report",
        "本助手面板「我的」标签列出你提交的全部工单与状态（新建/已修复等）；"
        "标记「已修复」的工单会请你验证是否解决。",
        "The My Tickets tab lists all your reports and their status; tickets "
        "marked fixed will ask you to verify.",
        "工单 进度 报障进度 修复 状态 ticket "
        "处理得怎么样 到哪了 有结果吗 我提交的 跟进 什么时候好",
        "",
    ),
    (
        "account-banned",
        "账号被风控/封禁了怎么办",
        "What to do when an account is restricted or banned",
        "先在「账号资产中心」查看该账号的资产与状态（联系人/可达性/备份）。"
        "风控事件会记录在运维事件台账并触发告警。恢复或迁移账号请联系管理员"
        "处理，不要反复重试登录以免加重风控。",
        "Check the Account Asset Center for the account's assets and status. "
        "Risk-control events are logged and alerted. Ask an admin before "
        "retrying logins — repeated attempts can make it worse.",
        "风控 封号 封禁 被封 banned 限制 账号异常",
        "/workspace/assets",
    ),
    (
        "reply-budget",
        "回复额度用完了怎么办",
        "What to do when the daily reply budget is used up",
        "「自动回复设置」页的「回复额度守卫」卡可查看/调整每日额度；触顶的会话"
        "可在收件箱横幅或设置页点「今日继续」豁免（管理员权限）。",
        "The Reply Budget Guard card on the Auto-reply Settings page shows and "
        "adjusts daily budgets; capped conversations can be granted a "
        "same-day relief from the inbox banner or the settings list (admin).",
        "额度 回复额度 budget 触顶 限额 豁免 用完",
        "/reply-settings",
    ),
    (
        "context-depth",
        "上下文和记忆深度怎么调 / 怎么让 AI 记得更久",
        "How to change context and memory depth",
        "打开「自动回复设置」，在风格卡里找到「上下文与记忆深度」："
        "标准（约 12k · 最近 10 条 · 8 条记忆）/ 深度 / 最大 / 超大（约 900k，"
        "与 DeepSeek 1M 窗口平齐）。选一档保存即可，不用重启。"
        "越深越记得住人设和长对话，也越贵（峰时单轮上限约 ¥0.02 / ¥0.06 / ¥0.26 / ¥1.8）。"
        "旁边的「用量模式」是另一套旋钮：完整＝按深度档；经济＝只留最近 4 条、压缩人设、少抽取。"
        "钱包用尽时即使选完整也会自动进经济档，避免断线。",
        "Open Reply settings. In the style card, Context & memory depth has "
        "Standard (~12k / last 10 / 8 memories), Deep, Max, and Ultra (~900k, "
        "on par with DeepSeek's 1M window). Pick a tier and save — no restart. "
        "Deeper remembers more and costs more (peak per-turn ≈ ¥0.02 / ¥0.06 / "
        "¥0.26 / ¥1.8). Usage mode next to it is a separate spend cap: Full "
        "follows the depth tier; Economy keeps the last 4 messages. An empty "
        "token wallet still drops Full into Economy so chat does not go dark.",
        "上下文 记忆深度 深度 超大 标准档 经济档 用量模式 记得更久 人设没了 没有记忆 context depth ultra economy",
        "/reply-settings",
    ),
    (
        "risk-grading",
        "风控分级怎么看 / 客户提到照片钱电话为什么不再转人工",
        "How risk grading works / why mentions of photos or money no longer go to human",
        "打开「自动回复设置」，找到「🧯 风控分级」卡：15 个类别列出词表、级别和动作。"
        "高（自伤 / 未成年 / 威胁 / 要钱要凭据 / 诈骗 / 支付词）→ 稿子转人审 + 会话「需人工」并保持；"
        "中（成人内容、要联系方式 / 照片 / 见面 / 礼物、停联）→ 只打「风险 · 中」标签，按人设政策委婉延后，不进需人工；"
        "低（隐私词和照片 / 钱 / 电话的叙述性提及）→ 只记日志。"
        "选一个人设可把「索要照片 / 视频」等类别改高 / 中 / 低，保存即生效。"
        "人工发送或点「我知道了」摘标后，同类别 30 分钟内不重复打标、不重弹横幅；会话头「风险保持 · 类别」胶囊会写明命中类别和命中词。",
        "Open Reply settings and find the Risk grading card: 15 categories with word lists, tier and action. "
        "High (self-harm / minor / threat / asking for money or credentials / scam / payment words) → draft to human review + thread tagged Needs human and held; "
        "Medium (adult content, asking for contact / photos / meetup / gifts, stop-contact) → only a Risk · medium tag, the persona policy softly defers, no Needs human; "
        "Low (privacy words and narrative mentions of photos / money / phone) → log only. "
        "Pick a persona to move categories such as asking for photos between high / medium / low; saving takes effect at once. "
        "After a human send or Got it clears the tag, the same category is not re-tagged for 30 minutes; the header chip Risk hold · category shows the matched category and words.",
        "风控 分级 高风险 需人工 误判 转人工 照片 要钱 摘标 重复打标 横幅 风险保持 risk grading needs human cooldown",
        "/reply-settings",
    ),
    (
        "send-lang-per-conv",
        "发送语言怎么按会话设 / 为什么别的会话也变成日语了",
        "How the send language is set per thread / why another thread switched to Japanese",
        "翻译工具条里「我的消息→ 某语言」只对当前会话生效（旁边标「本会话」），切到别的会话不会带过去；"
        "没设过的会话显示「自动 · 跟对方：English」，AI 自动回复跟对方语言，手发原样发出。"
        "「对方消息→」（收→）仍是全局默认。手发时目标语言和客户语言不一致会先弹确认「将译成 X 发给 Y 客户，确定发送？」，按取消就不发。",
        "In the translation toolbar, My messages → language applies to the current thread only (marked This thread) and does not carry over when you switch threads; "
        "an unset thread shows Auto · follow peer: English — AI replies follow the customer's language and manual sends go as typed. "
        "Receive → stays a global default. If a manual send's target language differs from the customer's, a confirmation appears first; Cancel sends nothing.",
        "发送语言 翻译 发→ 本会话 日语 误发 全局 确认弹窗 send language per thread translate mismatch confirm",
        "/workspace/inbox",
    ),
    (
        "ui-lang-switch",
        "界面语言怎么切换 / 为什么切了语言没反应",
        "How to change the interface language / why switching did nothing",
        "点顶栏的地球按钮，或右上用户菜单里的「语言」，在菜单里选：简体中文 / 繁體中文 / English / Tiếng Việt / ไทย / Bahasa Indonesia，第一项「跟随系统语言」是跟着系统走。"
        "选完页面刷新一次即生效；桌面版的文件 / 编辑 / 视图 / 窗口 / 帮助菜单一起变，登录页、初始化页也是同一个菜单。"
        "越南语 / 泰语 / 印尼语标 β：没翻到的地方显示英文。"
        "如果菜单顶部写着「当前语言由链接参数指定」，直接选一项就会覆盖；旧版桌面端有「切了不变」的问题，升级到 1.0.83 及以上即修复。",
        "Click the globe button in the top bar, or Language in the user menu, then pick Simplified Chinese / Traditional Chinese / English / Vietnamese / Thai / Indonesian; the first item, Follow system language, tracks your OS. "
        "The page reloads once and the choice applies; the desktop app's File / Edit / View / Window / Help menus follow, and the login and setup pages share the same menu. "
        "Vietnamese / Thai / Indonesian are marked β: untranslated parts show in English. "
        "If the menu says the current language is set by a URL parameter, picking any item overrides it; older desktop builds had a switch-does-nothing bug, fixed in 1.0.83 and later.",
        "界面语言 切换语言 中文 英文 繁体 越南语 泰语 印尼语 跟随系统 切了没反应 language switch interface language locale",
        "/workspace",
    ),
    (
        "kb-add",
        "知识库怎么添加条目",
        "How to add a knowledge base entry",
        "打开「知识库」页 → 新建条目：填分类/标题/触发词/回复模板，保存后建议"
        "点「向量化」让语义检索生效。也可一键播种起步包快速备货。",
        "Open the Knowledge page → New entry (category/title/triggers/reply), "
        "then run embed-all so semantic search picks it up. Starter packs can "
        "seed a domain in one click.",
        "知识库 加条目 新建 KB 词条 话术 添加",
        "/knowledge",
    ),
    (
        "persona-edit",
        "怎么编辑/更换人设",
        "How to edit or switch personas",
        "「人设工作室」页可编辑人设档案（性格/背景/语音/相册）。会话绑定的身份"
        "在收件箱回复区的身份条可见；换绑后语音音色偏好会自动校正。",
        "Persona Studio edits persona profiles (personality/voice/albums). "
        "The identity bar above the composer shows the conversation's bound "
        "persona; voice preferences auto-correct after rebinding.",
        "人设 换人设 编辑人设 persona 身份 角色",
        "/personas",
    ),
    (
        "persona-album",
        "人设相册怎么上传图片",
        "How to upload persona album media",
        "「人设工作室」→ 选中人设 → 相册面板上传图/视频，可配触发词让客户要图"
        "时自动发送；支持多语配文与关系等级门槛。",
        "Persona Studio → pick a persona → Album panel: upload images/videos, "
        "set trigger words for auto-send, captions per language.",
        "相册 上传图片 图片 照片 album 自拍 视频",
        "/personas",
    ),
    (
        "goal-create",
        "怎么给会话建工作目标",
        "How to create a work goal for a conversation",
        "收件箱右栏「目标」组件点建目标，两步弹层填写；输入会自动暂存（误关可"
        "断点续写），创建成功后回列表。",
        "Use the Goal panel in the inbox right sidebar; the two-step form "
        "auto-saves drafts so accidental closes never lose input.",
        "目标 建目标 工作目标 goal 议程",
        "/workspace",
    ),
    (
        "draft-review",
        "AI 草稿怎么审批/为什么发送按钮是灰的",
        "How draft review works / why Send is greyed out",
        "「草稿审批」页逐条通过/编辑/拒绝。发送置灰通常是护栏拦截：草稿超过 "
        "24 小时（内容已脱节，请重新生成）或该会话已经回复过（原样发会重复）。"
        "点「编辑后发送」是推荐出路。",
        "The Drafts page approves/edits/rejects drafts. A greyed Send means a "
        "guard tripped: the draft is older than 24h or the conversation was "
        "already answered. Edit-and-send is the recommended path.",
        "草稿 审批 通过 发送灰 灰色 拦截 陈旧",
        "/workspace/drafts",
    ),
    (
        "takeover",
        "怎么人工接管会话 / 让 AI 停止自动回复",
        "How to take over a conversation from AI",
        "在收件箱会话里切换自动化模式：全自动（AI 自答）/ 人审（AI 拟稿人发）/"
        " 人工。接管后 AI 不再自动发送，坐席手动回复。",
        "Switch the conversation's automation mode in the inbox: full-auto, "
        "review (AI drafts, human sends), or manual takeover.",
        "接管 人工 转人工 停止AI 自动回复 automation",
        "/workspace",
    ),
    (
        "image-translate",
        "客户发的图片怎么翻译",
        "How to translate an image from a customer",
        "点开收件箱里的图片，在查看器里点翻译即可 OCR+翻译；也可以把截图直接 "
        "Ctrl+V 粘贴进输入区做图片翻译。",
        "Open the image in the inbox viewer and hit Translate (OCR + "
        "translation); you can also paste a screenshot into the composer.",
        "图片翻译 OCR 翻译图片 截图翻译 识别",
        "/workspace",
    ),
    (
        "outbound-translate",
        "发出去的消息会自动翻译吗",
        "Does outbound text get auto-translated",
        "开启出站自动翻译后，AI 自动发送与主动触达会先译成会话客户的语言再发；"
        "文本已是客户语言时自动跳过。该开关由管理员在配置里启用。",
        "With outbound auto-translate enabled, autosend and proactive messages "
        "are translated into the customer's language first (skipped when "
        "already in it). Admins enable it in config.",
        "自动翻译 出站翻译 翻译 客户语言 外语",
        "",
    ),
    (
        "send-media",
        "怎么给客户发图片/文件",
        "How to send media to a customer",
        "收件箱输入区的图片按钮选择文件，或直接把截图 Ctrl+V 粘贴到输入区，"
        "预览确认后发送。",
        "Use the media button in the composer or paste a screenshot with "
        "Ctrl+V, preview, then send.",
        "发图 发图片 发文件 媒体 图片 粘贴",
        "/workspace",
    ),
    (
        "voice-transcribe",
        "客户的语音怎么变成文字",
        "How customer voice notes become text",
        "客户语音会自动转写成文字显示在会话里（自建 GPU 转写，多语言自动识别），"
        "无需手动操作。转写异常时请走报障。",
        "Inbound voice notes are transcribed automatically (self-hosted GPU "
        "ASR, language auto-detect). Report it if transcription misbehaves.",
        "语音转文字 转写 听不懂 语音识别 ASR",
        "",
    ),
    (
        "theme-switch",
        "怎么切换暗色/亮色主题",
        "How to switch dark/light theme",
        "坐席工作台：顶栏主题切换按钮。后台管理页：Ctrl+K 命令面板输入「主题」"
        "执行切换。",
        "Workspace: theme toggle in the top bar. Admin pages: Ctrl+K → type "
        "'theme' → run the toggle action.",
        "暗色 夜间 主题 dark 亮色 深色模式",
        "",
    ),
    (
        "lang-switch",
        "界面语言怎么切换中/英",
        "How to switch UI language",
        "坐席工作台顶栏有语言切换按钮（中⇄英，整页生效）；后台管理页在用户菜单"
        "里切换。",
        "Workspace: the language button in the top bar switches zh⇄en for the "
        "whole page; admin pages switch it from the user menu.",
        "语言 英文 中文 切换语言 language english",
        "",
    ),
    (
        "multiwin",
        "为什么提示「在此使用/保持待机」",
        "Why do I see the use-here / standby prompt",
        "同一浏览器开了多个工作台窗口时，只有一个主控窗口响铃和操作，其余待机"
        "（防止重复发送与双响铃）。点「在此使用」把主控切到当前窗口。",
        "With multiple workspace windows in one browser, only one is primary "
        "(prevents double-send and double bells). Click Use Here to promote "
        "the current window.",
        "多窗口 待机 在此使用 双开 两个窗口",
        "",
    ),
    (
        "ai-degraded",
        "AI 回复变慢/顶部出现降级提示怎么办",
        "What the AI degradation banner means",
        "顶部降级条表示云端 AI 暂不可用，系统已自动切换本地兜底模型继续出话"
        "（恢复后自动消失），无需操作。若长时间不恢复或完全无回复，请走报障。",
        "The banner means the cloud LLM is unreachable and the local fallback "
        "is serving replies; it clears automatically on recovery. Report if "
        "it persists.",
        "降级 变慢 AI不回 云端 本地兜底 慢",
        "",
    ),
    (
        "channel-down",
        "平台通道离线（红条）怎么处理",
        "How to handle the channel-offline red banner",
        "收件箱顶部红条说明某平台会话掉线（客户消息收不到）。Messenger/WhatsApp "
        "支持红条上一键重登；其他平台请联系管理员处理。",
        "The red banner means a platform session dropped. Messenger/WhatsApp "
        "offer one-click relogin on the banner; other platforms need an admin.",
        "掉线 离线 红条 通道 连接中断 收不到消息 重登",
        "/workspace",
    ),
    (
        "alert-webhook",
        "告警怎么推送到 Telegram",
        "How to push alerts to Telegram",
        "后台「告警渠道」面板填 Telegram bot token 与 chat_id，保存后可点测试"
        "发送；按需订阅告警类型，免重启即时生效（管理员操作）。",
        "Fill in the Telegram bot token and chat_id on the alert-channel "
        "panel, test-send, and pick alert types to subscribe (admin; applies "
        "without restart).",
        "告警 通知 推送 webhook telegram 报警",
        "",
    ),
    (
        "proactive-config",
        "AI 会主动给客户发消息吗 / 怎么控制",
        "Does the AI proactively message customers / how to control it",
        "支持按沉默时长主动问候、早晚安仪式、主动关怀，均有冷却与安静时段护栏。"
        "开关与频率属管理员配置项（companion 配置段），运维看板可观测发送量与"
        "回复率。",
        "Proactive check-ins, morning/night rituals and care messages are "
        "supported with cooldown and quiet-hour guards; admins configure them "
        "and the ops board shows volume and reply rates.",
        "主动 主动触达 问候 打招呼 主动消息 关怀",
        "",
    ),
    (
        "dashboard-where",
        "运营数据在哪里看",
        "Where to see operational data",
        "「仪表盘」页看整体经营数据；「运维总览」页看系统健康与各子系统读数；"
        "工作台侧栏有坐席绩效/ROI/AI 质量等看板。",
        "The Dashboard page shows business metrics; Ops Overview shows system "
        "health; the workspace sidebar has agent-perf/ROI/AI-quality boards.",
        "数据 看板 仪表盘 报表 统计 dashboard",
        "/dashboard",
    ),
    (
        "ai-style",
        "怎么调整 AI 回复的风格/长度",
        "How to tune AI reply style",
        "「回复策略」页调整温度、长度与策略；人设的说话风格在「人设工作室」的"
        "档案里改（personality/style）。改动即时生效。",
        "Tune temperature/length on the Reply Strategies page; per-persona "
        "speaking style lives in Persona Studio profiles.",
        "风格 语气 回复长度 温度 temperature 策略",
        "/strategies",
    ),
    (
        "term-tips",
        "术语悬停提示怎么开关",
        "How to toggle term tooltips",
        "本助手面板底部工具行可开关「术语提示」；开启后页面上带虚线下划线的"
        "词条悬停即可看解释。",
        "Toggle Term Tips in this panel's footer; hovering dashed-underlined "
        "terms then shows explanations.",
        "术语 提示 悬停 词条 下划线 tooltip",
        "",
    ),
    (
        "kb-miss",
        "客户问的问题 AI 答不上来怎么办",
        "What to do when the AI can't answer a customer",
        "知识库页有「未命中查询」清单（客户问了但没答上的问题），照单补条目后"
        "点向量化即可让 AI 学会。高频缺口优先补。",
        "The Knowledge page lists missed queries; add entries for them and "
        "run embed-all so the AI learns. Fix high-frequency gaps first.",
        "答不上 不会答 未命中 miss 补充 学习",
        "/knowledge",
    ),
    # ── 实施74 P1（2026-08-27）：页面级操作指引补齐 ───────────────────────────
    # 缺口实测：nav_schema 40 个页面里只有 6 个有 how-to（用户问「这页怎么用」
    # 34 页答不上）。本批补 12 个最高价值页，逐条**先读代码/模板核实真实行为**
    # 再写（本文件红线：禁止写「我以为有」的功能）。关键纠正：四个渠道页
    # **本身不接入账号**，接入统一在坐席工作台的账号抽屉——差点写反。
    (
        "connect-telegram",
        "Telegram 账号怎么接入 / 渠道页怎么用",
        "How to connect Telegram and use its channel page",
        "接账号在**坐席工作台的账号抽屉**扫码（渠道页本身不接入）：工作台 → "
        "账号面板 → 扫码，加的是「坐席多开号」。渠道页「真机矩阵」分四个页签"
        "（配置 / 语音 / 运维 / 漏斗），改完点「保存消息配置」即热更新、无需重启。"
        "注意主会话（A 线）掉线不能靠扫码恢复，需管理员在服务器处理。",
        "Connect accounts from the account drawer in the agent workspace (the "
        "channel page itself does not onboard accounts). The channel page has "
        "Config / Voice / Ops / Funnel tabs; Save applies as a hot reload. A "
        "dropped main session cannot be restored by scanning a QR code.",
        "telegram 接入 接账号 扫码 添加账号 tg 渠道 登录 多开 电报 电报号",
        "/workspace/channels/telegram",
    ),
    (
        "connect-line",
        "LINE 怎么接入 / 渠道页怎么用",
        "How to connect LINE and use its channel page",
        "接账号在**坐席工作台的账号抽屉**完成。渠道页分监控 / 待审 / 配置 / "
        "运维 / 漏斗五个页签，顶部控制条可「立即触发一轮」「暂停 5/15/30 分钟」"
        "「立即恢复」「手动发送」，配置改完点「保存配置」。",
        "Connect LINE from the account drawer in the agent workspace. The "
        "channel page has Monitor / Review / Config / Ops / Funnel tabs, with "
        "run-now, pause and manual-send controls at the top.",
        "line 接入 接账号 扫码 渠道 暂停 触发 手动发送 登录 登陆 上号 line号",
        "/workspace/channels/line",
    ),
    (
        "connect-messenger",
        "Messenger 怎么接入 / 掉线怎么重登",
        "How to connect Messenger and re-login after a drop",
        "接账号在**坐席工作台的账号抽屉**发起（服务器托管浏览器）。掉线时页面"
        "下方会出现「网页会话异常」健康条，点上面的「重新登录」即可触发，之后"
        "提示「已触发重登，30 分钟内在 worker 主机完成登录」。Messenger 是唯一"
        "支持页面内一键重登的渠道。",
        "Start the connection from the account drawer in the agent workspace "
        "(server-hosted browser). When a session drops, use Re-login on the "
        "health bar; you then have 30 minutes to finish login on the worker "
        "host. Messenger is the only channel with in-page re-login.",
        "messenger 接入 掉线 重新登录 重登 会话异常 fb",
        "/workspace/channels/messenger",
    ),
    (
        "connect-whatsapp",
        "WhatsApp 怎么接入 / 掉线怎么办",
        "How to connect WhatsApp and handle a dropped session",
        "接账号在**坐席工作台的账号抽屉**扫码登录。掉线后渠道页只做观测，"
        "不支持页面内重登——提示会写「请到聊天坐席的账号面板重新扫码」，"
        "点「去聊天坐席」回工作台重扫即可。",
        "Scan to connect from the account drawer in the agent workspace. The "
        "channel page only observes session health; there is no in-page "
        "re-login for WhatsApp — go back to the workspace account panel and "
        "scan again.",
        "whatsapp wa 接入 扫码 掉线 重新扫码 离线",
        "/workspace/channels/whatsapp",
    ),
    (
        "add-agent-user",
        "怎么给团队加一个坐席账号",
        "How to add a team member account",
        "「用户管理」页点「＋ 添加子帐号」→ 填用户名 / 显示名称 / 密码（可点「🎲 生成」）"
        "/ 角色 → 「创建」。创建成功会弹出一次性的「登录信息」卡（登录地址 / 用户名 / 密码），"
        "点「复制全部」当场发给对方。主帐号可建管理员、主管、坐席、只读观察员；管理员可建"
        "主管、坐席、只读观察员。密码至少 6 位，主帐号不可删除或降级。建好后每张帐号卡上可"
        "改角色（会先确认）、点「🔑 重置密码」，「⋯」菜单里有额度 / 权限 / 通知 / 复制登录信息 "
        "/ 禁用 / 删除（删除要逐字输入用户名确认）。",
        "On the Users page click Add sub-account, fill in username / display "
        "name / password (or click Generate) / role, then Create. A one-time sign-in "
        "info card (address / username / password) appears — Copy all and send it to the "
        "person. The master account can create admin, supervisor, agent and viewer; an "
        "admin can create supervisor, agent and viewer. Passwords need 6+ characters; the "
        "master account cannot be deleted or demoted. On each card you can change the role "
        "(with confirmation), Reset password, and use the ⋯ menu for quota / permissions / "
        "notifications / copy sign-in info / disable / delete (delete requires typing the username).",
        # 「客服人员」必须留在这条：escalation-config 也含「人工客服」，
        # 2026-08-27 评测实测它会把「怎么新增一个客服人员」抢走——补关键词有
        # 交叉影响，改任一条都该复跑 assistant_qa_eval 看有没有误伤别人。
        "加坐席 添加用户 子帐号 新增账号 角色 权限 用户管理 开账号 "
        "客服人员 新增人员 员工 同事 新人",
        "/users",
    ),
    (
        "kick-sessions",
        "怎么把其他设备踢下线 / 看谁在登录",
        "How to see active sessions and sign out other devices",
        "「用户管理」页下方有「已登录的设备」区：每张卡显示设备（智聊桌面端 / 浏览器）、"
        "操作员与最后活跃时间，「技术信息」可展开看 IP 等；点「刷新」更新，单张卡点「踢出」，"
        "怀疑账号被别人用了就点「踢出所有其他设备」，只保留你当前这台。每张帐号卡上的 🖥 数字"
        "是该帐号当前登录的设备数。",
        "The Users page has a Signed-in devices section: each card shows the device (ChatX "
        "desktop / browser), operator and last activity, with technical details on expand. "
        "Refresh to update, Sign out a single card, or sign out all other devices to keep "
        "only your current one. The 🖥 number on an account card is its current device count.",
        "踢下线 已登录的设备 活跃会话 登录设备 挤下线 安全 会话 谁在登录",
        "/users",
    ),
    (
        "system-settings",
        "系统设置里能改什么 / 为什么我打不开",
        "What the system settings page covers and why it may be blocked",
        "「系统设置」**只有主帐号能进**，管的是 AI 提示词与行为、人工转接、"
        "品牌白标、授权激活、演示数据等系统级配置。注意 Telegram 的「私聊消息」"
        "开关**不在这页**——在「开发者工具」→ Bot 行为配置里勾选后保存。"
        "简洁模式下本页只显示 AI 提示词与人工转接两块。",
        "System settings is master-account only: AI prompts and behaviour, "
        "human escalation, branding, licensing and demo data. The Telegram "
        "private-chat toggle is NOT here — it lives in Developer tools under "
        "bot behaviour. Simple mode shows only prompts and escalation.",
        "系统设置 设置 打不开 权限 提示词 人工转接 品牌 授权",
        "/settings",
    ),
    (
        "usage-quota",
        "用量与额度在哪看 / 额度快用完怎么办",
        "Where to check usage and what to do when quota runs low",
        "「用量与额度」页：主管/管理员看团队（授权总池、本月团队消耗、每日趋势、"
        "分坐席用量表、计费对账单可按月生成并导出 CSV），坐席只看「我的用量」。"
        "进度条到 80% 变黄、100% 变红，并按近 7 天日均估算「约 X 天耗尽」；"
        "要加量点标题行的「增购 →」到会员中心。",
        "The Usage page shows the licence pool, this month's team consumption, "
        "daily trend, per-agent table and a monthly billing statement (CSV "
        "export). Agents see only their own usage. The bar turns amber at 80% "
        "and red at 100%, with a days-to-exhaustion estimate; use Buy more to "
        "top up.",
        "用量 额度 字符 快用完 耗尽 增购 充值 计费 对账单 "
        "花了多少 花费 消费 用了多少 这个月 本月 账单",
        "/workspace/usage",
    ),
    (
        "personal-settings",
        "怎么改界面主题 / 个人偏好",
        "How to change the theme and personal preferences",
        "「个人设置」页的「外观与个性化」可调主题配色，改动带实时预览、"
        "且跟随账号跨设备漫游。坐席工作台左下角的「主题配色」是同一套设置的"
        "快捷入口。这页所有登录用户都能用，与主帐号的「系统设置」无关。",
        "Personal settings → Appearance lets you change the theme with live "
        "preview; it roams with your account across devices. The Theme "
        "shortcut at the bottom-left of the workspace opens the same settings. "
        "Available to every signed-in user.",
        "主题 换肤 暗色 亮色 外观 个性化 个人设置 配色",
        "/personal-settings",
    ),
    (
        "audit-log",
        "谁改了什么怎么查 / 操作记录怎么导出",
        "How to audit who changed what and export the records",
        "「操作记录」页有最近 84 天的操作热力图，下面可按条件筛选记录并分页浏览；"
        "「导出筛选结果」按当前筛选导 CSV、「导出全部」导全量。日志有保留期限，"
        "页面顶部会写明当前保留策略。",
        "The Audit page shows an 84-day activity heatmap plus filterable, "
        "paginated records. Export filtered results or everything as CSV. "
        "Retention policy is shown at the top of the page.",
        "操作记录 审计 谁改的 日志 导出 csv 留痕 "
        "谁把 谁动了 改了什么 变更 谁操作的 改动记录",
        "/audit",
    ),
    (
        "today-overview",
        "今日概览看什么 / 为什么我看不到首页",
        "What the dashboard shows and why you may not see it",
        "「今日概览」是完整模式的后台首页：待办条、本周 AI 价值（可点「完整报表」"
        "进运营总览）、授权状态、系统告警横幅。**简洁模式下打开首页会直接跳到"
        "「案例跟进」**——想看概览请切到完整模式，或用侧栏的「今日概览」入口。",
        "The dashboard is the full-mode home page: to-dos, weekly AI value "
        "(with a link to the full report), licence status and system alerts. "
        "In simple mode the home page redirects to Cases — switch to full mode "
        "or use the sidebar entry to reach it.",
        "首页 今日概览 仪表盘 看不到 跳转 待办 dashboard",
        "/",
    ),
    (
        "help-center",
        "帮助中心 / 指令参考在哪",
        "Where to find the command reference and training",
        "「帮助中心」可搜索并按分区查看指令参考，指令可直接复制；页面上还有"
        "「打开培训演示」进入全屏培训幻灯片。简洁模式只列常用指令，切完整模式"
        "看全部。所有能登录的用户都可以用。",
        "The Help centre lets you search and browse the command reference "
        "(commands are copyable) and open a full-screen training deck. Simple "
        "mode lists only common commands; switch to full mode for everything.",
        "帮助 帮助中心 指令 命令 培训 教程 参考",
        "/help",
    ),
    # ── 实施74 P1 第二批（2026-08-27）：运营/看板/技术页 ─────────────────────
    # 同样逐条先读代码核实。两条判断值得记：
    # ① /developer 有二次密码门——**密码绝不写进面向客户的帮助文案**（核实时
    #    读到了明文，刻意只写「向技术支持索取」）；
    # ② /logs 与 /developer 对普通客户就是「你不需要进」——明确告诉用户某页
    #    不用管，本身就是有效的使用帮助，比装作它有用更诚实。
    (
        "cases-page",
        "案例跟进是什么 / 案例从哪来",
        "What the Cases page is and where cases come from",
        "「案例跟进」是 AI 判定**需要人接手**的会话清单，由系统自动开案——"
        "触发信号包括：客户要求人工、危机信号、怀疑对方是机器人、质疑照片或身份、"
        "满意度走低后的连续追问等。坐席的动线是：认领 → 打开会话处理 → 加备注 → "
        "结案（可标「已安抚 / 已转人工 / 误报」）。顶部可按「待处理 / 我认领的 / "
        "高风险 / 已结案」筛选。简洁模式下这页就是首页。",
        "Cases lists conversations the AI decided need a human: explicit "
        "requests for a human, crisis signals, bot suspicion, doubts about "
        "photos or identity, repeated follow-ups after low satisfaction. Claim "
        "it, open the conversation, add a note, then close it. In simple mode "
        "this is the home page.",
        "案例 案例跟进 开案 认领 结案 需要人工 转人工 风险会话",
        "/cases",
    ),
    (
        "membership-page",
        "会员中心能干什么 / 在哪买额度",
        "What the Membership page does and how to buy more",
        "「会员中心」展示当前授权档位、字符额度、功能矩阵与升级引导，**本身不是"
        "下单页**：只有配置了商城链接时才会出现「购买 / 续费」外链按钮，否则"
        "可走的路是——激活授权码、注册领取免费额度、邀请好友、或联系客服申请。"
        "主帐号还能点「同步授权」拉取最新状态。侧栏入口只对主帐号显示。",
        "Membership shows your licence tier, character quota, feature matrix "
        "and upgrade guidance. It is not a checkout page: a Buy/Renew link "
        "appears only when a shop URL is configured; otherwise activate a "
        "licence key, claim free credits, invite friends or contact support. "
        "The master account can also re-sync the licence.",
        "会员 会员中心 购买 续费 增购 授权 激活 额度 套餐 升级",
        "/membership",
    ),
    (
        "monetization-page",
        "客户营收看板看什么",
        "What the Monetization page shows",
        "「客户营收」是 C 端用户订阅 / 单点解锁 / 打赏的营收与转化看板，可下钻"
        "「触墙名单 / 被预算拦名单 / 送达名单 / 预告触达名单」，也能给指定客户"
        "「开通 / 入账」和「查权益」。需要先开启 monetization.enabled（未开时"
        "侧栏不显示这个入口），角色需主帐号或管理员。",
        "Monetization is the revenue and conversion board for end-user "
        "subscriptions, unlocks and tips, with drill-downs into paywall / "
        "budget-blocked / delivered lists, plus manual grants and entitlement "
        "lookup. Requires monetization.enabled and a master/admin role.",
        "营收 变现 订阅 解锁 打赏 付费 转化 monetization 权益",
        "/monetization",
    ),
    (
        "learner-page",
        "学习队列是干嘛的 / 怎么让 AI 变聪明",
        "What the Learning queue does",
        "「学习队列」把 AI 没答好的问题变成知识草稿，**必须人工审核才入库**——"
        "来源有三种：知识库未命中达到次数、客户负反馈、以及你手动「入队学习」"
        "粘贴的问题。在页面上逐条「通过入库 / 拒绝 / 编辑」，也可批量通过。"
        "想立刻跑一轮点「立即学习」。注意它教的是「下次答得更好」，"
        "现在就需要人接手的风险会话在「案例跟进」。",
        "The Learning queue turns questions the AI handled poorly into "
        "knowledge drafts that a human must approve before they enter the KB. "
        "Sources: repeated KB misses, negative feedback, and manually queued "
        "questions. Approve, reject or edit each draft. It improves future "
        "answers; conversations needing a human right now live in Cases.",
        "学习队列 学习 训练 变聪明 草稿 审核 入库 未命中 knowledge",
        "/learner",
    ),
    (
        "relations-health-page",
        "流失预警怎么用 / 客户要跑了怎么发现",
        "How to use the churn-risk page",
        "「流失预警」按流失风险给联系人排序，选中后可「生成话术」再「直接发送」"
        "或「打开会话」自己接手，形成发现→挽回的闭环。它有两种模式：开启"
        "contacts.enabled 并重启后是跨平台旅程的全量榜，否则是基于收件箱信号的"
        "轻量榜（页面顶部会说明当前跑在哪种模式）。",
        "Churn risk ranks contacts by risk, then lets you generate a win-back "
        "message and send it or open the conversation yourself. With "
        "contacts.enabled it uses the full cross-platform journey; otherwise a "
        "lightweight inbox-signal ranking. The page states which mode is live.",
        "流失 预警 挽回 客户要跑 风险 沉默 relations 召回",
        "/relations-health",
    ),
    (
        "care-schedule-page",
        "主动关怀是什么 / 怎么开",
        "What proactive care is and how to enable it",
        "「主动关怀」让 AI 记住客户在聊天里说过的事（面试、复查、搬家…），"
        "到点主动问候。开启是分两步的安全设计：先点「开启主动关怀（试运行）」"
        "——只拟稿不发送，你在页面上逐条「看 AI 会说什么」；读数满意再点"
        "「开始真发」。随时可「暂停引擎」。",
        "Proactive care remembers things customers mention (an interview, a "
        "check-up, a move) and reaches out when the day comes. Enabling is "
        "deliberately two-stage: start in dry-run (drafts only, review each "
        "one), then switch to live sending. You can pause the engine anytime.",
        "主动关怀 关怀 主动问候 提醒 试运行 dry run 定时 care",
        "/care-schedule",
    ),
    (
        "crisis-audit-page",
        "客户安全预警是什么",
        "What the Customer Safety Alerts page is",
        "「客户安全预警」（原「危机审计」）记录 AI 在客户消息里识别到自伤、绝望等危机"
        "信号的时刻：谁、什么时候、AI 怎么兜底、有没有叫人。可按客户搜索、只看未处理，"
        "逐条「标记已处理」。页面顶部若出现红条「危机留痕未开启」，点一键开启即可"
        "（同时开启人工升级：严重事件点亮工作台「需人工」红徽标并置顶会话）。"
        "内容敏感，只有主管/合规角色可见。",
        "Customer Safety Alerts (formerly Crisis audit) records the moments the AI "
        "detected self-harm or despair signals in a customer's messages: who, when, "
        "how the AI applied its safety net, and whether a human was called. Search "
        "by customer, show only unhandled, mark items handled. If a red banner says "
        "logging is off, use the one-click enable (it also turns on human escalation: "
        "severe events light the red workbench badge and pin the conversation). "
        "Sensitive — visible to supervisors/compliance only.",
        "危机 审计 自伤 极端 安全 预警 留痕 处置 crisis safety",
        "/crisis-audit",
    ),
    (
        "episodic-memory-page",
        "AI 记忆在哪看 / 记错了怎么删",
        "How to review and fix the AI's long-term memory",
        "「AI 记忆」页可浏览 AI 记住的长期事实，点「加载」拉取；每条可「确认属实」"
        "或「删除」，也支持按关键词批量删除（有二次确认）。记错的事实会一直影响"
        "后续对话，发现了就删——但**误删同样会降低对话质量**，删之前先看清楚。"
        "确认与删除需要主帐号或管理员权限。",
        "The AI memory page lists long-term facts the AI has stored. Load them, "
        "then confirm or delete each one; bulk delete by keyword is available "
        "with a confirmation step. Wrong facts keep affecting future replies, "
        "but deleting good ones hurts quality too — read before you delete. "
        "Confirm/delete requires master or admin.",
        "记忆 AI记忆 长期记忆 记错 删除 事实 episodic 忘记",
        "/episodic-memory",
    ),
    (
        "escalation-config",
        "怎么让 AI 答不上时转人工",
        "How to hand off to a human when the AI is stuck",
        "在「系统设置」的「人工客服转接」卡里配（侧栏「人工转接」直达）：启用"
        "「同问多次转人工」→ 填客服用户名 → 设「同一问题连发几次触发」（默认 3）、"
        "统计窗口、最短问句长度、触发后冷却 → 点「保存转接配置」。有「一键常用"
        "默认值」可快速填。这页只有主帐号能进。",
        "Configure it in the Human handoff card on the Settings page (the "
        "sidebar Handoff entry deep-links there): enable repeat-question "
        "handoff, set the agent username, the repeat threshold (default 3), "
        "the counting window, minimum question length and the cooldown, then "
        "save. A one-click defaults button is available. Master account only.",
        "转人工 人工转接 escalation 接管 答不上 转接 客服 同问多次 "
        "找真人 要真人 真人 人工客服 找人工 要人工",
        "/settings#escalation",
    ),
    (
        "queue-board",
        "主管看板看什么 / 怎么给坐席重新分配会话",
        "What the supervisor queue board does",
        "「主管看板」实时显示各坐席的负载与待处理会话，可在卡片视图或表格视图"
        "之间切换，对有会话的坐席点「重新分配」把会话转给别人（弹层里确认分配）。"
        "**这页只有主管角色能进**，普通坐席打开会被送回工作台。",
        "The supervisor board shows each agent's load and open conversations in "
        "card or table view, and lets you reassign conversations to someone "
        "else. Supervisor role only — other agents are redirected back to the "
        "workspace.",
        "主管看板 队列 负载 重新分配 转派 坐席 supervisor queue "
        "每个客服 接了多少 工作量 谁在忙 分配情况 谁接的",
        "/workspace/queue",
    ),
    (
        "ops-overview-page",
        "运营总览 / 老板单页看板怎么看",
        "How to read the ops overview board",
        "「运营总览」把 ROI、计费、运行时健康、可靠性等聚合成一页，顶部可"
        "「筛选卡片」按关键词找、「全部展开·收起」、切「老板视图」只看经营口径。"
        "**零流量的卡片会整卡隐藏**——看不到某张卡通常表示那个子系统还没启用或"
        "本窗口没有数据，不是坏了；页面里有「隐藏能力清单」说明哪些没开。",
        "Ops overview aggregates ROI, billing, runtime health and reliability "
        "on one page, with card search, expand/collapse all and a boss view. "
        "Cards with no traffic hide themselves — a missing card usually means "
        "that subsystem is off or had no data, not that it broke; the page "
        "lists which capabilities are hidden.",
        "运营总览 老板看板 ops 指标 健康 卡片 不见了 隐藏 "
        "一页看全 全部数据 汇总 大盘 总数据 一页",
        "/admin/ops",
    ),
    (
        "logs-page",
        "实时日志是给谁用的",
        "Who the live log page is for",
        "「实时日志」是给技术支持排查问题用的服务端日志流，可按级别"
        "（DEBUG/INFO/WARN/ERROR/CRIT）和关键词过滤，支持暂停/清空/下载/跟随。"
        "**日常运营用不到这页**——遇到问题更快的路是用小智的「报障」一键截图提交，"
        "我们会带着日志一起看。页面上的「清空」只清当前显示，不影响服务器日志。",
        "Live logs is a server log stream for technical troubleshooting, with "
        "level and keyword filters plus pause/clear/download/follow. Day-to-day "
        "operations do not need it — reporting the problem through the "
        "assistant (with a screenshot) gets it looked at faster. Clear only "
        "clears the view, not the server-side log files.",
        "日志 实时日志 logs 排查 报错 技术 调试",
        "/logs",
    ),
    (
        "developer-page",
        "开发者工具是什么 / 我进不去",
        "What the Developer tools page is",
        "「开发者工具」放的是敏感配置：AI 接口与密钥、备用 Key 池、Bot 行为、"
        "内部界面显隐等，因此在登录之外还有一道**独立的开发者密码**门"
        "（需要时请向技术支持索取，不要在聊天里传）。"
        "**这页不是给日常运营用的**——里面的开关配错可能让全站异常；"
        "需要改的东西大多在「系统设置」或「自动回复设置」里就有。",
        "Developer tools holds sensitive configuration: AI endpoints and keys, "
        "the backup key pool, bot behaviour and internal UI visibility, so it "
        "sits behind a separate developer password (ask technical support when "
        "you need it). It is not meant for day-to-day operations — a wrong "
        "toggle here can break the whole site; most things you need are in "
        "Settings or Reply settings.",
        "开发者 开发者工具 developer 进不去 密码 高级 API key",
        "/developer",
    ),
    # ── 总览缺口（2026-08-29，老板实测「回复的内容没一点帮助」）───────────────
    # 44 条 how-to 全是**具体任务**，没有一条讲「整体怎么用、我该从哪开始」——
    # 而那正是新用户问的第一句。更坏的是纯词汇检索给了**同词不同义的假命中**：
    # 实测「怎么使用这个软件」62.01 分命中 howto:multiwin（「在此**使用**/保持
    # 待机」）、「怎么操作这个软件」79.59 分命中 term:nav_audit（「**操作**记录」）。
    # 都过了 min_score=35，所以走的不是零命中，而是 NO_BASIS 哨兵：答题 LLM 看着
    # 不相关的条目，诚实地拒绝硬凑——机制没坏，缺的是这条语料。
    # keywords 刻意把「使用 / 操作」这两个被假命中占据的词收进来，让总览条目在
    # 它们上面压过那两条（改词表后必须重跑 tools/... 的分数对照，见门禁）。
    (
        "getting-started",
        "这个软件怎么用 / 我第一次用该从哪开始",
        "How to use this system / where do I start",
        "主线四步：① **接账号**——在坐席工作台的账号抽屉扫码（四个渠道页本身"
        "不接入账号）；② **配人设**——「人设工作室」设定性格/语音/相册，决定 AI "
        "用什么身份跟客户说话；③ **定自动化档位**——每个会话可选 全自动（AI 自答）"
        "/ 人审（AI 拟稿、人点发送）/ 人工，在收件箱会话里切换；④ **日常盯盘**"
        "——「坐席工作台」处理会话，「草稿审批」过 AI 拟稿，「案例跟进」是 AI 判定"
        "需要人接手的清单。想看效果去「今日概览」（完整模式的首页）。"
        "要具体步骤就直接问我那件事（例如「怎么发语音」「Telegram 怎么接入」"
        "「怎么人工接管」），或去「帮助中心」看指令参考。",
        "Four steps. (1) Connect accounts: scan from the account drawer in the "
        "agent workspace — the four channel pages do not onboard accounts. "
        "(2) Set up personas in Persona Studio (personality/voice/album); that "
        "is the identity the AI speaks as. (3) Choose an automation mode per "
        "conversation in the inbox: full-auto, review (AI drafts, a human "
        "sends), or manual. (4) Day to day: work conversations in the agent "
        "workspace, clear AI drafts on the Drafts page, and handle Cases (what "
        "the AI flagged for a human). The dashboard shows results. For a "
        "specific task just ask me about it, or open the Help centre.",
        # ⚠ 词表刻意**不含「怎么」**：它是零区分度的疑问词，几乎每条 how-to 问句
        # 都带它。首版写了「怎么用/怎么使用/怎么操作」三遍，把「怎么」的词频堆到
        # 让本条变成「所有怎么X问题」的万能匹配——负样本「红烧肉怎么做才好吃」
        # 当场从 32.33（拒答）被吸到 41.28 命中本条，拒答率 7/15 掉到 6/15。
        # 区分度靠「上手/入门/主线/总览」这些词，不靠堆常见字。
        "使用 用法 操作 上手 快速上手 入门 新手 第一次 从哪开始 开始 "
        "干什么 有什么功能 功能 主线 流程 整体 总览 教程 "
        "这个软件 这个系统 软件 系统 how to use getting started overview",
        "/workspace",
    ),
    # ── v1.0.66 新功能批（2026-09-01，实施93 发版随包；老板指令：每个新功能
    #    小智必须能正常引导——发版前对话测试实测 5/8 落「无依据拒答」，本批补齐。
    #    内容红线照旧：只写已验证的真实 UI 行为，运营侧服务端开关一律写明
    #    「请管理员操作」。keywords 不堆「怎么」（零区分度，见 getting-started 教训）。──
    (
        "attach-workflow",
        "工作链（跟进 SOP）怎么给客户挂上",
        "How to attach a follow-up SOP chain to a customer",
        "工作链＝预设的多步跟进剧本（破冰/报价跟单/复购唤醒等），按设定的天数"
        "间隔逐步推进。挂链：在坐席工作台打开会话 → 右栏「工作链」组件 → 选一条"
        "链 → 启动；同一处可换链/停链，进行中的链会显示当前步与下一步时间。"
        "批量挂链：收件箱筛选面板筛出目标客户 → 面板底部「按当前筛选批量挂链」"
        "（默认先试算再确认）。链的执行档有「拟稿人审」与「自动发送」两种，链"
        "推进期间 AI 普通回复会自动给链让路。链本身在「工作链」页（/workflows）"
        "创建或用预设包一键生成。",
        "A workflow chain is a preset multi-step follow-up script advanced on "
        "day intervals. Attach: open the conversation in the workspace, use "
        "the Workflows panel on the right, pick a chain and start it; the "
        "same panel switches or stops chains. Bulk attach: filter the inbox, "
        "then use \"bulk attach by current filter\" at the bottom of the "
        "filter panel (dry-run first). Chains run in draft-review or "
        "auto-send mode, and normal AI replies yield to a running chain. "
        "Create chains (or seed preset packs) on the Workflows page.",
        "工作链 跟进链 链条 挂链 SOP 剧本 跟单 批量挂链 停链 换链 "
        "follow-up workflow chain attach",
        "/workspace/workflows",
    ),
    (
        "deal-engine",
        "成交引擎是什么、在哪打开",
        "What is the deal engine and where to enable it",
        "成交引擎按会话内容识别客户所处的旅程阶段（破冰→试探→报价→成交），"
        "在工作台右栏给出「下一步最优动作」与建议挂的跟进链（一键启动），并在"
        "「工作链」页出成交看板（阶段分布/链归因）。它是服务端开关：若「工作链」"
        "页看不到成交引擎卡，说明当前部署尚未开启，请管理员在服务端配置里启用；"
        "开启后旅程阶段与建议会自动出现，无需坐席逐个设置。",
        "The deal engine classifies each conversation's journey stage "
        "(icebreak, probing, quoting, closing), suggests the next best action "
        "and a chain to attach (one tap) in the right-side panel, and adds a "
        "deal dashboard to the Workflows page. It is a server-side switch: if "
        "the Workflows page shows no deal-engine card, this deployment has "
        "not enabled it - ask an admin. Once on, stages and suggestions "
        "appear automatically.",
        "成交引擎 成交 旅程 阶段 报价 下一步 建议链 NBA deal engine journey "
        "stage 看板",
        "/workspace/workflows",
    ),
    (
        "clone-voice-from-message",
        "怎么从客户语音克隆音色并绑定人设",
        "How to clone a voice from a message and bind it to a persona",
        "在会话消息流里找到一条语音 → 点语音行的「⋮」→ 「克隆音色并绑定人设」→ "
        "弹层里确认音色名、选要绑定的人设、勾选授权确认 → 登记。完成页可直接"
        "试听克隆效果，并可「设为本会话音色」或「撤销」。弹层只能用 × / 完成 / "
        "Esc 关闭，点弹层外不会误关（试听生成中也不怕误触）。另一条路：右栏"
        "「语音」组件上传参考音频登记（见「怎么登记/克隆一个新音色」）。",
        "Find a voice message in the thread, click its \"...\" menu, choose "
        "\"Clone voice & bind persona\", then confirm the name, pick the "
        "persona, tick the consent box and register. The success screen lets "
        "you audition the clone, set it for this conversation, or undo. The "
        "dialog closes only via X / Done / Esc - clicking outside never "
        "dismisses it mid-audition. Alternative: upload a reference clip in "
        "the right-side Voice panel.",
        "克隆音色 克隆 音色 绑定人设 语音克隆 声音 试听 授权 voice clone bind "
        "persona",
        "/workspace",
    ),
    (
        "view-image-summary",
        "客户图片的 AI 识别摘要在哪看",
        "Where to see the AI summary of a customer's image",
        "收件箱消息流里，客户发来的图片/视频下方会出现「AI 识图 / AI 识视频」"
        "摘要行（超长默认折叠 3 行，点「展开全文」看完整识别内容）。对着图片"
        "点「问这张图」可以就画面内容追问。识别是给坐席看的内部参考，绝不会以"
        "消息气泡出现在会话里，也不会发给客户（v1.0.66 起连无图附件的识别行"
        "也统一收进摘要卡）。",
        "In the inbox thread, an \"AI vision / AI video\" summary row appears "
        "under customer images and videos (long text folds to three lines - "
        "click expand). Use \"Ask about this image\" on the picture to probe "
        "further. The summary is internal reference for agents: it never "
        "renders as a chat bubble and is never sent to the customer (since "
        "v1.0.66 even rows without a media attachment fold into the card).",
        "识图 识别 图片识别 AI识图 摘要 OCR 图片内容 识视频 问这张图 vision "
        "summary image",
        "/workspace",
    ),
    (
        "gen-image",
        "AI 生成图片怎么用、点了没反应怎么办",
        "How to use AI image generation / button seems dead",
        "坐席工作台右栏工具箱「AI 生成图片」卡：选人设、模式（自拍/物件）、"
        "引擎与场景，填提示词 → 生成 → 预览后可发送到当前会话、存入人设相册或"
        "下载；相册已有合适存货时会先出缩略图，点选可零等待直接发送。面板会如实"
        "显示状态：「功能未启用」＝该部署没有接出图算力（外网部署通常如此，属"
        "正常，不是故障）；引擎显示「未部署」＝选项置灰不可选；「服务器不可达」"
        "＝黄条预警。生成失败会出人话错误卡，附「复制诊断信息」可直接发给运维。",
        "Workspace right-rail toolbox card \"AI image\": pick persona, mode "
        "(selfie/object), engine and scene, write a prompt, generate, then "
        "send to the conversation, save to the album or download. If the "
        "album already has matching stock, thumbnails appear for zero-wait "
        "sending. The panel is honest about state: \"not enabled\" means this "
        "deployment has no image backend (normal for off-LAN installs, not a "
        "bug); undeployed engines are greyed out; \"server unreachable\" "
        "shows an amber banner. Failures show a human-readable error card "
        "with one-tap diagnostic copy.",
        "生成图片 AI生图 出图 自拍 图片生成 没反应 未启用 image generation "
        "generate picture",
        "/workspace",
    ),
    # ── 真实缺口批②（2026-09-01，xiaozhi_gap_report 首跑捞出的高频未答；
    #    口径：问候语代写=老板拍板「引到主输入框 AI 拟稿」；导出/平台=按代码
    #    实况如实说（CSV 字段核实无手机号、平台矩阵四平台）。──
    (
        "ai-draft-for-me",
        "让 AI 帮我写要发给客户的话（问候/回复代写）",
        "Have the AI draft what to say to a customer (greetings/replies)",
        "打开该客户的会话，点输入区的「AI回复」按钮——AI 会按当前人设与聊天"
        "上下文生成草稿并拆成聊天短句，勾选要发的句子（可先编辑）后逐条发送。"
        "要深度改写、换语气或换语言，用右栏「回复工坊」。问候语、破冰、回复"
        "都走这条链：在会话里生成才带得上人设和上下文，我这里代写反而没有"
        "这些信息。",
        "Open the customer's conversation and click the AI Reply button in "
        "the composer: the AI drafts persona-aware text from the chat "
        "context, split into short messages you can tick, edit and send one "
        "by one. For deep rewrites, tone or language changes use the Reply "
        "Workshop in the right rail. Greetings, icebreakers and replies all "
        "go through this - drafting inside the conversation is what gives "
        "the AI your persona and context.",
        # ⚠ 词表刻意不含「帮我写/写一句」：宽词会把「帮我写一首诗」这类闲聊
        # 负样本吸过 min_score（首版实测 90.97 分假命中，金标当场抓出）。
        "问候语 代写 话术 开场白 破冰 草稿 拟稿 AI回复 "
        "greeting draft icebreaker",
        "/workspace",
    ),
    (
        "export-contacts",
        "怎么导出客户列表 / 能导出手机号吗",
        "How to export the customer list / can I export phone numbers",
        "「客户列表」页（/workspace/contacts）右上有「导出 CSV」按钮，按当前"
        "筛选导出（最多 5000 行），字段含：姓名、渠道、旅程阶段、亲密度、"
        "是否有线索、标签、跟进时间、最后活跃时间。**CSV 不含手机号**——"
        "当前版本没有批量导出客户手机号的功能（多数平台本就不向应用暴露"
        "客户真实手机号）。要给单个账号做完整聊天备份，走账号管理的"
        "「导出历史」（JSONL 格式，需管理员权限）。",
        "The Customers page (/workspace/contacts) has an Export CSV button "
        "(current filter, up to 5000 rows) with name, channels, journey "
        "stage, intimacy, lead flag, tags, follow-up and last-active times. "
        "The CSV does NOT contain phone numbers - bulk phone-number export "
        "does not exist in this version (most platforms never expose real "
        "phone numbers to apps). For a full per-account chat backup use "
        "Export history in account management (JSONL, admin only).",
        # ⚠ 词表刻意不含裸「导出/电话/号码/客户」：宽词把「能不能自动打电话
        # 给客户」（92.17 假命中）与「客户是外国人…」这类含「客户」的别家正样
        # 本都吸过来；标题已自带「导出客户列表/手机号」锚点，词表只留最有
        # 区分度的三个名词。
        "手机号 通讯录 CSV export contacts phone backup",
        "/workspace/contacts",
    ),
    (
        "supported-platforms",
        "支持哪些平台 / 支持抖音吗",
        "Which platforms are supported / is Douyin (TikTok) supported",
        "当前支持：Telegram、WhatsApp、LINE、Messenger（Facebook）、Instagram、Zalo，"
        "以及微信客服（企业微信官方通道，工作台「账号管理 → 微信客服 → 接入」五步引导，填企微自建应用凭证、"
        "不用扫码，微信用户扫客服二维码咨询）。个人微信不作为自动聊天渠道接入，但提供「PC 副驾」："
        "读取电脑上已登录的微信、AI 给建议、半自动代发（Windows，入口在账号管理的「个人微信 · PC 副驾」卡，"
        "教程 /help/onboarding/wechat_pc）。账号接入在坐席工作台的账号抽屉或「接入向导」完成。"
        "抖音官方私信仍在接入中（默认关）。TikTok：**个人号没有官方私信接口**；可选获客真机"
        "（非官方、默认关）接管对方开口之后的对话，不代发首触。TikTok Shop 店铺客服走官方 API（默认关）。"
        "官方 Business Messaging 等权限，个人号不在范围内。需要接入其他平台，请切到「报障」标签把需求提交给产品团队。",
        "Supported today: Telegram, WhatsApp, LINE, Messenger (Facebook), Instagram, Zalo, "
        "and WeChat Customer Service (the WeCom official channel — a 5-step guide under Accounts → "
        "WeChat Service → Connect; enter your WeCom self-built app credentials, no QR login; WeChat users "
        "scan the service QR to chat). Personal WeChat is not an auto-chat channel, but a PC copilot is "
        "available: it reads the signed-in WeChat on the PC, drafts suggestions and can semi-auto send "
        "(Windows; see the Personal WeChat · PC copilot card and /help/onboarding/wechat_pc). Accounts "
        "are connected from the account drawer or the Setup Wizard. Douyin official DMs are still being "
        "wired (off by default). TikTok: there is no official DM API for personal accounts; an optional "
        "capture-phone path (unofficial, off by default) can take over a chat after the other person writes "
        "first. TikTok Shop CS uses the official API (off by default). Business Messaging is pending access "
        "and does not cover personal accounts. To request another platform, submit it via the Report tab.",
        # ⚠ 词表刻意不含裸「支持/接入/渠道/哪些」：宽词把「支持多少个并发坐席」
        # （189.92 假命中）与「**哪些**对话需要我亲自处理」这类别家问题吸过来；
        # 标题已自带「支持哪些平台/支持抖音吗」锚点，词表只留平台名。
        "平台 抖音 tiktok 微信 douyin platform supported channels",
        "/workspace",
    ),
    (
        "update-app",
        "怎么把智聊更新到最新版本",
        "How to update ChatX to the latest version",
        "有新版本时应用内会弹更新公告，顶部出现「立即重启更新」横幅——点它重启"
        "即完成升级，聊天数据全部保留。也可以到官网下载页重新下载安装包覆盖"
        "安装（同样保数据）。装好后在「关于」页核对版本号。小修补丁会自动在"
        "后台下载，同样通过重启横幅生效。",
        "When a new version ships, an in-app announcement appears and a "
        "\"restart to update\" banner shows at the top - click it and the "
        "upgrade completes on restart with all data kept. You can also "
        "reinstall from the official download page (data is kept too). Check "
        "the About page for the version afterwards. Small hotfixes download "
        "in the background and apply via the same restart banner.",
        "更新 升级 新版本 最新版 版本 重启更新 下载页 update upgrade version",
        "",
    ),
    # ── v1.0.68 随包新语料（2026-09-02，SOP：宣传的新入口小智必须答得上）──
    (
        "buried-archived-unread",
        "「归档中还有 N 条未读」是什么 / 未读徽标和列表对不上",
        "What is \"N unread in archived\" / badge doesn't match the list",
        "顶部账号/平台的未读徽标现在只统计**未归档私聊**的有效未读——与列表"
        "默认视图同一口径，不再出现「徽标有数、列表里找不到」。归档会话里还有"
        "未读时，收件箱筛选条下会出现琥珀色「归档中还有 N 条未读 · 查看」入口，"
        "点击直达归档视图处理；没有则自动隐藏。归档/取消归档会话时，未读数会"
        "即时在主徽标与该入口之间转移。要回到旧口径（归档未读也计入主徽标），"
        "由管理员开配置 inbox.badge.include_archived。",
        "The account/platform unread badges now count only unarchived private "
        "chats - the same scope as the default list view, so the badge can no "
        "longer show counts you cannot find. When archived conversations "
        "still hold unread messages, an amber \"N unread in archived - view\" "
        "entry appears under the inbox filter bar and jumps straight to the "
        "archived view; it hides itself at zero. Archiving or unarchiving a "
        "conversation moves its unread count between the two instantly. An "
        "admin can restore the old scope via inbox.badge.include_archived.",
        "归档 未读 徽标 被埋 归档未读 badge archived buried unread",
        "/workspace",
    ),
    (
        "resume-deliver-gate",
        "提示「全局已暂停真发」、全自动会话不外发怎么办",
        "Banner says auto-send is paused globally / full-auto chats not sending",
        "全局「真发总闸」被关闭时，全自动/多选档位的会话只拟稿、不外发——"
        "收件箱顶栏会出现「⏸️ 全局已暂停真发」横幅，写明谁、几点、从哪个入口"
        "关的，以及是否会自动恢复；打开受影响的会话也能看到「本会话未真发」"
        "提示条。恢复：主管点横幅上的「▶ 恢复真发」并确认即可，全自动会话"
        "立即恢复自动发送（需主管权限，非主管请联系管理员）。每次开关都有"
        "留痕，回复设置页能看到「最近一次谁关的」；管理员还可配置到点自动"
        "恢复（inbox.l2_autosend.pause_auto_resume_hours，默认关闭）。",
        "When the global deliver master switch is off, full-auto/multi-choice "
        "conversations keep drafting but stop sending - the inbox shows a "
        "\"⏸️ Auto-send paused globally\" banner naming who closed it, when, "
        "via which entry, and whether it auto-resumes; affected conversations "
        "show their own notice. To resume, a supervisor clicks \"▶ Resume "
        "auto-send\" on the banner and confirms - full-auto sending restarts "
        "immediately (supervisor permission required). Every flip is audited "
        "and the reply-settings page shows who last closed it; admins can "
        "also set timed auto-resume (inbox.l2_autosend.pause_auto_resume_"
        "hours, off by default).",
        "暂停真发 真发总闸 恢复真发 不外发 只拟稿 没有发出去 deliver paused "
        "resume auto-send",
        "/workspace",
    ),
    (
        "acct-unread-open",
        "怎么点开账号栏上的未读数字",
        "How to open unread conversations from the account badge",
        "账号栏（顶部账号条或左侧账号坞）每个号旁边的未读数字可以点。"
        "点开后弹出的列表就是这个数字对应的那几条会话（和徽标同一口径），"
        "点一行即打开该会话。有具体账号时还可以点「全部标已读」一次清掉这一号的未读。",
        "The unread number beside each account on the account bar or dock is "
        "clickable. The popover lists exactly the conversations that number "
        "counts; click a row to open it. For a specific account you can also "
        "Mark all read.",
        "未读数字 未读徽标 点开未读 哪几条未读 account unread badge",
        "/workspace",
    ),
    (
        "bind-persona-yellow-dot",
        "账号黄点怎么绑人设",
        "How to bind a persona from the yellow dot",
        "账号栏上的黄色小点表示这个号还没绑定人设，回复会走默认配置。"
        "直接点黄点会打开账号抽屉并进入该号详情，在人设一栏选好人设保存即可。",
        "A yellow dot on an account means no persona is bound. Click the dot "
        "to open the account drawer on that account, then pick a persona and save.",
        "未绑人设 黄点 绑定人设 账号人设 yellow dot persona",
        "/workspace",
    ),
    (
        "set-address-names",
        "怎么设置我和客户怎么互相称呼",
        "How to set what I call the customer and what they call me",
        "打开会话后，右栏「客户关系」身份区有两个输入框：「我怎么叫对方」"
        "和「对方怎么叫我」。改完点别处即保存。空着则沿用人设默认称呼；"
        "想明确不用爱称，把框清空再保存。",
        "Open a conversation. On the Customer tab, the two address fields "
        "set how you call them and how they call you. Changes save on blur. "
        "Leave blank to inherit the persona default; clear a field to drop a nickname.",
        "称呼 怎么叫 爱称 对方怎么叫我 call_peer peer_calls_you 双向称呼",
        "/workspace",
    ),
    (
        "autosend-shadow-release",
        "全自动还会被高风险扣稿吗",
        "Does full-auto still hold high-risk drafts",
        "全自动就是全自动：自动回复不再因「高风险」扣稿转人工，触发只后台记台账、"
        "坐席侧零拦截。你自己在输入框点发送也不会被二次拦住。"
        "风险仍会记入运维台账，一个月后再决定要不要立新规则。",
        "Full-auto stays full-auto: drafts are no longer held for high risk. "
        "Triggers are logged in the back-office ledger only; agent-typed send "
        "is not blocked either. New rules will wait for a month of real data.",
        "全自动 放行 不拦 台账 高风险 扣稿 影子 shadow autosend",
        "/workspace",
    ),
    # ── R82 / Q-18 #292 #291（2026-09-11）：让位 = 延后不丢稿 + 明示接回 ──
    (
        "agent-yield-resume",
        "我手发一句后 AI 为什么不回客户了 / 会话头「AI 让位中」胶囊是什么",
        "Why the AI stops replying after I send manually / what the AI yielding chip means",
        "全自动会话里你手发或打字后 60 秒内，AI 会先让位：这段时间客户来的消息，AI 稿留队等着不发，"
        "会话头出蓝色胶囊「AI 让位中 · 坐席 60s 内发过 · N s 后接回」倒数，倒数到 0 稿自动发出，不丢。"
        "要 AI 马上接回：点这个胶囊，或顶栏重选一次「全自动」，待发稿立即放行；你再手发一句 AI 会再让位一次。"
        "体检面板在让位期间也会出一行黄字并带「立即让 AI 接回」按钮。"
        "1.0.81 及之前这 60 秒内的客户消息会被直接取消不回，本版起只是延后。",
        "In a full-auto thread, for 60 seconds after you send or type, the AI yields: drafts for customer messages in that "
        "window wait in queue and the header shows a blue chip AI yielding · agent sent within 60s · resumes in Ns. "
        "When the countdown hits zero the draft goes out; nothing is dropped. To resume at once, click the chip or re-pick "
        "Full auto in the header; sending manually again yields again. The diagnosis panel shows a yellow row with "
        "Let AI resume now while yielding. Up to 1.0.81 those messages were cancelled with no reply; now they are only deferred.",
        "让位 接回 不回了 手发后 60秒 全自动 不发 胶囊 倒数 立即让AI接回 agent yield resume defer chip",
        "/workspace",
    ),
    # ── R82 / Q-18 #293 #285（2026-09-11）：拦截台账可见 ──
    (
        "blocked-today-card",
        "怎么看今天 AI 为什么没回 / 「今日拦截」卡在哪",
        "How to see why the AI didn't reply today / where the Blocked today card is",
        "打开「自动回复设置」，滚到「🧯 风控分级」卡下面的「🚧 今日拦截 · AI 为什么没回」卡：右上「24h 共 N 次」，"
        "下面按原因码计数——成人内容 / 风险保持 / 需人工 / 坐席刚发过（让位） / 坐席在打字（让位） / 档位切换 / 班表休息，"
        "再往下是最近 5 条（会话显示名 · 时间 · 原因 · 命中词 + 阶段）。台账从本次启动起记、滚动保留 200 条，历史不回填。"
        "让位两项只是延后不是丢稿，窗过自动发；真正没回的看成人 / 风险保持 / 需人工三项，点进对应会话核命中词。"
        "单个会话的原因也可以在会话体检面板里看。",
        "Open Reply settings and scroll below the Risk grading card to Blocked today · why the AI didn't reply: the chip "
        "shows the 24h total, then counts by reason — adult content / risk hold / needs human / agent just sent (yield) / "
        "agent typing (yield) / mode switched / off-hours schedule — and the last 5 rows (thread name · time · reason · "
        "matched words + stage). The ledger starts at this launch, keeps 200 rows rolling and does not backfill. The two "
        "yield reasons only defer and auto-send after the window; for real misses check adult / risk hold / needs human "
        "and open the thread to see the matched words. Per-thread reasons are also in the diagnosis panel.",
        "今日拦截 为什么没回 没回复 拦截 原因 成人 风险保持 需人工 台账 24小时 blocked today abort ledger why no reply",
        "/reply-settings",
    ),
    # ── R82 / Q-19 #294（2026-09-11）：画像 AI 推断只写可锚定事实 ──
    (
        "profile-anchored-facts",
        "画像里的年龄 / 职业为什么是一句话或乱填 / 进度 N/10 怎么算",
        "Why a profile slot shows a whole sentence or a wrong age / how the N/10 progress is counted",
        "本版起 AI 推断只写能在客户原话里逐字找到的事实：年龄只收 16–99 的数字或年龄段（30s / 三十多 / 90后），"
        "职业 / 坐标 / 居住地只收 24 字以内的短语、整句不写；候选必须带客户原句且能在最近 30 条入站里查到，"
        "我方回复、译文、关怀稿不再进抽取，值的语种和客户主语种不符也不写。"
        "目标面板的进度 N/10 只数你点过 ✓ 确认（或手录）的槽，AI 推断另显「待确认 M」，点卡上 ✓ 确认 / ✕ 拒绝处理；"
        "年龄输入框只收数字或年龄段，填错先提示不保存；过长的值按 24 字省略，悬停看全文。"
        "旧版留下的乱值：拒绝掉即可，也可让值守跑清洗脚本按会话清理。",
        "AI inference now writes only facts found verbatim in the customer's own messages: age accepts a number 16–99 or "
        "an age band; occupation / location / residence must be a phrase of 24 characters or fewer, never a sentence; a "
        "candidate needs the customer's quote, verifiable in the last 30 inbound messages; our replies, translations and "
        "care drafts no longer feed extraction, and a value in a language other than the customer's main one is dropped. "
        "The goal panel's N/10 counts only slots you confirmed with ✓ (or typed in); AI-inferred ones show as N unconfirmed "
        "— use ✓ confirm / ✕ reject on the card. The age field accepts a number or band only and warns before saving; long "
        "values are clipped to 24 characters with the full text on hover. Reject leftover bad values from older versions, "
        "or ask ops to run the purge script per thread.",
        "画像 年龄 职业 居住地 乱填 整句 AI推断 待确认 进度 N/10 确认 拒绝 锚定 原话 profile age occupation unconfirmed anchored",
        "/workspace",
    ),
    # ── J-9 #184（2026-09-05）：厂商产品说明从「知识库对客条目」迁到这里 ──
    # 背景：首装曾把生产机 110 条厂商话术（智聊/通译/幻声…价格与卖点）播进
    # 用户 KB → 用户的 AI 对着**用户的客户**推销厂商产品。产品说明属于「用户问
    # 助手」的内置帮助，不属于对客检索语料。价格刻意不写（会变，权威口径在
    # 官方客服/官网），这里只答「是什么 / 去哪问」。
    (
        "vendor-product-lines",
        "无界科技有哪些产品（智聊/通译/幻声…）",
        "What products does Boundless offer (ChatX / LingoX / VoiceX ...)",
        "你现在用的是「智聊 ChatX」——AI 自动接客、翻译、跟进、促单的聊天系统。"
        "同一家（无界科技 BOUNDLESS，官网 bd2026.cc）还有：通译 LingoX（多平台聊天"
        "实时互译 SCRM）、通传 VoxX（克隆声会议/直播同传）、幻声 VoiceX（声音克隆）、"
        "幻颜 FaceX（AI 换脸）、幻影 LiveX（数字人直播/口播）、智拓 ReachX（真机获客），"
        "底座是可私有部署的「无界底座」大模型。价格与试用以官网和官方客服为准。",
        "You are using ChatX, the AI sales-chat product. The same vendor "
        "(Boundless, bd2026.cc) also ships LingoX (multi-platform chat "
        "translation SCRM), VoxX (voice-cloned live interpretation), VoiceX "
        "(voice cloning), FaceX (face swap), LiveX (digital human live/voice-over) "
        "and ReachX (real-device lead generation), all on a self-hostable LLM base. "
        "Pricing and trials: see the official site or vendor support.",
        "产品线 智聊 通译 通传 幻声 幻颜 幻影 智拓 无界 无界科技 ChatX LingoX VoiceX "
        "FaceX LiveX ReachX 有哪些产品 你们还有什么产品 官网",
        "",
    ),
    (
        "vendor-support-contact",
        "怎么联系厂商客服 / 官网下载中心在哪",
        "How to reach vendor support / where is the download center",
        "厂商官方 Telegram 客服 @WJKJ2026（7×24，人工响应约 5 分钟）；产品与价格"
        "自助查询 Bot @tgzkw_bot；官网 bd2026.cc（下载中心在 /download，各版本更新"
        "说明也在那里）。软件报障优先走产品内「诊断包/一键体检」拿 6 位码再发给客服，"
        "定位最快。付款前务必通过官方客服核对收款信息，勿信第三方转发的地址。",
        "Official vendor support on Telegram: @WJKJ2026 (24/7, ~5 min human "
        "response); self-service product/price bot @tgzkw_bot; website bd2026.cc "
        "(downloads and release notes under /download). For bugs, generate a "
        "diagnostic code in-app first, then send it to support. Always verify "
        "payment details with official support only.",
        "客服 联系客服 厂商 官方 下载 下载中心 安装包 官网 报障 诊断包 support "
        "download contact vendor",
        "",
    ),
    (
        "kb-vendor-preset",
        "知识库里的「厂商预置」条目是什么，能删吗",
        "What are the 'vendor preset' knowledge entries and can I delete them",
        "旧版安装包曾把厂商自己的产品话术（智聊/通译等的卖点与报价）一并装进了"
        "你的知识库，它们带「厂商预置」标记。桌面版对客回复检索**已硬性排除**这些"
        "条目（不看启用/停用），不会对你的客户推销厂商产品。知识库页顶部有提示条，"
        "点「查看」可按来源筛出来，点「一键清空」即可全部删除；也可以在来源筛选里"
        "选「厂商预置」逐条处理。你自己新建/导入的条目不受影响。",
        "Older installers copied the vendor's own sales knowledge (ChatX / LingoX "
        "pitches and pricing) into your knowledge base; they are tagged "
        "'vendor preset'. On desktop, customer-facing retrieval hard-excludes them "
        "regardless of the enabled flag, so your AI never pitches vendor products "
        "to your customers. Use the banner on the Knowledge page (View / Clear all) "
        "or the source filter to remove them. Your own entries are untouched.",
        "厂商预置 知识库 清空 删除 预置条目 系统预置 厂商 vendor preset knowledge "
        "清理 一键清空 来源筛选",
        "/knowledge",
    ),
]

# 渠道接入教程（实施96 / TK-1，2026-09-08 老板拍板「也加入小智的提问问答中」）：抖音企业版申请
# （小程序 + 能力实验室）/ TikTok 官方通道 / 付款方式与人民币通道进度。答案与教程页
# /help/onboarding/{slug} 同一份数据（src/assistant/onboarding_guides.py），path 直达教程页。
try:
    from src.assistant.onboarding_guides import howto_tuples as _onboarding_howto_tuples
    _HOWTO.extend(_onboarding_howto_tuples())
except Exception:  # 教程数据不可用不影响其余帮助条目
    pass


# 「带我去」聚光灯锚点（P3 2026-08-21）：跳页后高亮的目标选择器。
# 纪律：**只登记核实过真实存在的稳定 id/data 属性**（2026-08-21 已逐个查证；
# knowledge 新建按钮无 id 刻意不录——猜选择器=聚光灯指向空气比没有更糟）。
# 目标元素不可见时前端优雅跳过（如 reply-settings 守卫卡按 feat 探测显隐）。
_ANCHORS: dict[str, str] = {
    "reply-budget": "#rps-sec-guard",        # 回复额度守卫卡（reply_settings.html 实证）
    "persona-album": '[data-dtab="album"]',  # 人设编辑器「相册」页签（personas.html 实证）
    "clear-filters": ".fp-clear",            # 收件箱「清空筛选」按钮（unified_inbox.html 实证）
}


def build_howto_entries() -> list[dict]:
    """how-to 任务条目 → HelpKB 条目（id=howto:<slug>，幂等）。"""
    out: list[dict] = []
    for slug, title, title_en, content, content_en, keywords, path in _HOWTO:
        out.append(
            {
                "id": f"howto:{slug}",
                "title": title,
                "title_en": title_en,
                "content": content,
                "content_en": content_en,
                "keywords": keywords,
                "source": "seed:howto",
                "path": path,
                "anchor": _ANCHORS.get(slug, ""),
            }
        )
    return out


__all__ = ["build_howto_entries"]
