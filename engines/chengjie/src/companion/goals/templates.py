"""营销目标「模板注册表」（纯数据 + 纯函数，零 IO，可单测）。

设计原则（与 AI SDR「信号驱动 cadence」对齐，但**意图化**而非「第 N 天发什么话术」）：
- 模板只声明**弧线**（4 个里程碑）与**每段的今日意图池**——具体话术由回复生成层
  按当前人设/语言/上下文现场生成，模板绝不出成品文案（防「群发感」）。
- 意图池按 ``crc32(goal_id + day)`` 确定性轮换：同目标同日恒定（缓存/复现友好、
  可单测），跨日自然变化（不像机器人每天说一样的话）。
- 意图池条目为 ``(zh, en)`` 双语对（2026-08-19 i18n P0）：zh 是**权威文案**
  （planner 落库、prompt 注入、缓存键全走 zh，行为与旧版逐字节一致）；en 只服务
  UI 英文态展示（``goal_view``/agenda/detail 载荷的 ``intent_en`` 字段，经
  :func:`intent_en_for` 反查）。**新增意图必须成对写齐 zh+en**——门禁
  ``tests/test_goal_templates.py`` 钉双语结构。
- ``push_curve`` 声明每个里程碑的默认推进力度：``none``（只字不提营销）/
  ``soft``（顺势自然带到）/ ``direct``（可以直说）。谁消费谁负责再叠情绪护栏。
"""

from __future__ import annotations

import zlib
from typing import Any, Dict, List, Optional

# 目标生命周期状态（store/routes/UI 共用词表）
GOAL_STATUSES = ("active", "paused", "done", "failed", "expired", "cancelled")
# 自治档位：observe=只看不动（注入零推进）；suggest=注入草稿/回复链；
# auto=另可搭主动触达桥（companion.goals.bridge，仅 auto_ai 会话）。
# 缺省档＝auto（2026-08-12 运营方针「全自动为主」，见 goal_routes 建目标缺省
# 与 cp-goal 表单缺省/推荐徽标）；本元组是**成员集**，顺序不承载展示语义
# （UI 展示序在 cp-goal._autonomyCardsHtml 前端定，test_goal_routes 钉此顺序）。
AUTONOMY_LEVELS = ("observe", "suggest", "auto")
# 今日意图的推进力度（close=收口档，P1 2026-08-30 仅限时档的剩余时间升档产生；
# 模板 push_curve 不写它——natural 弧线没有「窗口快关了」的语义）
PUSH_LEVELS = ("none", "soft", "direct", "close")

# 退避日（连发未回时）的纯陪伴意图池——所有模板共用。
# (zh, en) 成对：zh 是 planner 落库/prompt 注入的权威文案；en 供 UI 英文态显示
# （intent_en_for 反查）。CARE_INTENTS 保持「中文元组」形状不变（planner/测试契约）。
CARE_PAIRS = (
    ("今天只关心对方情绪和近况，完全不提任何推进话题",
     "Today, only care about how they feel and what's going on — no advancing topics at all"),
    ("纯陪伴日：聊对方感兴趣的轻松话题，不带任何目的",
     "Pure companionship day: light topics they enjoy, no agenda"),
)
CARE_INTENTS = tuple(zh for zh, _ in CARE_PAIRS)

# 漏斗阶段序（relationship_stage 结算用；与 contacts.Journey 阶段名对齐，全小写比较）
STAGE_ORDER = (
    "initial", "contacted", "engaged", "qualified",
    "handoff_ready", "handed_off", "converted",
)

