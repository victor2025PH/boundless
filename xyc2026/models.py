# TrustCheck - SQLAlchemy models (users, blacklist, reports, wallet_entity_graph, feedback)
from datetime import datetime, date
from sqlalchemy import String, Integer, BigInteger, Boolean, DateTime, Date, Text, JSON, UniqueConstraint, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base
import enum


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class ReportStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class User(Base):
    """被查询主体：TG 用户或钱包对应的实体"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    register_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_score: Mapped[int] = mapped_column(Integer, default=50)  # 0-100
    risk_level: Mapped[str] = mapped_column(String(20), default=RiskLevel.MEDIUM.value)
    high_risk_group_count: Mapped[int] = mapped_column(Integer, default=0)
    blacklist: Mapped[bool] = mapped_column(Boolean, default=False)
    tips: Mapped[list | None] = mapped_column(JSON, nullable=True)  # ["新账号", "高危群活跃"]
    label: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 商户/用户/高危
    report_count: Mapped[int] = mapped_column(Integer, default=0)  # 被举报次数（verified）
    query_count: Mapped[int] = mapped_column(Integer, default=0)    # 被查询次数（热度）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 本系统内最后被查询/交互时间
    notify_on_checked: Mapped[bool] = mapped_column(Boolean, default=True)  # V2.0 被查时是否接收 Bot 通知，默认开
    privacy_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 阶段 B：对查阅者展示项 {"show_register_year":true,"show_premium":true,"show_tx_30d":true}

    def to_public_dict(self):
        return {
            "tg_id": self.tg_id,
            "username": self.username,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "blacklist": self.blacklist,
            "tips": self.tips or [],
            "label": self.label,
        }


class Blacklist(Base):
    __tablename__ = "blacklist"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    evidence_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="user_report")
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# 举报分类（V2.0）：scam=诈骗, run_order=跑单, lost_contact=失联, other=其他
REPORT_CATEGORIES = ("scam", "run_order", "lost_contact", "other")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reporter_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # scam | run_order | lost_contact | other
    evidence_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # tx_hash, screenshot_url, description
    status: Mapped[str] = mapped_column(String(20), default=ReportStatus.PENDING.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WalletEntityGraph(Base):
    """ID 与钱包关联，用于换号不换地址追踪"""
    __tablename__ = "wallet_entity_graph"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True)
    chain: Mapped[str] = mapped_column(String(32), default="TRC20")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("tg_id", "wallet_address", "chain", name="uq_wallet_entity"),)


# 绑定收款地址：用户主动绑定，用于对查阅者展示流水（与 wallet_entity_graph 分离）
SUPPORTED_BIND_CHAINS = ("TRC20", "TON")  # 可扩展 ERC20
MAX_BOUND_WALLETS_PER_USER = 5


class UserBoundWallet(Base):
    __tablename__ = "user_bound_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chain: Mapped[str] = mapped_column(String(32))  # TRC20 | TON
    address: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 如「主收款」
    show_flow_to_public: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否对查报告的人展示流水
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 可选所有权验证时间

    __table_args__ = (UniqueConstraint("tg_id", "address", "chain", name="uq_user_bound_wallet"),)


class WalletTransactionSnapshot(Base):
    """流水快照：按需同步的链上交易记录，用于分页/统计/离线展示。"""
    __tablename__ = "wallet_transaction_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(Integer, index=True)  # user_bound_wallets.id
    tx_hash: Mapped[str] = mapped_column(String(128), index=True)
    chain: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))  # in | out
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(16), default="USDT")
    counterparty_masked: Mapped[str] = mapped_column(String(32), default="")
    block_time_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("wallet_id", "tx_hash", name="uq_wallet_tx_snapshot"),)


class Feedback(Base):
    """纠错/误报反馈"""
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reporter_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    evidence_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QueryLog(Base):
    """匿名化查询日志（可选）：仅用于统计"""
    __tablename__ = "query_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_hash: Mapped[str] = mapped_column(String(64), index=True)  # hash(target_id + day)
    result_risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingInvite(Base):
    """邀请链接：对方点击并发消息后，将信用报告发回给 querier"""
    __tablename__ = "pending_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    querier_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class CheckLog(Base):
    """谁查过我：查询人 + 被查人 + 时间，供「我的信用」展示最近被查次数与时间"""
    __tablename__ = "check_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    querier_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReportShareToken(Base):
    """出示我的报告：一次性链接，对方点开可查看 owner 的最新报告"""
    __tablename__ = "report_share_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    owner_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ActivityEvent(Base):
    """本系统活动时间线：某用户被查、邀请打开、分享被查看等，按日聚合展示"""
    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)  # queried, invite_clicked, share_viewed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RiskAlertSubscription(Base):
    """风险变化/上下线提醒：谁订阅了谁；alert_type=risk_change|online_offline；preferred_lang 用于推送；source 可选统计"""
    __tablename__ = "risk_alert_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscriber_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    alert_type: Mapped[str] = mapped_column(String(32), default="risk_change", index=True)  # risk_change | online_offline
    preferred_lang: Mapped[str | None] = mapped_column(String(8), nullable=True)  # en, zh, tl
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # query, invite, recheck, bot
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("subscriber_tg_id", "target_tg_id", "alert_type", name="uq_risk_alert_sub"),)


class OnlineStatusEvent(Base):
    """监控端上报的上下线事件（众包或专用监控账号）；用于去重与可选「最新状态」查询"""
    __tablename__ = "online_status_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)  # online | offline
    reporter_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # 众包上报者
    monitor_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # 专用监控账号（预留）
    at_ts: Mapped[datetime] = mapped_column(DateTime)  # 事件发生时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 扩展：平台、last_seen 等


class RiskAlertPending(Base):
    """待发送的风险提醒（Bot 拉取并发送后标记 sent_at）；subscriber_lang 用于按用户语言推送"""
    __tablename__ = "risk_alert_pending"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscriber_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)  # new_report, blacklist_added
    subscriber_lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class GroupScanLog(Base):
    """V2.0 群扫描记录：每群每日限 1 次，仅管理员可触发"""
    __tablename__ = "group_scan_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)  # Telegram chat_id (负数表示群组)
    requester_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotifyCheckedPending(Base):
    """V2.0 被查通知待发送：CheckLog 写入后若被查人开启通知则入队，Bot 拉取并发送后标记 sent_at"""
    __tablename__ = "notify_checked_pending"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)   # 被查人
    querier_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)  # 查询人
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Transaction(Base):
    """V2.0 交易确认：发起方请求对方确认，对方同意/拒绝后更新状态；用于信用报告展示成功笔数/成功率/金额"""
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    initiator_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)   # 发起确认请求的一方
    counterparty_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)  # 被请求确认的一方
    amount: Mapped[str] = mapped_column(String(32))   # 金额字符串，如 "100.50"
    currency: Mapped[str] = mapped_column(String(16), default="USDT")
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=TransactionStatus.PENDING.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 对方确认时间；拒绝则为 null


class ReportSnapshot(Base):
    """阶段 A：报告防伪指纹。每份报告生成时写入，供 /verify 验真。"""
    __tablename__ = "report_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_hash: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # 8 位如 TC-78A2B9
    target_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    content_hash: Mapped[str] = mapped_column(String(64))  # 报告正文 sha256  hex，防篡改校验
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReporterStats(Base):
    """V2.0 任务 1.6：举报人统计；report_create 时更新 total；update_report_status 时更新 verified/rejected；scoring 可接入权重。"""
    __tablename__ = "reporter_stats"

    reporter_tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    total_reports: Mapped[int] = mapped_column(Integer, default=0)  # 该举报人发起的举报总数
    verified_count: Mapped[int] = mapped_column(Integer, default=0)  # 其中已被核实为 verified 的数量
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)  # 其中已被驳回的数量
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次举报时间
