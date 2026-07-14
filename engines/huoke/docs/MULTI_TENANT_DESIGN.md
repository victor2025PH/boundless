# 多租户改造设计文档

> 状态: **设计阶段** (未实施)
> 优先级: P0 (商业化 SaaS blocker)
> 工作量预估: **3-5 天** (1 人)
> 适用版本: OpenClaw v1.3+ (当前 v1.2)

---

## 1. 背景

OpenClaw 当前是**单租户**架构 — 所有客户数据混在同一个 PG database。要做 SaaS 卖给多个企业客户, 必须做多租户改造让数据完全隔离。

### 当前架构问题
- 客户 A 的 customers / customer_events / customer_chats / customer_handoffs 全在同一表
- 客户 A 的 admin 能查到客户 B 的客户列表
- A/B 实验、决策聚合、客服 SLA 都跨租户混着算

---

## 2. 三种多租户架构对比

| 维度 | Schema-per-tenant | Database-per-tenant | Row-level (tenant_id) |
|---|---|---|---|
| 隔离强度 | 中 | 强 | 弱 (依赖应用层 + RLS) |
| 跨租户聚合 | 难 | 难 | 容易 (admin 跨租户报表) |
| schema 迁移 | N 次 | N 次 | 1 次 |
| 备份/还原 | 中等 | 简单 | 复杂 |
| 成本 | 中 | 高 | 低 |
| 适合规模 | < 100 租户 | < 50 租户 | < 10000 租户 |

### 推荐: **Row-level + PG RLS (Row-Level Security)**

理由:
- OpenClaw 目标客户 < 100 (B2B 中型 SaaS), 数据量 < 千万级
- 跨租户管理 (admin 看全平台漏斗) 简单
- 1 个 PG instance 维护成本低
- PG 16 RLS 性能成熟, 隔离强度足够

---

## 3. Schema 改造方案

### 3.1 新建 tenants 表

```sql
CREATE TABLE tenants (
    tenant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'trial',  -- trial / standard / enterprise
    max_devices INT NOT NULL DEFAULT 5,
    max_workers INT NOT NULL DEFAULT 1,
    brand_name TEXT DEFAULT 'OpenClaw',
    brand_logo_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    contact_email TEXT,
    persona_yaml TEXT,  -- 企业自定义 referral_strategies.yaml 内容
    notify_webhook TEXT,
    notify_type TEXT
);

CREATE INDEX idx_tenants_active ON tenants(is_active);
```

### 3.2 现有表加 tenant_id (PG migration)

```sql
-- 1. 加列 (NULL 允许, 现有数据迁移到 default tenant)
ALTER TABLE customers ADD COLUMN tenant_id UUID REFERENCES tenants(tenant_id);
ALTER TABLE customer_events ADD COLUMN tenant_id UUID REFERENCES tenants(tenant_id);
ALTER TABLE customer_chats ADD COLUMN tenant_id UUID REFERENCES tenants(tenant_id);
ALTER TABLE customer_handoffs ADD COLUMN tenant_id UUID REFERENCES tenants(tenant_id);
ALTER TABLE ab_experiments ADD COLUMN tenant_id UUID REFERENCES tenants(tenant_id);
ALTER TABLE customer_views ADD COLUMN tenant_id UUID REFERENCES tenants(tenant_id);

-- 2. 创建 default tenant 给现有数据
INSERT INTO tenants (tenant_id, name, plan)
VALUES ('00000000-0000-0000-0000-000000000000', 'default', 'enterprise');

UPDATE customers SET tenant_id = '00000000-0000-0000-0000-000000000000' WHERE tenant_id IS NULL;
UPDATE customer_events SET tenant_id = '00000000-0000-0000-0000-000000000000' WHERE tenant_id IS NULL;
-- ... (每个表)

-- 3. 加 NOT NULL + 索引
ALTER TABLE customers ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE customer_events ALTER COLUMN tenant_id SET NOT NULL;
-- ...

CREATE INDEX idx_customers_tenant ON customers(tenant_id);
CREATE INDEX idx_events_tenant_ts ON customer_events(tenant_id, ts DESC);
CREATE INDEX idx_chats_tenant_ts ON customer_chats(tenant_id, ts DESC);
CREATE INDEX idx_handoffs_tenant ON customer_handoffs(tenant_id, initiated_at DESC);
```

### 3.3 启用 PG RLS