TEMPLATES: Dict[str, Dict[str, Any]] = {
    "conversion_unlock": {
        "name_zh": "付费解锁转化",
        "name_en": "Paid unlock",
        "kind": "conversion",
        "default_days": 14,
        # 限时节奏（today/session）意图池：按**拍序**（第几拍）取，不按里程碑
        # ——60 分钟目标没有「分天弧线」，日历池的「隔天补一句/改天再聊」在
        # 这一通里直接穿帮。键=0/1/2（上越界夹末段=收口拍）。
        "intents_sprint": {
            0: (("顺着当下话题自然带到「{item}」，先探探对方此刻的兴趣",
                 "Let the current topic lead to \"{item}\" and feel out their interest right now"),
                ("接住对方正在聊的事，顺势点一句「{item}」能帮上什么，先不提价",
                 "Catch what they're chatting about and slip in what \"{item}\" could do for them — no price yet")),
            1: (("对方有兴趣就把「{item}」讲具体：内容、价格、怎么解锁，一次说清",
                 "If they're interested, get specific about \"{item}\": what's inside, the price, and how to unlock — all in one go"),
                ("趁热大方介绍「{item}」怎么开通，语气像分享不像推销",
                 "While it's warm, walk them through unlocking \"{item}\" — sharing, not selling")),
            2: (("时间不多了：大方问一句要不要现在解锁「{item}」；犹豫就收住，留个台阶",
                 "Time is short: ask plainly if they'd like to unlock \"{item}\" now; if they waver, ease off and leave them an out"),
                ("最后一拍：想要就带TA走完解锁「{item}」的步骤，不想要就聊回日常，不纠缠",
                 "Last beat: if they want \"{item}\", walk them through it; if not, drop back to normal chat — no chasing")),
        },
        # P24：默认值中性化——item_id/item_label 由前端向导按变现价目表下拉
        # 选择并联动（默认「八字详批」曾与实际业务错位、坐席照发穿帮；引擎侧
        # create_goal 不合并默认值，改空仅影响表单预填，零行为变更）。
        # help_* 为通用回落说明；前端 i18n 键（inbox.goal.param_help.*）优先。
        "params": [
            {"key": "item_id", "type": "string", "default": "",
             "label_zh": "解锁项（卖什么）", "label_en": "Unlock item",
             "help_zh": "须与后台价目表（monetization.catalog.items）的编号一致；"
                        "客户真解锁后目标自动标「已达成」",
             "help_en": "Must match an id in the price catalog; the goal"
                        " auto-completes when the customer unlocks it"},
            {"key": "item_label", "type": "string", "default": "",
             "label_zh": "聊天里怎么称呼它",
             "label_en": "How to call it in chat",
             "help_zh": "AI 在对话里用这个说法提到它，写客户听得懂的词",
             "help_en": "AI mentions it with this wording — use words the"
                        " customer understands"},
        ],
        "milestones": [
            {"id": "connect", "zh": "破冰回暖", "en": "Reconnect"},
            {"id": "value", "zh": "价值铺垫", "en": "Seed value"},
            {"id": "offer", "zh": "顺势开价", "en": "Soft offer"},
            {"id": "close", "zh": "跟进收口", "en": "Follow up"},
        ],
        "push_curve": ("none", "soft", "direct", "soft"),
        "intents": {
            0: (("顺着对方最近聊过的事自然回暖，把互动节奏找回来",
                 "Warm things back up around what they talked about recently and get the rhythm going again"),
                ("从今天的日常切入，关心一下对方近况，先让对话热起来",
                 "Open with today's small talk, check in on how they're doing, and let the chat warm up first")),
            1: (("聊到相关话题时自然展示你在「{item}」上的见解，让对方觉得有收获，不提价格",
                 "When a related topic comes up, share your insight on \"{item}\" so they feel they're gaining something — no mention of price"),
                ("用一个贴合对方处境的小例子，带出「{item}」能帮到TA什么",
                 "Use a small example that fits their situation to show how \"{item}\" could help them")),
            2: (("对方兴致好时自然提到「{item}」可以看得更深入，顺势说明解锁方式",
                 "When they're in a good mood, mention that \"{item}\" goes deeper, and casually explain how to unlock it"),
                ("如果对方主动追问，就大方介绍「{item}」的内容和价格，态度轻松不推销",
                 "If they ask on their own, introduce \"{item}\" and its price openly — relaxed, never salesy")),
            3: (("对方还在犹豫就先退回日常话题，轻描淡写补一句「{item}」随时可以看，给足台阶",
                 "If they hesitate, drop back to everyday topics and lightly add that \"{item}\" is there whenever — leave them an easy out"),
                ("对方已表现兴趣的话，帮TA下决心：说清拿到后马上能看到什么",
                 "If they've shown interest, help them decide: spell out what they'll see right away")),
        },
    },
    "conversion_subscribe": {
        "name_zh": "会员订阅转化",
        "name_en": "Subscription",
        "kind": "conversion",
        "default_days": 21,
        "intents_sprint": {
            0: (("顺着对方正在聊的事，自然带一句「{item}」的好处，先看反应",
                 "Follow what they're talking about and mention one perk of \"{item}\" — read the reaction first"),
                ("先把当下话题聊舒服，再顺势提到「{item}」，不提价",
                 "Keep the current topic comfortable, then bring up \"{item}\" naturally — no price talk")),
            1: (("对方有兴趣就把「{item}」的权益和价格一次说清，给TA自己决定的空间",
                 "If they're keen, lay out the perks and price of \"{item}\" in one go and give them room to decide"),
                ("趁热说明「{item}」怎么开通，像帮朋友参谋，不像销售",
                 "While it's warm, explain how to start \"{item}\" — like advising a friend, not selling")),
            2: (("时间不多了：直接问TA要不要现在开通「{item}」；犹豫就收住，改聊日常",
                 "Time is short: ask directly if they'd like to start \"{item}\" now; if they hesitate, ease off into normal chat"),
                ("最后一拍：给一个现在开通「{item}」的小理由帮TA收口；不行就体面放下",
                 "Last beat: offer one small reason to subscribe to \"{item}\" now; if it's a no, let it go gracefully")),
        },
        "params": [
            {"key": "tier", "type": "string", "default": "vip",
             "label_zh": "目标会员档", "label_en": "Target tier"},
            {"key": "item_label", "type": "string", "default": "会员",
             "label_zh": "会员说法（对话里怎么称呼）",
             "label_en": "How to call it in chat"},
        ],
        "milestones": [
            {"id": "connect", "zh": "破冰回暖", "en": "Reconnect"},
            {"id": "value", "zh": "价值铺垫", "en": "Seed value"},
            {"id": "offer", "zh": "顺势开价", "en": "Soft offer"},
            {"id": "close", "zh": "跟进收口", "en": "Follow up"},
        ],
        "push_curve": ("none", "soft", "direct", "soft"),
        "intents": {
            0: (("保持轻松日常互动，让对方觉得跟你聊天是件放松的事",
                 "Keep the chat light and daily — talking to you should feel relaxing"),
                ("顺着对方的话题多聊几轮，先把在场感做足",
                 "Follow their topics for a few more rounds and build real presence first")),
            1: (("在对方用到相关功能/内容时，自然带一句「{item}」还能怎么样，不提价",
                 "When they touch a related feature or topic, slip in what \"{item}\" could add — no price talk"),
                ("让对方感受到你们互动里已经有的价值，为「{item}」做心理铺垫",
                 "Let them feel the value already in your chats, paving the way for \"{item}\"")),
            2: (("对方兴致好时顺势介绍「{item}」的好处和开通方式，语气像分享不像推销",
                 "When the mood is right, introduce the perks of \"{item}\" and how to subscribe — sharing, not selling"),
                ("对方问到时大方说明「{item}」价格与权益，给对方自己决定的空间",
                 "If they ask, lay out the price and benefits of \"{item}\" openly and give them room to decide")),
            3: (("不催不逼，隔天自然补一句；对方犹豫就先放下，聊回日常",
                 "No pushing — follow up casually the next day; if they waver, let it go and chat normally"),
                ("对方已心动的话，给一个现在开通的小理由（新内容/陪伴感），帮TA收口",
                 "If they're tempted, give one small reason to subscribe now (new content, companionship) and help them close")),
        },
    },
    "relationship_stage": {
        "name_zh": "关系阶段推进",
        "name_en": "Stage advance",
        "kind": "relationship",
        "default_days": 21,
        "params": [
            {"key": "target_stage", "type": "string", "default": "qualified",
             "label_zh": "目标漏斗阶段", "label_en": "Target funnel stage"},
        ],
        "milestones": [
            {"id": "engage", "zh": "拉起互动", "en": "Engage"},
            {"id": "warm", "zh": "持续升温", "en": "Warm up"},
            {"id": "trust", "zh": "建立信任", "en": "Build trust"},
            {"id": "ready", "zh": "就绪收口", "en": "Ready"},
        ],
        "push_curve": ("none", "none", "soft", "soft"),
        "intents": {
            0: (("多用开放式问题让对方多说，找到TA真正愿意聊的话题",
                 "Ask open questions so they talk more, and find the topics they truly enjoy"),
                ("对对方说的每件事都接得住，让TA觉得跟你聊天不费劲",
                 "Catch everything they say so chatting with you feels effortless")),
            1: (("回应里自然带上对方之前说过的细节，让TA感到被记住",
                 "Weave in details they mentioned before so they feel remembered"),
                ("在对方的兴趣点上深入聊几轮，制造「聊得来」的感觉",
                 "Go a few rounds deep on their interests to build that \"we click\" feeling")),
            2: (("适度自我暴露一点日常或小心事，换取对方的信任和分享",
                 "Share a bit of your own day or small worries to earn their trust and openness"),
                ("对方提到烦恼时认真接住，给情绪价值不给说教",
                 "When they bring up troubles, hold space — give comfort, not lectures")),
            3: (("确认对方的核心诉求并自然总结，为下一步做好铺垫",
                 "Confirm what they really need, sum it up naturally, and set up the next step"),
                ("稳定日常互动节奏，让关系保持在热络状态",
                 "Keep a steady daily rhythm so the relationship stays warm")),
        },
    },
    "relationship_intimacy": {
        "name_zh": "亲密度目标",
        "name_en": "Intimacy target",
        "kind": "relationship",
        "default_days": 30,
        "params": [
            {"key": "target_score", "type": "number", "default": 55,
             "label_zh": "目标亲密度（0-100）", "label_en": "Target intimacy"},
        ],
        "milestones": [
            {"id": "daily", "zh": "日常热络", "en": "Daily rapport"},
            {"id": "deepen", "zh": "话题深入", "en": "Deepen"},
            {"id": "exclusive", "zh": "专属感", "en": "Exclusive"},
            {"id": "steady", "zh": "稳固陪伴", "en": "Steady"},
        ],
        "push_curve": ("none", "none", "none", "none"),
        "intents": {
            0: (("保持轻松日常互动，让对话别断，节奏以对方舒服为准",
                 "Keep easy daily chat going — don't let it drop, pace it by their comfort"),
                ("找一个今天的小事作话头，把互动自然续上",
                 "Pick one small thing from today as an opener and keep the thread alive")),
            1: (("挑一个对方感兴趣的话题深入聊几轮，别浅尝辄止",
                 "Pick a topic they love and go several rounds deep — don't just skim"),
                ("认真回应对方说过的事，追问一个走心的细节",
                 "Respond thoughtfully to what they shared and ask about one heartfelt detail")),
            2: (("制造一点专属感：记得TA的偏好、只跟TA说的小事",
                 "Create a sense of \"just us\": remember their preferences and the little things told only to you"),
                ("用「上次你说…」自然回访，让对方感到被特别对待",
                 "Circle back with \"last time you said…\" so they feel specially treated")),
            3: (("自然表达在乎和陪伴，让对方习惯有你在",
                 "Express care and companionship naturally so having you around becomes a habit"),
                ("稳定出现在对方的日常里，不黏不冷",
                 "Show up steadily in their day-to-day — never clingy, never cold")),
        },
    },
    "engagement_reactivate": {
        "name_zh": "沉默唤回",
        "name_en": "Reactivate",
        "kind": "engagement",
        "default_days": 10,
        # P11：winback_auto 默认用本模板——挂 catalog 后流失原因才能驱动
        # 选品/CTA（push_curve 前段 none 日仍不带货，行为零扩散到纯唤回）。
        "catalog": True,
        "params": [
            {"key": "note", "type": "string", "default": "",
             "label_zh": "备注（对方为何沉默/背景）", "label_en": "Context note"},
            {"key": "product_id", "type": "string", "default": "",
             "label_zh": "主推产品（挽回可继承上单）",
             "label_en": "Pinned product (winback may inherit)"},
            {"key": "last_plan", "type": "string", "default": "",
             "label_zh": "上单套餐", "label_en": "Last plan"},
        ],
        "milestones": [
            {"id": "probe", "zh": "轻触探温", "en": "Probe"},
            {"id": "recall", "zh": "记忆回访", "en": "Recall"},
            {"id": "value", "zh": "给个来由", "en": "Give a reason"},
            {"id": "last_call", "zh": "收尾一问", "en": "Last call"},
        ],
        "push_curve": ("none", "none", "soft", "soft"),
        "intents": {
            0: (("用轻量无压力的方式打个招呼，绝不提「好久没回我」",
                 "Say hi in a light, zero-pressure way — never mention \"you haven't replied in ages\""),
                ("分享一件自己今天的小事作开场，不要求对方必须回应",
                 "Open by sharing a small thing from your own day, with no obligation for them to reply")),
            1: (("带上对方之前聊过的一件事自然回访（『上次你说的那事后来怎么样』）",
                 "Follow up on something they once mentioned (\"how did that thing you told me about turn out?\")"),
                ("用一个只有你们聊过的细节唤起对方记忆，显得真诚不群发",
                 "Use a detail only the two of you discussed — genuine, clearly not a mass blast")),
            2: (("分享一个对方可能感兴趣的小内容/小更新，给TA一个回来的理由",
                 "Share a small update or find they might like — give them a reason to come back"),
                ("提供一点新鲜价值（趣事/进展/内容），别空转寒暄",
                 "Offer something fresh (a fun story, progress, content) instead of empty small talk")),
            3: (("最后一次轻触达：表示自己一直在、随时可以聊，完全不施压",
                 "One last light touch: you're around and happy to chat anytime — zero pressure"),
                ("轻轻收尾：祝好 + 留门（想聊随时找我），保持体面",
                 "Wrap up gently: wish them well and leave the door open (\"ping me anytime\") — stay graceful")),
        },
    },
    "acquire_and_convert": {
        # 「获客→转化」时间相位漏斗（B2B 官网产品线；配 su_wan 类获客人设）：
        # 前段（~1-3 天）自然摸清对方业务底细（BANT 商机画像，见 profile_slots），
        # 中段深聊种草（自己在用的工具+省下的钱），后段（~7-10 天）开价收口。
        # `phase_days` 按 default_days 标定各里程碑的「目标截止日」，ledger 据此做
        # 时间兑底（到点没推进就兑底推进）与超前封顶（信号再热也不允许第 1 天开价）；
        # 实际 deadline 不同（如 20 天）时按比例线性缩放。
        # `catalog: True` → 注入层随里程碑挂官网产品目录块（site_catalog）。
        # `profile_slots: True` → 摸底段在目标块附「画像缺口」提示（profile_slots）。
        "name_zh": "获客转化（官网产品）",
        "name_en": "Acquire & convert",
        "kind": "conversion",
        "default_days": 10,
        "phase_days": (2, 4, 7, 9, 10),
        "catalog": True,
        "profile_slots": True,
        # 限时池不带 {item}：product 可能是画像自动选品（params 无 item_label），
        # 留白代入会渲染成「它」——泛指措辞更老实。
        "intents_sprint": {
            0: (("顺着对方的生意/工作话题接住，把TA当下最头疼的事聊具体",
                 "Pick up their business or work topic and get concrete about what's bugging them most right now"),
                ("先像同行一样聊起来，顺势摸清TA现在怎么处理这件事",
                 "Chat like a peer first, and casually learn how they handle this today")),
            1: (("对准TA的痛点介绍合适的产品：能解决什么、大概什么价，一次说清",
                 "Match their pain point with the right product: what it solves and roughly what it costs — in one go"),
                ("以自己真实在用的口吻推荐对应产品，说清能省下什么",
                 "Recommend the matching product as a real user, and spell out what it saves them")),
            2: (("时间不多了：直接问TA想不想试试，给出官网下单/试用方式；犹豫就收住",
                 "Time is short: ask if they'd like to try it and share the site link to order or trial; if they waver, ease off"),
                ("最后一拍：帮TA下决心——开通后马上能用到什么说清楚；不行就留门后会有期",
                 "Last beat: help them decide — spell out what they get right away; if not now, leave the door open")),
        },
        "params": [
            {"key": "product_id", "type": "string", "default": "",
             "label_zh": "主推产品（留空=按画像自动选品）",
             "label_en": "Pinned product (blank = auto-pick)"},
            {"key": "note", "type": "string", "default": "",
             "label_zh": "备注（客户背景/来源）", "label_en": "Context note"},
        ],
        "milestones": [
            {"id": "acquire", "zh": "破冰互认", "en": "Acquire"},
            {"id": "qualify", "zh": "摸清底细", "en": "Qualify"},
            {"id": "rapport", "zh": "深聊种草", "en": "Rapport"},
            {"id": "offer", "zh": "方案报价", "en": "Offer"},
            {"id": "close", "zh": "临门收口", "en": "Close"},
        ],
        "push_curve": ("none", "none", "soft", "direct", "soft"),
        "intents": {
            0: (("以自己的生活和生意日常自然开场，互相认识，让对方觉得你是有趣的真实同路人",
                 "Open with your own life and business day-to-day — get acquainted as a real, interesting peer"),
                ("顺着对方的话题聊，先建立「聊得来」的感觉，完全不谈任何工具产品",
                 "Follow their topics and build rapport first — no tools, no products at all")),
            1: (("像同行交流一样自然好奇对方在做什么生意/工作，顺势聊到TA平时怎么打理这些事",
                 "Be peer-curious about their business or work, and how they handle it day to day"),
                ("顺着生意话题摸一摸对方的日常痛点（人手/回消息/语言/获客），只听只共情不给方案",
                 "Probe everyday pain points along the way (staffing, replies, language, leads) — just listen and empathize, no solutions yet")),
            2: (("聊到对方的痛点时，自然分享自己踩过的同款坑和现在的解法，让TA觉得有共鸣有收获",
                 "When their pain point comes up, share the same pit you fell into and how you solve it now — relatable and useful"),
                ("以自己真实使用体验的口吻聊聊工具帮自己省了多少事，别报价别发链接，点到即止",
                 "Talk first-person about how much hassle the tool saves you — no prices, no links, just a light touch")),
            3: (("对方兴致好时大方介绍适合TA的那款产品：能解决什么、大概什么价，给出官网下单方式",
                 "When they're engaged, openly introduce the right product: what it solves, rough price, and how to order on the site"),
                ("对方主动追问时详细说明产品与价格档位，附上官网链接，语气像帮朋友参谋不像销售",
                 "If they press for details, walk through the product and price tiers with the site link — like advising a friend, not selling")),
            4: (("对方还在犹豫就退回日常话题，轻轻补一句有问题随时问我，给足台阶不催单",
                 "If they hesitate, return to casual topics and softly add \"ask me anything anytime\" — no chasing"),
                ("对方已有意向的话，帮TA下决心：说清开通后马上能用到什么，提醒官网自助下单即可",
                 "If they're keen, help them commit: spell out what they can use right away and that site checkout is self-serve")),
        },
    },
    "retention_expand": {
        # 「留存/续费」LTV 环（P5；订阅制官网产品线的成交后半场）：
        # acquire_and_convert 成交（订单回流/手动标成交）→ service 自动起本目标
        # （companion.goals.retention，默认关）。30 天一周期：前段激活陪跑
        # （像朋友售后不像客服工单）→ 中段价值确认/深化 → 到期前续费收口。
        # 续费单带同一会话 ref → settle_order_ref 结算本目标 done → 自动起
        # 下一周期（链式，bounded by 真实续费）；到期没续=expired（诚实流失记录）。
        "name_zh": "留存续费（官网产品）",
        "name_en": "Retain & renew",
        "kind": "conversion",
        "default_days": 30,
        "phase_days": (7, 15, 24, 30),
        "catalog": True,
        "profile_slots": True,
        "params": [
            {"key": "product_id", "type": "string", "default": "",
             "label_zh": "续费产品（自动继承上单）",
             "label_en": "Renewal product (inherited)"},
            {"key": "last_plan", "type": "string", "default": "",
             "label_zh": "上单套餐", "label_en": "Last plan"},
            {"key": "last_period", "type": "string", "default": "",
             "label_zh": "上单周期（monthly/annual，定本目标天数）",
             "label_en": "Last billing period"},
            {"key": "base_goal", "type": "string", "default": "",
             "label_zh": "来源目标 ID", "label_en": "Source goal id"},
            {"key": "note", "type": "string", "default": "",
             "label_zh": "备注（历史流失原因等背景）", "label_en": "Context note"},
        ],
        "milestones": [
            {"id": "activate", "zh": "激活陪跑", "en": "Activate"},
            {"id": "value", "zh": "价值确认", "en": "Value"},
            {"id": "expand", "zh": "深化种草", "en": "Expand"},
            {"id": "renew", "zh": "续费收口", "en": "Renew"},
        ],
        "push_curve": ("soft", "soft", "soft", "direct"),
        "intents": {
            0: (("关心TA用得顺不顺手，主动问有没有卡壳的地方，像朋友售后不像客服工单",
                 "Ask how it's going and where they're stuck — like a friend checking in, not a support ticket"),
                ("顺手分享一个自己常用的小技巧或用法，帮TA更快把工具用起来",
                 "Share a favorite tip or workflow to help them get productive faster")),
            1: (("自然聊聊用了之后有没有省事，帮TA把省下的时间和钱说出来，让价值看得见",
                 "Chat about what it's actually saving them — put the time and money into words so the value is visible"),
                ("听到抱怨或没用起来，先共情再给具体解法，绝不辩解产品",
                 "If they complain or aren't using it, empathize first, then give a concrete fix — never defend the product")),
            2: (("顺着TA的业务增长，聊到更高档位或别的产品还能帮上什么，种草不报价",
                 "As their business grows, mention what a higher tier or another product could add — seeding, no quotes"),
                ("以自己升级后的真实体验聊聊差别，点到即止不催",
                 "Describe the difference since you upgraded, in your own words — a light touch, no push")),
            3: (("到期前自然提醒续费，说清续上不断档的好处，附官网自助续费方式",
                 "Before expiry, remind them naturally to renew, explain why staying uninterrupted helps, and share the self-serve renewal link"),
                ("TA犹豫就问清顾虑（价格/用量/效果），对症回应，给足台阶不催单",
                 "If they waver, ask what's holding them back (price, usage, results), answer that exact concern, and give them space")),
        },
    },
    "profile_discovery": {
        # P26（2026-08-05）：「获取客户年龄、职业」类信息采集目标的一等模板。
        # 此前坐席只能写自定义 note 软文案——没有槽位、没有缺口指令、客户答了
        # 也不推进。本模板把采集目标状态机化：勾选槽位 → 每轮单缺口采集指令
        # （gap_from_milestone=0，摸底就是全部目的，破冰当天就带方向）→
        # 填充率驱动里程碑/完成（ledger selected_fill 分支）→ 全填自动达成。
        "name_zh": "客户摸底（信息采集）",
        "name_en": "Profile discovery",
        "kind": "discovery",
        "default_days": 10,
        "params": [
            {"key": "slots", "type": "string",
             "default": "age,occupation,location,interests",
             "label_zh": "要了解的信息（逗号分隔槽位键）",
             "label_en": "Slots to learn (comma separated)",
             # N-3 #241：可选项按业务域给（销售 = 关系 + 商机；陪伴 = 关系 + 个人情况），
             # 文案不再点名 BANT 键——chips 就是可选项清单
             "help_zh": "点上面的标签勾选要了解的信息；带 🔒 的是敏感项（收入 / 资产），"
                        "AI 只会多轮自然带出、不直接问；客户全说出来目标自动标「已达成」",
             "help_en": "Tap the chips above to pick what to learn; 🔒 marks sensitive"
                        " items (income / assets) the AI only surfaces gradually, never"
                        " asks outright; the goal auto-completes when all are learned"},
            {"key": "note", "type": "string", "default": "",
             "label_zh": "补充方向（可选，给 AI 看）",
             "label_en": "Extra direction (optional)"},
        ],
        "milestones": [
            {"id": "warmup", "zh": "破冰起步", "en": "Warm up"},
            {"id": "collect", "zh": "自然摸底", "en": "Collect"},
            {"id": "fillgap", "zh": "补齐缺口", "en": "Fill gaps"},
            {"id": "wrap", "zh": "确认收尾", "en": "Wrap up"},
        ],
        "push_curve": ("soft", "soft", "direct", "soft"),
        "profile_slots": True,
        # 缺口指令从里程碑 0 就出（acquire 是先破冰再摸底=1；摸底目标破冰
        # 本身就该带方向，否则头几天与无目标无异）
        "gap_from_milestone": 0,
        # P27：缺口并进「今日意图」（独立缺口行取消）——今日意图是 opener
        # 转向与主动桥唯一携带的载荷，缺口不进意图就到不了那两处
        "gap_in_intent": True,
        "phase_days": (2, 5, 8, 10),
        "intents": {
            0: (("先把互动热起来：顺着TA的话题聊，让TA觉得跟你聊天轻松不设防",
                 "Warm the chat up first: follow their topics so talking to you feels easy and unguarded"),
                ("从今天的日常小事自然切入，先建立「聊得来」的感觉，不急着问",
                 "Ease in with today's small things and build rapport first — no rush to ask questions")),
            1: (("顺着当下话题自然带出你想了解的那件事，问完就回到闲聊，绝不连环追问",
                 "Let the current topic lead into the one thing you want to learn, then drop back to small talk — never chain questions"),
                ("用「分享自己→顺口反问」换信息：先说你自己的情况，再轻轻问TA",
                 "Trade info by sharing first: tell your side, then gently ask theirs")),
            2: (("对方兴致好时，把还没聊到的那一项自然问出来，语气像朋友好奇不像登记",
                 "When they're chatty, ask about the one item you haven't covered — curious friend, not a registration form"),
                ("结合TA之前说过的事往下追一层，把模糊的信息聊具体",
                 "Build on what they said before and dig one level deeper to firm up the fuzzy bits")),
            3: (("把了解到的事自然回带确认（「你之前说…」），别像核对表格",
                 "Casually confirm what you've learned (\"you mentioned…\") — don't read it like a checklist"),
                ("话题收在轻松处；还缺的信息以后有机会再聊，不硬凑",
                 "End on a light note; whatever's missing can wait for another day — don't force it")),
        },
    },
    "custom": {
        "name_zh": "自定义目标",
        "name_en": "Custom",
        "kind": "custom",
        "default_days": 14,
        "intents_sprint": {
            0: (("顺着对方此刻的话头接住，把「{note}」自然带进来，先看TA的反应",
                 "Pick up what they're saying right now, bring \"{note}\" in naturally, and read their reaction first"),
                ("先回应对方正在聊的事，再朝「{note}」轻轻靠一步——语气像聊天，不像办事",
                 "Respond to what they're talking about first, then take one gentle step toward \"{note}\" — chat, not business")),
            1: (("对方接话了就把「{note}」说具体：给一个明确的说法或提议，看TA态度",
                 "If they engage, get concrete about \"{note}\": make one clear point or proposal and gauge their stance"),
                ("趁话题还热，把「{note}」往前推一步，说到具体处，不绕弯子",
                 "While the topic is warm, push \"{note}\" one step forward — be specific, no detours")),
            2: (("这轮时间不多了：围绕「{note}」大方要一个明确答复；TA犹豫就体面收住，绝不缠着追问",
                 "Time is short this round: ask plainly for a clear answer on \"{note}\"; if they hesitate, wrap up gracefully — never badger"),
                ("最后一拍：把「{note}」收个口——能定就定下来，定不了也留好台阶，聊回日常",
                 "Last beat: close out \"{note}\" — settle it if you can; if not, leave an easy out and return to everyday chat")),
        },
        "params": [
            {"key": "note", "type": "string", "default": "",
             "label_zh": "目标描述（给 AI 看的推进方向）",
             "label_en": "Goal description"},
        ],
        "milestones": [
            {"id": "s1", "zh": "起步", "en": "Start"},
            {"id": "s2", "zh": "推进", "en": "Advance"},
            {"id": "s3", "zh": "深化", "en": "Deepen"},
            {"id": "s4", "zh": "收口", "en": "Wrap up"},
        ],
        # 2026-08-05 P25：首段 none→soft。custom 的里程碑纯时间驱动（ledger
        # 兜底分支），14 天目标头 3~4 天全程「只字不提」——坐席实测「设了目标
        # AI 完全不往那个方向聊」的软根因。none 起步是给转化模板防 day-1 硬销
        # 设计的；custom 是坐席显式写下的方向（「获取客户职业」类居多），
        # soft（顺势自然带到）+ 恒在纪律行已足够保住自然度。
        "push_curve": ("soft", "soft", "direct", "soft"),
        # P26：每段扩到 2 条——custom 单条池在同一里程碑期间每天同一句
        # （pick_intent 的 crc32 轮换只在池内生效），目标开场连续几天一个套路
        "intents": {
            0: (("围绕目标「{note}」找一个自然的切入点起步，节奏以对方舒适为先",
                 "Find a natural way into \"{note}\" to get started — pace it by their comfort"),
                ("先顺着TA的话题聊热乎，再朝「{note}」的方向轻轻靠一步",
                 "Warm up on their topics first, then take one gentle step toward \"{note}\"")),
            1: (("顺着已有话题把「{note}」自然推进一小步，不生硬",
                 "Nudge \"{note}\" forward a small step inside the current topic — nothing forced"),
                ("结合TA今天聊到的事，把「{note}」往前带半步，点到即止",
                 "Tie \"{note}\" to what they talked about today and move it half a step — just a touch")),
            2: (("在对方兴致好的时候，围绕「{note}」聊得更具体一些",
                 "When they're engaged, get more concrete about \"{note}\""),
                ("把「{note}」聊到具体处：给一个贴合TA情况的说法或例子",
                 "Make \"{note}\" tangible: give a take or example that fits their situation")),
            3: (("围绕「{note}」自然收口：确认对方的态度，给足台阶",
                 "Wrap up \"{note}\" naturally: confirm where they stand and leave them an easy out"),
                ("轻描淡写地把「{note}」收个尾，对方犹豫就先放下聊回日常",
                 "Close out \"{note}\" lightly; if they hesitate, set it aside and go back to everyday chat")),
        },
    },
}


