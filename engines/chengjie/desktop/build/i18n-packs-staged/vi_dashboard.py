# -*- coding: utf-8 -*-
"""越南语 (vi) 主管看板高频率词条子集（P4 Phase 4）。

仅定义 ``VI`` —— 键须已存在于 ZH/EN（单体或其它 pack）；合并时
``{**en_merged, **vi}``，缺键自动回落英文。
消费方：templates/workspace_dashboard.html（/workspace/dash）。
"""

VI = {
    # ── 页面标题 / 导航 ──
    "dash.back": "← Quay lại bàn làm việc",
    "dash.title": "Bảng điều khiển",
    "dash.tab.today": "Hôm nay",
    "dash.tab.quality": "Chất lượng",
    "dash.tab.biz": "Kinh doanh",
    "dash.tab.system": "Hệ thống",
    # ── 时间范围 ──
    "dash.range.7d": "7 ngày qua",
    "dash.range.30d": "30 ngày qua",
    "dash.range.90d": "90 ngày qua",
    "dash.range_t": "Khoảng thời gian dữ liệu (ảnh hưởng xu hướng/hiệu suất)",
    "dash.period.today": "Hôm nay",
    "dash.period.week": "Tuần này",
    "dash.period.month": "Tháng này",
    # ── 操作按钮 ──
    "dash.quick_approve": "Gửi tự động tất cả",
    "dash.quick_approve_t": "Gửi một lần tất cả phản hồi AI rủi ro thấp đang chờ",
    "dash.export": "Xuất CSV",
    "dash.refresh": "Làm mới",
    "dash.btn.push": "Đẩy",
    "dash.btn.create": "Tạo",
    # ── 区块标题 (dash.sec.*) ──
    "dash.sec.trend": "Xu hướng (Khách mới / Lead / Chuyển giao)",
    "dash.sec.frt": "Xu hướng tỷ lệ trả lời đúng hạn (trả lời lần đầu trong thời gian mục tiêu)",
    "dash.sec.res": "Xu hướng thời gian giải quyết (tin nhắn đầu → chuyển giao đã gửi, phút)",
    "dash.sec.xlate": "Tổng quan đa ngôn ngữ (theo khoảng thời gian đã chọn)",
    "dash.sec.agent_frt": "Hiệu suất trả lời lần đầu của đại lý (khoảng đã chọn, theo gửi thủ công)",
    "dash.sec.sla_agents": "Ai đang xử lý (chờ trả lời, theo đại lý đã nhận)",
    "dash.sec.esc": "Hội thoại quá hạn chưa ai nhận",
    "dash.sec.agents": "Khối lượng công việc đại lý (theo dõi chưa hoàn thành)",
    "dash.sec.stages": "Phân bổ giai đoạn khách hàng",
    "dash.sec.risk": "Phân bổ rủi ro trả lời AI (góc nhìn supervisor)",
    "dash.sec.presence": "Trạng thái trực tuyến nhóm",
    "dash.sec.metrics": "Chỉ số hệ thống",
    "dash.sec.myperf": "Hiệu suất của tôi",
    "dash.sec.qtrend": "Xu hướng chất lượng",
    "dash.sec.leaderboard": "Bảng xếp hạng hiệu suất",
    "dash.sec.report": "Báo cáo công việc",
    "dash.sec.workspace": "Quản lý workspace",
    "dash.sec.workload": "Cân bằng tải",
    "dash.sec.quality": "Chất lượng & cơ sở kiến thức",
    "dash.sec.health": "Sức khỏe hệ thống",
    # ── KPI 卡片 ──
    "dash.card.due_mine": "Theo dõi đến hạn của tôi",
    "dash.card.waiting": "Chờ trả lời",
    "dash.card.breaching": "Quá hạn",
    "dash.card.critical": "Nghiêm trọng",
    "dash.card.frt_avg": "Trả lời lần đầu TB (hôm nay)",
    "dash.card.new_contacts": "Khách mới (hôm nay)",
    "dash.card.leads": "Lead (hôm nay)",
    "dash.card.handoffs": "Chuyển giao (hôm nay)",
    "dash.card.done": "Đang trong phễu",
    "dash.card.attain_rate": "Tỷ lệ trả lời đúng hạn (hôm nay)",
    "dash.card.resolved": "Đã giải quyết (hôm nay)",
    # ── 加载 / 空状态 ──
    "dash.loading": "Đang tải…",
    "dash.load_fail": "Tải thất bại",
    "dash.no_data": "Chưa có dữ liệu",
    "dash.none": "Không có",
    # ── 我的绩效 ──
    "dash.mp.total": "Đã xử lý",
    "dash.mp.approved": "Đã duyệt",
    "dash.mp.rejected": "Đã từ chối",
    "dash.mp.rank": "Xếp hạng CSAT: ",
    "dash.mp.people": " đại lý",
    "dash.mp.vol_trend": "Xu hướng khối lượng",
    # ── 一键自动发送 ──
    "dash.qa.confirm": "Gửi ngay tất cả phản hồi AI rủi ro thấp đang chờ?",
    "dash.qa.sent": "Đã gửi {sent} phản hồi rủi ro thấp",
    "dash.qa.errors": " ({n} thất bại)",
    "dash.qa.fail": "Thao tác thất bại, vui lòng thử lại",
    # ── 链接 ──
    "dash.link.esc_hist": "Xem lịch sử / độ trễ tiếp quản →",
    "dash.link.pending_drafts": "Xem bản nháp chờ duyệt →",
    # ── 质量 / 工作区 ──
    "dash.q.dist_title": "Phân bổ điểm chất lượng bản nháp",
    "dash.q.kb_title": "Tỷ lệ trúng đích gợi ý KB",
    "dash.ws.new_title": "Tạo/cập nhật workspace",
    # ── 弹窗 / 明细 / 跟进 ──
    "dash.modal.title": "Chi tiết",
    "dash.mk_task": "Tạo theo dõi",
    "dash.created_ok": "✓ Đã tạo",
    "dash.task_ok": "Đã tạo nhiệm vụ theo dõi: {name}",
    "dash.task_fail_generic": "Tạo thất bại",
    "dash.unassigned": "Chưa nhận",
    "dash.unknown": "Không rõ",
    "dash.scope.waiting": "Chờ trả lời",
    "dash.scope.breaching": "Quá hạn",
    "dash.scope.critical": "Nghiêm trọng",
    "dash.scope.unresponded": "Chưa phản hồi hôm nay",
    # ── 质量趋势 / 排行榜 ──
    "dash.tr.csat": "Xu hướng CSAT",
    "dash.tr.risk_pct": "Xu hướng tỷ lệ L3/L4 (%)",
    "dash.tr.cur_avg": "TB kỳ này: ",
    "dash.tr.prev_avg": "TB kỳ trước: ",
    "dash.lb.empty": "Chưa có dữ liệu (không có xử lý trong kỳ này)",
    "dash.lb.rank": "Hạng",
    "dash.col.agent": "Đại lý",
    "dash.lb.handled": "Đã xử lý",
    "dash.lb.auto": "Tự động",
    # ── 团队在线 ──
    "dash.pr.online": "Trực tuyến",
    "dash.pr.busy": "Bận",
    "dash.pr.away": "Vắng mặt",
    "dash.pr.offline": "Ngoại tuyến",
    # ── 升级 / 风险 ──
    "dash.esc.reassign": "Chuyển giao",
    "dash.esc.assigned_me": " ★ Giao cho tôi",
    "dash.esc.reassign_prompt": "Chuyển giao cho agent_id supervisor (để trống để hủy):",
    "dash.risk.need_review": "Cần duyệt",
    # ── 系统指标 pill ──
    "dash.m.running": "Đang chạy",
    "dash.m.stopped": "Đã dừng",
    "dash.m.off": "Tắt",
    "dash.m.lbl_autosend": "Tự động gửi",
    "dash.m.lbl_sla": "Giám sát SLA",
    "dash.m.lbl_claim": "Tự động phân công",
}
