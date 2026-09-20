# -*- coding: utf-8 -*-
"""主对话链模式（ai.primary：cloud / local / local_only）运营面词条。

覆盖 developer 页切换卡 + setup 路由错误/成功文案。本键只管主对话链——
嵌入/视觉/翻译各有端点，不由本开关代管（与 AIClient 注释口径一致）。
"""

ZH = {
    "dv_ai_p_title": "主对话模型位置",
    "dv_ai_p_sub": "决定聊天内容走云端还是本机 GPU。local_only＝严格隐私，本地挂了也不回落云端。需先配好下方「本地兜底」端点（ai.fallback）。",
    "dv_ai_p_mode_cloud": "云端优先（默认）",
    "dv_ai_p_mode_local": "本地优先（挂了可回落云）",
    "dv_ai_p_mode_local_only": "仅本地（绝不上传）",
    "dv_ai_p_btn_save": "切换并生效",
    "dv_ai_p_saving": "切换中…",
    "dv_ai_p_need_pick": "请先选择模式",
    "dv_ai_p_local_model": "本地模型",
    "dv_ai_p_not_ready": "本地端点未就绪——请先在配置里启用 ai.fallback（enabled + base_url + model）",
    "dv_ai_p_divergent": "声明 {configured}，运行时实际 {effective}（端点缺或热重建失败时会出现）",
    "dv_ai_p_js_u_local_primary": "本地主链",
    "err.setup.ai_primary_invalid": "primary 须为 cloud / local / local_only",
    "err.setup.ai_primary_need_local": "切换本地模式前须配好 ai.fallback（enabled + base_url + model）",
    "err.setup.ai_primary_save_failed": "保存失败：{reason}",
    "err.setup.ai_primary_locked": "主链模式已被锁定为 {mode}（老板指令，2026-08-22）。变更需老板批准后在 overlay 解除 ai.primary_lock。",
    "setup.ai_primary.saved": "主对话模式已切换",
}

EN = {
    "dv_ai_p_title": "Primary chat model location",
    "dv_ai_p_sub": "Where chat content goes: cloud or on-prem GPU. local_only never falls back to cloud. Requires ai.fallback endpoint first.",
    "dv_ai_p_mode_cloud": "Cloud first (default)",
    "dv_ai_p_mode_local": "Local first (cloud fallback OK)",
    "dv_ai_p_mode_local_only": "Local only (never upload)",
    "dv_ai_p_btn_save": "Switch & apply",
    "dv_ai_p_saving": "Switching…",
    "dv_ai_p_need_pick": "Pick a mode first",
    "dv_ai_p_local_model": "Local model",
    "dv_ai_p_not_ready": "Local endpoint not ready — enable ai.fallback (enabled + base_url + model) first",
    "dv_ai_p_divergent": "Configured {configured}, runtime {effective} (missing endpoint or reload failure)",
    "dv_ai_p_js_u_local_primary": "local primary",
    "err.setup.ai_primary_invalid": "primary must be cloud / local / local_only",
    "err.setup.ai_primary_need_local": "Configure ai.fallback (enabled + base_url + model) before switching to local mode",
    "err.setup.ai_primary_save_failed": "Save failed: {reason}",
    "err.setup.ai_primary_locked": "Primary chat mode is locked to {mode} (boss order, 2026-08-22). Remove ai.primary_lock in the overlay with boss approval to change it.",
    "setup.ai_primary.saved": "Primary chat mode updated",
}