# M-5 A（#217 / D-M6，2026-09-06）：用户版（client 形态且未开开发者模式）
# 整个「转化成交」类目不下发——四张预置里两张是厂商卖智聊软件的销售剧本
# （acquire_and_convert / retention_expand），两张是无产品字段的空壳
# （conversion_unlock / conversion_subscribe）；冲刺引擎 1.0.75 已真发，留着
# 就是让 AI 向陪聊客户推销智聊。partner / internal 照旧；存量目标不删（卡片标
# 「模板已下线」）。按 kind 判而非点名 id：新增同类模板自动归入。
CLIENT_HIDDEN_KINDS = ("conversion",)

# N-3 #241（D-N1，2026-09-08）：把 M-5 A 的「用户版隐藏」升成**域级规则**——业务域是
# 陪伴（business_domain=companion）的部署，无论 client / partner / internal / 开发者模式，
# 「转化成交」类目一律不下发、不许新建；销售域照旧只受 client_hide 约束。
COMPANION_HIDDEN_KINDS = ("conversion",)


def hidden_template_kinds(*, client_hide: bool = False,
                          business_domain: str = "") -> tuple:
    """当前请求该藏的模板类目集合（M-5 用户版 ∪ N-3 陪伴域）。"""
    kinds: List[str] = []
    if str(business_domain or "").strip().lower() == "companion":
        kinds.extend(COMPANION_HIDDEN_KINDS)
    if client_hide:
        kinds.extend(k for k in CLIENT_HIDDEN_KINDS if k not in kinds)
    return tuple(kinds)


