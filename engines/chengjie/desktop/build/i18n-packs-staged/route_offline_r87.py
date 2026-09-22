# -*- coding: utf-8 -*-
"""R87 #330 词条：会话「无限制」ChatX聊天模型端点离线 → 按标准档代答 + 状态带可见（2026-09-17）。

- ``inbox.cs.route_offline*``：conv_state 新状态 ``route_offline``——回退开着（默认）＝琥珀
  「ChatX聊天模型离线 · 已按标准档回复」；``ai.unrestricted.offline_fallback=false`` ＝红「这轮没人回」。
- ``inbox.cs.act.route_standard``：动作「切回标准」（POST /api/unified-inbox/conv-model-route
  profile=standard）。
- ``inbox.route.unr_offline*``：模式面板选项屏里「无限制」行在端点离线时的副标题 /
  点击提示（行灰显但可看原因）。
繁体手写 ZH_HANT（不碰生成物 zh_hant_auto）。占位符：``{hhmm}`` 时刻；``{err}`` 探活错误码。
"""

ZH = {
    "inbox.cs.route_offline": "ChatX聊天模型离线 · 已按标准档回复 · {hhmm}",
    "inbox.cs.route_offline_t": "本会话选了「无限制」（ChatX聊天模型），但端点连不上。为了不让客户等，这几轮按「标准」档（云端主链、规则全开）代答；端点恢复后自动回到无限制。不想再试 ChatX 就点「切回标准」",
    "inbox.cs.route_offline.hold": "ChatX聊天模型离线 · 这轮没人回 · {hhmm}",
    "inbox.cs.route_offline.hold_t": "本会话选了「无限制」（ChatX聊天模型），端点连不上，且运营关掉了离线回退（ai.unrestricted.offline_fallback=false）——按设计不回落云端，所以这轮 AI 没有回复。点「切回标准」立刻恢复自动回复",
    "inbox.cs.act.route_standard": "切回标准",
    "inbox.cs.act.route_standard_t": "把本会话的模式从「无限制」改回「标准」（云端主链、规则全开）",
    "inbox.cs.route_standard_ok": "已切回标准档",
    "inbox.cs.route_standard_fail": "切换失败",
    "inbox.route.unr_offline": "ChatX聊天模型离线（{err}）· 现在选它会按标准档代答",
    "inbox.route.unr_offline_pick": "ChatX聊天模型现在连不上，选了也会按标准档代答；端点恢复后再切",
}

EN = {
    "inbox.cs.route_offline": "ChatX chat model offline · answered on Standard · {hhmm}",
    "inbox.cs.route_offline_t": "This thread is set to Unrestricted (ChatX chat model) but the endpoint is unreachable. So the customer is not left waiting, these turns are answered on the Standard profile (cloud main chain, all rules on); it returns to Unrestricted automatically once the endpoint is back. Click “Back to Standard” to stop trying ChatX",
    "inbox.cs.route_offline.hold": "ChatX chat model offline · nobody answered this turn · {hhmm}",
    "inbox.cs.route_offline.hold_t": "This thread is set to Unrestricted (ChatX chat model), the endpoint is unreachable, and offline fallback is disabled (ai.unrestricted.offline_fallback=false) — by design it never falls back to the cloud, so the AI did not reply this turn. Click “Back to Standard” to resume auto-replies now",
    "inbox.cs.act.route_standard": "Back to Standard",
    "inbox.cs.act.route_standard_t": "Switch this thread from Unrestricted back to Standard (cloud main chain, all rules on)",
    "inbox.cs.route_standard_ok": "Switched back to Standard",
    "inbox.cs.route_standard_fail": "Switch failed",
    "inbox.route.unr_offline": "ChatX chat model offline ({err}) · picking it now answers on Standard",
    "inbox.route.unr_offline_pick": "ChatX chat model is unreachable right now; even if selected, replies use Standard. Switch once the endpoint is back",
}

ZH_HANT = {
    "inbox.cs.route_offline": "ChatX聊天模型離線 · 已按標準檔回覆 · {hhmm}",
    "inbox.cs.route_offline_t": "本會話選了「無限制」（ChatX聊天模型），但端點連不上。為了不讓客戶等，這幾輪按「標準」檔（雲端主鏈、規則全開）代答；端點恢復後自動回到無限制。不想再試 ChatX 就點「切回標準」",
    "inbox.cs.route_offline.hold": "ChatX聊天模型離線 · 這輪沒人回 · {hhmm}",
    "inbox.cs.route_offline.hold_t": "本會話選了「無限制」（ChatX聊天模型），端點連不上，且營運關掉了離線回退（ai.unrestricted.offline_fallback=false）——按設計不回落雲端，所以這輪 AI 沒有回覆。點「切回標準」立刻恢復自動回覆",
    "inbox.cs.act.route_standard": "切回標準",
    "inbox.cs.act.route_standard_t": "把本會話的模式從「無限制」改回「標準」（雲端主鏈、規則全開）",
    "inbox.cs.route_standard_ok": "已切回標準檔",
    "inbox.cs.route_standard_fail": "切換失敗",
    "inbox.route.unr_offline": "ChatX聊天模型離線（{err}）· 現在選它會按標準檔代答",
    "inbox.route.unr_offline_pick": "ChatX聊天模型現在連不上，選了也會按標準檔代答；端點恢復後再切",
}
