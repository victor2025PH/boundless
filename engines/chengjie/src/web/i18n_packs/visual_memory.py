# -*- coding: utf-8 -*-
"""#333 视觉记忆 API 词条（2026-09-17）：``/api/visual-memory/*`` 的 4xx/5xx 文案。

图片记忆可看 / 可纠正 / 可删（REA733「用户纠正必须能覆盖 AI 标签」）。繁体手写 ZH_HANT。
"""

ZH = {
    "err.vmem.store_unavailable": "视觉记忆库未装载（config/visual_memory.db 打不开），稍后再试",
    "err.vmem.bad_conversation": "会话 id 不合法",
    "err.vmem.relation_required": "确认关系人时必须给 relation（如 sister / friend / pet）",
    "err.vmem.bad_kind": "kind 只能是 self（本人）或 relation（关系人）",
    "err.vmem.no_face_observation": "这个会话没有可确认的带脸图片记录（近 24h 内客户没发过能识出人脸的图）",
    "err.vmem.entity_not_found": "没有这个视觉实体（可能已退休或不属于该会话）",
}

EN = {
    "err.vmem.store_unavailable": "Visual memory store is not loaded (config/visual_memory.db cannot be opened); try again later",
    "err.vmem.bad_conversation": "Invalid conversation id",
    "err.vmem.relation_required": "Confirming a relation requires `relation` (e.g. sister / friend / pet)",
    "err.vmem.bad_kind": "kind must be `self` or `relation`",
    "err.vmem.no_face_observation": "No face observation to confirm in this conversation (no recognizable face photo from the customer in the last 24h)",
    "err.vmem.entity_not_found": "No such visual entity (retired or not in this conversation)",
}

ZH_HANT = {
    "err.vmem.store_unavailable": "視覺記憶庫未載入（config/visual_memory.db 打不開），稍後再試",
    "err.vmem.bad_conversation": "會話 id 不合法",
    "err.vmem.relation_required": "確認關係人時必須給 relation（如 sister / friend / pet）",
    "err.vmem.bad_kind": "kind 只能是 self（本人）或 relation（關係人）",
    "err.vmem.no_face_observation": "這個會話沒有可確認的帶臉圖片記錄（近 24h 內客戶沒發過能識出人臉的圖）",
    "err.vmem.entity_not_found": "沒有這個視覺實體（可能已退休或不屬於該會話）",
}
