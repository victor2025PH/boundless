# -*- coding: utf-8 -*-
"""SQLite 持久化层。WAL 模式，支持并发读写。"""

import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

from src.host.device_registry import data_file

logger = logging.getLogger(__name__)

DB_PATH = data_file("openclaw.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    device_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    params      TEXT DEFAULT '{}',
    result      TEXT,
    policy_id   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_device  ON tasks(device_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    cron_expr   TEXT NOT NULL,
    task_type   TEXT NOT NULL,
    device_id   TEXT,
    params      TEXT DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run    TEXT,
    next_run    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled);
CREATE INDEX IF NOT EXISTS idx_schedules_next    ON schedules(next_run);

CREATE TABLE IF NOT EXISTS device_states (
    device_id   TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'tiktok',
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, platform, key)
);
CREATE INDEX IF NOT EXISTS idx_dstate_device ON device_states(device_id, platform);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id  TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'general',
    status         TEXT NOT NULL DEFAULT 'active',
    variants       TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    ended_at       TEXT
);

CREATE TABLE IF NOT EXISTS experiment_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    variant        TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    device_id      TEXT DEFAULT '',
    metadata       TEXT DEFAULT '{}',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_events_exp ON experiment_events(experiment_id, variant);
CREATE INDEX IF NOT EXISTS idx_exp_events_type ON experiment_events(experiment_id, event_type);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    source      TEXT DEFAULT 'api',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_logs(timestamp);

CREATE TABLE IF NOT EXISTS device_groups (
    group_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#60a5fa',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_group_members (
    group_id    TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_dgm_group ON device_group_members(group_id);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'draft',
    target_accounts TEXT DEFAULT '[]',
    device_ids      TEXT DEFAULT '[]',
    task_sequence   TEXT DEFAULT '[]',
    params          TEXT DEFAULT '{}',
    message_template TEXT DEFAULT '',
    ai_rewrite      INTEGER DEFAULT 1,
    batch_id        TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS seed_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed_username TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    follows_count INTEGER DEFAULT 0,
    replies_count INTEGER DEFAULT 0,
    referrals_count INTEGER DEFAULT 0,
    conversions_count INTEGER DEFAULT 0,
    last_used_at TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    UNIQUE(seed_username, device_id)
);
CREATE INDEX IF NOT EXISTS idx_seed_quality_country ON seed_quality(country, follows_count);

CREATE TABLE IF NOT EXISTS crm_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'inbound',
    text TEXT DEFAULT '',
    intent TEXT DEFAULT '',
    device_id TEXT DEFAULT '',
    action TEXT DEFAULT '',
    platform TEXT DEFAULT 'tiktok',
    ts TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_crm_contact ON crm_interactions(contact);

