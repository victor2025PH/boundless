"""「自动回复设置」页的纯函数核心（P0，2026-08-02）。

背景：全自动回复的关键旋钮（全局档位 / 新会话 bootstrap / 拟人打字延迟 /
已读+打字气泡 / 语音回复 / L3 缓冲话术）此前散落在 config overlay 手改 +
收件箱逐会话下拉 + 能力看板三处，运营改「回复速度」只能 SSH 编 YAML。
本模块为集中设置页提供**白名单校验 + 有效值快照 + 热生效标注**，路由层只负责
读 config / 写 overlay（``config_manager.save_overlay_patch``）/ 审计。

三个刻意的设计约束：
1. **白名单即全部**：只接受 ``FIELDS`` 里登记的点分 config 路径，未知键一律拒
   （防任意键注入 overlay，与 capability_toggle 同哲学）。
2. **热生效诚实标注**：部分键是启动时固化的——写 overlay 后行为**不会**立刻变。
   每个字段带 ``hot``：
   - True   = 消费方调用时实时读 config（写完即生效；hot-reload 就地 deep-merge
              同一 dict，持有根引用的读点天然看到新值）。P1 起 ``min_text_len``
              也属此类（make_auto_draft_cb 改为活读 app_config）；
   - False  = 启动时固化，重启后生效（UI 必须如实展示，绝不谎报「已生效」）；
   - "worker" = 默认固化，但路由可对活体 AutosendWorker 热更——deliver_delay 族
              走 ``apply_deliver_delay``、已读/打字开关走 ``apply_humanize_flags``。
              成功=即时生效，失败=按重启口径（split_hot_pending 按「实际应用成功
              的路径集合」逐键判定，两类热更独立成败互不牵连）。
3. **危险闸门不在这**：``l2_autosend.enabled`` / ``deliver``（AI 真发主开关）
   走能力看板的 critical 双重 opt-in 护栏（capability_toggle），本页只读展示 +
   深链过去，不提供第二个写入口（防绕过护栏 + 防两处口径分叉）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

AUTOMATION_MODES = ("manual", "review", "multi_choice", "auto_ai")
VOICE_TRIGGERS = ("never", "always", "when_peer_voice", "smart")
# O-1 D 「拟人程度」三档 + custom（旧模型）。刻意本地定义（本模块零依赖纯函数）；
# 与 humanize.PACING_PROFILE_NAMES 一致性由门禁钉住（test_pacing_profiles）。
PACING_PROFILE_CHOICES = ("custom", "natural", "fast", "slow")
# 内容与风格全局默认（P0-style，2026-08-02）：空串＝不干预（跟随人设/旧行为）。
# 消费点在 persona_manager（活读 config，人设显式值永远优先——见
# resolve_reply_defaults 的 precedence 说明），写完即生效故 hot=True。
REPLY_LENGTH_CHOICES = ("", "concise", "moderate", "detailed")
EMOJI_LEVEL_CHOICES = ("", "none", "minimal", "moderate", "rich")
TONE_HINT_MAXLEN = 120
# 上下文/记忆深度四档；与 context_depth.TIER_KEYS 一致性由门禁钉住
# （test_context_depth::test_settings_choices_synced）。全局档给云端主链；
# 无限制/本机会话发送时按端点窗口封顶。
CONTEXT_DEPTH_CHOICES = ("standard", "deep", "max", "ultra")
# 用量模式（B9，2026-09-11）：与深度四档并列、不混成第五档。full = 不压帽；
# economy = usage_economy overlay（历史 4 / compact 人设 / 少抽）。钱包用尽时
# 即使选 full 也会自动压（ai.economy_on_degrade 默认开）。
USAGE_MODE_CHOICES = ("full", "economy")

# 平台专家覆写（P1，2026-08-03）：键域=编排器 worker 的平台集合。
# 刻意本地定义而非 import platform_capabilities（本模块保持零依赖纯函数）；
# 与 WORKERS 注册表的一致性由门禁钉住（test_platforms_match_worker_registry）。
# 2026-08-19：zalo/instagram 个人号 worker 进能力矩阵 → 键域同步扩（两平台的
# 节奏/档位/班表覆写自此可配；拟人开关受 validate_platform_flags_caps 的能力
# 校验约束——两平台无 mark_read/typing，开了会被如实提示不支持）。
# 2026-09-07：qq（协议登录个人号，Milky）进能力矩阵 → 键域同步扩（typing 是协议层硬限制，
# 开了会被 validate_platform_flags_caps 如实提示不支持）。
PLATFORMS = ("telegram", "whatsapp", "line", "messenger", "zalo", "instagram", "qq")
# 平台拟人开关覆写的可编辑键（platform_humanize 条目白名单）
HUMANIZE_FLAG_KEYS = ("mark_read", "typing")

# 分条「条间节奏」出厂值镜像（P0-bub 2026-08-14 / 同日 5–10s 换挡）。刻意本地
# 定义而非 import reply_split（本模块保持零依赖纯函数，同 PLATFORMS 手法）；与
# reply_split 的「时代缺省」常量一致性由门禁钉住
# （test_reply_settings::test_bubble_defaults_synced_with_parse_cfg）。
# 换挡语义见 reply_split 常量块：典型条间隔=思考 uniform(3,8)×0.55 + 真人手速
# 打字分量，自然落 5–10s；长泡触 12s 封顶；40s 总预算防手动链前端超时。
BUBBLE_GAP_LO_DEFAULT = 3.0
BUBBLE_GAP_HI_DEFAULT = 8.0
BUBBLE_CJK_PER_CHAR_DEFAULT = 0.22
BUBBLE_LATIN_PER_CHAR_DEFAULT = 0.15
BUBBLE_MAX_GAP_DEFAULT = 12.0
BUBBLE_TOTAL_BUDGET_DEFAULT = 40.0
# 拆条形态 1.0.75 收紧（#210 / D-L3）：仅显式换行才拆 + 短回复门 80（加权）。
# 与 reply_split.DEFAULT_EXPLICIT_NEWLINE_ONLY / DEFAULT_MIN_TOTAL_CHARS 同值。
BUBBLE_EXPLICIT_NEWLINE_ONLY_DEFAULT = True
BUBBLE_MIN_TOTAL_CHARS_DEFAULT = 80

# path -> {type, hot, default, (choices | lo/hi)}
# default 与消费方代码内缺省一致（config 缺键时快照展示的值必须是真实行为）。
FIELDS: Dict[str, Dict[str, Any]] = {
    "inbox.auto_draft.automation_mode": {
        "type": "enum", "choices": AUTOMATION_MODES,
        "default": "auto_ai", "hot": True,
    },
    # 缺键时的真实缺省是「全局档位==auto_ai 则开」（automation_mode.py），
    # 快照里用哨兵 None 表示「跟随档位」，由 effective_values 二次求值。
    "inbox.auto_draft.bootstrap_automation_mode": {
        "type": "bool", "default": None, "hot": True,
    },
    "inbox.auto_draft.min_text_len": {
        "type": "int", "lo": 0, "hi": 500, "default": 0, "hot": True,
    },
    "inbox.l2_autosend.deliver_delay.min_sec": {
        "type": "number", "lo": 0, "hi": 600, "default": 0, "hot": "worker",
    },
    "inbox.l2_autosend.deliver_delay.max_sec": {
        "type": "number", "lo": 0, "hi": 600, "default": 0, "hot": "worker",
    },
    "inbox.l2_autosend.deliver_delay.adaptive": {
        "type": "bool", "default": False, "hot": "worker",
    },
    # P1（2026-08-12）连发/秒回两道地板（修 198 实录「第 2/3 条秒回」）：
    # min_gap_sec = 同会话两条出站间最小间隔（B 线队列抵扣归零的正交兜底）；
    # min_residual_sec = adaptive 抵扣后至少保留的残余延迟（生成耗时 ≥ 目标时
    # 不再秒回，打字气泡有露出窗口）。均默认 0=关（旧行为）；同住 deliver_delay
    # 块 → 随 merged_delay_block/apply_deliver_delay 热更，跟随滑杆的 A 线/
    # 协议链整块继承。
    "inbox.l2_autosend.deliver_delay.min_gap_sec": {
        "type": "number", "lo": 0, "hi": 120, "default": 0, "hot": "worker",
    },
    "inbox.l2_autosend.deliver_delay.min_residual_sec": {
        "type": "number", "lo": 0, "hi": 60, "default": 0, "hot": "worker",
    },
    # O-1 D（#253 #254 · D-O4，2026-09-08）「拟人程度」三档：natural（自然）/ fast（偏快）/
    # slow（偏慢）→ humanize.PACING_PROFILES 的读 / 想 / 打字分量模型（读 3–6s + 每 10 词 +1s、
    # 想 3–8s、打字 35–45 wpm / 60–90 字/分 ±30%、发出前 1s 停 composing），一键改全部节奏
    # 参数；custom＝沿用 min/max/adaptive 旧模型（存量默认，行为零变化）。同住 deliver_delay
    # 块 → apply_deliver_delay 热更。出厂基线 natural 由 feature_registry A 类 + min 种子带
    # （O-1 D ④：clean 升级机首启 _ensure_baseline 补进 overlay；块里已显式写 profile 的一字不动）。
    # 此处 default 保持 custom：它是「配置里没写时 UI 回填什么」——与基线补齐不同层，且旧后端
    # 错位期 feat 探测为假时该行整行隐藏，不会把 custom 写回去。
    "inbox.l2_autosend.deliver_delay.profile": {
        "type": "enum", "choices": PACING_PROFILE_CHOICES,
        "default": "custom", "hot": "worker",
    },
    # 人设节奏覆写（P1）：{persona_id: {min_sec?, max_sec?, adaptive?}} 整表提交，
    # 删掉的人设真被删掉（save_overlay_patch 走 REPLACE_PATHS 整树替换）；
    # UI 不管的键（per_char_sec 等 YAML 手调项）由 merged_persona_overrides
    # 按人设保留，不被整表替换误抹。
    "inbox.l2_autosend.deliver_delay.persona_overrides": {
        "type": "delay_overrides", "default": {}, "hot": "worker",
    },
    "inbox.l2_autosend.mark_read_before_reply": {
        "type": "bool", "default": True, "hot": "worker",
    },
    "inbox.l2_autosend.typing_indicator": {
        "type": "bool", "default": True, "hot": "worker",
    },
    # O-4（#254，2026-09-08）引用回复开关：消费方 reply_quote_policy.parse_quote_cfg
    # 每次投递现读 config → hot=True；default 与 reply_quote_policy.DEFAULTS["enabled"]
    # 同值（门禁钉住，test_reply_quote_policy::test_settings_switch_whitelisted）。
    # 少 / 中 / 多三档二期再做，一期只有开关（中档参数在代码缺省）。
    "inbox.l2_autosend.quote_reply.enabled": {
        "type": "bool", "default": True, "hot": True,
    },
    "inbox.l2_autosend.voice.enabled": {
        "type": "bool", "default": False, "hot": True,
    },
    "inbox.l2_autosend.voice.trigger": {
        "type": "enum", "choices": VOICE_TRIGGERS,
        "default": "when_peer_voice", "hot": True,
    },
    "inbox.l2_autosend.holding.enabled": {
        "type": "bool", "default": False, "hot": True,
    },
    # ── 内容与风格全局默认（表达层兜底；人设显式设置永远优先）──────────
    "ai.reply_defaults.length": {
        "type": "enum", "choices": REPLY_LENGTH_CHOICES,
        "default": "", "hot": True,
    },
    "ai.reply_defaults.max_sentences": {
        "type": "int", "lo": 0, "hi": 10, "default": 0, "hot": True,
    },
    "ai.reply_defaults.emoji_level": {
        "type": "enum", "choices": EMOJI_LEVEL_CHOICES,
        "default": "", "hot": True,
    },
    "ai.reply_defaults.tone_hint": {
        "type": "text", "maxlen": TONE_HINT_MAXLEN, "default": "", "hot": True,
    },
    # 上下文/记忆深度四档（云端 12k/32k/128k/900k）。无限制会话另外按端点封顶。
    # 单一事实源 src/ai/context_depth.py。standard = 零行为变化；深档只抬地板。hot=True。
    "ai.context_depth": {
        "type": "enum", "choices": CONTEXT_DEPTH_CHOICES,
        "default": "standard", "hot": True,
    },
    "ai.usage_mode": {
        "type": "enum", "choices": USAGE_MODE_CHOICES,
        "default": "full", "hot": True,
    },
    # ── 平台专家覆写（P1，2026-08-03；缺省全空表=继承全局，零行为变更）────
    # 档位封顶：{platform: mode}——只降不升（cap_automation_mode），删除=不封顶。
    # 消费点 autodraft 回调**活读** app_config（与 min_text_len 同款）→ hot=True。
    "inbox.auto_draft.platform_modes": {
        "type": "platform_enum_map", "choices": AUTOMATION_MODES,
        "default": {}, "hot": True,
    },
    # 节奏平台覆写：{platform: {min_sec?, max_sec?, adaptive?}}；
    # 优先级 人设 > 平台 > 全局（humanize._apply_scoped_overrides，键级合并）。
    # 随 deliver_delay 整块走 apply_deliver_delay 热更 → hot="worker"。
    "inbox.l2_autosend.deliver_delay.platform_overrides": {
        "type": "delay_overrides", "keys": PLATFORMS, "default": {},
        "hot": "worker",
    },
    # 拟人链平台开关：{platform: {mark_read?, typing?}}；显式值覆盖全局开关
    # （全局关也可单平台开），缺省=跟随全局。热更走 apply_platform_humanize。
    "inbox.l2_autosend.platform_humanize": {
        "type": "platform_flags", "default": {}, "hot": "worker",
    },
    # 语音触发平台覆写：{platform: trigger}；消费点 autosend_voice 每次投递
    # 活读 config（effective_voice_block）→ hot=True。
    "inbox.l2_autosend.voice.platform_triggers": {
        "type": "platform_enum_map", "choices": VOICE_TRIGGERS,
        "default": {}, "hot": True,
    },
    # ── 工作时间班表（P0-ws，2026-08-04；判定核心 work_hours_gate.py）────
    # 消费点（A 线直发 / B 线 autosend / 协议号 / auto_draft 节流 / watchdog）
    # 全部每次活读 config 根 → 全部 hot=True。缺省与 work_hours_gate 的
    # fail-open 语义一致：start/end 空 = 未配置 = 不闸。
    "inbox.work_schedule.enabled": {
        "type": "bool", "default": False, "hot": True,
    },
    "inbox.work_schedule.timezone": {
        "type": "tzname", "default": "", "hot": True,
    },
    "inbox.work_schedule.default.workdays": {
        "type": "workdays", "default": [], "hot": True,
    },
    "inbox.work_schedule.default.start": {
        "type": "hhmm", "default": "", "hot": True,
    },
    "inbox.work_schedule.default.end": {
        "type": "hhmm", "default": "", "hot": True,
    },
    "inbox.work_schedule.edge_jitter_min": {
        "type": "int", "lo": 0, "hi": 180, "default": 20, "hot": True,
    },
    "inbox.work_schedule.crisis_bypass": {
        "type": "bool", "default": True, "hot": True,
    },
    "inbox.work_schedule.off_hours.generate_drafts": {
        "type": "bool", "default": True, "hot": True,
    },
    "inbox.work_schedule.off_hours.catch_up": {
        "type": "bool", "default": True, "hot": True,
    },
    # Q-4（#267）：默认 0 = 过夜积压全部作废重写；>0 = 只重拟稿龄超 N 小时的
    "inbox.work_schedule.off_hours.catch_up_regenerate_hours": {
        "type": "number", "lo": 0, "hi": 48, "default": 0, "hot": True,
    },
    # 账号班表覆写：{"platform:account_id": {enabled?/workdays?/start?/end?/
    # timezone?}} 整表提交（缺席=删除，走 REPLACE_PATHS 整树替换）。
    "inbox.work_schedule.accounts": {
        "type": "schedule_overrides", "default": {}, "hot": True,
    },
    # ── 回复额度守卫（P0-guard，2026-08-12）────────────────────────────
    # 消费方 peer_bot_guard.parse_cfg 逐调用现读 config → 写 overlay 即生效
    # （~30s 热重载节流）→ 两键均 hot=True。default 与 parse_cfg._DEFAULTS
    # 同值（有门禁钉住）。
    "inbox.peer_bot_guard.enabled": {
        "type": "bool", "default": False, "hot": True,
    },
    # 2026-08-17：默认 40 → 500（与 parse_cfg._DEFAULTS 同步改，门禁钉住）。
    # 2026-08-24 老板拍板（B34 落定，内测实录 600 被旧 [5,500] 白名单拒）：
    # 每日额度**不设业务上限**——range 放开为 [0, 1_000_000]：
    # - 0＝不限额，与 YAML/parse_cfg/budget_flags 全链同一语义（budget>0 才
    #   检查，0 时触顶判定与收件箱横幅整体熄灭）。旧版怕「0 被当成全拦」而
    #   把 0 挡在 UI 外，现改为在页面提示里写明语义，不再靠禁止输入防误解；
    # - 上限 1_000_000 是纯技术帽（防误粘贴超长数字/int 溢出面），单会话
    #   一天百万轮物理不可达，业务语义等于不设上限。
    # 守卫的保险丝定位不变（防双 bot 互刷 + 单会话过密风控），默认值仍 500。
    "inbox.peer_bot_guard.daily_reply_budget": {
        "type": "int", "lo": 0, "hi": 1_000_000, "default": 500, "hot": True,
    },
    # ── 守卫高级参数（P1，2026-08-12；设置页折叠区，默认只露开关+额度）────
    # 均 hot=True（parse_cfg 逐调用现读）。clamp 语义逐键说明：
    # - repeat_streak_n 下限 2：n=1 时任何非空入站 streak≥1 → 全量降 review
    #   （必炸的脚枪，parse_cfg 只 clamp ≥0 拦不住）；关闭该刹车走 YAML 置 0。
    # - instant_reply_sec 下限 0.5：0=关窗（YAML 语义），UI 只调窗宽。
    # - instant_repeat_n 下限 1：n=1 已要求「秒回且复读」双条件，激进但合法。
    # - suspect_threshold **允许 0**＝只算分不拦截（观察模式，evaluate 的
    #   thr>0 闸），decimals=2 保 0.05 粒度（旧 1 位小数会把 0.65 磨成 0.6/0.7）。
    "inbox.peer_bot_guard.repeat_streak_n": {
        "type": "int", "lo": 2, "hi": 10, "default": 3, "hot": True,
    },
    "inbox.peer_bot_guard.instant_reply_sec": {
        "type": "number", "lo": 0.5, "hi": 30, "default": 3.0, "hot": True,
    },
    "inbox.peer_bot_guard.instant_repeat_n": {
        "type": "int", "lo": 1, "hi": 10, "default": 2, "hot": True,
    },
    "inbox.peer_bot_guard.heuristics": {
        "type": "bool", "default": True, "hot": True,
    },
    "inbox.peer_bot_guard.suspect_threshold": {
        "type": "number", "lo": 0, "hi": 1, "default": 0.6, "hot": True,
        "decimals": 2,
    },
    "inbox.peer_bot_guard.proactive_filter": {
        "type": "bool", "default": True, "hot": True,
    },
    # ── 消息分条「条间节奏」（P0-bub，2026-08-14）─────────────────────────
    # 修 173 实录「第一条守延迟、第 2/3 条机关枪」：那不是 deliver_delay 失效
    # ——deliver_delay 管「第一条什么时候发」，同一条回复被拆成多条 bubble 后，
    # **条与条之间**走 inbox.reply_style.bubbles.* 的独立节奏模型
    # （reply_split.inter_part_delay_sec = 思考抖动×0.55 + 下一条长度×手速，
    # 封顶 max_gap_sec），此前只能 SSH 改 YAML。消费方三条链
    # （A 线 telegram_client / B 线 autosend_helpers / 手动 send 路由）逐次投递
    # 现读 config → 全部 hot=True。default 必须与 parse_bubbles_cfg 缺省一致
    # （门禁钉住）。enabled 不是「AI 真发」级危险闸门（只改呈现形态不改内容），
    # 收进本页与 typing_indicator 同级。
    "inbox.reply_style.bubbles.enabled": {
        "type": "bool", "default": False, "hot": True,
    },
    "inbox.reply_style.bubbles.per_sentence": {
        "type": "bool", "default": False, "hot": True,
    },
    # ── 1.0.75 收紧（#210 / D-L3，2026-09-06）：拆与不拆只由草稿显式换行决定，
    # 算法切句（逐句/句界打包）降为显式关掉本键才进的兜底档；短回复门抬到 80
    # （加权：CJK 80 字 / 英文 ≈320 字符），两句英文永远整条。消费方三链逐次
    # 投递现读 parse_bubbles_cfg → hot=True。default 与 reply_split 同值（门禁钉住）。
    "inbox.reply_style.bubbles.explicit_newline_only": {
        "type": "bool", "default": BUBBLE_EXPLICIT_NEWLINE_ONLY_DEFAULT,
        "hot": True,
    },
    "inbox.reply_style.bubbles.min_total_chars": {
        "type": "int", "lo": 0, "hi": 500,
        "default": BUBBLE_MIN_TOTAL_CHARS_DEFAULT, "hot": True,
    },
    # 真实缺省是动态的（per_sentence 开且未显式配 → 5，否则 3）——快照用哨兵
    # None 表示「跟随模式」，effective_values 二次求值（bootstrap 哨兵同手法）。
    # 刻意**不**进平台覆写表（OVERRIDE_EDITABLE_KEYS 只有延迟三键）：同一人设在
    # TG/WA 的消息形态必须一致，平台层只许调延迟不许改条数（#210 跨平台识破）。
    "inbox.reply_style.bubbles.max_parts": {
        "type": "int", "lo": 1, "hi": 5, "default": None, "hot": True,
    },
    "inbox.reply_style.bubbles.gap_sec_lo": {
        "type": "number", "lo": 0, "hi": 30,
        "default": BUBBLE_GAP_LO_DEFAULT, "hot": True,
    },
    "inbox.reply_style.bubbles.gap_sec_hi": {
        "type": "number", "lo": 0, "hi": 60,
        "default": BUBBLE_GAP_HI_DEFAULT, "hot": True,
    },
    # 手速二键 decimals=3：0.22/0.15 这类步进 1 位小数会磨没；hi 与
    # parse_bubbles_cfg 的 clamp 同（per_char 无上限 clamp 但 >1s/字已属荒诞，
    # UI 收 [0,1]；latin clamp 2.0 同源）。
    "inbox.reply_style.bubbles.per_char_sec": {
        "type": "number", "lo": 0, "hi": 1,
        "default": BUBBLE_CJK_PER_CHAR_DEFAULT, "hot": True, "decimals": 3,
    },
    "inbox.reply_style.bubbles.latin_per_char_sec": {
        "type": "number", "lo": 0, "hi": 2,
        "default": BUBBLE_LATIN_PER_CHAR_DEFAULT, "hot": True, "decimals": 3,
    },
    "inbox.reply_style.bubbles.max_gap_sec": {
        "type": "number", "lo": 1, "hi": 60,
        "default": BUBBLE_MAX_GAP_DEFAULT, "hot": True,
    },
    # 0=不设总预算；默认 40＝手动链前端 60s abort 的防超时软顶（见 reply_split）
    "inbox.reply_style.bubbles.total_budget_sec": {
        "type": "number", "lo": 0, "hi": 300,
        "default": BUBBLE_TOTAL_BUDGET_DEFAULT, "hot": True,
    },
    # ── 账号发送额度 / 防轰炸闸门（companion_send_gate，2026-08-29）──────
    # 老板指令（同日群实录沉淀）：对外支持不得再教用户改配置文件——这组键
    # 此前只能 YAML 手改（设置页缺位正是那条「方法二」群回复的土壤），全部
    # 收进白名单。消费方 send_guard.send_blocked → companion_send_gate.
    # evaluate 逐次发送现读 config → 写 overlay 即生效，全部 hot=True。
    # default 与 gate_decision/evaluate 的代码缺省一致（门禁按签名钉住）；
    # 安装包种子（config.desktop.min.yaml）显式带 300/100/3，不受此影响。
    "companion_send_gate.enabled": {
        "type": "bool", "default": False, "hot": True,
    },
    # target_cap/warmup_start_cap 下限 1：闸门语义里 0＝每天 0 条＝全停
    # （sends_today >= cap 即拦），与 peer_bot_guard.daily_reply_budget 的
    # 「0=不限」**相反**——全停走 Kill-Switch，不给这个脚枪留 UI 入口；
    # 上限沿用 1_000_000 纯技术帽（业务上不封顶，B34 同哲学）。
    "companion_send_gate.target_cap": {
        "type": "int", "lo": 1, "hi": 1_000_000, "default": 15, "hot": True,
    },
    "companion_send_gate.warmup_start_cap": {
        "type": "int", "lo": 1, "hi": 1_000_000, "default": 2, "hot": True,
    },
    "companion_send_gate.warmup_ramp_days": {
        "type": "int", "lo": 0, "hi": 365, "default": 14, "hot": True,
    },
    "companion_send_gate.block_on_red": {
        "type": "bool", "default": True, "hot": True,
    },
    "companion_send_gate.reserve_for_manual": {
        "type": "int", "lo": 0, "hi": 100_000, "default": 0, "hot": True,
    },
    # 限额白名单（exempt_peers 子串匹配 chat_key）：收件箱横幅「白名单此
    # 客户」按钮追加的就是这张表——这里是全表管理口（含删除；列表在
    # overlay 合并里整体替换，无需 REPLACE_PATHS）。
    "companion_send_gate.exempt_peers": {
        "type": "str_list", "default": [], "hot": True,
        "item_maxlen": 64, "max_items": 500,
    },
}

_DELAY_PREFIX = "inbox.l2_autosend.deliver_delay."
_BUB_PREFIX = "inbox.reply_style.bubbles."
_BUB_LO_PATH = _BUB_PREFIX + "gap_sec_lo"
_BUB_HI_PATH = _BUB_PREFIX + "gap_sec_hi"
_BUB_MAXPARTS_PATH = _BUB_PREFIX + "max_parts"
_BUB_PER_SENT_PATH = _BUB_PREFIX + "per_sentence"
_OVERRIDES_PATH = _DELAY_PREFIX + "persona_overrides"
_PLAT_OVERRIDES_PATH = _DELAY_PREFIX + "platform_overrides"
_PLAT_HUMANIZE_PATH = "inbox.l2_autosend.platform_humanize"
_PLAT_MODES_PATH = "inbox.auto_draft.platform_modes"
_PLAT_VOICE_PATH = "inbox.l2_autosend.voice.platform_triggers"

# ── 场景化预设档（P1，2026-08-02）────────────────────────────────
# 每档＝一组白名单键的组合写入（不引入新 config 键）。刻意**不碰**：
# voice.enabled / holding（能力决策）、tone_hint / emoji / max_sentences
# （运营个性化，预设不清洗）、危险闸门（能力看板辖区）。
# 键集必须 ⊆ FIELDS（有门禁钉住）；匹配判定＝全键相等（match_preset）。
PRESETS: Dict[str, Dict[str, Any]] = {
    # O-1 D（D-O4）：三档即「拟人程度」——cautious=偏慢（slow）/ natural=自然（natural）/
    # rapid=偏快（custom 旧模型近即回，客服效率场景）。profile 在场时 humanize 走读 / 想 /
    # 打字分量模型，min/max/adaptive 只作 custom 回落值保留。
    # 谨慎养号：新号/防风控——慢节奏 + 全拟人 + 简短克制 + 语音只跟随
    # （P0-bub 起预设连带条间思考抖动：分条关着也只是躺着的值，开闸即同档）
    "cautious": {
        "inbox.l2_autosend.deliver_delay.profile": "slow",
        "inbox.l2_autosend.deliver_delay.min_sec": 8,
        "inbox.l2_autosend.deliver_delay.max_sec": 20,
        "inbox.l2_autosend.deliver_delay.adaptive": True,
        "inbox.l2_autosend.mark_read_before_reply": True,
        "inbox.l2_autosend.typing_indicator": True,
        "inbox.l2_autosend.voice.trigger": "when_peer_voice",
        "ai.reply_defaults.length": "concise",
        "inbox.reply_style.bubbles.gap_sec_lo": 5.0,
        "inbox.reply_style.bubbles.gap_sec_hi": 12.0,
        "inbox.reply_style.bubbles.max_gap_sec": 18,
    },
    # 自然真人（推荐）：日常运营平衡档
    "natural": {
        "inbox.l2_autosend.deliver_delay.profile": "natural",
        "inbox.l2_autosend.deliver_delay.min_sec": 3,
        "inbox.l2_autosend.deliver_delay.max_sec": 12,
        "inbox.l2_autosend.deliver_delay.adaptive": True,
        "inbox.l2_autosend.mark_read_before_reply": True,
        "inbox.l2_autosend.typing_indicator": True,
        "inbox.l2_autosend.voice.trigger": "when_peer_voice",
        "ai.reply_defaults.length": "moderate",
        # ＝出厂缺省档（5–10s 量级换挡后 natural 就是缺省本身）
        "inbox.reply_style.bubbles.gap_sec_lo": 3.0,
        "inbox.reply_style.bubbles.gap_sec_hi": 8.0,
        "inbox.reply_style.bubbles.max_gap_sec": 12,
    },
    # 快速响应：客服/效率场景——几乎即回 + 简短 + 不发语音（偏快 = fast 档：读 1.5–3.5s、
    # 想 1–4s、50–65 wpm——仍是人的手速，不是秒回；真要秒回走专家区 custom + 0–3s）
    "rapid": {
        "inbox.l2_autosend.deliver_delay.profile": "fast",
        "inbox.l2_autosend.deliver_delay.min_sec": 0,
        "inbox.l2_autosend.deliver_delay.max_sec": 3,
        "inbox.l2_autosend.deliver_delay.adaptive": False,
        "inbox.l2_autosend.mark_read_before_reply": True,
        "inbox.l2_autosend.typing_indicator": True,
        "inbox.l2_autosend.voice.trigger": "never",
        "ai.reply_defaults.length": "concise",
        # ＝旧时代出厂值（换挡前的 0.8/2.5/6）：要回「秒回条间」按这档
        "inbox.reply_style.bubbles.gap_sec_lo": 0.8,
        "inbox.reply_style.bubbles.gap_sec_hi": 2.5,
        "inbox.reply_style.bubbles.max_gap_sec": 6,
    },
}


def match_preset(values: Mapping[str, Any]) -> str:
    """当前有效值命中哪个预设档（全键相等才算；都不中返回 ""＝自定义）。

    数值用 float 比（8 == 8.0），其余用 == ——与 sanitize 归一后的类型口径一致。
    """
    def _eq(a: Any, b: Any) -> bool:
        if isinstance(a, bool) or isinstance(b, bool):
            return bool(a) is bool(b)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return float(a) == float(b)
        return a == b

    for pid, kv in PRESETS.items():
        if all(_eq(values.get(k), v) for k, v in kv.items()):
            return pid
    return ""


# 溯源来源标签（explain_pacing 用；与前端 chips 文案键一一对应）
_SRC_PERSONA = "persona"
_SRC_PLATFORM = "platform"
_SRC_GLOBAL = "global"
_SRC_DEFAULT = "default"


def explain_pacing(
    config: Any, persona_id: str = "", platform: str = "",
) -> Dict[str, Any]:
    """逐键回答「这次投递的节奏参数来自哪一层」：人设 → 平台 → 全局 → 代码缺省。

    merge 语义与 ``humanize._apply_scoped_overrides`` 完全一致（键级覆盖、
    人设 > 平台）；来源按**键级**标注——人设只给 max_sec、平台只给 min_sec 时，
    两键各标各的来源，不搞整块连坐。``platform`` 缺省 = 旧两层行为。
    """
    block = _dig(config, "inbox.l2_autosend.deliver_delay", {}) or {}
    overrides = block.get("persona_overrides") if isinstance(block, Mapping) else {}
    entry: Mapping[str, Any] = {}
    if persona_id and isinstance(overrides, Mapping):
        cand = overrides.get(persona_id)
        entry = cand if isinstance(cand, Mapping) else {}
    plat_ovs = block.get("platform_overrides") if isinstance(block, Mapping) else {}
    p_entry: Mapping[str, Any] = {}
    if platform and isinstance(plat_ovs, Mapping):
        cand = plat_ovs.get(str(platform).lower())
        p_entry = cand if isinstance(cand, Mapping) else {}

    def _pick(key: str, default: Any) -> Dict[str, Any]:
        if key in entry and entry.get(key) is not None:
            return {"value": entry.get(key), "source": _SRC_PERSONA}
        if key in p_entry and p_entry.get(key) is not None:
            return {"value": p_entry.get(key), "source": _SRC_PLATFORM}
        if isinstance(block, Mapping) and key in block and block.get(key) is not None:
            return {"value": block.get(key), "source": _SRC_GLOBAL}
        return {"value": default, "source": _SRC_DEFAULT}

    return {
        "persona_id": str(persona_id or ""),
        "platform": str(platform or "").lower(),
        "min_sec": _pick("min_sec", 0),
        "max_sec": _pick("max_sec", 0),
        "adaptive": _pick("adaptive", False),
    }


def _pace_of_block(block: Any) -> Dict[str, Any]:
    """一个延迟块的可读快照：min/max/adaptive + paced（max>0 且区间合法）。"""
    b = block if isinstance(block, Mapping) else {}
    try:
        mn = float(b.get("min_sec", 0) or 0)
        mx = float(b.get("max_sec", 0) or 0)
    except (TypeError, ValueError):
        mn, mx = 0.0, 0.0
    return {
        "min_sec": mn,
        "max_sec": mx,
        "adaptive": bool(b.get("adaptive", False)),
        "paced": mx > 0 and mx >= mn,
    }


def chain_pacing_coverage(config: Any) -> Dict[str, Any]:
    """三条自动回复发送链的「节奏生效自检」快照（纯函数，2026-08-07）。

    修的洞：设置页滑杆只写 ``inbox.l2_autosend.deliver_delay``，而系统里有
    **三条**互相独立的自动回复发送链——A 线（本机主号原生 TG）、B 线（收件箱
    草稿全自动投递）、协议 7×24 快链（ChatX 独立包默认档 deliver=false 时的
    实际发送者）。协议链此前读独立键 ``protocol_autoreply.delay``（从未有人
    配置 → 恒 0s 秒回）且零观测——「滑杆拖了没生效」在页面上完全隐形。
    本函数把三条链各自的 生效延迟来源/值/是否有节奏 摆到同一张快照里，
    UI 对「active 且 !paced」的链亮红。

    follow 语义与运行时**同构**（有契约测试对拍，勿单边改）：
    - A 线 = ``sender.run_prereply_humanize``：thinking_delay 配了值优先，
      否则（follow≠false）跟随滑杆键；
    - 协议链 = ``protocol_autoreply.resolve_send_pacing_block``：同构；
    - B 线 = 滑杆键本体。
    active 判定为**全局静态口径**（账号级 autoreply_override / 会话档位覆写
    不在本快照；它们只会把显式值改得更显式，不会把「跟随」变成隐性秒回）：
    - native_tg：telegram.api_id 非空（主号客户端会启动）；
    - autosend：l2 enabled（缺省 true）且 deliver=true（真投递）；
    - protocol：protocol_autoreply.enabled 且 deliver≠true（deliver=true 时
      auto_ai 会话让位 B 线、其余档位人审闸静音 → 本链不自动外发）。
    """
    from src.inbox.humanize import resolve_following_delay_block
    dd = _dig(config, "inbox.l2_autosend.deliver_delay", {}) or {}
    deliver_on = bool(_dig(config, "inbox.l2_autosend.deliver", False))
    l2_enabled = bool(_dig(config, "inbox.l2_autosend.enabled", True))
    slider_src = "inbox.l2_autosend.deliver_delay"

    # A 线 / 协议链的 follow 判定都走 humanize.resolve_following_delay_block
    # （与运行时同一函数——source 用 following 标注，不再各自重推 follow 规则）。
    td = _dig(config, "telegram.reply_humanize.thinking_delay", {})
    native_block, native_follow = resolve_following_delay_block(
        td if isinstance(td, Mapping) else {}, dd)
    native_src = (slider_src if native_follow
                  else "telegram.reply_humanize.thinking_delay")
    native_active = bool(str(_dig(config, "telegram.api_id", "") or "").strip())

    # 协议链走 runtime 同一 effective_protocol_pacing——含「都没配→出厂默认 8-20s」
    # 兜底（存量节点收口）+ follow:false 秒回逃生阀。banner 与 runtime 同函数，
    # 绝不说谎（延续 SSOT 铁律）。source ∈ own|slider|default|instant → 标注来源。
    pd = _dig(config, "protocol_autoreply.delay", {})
    try:
        from src.integrations.protocol_autoreply import effective_protocol_pacing
        proto_block, _psrc = effective_protocol_pacing(
            pd if isinstance(pd, Mapping) else {}, dd)
    except Exception:
        proto_block, _psrc = resolve_following_delay_block(
            pd if isinstance(pd, Mapping) else {}, dd)[0], "slider"
    proto_src = {
        "slider": slider_src,
        "own": "protocol_autoreply.delay",
        "default": "__protocol_default__",
        "instant": "protocol_autoreply.delay",
    }.get(_psrc, slider_src)
    proto_active = (bool(_dig(config, "protocol_autoreply.enabled", False))
                    and not deliver_on)

    chains = {
        "native_tg": {"active": native_active, "source": native_src,
                      **_pace_of_block(native_block)},
        "autosend": {"active": l2_enabled and deliver_on, "source": slider_src,
                     **_pace_of_block(dd)},
        "protocol": {"active": proto_active, "source": proto_src,
                     **_pace_of_block(proto_block)},
    }
    # diverged（黄档，2026-08-12）：链路有节奏（不红），但吃的是**独立键**——
    # 拖本页滑杆对它不生效。问题不在当前数值像不像滑杆，而在**绑定关系**：
    # 值恰好相同也算 diverged（下次拖滑杆就分叉），故不比区间只看来源。
    # 出厂默认（__protocol_default__）是「都没配」的兜底，没有可分叉的对象，
    # 不算 diverged（标黄会逼用户去改一个本来无害的状态）。
    # followable：能一键改为跟随滑杆的链（有独立键的两条；autosend 本体＝滑杆）。
    # 覆盖黄档（独立值）与红档 instant（follow:false 逃生阀）——红档此前只有
    # 警告没有出路，按钮就是出路。
    _own_keys = {"native_tg": "telegram.reply_humanize.thinking_delay",
                 "protocol": "protocol_autoreply.delay"}
    for k, v in chains.items():
        own = _own_keys.get(k)
        is_own = bool(own) and v["source"] == own
        v["diverged"] = bool(v["active"] and v["paced"] and is_own)
        v["followable"] = bool(v["active"] and is_own)
    unpaced = sorted(k for k, v in chains.items()
                     if v["active"] and not v["paced"])
    diverged = sorted(k for k, v in chains.items() if v["diverged"])
    return {"chains": chains, "unpaced_active": unpaced,
            "diverged_active": diverged, "ok": not unpaced}


# UI 可编辑的人设覆写键；其余键（per_char_sec / jitter 等 YAML 手调项）保留不动。
OVERRIDE_EDITABLE_KEYS = ("min_sec", "max_sec", "adaptive")
# 账号班表覆写条目的可编辑键（work_hours_gate.resolve_entry 的消费面）
SCHEDULE_EDITABLE_KEYS = ("enabled", "workdays", "start", "end", "timezone")
_WS_ACCOUNTS_PATH = "inbox.work_schedule.accounts"
_WS_START_PATH = "inbox.work_schedule.default.start"
_WS_END_PATH = "inbox.work_schedule.default.end"
# save_overlay_patch 的整树替换路径：各覆写表都是「整字典替换」语义——
# 深合并会让删掉的条目（人设/平台/账号班表）赖在 overlay 里不走。
REPLACE_PATHS = (_OVERRIDES_PATH, _PLAT_OVERRIDES_PATH,
                 _PLAT_HUMANIZE_PATH, _PLAT_MODES_PATH, _PLAT_VOICE_PATH,
                 _WS_ACCOUNTS_PATH)


def _coerce_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return None


def _coerce_number(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_hhmm(v: Any) -> Optional[str]:
    """'H:M'/'HH:MM' → 归一 'HH:MM'；空串合法（=清除/未配置）；非法 → None。"""
    s = str(v if v is not None else "").strip()
    if not s:
        return ""
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return f"{h:02d}:{m:02d}"
    return None


def _coerce_workdays(v: Any) -> Optional[List[int]]:
    """ISO 周几列表 → 去重升序；空列表合法（work_hours_gate 按「每天」解释）。"""
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        return None
    out = set()
    for item in v:
        if isinstance(item, bool):
            return None
        try:
            d = int(item)
        except (TypeError, ValueError):
            return None
        if not (1 <= d <= 7):
            return None
        out.add(d)
    return sorted(out)


def _coerce_str_list(
    v: Any, *, item_maxlen: int = 64, max_items: int = 500,
) -> Tuple[Optional[List[str]], str]:
    """字符串列表（发送闸门白名单等）→ (归一值, 错误码)；错误码空串＝合法。

    条目内空白折叠（chat_key/号码不含空白，折叠比拒绝宽容）、空条目剔除、
    去重保序；YAML 手写的数字条目（号码不加引号）容忍为 str。空列表合法
    ＝清空。非列表 / 嵌套容器 → ``bad_list``；条目超长 / 条数超帽 →
    ``too_long``（拒绝而非静默截断，与 text 同哲学）。
    """
    if v is None:
        return [], ""
    if isinstance(v, str) or not isinstance(v, (list, tuple)):
        return None, "bad_list"
    out: List[str] = []
    seen = set()
    for item in v:
        if isinstance(item, (dict, list, tuple)):
            return None, "bad_list"
        s = " ".join(str(item if item is not None else "").split())
        if not s:
            continue
        if len(s) > item_maxlen:
            return None, "too_long"
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    if len(out) > max_items:
        return None, "too_long"
    return out, ""


def _coerce_tzname(v: Any) -> Optional[str]:
    """IANA 时区名（空=跟随服务器本地钟）；非法名拒绝而非静默回落。"""
    s = str(v if v is not None else "").strip()
    if not s:
        return ""
    if len(s) > 64:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(s)
        return s
    except Exception:
        return None


def sanitize_patch(
    changes: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """校验前端提交的 {path: value}，返回 (合法项, 错误列表)。

    错误项 ``{"field": path, "code": ...}``；code ∈
    unknown_field / bad_bool / bad_enum / bad_number / out_of_range。
    合法项的值已归一（bool 真 bool、int 真 int、数字夹进 [lo,hi] 由错误挡而非静默裁剪）。
    """
    clean: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    for raw_key, raw_val in (changes or {}).items():
        key = str(raw_key)
        spec = FIELDS.get(key)
        if spec is None:
            errors.append({"field": key, "code": "unknown_field"})
            continue
        t = spec["type"]
        if t == "delay_overrides":
            ov, ov_errors = _sanitize_overrides(
                raw_val, field=key, key_domain=spec.get("keys"))
            if ov_errors:
                errors.extend(ov_errors)
            else:
                clean[key] = ov
        elif t == "platform_flags":
            ov, ov_errors = _sanitize_platform_flags(raw_val, field=key)
            if ov_errors:
                errors.extend(ov_errors)
            else:
                clean[key] = ov
        elif t == "platform_enum_map":
            ov, ov_errors = _sanitize_platform_enum_map(
                raw_val, spec["choices"], field=key)
            if ov_errors:
                errors.extend(ov_errors)
            else:
                clean[key] = ov
        elif t == "schedule_overrides":
            ov, ov_errors = _sanitize_schedule_overrides(raw_val, field=key)
            if ov_errors:
                errors.extend(ov_errors)
            else:
                clean[key] = ov
        elif t == "hhmm":
            hh = _coerce_hhmm(raw_val)
            if hh is None:
                errors.append({"field": key, "code": "bad_time"})
            else:
                clean[key] = hh
        elif t == "workdays":
            wd = _coerce_workdays(raw_val)
            if wd is None:
                errors.append({"field": key, "code": "bad_workdays"})
            else:
                clean[key] = wd
        elif t == "tzname":
            tzv = _coerce_tzname(raw_val)
            if tzv is None:
                errors.append({"field": key, "code": "bad_timezone"})
            else:
                clean[key] = tzv
        elif t == "str_list":
            lst, code = _coerce_str_list(
                raw_val,
                item_maxlen=int(spec.get("item_maxlen", 64)),
                max_items=int(spec.get("max_items", 500)))
            if code:
                errors.append({"field": key, "code": code})
            else:
                clean[key] = lst
        elif t == "bool":
            b = _coerce_bool(raw_val)
            if b is None:
                errors.append({"field": key, "code": "bad_bool"})
            else:
                clean[key] = b
        elif t == "enum":
            s = str(raw_val or "").strip().lower()
            if key == "ai.context_depth":
                if s == "深度":
                    s = "deep"
                elif s == "超大":
                    s = "ultra"
            if s not in spec["choices"]:
                errors.append({"field": key, "code": "bad_enum"})
            else:
                clean[key] = s
        elif t == "text":
            # 单行短文本：换行/连续空白折叠成单空格；超长拒绝而非静默截断
            s = " ".join(str(raw_val if raw_val is not None else "").split())
            if len(s) > int(spec.get("maxlen", 200)):
                errors.append({"field": key, "code": "too_long"})
            else:
                clean[key] = s
        else:  # int / number
            n = _coerce_number(raw_val)
            if n is None:
                errors.append({"field": key, "code": "bad_number"})
                continue
            if not (spec["lo"] <= n <= spec["hi"]):
                errors.append({"field": key, "code": "out_of_range"})
                continue
            # number 默认 1 位小数；spec 可带 decimals 提精度（如疑似阈值 0.05 档）
            clean[key] = int(n) if t == "int" else (
                int(n) if n == int(n)
                else round(n, int(spec.get("decimals", 1))))
    return clean, errors


def _sanitize_overrides(
    raw: Any, *, field: str = _OVERRIDES_PATH,
    key_domain: Optional[Tuple[str, ...]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, str]]]:
    """校验节奏覆写表 {key: {min_sec?, max_sec?, adaptive?}}（人设/平台两用）。

    只接受 ``OVERRIDE_EDITABLE_KEYS`` 白名单键（防经此口往覆写里注任意键）。
    外层键规则二选一：``key_domain=None`` → 人设 ID（非空、≤64 字符）；
    ``key_domain=PLATFORMS`` → 归一小写后必须落在平台白名单（防拼错平台名
    静默不生效）。空表 {} 合法＝清空全部覆写。
    """
    errors: List[Dict[str, str]] = []
    if not isinstance(raw, Mapping):
        return {}, [{"field": field, "code": "bad_overrides"}]
    out: Dict[str, Dict[str, Any]] = {}
    for pid_raw, entry in raw.items():
        pid = str(pid_raw or "").strip()
        if key_domain is not None:
            pid = pid.lower()
            if pid not in key_domain:
                errors.append({"field": field, "code": "bad_platform"})
                continue
        elif not pid or len(pid) > 64:
            errors.append({"field": field, "code": "bad_persona_id"})
            continue
        if not isinstance(entry, Mapping):
            errors.append({
                "field": f"{field}.{pid}", "code": "bad_overrides"})
            continue
        cleaned_entry: Dict[str, Any] = {}
        for k, v in entry.items():
            key = str(k)
            if key not in OVERRIDE_EDITABLE_KEYS:
                errors.append({
                    "field": f"{field}.{pid}.{key}",
                    "code": "bad_override_key"})
                continue
            if key == "adaptive":
                b = _coerce_bool(v)
                if b is None:
                    errors.append({
                        "field": f"{field}.{pid}.{key}",
                        "code": "bad_bool"})
                else:
                    cleaned_entry[key] = b
            else:
                n = _coerce_number(v)
                if n is None:
                    errors.append({
                        "field": f"{field}.{pid}.{key}",
                        "code": "bad_number"})
                elif not (0 <= n <= 600):
                    errors.append({
                        "field": f"{field}.{pid}.{key}",
                        "code": "out_of_range"})
                else:
                    cleaned_entry[key] = int(n) if n == int(n) else round(n, 1)
        if cleaned_entry:
            out[pid] = cleaned_entry
        # 提交了条目但清空了全部键 → 视同删除（不落空 dict 噪音）
    return out, errors


def _sanitize_platform_flags(
    raw: Any, *, field: str = _PLAT_HUMANIZE_PATH,
) -> Tuple[Dict[str, Dict[str, bool]], List[Dict[str, str]]]:
    """校验平台拟人开关表 {platform: {mark_read?: bool, typing?: bool}}。

    平台键归一小写且必须在 ``PLATFORMS``；条目键限 ``HUMANIZE_FLAG_KEYS``；
    值三态语义在存储层只存 true/false（缺席=跟随全局），条目清空=删除该平台。
    """
    errors: List[Dict[str, str]] = []
    if not isinstance(raw, Mapping):
        return {}, [{"field": field, "code": "bad_overrides"}]
    out: Dict[str, Dict[str, bool]] = {}
    for plat_raw, entry in raw.items():
        plat = str(plat_raw or "").strip().lower()
        if plat not in PLATFORMS:
            errors.append({"field": field, "code": "bad_platform"})
            continue
        if not isinstance(entry, Mapping):
            errors.append({"field": f"{field}.{plat}", "code": "bad_overrides"})
            continue
        cleaned: Dict[str, bool] = {}
        for k, v in entry.items():
            key = str(k)
            if key not in HUMANIZE_FLAG_KEYS:
                errors.append({
                    "field": f"{field}.{plat}.{key}",
                    "code": "bad_override_key"})
                continue
            b = _coerce_bool(v)
            if b is None:
                errors.append({
                    "field": f"{field}.{plat}.{key}", "code": "bad_bool"})
            else:
                cleaned[key] = b
        if cleaned:
            out[plat] = cleaned
    return out, errors


def _sanitize_schedule_overrides(
    raw: Any, *, field: str = _WS_ACCOUNTS_PATH,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, str]]]:
    """校验账号班表覆写表 {"platform:account_id": {enabled?/workdays?/start?/
    end?/timezone?}}。

    外层键必须是 ``平台:账号``（平台归一小写且落 ``PLATFORMS`` 白名单，账号非空
    ≤64 字符——防拼错平台名静默不生效）；条目键限 ``SCHEDULE_EDITABLE_KEYS``。
    start/end 在覆写里**不许空串**（空=想跟随默认就别提交该键）；timezone 空串
    =跟随全局（不落键）。条目清空=删除；空表 {} 合法=清空全部覆写。
    """
    errors: List[Dict[str, str]] = []
    if not isinstance(raw, Mapping):
        return {}, [{"field": field, "code": "bad_overrides"}]
    out: Dict[str, Dict[str, Any]] = {}
    for key_raw, entry in raw.items():
        key = str(key_raw or "").strip()
        plat, sep, acct = key.partition(":")
        plat = plat.strip().lower()
        acct = acct.strip()
        if not sep or plat not in PLATFORMS or not acct or len(acct) > 64:
            errors.append({"field": f"{field}.{key}", "code": "bad_account_key"})
            continue
        norm_key = f"{plat}:{acct}"
        if not isinstance(entry, Mapping):
            errors.append({"field": f"{field}.{norm_key}", "code": "bad_overrides"})
            continue
        cleaned: Dict[str, Any] = {}
        for k, v in entry.items():
            ekey = str(k)
            if ekey not in SCHEDULE_EDITABLE_KEYS:
                errors.append({
                    "field": f"{field}.{norm_key}.{ekey}",
                    "code": "bad_override_key"})
                continue
            if ekey == "enabled":
                b = _coerce_bool(v)
                if b is None:
                    errors.append({
                        "field": f"{field}.{norm_key}.{ekey}",
                        "code": "bad_bool"})
                else:
                    cleaned[ekey] = b
            elif ekey == "workdays":
                wd = _coerce_workdays(v)
                if wd is None:
                    errors.append({
                        "field": f"{field}.{norm_key}.{ekey}",
                        "code": "bad_workdays"})
                else:
                    cleaned[ekey] = wd
            elif ekey in ("start", "end"):
                hh = _coerce_hhmm(v)
                if not hh:
                    errors.append({
                        "field": f"{field}.{norm_key}.{ekey}",
                        "code": "bad_time"})
                else:
                    cleaned[ekey] = hh
            else:  # timezone
                tzv = _coerce_tzname(v)
                if tzv is None:
                    errors.append({
                        "field": f"{field}.{norm_key}.{ekey}",
                        "code": "bad_timezone"})
                elif tzv:
                    cleaned[ekey] = tzv
                # 空串=跟随全局时区 → 不落键
        if cleaned:
            out[norm_key] = cleaned
        # 提交了条目但清空了全部键 → 视同删除（与人设覆写同语义）
    return out, errors


def _sanitize_platform_enum_map(
    raw: Any, choices: Tuple[str, ...], *, field: str,
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """校验平台→枚举标量表（档位封顶 / 语音触发覆写共用）。

    平台键归一小写且必须在 ``PLATFORMS``；值归一小写且必须在 ``choices``；
    空串值＝删除该平台（UI「跟随全局」）。空表 {} 合法＝全部清除。
    """
    errors: List[Dict[str, str]] = []
    if not isinstance(raw, Mapping):
        return {}, [{"field": field, "code": "bad_overrides"}]
    out: Dict[str, str] = {}
    for plat_raw, v in raw.items():
        plat = str(plat_raw or "").strip().lower()
        if plat not in PLATFORMS:
            errors.append({"field": field, "code": "bad_platform"})
            continue
        s = str(v or "").strip().lower()
        if not s:
            continue  # 空值＝删除（跟随全局）
        if s not in choices:
            errors.append({"field": f"{field}.{plat}", "code": "bad_enum"})
        else:
            out[plat] = s
    return out, errors


def merged_scoped_overrides(
    config: Any, path: str, submitted: Mapping[str, Mapping[str, Any]],
    editable_keys: Tuple[str, ...] = OVERRIDE_EDITABLE_KEYS,
) -> Dict[str, Dict[str, Any]]:
    """提交表（UI 管理键）× 现存表（含 YAML 手调键）→ 最终整表（人设/平台通用）。

    语义：提交表定「哪些条目存在」（缺席＝删除）；每个保留条目先取现存覆写
    （``editable_keys`` 之外的 YAML 手调键原样保留），再叠 UI 管理键。UI 提交的
    键值为准，UI 没提交的可编辑键**从现存里剔除**（运营在 UI 里清掉某格＝真的清掉）。
    """
    existing = _dig(config, path, {}) or {}
    out: Dict[str, Dict[str, Any]] = {}
    for pid, entry in submitted.items():
        base = dict(existing.get(pid) or {}) if isinstance(existing, Mapping) else {}
        for k in editable_keys:
            base.pop(k, None)
        base.update(entry)
        out[pid] = base
    return out


def merged_persona_overrides(
    config: Any, submitted: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """人设节奏覆写的整表合并（``merged_scoped_overrides`` 的薄封装，向后兼容）。"""
    return merged_scoped_overrides(config, _OVERRIDES_PATH, submitted)


def validate_platform_flags_caps(
    flags: Any, caps: Any,
) -> List[Dict[str, str]]:
    """能力护栏：显式**开启**的平台拟人开关必须该平台真支持（纯函数）。

    前端把不支持的格子 disabled 只是体验，不算护栏——绕过 UI 直打 API 也要拦。
    ``caps`` = ``humanize_caps_by_platform`` 输出（{platform: {cap: {state, hard}}}）；
    None/不可用 → 全放行（fail-open：能力探针挂了不该锁死保存）。
    只拦 ``state=="unsupported"``（partial=部分模式支持、unknown=判不了，都放行）；
    只拦显式 True（把不支持的能力关掉无害，永远放行）。
    """
    if not isinstance(caps, Mapping) or not isinstance(flags, Mapping):
        return []
    errors: List[Dict[str, str]] = []
    for plat, entry in flags.items():
        if not isinstance(entry, Mapping):
            continue
        for key, val in entry.items():
            if val is not True:
                continue
            st = ((caps.get(str(plat)) or {}).get(str(key)) or {}).get("state")
            if st == "unsupported":
                errors.append({
                    "field": f"{_PLAT_HUMANIZE_PATH}.{plat}.{key}",
                    "code": "cap_unsupported"})
    return errors


def _dig(config: Any, path: str, default: Any = None) -> Any:
    node = config
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return default
        node = node.get(part)
    return default if node is None else node


def cross_validate(
    clean: Mapping[str, Any], config: Any,
) -> List[Dict[str, str]]:
    """跨字段规则：任何节奏键被触碰时，验证**合并后的全矩阵**自洽（min ≤ max）。

    合并视图＝新值优先、现值兜底；生效值按 humanize 同款键级优先级
    （人设 > 平台 > 全局）。逐层校验：
    - 顶层：仅当本次触碰 min/max（历史遗留的顶层不自洽不拦无关保存）；
    - 每个平台覆写 ×（顶层兜底）；
    - 每个人设覆写 ×（无平台 + 每个平台）——人设 min=30 落到平台 max=10 上，
      运行时 resolve_pacing 会按「min>max=禁用」**静默秒回**，必须提前拦下；
      每个人设最多报一条（首个冲突组合），防四平台连坐刷屏。
    config 现值来自 YAML（可能手写脏类型）→ 比较包 TypeError 守卫，绝不 500。
    """
    errors_ws = _validate_schedule_window(clean, config)
    errors_ws += _validate_schedule_timezone(clean, config)
    errors_ws += _validate_bubble_gap(clean, config)
    errors_ws += _validate_sendgate_ramp(clean, config)
    kmin = _DELAY_PREFIX + "min_sec"
    kmax = _DELAY_PREFIX + "max_sec"
    if not any(k.startswith(_DELAY_PREFIX) for k in clean):
        return errors_ws
    top_min = clean.get(kmin, _coerce_number(_dig(config, kmin, 0)) or 0)
    top_max = clean.get(kmax, _coerce_number(_dig(config, kmax, 0)) or 0)
    errors: List[Dict[str, str]] = []

    def _broken(vmin: Any, vmax: Any) -> bool:
        try:
            return vmin > vmax
        except TypeError:
            return False

    if (kmin in clean or kmax in clean) and _broken(top_min, top_max):
        errors.append({"field": kmin, "code": "min_gt_max"})

    plats = clean.get(_PLAT_OVERRIDES_PATH)
    if plats is None:
        plats = _dig(config, _PLAT_OVERRIDES_PATH, {}) or {}
    personas = clean.get(_OVERRIDES_PATH)
    if personas is None:
        personas = _dig(config, _OVERRIDES_PATH, {}) or {}
    if not isinstance(plats, Mapping):
        plats = {}
    if not isinstance(personas, Mapping):
        personas = {}

    def _eff(entry: Any, plat_entry: Any, key: str, top: Any) -> Any:
        v = entry.get(key) if isinstance(entry, Mapping) else None
        if v is None and isinstance(plat_entry, Mapping):
            v = plat_entry.get(key)
        return top if v is None else v

    for plat, pe in plats.items():
        if not isinstance(pe, Mapping):
            continue
        if _broken(_eff(pe, None, "min_sec", top_min),
                   _eff(pe, None, "max_sec", top_max)):
            errors.append({
                "field": f"{_PLAT_OVERRIDES_PATH}.{plat}",
                "code": "min_gt_max"})
    for pid, entry in personas.items():
        if not isinstance(entry, Mapping):
            continue
        contexts = [("", None)] + [
            (str(p), pe) for p, pe in plats.items() if isinstance(pe, Mapping)]
        for plat, pe in contexts:
            if _broken(_eff(entry, pe, "min_sec", top_min),
                       _eff(entry, pe, "max_sec", top_max)):
                suffix = f"@{plat}" if plat else ""
                errors.append({
                    "field": f"{_OVERRIDES_PATH}.{pid}{suffix}",
                    "code": "min_gt_max"})
                break
    return errors + errors_ws


def _validate_schedule_window(
    clean: Mapping[str, Any], config: Any,
) -> List[Dict[str, str]]:
    """班表窗口成对规则：default.start / end 合并视图必须**同空或同有**。

    只配一半（如只填 start）时 work_hours_gate 会 fail-open 成「不闸」——
    运营以为开了班表实际没生效，这类静默失效必须在保存时拦下。
    只在本次触碰任一端时校验（历史脏配置不拦无关项的保存）。
    """
    if _WS_START_PATH not in clean and _WS_END_PATH not in clean:
        return []
    start = clean.get(
        _WS_START_PATH, str(_dig(config, _WS_START_PATH, "") or ""))
    end = clean.get(_WS_END_PATH, str(_dig(config, _WS_END_PATH, "") or ""))
    if bool(str(start).strip()) != bool(str(end).strip()):
        empty_key = _WS_START_PATH if not str(start).strip() else _WS_END_PATH
        return [{"field": empty_key, "code": "incomplete_window"}]
    return []


_WS_ENABLED_PATH = "inbox.work_schedule.enabled"
_WS_TZ_PATH = "inbox.work_schedule.timezone"


def _validate_schedule_timezone(
    clean: Mapping[str, Any], config: Any,
) -> List[Dict[str, str]]:
    """D-Q1（Q-4 #267，基线红线③）：班表**开启**时时区必填。

    1.0.78 事故：「timezone 空＝服务器本地」让上海机器把纽约客户的下午当凌晨静默扣留。
    合并视图 enabled 为真 且 timezone 为空 → ``tz_required``。只在本次触碰 enabled /
    timezone 任一时校验（历史已开且空时区的存量配置不拦无关项的保存——它们由启动横幅
    ``[baseline] work_schedule.enabled=True tz=''`` 露出，用户下次改班表时会被要求补）。
    """
    if _WS_ENABLED_PATH not in clean and _WS_TZ_PATH not in clean:
        return []
    enabled = clean.get(_WS_ENABLED_PATH, bool(_dig(config, _WS_ENABLED_PATH, False)))
    tz = clean.get(_WS_TZ_PATH, str(_dig(config, _WS_TZ_PATH, "") or ""))
    if bool(enabled) and not str(tz or "").strip():
        return [{"field": _WS_TZ_PATH, "code": "tz_required"}]
    return []


_SG_TARGET_PATH = "companion_send_gate.target_cap"
_SG_START_PATH = "companion_send_gate.warmup_start_cap"


def _validate_sendgate_ramp(
    clean: Mapping[str, Any], config: Any,
) -> List[Dict[str, str]]:
    """发送闸门爬坡成对规则：合并视图 warmup_start_cap ≤ target_cap。

    起点比目标还高时 account_health 的线性爬坡会倒着走（额度随号龄**下降**），
    没有一种运营意图长这样——必是填反了，保存时拦下。只在本次触碰任一端时
    校验（历史脏配置不拦无关项的保存），与班表/条间隔成对规则同哲学。
    """
    if _SG_TARGET_PATH not in clean and _SG_START_PATH not in clean:
        return []
    target = clean.get(
        _SG_TARGET_PATH, _coerce_number(_dig(config, _SG_TARGET_PATH, 15)) or 15)
    start = clean.get(
        _SG_START_PATH, _coerce_number(_dig(config, _SG_START_PATH, 2)) or 2)
    try:
        broken = start > target
    except TypeError:
        broken = False
    return [{"field": _SG_START_PATH, "code": "min_gt_max"}] if broken else []


def _validate_bubble_gap(
    clean: Mapping[str, Any], config: Any,
) -> List[Dict[str, str]]:
    """条间思考抖动 lo/hi 成对规则：合并视图 lo ≤ hi。

    运行时 parse_bubbles_cfg 会静默把 hi 抬到 lo（不炸），但那是「运营以为配了
    2..5s 实际恒 5s」的静默失真——保存时拦下，与 deliver_delay 的 min_gt_max
    同码同语义。只在本次触碰任一端时校验。
    """
    if _BUB_LO_PATH not in clean and _BUB_HI_PATH not in clean:
        return []
    lo = clean.get(
        _BUB_LO_PATH, _coerce_number(_dig(config, _BUB_LO_PATH, 0.8)) or 0.8)
    hi = clean.get(
        _BUB_HI_PATH, _coerce_number(_dig(config, _BUB_HI_PATH, 2.5)) or 2.5)
    try:
        broken = lo > hi
    except TypeError:
        broken = False
    return [{"field": _BUB_LO_PATH, "code": "min_gt_max"}] if broken else []


def nested_patch(clean: Mapping[str, Any]) -> Dict[str, Any]:
    """点分路径 dict → 嵌套 dict（喂 ``save_overlay_patch``）。"""
    out: Dict[str, Any] = {}
    for path, value in clean.items():
        node = out
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    return out


def effective_values(config: Any) -> Dict[str, Any]:
    """按白名单读当前有效值（config 缺键回代码真实缺省）。"""
    out: Dict[str, Any] = {}
    for path, spec in FIELDS.items():
        val = _dig(config, path, spec["default"])
        if spec["type"] in ("delay_overrides", "platform_flags"):
            # 深拷贝一层：调用方（快照序列化/diff）拿到的不该是 config 活引用
            val = {str(k): dict(v) for k, v in (val or {}).items()
                   if isinstance(v, Mapping)} if isinstance(val, Mapping) else {}
        elif spec["type"] == "platform_enum_map":
            val = ({str(k): str(v) for k, v in val.items()}
                   if isinstance(val, Mapping) else {})
        elif spec["type"] == "schedule_overrides":
            # 深拷贝含内层 workdays 列表（快照不得持 config 活引用）
            val = ({str(k): {ek: (list(ev) if isinstance(ev, list) else ev)
                             for ek, ev in v.items()}
                    for k, v in val.items() if isinstance(v, Mapping)}
                   if isinstance(val, Mapping) else {})
        elif spec["type"] == "workdays":
            val = list(val) if isinstance(val, (list, tuple)) else []
        elif spec["type"] == "str_list":
            # YAML 手写数字条目（号码不加引号）统一 str 化，快照不持活引用
            val = ([str(x) for x in val]
                   if isinstance(val, (list, tuple)) else [])
        if path == "inbox.auto_draft.bootstrap_automation_mode" and val is None:
            # 真实缺省语义：全局档位为 auto_ai 时自动 bootstrap
            mode = str(_dig(config, "inbox.auto_draft.automation_mode",
                            "auto_ai") or "auto_ai").lower()
            val = (mode == "auto_ai")
        if path == _BUB_MAXPARTS_PATH and val is None:
            # 哨兵二次求值：parse_bubbles_cfg 的真实缺省＝逐句模式 5、打包模式 3
            val = 5 if bool(_dig(config, _BUB_PER_SENT_PATH, False)) else 3
        if path == "ai.context_depth":
            s = str(val or "").strip().lower()
            if s == "深度":
                val = "deep"
            elif s == "超大":
                val = "ultra"
            elif s not in CONTEXT_DEPTH_CHOICES:
                val = spec["default"]
        out[path] = val
    return out


def field_meta() -> Dict[str, Dict[str, Any]]:
    """给前端的字段元数据（hot 标注 + 枚举可选值 + 数值边界）。"""
    meta: Dict[str, Dict[str, Any]] = {}
    for path, spec in FIELDS.items():
        m: Dict[str, Any] = {"type": spec["type"], "hot": spec["hot"]}
        if spec["type"] in ("enum", "platform_enum_map"):
            m["choices"] = list(spec["choices"])
        if spec["type"] == "delay_overrides":
            m["editable_keys"] = list(OVERRIDE_EDITABLE_KEYS)
        if spec["type"] == "platform_flags":
            m["editable_keys"] = list(HUMANIZE_FLAG_KEYS)
        if spec["type"] == "schedule_overrides":
            m["editable_keys"] = list(SCHEDULE_EDITABLE_KEYS)
        if spec["type"] in ("platform_enum_map", "platform_flags") \
                or spec.get("keys"):
            m["keys"] = list(spec.get("keys") or PLATFORMS)
        if spec["type"] == "text":
            m["maxlen"] = spec.get("maxlen", 200)
        if spec["type"] == "str_list":
            m["item_maxlen"] = spec.get("item_maxlen", 64)
            m["max_items"] = spec.get("max_items", 500)
        if "lo" in spec:
            m["lo"] = spec["lo"]
            m["hi"] = spec["hi"]
        if "decimals" in spec:
            m["decimals"] = spec["decimals"]
        meta[path] = m
    return meta


def gate_status(config: Any) -> Dict[str, Any]:
    """只读展示的上游闸门状态（写入口在能力看板，本页只看不改）。"""
    return {
        "worker_enabled": bool(_dig(config, "inbox.l2_autosend.enabled", False)),
        "deliver": bool(_dig(config, "inbox.l2_autosend.deliver", False)),
        "auto_draft_enabled": bool(_dig(config, "inbox.auto_draft.enabled", True)),
        "platform_modes": dict(
            _dig(config, "inbox.auto_draft.platform_modes", {}) or {}),
    }


def build_snapshot(config: Any) -> Dict[str, Any]:
    """GET /api/reply-settings 的完整响应体。"""
    return {
        "ok": True,
        "values": effective_values(config),
        "meta": field_meta(),
        "gates": gate_status(config),
    }


def split_hot_pending(
    clean: Mapping[str, Any], *, worker_applied: Any = (),
) -> Tuple[List[str], List[str]]:
    """按热生效能力把本次写入分为（即时生效, 待重启生效）两组路径。

    ``worker_applied``：路由**实际热更成功**的路径集合。hot=="worker" 的键
    分属两类热更入口（deliver_delay 族 → ``apply_deliver_delay``、拟人开关
    → ``apply_humanize_flags``），任一入口独立成败——按路径逐键判定，
    不再用一个布尔连坐。
    """
    applied = set(worker_applied or ())
    live: List[str] = []
    pending: List[str] = []
    for path in clean:
        hot = FIELDS[path]["hot"]
        if hot is True:
            live.append(path)
        elif hot == "worker":
            (live if path in applied else pending).append(path)
        else:
            pending.append(path)
    return live, pending


def merged_delay_block(config: Any, clean: Mapping[str, Any]) -> Dict[str, Any]:
    """现有 deliver_delay 块 + 本次改动 → 完整块（喂 worker 热更新）。

    以现块为底：persona_overrides 等本页不管的键原样保留，不被热更抹掉。
    """
    block = dict(_dig(config, "inbox.l2_autosend.deliver_delay", {}) or {})
    for path, value in clean.items():
        if path.startswith(_DELAY_PREFIX):
            block[path[len(_DELAY_PREFIX):]] = value
    return block


__all__ = [
    "AUTOMATION_MODES", "VOICE_TRIGGERS", "FIELDS",
    "REPLY_LENGTH_CHOICES", "EMOJI_LEVEL_CHOICES", "TONE_HINT_MAXLEN",
    "OVERRIDE_EDITABLE_KEYS", "REPLACE_PATHS", "PRESETS",
    "PLATFORMS", "HUMANIZE_FLAG_KEYS", "SCHEDULE_EDITABLE_KEYS",
    "BUBBLE_GAP_LO_DEFAULT", "BUBBLE_GAP_HI_DEFAULT",
    "BUBBLE_CJK_PER_CHAR_DEFAULT", "BUBBLE_LATIN_PER_CHAR_DEFAULT",
    "BUBBLE_MAX_GAP_DEFAULT", "BUBBLE_TOTAL_BUDGET_DEFAULT",
    "BUBBLE_EXPLICIT_NEWLINE_ONLY_DEFAULT", "BUBBLE_MIN_TOTAL_CHARS_DEFAULT",
    "sanitize_patch", "cross_validate", "nested_patch",
    "effective_values", "field_meta", "gate_status", "build_snapshot",
    "split_hot_pending", "merged_delay_block", "merged_persona_overrides",
    "merged_scoped_overrides", "validate_platform_flags_caps",
    "match_preset", "explain_pacing",
]
