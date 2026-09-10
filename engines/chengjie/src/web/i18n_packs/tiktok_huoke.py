# -*- coding: utf-8 -*-
"""TikTok 个人号真机桥闸门人话（TK-3 B6，2026-09-10）。

拦因族谱见 ``send_gate_status.blocked_reason_key``：消息请求未回 / 对方沉默 /
日上限 / 夜间静默 / 一入站一条 / 评论超长 / 真机离线。
六语（zh / en / zh_hant / vi / th / id）同文件，避免碰 ``zh_hant_auto`` / 机翻底稿。
``inbox.failr.*`` 给失败气泡；``err.inbox.send_blocked_*`` 给 409 横幅。
"""

ZH = {
    "err.inbox.send_blocked_policy_message_request":
        "对方还没回过这条私信（消息请求未打开），智聊不会代发——等客户先开口，或让获客真机做首触。",
    "err.inbox.send_blocked_policy_peer_silent":
        "对方超过 72 小时没回。自动回复已停；人工最多再补一条，再发等客户开口。",
    "err.inbox.send_blocked_policy_daily_cap":
        "该 TikTok 个人号今日私信/评论回复次数已用完（{reason}），请明天再发或调高日上限。",
    "err.inbox.send_blocked_policy_quiet_hours":
        "现在是该账号的休息时间，自动回复已静默；要现在发请改用人工发送。",
    "err.inbox.send_blocked_policy_one_reply":
        "这条评论已经回过一次（公开可见，多回像刷屏）。等对方再评一条后再回。",
    "err.inbox.send_blocked_policy_comment_len":
        "评论超过 TikTok 150 字上限（{reason}），请删短后再发。",
    "err.inbox.send_blocked_device_offline":
        "获客真机超过 2 小时没有心跳，消息发不出去——请检查手机上的获客 App 是否在跑。",
    "inbox.failr.policy_message_request": "消息请求未打开：对方还没回过，智聊不代发首触",
    "inbox.failr.policy_peer_silent": "对方超过 72 小时未回，自动已停、人工只能再补一条",
    "inbox.failr.policy_daily_cap": "该号今日回复次数已用完",
    "inbox.failr.policy_quiet_hours": "休息时间，自动回复已静默",
    "inbox.failr.policy_one_reply": "这条评论已经回过一次",
    "inbox.failr.policy_comment_len": "评论超过 150 字",
    "inbox.failr.device_offline": "获客真机离线，请检查手机",
}

EN = {
    "err.inbox.send_blocked_policy_message_request":
        "This DM is still a message request (they haven't replied). ChatX will not send first — wait for them to write, or let the phone bot do the first touch.",
    "err.inbox.send_blocked_policy_peer_silent":
        "They haven't replied in over 72 hours. Auto-reply is paused; a human may send one more message, then wait for them.",
    "err.inbox.send_blocked_policy_daily_cap":
        "This TikTok personal account has used today's reply allowance ({reason}). Try tomorrow or raise the daily cap.",
    "err.inbox.send_blocked_policy_quiet_hours":
        "This account is off-hours; auto-replies are paused. Send manually if it must go out now.",
    "err.inbox.send_blocked_policy_one_reply":
        "This comment already has one reply (public; another looks like spam). Wait for another comment.",
    "err.inbox.send_blocked_policy_comment_len":
        "Comment is over TikTok's 150-character limit ({reason}); shorten it and resend.",
    "err.inbox.send_blocked_device_offline":
        "The capture phone has not checked in for over 2 hours — the message cannot be sent. Check that the capture app is running.",
    "inbox.failr.policy_message_request": "Message request still closed — ChatX will not send first",
    "inbox.failr.policy_peer_silent": "No reply for 72h — auto paused; human may send one more",
    "inbox.failr.policy_daily_cap": "Today's reply allowance for this account is used up",
    "inbox.failr.policy_quiet_hours": "Off-hours — auto-reply paused",
    "inbox.failr.policy_one_reply": "This comment already has one reply",
    "inbox.failr.policy_comment_len": "Comment over 150 characters",
    "inbox.failr.device_offline": "Capture phone offline — check the device",
}