```sql
-- 每张业务表启用 RLS
ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ab_experiments ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_views ENABLE ROW LEVEL SECURITY;

-- Policy: 只能看 current_tenant
CREATE POLICY tenant_isolation ON customers
    USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);
-- ... (每张表同样的 policy)

-- bypass policy 给超级管理员 (跨租户报表)
CREATE POLICY admin_full_access ON customers
    USING (current_setting('app.is_super_admin', TRUE)::BOOLEAN = TRUE);
```

---

## 4. 应用层改造

### 4.1 中间件 — 每个请求设 tenant 上下文

`src/host/routers/auth.py`:

```python
from fastapi import Request, HTTPException
from contextvars import ContextVar

current_tenant: ContextVar[str] = ContextVar("current_tenant", default="")

async def tenant_middleware(request: Request, call_next):
    # 从 JWT / API key / cookie 提取 tenant_id
    auth_header = request.headers.get("Authorization", "")
    api_key = request.headers.get("X-API-Key", "")
    
    tenant_id = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # decode JWT, 提 tenant_id
        tenant_id = _decode_jwt(token).get("tenant_id")
    elif api_key:
        # 查 api_keys 表, 找对应 tenant_id
        tenant_id = _api_key_to_tenant(api_key)
    
    if not tenant_id and not _is_public_path(request.url.path):
        raise HTTPException(401, "未登录或缺 tenant_id")
    
    current_tenant.set(tenant_id or "")
    response = await call_next(request)
    return response
```

### 4.2 PG session 设 tenant

`src/host/central_customer_store.py::_conn`:

```python
@contextmanager
def _conn(self):
    c = self._pool.getconn()
    try:
        # 每次借连接, 设当前 tenant_id session 变量
        # PG RLS policy 会自动按这个变量过滤
        tenant_id = current_tenant.get() or "00000000-0000-0000-0000-000000000000"
        with c.cursor() as cur:
            cur.execute("SET app.current_tenant = %s", (tenant_id,))
        yield c
        c.commit()
    except (UnicodeDecodeError, psycopg2.OperationalError):
        # 现有 discard 逻辑
        ...
```

### 4.3 store 层 — 写入时塞 tenant_id

```python
def upsert_customer(self, ...):
    tenant_id = current_tenant.get() or _DEFAULT_TENANT
    with self._cursor() as cur:
        cur.execute("""
            INSERT INTO customers (tenant_id, customer_id, canonical_id, ...)
            VALUES (%s, %s, %s, ...)
            ON CONFLICT (tenant_id, canonical_source, canonical_id) DO UPDATE SET ...
        """, (tenant_id, ...))
```

### 4.4 RBAC 三级权限

```sql
CREATE TABLE tenant_users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    username TEXT NOT NULL,
    email TEXT,
    password_hash TEXT,
    role TEXT NOT NULL,  -- 'tenant_admin' / 'agent' / 'viewer'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, username)
);

CREATE TABLE api_keys (
    api_key TEXT PRIMARY KEY,  -- hashed
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    name TEXT,  -- 'production' / 'staging' / etc
    scopes TEXT[],  -- ['read', 'write', 'admin']
    created_at TIMESTAMPTZ DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
```

### 4.5 Endpoint 改造示例

`/cluster/customers` (现有):
```python
@router.get("/cluster/customers")
def list_customers(...):
    store = _safe_get_store()
    return store.list_customers(...)  # PG 自动按 RLS 过滤当前 tenant
```

不需要改业务代码！只要中间件设了 `app.current_tenant`, RLS 自动隔离。

---

## 5. dashboard 改造

### 5.1 顶部加 tenant 选择器 (super admin 跨租户)

```html
<select id="tenant-select" onchange="switchTenant()">
    <option value="">— 当前租户 —</option>
    <!-- super admin 看到所有 tenant, 普通用户只看自己的 -->
</select>
```

### 5.2 白标支持

dashboard HTML 顶部 logo / 标题从 `tenants.brand_name` / `tenants.brand_logo_url` 读:

```python
# 新 endpoint
@router.get("/dashboard/branding")
def get_branding():
    tenant_id = current_tenant.get()
    with store._cursor() as cur:
        cur.execute("SELECT brand_name, brand_logo_url FROM tenants WHERE tenant_id=%s", (tenant_id,))
        row = cur.fetchone()
    return {"brand_name": row["brand_name"], "logo": row["brand_logo_url"]}
```

dashboard JS 启动时拉这个端点, 替换 `<title>` + 顶部 logo。

---

## 6. 计费 + 配额

### 6.1 配额检查

