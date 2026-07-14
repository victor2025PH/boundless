# -*- coding: utf-8 -*-
from typing import Optional, Any, List
from enum import Enum
from pydantic import BaseModel, Field


class TaskType(str, Enum):
    # Telegram
    TELEGRAM_SEND_MESSAGE = "telegram_send_message"
    TELEGRAM_READ_MESSAGES = "telegram_read_messages"
    TELEGRAM_SEND_FILE = "telegram_send_file"
    TELEGRAM_WORKFLOW = "telegram_workflow"
    TELEGRAM_SWITCH_ACCOUNT = "telegram_switch_account"
    TELEGRAM_LIST_ACCOUNTS = "telegram_list_accounts"
    TELEGRAM_FORWARD = "telegram_forward"
    # WhatsApp
    WHATSAPP_SEND_MESSAGE = "whatsapp_send_message"
    WHATSAPP_READ_MESSAGES = "whatsapp_read_messages"
    # LinkedIn
    LINKEDIN_SEND_MESSAGE = "linkedin_send_message"
    LINKEDIN_READ_MESSAGES = "linkedin_read_messages"
    LINKEDIN_POST_UPDATE = "linkedin_post_update"
    LINKEDIN_SEARCH_PROFILE = "linkedin_search_profile"
    LINKEDIN_SEND_CONNECTION = "linkedin_send_connection"
    LINKEDIN_ACCEPT_CONNECTIONS = "linkedin_accept_connections"
    LINKEDIN_LIKE_POST = "linkedin_like_post"
    LINKEDIN_COMMENT_POST = "linkedin_comment_post"
    # TikTok
    TIKTOK_WARMUP = "tiktok_warmup"
    TIKTOK_BROWSE_FEED = "tiktok_browse_feed"
    TIKTOK_FOLLOW = "tiktok_follow"
    TIKTOK_TEST_FOLLOW = "tiktok_test_follow"
    TIKTOK_CHAT = "tiktok_chat"
    TIKTOK_SEND_DM = "tiktok_send_dm"
    TIKTOK_CHECK_INBOX = "tiktok_check_inbox"
    TIKTOK_FOLLOW_UP = "tiktok_follow_up"
    TIKTOK_AUTO = "tiktok_auto"
    TIKTOK_STATUS = "tiktok_status"
    TIKTOK_WORKFLOW = "tiktok_workflow"
    TIKTOK_KEYWORD_SEARCH = "tiktok_keyword_search"
    TIKTOK_LIVE_ENGAGE = "tiktok_live_engage"
    TIKTOK_CHECK_COMMENT_REPLIES = "tiktok_check_comment_replies"
    TIKTOK_CHECK_AND_CHAT_FOLLOWBACKS = "tiktok_check_and_chat_followbacks"
    TIKTOK_FOLLOW_USER = "tiktok_follow_user"
    TIKTOK_INTERACT_USER = "tiktok_interact_user"
    # Facebook (与 executor 对齐，否则 POST /tasks 无法创建)
    FACEBOOK_SEND_MESSAGE = "facebook_send_message"
    FACEBOOK_ADD_FRIEND = "facebook_add_friend"
    # 2026-04-23: 搜索 → 加好友 → 打招呼一体化（方案 A2）
    FACEBOOK_ADD_FRIEND_AND_GREET = "facebook_add_friend_and_greet"
    FACEBOOK_SEND_GREETING = "facebook_send_greeting"
    FACEBOOK_BROWSE_FEED = "facebook_browse_feed"
    FACEBOOK_BROWSE_FEED_BY_INTEREST = "facebook_browse_feed_by_interest"
    FACEBOOK_SEARCH_LEADS = "facebook_search_leads"
    FACEBOOK_JOIN_GROUP = "facebook_join_group"
    # Facebook — Sprint 1 新增（群组 / 收件箱 / 串行剧本）
    FACEBOOK_BROWSE_GROUPS = "facebook_browse_groups"
    FACEBOOK_GROUP_ENGAGE = "facebook_group_engage"
    FACEBOOK_EXTRACT_MEMBERS = "facebook_extract_members"
    FACEBOOK_GROUP_MEMBER_GREET = "facebook_group_member_greet"
    FACEBOOK_CHECK_INBOX = "facebook_check_inbox"
    FACEBOOK_CHECK_MESSAGE_REQUESTS = "facebook_check_message_requests"
    FACEBOOK_CHECK_FRIEND_REQUESTS = "facebook_check_friend_requests"
    FACEBOOK_CAMPAIGN_RUN = "facebook_campaign_run"
    # Phase 11+12 LINE referral 闭环 (2026-04-25)
    FACEBOOK_LINE_DISPATCH_FROM_REPLY = "facebook_line_dispatch_from_reply"
    FACEBOOK_SEND_REFERRAL_REPLIES = "facebook_send_referral_replies"
    FACEBOOK_RECYCLE_DEAD_PEERS = "facebook_recycle_dead_peers"
    FACEBOOK_DAILY_REFERRAL_SUMMARY = "facebook_daily_referral_summary"
    FACEBOOK_ALERT_CHECK_HOURLY = "facebook_alert_check_hourly"
    # Phase 20.1 (2026-04-25): B 侧 inbox 检测 referral 回复
    FACEBOOK_CHECK_REFERRAL_REPLIES = "facebook_check_referral_replies"
    # Phase 20.2 (2026-04-25): SLA 死信回收 — 标 stale + 升级 dead
    FACEBOOK_MARK_STALE_REFERRALS = "facebook_mark_stale_referrals"
    # Instagram
    INSTAGRAM_BROWSE_FEED = "instagram_browse_feed"
    INSTAGRAM_SEARCH_LEADS = "instagram_search_leads"
    INSTAGRAM_SEND_DM = "instagram_send_dm"
    INSTAGRAM_BROWSE_HASHTAG = "instagram_browse_hashtag"
    # X (Twitter)
    TWITTER_BROWSE_TIMELINE = "twitter_browse_timeline"
    TWITTER_SEARCH_LEADS = "twitter_search_leads"
    TWITTER_SEARCH_AND_ENGAGE = "twitter_search_and_engage"
    TWITTER_SEND_DM = "twitter_send_dm"
    # Cross-platform
    BATCH_SEND = "batch_send"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskBatchDelete(BaseModel):
    """批量删除任务记录（服务端跳过 running/pending/不存在）。"""
    task_ids: List[str] = Field(default_factory=list, max_length=100)


