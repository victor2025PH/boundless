# -*- coding: utf-8 -*-
"""主动触达候选诊断词条（P1-198 问题 7：「队列看起来没动」的可读原因）。

键族：``ov_ppskip_*``——/rpa 概览「主动话题预览」卡的不发原因标签。
reason 枚举与 ``companion_proactive.plan_proactive_sends(diagnostics=)`` 的
收集器一一对应；未登记的 reason 前端回落显示原始枚举名。
"""

ZH = {
    "ov_ppskip_hd": "不发原因",
    "ov_ppskip_hint": "本轮被各闸口拦下的会话与原因（只读诊断；候选为 0 时看这里）",
    "ov_ppskip_min_silent": "沉默未达标",
    "ov_ppskip_cooldown": "冷却中",
    "ov_ppskip_awaiting_reply": "欠回复（对方最后发言）",
    "ov_ppskip_care_pending": "关怀让路",
    "ov_ppskip_backoff": "连续未回，停发",
    "ov_ppskip_never_replied": "从未回复，出圈",
    "ov_ppskip_user_quiet": "对方安静时段",
    "ov_ppskip_quiet_global": "全局安静时段",
    "ov_ppskip_no_opener": "无可用开场",
    "ov_ppskip_opener_error": "开场生成异常",
    "ov_ppskip_opener_blocked": "开场被护栏拦下",
    "ov_ppskip_crisis": "危机保护",
    "ov_ppskip_archived": "已归档",
    "ov_ppskip_no_ts": "无活动时间",
}

EN = {
    "ov_ppskip_hd": "Why not sending",
    "ov_ppskip_hint": "Conversations blocked by each gate this round (read-only; check here when candidates = 0)",
    "ov_ppskip_min_silent": "Silence below threshold",
    "ov_ppskip_cooldown": "Cooling down",
    "ov_ppskip_awaiting_reply": "Awaiting our reply (peer spoke last)",
    "ov_ppskip_care_pending": "Care message queued",
    "ov_ppskip_backoff": "No replies, backed off",
    "ov_ppskip_never_replied": "Never replied, retired",
    "ov_ppskip_user_quiet": "Peer quiet hours",
    "ov_ppskip_quiet_global": "Global quiet hours",
    "ov_ppskip_no_opener": "No usable opener",
    "ov_ppskip_opener_error": "Opener error",
    "ov_ppskip_opener_blocked": "Opener blocked by guard",
    "ov_ppskip_crisis": "Crisis protection",
    "ov_ppskip_archived": "Archived",
    "ov_ppskip_no_ts": "No activity timestamp",
}
