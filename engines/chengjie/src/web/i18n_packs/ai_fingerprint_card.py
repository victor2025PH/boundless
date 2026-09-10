# -*- coding: utf-8 -*-
"""ops-overview「🧬 AI 指纹」卡词条（P-1 D · #259 #254，2026-09-08）。

四格（近 24h）：破折号率（起草层 punct_fix>0 占比）/ 客服腔命中率（O-1 C 守卫）/ 引用无锚点
（P-1 B claim_guard）/ 承诺无动作（P-3 落日志后自动有值）。阈值 0 绿 / <2% 黄 / ≥2% 红。
数据源：``/api/workspace/metrics`` → ``ai_fingerprint``（``src/inbox/ai_fingerprint_stats.snapshot``）；
消费方：``ops_overview.html::renderAiFingerprint``。

独立小 pack（与 buried_conv_ops.py 同理：大包常被并行编辑，拆小文件零冲突）。
繁体手写 ZH_HANT（scripts/i18n_hant.py 生成时跳过人工包覆盖的键）。
"""

ZH = {
    "ov2_s_aifp": "AI 指纹（近 24h · 草稿 = 将发文本）",
    "ov2_aifp_hint": "四个数都该是 0。破折号率 = 起草层净化前带 — / ; / 弯引号的稿子占比（生成侧读数，"
                     "后处理已兜到 0）；客服腔 = 陪伴域稿命中「I hear you / 如有需要 / 您」被改写或转人审的占比；"
                     "引用无锚点 = 「you mentioned / 你之前说」在会话与记忆里找不到依据被改中性的次数；"
                     "承诺无动作 = 说了发图却没动作的次数（P-3 落日志后有值）。"
                     "红黄绿：0 / <2% / ≥2%。数值与 backend.log 的 [draft] / [persona-guard] / [claim-guard] 行一致，"
                     "持久口径 logs/ai_fingerprint/*.jsonl。",
    "ov2_js_aifp_dash": "破折号率",
    "ov2_js_aifp_svc": "客服腔命中率",
    "ov2_js_aifp_claim": "引用无锚点",
    "ov2_js_aifp_promise": "承诺无动作",
    "ov2_js_aifp_gate": "发送门兜底命中",
    "ov2_js_aifp_cmt": "答应见面/地址",
    "ov2_js_aifp_blame": "自责再承诺",
    "ov2_js_aifp_drafts": "稿",
    "ov2_js_aifp_na": "无样本",
    "ov2_js_aifp_pending": "待 P-3 落日志",
    "ov2_js_aifp_gate_note": "发送门兜底命中 > 0 说明有出站路径绕过了起草层净化（协议直发 / 未挂点的生成口），grep [outbound-leak] 定位 stage。",
    "ov2_js_aifp_window": "窗口",
}

EN = {
    "ov2_s_aifp": "AI fingerprint (last 24h · draft = what gets sent)",
    "ov2_aifp_hint": "All four should read 0. Dash rate = share of drafts that still had — / ; / curly quotes "
                     "before draft-stage cleanup (a generation-side reading; post-processing already brings it to 0). "
                     "Service tone = share of companion drafts hit by \"I hear you / feel free / formal you\" and rewritten "
                     "or sent to review. Unanchored reference = times \"you mentioned / last time you\" had no basis in the "
                     "conversation or memory and was neutralised. Promise without action = photo promised, nothing sent "
                     "(populated once P-3 logs it). Traffic light: 0 / <2% / ≥2%. Numbers match the [draft] / [persona-guard] / "
                     "[claim-guard] lines in backend.log; persistent source logs/ai_fingerprint/*.jsonl.",
    "ov2_js_aifp_dash": "Dash rate",
    "ov2_js_aifp_svc": "Service-tone hits",
    "ov2_js_aifp_claim": "Unanchored references",
    "ov2_js_aifp_promise": "Promise without action",
    "ov2_js_aifp_gate": "Send-gate fallback hits",
    "ov2_js_aifp_cmt": "Meet/address claims",
    "ov2_js_aifp_blame": "Self-blame re-promise",
    "ov2_js_aifp_drafts": "drafts",
    "ov2_js_aifp_na": "no samples",
    "ov2_js_aifp_pending": "waiting for P-3 logging",
    "ov2_js_aifp_gate_note": "Send-gate fallback hits > 0 means some outbound path bypassed draft-stage cleanup (protocol direct send / an unhooked generator); grep [outbound-leak] for the stage.",
    "ov2_js_aifp_window": "window",
}

ZH_HANT = {
    "ov2_s_aifp": "AI 指紋（近 24h · 草稿 = 將發文字）",
    "ov2_aifp_hint": "四個數都該是 0。破折號率 = 起草層淨化前帶 — / ; / 彎引號的稿子佔比（生成側讀數，"
                     "後處理已兜到 0）；客服腔 = 陪伴域稿命中「I hear you / 如有需要 / 您」被改寫或轉人審的佔比；"
                     "引用無錨點 = 「you mentioned / 你之前說」在會話與記憶裡找不到依據被改中性的次數；"
                     "承諾無動作 = 說了發圖卻沒動作的次數（P-3 落日誌後有值）。"
                     "紅黃綠：0 / <2% / ≥2%。數值與 backend.log 的 [draft] / [persona-guard] / [claim-guard] 行一致，"
                     "持久口徑 logs/ai_fingerprint/*.jsonl。",
    "ov2_js_aifp_dash": "破折號率",
    "ov2_js_aifp_svc": "客服腔命中率",
    "ov2_js_aifp_claim": "引用無錨點",
    "ov2_js_aifp_promise": "承諾無動作",
    "ov2_js_aifp_gate": "發送門兜底命中",
    "ov2_js_aifp_cmt": "答應見面/地址",
    "ov2_js_aifp_blame": "自責再承諾",
    "ov2_js_aifp_drafts": "稿",
    "ov2_js_aifp_na": "無樣本",
    "ov2_js_aifp_pending": "待 P-3 落日誌",
    "ov2_js_aifp_gate_note": "發送門兜底命中 > 0 說明有出站路徑繞過了起草層淨化（協定直發 / 未掛點的生成口），grep [outbound-leak] 定位 stage。",
    "ov2_js_aifp_window": "視窗",
}