class TaskCreate(BaseModel):
    # 2026-05-07: 从 TaskType 枚举放宽为 str，兼容主控与 Worker 版本不一致场景。
    # 业务级校验由 gate_registry / executor 负责，API 层不再拦截未知 task_type。
    type: str
    device_id: Optional[str] = None
    params: dict = Field(default_factory=dict)
    policy_id: Optional[str] = None
    batch_id: str = ""  # 批次 ID，批量任务追踪用
    run_on_host: bool = True  # False = 仅下发，由手机 agent 执行并上报
    priority: int = Field(default=50, ge=0, le=100,
                          description="任务优先级: 0最低, 50默认, 100紧急高意向回复")
    # 可选：写入 params._created_via，供任务中心展示来源（api / ai_chat / scheduler 等）
    created_via: Optional[str] = None


class TaskResultReport(BaseModel):
    success: bool
    error: str = ""
    screenshot_path: str = ""


class TaskResponse(BaseModel):
    task_id: str
    type: str
    type_label_zh: Optional[str] = None  # 中文展示名（后端单一真源，前端优先用这个）
    device_id: Optional[str]
    status: str
    params: dict = Field(default_factory=dict)
    result: Optional[dict] = None
    created_at: str = ""
    updated_at: str = ""
    # 展示层（由 task_ui_enrich 填充，旧 Worker 可无）
    device_label: Optional[str] = None
    worker_host: Optional[str] = None
    task_origin: Optional[str] = None
    task_origin_label_zh: Optional[str] = None
    phase_caption: Optional[str] = None
    execution_policy_hint: Optional[str] = None
    stuck_reason_zh: Optional[str] = None  # pending 任务的卡住原因（便于用户一眼看明白）
    # 2026-04-27 Phase 2 P0 #2: 当前业务步骤 (running task 实时可见)
    # 业务方法调 task_store.set_task_step("搜索群组", "ママ友") → 写 checkpoint
    # → _to_response 提取暴露. 旧 task / 未接入业务方法的 task 为 None.
    current_step: Optional[str] = None
    current_sub_step: Optional[str] = None
    current_step_at: Optional[str] = None
    # 2026-04-27 Phase 2 P1 后端 normalize: 失败原因归类
    # {layer, code, msg, tone, emoji} - 详见 src/host/error_classifier.py
    # 旧 task / 未失败 / 无 last_error 为 None. 前端优先用本字段, fallback regex.
    error_classification: Optional[dict] = None
    deleted_at: Optional[str] = None


class DeviceListItem(BaseModel):
    device_id: str
    display_name: str
    status: str
    model: str = ""
    android_version: str = ""


class ScreenDigestBatch(BaseModel):
    """批量请求屏幕画面摘要（用于监控页仅在画面变化时刷新缩略图）。"""
    device_ids: list[str] = Field(default_factory=list)