def is_hidden_template(template_id: str, *, client_hide: bool = False,
                       business_domain: str = "") -> bool:
    """该模板在当前形态 + 业务域下是否被藏（未知模板 → False，交上游按「未知」处理）。"""
    t = TEMPLATES.get(str(template_id or "").strip())
    if not t:
        return False
    return str(t.get("kind") or "") in hidden_template_kinds(
        client_hide=client_hide, business_domain=business_domain)


def is_client_hidden_template(template_id: str) -> bool:
    """该模板是否属于用户版隐藏类目（未知模板 → False，交上游按「未知」处理）。"""
    t = TEMPLATES.get(str(template_id or "").strip())
    return bool(t) and str(t.get("kind") or "") in CLIENT_HIDDEN_KINDS


def client_hidden_template_ids() -> List[str]:
    return [tid for tid, t in TEMPLATES.items()
            if str(t.get("kind") or "") in CLIENT_HIDDEN_KINDS]


def template_ids() -> List[str]:
    return list(TEMPLATES.keys())


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    return TEMPLATES.get(str(template_id or "").strip())


def list_templates(*, client_hide: bool = False,
                   business_domain: str = "") -> List[Dict[str, Any]]:
    """API/UI 消费的公开形状（含 id；不含 intents 内部池）。

    ``push_curve``（P24）随形状导出——建目标向导第二步的「AI 会怎么推进」
    节奏预览据此给每段里程碑标推进力度（不提销售/顺势/可直说）。
    ``client_hide=True``（M-5 A）→ 剔 :data:`CLIENT_HIDDEN_KINDS` 类目；
    ``business_domain="companion"``（N-3 #241）→ 剔 :data:`COMPANION_HIDDEN_KINDS`。
    """
    out: List[Dict[str, Any]] = []
    from src.companion.goals.pace import sprint_ok as _sprint_ok
    hidden = hidden_template_kinds(
        client_hide=client_hide, business_domain=business_domain)
    for tid, t in TEMPLATES.items():
        if hidden and str(t.get("kind") or "") in hidden:
            continue
        out.append({
            "id": tid,
            "name_zh": t["name_zh"],
            "name_en": t["name_en"],
            "kind": t["kind"],
            "default_days": t["default_days"],
            "params": [dict(p) for p in t["params"]],
            "milestones": [dict(m) for m in t["milestones"]],
            "push_curve": list(t.get("push_curve") or ()),
            # 限时节奏（今天收口 / 这轮聊完）白名单：旧前端缺字段则不展示档位
            "sprint_ok": _sprint_ok(tid),
        })
    return out


