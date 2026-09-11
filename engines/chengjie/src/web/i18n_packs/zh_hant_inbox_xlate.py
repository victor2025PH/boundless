# -*- coding: utf-8 -*-
"""繁體中文 (zh_hant) 對話翻譯域人工詞條（#276 Q-16，2026-09-11）。

「發→X」會話級作用域文案 / 空檔＝自動跟對方 / 目標語≠客戶語言阻斷確認。
人工詞包優先於 zh_hant_auto.py（regen 不會復活已轉正鍵）；鍵須已存在於 ZH/EN
（inbox_workspace.py）。門禁：tests/test_i18n_zh_hant.py + tests/test_i18n_extra_langs.py。
"""

ZH_HANT = {
    "inbox.xl.out_auto_peer": "自動 · 跟對方：{lang}",
    "inbox.xl.out_auto_peer_unk": "自動 · 跟對方語言",
    "inbox.xl.out_auto_peer_sub": "未設本會話目標語：AI 自動回覆跟對方語言；手發原樣傳送",
    "inbox.xl.dir_scope_conv": "本會話",
    "inbox.xl.dir_scope_t": "收→ 全域預設 / 發→ 僅本會話",
    "inbox.xl.confirm_mismatch": "將譯成 {to} 發給 {cust} 客戶，確定傳送？",
}
