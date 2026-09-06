# -*- coding: utf-8 -*-
"""运营总览「📚 知识库接通性」卡词条（J-9 #184，2026-09-05）。

背景：skuio 反馈「知识库/技能检索对拟稿零贡献」——此前没有任何读数能回答
「用户 KB 是不是空的 / 检索到底有没有进过 prompt」。本卡读 ``GET /api/kb/health``：
用户条目数 / 厂商预置数（桌面对客检索已硬排除）/ 向量化数 / 近 7 天注入命中数 /
最近命中时刻。命中口径＝skill_manager 在 kb_gate 真把 kb_context 注入 prompt 时的
``log_query(hit=True)``，不是「搜到过东西」。

**刻意独立成 pack**（与 ops_killswitch_card 同决策）：共享树多线并发下新文件＝零撞车。
"""

ZH = {
    "ov2_s_kbh": "知识库接通性（用户条目 / 近 7 天注入命中）",
    "ov2_kbh_user": "用户条目",
    "ov2_kbh_vendor": "厂商预置",
    "ov2_kbh_embedded": "已向量化",
    "ov2_kbh_hits7": "近 7 天注入命中",
    "ov2_kbh_line": "知识库：用户条目 {u} / 近 7 天命中 {h}（检索 {q} 次）",
    "ov2_kbh_last_hit": "最近一次注入 prompt：{t}",
    "ov2_kbh_never": "近 7 天没有任何知识条目进过 prompt",
    "ov2_kbh_empty_user": "用户知识库为空——AI 拟稿只能靠人设与对话上下文，去 /knowledge 添加你自己的条目",
    "ov2_kbh_vendor_excluded": "厂商预置 {n} 条已从对客检索硬排除（不看启用/停用），可在 /knowledge 一键清空",
    "ov2_kbh_vendor_included": "⚠ 厂商预置 {n} 条正参与对客检索（服务器部署默认；桌面应为排除）",
    "ov2_kbh_zero_hits": "有 {u} 条用户条目却 7 天零命中：触发词是否贴近客户原话？向量化 {e}/{u}",
    "ov2_kbh_feedback_none": "反馈：暂无",
    "ov2_kbh_feedback": "反馈满意率 {p}%（{n} 条）",
    "ov2_kbh_hint": "命中＝kb_gate 真把条目注入 prompt 的次数（kb_query_log，滚动 7 天）；"
                    "日志里的 hits=- 是风控关键词字段，与知识库无关。",
}

EN = {
    "ov2_s_kbh": "Knowledge base wiring (user entries / prompt hits, 7d)",
    "ov2_kbh_user": "User entries",
    "ov2_kbh_vendor": "Vendor preset",
    "ov2_kbh_embedded": "Embedded",
    "ov2_kbh_hits7": "Prompt hits (7d)",
    "ov2_kbh_line": "KB: {u} user entries / {h} hits in 7d ({q} lookups)",
    "ov2_kbh_last_hit": "Last injected into a prompt: {t}",
    "ov2_kbh_never": "No knowledge entry reached a prompt in the last 7 days",
    "ov2_kbh_empty_user": "User KB is empty — drafts rely on persona + context only; add your own entries at /knowledge",
    "ov2_kbh_vendor_excluded": "{n} vendor preset entries are hard-excluded from customer retrieval (regardless of enabled); clear them at /knowledge",
    "ov2_kbh_vendor_included": "⚠ {n} vendor preset entries participate in customer retrieval (server default; desktop should exclude)",
    "ov2_kbh_zero_hits": "{u} user entries but zero hits in 7d: do triggers match customer wording? Embedded {e}/{u}",
    "ov2_kbh_feedback_none": "Feedback: none yet",
    "ov2_kbh_feedback": "Feedback satisfaction {p}% ({n} ratings)",
    "ov2_kbh_hint": "A hit = kb_gate actually injected an entry into the prompt (kb_query_log, rolling 7d); "
                    "the hits=- field in logs is the risk-control keyword field, unrelated to the KB.",
}