def milestone_label(template: Dict[str, Any], idx: int, lang: str = "zh") -> str:
    ms = template.get("milestones") or []
    i = max(0, min(int(idx), len(ms) - 1)) if ms else 0
    if not ms:
        return ""
    key = "en" if str(lang).lower().startswith("en") else "zh"
    return str(ms[i].get(key) or ms[i].get("zh") or "")


def push_for_milestone(template: Dict[str, Any], idx: int) -> str:
    curve = template.get("push_curve") or ()
    if not curve:
        return "soft"
    i = max(0, min(int(idx), len(curve) - 1))
    lvl = str(curve[i])
    return lvl if lvl in PUSH_LEVELS else "soft"


def _intent_zh(entry: Any) -> str:
    """池条目 → 中文权威文案（条目=(zh, en) 对；容忍历史纯字符串条目）。"""
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0] or "")
    return str(entry or "")


def _intent_en(entry: Any) -> str:
    """池条目 → 英文文案（无英文变体返回 ""）。"""
    if isinstance(entry, (list, tuple)) and len(entry) > 1:
        return str(entry[1] or "")
    return ""


def _format_intent(raw: str, params: Dict[str, Any], *, en: bool = False) -> str:
    """把模板参数代入意图串（只认 {item}/{note}；缺参优雅留白不抛）。

    ``en=True`` 时留白词用英文（"it"/"this goal"）；{note}/{item} 本身是运营
    手输数据，原样代入不翻译（数据不译原则）。"""
    item = str((params or {}).get("item_label")
               or (params or {}).get("item_id") or "").strip()
    note = str((params or {}).get("note") or "").strip()
    item_blank = "it" if en else "它"
    note_blank = "this goal" if en else "这个目标"
    try:
        return raw.replace("{item}", item or item_blank).replace(
            "{note}", note or note_blank)
    except Exception:
        return raw