ZH_HANT = {
    "err.inbox.send_blocked_policy_message_request":
        "對方還沒回過這條私信（訊息請求未打開），智聊不會代發——等客戶先開口，或讓獲客真機做首觸。",
    "err.inbox.send_blocked_policy_peer_silent":
        "對方超過 72 小時沒回。自動回覆已停；人工最多再補一條，再發等客戶開口。",
    "err.inbox.send_blocked_policy_daily_cap":
        "該 TikTok 個人號今日私信/評論回覆次數已用完（{reason}），請明天再發或調高日上限。",
    "err.inbox.send_blocked_policy_quiet_hours":
        "現在是該帳號的休息時間，自動回覆已靜默；要現在發請改用人工發送。",
    "err.inbox.send_blocked_policy_one_reply":
        "這條評論已經回過一次（公開可見，多回像刷屏）。等對方再評一條後再回。",
    "err.inbox.send_blocked_policy_comment_len":
        "評論超過 TikTok 150 字上限（{reason}），請刪短後再發。",
    "err.inbox.send_blocked_device_offline":
        "獲客真機超過 2 小時沒有心跳，訊息發不出去——請檢查手機上的獲客 App 是否在跑。",
    "inbox.failr.policy_message_request": "訊息請求未打開：對方還沒回過，智聊不代發首觸",
    "inbox.failr.policy_peer_silent": "對方超過 72 小時未回，自動已停、人工只能再補一條",
    "inbox.failr.policy_daily_cap": "該號今日回覆次數已用完",
    "inbox.failr.policy_quiet_hours": "休息時間，自動回覆已靜默",
    "inbox.failr.policy_one_reply": "這條評論已經回過一次",
    "inbox.failr.policy_comment_len": "評論超過 150 字",
    "inbox.failr.device_offline": "獲客真機離線，請檢查手機",
}

VI = {
    "err.inbox.send_blocked_policy_message_request":
        "Đây vẫn là lời mời nhắn tin (họ chưa trả lời). ChatX không gửi trước — đợi họ viết, hoặc để máy thật chạm lần đầu.",
    "err.inbox.send_blocked_policy_peer_silent":
        "Họ chưa trả lời hơn 72 giờ. Tự động đã dừng; người chỉ được gửi thêm một tin, rồi đợi họ.",
    "err.inbox.send_blocked_policy_daily_cap":
        "Tài khoản TikTok cá nhân này đã hết hạn mức trả lời hôm nay ({reason}). Thử lại ngày mai hoặc tăng hạn mức.",
    "err.inbox.send_blocked_policy_quiet_hours":
        "Tài khoản đang ngoài giờ làm; tự động im lặng. Gửi thủ công nếu phải gửi ngay.",
    "err.inbox.send_blocked_policy_one_reply":
        "Bình luận này đã có một trả lời (công khai; thêm nữa giống spam). Đợi bình luận mới.",
    "err.inbox.send_blocked_policy_comment_len":
        "Bình luận vượt giới hạn 150 ký tự của TikTok ({reason}); hãy rút ngắn rồi gửi lại.",
    "err.inbox.send_blocked_device_offline":
        "Máy thật hơn 2 giờ không nhịp tim — tin không gửi được. Kiểm tra app thu hút trên điện thoại.",
    "inbox.failr.policy_message_request": "Lời mời nhắn tin chưa mở — ChatX không gửi trước",
    "inbox.failr.policy_peer_silent": "Hơn 72 giờ chưa trả lời — tự động dừng, người chỉ gửi thêm một tin",
    "inbox.failr.policy_daily_cap": "Hết hạn mức trả lời hôm nay",
    "inbox.failr.policy_quiet_hours": "Ngoài giờ — tự động im lặng",
    "inbox.failr.policy_one_reply": "Bình luận này đã có một trả lời",
    "inbox.failr.policy_comment_len": "Bình luận quá 150 ký tự",
    "inbox.failr.device_offline": "Máy thật ngoại tuyến — kiểm tra điện thoại",
}