```python
def _check_tenant_quota(action: str, tenant_id: str):
    """每次创建 device / 加 worker / push 任务前检查."""
    with store._cursor() as cur:
        cur.execute("SELECT plan, max_devices, max_workers, expires_at, is_active FROM tenants WHERE tenant_id=%s", (tenant_id,))
        t = cur.fetchone()
    if not t['is_active']:
        raise HTTPException(403, "租户已停用")
    if t['expires_at'] and t['expires_at'] < datetime.utcnow():
        raise HTTPException(402, "订阅已过期, 请续费")
    if action == 'add_device':
        cur.execute("SELECT COUNT(*) FROM device_aliases WHERE tenant_id=%s", (tenant_id,))
        cnt = cur.fetchone()[0]
        if cnt >= t['max_devices']:
            raise HTTPException(403, f"已达 {t['max_devices']} device 上限")
```

### 6.2 用量计费

每天 daily_snapshot 时累计:
- `devices_active`: distinct device_id with task in 24h
- `tasks_total`: COUNT(*) tasks
- `llm_calls`: count from chat_brain calls
- `llm_tokens`: sum approximate

存到新表:

```sql
CREATE TABLE tenant_usage_daily (
    tenant_id UUID,
    date DATE,
    devices_active INT,
    tasks_total INT,
    llm_calls INT,
    llm_tokens BIGINT,
    referral_decisions INT,
    PRIMARY KEY (tenant_id, date)
);
```

按 plan 计费:
- trial: 免费, 最多 3 device × 30 天
- standard: $99/device/月, max 30 devices
- enterprise: $79/device/月, unlimited, 含 SLA

---

## 7. 迁移策略 (从单租户到多租户)

### 阶段 1 (Day 1-2): 后端 schema + RLS + 中间件
- 应用 migrations (不停服, RLS 默认全 bypass policy)
- 部署中间件 (兼容: 没 tenant_id 时走 default tenant)
- 灰度 1 天验证

### 阶段 2 (Day 3): 强制 RLS
- 删 bypass policy
- 所有请求必须有 tenant_id
- 监控 RLS 日志确保 0 数据泄漏

### 阶段 3 (Day 4-5): UI + 计费
- dashboard 加 tenant 选择 + 白标
- 计费模块 + 配额
- 邀请第一个真实客户作为非 default tenant

---

## 8. 风险 + 缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| RLS policy 漏写表导致数据混 | 中 | 高 | 全表 review + 自动化测试覆盖 |
| 现有 PG 索引性能下降 | 中 | 中 | 加 (tenant_id, ...) 复合索引 |
| 应用层忘传 tenant_id 跨租户写错 | 低 | 高 | DB INSERT 触发器强制校验 |
| migrations 失败需回滚 | 低 | 高 | 每步 migration 配 rollback SQL |
| 老客户升级时数据丢失 | 低 | 极高 | 升级前 pg_dump 全量备份 |

---

## 9. 替代方案: Schema-per-tenant (当 RLS 性能不够时)

如果未来某个客户 customers 表 > 1 亿行:
- 切到 schema-per-tenant: `tenant_xxx.customers`
- search_path 路由: `SET search_path TO tenant_xxx, public`
- 优点: PG 索引 100% per-tenant, 无 RLS overhead
- 缺点: schema 数 = 客户数, 维护负担升

但在 100 客户 + 千万级数据规模下, RLS 完全够用。

---

## 10. 实施 checklist

P0 (Day 1-3):
- [ ] migrations 写好 + 应用 (本地 dev)
- [ ] tenant_middleware 写完 + 单测覆盖
- [ ] _conn SET app.current_tenant 改造
- [ ] RLS policy 写完 6 个核心表
- [ ] e2e_smoke 在多租户模式跑通

P1 (Day 4-5):
- [ ] dashboard 白标 (logo / title)
- [ ] tenant_users 表 + RBAC 三级
- [ ] api_keys 表 + 中间件支持
- [ ] tenant 选择器 (super admin 用)

P2 (Day 6-7, 不在 MVP):
- [ ] 计费模块 (配额检查 + 用量统计)
- [ ] tenant 自助注册 / onboarding
- [ ] Stripe 集成

---

## 11. 文档清单 (实施完后更新)

- INSTALL.md 加多租户配置
- OPS_RUNBOOK.md 加 tenant 管理命令
- 新增 docs/TENANT_ADMIN.md (super admin 操作手册)
- 新增 docs/API.md (含 tenant 鉴权)

---

> 此文档为设计阶段, 实施时按 §10 checklist 推进, 每完成 1 个 P0 task 就更新本文档状态。
