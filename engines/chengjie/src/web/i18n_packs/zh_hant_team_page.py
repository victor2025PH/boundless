# -*- coding: utf-8 -*-
"""繁體中文 (zh_hant) 使用者管理域人工詞條（用戶管理 P3-20，2026-09-11）。

OpenCC s2twp 把「权限」轉成「許可權」（台灣軟體介面慣用「權限」），使用者管理頁
角色名 / 權限覆寫編輯器整片受影響；此處人工轉正。scripts/i18n_hant.py 已加術語釘
「許可權→權限」，下次 regen 全站一致，且 regen 不會復活已轉正鍵。
鍵須已存在於 ZH/EN（web_i18n.py 單體 / team_page.py）。
"""

ZH_HANT = {
    "role_master": "主帳號（全部權限）",
    "role_admin": "管理員（編輯權限）",
    "us_s003": "管理後台帳號、角色與權限",
    "tq_perm_btn": "權限",
    "tq_perm_title": "按人權限覆寫",
    "tq_perm_hint": "覆寫只對該帳號生效；「繼承預設」跟隨角色權限。",
    "tq_perm_saved": "權限已儲存",
    "tq_perm_load_fail": "權限介面未就緒（服務重啟後可用）",
    "err.team.bad_perms": "權限提交無效（未知能力鍵，或同一能力同時出現在允許與禁止）",
    # 本批新增鍵（尚未 regen 進 zh_hant_auto）
    "us_role_desc_master": "擁有全部權限，含授權、設定與團隊管理。",
    "us_role_desc_admin": "管理後台設定、知識庫與團隊成員；不能建立同級管理員。",
    "us_role_desc_supervisor": "坐席工作台 + 團隊看板與質檢；可管理坐席。",
    "us_role_desc_agent": "只用聊天工作台處理會話，不進後台。",
    "us_role_desc_viewer": "唯讀檢視後台資料，不能改設定。",
    "us_role_change_confirm": "把 @{name} 的角色改為「{role}」？權限立即生效。",
    "err.team.master_no_delete": "無法刪除（主帳號不可刪除）",
    "us_backend_lbl": "帳號所在後台",
    "us_backend_t": "子帳號只存在於這台後台；坐席機若連的是另一台後台（各自捆綁的 backend），要在那台上建立",
    "us_what_is": "這是什麼帳號？",
    "us_search_ph": "搜尋使用者名稱 / 顯示名",
    "us_filter_all": "全部",
    "us_filter_disabled": "已停用",
    "us_showing": "顯示 {shown} / {total}",
    "us_empty_filter": "沒有符合的帳號",
    "us_reset_pwd": "重設密碼",
    "us_copy_login": "複製登入資訊",
    "us_more": "更多操作",
    "us_never_badge": "從未登入",
    "us_never_t": "該帳號還沒登入過——用「複製登入資訊」把登入網址和使用者名稱發給對方；忘記密碼可「重設密碼」",
    "us_pwd_title": "重設密碼",
    "us_pwd_new": "新密碼",
    "us_pwd_gen": "產生",
    "us_pwd_hint": "儲存後舊密碼立即失效，請把新密碼交給對方。已登入的裝置不會被踢出；需要的話在下方「已登入的裝置」裡踢出。",
    "us_pwd_saved": "密碼已重設",
    "us_cred_title": "登入資訊（只顯示這一次）",
    "us_cred_url": "登入網址",
    "us_cred_user": "使用者名稱",
    "us_cred_pwd": "密碼",
    "us_cred_note": "把這幾行發給對方；關閉後不再顯示密碼。在智聊桌面端登入：先點右上角頭像 → 退出登入，再用這個帳號登入。",
    "us_copy_all": "複製全部",
    "us_copied": "已複製",
    "us_copy_fail": "複製失敗，請手動選取文字複製",
    "us_del_hint": "刪除後該帳號的登入立即失效；其經手的聊天記錄保留。",
    "us_create_hint": "建立成功後會顯示一次登入資訊，請當場複製給對方。",
    "us_sess_for": "帳號",
}
