# -*- coding: utf-8 -*-
"""AI 助手悬浮球（「小智」，2026-08-19 P0）服务端词条域。

只收 /api/assistant/* 路由的 detail/error 文案与服务端生成的回答话术
（asb.* 前缀）；前端面板 UI 文案按 cp-i18n 模式自带于
shared/assistant/assistant-ball.js（组件跨壳/桌面镜像自包含，模板零键）。
旧帮助球词条见 help_ball.py（hb.*），面板工具行直接复用。
"""

ZH = {
    "asb.err.disabled": "AI 助手未启用",
    "asb.err.q_empty": "问题不能为空",
    "asb.err.q_too_long": "问题太长（≤20000 字，超长部分已截断）",
    "asb.err.rate_limited": "问得太快啦，稍后再试",
    "asb.err.timeout": "生成超时，请重试；若持续失败请走「报障」提交",
    "asb.err.answer_failed": "回答生成失败，请重试；若持续失败请走「报障」提交",
    "asb.err.report_disabled": "报障通道未启用",
    "asb.err.desc_too_short": "请至少用一句话描述问题（≥5 字）",
    "asb.err.bad_shot": "截图无效（{reason}）：仅支持 PNG/JPEG 且不超过大小上限",
    "asb.err.report_failed": "报障提交失败，请稍后重试",
    "asb.err.bad_feedback": "反馈参数无效",
    "asb.err.voice_disabled": "语音提问未启用",
    "asb.err.bad_audio": "音频无效（{reason}）",
    "asb.err.asr_unavailable": "转写服务未配置，请联系管理员",
    "asb.err.asr_empty": "没有听清内容，请靠近麦克风重说一次",
    "asb.err.asr_failed": "转写失败，请重试或改用文字输入",
    "asb.a.no_hit": (
        "这个问题我在产品帮助库里没有找到可靠依据，为避免误导就不猜了；"
        "你的问题已记录，我们会尽快补充。若这是一个故障，请切到「报障」"
        "标签一键提交（会自动带上页面与版本信息）。"
    ),
    # 动作注册表 /api/assistant/act*（实施58 P1，2026-08-23）
    "asb.act.unknown": "未知动作",
    "asb.act.forbidden": "当前角色无权执行该动作",
    "asb.act.bad_params": "动作参数无效（{detail}）",
    "asb.act.path_na": "目标页面不在导航白名单内",
    "asb.act.confirm_expired": "确认已过期或已使用，请重新发起",
    "asb.act.apply_failed": "应用失败：{detail}",
    "asb.act.undo_missing": "撤销记录不存在或已过期",
    # 「替我做」智能体 /api/assistant/agent/*（实施58 P2）
    "asb.agent.disabled": "「替我做」智能体未启用",
    "asb.agent.goal_empty": "请用一句话描述要做什么",
    "asb.agent.llm_down": "AI 规划服务暂不可用，请稍后再试",
    "asb.agent.plan_failed": "没能生成可靠的计划，请换个说法再试",
    # 手机扫码操控 /api/assistant/pair*（实施58 P4）
    "asb.pair.revoked": "手机配对已断开或过期，请重新扫码",
    "asb.pair.missing": "该手机会话不存在或已过期",
    # ops-overview「🤖 AI 助手小智」卡（2026-08-20）
    "ov2_s_assist": "AI 助手小智（问答 / 报障 / 语料）",
    "ov2_as_hint": (
        "问答量/自答率=持久 7 天口径（重启不清零）；报障/限频=本进程口径。"
        "「未答清单」按频次排序＝运营补语料照单：跑 seed_product_help 或往 "
        "howto_pack 加条目。"
    ),
    # 拒答分型行（实施74 P5，2026-08-27）：「没答上」有两种成因，处置完全不同
    "ov2_as_refuse": "拒答分型（7天）",
    "ov2_as_refuse_hit": "零命中",
    "ov2_as_refuse_basis": "有条目但答不了",
    "ov2_as_refuse_tip": (
        "零命中＝语料里压根没有相关条目，处置是**补 how-to**（照「未答清单」加）；"
        "有条目但答不了＝检索命中了、LLM 自认答不了（NO_BASIS 哨兵），处置是看"
        "这批问题该不该进产品、或该条目内容不够。哨兵占比恒 0 且有拒答 → "
        "哨兵没在工作（提示词没生效/模型不听话），此时小智会把沾边条目硬凑成答案。"
    ),
    "ov2_as_refuse_none": "本窗口没有拒答",
    "ov2_as_refuse_silent": "哨兵零触发（有拒答却全是零命中）——请核查提示词是否生效",
    "ov2_as_kb": "语料条数",
    "ov2_as_qa7": "问答（7天）",
    "ov2_as_rate": "自答率",
    "ov2_as_fb": "反馈",
    "ov2_as_reports": "报障（本进程）",
    "ov2_as_rl": "限频拦截",
    "ov2_as_miss": "未答清单 Top（14 天，补语料照单）",
    "ov2_as_orb_hd": "对话球（14 天）",
    "ov2_as_orb_lv": "开机档位",
    "ov2_as_orb_say": "播报",
    "ov2_as_orb_listen": "语音聆听",
    "ov2_as_orb_deg": "性能降档",
    "ov2_as_orb_calm": "切安静档",
    # 助教用量行（实施58 P6 观察期，2026-08-23）
    "ov2_as_xz_t": "助教用量（14天）",
    "ov2_as_xz_teach": "教学 开/点/问",
    "ov2_as_xz_tasks": "代办任务",
    "ov2_as_xz_flows": "登录流程 起→成",
    "ov2_as_xz_pair": "手机扫码",
    # 用量行内联统计徽标（2026-08-27 封板门禁收口：模板脚本禁硬编码 CJK）
    "ov2_as_xz_tasks_d": "（✓{ok} 确{cf} 撤{un} 🎤{vg}）",
    "ov2_as_xz_fail": "（败{n}）",
    "ov2_as_xz_cancel": "（取消{n}）",
    "ov2_as_xz_kick": "（踢{n}）",
    "ov2_as_nomiss": "暂无未答问题——语料覆盖良好",
}