TH = {
    "err.inbox.send_blocked_policy_message_request":
        "นี่ยังเป็นคำขอข้อความ (อีกฝ่ายยังไม่ตอบ) ChatX จะไม่ส่งก่อน — รอให้ลูกค้าเขียนก่อน หรือให้มือถือจริงแตะครั้งแรก",
    "err.inbox.send_blocked_policy_peer_silent":
        "อีกฝ่ายไม่ตอบเกิน 72 ชั่วโมง ตอบอัตโนมัติหยุดแล้ว คนส่งได้อีกหนึ่งข้อความ แล้วรอ",
    "err.inbox.send_blocked_policy_daily_cap":
        "บัญชี TikTok ส่วนตัวนี้ใช้โควตารายวันนี้หมดแล้ว ({reason}) ลองพรุ่งนี้หรือเพิ่มเพดาน",
    "err.inbox.send_blocked_policy_quiet_hours":
        "นอกเวลาทำการ ตอบอัตโนมัติเงียบอยู่ ส่งด้วยมือถ้าต้องออกตอนนี้",
    "err.inbox.send_blocked_policy_one_reply":
        "คอมเมนต์นี้ตอบไปแล้วหนึ่งครั้ง (สาธารณะ ตอบซ้ำดูเหมือนสแปม) รอคอมเมนต์ใหม่",
    "err.inbox.send_blocked_policy_comment_len":
        "คอมเมนต์เกิน 150 ตัวอักษรของ TikTok ({reason}) ตัดสั้นแล้วส่งใหม่",
    "err.inbox.send_blocked_device_offline":
        "มือถือจริงไม่ส่งชีพจรเกิน 2 ชั่วโมง ส่งข้อความไม่ได้ — ตรวจแอปเก็บลีดบนมือถือ",
    "inbox.failr.policy_message_request": "คำขอข้อความยังไม่เปิด — ChatX ไม่ส่งก่อน",
    "inbox.failr.policy_peer_silent": "ไม่ตอบเกิน 72 ชม. — อัตโนมัติหยุด คนส่งได้อีกหนึ่งข้อ",
    "inbox.failr.policy_daily_cap": "โควตาตอบวันนี้หมดแล้ว",
    "inbox.failr.policy_quiet_hours": "นอกเวลา — ตอบอัตโนมัติเงียบ",
    "inbox.failr.policy_one_reply": "คอมเมนต์นี้ตอบไปแล้วหนึ่งครั้ง",
    "inbox.failr.policy_comment_len": "คอมเมนต์เกิน 150 ตัวอักษร",
    "inbox.failr.device_offline": "มือถือจริงออฟไลน์ — ตรวจอุปกรณ์",
}

ID = {
    "err.inbox.send_blocked_policy_message_request":
        "Ini masih permintaan pesan (mereka belum membalas). ChatX tidak mengirim duluan — tunggu mereka menulis, atau biarkan HP nyata menyentuh pertama.",
    "err.inbox.send_blocked_policy_peer_silent":
        "Mereka belum membalas lebih dari 72 jam. Balasan otomatis berhenti; manusia boleh kirim satu lagi, lalu tunggu.",
    "err.inbox.send_blocked_policy_daily_cap":
        "Akun TikTok pribadi ini sudah memakai jatah balasan hari ini ({reason}). Coba besok atau naikkan batas harian.",
    "err.inbox.send_blocked_policy_quiet_hours":
        "Akun sedang di luar jam kerja; balasan otomatis diam. Kirim manual jika harus keluar sekarang.",
    "err.inbox.send_blocked_policy_one_reply":
        "Komentar ini sudah punya satu balasan (publik; menambah lagi seperti spam). Tunggu komentar baru.",
    "err.inbox.send_blocked_policy_comment_len":
        "Komentar melebihi batas 150 karakter TikTok ({reason}); persingkat lalu kirim ulang.",
    "err.inbox.send_blocked_device_offline":
        "HP nyata lebih dari 2 jam tanpa detak — pesan tidak terkirim. Periksa aplikasi pengumpul di HP.",
    "inbox.failr.policy_message_request": "Permintaan pesan belum terbuka — ChatX tidak mengirim duluan",
    "inbox.failr.policy_peer_silent": "Tidak membalas 72 jam — otomatis berhenti, manusia boleh satu lagi",
    "inbox.failr.policy_daily_cap": "Jatah balasan hari ini habis",
    "inbox.failr.policy_quiet_hours": "Di luar jam — balasan otomatis diam",
    "inbox.failr.policy_one_reply": "Komentar ini sudah punya satu balasan",
    "inbox.failr.policy_comment_len": "Komentar lebih dari 150 karakter",
    "inbox.failr.device_offline": "HP nyata offline — periksa perangkat",
}
