-- L2 中央客户画像 schema (2026-04-26)
-- 作用: 跨 worker (10 PC × 20 phones = 200 设备) 客户画像 + 聊天历史 + 漏斗事件中央化
-- 部署: 主控 192.168.0.118:5432 / db=openclaw / user=openclaw_app
-- 各 worker 通过 HTTP push API 写入 (write-through, async)

BEGIN;

-- ── schema 版本表 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description TEXT
);

INSERT INTO _schema_version (version, description)
VALUES (1, 'L2 central customer store: customers + events + chats + handoffs')
ON CONFLICT (version) DO NOTHING;

-- ── customers (master 表) ────────────────────────────────────────────
-- Phase-3: priority_tag 客户分群 (high/medium/low) - 由 customer_sync_bridge 启发式更新
-- 直接 SQL: ALTER TABLE customers ADD COLUMN IF NOT EXISTS priority_tag TEXT DEFAULT 'medium';
-- 已有 customers 表的环境用上面 ALTER, 新建走下面 CREATE
CREATE TABLE IF NOT EXISTS customers (
    customer_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id TEXT NOT NULL,            -- 业务侧 ID, 例: fb_uid / messenger_thread_id / line_user_id
    canonical_source TEXT NOT NULL,         -- "facebook" / "messenger" / "line" / "telegram"
    primary_name TEXT,
    age_band TEXT,                          -- "20s" / "30s" / "40s" / ...
    gender TEXT,                            -- "male" / "female" / "unknown"
    country TEXT,                           -- ISO2 e.g. "JP" / "TW" / "US"
    interests TEXT[],                       -- ["family", "travel", "food"]
    ai_profile JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 完整 AI 画像 (可扩字段)
    status TEXT NOT NULL DEFAULT 'in_funnel',
        -- in_funnel: 漏斗中, 未转化
        -- in_messenger: messenger 聊天中
        -- in_line: 引到 LINE, 等人工接管
        -- accepted_by_human: 人工已接管
        -- converted: 真转化 (打款 / 实际成交)
        -- lost: 流失
    priority_tag TEXT NOT NULL DEFAULT 'medium',  -- Phase-3: high/medium/low 客户分群
    last_worker_id TEXT,                    -- 最后写入的 worker (e.g. "worker-175")
    last_device_id TEXT,                    -- 最后操作的设备 serial
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (canonical_source, canonical_id)
);

-- Phase-3/6: 兼容老 db 升级 (CREATE TABLE IF NOT EXISTS 不会加新列)
ALTER TABLE customers ADD COLUMN IF NOT EXISTS priority_tag TEXT NOT NULL DEFAULT 'medium';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS custom_tags TEXT[] DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_customers_custom_tags ON customers USING GIN (custom_tags);

CREATE INDEX IF NOT EXISTS idx_customers_status_updated
    ON customers (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_customers_country
    ON customers (country) WHERE country IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customers_worker
    ON customers (last_worker_id, updated_at DESC);
-- AI profile 内字段查询 (e.g. ai_profile->>'topic_preference' = 'cooking')
CREATE INDEX IF NOT EXISTS idx_customers_ai_profile_gin
    ON customers USING GIN (ai_profile);

-- ── customer_events (append-only 业务事件) ───────────────────────────
CREATE TABLE IF NOT EXISTS customer_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
        -- 例: friend_request_sent / friend_request_accepted /
        --     greeting_sent / greeting_replied /
        --     messenger_message_sent / messenger_message_received /
        --     line_handoff_sent / line_first_text_received /
        --     handoff_accepted_by_human / customer_converted / customer_lost
    worker_id TEXT NOT NULL,
    device_id TEXT,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_customer_ts
    ON customer_events (customer_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_ts
    ON customer_events (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_worker_ts
    ON customer_events (worker_id, ts DESC);

-- ── customer_chats (完整聊天历史) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS customer_chats (
    chat_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,                  -- "facebook" / "messenger" / "line"
    direction TEXT NOT NULL,                -- "incoming" (客户发) / "outgoing" (我发)
    content TEXT NOT NULL,
    content_lang TEXT,                      -- "ja" / "zh-CN" / "en" / null
    ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
    template_id TEXT,                       -- 用模板生成时
    worker_id TEXT,
    device_id TEXT,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chats_customer_ts
    ON customer_chats (customer_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_chats_channel_ts
    ON customer_chats (channel, ts DESC);
-- 全文搜索 (人工坐席搜聊天内容)
CREATE INDEX IF NOT EXISTS idx_chats_content_gin
    ON customer_chats USING GIN (to_tsvector('simple', content));

-- ── customer_handoffs (引流人机交接状态机) ──────────────────────────
CREATE TABLE IF NOT EXISTS customer_handoffs (
    handoff_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    from_stage TEXT NOT NULL,               -- "messenger"
    to_stage TEXT NOT NULL,                 -- "line"
    initiated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_by_human TEXT,                 -- 坐席 ID (NULL = 待接管)
    accepted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    outcome TEXT,                           -- "accepted" / "lost" / "timeout" / NULL (in_progress)
    ai_summary TEXT,                        -- AI 自动总结的客户上下文 + 兴趣 (人工接管时一眼看懂)
    initiating_worker_id TEXT,
    initiating_device_id TEXT,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_handoffs_pending
    ON customer_handoffs (initiated_at DESC) WHERE accepted_by_human IS NULL;
CREATE INDEX IF NOT EXISTS idx_handoffs_human
    ON customer_handoffs (accepted_by_human, accepted_at DESC)
    WHERE accepted_by_human IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_handoffs_customer
    ON customer_handoffs (customer_id, initiated_at DESC);

-- ── updated_at 自动更新 trigger ──────────────────────────────────────
CREATE OR REPLACE FUNCTION _set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_customers_updated_at ON customers;
CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION _set_updated_at();

COMMIT;
