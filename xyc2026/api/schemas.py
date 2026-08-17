# Pydantic request/response for v1 API
from typing import Literal
from pydantic import BaseModel, Field


class TimelineDay(BaseModel):
    date: str
    queried: int = 0
    invite_clicked: int = 0
    share_viewed: int = 0


class TimelineResponse(BaseModel):
    """P1: Activity timeline response."""
    timeline: list[TimelineDay] = []


class CheckResponse(BaseModel):
    risk: str
    score: int
    tips: list[str]
    blacklist: bool
    tg_id: int | None = None
    username: str | None = None
    message: str | None = None
    first_seen: str | None = None
    report_count: int = 0
    report_pending: int = 0
    report_verified: int = 0
    is_premium: bool = False
    query_count: int = 0
    updated_at: str | None = None
    last_seen_at: str | None = None
    verdict: str = ""
    tags: list[str] = []
    linked_wallets: list[str] = []
    linked_tg_ids: list[int] = []
    linked_wallet_count: int = 0
    linked_tg_count: int = 0
    timeline: list[TimelineDay] = []
    tx_success_count: int = 0
    tx_total_count: int = 0
    tx_success_rate: float = 0.0
    tx_total_amount: float = 0.0
    high_risk_group_count: int = 0
    risk_dimensions: dict[str, str] = {}  # 阶段 C：identity, funds, community, association


class MyCreditResponse(BaseModel):
    risk: str
    score: int
    tips: list[str]
    blacklist: bool
    tg_id: int | None = None
    username: str | None = None
    first_seen: str | None = None
    report_count: int = 0
    report_pending: int = 0
    report_verified: int = 0
    is_premium: bool = False
    query_count: int = 0
    updated_at: str | None = None
    last_seen_at: str | None = None
    verdict: str = ""
    tags: list[str] = []
    linked_wallets: list[str] = []
    recent_7d_query_count: int = 0
    last_queried_at: str | None = None
    timeline: list[TimelineDay] = []
    tx_success_count: int = 0
    tx_total_count: int = 0
    tx_success_rate: float = 0.0
    tx_total_amount: float = 0.0
    notify_on_checked: bool = True


class ShareCreateResponse(BaseModel):
    token: str
    link: str
    expires_in_hours: int


class ReportCreate(BaseModel):
    reporter_tg_id: int
    target_tg_id: int
    reason: str = Field(..., min_length=1, max_length=2000)
    category: str | None = Field(None, description="scam | run_order | lost_contact | other")
    tx_hash: str | None = None
    screenshot_url: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=2000)


class RiskFactorBreakdown(BaseModel):
    report: int = 0
    blacklist: int = 0
    high_risk_groups: int = 0
    new_account: int = 0
    wallet_anomaly: int = 0


class ReportBreakdownResponse(CheckResponse):
    """V2.0: 风险画像 = check 结果 + 风险来源占比 + 举报分类与时间分布。"""
    risk_factor_breakdown: RiskFactorBreakdown | dict = Field(default_factory=dict)
    report_by_category: dict[str, int] = Field(default_factory=dict)
    last_7d_reports: int = 0
    last_30d_reports: int = 0


class ReportResponse(BaseModel):
    status: str = "received"
    report_id: int | None = None


class AdminStatsResponse(BaseModel):
    queries_today: int
    total_users: int
    blacklist_count: int
    reports_pending: int
    feedback_pending: int = 0
    transactions_pending: int = 0
    bound_wallets_count: int = 0
    users_with_flow_visible: int = 0
    flow_snapshots_count: int = 0


class BlacklistAddBody(BaseModel):
    tg_id: int
    reason: str | None = None
    source: str = "admin"


class BlacklistAddResponse(BaseModel):
    ok: bool
    added: bool  # True if newly added, False if already on blacklist


class InviteCreateResponse(BaseModel):
    token: str
    link: str
    expires_in_minutes: int


class InviteConsumeResponse(BaseModel):
    querier_tg_id: int


class RecentQueryItem(BaseModel):
    target_tg_id: int
    last_queried_at: str


class RecentQueriesResponse(BaseModel):
    items: list[RecentQueryItem]


class MyFeedbackItem(BaseModel):
    id: int
    target_tg_id: int
    reason: str
    status: str
    created_at: str | None