CREATE TABLE IF NOT EXISTS ab_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment TEXT NOT NULL,
    variant TEXT NOT NULL,
    event_type TEXT NOT NULL,
    contact_id TEXT DEFAULT '',
    ts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ab_exp ON ab_events(experiment, variant);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN batch_id TEXT DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN checkpoint TEXT DEFAULT ''",
    "CREATE TABLE IF NOT EXISTS campaigns (campaign_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'draft', target_accounts TEXT DEFAULT '[]', device_ids TEXT DEFAULT '[]', task_sequence TEXT DEFAULT '[]', params TEXT DEFAULT '{}', message_template TEXT DEFAULT '', ai_rewrite INTEGER DEFAULT 1, batch_id TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')), started_at TEXT, completed_at TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status)",
    # 任务优先级：0=最低, 50=默认, 100=高意向紧急回复
    "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 50",
    "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(status, priority DESC, created_at)",
    # 任务重试机制
    "ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN next_retry_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tasks_retry ON tasks(status, next_retry_at) WHERE max_retries > 0",
    # 软删除（回收站）：非空表示已移入回收站
    "ALTER TABLE tasks ADD COLUMN deleted_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tasks_deleted ON tasks(deleted_at) WHERE deleted_at IS NOT NULL",

    # ─── Sprint 2 P0: Facebook 业务表(漏斗 + AI 日报数据源) ───
    "CREATE TABLE IF NOT EXISTS facebook_groups ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " device_id TEXT NOT NULL,"
    " group_name TEXT NOT NULL,"
    " group_url TEXT DEFAULT '',"
    " member_count INTEGER DEFAULT 0,"
    " language TEXT DEFAULT '',"
    " country TEXT DEFAULT '',"
    " status TEXT NOT NULL DEFAULT 'joined',"  # joined/left/pending/banned
    " joined_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " last_visited_at TEXT,"
    " visit_count INTEGER DEFAULT 0,"
    " extracted_member_count INTEGER DEFAULT 0,"
    " UNIQUE(device_id, group_name)"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_fb_groups_device ON facebook_groups(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_fb_groups_status ON facebook_groups(status)",

    "CREATE TABLE IF NOT EXISTS facebook_friend_requests ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " device_id TEXT NOT NULL,"
    " target_name TEXT NOT NULL,"
    " target_profile_url TEXT DEFAULT '',"
    " note TEXT DEFAULT '',"
    " source TEXT DEFAULT '',"  # group_name / search_keyword / suggestion
    " status TEXT NOT NULL DEFAULT 'sent',"  # sent/accepted/rejected/cancelled/risk
    " sent_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " accepted_at TEXT,"
    " lead_id INTEGER,"  # 对应 leads.db 中的 lead_id
    " UNIQUE(device_id, target_name, sent_at)"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_device ON facebook_friend_requests(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_status ON facebook_friend_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_sent ON facebook_friend_requests(sent_at)",

    "CREATE TABLE IF NOT EXISTS facebook_inbox_messages ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " device_id TEXT NOT NULL,"
    " peer_name TEXT NOT NULL,"
    " peer_type TEXT DEFAULT 'friend',"  # friend/stranger(message_request)/group
    " message_text TEXT DEFAULT '',"
    " direction TEXT NOT NULL DEFAULT 'incoming',"  # incoming/outgoing
    " ai_decision TEXT DEFAULT '',"  # reply/skip/escalate/wa_referral
    " ai_reply_text TEXT DEFAULT '',"
    " language_detected TEXT DEFAULT '',"
    " seen_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " replied_at TEXT,"
    " lead_id INTEGER"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_device ON facebook_inbox_messages(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_peer ON facebook_inbox_messages(peer_name)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_seen ON facebook_inbox_messages(seen_at)",

    # ─── Sprint 3 P0: 给 friend_requests / inbox 加 preset_key,支持按预设切片 ───
    "ALTER TABLE facebook_friend_requests ADD COLUMN preset_key TEXT DEFAULT ''",
    "ALTER TABLE facebook_inbox_messages ADD COLUMN preset_key TEXT DEFAULT ''",
    "ALTER TABLE facebook_groups ADD COLUMN preset_key TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_preset ON facebook_friend_requests(preset_key, sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_preset ON facebook_inbox_messages(preset_key, seen_at)",

    # ─── Sprint 4 P1: TikTok 漏斗事件统一表 ──────────────────────────────
    # 复制 FB 的埋点模式到 TK,让 /dashboard/cross-platform-funnel 两侧有
    # 真实可比较的 6 阶段数据,而不是从 tasks 表 COUNT 估算。
    # 设计原则:
    #   * 一张表统一 6 个 stage,方便 GROUP BY 聚合
    #   * stage 用字符串枚举(exposure/interest/engagement/direct_msg/
    #     guidance/conversion) 和 FB 的 get_funnel_metrics 的 stage_* 对齐
    #   * target_key 自由文本(username/video_id/dm_peer),用于去重
    #   * preset_key 复用 FB 的切片模式(warmup/growth/emergency_cooldown)
    """CREATE TABLE IF NOT EXISTS tiktok_funnel_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        target_key TEXT NOT NULL DEFAULT '',
        preset_key TEXT DEFAULT '',
        meta_json TEXT DEFAULT '',
        at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tt_funnel_stage ON tiktok_funnel_events(stage, at)",
    "CREATE INDEX IF NOT EXISTS idx_tt_funnel_device ON tiktok_funnel_events(device_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_tt_funnel_preset ON tiktok_funnel_events(preset_key, stage, at)",

    # ─── 2026-04-21 P0-3: Facebook 风控事件落库（驱动自动冷却红旗）─────
    # 动机: `_report_risk` 原先只推 event_stream + 写 DeviceStateStore，
    # 无历史、无 24h 聚合，Gate 无法做"≥3 次/24h 自动冷却"。
    # kind: checkpoint / identity_verify / captcha / account_review /
    #       policy_warning / other（由关键词映射，见 fb_store._classify_risk_kind）
    """CREATE TABLE IF NOT EXISTS fb_risk_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        task_id TEXT DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'other',
        raw_message TEXT DEFAULT '',
        detected_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_risk_device_at ON fb_risk_events(device_id, detected_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_risk_kind ON fb_risk_events(kind, detected_at)",

    # ─── 2026-04-21 P1-2: Facebook 账号阶段状态机 ──────────────────────
    # 每台设备（= FB 账号）一行。phase ∈ cold_start/growth/mature/cooldown。
    # 迁移规则见 config/facebook_playbook.yaml 的 phase_transitions，
    # 由 fb_account_phase.evaluate_transition() 事件驱动更新。
    """CREATE TABLE IF NOT EXISTS fb_account_phase (
        device_id TEXT PRIMARY KEY,
        phase TEXT NOT NULL DEFAULT 'cold_start',
        since_at TEXT NOT NULL DEFAULT (datetime('now')),
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        total_scrolls INTEGER DEFAULT 0,
        total_likes INTEGER DEFAULT 0,
        total_risk_events INTEGER DEFAULT 0,
        last_task_at TEXT,
        last_risk_at TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_phase_phase ON fb_account_phase(phase)",

    # ─── 2026-04-21 P1-3a: Facebook campaign_run 断点续跑 ─────────────
    # 每次 facebook_campaign_run 任务一行。步骤完成情况写 state_json，
    # 失败/中断后，同一 run_id 再次提交 → 跳过已完成步骤。
    """CREATE TABLE IF NOT EXISTS fb_campaign_runs (
        run_id TEXT PRIMARY KEY,
        task_id TEXT DEFAULT '',
        device_id TEXT NOT NULL,
        preset_key TEXT DEFAULT '',
        total_steps INTEGER DEFAULT 0,
        current_step_idx INTEGER DEFAULT 0,
        current_step_name TEXT DEFAULT '',
        state TEXT NOT NULL DEFAULT 'running',
        state_json TEXT DEFAULT '{}',
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_campaign_device ON fb_campaign_runs(device_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_campaign_state ON fb_campaign_runs(state)",

    # ─── 2026-04-21 P2-4 Sprint A: 目标画像识别（日本 37-60 岁女性） ──────
    # 动机：把"谁值得互动"沉淀成可配置、可追溯、可审计的数据结构，
    # 不再用硬编码分支判断。Sprint A 先做 4 张表 + YAML 画像。
    #
    # 1) fb_target_personas: 目标画像定义（多画像，每行一个）
    #    配置来源为 config/fb_target_personas.yaml 热加载，
    #    本表仅作为"任务→使用了哪个画像 snapshot"的审计追溯。
    #    active=1 表示该画像当前启用，用于默认选中。
    """CREATE TABLE IF NOT EXISTS fb_target_personas (
        persona_key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        age_min INTEGER DEFAULT 0,
        age_max INTEGER DEFAULT 120,
        gender TEXT DEFAULT 'any',
        locale TEXT DEFAULT '',
        rules_json TEXT DEFAULT '{}',
        vlm_prompt TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_persona_active ON fb_target_personas(active)",

    # 2) fb_profile_insights: 识别结果（每次对同一 target_key 的判断结果）
    #    stage ∈ L1/L2; match=1 表示判定命中画像；score ∈ [0,100]
    #    target_key 通常是 profile_url / user_id / username 其中一种
    #    insights_json 存 {age_band, gender, is_japanese, topics:[...], reasons:[...]}
    """CREATE TABLE IF NOT EXISTS fb_profile_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        task_id TEXT DEFAULT '',
        persona_key TEXT NOT NULL,
        target_key TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        stage TEXT NOT NULL DEFAULT 'L1',
        match INTEGER NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        confidence REAL DEFAULT 0,
        insights_json TEXT DEFAULT '{}',
        image_paths TEXT DEFAULT '[]',
        vlm_model TEXT DEFAULT '',
        latency_ms INTEGER DEFAULT 0,
        classified_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_persona ON fb_profile_insights(persona_key, classified_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_target ON fb_profile_insights(target_key, classified_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_device ON fb_profile_insights(device_id, stage, classified_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_insights_match ON fb_profile_insights(match, classified_at)",

    # 3) fb_content_exposure: 浏览内容主题曝光日志
    #    browse_feed 里看到的 post 封面做主题识别后落库，
    #    用于后续"按账号兴趣曲线决定点赞/互动倾向"。
    """CREATE TABLE IF NOT EXISTS fb_content_exposure (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        task_id TEXT DEFAULT '',
        topic TEXT NOT NULL DEFAULT 'other',
        lang TEXT DEFAULT '',
        liked INTEGER DEFAULT 0,
        dwell_ms INTEGER DEFAULT 0,
        meta_json TEXT DEFAULT '{}',
        seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_expose_device ON fb_content_exposure(device_id, seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_expose_topic ON fb_content_exposure(topic, seen_at)",

    # 4) ai_cost_events: AI 调用审计（本地 Ollama 记 0 USD，但记条数/耗时/显存）
    #    保留扩展 provider=openai/gemini/anthropic 做云调用时的成本追踪。
    """CREATE TABLE IF NOT EXISTS ai_cost_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL DEFAULT 'ollama',
        model TEXT NOT NULL DEFAULT '',
        task_id TEXT DEFAULT '',
        scene TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        image_count INTEGER DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0,
        ok INTEGER NOT NULL DEFAULT 1,
        error TEXT DEFAULT '',
        at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_cost_provider ON ai_cost_events(provider, at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_cost_scene ON ai_cost_events(scene, at)",

    # ─── P2-4 Sprint C-1: VLM 并发排队指标 + 设备维度 ───
    "ALTER TABLE ai_cost_events ADD COLUMN queue_wait_ms INTEGER DEFAULT 0",
    "ALTER TABLE ai_cost_events ADD COLUMN device_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_ai_cost_device ON ai_cost_events(device_id, at)",

    # ─── 2026-04-23: 打招呼(greeting)特化列 ─────────────────────────────
    # 动机:
    #   * seen_at 对 outgoing 语义不清(真实意义是"发出时间")。新增专用 sent_at
    #     列,count_outgoing_messages_since 用它替代 seen_at,漏斗也更精确。
    #   * template_id 记录本次打招呼从哪条 chat_messages.yaml 模板抽的,
    #     供 A/B 效果分析。格式 "<country>:<index>"(如 "jp:3"),不强约束。
    # 向后兼容: 老行 sent_at=NULL, template_id='' 都不影响现有查询。
    "ALTER TABLE facebook_inbox_messages ADD COLUMN sent_at TEXT",
    "ALTER TABLE facebook_inbox_messages ADD COLUMN template_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_sent_at ON facebook_inbox_messages(device_id, direction, sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_inbox_template ON facebook_inbox_messages(template_id)",

    # ─── 2026-04-23 Phase 5: Lead Mesh (跨平台 Lead Dossier + Agent 通信) ──
    # 动机: 把引流交接从"单一 handoffs 表"升级为"Lead 全旅程卷宗 + Agent Mesh
    # 通信层"。让 A 机 / B 机 / 人工 / 外部 Webhook 系统都能用统一模型操作 lead,
    # 跨真机/云手机、跨账号自动去重、跨平台身份聚合(FB+LINE+WA+TG+IG)。
    #
    # 5 张表:
    #   leads_canonical    — 跨平台统一 lead 抽象(UUID 主键)
    #   lead_identities    — 平台身份映射(platform, account_id)→canonical_id
    #   lead_journey       — append-only 事件流, 所有 agent/人动作都记一笔
    #   lead_handoffs      — 引流交接状态机(pending→ack→completed/rejected)
    #   agent_messages     — Agent 间消息队列(SQLite 持久化 + HTTP 实时入口)
    #   lead_locks         — 软锁, 防止多 agent 并发操作同一 lead
    #   lead_merges        — 合并审计日志, 自动合并可撤销
    #   webhook_dispatches — Webhook 外发记录 + 失败重试跟踪

    """CREATE TABLE IF NOT EXISTS leads_canonical (
        canonical_id TEXT PRIMARY KEY,
        primary_name TEXT DEFAULT '',
        primary_language TEXT DEFAULT '',
        primary_persona_key TEXT DEFAULT '',
        merged_into TEXT,                      -- 若本 lead 被合并到另一个, 存 target canonical_id
        tags TEXT DEFAULT '',                  -- 逗号分隔标签 (便于简单查询)
        metadata_json TEXT DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_leads_canonical_merged ON leads_canonical(merged_into)",
    "CREATE INDEX IF NOT EXISTS idx_leads_canonical_name ON leads_canonical(primary_name)",

    """CREATE TABLE IF NOT EXISTS lead_identities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_id TEXT NOT NULL,
        platform TEXT NOT NULL,               -- facebook / line / whatsapp / telegram / instagram / messenger
        account_id TEXT NOT NULL,             -- FB profile_url / LINE @id / phone / @tg_user ...
        display_name TEXT DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 1,  -- 1=硬匹配(account_id 唯一), 0=软匹配候选
        discovered_via TEXT DEFAULT '',       -- 来源: group_extract / inbox / handoff ...
        discovered_by_device TEXT DEFAULT '',
        metadata_json TEXT DEFAULT '{}',
        discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(platform, account_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lead_identities_canonical ON lead_identities(canonical_id)",
    "CREATE INDEX IF NOT EXISTS idx_lead_identities_platform ON lead_identities(platform, account_id)",

    """CREATE TABLE IF NOT EXISTS lead_journey (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_id TEXT NOT NULL,
        actor TEXT NOT NULL,                  -- agent_a / agent_b / human:<user> / lead_self / system
        actor_device TEXT DEFAULT '',
        platform TEXT DEFAULT '',
        action TEXT NOT NULL,                 -- 事件类型枚举, 新增不改 schema
        data_json TEXT DEFAULT '{}',
        at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lead_journey_canonical ON lead_journey(canonical_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_lead_journey_action ON lead_journey(action, at)",
    "CREATE INDEX IF NOT EXISTS idx_lead_journey_actor ON lead_journey(actor, at)",

    """CREATE TABLE IF NOT EXISTS lead_handoffs (
        handoff_id TEXT PRIMARY KEY,
        canonical_id TEXT NOT NULL,
        source_agent TEXT NOT NULL,           -- 发起方 agent/device
        source_device TEXT DEFAULT '',
        target_agent TEXT DEFAULT '',          -- 接手方 agent/人(可空 = 等人认领)
        channel TEXT NOT NULL,                -- line / whatsapp / ...
        receiver_account_key TEXT DEFAULT '',  -- 接收方账号 key (配置在 referral_receivers.yaml)
        conversation_snapshot_json TEXT DEFAULT '[]',  -- 最近 N 轮对话 (已脱敏)
        snippet_sent TEXT DEFAULT '',          -- 发出的引流话术原文
        state TEXT NOT NULL DEFAULT 'pending',    -- pending / acknowledged / completed / rejected / expired / duplicate_blocked
        state_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        state_notes TEXT DEFAULT '',
        webhook_dispatched INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_canonical ON lead_handoffs(canonical_id)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_receiver ON lead_handoffs(receiver_account_key, state, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_state ON lead_handoffs(state, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_channel_dedup ON lead_handoffs(canonical_id, channel, state)",

    """CREATE TABLE IF NOT EXISTS agent_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_agent TEXT NOT NULL,
        to_agent TEXT NOT NULL,
        canonical_id TEXT DEFAULT '',         -- 关联的 lead (可空 = 通用消息)
        message_type TEXT NOT NULL,           -- query / reply / notification / command / ack
        correlation_id TEXT DEFAULT '',       -- request-response 配对 (query/reply 同一 id)
        payload_json TEXT DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',   -- pending / delivered / acknowledged / failed
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        delivered_at TEXT,
        acknowledged_at TEXT,
        error TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_agent_msg_to ON agent_messages(to_agent, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_msg_correlation ON agent_messages(correlation_id, message_type)",
    "CREATE INDEX IF NOT EXISTS idx_agent_msg_canonical ON agent_messages(canonical_id, created_at)",

    """CREATE TABLE IF NOT EXISTS lead_locks (
        canonical_id TEXT NOT NULL,
        action TEXT NOT NULL,                 -- 被锁的动作类型 (referring / chatting / merging)
        locked_by TEXT NOT NULL,              -- agent/device key
        acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL,             -- TTL 过期时间
        PRIMARY KEY (canonical_id, action)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lead_locks_expires ON lead_locks(expires_at)",

    """CREATE TABLE IF NOT EXISTS lead_merges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_canonical_id TEXT NOT NULL,    -- 被合并的(loser)
        target_canonical_id TEXT NOT NULL,    -- 合并到的(winner)
        merge_mode TEXT NOT NULL,             -- 'auto' / 'manual'
        confidence REAL DEFAULT 0.0,
        merge_reasons_json TEXT DEFAULT '[]', -- 触发合并的规则 list
        merged_by TEXT DEFAULT '',            -- 'system' / 'human:<user>'
        reverted_at TEXT,                     -- 若后来撤销, 填撤销时间
        reverted_reason TEXT DEFAULT '',
        merged_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lead_merges_source ON lead_merges(source_canonical_id)",
    "CREATE INDEX IF NOT EXISTS idx_lead_merges_target ON lead_merges(target_canonical_id)",

    """CREATE TABLE IF NOT EXISTS webhook_dispatches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,             -- handoff.created / handoff.completed / lead.merged ...
        target_url TEXT NOT NULL,
        payload_json TEXT DEFAULT '{}',
        related_canonical_id TEXT DEFAULT '',
        related_handoff_id TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', -- pending / delivered / failed / dead_letter
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT DEFAULT '',
        next_retry_at TEXT,                   -- 下次重试时间
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        delivered_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_webhook_pending ON webhook_dispatches(status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_related ON webhook_dispatches(related_handoff_id, related_canonical_id)",

    # ─── 2026-04-23 Phase 3 P3-3: 统一接触事件流水表 ──────────────────────
    # 动机:
    #   * facebook_friend_requests + facebook_inbox_messages 按"对象"组织,
    #     但漏斗分析需要按"事件序列"看(某人什么时候被加友/打招呼/回复/拒绝)。
    #   * B 做 Messenger 自动回复时, 需要回写"对方回复了 greeting"这一事件,
    #     把它放一张独立流水表最干净 —— 不污染 inbox 的 incoming 原始数据。
    #   * 后续 "接触配额" 建模(同一人 24h 被接触 N 次就算骚扰)直接查这张表。
    #
    # event_type 枚举:
    #   add_friend_sent / add_friend_risk / add_friend_accepted / add_friend_rejected
    #   greeting_sent / greeting_fallback / greeting_replied
    #   message_received (B 写入, 对方主动发起的消息)
    #   wa_referral_sent (B 写入, 引流话术发出)
    #
    # 由 A 和 B 共同写入; meta_json 放各自的扩展字段(JSON 字符串, 不强约束 schema)。
    """CREATE TABLE IF NOT EXISTS fb_contact_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        peer_name TEXT NOT NULL,
        event_type TEXT NOT NULL,
        template_id TEXT DEFAULT '',
        preset_key TEXT DEFAULT '',
        meta_json TEXT DEFAULT '',
        at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_contact_peer ON fb_contact_events(device_id, peer_name, at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_contact_type ON fb_contact_events(device_id, event_type, at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_contact_template ON fb_contact_events(template_id)",
    "CREATE INDEX IF NOT EXISTS idx_fb_contact_peer_global ON fb_contact_events(peer_name, at)",

    # ── Phase 8h (2026-04-24): Lead Blocklist ──────────────────────────
    # 跨 device / 跨 agent 的 peer 骚扰保护表. 运营在 Dashboard 一键
    # 把某个 peer 加黑后, 后续 A 端 add_friend / send_greeting 入口主动
    # skip + journey 记 peer_blocklisted. 不影响 B 端决策 (B 有自己的冷却).
    "CREATE TABLE IF NOT EXISTS lead_blocklist ("
    " canonical_id TEXT PRIMARY KEY,"
    " reason TEXT NOT NULL DEFAULT '',"  # 用户注解的原因
    " note TEXT DEFAULT '',"              # 自由备注
    " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " created_by TEXT DEFAULT ''"          # agent_a/operator 等
    ")",
    "CREATE INDEX IF NOT EXISTS idx_blocklist_created ON lead_blocklist(created_at DESC)",

    # ── Phase 11 (2026-04-25): LINE account pool ─────────────────────
    # 运营在后台管理可用于引流的 LINE 账号, 按轮循 (last_used_at ASC + daily cap)
    # 分配给 dispatcher / B 机 messenger 回复. region/persona_key 可做分群.
    "CREATE TABLE IF NOT EXISTS line_accounts ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " line_id TEXT NOT NULL UNIQUE,"              # @xxx / URL / phone
    " owner_device_id TEXT DEFAULT '',"            # 绑定设备 (可空 = 通用池)
    " persona_key TEXT DEFAULT '',"                # 限定 persona (可空 = 通用)
    " region TEXT DEFAULT '',"                     # 'jp' / 'it' / ...
    " status TEXT NOT NULL DEFAULT 'active',"      # active / cooldown / banned / disabled
    " last_used_at TEXT DEFAULT '',"
    " times_used INTEGER NOT NULL DEFAULT 0,"
    " daily_cap INTEGER NOT NULL DEFAULT 20,"      # 24h 分发上限
    " notes TEXT DEFAULT '',"
    " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_line_accounts_status_lastused"
    " ON line_accounts(status, last_used_at)",
    "CREATE INDEX IF NOT EXISTS idx_line_accounts_region_persona"
    " ON line_accounts(region, persona_key)",

    # Phase 11: 分发日志, 用于 24h 内 daily_cap 统计 + 审计
    "CREATE TABLE IF NOT EXISTS line_dispatch_log ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " line_account_id INTEGER NOT NULL,"
    " line_id TEXT NOT NULL,"
    " canonical_id TEXT NOT NULL DEFAULT '',"      # 引流给哪个 lead
    " peer_name TEXT DEFAULT '',"
    " source_device_id TEXT DEFAULT '',"            # 谁触发的 dispatch (A/B/operator)
    " source_event_id TEXT DEFAULT '',"             # fb_contact_events.id 或其它
    " status TEXT NOT NULL DEFAULT 'planned',"      # planned / sent / failed / skipped
    " note TEXT DEFAULT '',"
    " created_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_line_dispatch_account_time"
    " ON line_dispatch_log(line_account_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_line_dispatch_canonical"
    " ON line_dispatch_log(canonical_id, created_at DESC)",

    # ── Phase 18 (2026-04-25): peer_name reject 持久化日志 ──
    # 跨进程重启可查的 reject 历史 (上一阶段进程内 ring buffer 重启清零).
    # 用于 daily summary + 趋势分析 + 调 sanitize 规则.
    "CREATE TABLE IF NOT EXISTS peer_name_reject_log ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " peer_name TEXT NOT NULL DEFAULT '',"          # 被拦的 peer (前 40 字符)
    " event_type TEXT NOT NULL DEFAULT '',"
    " device_id TEXT NOT NULL DEFAULT '',"
    " at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_peer_reject_at"
    " ON peer_name_reject_log(at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_peer_reject_event"
    " ON peer_name_reject_log(event_type, at DESC)",
    # Phase 20.1.9.1 (2026-04-25): alert 触发历史 — 跨进程持久化, 用于
    # 回顾 7-30d alert 频率 / 分布 / 是否反复同一 type.
    "CREATE TABLE IF NOT EXISTS fb_alert_history ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " alert_type TEXT NOT NULL DEFAULT '',"
    " severity TEXT NOT NULL DEFAULT '',"
    " message TEXT NOT NULL DEFAULT '',"
    " region TEXT NOT NULL DEFAULT '',"        # per-region alert 用 (Phase 20.1.9.2)
    " context_json TEXT NOT NULL DEFAULT '{}',"
    " fired_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_alert_fired_at"
    " ON fb_alert_history(fired_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_alert_type_fired"
    " ON fb_alert_history(alert_type, fired_at DESC)",

    # ─── PR-6 真人后台扩展: lead_handoffs 加 7 列 ────────────────────
    "ALTER TABLE lead_handoffs ADD COLUMN assigned_to_username TEXT DEFAULT ''",
    "ALTER TABLE lead_handoffs ADD COLUMN assigned_at TEXT DEFAULT ''",
    "ALTER TABLE lead_handoffs ADD COLUMN customer_service_replies_json TEXT DEFAULT '[]'",
    "ALTER TABLE lead_handoffs ADD COLUMN internal_notes_json TEXT DEFAULT '[]'",
    "ALTER TABLE lead_handoffs ADD COLUMN outcome TEXT DEFAULT ''",
    "ALTER TABLE lead_handoffs ADD COLUMN outcome_notes TEXT DEFAULT ''",
    "ALTER TABLE lead_handoffs ADD COLUMN outcome_at TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_assigned"
    " ON lead_handoffs(assigned_to_username, state)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_outcome"
    " ON lead_handoffs(outcome, outcome_at DESC) WHERE outcome != ''",

    # ─── Phase A1+B2: 跨设备去重防线 ─────────────────────────────────
    # fb_contact_events 加 canonical_id: 跨设备事件可按统一身份聚合
    # fb_contact_events 加 reply_owner_device: 对话所有权锁 (B1)
    "ALTER TABLE fb_contact_events ADD COLUMN canonical_id TEXT DEFAULT ''",
    "ALTER TABLE fb_contact_events ADD COLUMN reply_owner_device TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_fb_contact_canonical"
    " ON fb_contact_events(canonical_id, event_type, at)",
    "CREATE INDEX IF NOT EXISTS idx_fb_contact_reply_owner"
    " ON fb_contact_events(peer_name, reply_owner_device)"
    " WHERE reply_owner_device != ''",
    # facebook_friend_requests 加 canonical_id: 全局去重查询
    "ALTER TABLE facebook_friend_requests ADD COLUMN canonical_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_canonical"
    " ON facebook_friend_requests(canonical_id, status)",
    # 全局 target_name 去重索引 (跨 device_id 查某人是否被任何设备加过)
    "CREATE INDEX IF NOT EXISTS idx_fb_fr_target_global"
    " ON facebook_friend_requests(target_name, status, sent_at)",

    # ─── Phase I3: 合并审核状态 ─────────────────────────────────────
    # audit_status: '' (未审核) / 'safe' / 'suspect'
    "ALTER TABLE lead_merges ADD COLUMN audit_status TEXT DEFAULT ''",
    "ALTER TABLE lead_merges ADD COLUMN reviewed_by TEXT DEFAULT ''",
    "ALTER TABLE lead_merges ADD COLUMN reviewed_at TEXT",

    # ─── Phase K2: Lead 生命周期状态 ─────────────────────────────────
    # lifecycle_stage: new → contacted → engaged → qualified → converted / lost
    "ALTER TABLE leads_canonical ADD COLUMN lifecycle_stage TEXT DEFAULT 'new'",
    "ALTER TABLE leads_canonical ADD COLUMN lifecycle_updated_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_canonical_lifecycle"
    " ON leads_canonical(lifecycle_stage, lifecycle_updated_at)",

    # ─── 2026-05-12: 智能社群互动引擎 — 帖子互动记录 ───
    """CREATE TABLE IF NOT EXISTS fb_post_engagements (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id       TEXT    NOT NULL,
        group_name      TEXT    NOT NULL,
        author_name     TEXT    NOT NULL,
        post_snippet    TEXT    DEFAULT '',
        post_topic      TEXT    DEFAULT '',
        l1_score        REAL    DEFAULT 0,
        content_score   REAL    DEFAULT 0,
        total_score     REAL    DEFAULT 0,
        tier            TEXT    DEFAULT '',
        action_liked    INTEGER DEFAULT 0,
        action_commented INTEGER DEFAULT 0,
        comment_text    TEXT    DEFAULT '',
        action_profile_visited INTEGER DEFAULT 0,
        action_friend_sent INTEGER DEFAULT 0,
        friend_request_id INTEGER,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fpe_device ON fb_post_engagements(device_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_fpe_author ON fb_post_engagements(author_name, group_name)",
    "CREATE INDEX IF NOT EXISTS idx_fpe_tier ON fb_post_engagements(tier, created_at)",
]


_PRE_MIGRATIONS = [
    # 2026-04-24: 修复 audit_logs schema drift (B 机 PR #17 报告).
    # 老 DB 该表列名是 `ts`, _SCHEMA 里已改为 `timestamp`, 但 CREATE INDEX
    # IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp) 在老表找不到该列
    # **直接抛异常, executescript 整体中断, 下游 FB 业务表全建不起来**.
    # 必须在 executescript *之前* 先把老列改名, 索引建立才能继续.
    # 新 DB 没 `ts` 列 → OperationalError → try/except 忽略.
    "ALTER TABLE audit_logs RENAME COLUMN ts TO timestamp",
]


def init_db():
    """建表（幂等）+ 增量迁移。服务启动时调用一次。

    执行顺序:
      1. _PRE_MIGRATIONS — 在 executescript 前必须跑的 schema 漂移修复
         (老列重命名, 否则 CREATE INDEX 在老表找不到新列会炸 executescript)
      2. executescript(_SCHEMA) — 幂等建表 + 索引 (新 DB 走这里)
      3. _MIGRATIONS — 常规增量列/表/索引 (老 DB 升级走这里)
    """
    conn = _connect()
    try:
        # 步骤 1: pre-migrate (必须在 executescript 之前)
        for sql in _PRE_MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()

        # 步骤 2: schema (幂等 CREATE TABLE IF NOT EXISTS + 索引)
        conn.executescript(_SCHEMA)

        # 步骤 3: 常规 migrations
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        logger.info("数据库初始化完成: %s", DB_PATH)
    finally:
        conn.close()


@contextmanager
def get_conn():
    """上下文管理器，自动 commit/rollback/close。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
