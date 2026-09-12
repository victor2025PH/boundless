# -*- coding: utf-8 -*-
"""繁體中文 (zh_hant) 會話語言計劃 chip 人工詞條（Q-21 B · #302 / Y82GWM，2026-09-12）。

「對方語言未知 · 按人設語言（X）回」：對方消息判不出語種時不再靜默轉人審，會話頭（lp-bar，
Q-26 狀態帶在場時由狀態帶說）與稿頭（lp-cdraft）明說原因。
人工詞包優先於 zh_hant_auto.py（regen 不會復活已轉正鍵）；鍵須已存在於 ZH/EN
（inbox_workspace.py ``inbox.lp.*``）。門禁：tests/test_i18n_zh_hant.py。
"""

ZH_HANT = {
    "inbox.lp.fallback_persona": "對方語言未知 · 按人設語言（{lang}）回",
    "inbox.lp.fallback_default": "對方語言未知 · 按帳號預設語言（{lang}）回",
    "inbox.lp.chip_t": "對方消息裡判不出語言（首條問候 / 表情 / 圖片），本輪按人設預設語言擬稿；對方一開口就會跟隨對方語言",
}
