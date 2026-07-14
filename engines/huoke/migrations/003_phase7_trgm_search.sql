-- Phase-7: pg_trgm + GIN trigram index 加速 chat 搜索
-- 32 客户量级 ILIKE 也够快, 但短期内会膨胀到 30 设备 × N 客户, 提前打底

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- trigram GIN 加速 ILIKE / % 操作 (与已有 idx_chats_content_gin 共存,
-- 后者是 to_tsvector('simple') 全文索引, 这里是 trigram 模糊匹配)
CREATE INDEX IF NOT EXISTS idx_chats_content_trgm
    ON customer_chats USING GIN (content gin_trgm_ops);

-- 客户姓名模糊搜索也加速 (search_customers 的 primary_name ILIKE)
CREATE INDEX IF NOT EXISTS idx_customers_name_trgm
    ON customers USING GIN (primary_name gin_trgm_ops);
