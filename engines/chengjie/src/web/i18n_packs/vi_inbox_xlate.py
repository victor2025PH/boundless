# -*- coding: utf-8 -*-
"""越南语 (vi) 对话翻译域词条子集（xlate P2，2026-08-16）。

覆盖坐席翻译工作流的全部高频面：翻译弹层（一键开关/方向卡片/发送方式/高级折叠/
单次工具）、默认语言管理弹窗、手发语言错配警示条、语言名（dash.lang.*）与弹层
用到的通用按钮词。仅定义 ``VI``——键须已存在于 ZH/EN（单体或其它 pack），合并时
``{**en_merged, **vi}``，缺键自动回落英文（错误类长文案刻意留英文回落，先覆盖
坐席每天看得见的部分）。

门禁：``tests/test_i18n_vi_xlate_pack.py``（VI 键 ⊆ ZH 合并视图 + 关键键在 vi
视图可解析 + 与其它 vi pack 零冲突由 collect_packs fail-fast 兜底）。
"""

VI = {
    # ── 底部工具栏入口 / 状态胶囊 ──
    "inbox.rt.xlate": "Dịch ▾",
    "inbox.rt.xlate_t": "Dịch hai chiều / dịch ảnh / dịch giọng nói",
    "inbox.rt.xl_status_off": "Chưa bật",
    "inbox.rt.xl_status_t": "Nhấn để cài đặt dịch hội thoại",
    "inbox.xls.recv": "Nhận→",
    "inbox.xls.send": "Gửi→",
    "inbox.xls.auto": "Tự động",
    # ── 弹层：标题 / 一键开关 / 提示条 ──
    "inbox.xl.pop_hd": "Dịch hội thoại",
    "inbox.xl.quick_on": "Bật một chạm",
    "inbox.xl.quick_on_done": "✓ Đã bật",
    "inbox.xl.quick_on_t": "Bật hai chiều một chạm: tin của khách → ngôn ngữ của bạn, tin của bạn → tự động theo ngôn ngữ khách",
    "inbox.xl.quick_off_t": "Nhấn để tắt dịch hai chiều (cả hai chiều cùng tắt)",
    "inbox.xl.toast_on": "Đã bật dịch hội thoại: tin của khách → {inn} · tin của tôi → {out}",
    "inbox.xl.toast_off": "Đã tắt dịch hội thoại (cả hai chiều)",
    "inbox.xl.same_hint": "Khách cũng đang dùng {lang}: tin cùng ngôn ngữ giữ nguyên văn, ngôn ngữ khác vẫn tự dịch sang {lang}.",
    "inbox.xl.suggest": "Khách đang chat bằng {cust}. Bật dịch hai chiều {cust}⇄{mine}?",
    "inbox.xl.suggest_btn": "Bật ngay",
    # ── 方向卡片 ──
    "inbox.xl.recv": "Tin của khách",
    "inbox.xl.send_lbl": "Tin của tôi",
    "inbox.xl.in_t": "Tự động dịch tin nhắn của khách sang",
    "inbox.xl.out_t": "Dịch tin nhắn của bạn sang ngôn ngữ này trước khi gửi",
    "inbox.xl.none": "Không dịch",
    "inbox.xl.auto_cust": "Tự động (ngôn ngữ của khách)",
    # ── 发送方式分段 ──
    "inbox.xl.sendmode": "Cách gửi",
    "inbox.xl.mode_direct": "Gửi ngay",
    "inbox.xl.mode_direct_t": "Khi gửi tự động dịch sang ngôn ngữ của khách, không cần bước xác nhận",
    "inbox.xl.mode_preview": "Xem trước rồi gửi",
    "inbox.xl.preview_t": "Khi bật, gửi gồm hai bước: xem bản dịch trước rồi xác nhận gửi",
    # ── 高级折叠：引擎 / 我的语言 / 运营默认 ──
    "inbox.xl.adv": "Nâng cao: engine / ngôn ngữ mặc định / ngôn ngữ của tôi",
    "inbox.xl.eng_pick": "Engine",
    "inbox.xl.eng_pick_t": "Engine dịch ưu tiên của hội thoại này: Tự động = chuyển dự phòng thông minh; engine đã chọn sẽ được ghi nhớ (giữ qua refresh), khi lỗi vẫn tự chuyển engine khác",
    "inbox.xl.eng_auto": "Tự động",
    "inbox.xl.eng_ai": "AI",
    "inbox.xl.ai_engine": "Engine AI",
    "inbox.xl.mylang": "Ngôn ngữ của tôi",
    "inbox.xl.mylang_follow": "Theo trình duyệt (tự động)",
    "inbox.xl.mylang_saved": "Đã lưu ngôn ngữ của tôi: {lang}",
    "inbox.xl.mylang_t": "Ngôn ngữ mẹ đẻ/làm việc của bạn: dùng cho「Bật một chạm」và mặc định bản dịch; lưu trên máy chủ, đổi máy không mất",
    "inbox.xl.set_default": "Đặt làm mặc định",
    "inbox.xl.scope_account": "Tài khoản này",
    "inbox.xl.scope_platform": "Nền tảng này",
    "inbox.xl.scope_global": "Toàn cục",
    "inbox.xl.scope_t": "Phạm vi áp dụng mặc định",
    "inbox.xl.save_lang": "Lưu ngôn ngữ hiện tại",
    "inbox.xl.save_lang_t": "Lưu ngôn ngữ「Tin của khách →」hiện tại làm mặc định cho phạm vi này; đổi máy/đổi nhân viên vẫn hiệu lực",
    "inbox.xl.manage": "Quản lý",
    "inbox.xl.manage_t": "Xem/quản lý tất cả ngôn ngữ dịch mặc định đã cấu hình (toàn cục / từng nền tảng / từng tài khoản)",
    # ── 顶部译文条 / 消息内译文 ──
    "inbox.xl.label": "Bản dịch",
    "inbox.xl.lang_t": "Ngôn ngữ của bản dịch hiển thị dưới mỗi tin nhắn (đồng bộ với「Tin của khách →」)",
    "inbox.xl.show": "Hiện bản dịch",
    "inbox.xl.show_t": "Ẩn/hiện tạm thời các dòng bản dịch (không đổi cài đặt, tiết kiệm không gian)",
    "inbox.xl.identity_eq": "≈ Trùng bản gốc, không cần dịch",
    "inbox.xl.failed": "Dịch thất bại",
    "inbox.xl.unavailable": "Dịch không khả dụng",
    "inbox.xl.fill_input": "Điền vào ô nhập",
    "inbox.xl.warn_badge": "⚠ Kiểm tra lại",
    "inbox.xl.conf_badge_low": "⚠ Độ tin cậy thấp",
    # ── 单次翻译工具 ──
    "inbox.xl.tools_lbl": "Công cụ dịch một lần",
    "inbox.xl.img": "Dịch ảnh",
    "inbox.xl.img_t": "Tải ảnh/ảnh chụp màn hình của khách, OCR nhận dạng và dịch",
    "inbox.xl.img_panel": "Nhận dạng ảnh + dịch",
    "inbox.xl.voice": "Dịch giọng nói",
    "inbox.xl.voice_t": "Tải tin voice của khách, chuyển thành chữ và dịch",
    "inbox.xl.voice_panel": "Chuyển chữ + dịch giọng nói",
    "inbox.xl.compare": "So sánh nhiều engine",
    "inbox.xl.compare_t": "Mỗi engine dịch dịch một bản, chọn bản ưng ý nhất để điền vào ô nhập",
    "inbox.xl.doc": "Dịch tài liệu",
    "inbox.xl.doc_t": "Dán văn bản dài hoặc tải .txt, dịch cả bài và giữ định dạng",
    "inbox.xl.doc_panel": "Dịch cả tài liệu",
    # ── 默认语言管理弹窗 ──
    "inbox.dlm.tx_hd": "Ngôn ngữ dịch mặc định · cấu hình vận hành",
    "inbox.dlm.reply_hd": "💬 Ngôn ngữ trả lời mặc định · bản nháp desktop",
    "inbox.dlm.priority": "Ưu tiên: tài khoản > nền tảng > toàn cục. Đổi máy/đổi nhân viên vẫn hiệu lực.",
    "inbox.dlm.reply_sub": "Chiều gửi đi: dịch bản nháp của nhân viên sang ngôn ngữ khách. Ưu tiên: tài khoản > nền tảng > toàn cục. Desktop dùng mặc định này khi hội thoại chưa có ghi nhớ (trống = theo persona/khách).",
    "inbox.dlm.tx_empty": "Chưa cấu hình ngôn ngữ dịch mặc định. Đặt ngay ở dòng「Thêm / sửa」bên dưới; hoặc chọn ngôn ngữ「Tin của khách →」trong popup dịch rồi nhấn「Lưu ngôn ngữ hiện tại」.",
    "inbox.dlm.reply_empty": "Chưa có ngôn ngữ trả lời mặc định. Dùng dòng「Thêm / sửa」bên dưới để đặt cho toàn cục/nền tảng/tài khoản.",
    "inbox.dlm.add_lbl": "Thêm / sửa",
    "inbox.dlm.by": "bởi ",
    "inbox.dlm.clear": "Xóa",
    "inbox.dlm.eff": "Đang áp dụng cho hội thoại này",
    "inbox.dlm.eff_none": "chưa cấu hình (dùng mặc định của máy này)",
    "inbox.dlm.eff_none_reply": "chưa cấu hình (theo persona/khách)",
    "inbox.dlm.follow": "Theo persona/khách",
    "inbox.dlm.follow_clear": "Theo persona/khách (xóa)",
    "inbox.dlm.no_targets": "Chưa có tài khoản để chọn: hãy khởi động tài khoản nền tảng hoặc chọn một hội thoại trước",
    "inbox.dlm.none_clear": "Không đặt mặc định (xóa phạm vi này)",
    "inbox.dlm.pick_account": "Tài khoản",
    "inbox.dlm.pick_platform": "Nền tảng",
    "inbox.dlm.scope_account": "Tài khoản · ",
    "inbox.dlm.scope_platform": "Nền tảng · ",
    # ── 默认语言保存/清除 toast ──
    "inbox.dlang.tx_saved": "Đã lưu ngôn ngữ dịch mặc định",
    "inbox.dlang.reply_saved": "Đã lưu ngôn ngữ trả lời mặc định",
    "inbox.dlang.cleared": "Đã xóa",
    "inbox.dlang.cleared_default": "Đã xóa mặc định",
    "inbox.dlang.clear_fail": "Xóa thất bại",
    "inbox.dlang.set_default": "Đã đặt mặc định → {lang}",
    "inbox.dlang.save_fail": "Lưu thất bại: {err}",
    "inbox.dlang.save_fail_hint": "Lưu thất bại",
    "inbox.dlang.select_hint": "Hãy chọn hội thoại trước",
    "inbox.dlang.select_locate": "Hãy chọn hội thoại để xác định nền tảng/tài khoản",
    "inbox.dlang.net_hint": "Lỗi mạng",
    "inbox.dlang.unknown": "không rõ",
    # ── 手发语言错配警示条 ──
    "inbox.langwarn.msg": "Khách dùng {lang}, bạn đang nhập tiếng Trung — gửi thẳng khách sẽ nhận nguyên văn",
    "inbox.langwarn.fix": "Bật dịch tự động",
    "inbox.langwarn.fix_t": "Khi gửi tự động dịch sang ngôn ngữ của khách (giống chọn「Tin của tôi → Tự động」)",
    "inbox.langwarn.dismiss_t": "Không nhắc lại trong hội thoại này",
    # ── 语言名（弹层下拉/建议条/toast 共用）──
    "dash.lang.zh": "Tiếng Trung",
    "dash.lang.en": "Tiếng Anh",
    "dash.lang.th": "Tiếng Thái",
    "dash.lang.vi": "Tiếng Việt",
    "dash.lang.id": "Tiếng Indonesia",
    "dash.lang.ja": "Tiếng Nhật",
    "dash.lang.ko": "Tiếng Hàn",
    "dash.lang.ru": "Tiếng Nga",
    "dash.lang.es": "Tiếng Tây Ban Nha",
    "dash.lang.pt": "Tiếng Bồ Đào Nha",
    # 注：inbox.send/save/close 与 inbox.toast.net_err/select_first 已由
    # vi_workspace_shell 覆盖（VI 跨包同键 fail-fast，勿在此重复定义）。
}