class MyFeedbackResponse(BaseModel):
    items: list[MyFeedbackItem]


class AdminFeedbackItem(BaseModel):
    id: int
    reporter_tg_id: int
    target_tg_id: int
    reason: str
    status: str
    created_at: str | None


class AdminFeedbackListResponse(BaseModel):
    items: list[AdminFeedbackItem]


class FeedbackUpdateBody(BaseModel):
    status: Literal["processed", "rejected"]


class FeedbackUpdateResponse(BaseModel):
    ok: bool
    message: str = ""


# ---------- 风险变化提醒 ----------
class AlertSubscribeResponse(BaseModel):
    status: str = "subscribed"
    target_tg_id: int


class AlertSubscriptionItem(BaseModel):
    target_tg_id: int
    created_at: str | None
    alert_type: str = "risk_change"  # risk_change | online_offline


class MonitorEventBody(BaseModel):
    """监控端上报：目标上下线事件（众包或专用监控）"""
    target_tg_id: int
    event_type: Literal["online", "offline"]
    reporter_tg_id: int | None = None  # 众包上报者 tg_id
    monitor_tg_id: int | None = None  # 专用监控账号（预留）
    payload: dict | None = None


class AlertSubscriptionsResponse(BaseModel):
    items: list[AlertSubscriptionItem]


class PendingAlertItem(BaseModel):
    id: int
    subscriber_tg_id: int
    target_tg_id: int
    event_type: str
    subscriber_lang: str | None = None
    created_at: str | None


class PendingAlertsResponse(BaseModel):
    items: list[PendingAlertItem]


class PendingAlertMarkSentBody(BaseModel):
    sent: bool = True


class PendingAlertMarkSentResponse(BaseModel):
    ok: bool


# ---------- V2.0 交易确认 ----------
class TransactionConfirmBody(BaseModel):
    initiator_tg_id: int
    counterparty_tg_id: int
    amount: str = Field(..., min_length=1, max_length=32)
    currency: str = Field(default="USDT", max_length=16)
    memo: str | None = Field(None, max_length=500)


class TransactionConfirmResponse(BaseModel):
    transaction_id: int
    status: str = "pending"


class TransactionRespondBody(BaseModel):
    transaction_id: int
    responder_tg_id: int
    accept: bool


class TransactionRespondResponse(BaseModel):
    transaction_id: int
    status: str
    initiator_tg_id: int
    counterparty_tg_id: int
    amount: str
    currency: str
    confirmed_at: str | None = None


class PendingTransactionItem(BaseModel):
    id: int
    initiator_tg_id: int
    counterparty_tg_id: int
    amount: str
    currency: str
    memo: str | None = None
    created_at: str | None = None


class PendingTransactionsResponse(BaseModel):
    items: list[PendingTransactionItem]


# ---------- V2.0 被查通知 ----------
class NotifyOnCheckedResponse(BaseModel):
    notify_on_checked: bool


class NotifyOnCheckedUpdateBody(BaseModel):
    tg_id: int
    notify_on_checked: bool


class PendingNotifyCheckedItem(BaseModel):
    id: int
    target_tg_id: int
    querier_tg_id: int
    created_at: str | None = None


class PendingNotifyCheckedResponse(BaseModel):
    items: list[PendingNotifyCheckedItem]


# ---------- V2.0 信用卡片 ----------
class SharecardResponse(BaseModel):
    """可分享的简要信用数据（文案由 Bot 按版式生成）；含背书字段以增强信任感。"""
    tg_id: int | None = None
    username: str | None = None
    risk: str = "medium"
    score: int = 50
    blacklist: bool = False
    tags: list[str] = []
    tx_success_count: int = 0
    tx_total_count: int = 0
    tx_success_rate: float = 0.0
    first_seen: str | None = None
    report_count: int = 0
    report_verified: int = 0
    updated_at: str | None = None
    label: str | None = None
    has_flow_visible: bool = False  # 是否对查阅者展示链上流水
    flow_verified: bool = False  # 第四阶段：展示流水的地址中是否至少有一个已验证