EN = {
    "asb.err.disabled": "AI assistant is not enabled",
    "asb.err.q_empty": "Question must not be empty",
    "asb.err.q_too_long": "Question too long (max 20000 chars, excess truncated)",
    "asb.err.rate_limited": "Too many questions, please slow down",
    "asb.err.timeout": (
        "Generation timed out, please retry; if it keeps failing use the "
        "Report tab"
    ),
    "asb.err.answer_failed": (
        "Failed to generate an answer, please retry; if it keeps failing "
        "use the Report tab"
    ),
    "asb.err.report_disabled": "Bug reporting is not enabled",
    "asb.err.desc_too_short": "Please describe the issue (at least 5 chars)",
    "asb.err.bad_shot": (
        "Invalid screenshot ({reason}): only PNG/JPEG within the size limit"
    ),
    "asb.err.report_failed": "Failed to submit the report, please retry later",
    "asb.err.bad_feedback": "Invalid feedback payload",
    "asb.err.voice_disabled": "Voice input is not enabled",
    "asb.err.bad_audio": "Invalid audio ({reason})",
    "asb.err.asr_unavailable": "Transcription backend not configured",
    "asb.err.asr_empty": "Couldn't hear that, please try again",
    "asb.err.asr_failed": "Transcription failed, please retry or type",
    "asb.a.no_hit": (
        "I couldn't find a reliable basis for this in the product help "
        "corpus, so I won't guess. Your question has been recorded and the "
        "corpus will be improved. If this is a malfunction, please switch "
        "to the Report tab to submit it in one click (page and build info "
        "are attached automatically)."
    ),
    # action registry /api/assistant/act* (impl58 P1, 2026-08-23)
    "asb.act.unknown": "Unknown action",
    "asb.act.forbidden": "Your role is not allowed to run this action",
    "asb.act.bad_params": "Invalid action parameters ({detail})",
    "asb.act.path_na": "Target page is not in the navigation whitelist",
    "asb.act.confirm_expired": "Confirmation expired or already used, please retry",
    "asb.act.apply_failed": "Apply failed: {detail}",
    "asb.act.undo_missing": "Undo record missing or expired",
    # agent /api/assistant/agent/* (impl58 P2)
    "asb.agent.disabled": "The do-it-for-me agent is not enabled",
    "asb.agent.goal_empty": "Please describe the goal in one sentence",
    "asb.agent.llm_down": "AI planning is temporarily unavailable, retry later",
    "asb.agent.plan_failed": "Could not build a reliable plan, please rephrase",
    # phone pairing /api/assistant/pair* (impl58 P4)
    "asb.pair.revoked": "Phone pairing revoked or expired, please rescan",
    "asb.pair.missing": "That phone session does not exist or has expired",
    # ops-overview assistant card (2026-08-20)
    "ov2_s_assist": "AI Assistant (Q&A / reports / corpus)",
    "ov2_as_hint": (
        "Q&A volume / self-answer rate = persistent 7-day window; reports / "
        "rate-limit hits = process counters. The missed-question list is the "
        "ops to-do for corpus refills (seed_product_help / howto_pack)."
    ),
    # Refusal breakdown row (impl74 P5, 2026-08-27)
    "ov2_as_refuse": "Refusals (7d)",
    "ov2_as_refuse_hit": "no retrieval hit",
    "ov2_as_refuse_basis": "hit but unanswerable",
    "ov2_as_refuse_tip": (
        "No retrieval hit = the corpus has nothing relevant; fix by adding "
        "how-to entries (see the missed-question list). Hit but unanswerable "
        "= retrieval matched yet the LLM declared it cannot answer (NO_BASIS "
        "sentinel); decide whether those requests belong in the product, or "
        "enrich that entry. A sentinel share stuck at 0 while refusals exist "
        "means the sentinel is not working and the assistant is stretching "
        "loosely-related entries into answers."
    ),
    "ov2_as_refuse_none": "No refusals in this window",
    "ov2_as_refuse_silent":
        "Sentinel never fired (all refusals were zero-hit) — check the prompt",
    "ov2_as_kb": "Corpus entries",
    "ov2_as_qa7": "Q&A (7d)",
    "ov2_as_rate": "Self-answer rate",
    "ov2_as_fb": "Feedback",
    "ov2_as_reports": "Reports (process)",
    "ov2_as_rl": "Rate-limited",
    "ov2_as_miss": "Missed questions Top (14d, corpus to-do)",
    "ov2_as_orb_hd": "Orb (14d)",
    "ov2_as_orb_lv": "Boot tier",
    "ov2_as_orb_say": "Read-aloud",
    "ov2_as_orb_listen": "Voice listens",
    "ov2_as_orb_deg": "Perf downgrades",
    "ov2_as_orb_calm": "Calm switches",
    # assistant usage row (impl58 P6 observation, 2026-08-23)
    "ov2_as_xz_t": "Copilot usage (14d)",
    "ov2_as_xz_teach": "Teach on/click/ask",
    "ov2_as_xz_tasks": "Tasks",
    "ov2_as_xz_flows": "Login flows start→done",
    "ov2_as_xz_pair": "Phone scans",
    "ov2_as_xz_tasks_d": " (✓{ok} · {cf} confirmed · {un} undone · 🎤{vg})",
    "ov2_as_xz_fail": " ({n} failed)",
    "ov2_as_xz_cancel": " ({n} cancelled)",
    "ov2_as_xz_kick": " ({n} kicked)",
    "ov2_as_nomiss": "No missed questions — corpus coverage looks good",
}
