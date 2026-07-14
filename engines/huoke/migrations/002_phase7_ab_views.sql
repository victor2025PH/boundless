-- Phase-7: A/B experiments lifecycle + customer saved views

CREATE TABLE IF NOT EXISTS ab_experiments (
    experiment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    variants TEXT[] NOT NULL DEFAULT ARRAY['v1','v2'],
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    winner TEXT,
    samples JSONB DEFAULT '{}',
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ab_exp_status ON ab_experiments(status, started_at DESC);

CREATE TABLE IF NOT EXISTS customer_views (
    view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    owner_username TEXT NOT NULL DEFAULT 'admin',
    params_json JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cust_views_owner ON customer_views(owner_username, created_at DESC);

INSERT INTO ab_experiments (name, status, variants, note)
SELECT 'baseline_v1_vs_v2', 'running', ARRAY['v1','v2'], 'Phase-3 initial A/B'
WHERE NOT EXISTS (SELECT 1 FROM ab_experiments);