def pick_intent(
    template: Dict[str, Any], milestone_idx: int, goal_id: str, day: str,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """确定性选今日意图：``crc32(goal_id+day)`` 定池内下标——同目标同日恒定、跨日轮换。

    返回值恒为**中文权威文案**（planner 落库 / prompt 注入的口径不变）；
    英文展示态由 :func:`intent_en_for` 反查同一池取对应英文变体。"""
    pools = template.get("intents") or {}
    keys = sorted(pools.keys())
    if not keys:
        return ""
    mi = max(0, min(int(milestone_idx), max(keys)))
    pool = pools.get(mi) or pools.get(keys[-1]) or ()
    if not pool:
        return ""
    h = zlib.crc32(f"{goal_id}:{day}".encode("utf-8", "ignore"))
    return _format_intent(_intent_zh(pool[h % len(pool)]), params or {})


def pick_sprint_intent(
    template: Dict[str, Any], beat_index: int, goal_id: str, day: str,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """限时档（today/session）按**拍序**取今日意图；无 sprint 池 → ""
    （调用方保留日历池意图，行为零回退）。

    键=第几拍（0/1/2，上越界夹末段——最后一拍的语义就是「收口」）；同拍槽
    仍按 crc32 在池内轮换（day=槽位键：session 每回合换、today 每小时换，
    同目标不同拍不复读）。返回中文权威文案，英文经 :func:`intent_en_for` 反查。"""
    pools = (template or {}).get("intents_sprint") or {}
    keys = sorted(pools.keys())
    if not keys:
        return ""
    bi = max(0, min(int(beat_index or 0), max(keys)))
    pool = pools.get(bi) or pools.get(keys[-1]) or ()
    if not pool:
        return ""
    h = zlib.crc32(f"sprint:{goal_id}:{day}".encode("utf-8", "ignore"))
    return _format_intent(_intent_zh(pool[h % len(pool)]), params or {})


def intent_en_for(
    template: Optional[Dict[str, Any]],
    params: Optional[Dict[str, Any]],
    intent_zh: str,
) -> str:
    """存量中文意图（goal_actions.intent 落库值）→ 英文对应文案。

    机制＝对模板全部意图池（含退避陪伴池 + 限时池）按同参渲染做**精确匹配**
    ——planner 写库的意图必然是某条池文案的参数化渲染（plan_beat 只有
    pick_intent / pick_care_intent 两个来源，限时档另有 pick_sprint_intent
    覆盖），所以精确匹配可靠且零歧义；匹配不到（历史参数已改 / prompt 链的
    缺口合流串 / 人工改过）返回 ""，**调用方回落中文**——宁可英文界面偶见
    中文原文，绝不给错译。{note}/{item} 是运营手输数据，英文句里原样保留
    （数据不译）。"""
    target = str(intent_zh or "").strip()
    if not target:
        return ""
    p = params or {}
    pools: List[Any] = list(((template or {}).get("intents") or {}).values())
    pools.extend(((template or {}).get("intents_sprint") or {}).values())
    pools.append(CARE_PAIRS)
    for pool in pools:
        for entry in (pool or ()):
            en_raw = _intent_en(entry)
            if not en_raw:
                continue
            if _format_intent(_intent_zh(entry), p) == target:
                return _format_intent(en_raw, p, en=True)
    return ""


def pick_care_intent(goal_id: str, day: str) -> str:
    """退避日纯陪伴意图（同样确定性轮换）。"""
    h = zlib.crc32(f"care:{goal_id}:{day}".encode("utf-8", "ignore"))
    return CARE_INTENTS[h % len(CARE_INTENTS)]


def milestone_count(template: Dict[str, Any]) -> int:
    """模板里程碑数（缺省 4——存量模板全是 4 段弧线）。"""
    ms = template.get("milestones") or ()
    return len(ms) if ms else 4


def scaled_phase_days(template: Dict[str, Any], total_days: float) -> List[float]:
    """把模板 ``phase_days``（按 default_days 标定）线性缩放到目标实际总天数。

    模板没声明 phase_days → []（纯信号驱动，零行为变更）。
    例：phase_days=(2,4,7,9,10)、default_days=10、实际 20 天 → (4,8,14,18,20)。
    """
    pd = template.get("phase_days") or ()
    if not pd:
        return []
    try:
        default_days = float(template.get("default_days") or 0) or float(pd[-1])
        total = float(total_days) if float(total_days) > 0 else default_days
        scale = total / default_days if default_days > 0 else 1.0
        return [float(d) * scale for d in pd]
    except (TypeError, ValueError):
        return []


def phase_floor(phase_days: List[float], elapsed_days: float) -> int:
    """时间兑底：某里程碑的天窗已过 → 至少推进到下一段。

    最后一段的边界是 deadline（过了归过期判定管），不参与兑底。
    例（2,4,7,9,10）：第 2.5 天 → 1（该摸底了）；第 7.5 天 → 3（该报价了）。
    """
    if not phase_days:
        return 0
    floor = 0
    try:
        e = float(elapsed_days)
    except (TypeError, ValueError):
        return 0
    for i, edge in enumerate(phase_days[:-1]):
        if e > float(edge):
            floor = i + 1
    return floor


def phase_cap(phase_days: List[float], elapsed_days: float, lookahead: int = 1) -> int:
    """超前封顶：信号再热也只允许比当前天窗超前 ``lookahead`` 段。

    防「第 1 天就开价」——获客节奏是弧线不是开关；对方当轮明确要买时由
    回复层直接应答（目标块是方向盘不是闸门），这里只约束**主动推进**的节奏。
    """
    if not phase_days:
        return 10**6
    try:
        e = float(elapsed_days)
    except (TypeError, ValueError):
        return 10**6
    cur = len(phase_days) - 1
    for i, edge in enumerate(phase_days):
        if e <= float(edge):
            cur = i
            break
    return min(cur + max(0, int(lookahead)), len(phase_days) - 1)


__all__ = [
    "AUTONOMY_LEVELS",
    "CARE_INTENTS",
    "CARE_PAIRS",
    "CLIENT_HIDDEN_KINDS",
    "COMPANION_HIDDEN_KINDS",
    "GOAL_STATUSES",
    "PUSH_LEVELS",
    "STAGE_ORDER",
    "TEMPLATES",
    "client_hidden_template_ids",
    "get_template",
    "hidden_template_kinds",
    "intent_en_for",
    "is_client_hidden_template",
    "is_hidden_template",
    "list_templates",
    "milestone_count",
    "milestone_label",
    "phase_cap",
    "phase_floor",
    "pick_care_intent",
    "pick_intent",
    "pick_sprint_intent",
    "push_for_milestone",
    "scaled_phase_days",
    "template_ids",
]
