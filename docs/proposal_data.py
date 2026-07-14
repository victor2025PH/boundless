# -*- coding: utf-8 -*-
"""
无界科技 BOUNDLESS · 全球全自动引流系统方案 —— 单一事实源（Single Source of Truth）。

网页 (public/proposal/index.html) 与 Word (generate_proposal_docx.py) 的关键事实
（费用、三档配置、产能、平台矩阵）统一从这里取，避免两处各改一处、数字漂移。

改价 / 改配置 / 改产能，只改这里；docx 直接消费，网页用 verify_proposal.py 做一致性校验。
"""

# ── 费用（我方软件报价，非硬件）──────────────────────────────
FEES = {
    "deploy": "11,999",   # 一次性部署搭建费
    "month": "3,000",     # 月度服务费
    "currency": "USDT",
}

# ── 集群与产能经验值 ────────────────────────────────────────
PER_MACHINE = 16          # 单台工作机稳定管理的真机数
CAP = {
    "reach": 650,         # 单机 · 月主动触达
    "dm": 280,            # 单机 · 月私信触达
    "scrape": 1000,       # 单机 · 月采集线索
    "lead_rate": 0.15,    # 私信触达 → 月意向对话
    "conv_rate": 0.08,    # 意向对话 → 引流转化（示例值）
}


def productivity(phones: int) -> dict:
    """按真机数估算成熟期月度产能。"""
    reach = phones * CAP["reach"]
    dm = phones * CAP["dm"]
    scrape = phones * CAP["scrape"]
    leads = round(dm * CAP["lead_rate"])
    conv = round(leads * CAP["conv_rate"])
    return {"reach": reach, "dm": dm, "scrape": scrape, "leads": leads, "conv": conv}


def fmt(n) -> str:
    return f"{n:,}"


# ── 三档硬件配置（仅配置与数量，不含价格）────────────────────
# bom: (类别, 型号/规格, 数量)
TIERS = [
    {
        "key": "starter", "label": "入门版 Starter", "phones": 30,
        "machines": 2, "workers": 1, "topo": "1 主控 + 1 Worker · 单点位",
        "bom": [
            ("主控一体机", "16 核 / 64GB / 2TB NVMe / RTX 4060Ti 16G（兼 Worker）", "1"),
            ("Worker 工作机", "8 核 / 32GB / 512GB SSD", "1"),
            ("引流真机", "Redmi 13C 全球版 8+256（Android 13/14）", "30"),
            ("工业 USB HUB", "16 口独立供电 USB 3.0 集线器", "2"),
            ("USB 数据线", "0.5m 带磁环工业级", "30"),
            ("养机充电架", "30 位多层散热机架", "1"),
            ("企业软路由", "多 WAN 负载 / 策略路由", "1"),
            ("千兆交换机", "24 口网管型", "1"),
            ("UPS 电源", "1500VA 不间断电源", "1"),
            ("SIM 卡", "目标地区本地卡（含首月流量）", "30"),
            ("辅材杂项", "理线 / 标签 / 散热风扇 / 电源排插", "1 套"),
        ],
    },
    {
        "key": "pro", "label": "专业版 Pro", "phones": 60,
        "machines": 4, "workers": 3, "topo": "1 主控 + 3 Worker · 换脸/本地 LLM",
        "bom": [
            ("主控服务器", "16 核 / 128GB / 2TB NVMe / RTX 4090 24G（换脸+本地大模型）", "1"),
            ("Worker 工作机", "8 核 / 32GB / 512GB SSD", "3"),
            ("引流真机", "Redmi 13C 全球版 8+256", "60"),
            ("工业 USB HUB", "16 口独立供电 USB 3.0", "4"),
            ("USB 数据线", "0.5m 带磁环工业级", "60"),
            ("养机充电架", "30 位多层散热机架", "2"),
            ("企业软路由", "多 WAN 负载 / 策略路由", "1"),
            ("千兆交换机", "24 口网管型", "1"),
            ("UPS 电源", "1500VA 不间断电源", "2"),
            ("SIM 卡", "目标地区本地卡（含首月流量）", "60"),
            ("辅材杂项", "理线 / 标签 / 散热 / 排插", "1 套"),
        ],
    },
    {
        "key": "ent", "label": "旗舰版 Enterprise", "phones": 120,
        "machines": 8, "workers": 7, "topo": "1 主控 + 7 Worker · 企业机房级",
        "bom": [
            ("主控服务器", "16 核 / 128GB / 2TB NVMe / RTX 4090 24G", "1"),
            ("Worker 工作机", "8 核 / 32GB / 512GB SSD", "7"),
            ("引流真机", "Redmi 13C 全球版 8+256", "120"),
            ("工业 USB HUB", "16 口独立供电 USB 3.0", "8"),
            ("USB 数据线", "0.5m 带磁环工业级", "120"),
            ("养机充电架", "30 位多层散热机架", "4"),
            ("企业软路由", "多 WAN 负载 / 策略路由", "2"),
            ("千兆交换机", "24 口网管型", "2"),
            ("UPS 电源", "1500VA 不间断电源", "3"),
            ("SIM 卡", "目标地区本地卡（含首月流量）", "120"),
            ("辅材杂项", "理线 / 标签 / 散热 / 机柜 / 排插", "1 套"),
        ],
    },
]

# 超大规模仅用于产能表参考
XLARGE_PHONES = 200

# ── 平台矩阵：(名称, 能力, 状态 live/dev) ────────────────────
PLATFORMS = [
    ("TikTok", "养号 · 采集 · 关注互动 · 私信 · 跨平台转化", "live"),
    ("Facebook", "加好友 · 打招呼 · 群成员提取 · 信息流养号", "live"),
    ("Messenger", "收件箱监控 · AI 自动回复 · 陌生人请求处理", "live"),
    ("Instagram", "关注 · 私信 · 评论区引流", "dev"),
    ("WhatsApp", "私聊承接 · 群发 · 客服应答", "dev"),
    ("Telegram", "私信 · 群组 · AI 客服（MTProto）", "dev"),
    ("Twitter / X", "关注 · 私信 · 话题触达", "dev"),
    ("LinkedIn", "B2B 触达 · 建立连接 · 私信", "dev"),
    ("LINE", "私聊承接 · 引流 · AI 客服", "dev"),
]

STATUS_LABEL = {"live": "● 已上线", "dev": "◆ 可扩展"}

# ── 已移除的硬件价格 token（一致性校验用：网页/Word 中都不应再出现）──
FORBIDDEN_PRICE_TOKENS = ["单价(USD)", "小计(USD)", "硬件预算", "硬件预算合计"]
