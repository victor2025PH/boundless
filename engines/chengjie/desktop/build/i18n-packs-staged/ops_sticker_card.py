# -*- coding: utf-8 -*-
"""ops-overview「表情包」卡词条（2026-08-17 表情包主线观测面）。

独立 pack（先例 ops_msg_ops_card.py）：ops_overview_page.py 是热区，
新卡词条独立成文件避免并发编辑互踩。
"""

ZH = {
    "ov2_s_stk": "表情包（备货 + 发送形态）",
    "ov2_stk_packs": "表情包数",
    "ov2_stk_items": "贴纸备货（张）",
    "ov2_stk_sends": "发送（本进程）",
    "ov2_stk_collects": "收藏入包（次）",
    "ov2_stk_by_plat": "按平台",
    "ov2_stk_sent_as": "发送形态",
    "ov2_stk_as_native": "原生贴纸",
    "ov2_stk_as_image": "图片回退",
    "ov2_stk_fallback_hint": "（回退占比偏高＝目标平台原生贴纸能力缺口：WA 边车未升级 / LINE 自建包为主）",
    "ov2_js_stk_none": "已备货，本进程尚无发送记录",
}

EN = {
    "ov2_s_stk": "Sticker packs (stock + send modes)",
    "ov2_stk_packs": "Packs",
    "ov2_stk_items": "Stickers stocked",
    "ov2_stk_sends": "Sends (process)",
    "ov2_stk_collects": "Collected",
    "ov2_stk_by_plat": "By platform",
    "ov2_stk_sent_as": "Sent as",
    "ov2_stk_as_native": "native sticker",
    "ov2_stk_as_image": "image fallback",
    "ov2_stk_fallback_hint": "(high fallback share = missing native sticker capability: WA sidecar not upgraded / LINE custom packs)",
    "ov2_js_stk_none": "Stocked; no sends recorded in this process yet",
}