# ---------- V2.0 群扫描 ----------
class GroupScanBody(BaseModel):
    group_id: int  # Telegram chat_id (负数表示群组)
    requester_tg_id: int  # 发起扫描的 TG 用户（需为群管理员）


class GroupScanResponse(BaseModel):
    ok: bool = True
    message: str = "ok"


# ---------- 管理后台扩展（V2.0 4.2）----------
class AdminReporterItem(BaseModel):
    reporter_tg_id: int
    report_count: int
    verified_count: int
    last_at: str | None = None


class AdminReportersResponse(BaseModel):
    items: list[AdminReporterItem]


class AdminReportItem(BaseModel):
    """管理后台：单条举报（用户对用户的举报）。"""
    id: int
    reporter_tg_id: int
    target_tg_id: int
    reason: str = ""
    category: str | None = None  # scam | run_order | lost_contact | other
    evidence_data: dict | None = None
    status: str  # pending | verified | rejected
    created_at: str | None = None


class AdminReportsResponse(BaseModel):
    items: list[AdminReportItem]


class ReportUpdateBody(BaseModel):
    status: Literal["verified", "rejected"]


class BatchReportUpdateBody(BaseModel):
    ids: list[int]
    status: Literal["verified", "rejected"]


class BatchReportUpdateResponse(BaseModel):
    ok: bool
    updated: int
    message: str


class AdminTransactionItem(BaseModel):
    id: int
    initiator_tg_id: int
    counterparty_tg_id: int
    amount: str
    currency: str
    status: str
    created_at: str | None = None
    confirmed_at: str | None = None


class AdminTransactionsResponse(BaseModel):
    items: list[AdminTransactionItem]


class AdminRiskTrendDay(BaseModel):
    date: str
    low: int = 0
    medium: int = 0
    high: int = 0
    extreme: int = 0


class AdminRiskTrendResponse(BaseModel):
    items: list[AdminRiskTrendDay]


# 阶段 A：报告防伪指纹
class ReportRegisterBody(BaseModel):
    target_tg_id: int
    content_hash: str = Field(..., min_length=32, max_length=64)


class ReportRegisterResponse(BaseModel):
    report_hash: str


class ReportVerifyResponse(BaseModel):
    valid: bool
    created_at: str | None = None
    target_tg_id: int | None = None
    message: str = ""
    content_match: bool | None = None  # 第四阶段：仅当传入 content_hash 时返回，True=与存档一致


# 阶段 B：隐私设置
class PrivacySettingsResponse(BaseModel):
    show_register_year: bool = True
    show_premium: bool = True
    show_tx_30d: bool = True


class PrivacySettingsUpdateBody(BaseModel):
    tg_id: int
    show_register_year: bool | None = None
    show_premium: bool | None = None
    show_tx_30d: bool | None = None


# ---------- 绑定收款地址与流水 ----------
class BoundWalletItem(BaseModel):
    id: int
    chain: str
    address: str = ""
    address_masked: str = ""
    label: str | None = None
    show_flow_to_public: bool = False
    created_at: str | None = None
    verified_at: str | None = None  # 所有权验证时间，有则展示「已验证」


class BoundWalletsResponse(BaseModel):
    items: list[BoundWalletItem]


class BindWalletBody(BaseModel):
    user_id: int
    chain: str = Field(..., description="TRC20 | TON")
    address: str = Field(..., min_length=10, max_length=128)
    label: str | None = None
    show_flow_to_public: bool = False


class BindWalletResponse(BaseModel):
    id: int
    chain: str
    address: str
    label: str | None = None
    show_flow_to_public: bool


class WalletShowFlowBody(BaseModel):
    user_id: int
    show_flow_to_public: bool


class FlowItem(BaseModel):
    direction: str  # in | out
    amount: float
    currency: str = "USDT"
    counterparty_masked: str = ""
    time: int = 0  # ms
    tx_hash: str = ""
    chain: str = ""


class FlowResponse(BaseModel):
    items: list[FlowItem]
    error: str | None = None  # no_wallets | fetch_failed


class FlowStatsResponse(BaseModel):
    days: int = 30
    in_count: int = 0
    in_amount: float = 0.0
    out_count: int = 0
    out_amount: float = 0.0


class CanViewFlowResponse(BaseModel):
    can_view_flow: bool
