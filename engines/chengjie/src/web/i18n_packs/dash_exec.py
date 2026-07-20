# -*- coding: utf-8 -*-
"""概览页「经营摘要」执行条词条（三期功能；七期从单体迁入 pack）。

背景：这些键最初写在 web_i18n.py 单体字典，被并行工作流的整体保存覆写丢失
（单体「后写覆盖先写」事故，pack 机制即为此而生）。恢复至此，单一事实来源。
消费方：templates/workspace_dashboard.html（#db-exec 执行条）。
"""

ZH = {
    "dash.exec.title": "经营摘要",
    "dash.exec.ai_share": "AI 分担率",
    "dash.exec.ai_split": "AI {ai} · 人工 {human}",
    "dash.exec.saved": "节省人时",
    "dash.exec.saved_note": "按每条人工回复 {sec}s 折算",
    "dash.exec.attain": "今日 SLA 达标率",
    "dash.exec.overdue": "超时待处理",
    "dash.exec.revenue": "近 30 天营收",
    "dash.exec.rev_sub": "{n} 笔 · {m} 订阅",
    "dash.exec.risk": "风控状态",
    "dash.exec.risk_ok": "正常",
    "dash.exec.risk_warn": "需关注",
    "dash.exec.risk_frozen": "已冻结",
    "dash.exec.risk_unknown": "未知",
}

EN = {
    "dash.exec.title": "Executive summary",
    "dash.exec.ai_share": "AI share",
    "dash.exec.ai_split": "AI {ai} · human {human}",
    "dash.exec.saved": "Hours saved",
    "dash.exec.saved_note": "Estimated at {sec}s per manual reply",
    "dash.exec.attain": "SLA attainment today",
    "dash.exec.overdue": "Overdue open",
    "dash.exec.revenue": "Revenue (30d)",
    "dash.exec.rev_sub": "{n} tx · {m} subs",
    "dash.exec.risk": "Risk status",
    "dash.exec.risk_ok": "Healthy",
    "dash.exec.risk_warn": "Attention",
    "dash.exec.risk_frozen": "Frozen",
    "dash.exec.risk_unknown": "Unknown",
}

VI = {
    "dash.exec.title": "Tóm tắt kinh doanh",
    "dash.exec.ai_share": "Tỷ lệ AI đảm nhận",
    "dash.exec.saved": "Giờ công tiết kiệm",
    "dash.exec.attain": "Đạt SLA hôm nay",
    "dash.exec.overdue": "Quá hạn chưa xử lý",
    "dash.exec.revenue": "Doanh thu (30 ngày)",
    "dash.exec.risk": "Trạng thái rủi ro",
    "dash.exec.risk_ok": "Bình thường",
    "dash.exec.risk_warn": "Cần chú ý",
    "dash.exec.risk_frozen": "Đã đóng băng",
    "dash.exec.risk_unknown": "Không rõ",
}
