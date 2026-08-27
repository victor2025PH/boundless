# -*- coding: utf-8 -*-
"""ops-overview「🎨 手动出图」卡词条（P2 2026-08-22）。

坐席「AI 生成图片」漏斗读数面：尝试/成功率/时延/失败码分布/相册秒发占比。
数据源 = /api/workspace/metrics.image_gen（进程口径）。
"""

ZH = {
    "ov2_s_imgen": "手动出图（坐席 AI 生成图片）",
    "ov2_ig_total": "生成尝试",
    "ov2_ig_okrate": "成功率",
    "ov2_ig_p50": "时延 p50",
    "ov2_ig_p95": "时延 p95",
    "ov2_ig_sent": "已发给客户",
    "ov2_ig_fails": "失败原因",
    "ov2_ig_srcsplit": "发送来源",
    "ov2_ig_src_gen": "现场生成",
    "ov2_ig_src_album": "相册秒发",
    "ov2_ig_stock": "存货命中（有货/查询）",
    "ov2_ig_cancel": "坐席取消",
    "ov2_ig_none": "本进程暂无明细",
}

EN = {
    "ov2_s_imgen": "Manual image gen (agent AI images)",
    "ov2_ig_total": "Attempts",
    "ov2_ig_okrate": "Success rate",
    "ov2_ig_p50": "Latency p50",
    "ov2_ig_p95": "Latency p95",
    "ov2_ig_sent": "Sent to customers",
    "ov2_ig_fails": "Failure codes",
    "ov2_ig_srcsplit": "Send source",
    "ov2_ig_src_gen": "Live render",
    "ov2_ig_src_album": "Album instant",
    "ov2_ig_stock": "Stock hits (had/queried)",
    "ov2_ig_cancel": "Agent cancels",
    "ov2_ig_none": "No detail yet in this process",
}
