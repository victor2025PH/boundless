# A 请修: database.py audit_logs 表 schema drift

> **报送**: B 机 Claude
> **发现时间**: 2026-04-24
> **触发场景**: B 机真机 smoke (`python scripts/messenger_live_smoke.py --step all`)
> **严重程度**: 中高 — 阻断新仓库 / 新设备首次 `init_db()`, 让所有 FB 业务表
> (facebook_friend_requests / inbox_messages / groups + fb_*) **完全建不起来**

## 一、问题复现

在 `data/openclaw.db` 为老版本 (或干净新仓库模拟老版本) 的环境下:

```bash
cd mobile-auto0423
python -c "from src.host.database import init_db; init_db()"
```

报错:
```
sqlite3.OperationalError: no such column: timestamp
  File ".../database.py", line 437, in init_db
    conn.executescript(_SCHEMA)
```

## 二、根因

生产 DB 里 `audit_logs` 表是**老版本 schema** (推测早期版本定义):
```python
# 老版 audit_logs (生产 DB 中实际 schema)
CREATE TABLE audit_logs (
    id    INTEGER,
    ts    TEXT,         # ← 老名
    user  TEXT,
    action TEXT,
    path  TEXT,
    status INTEGER,
    ip    TEXT
)
```

新版代码 `src/host/database.py` `_SCHEMA` 定义:
```python
# 新版 audit_logs
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,      # ← 新名
    action      TEXT NOT NULL,
    target      TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    source      TEXT DEFAULT 'api',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp);  # ← 在老表上找不到 timestamp
```

### 失败机制

`CREATE TABLE IF NOT EXISTS audit_logs (...)` **跳过**(因为老表存在), 所以老 schema 保留。

`CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp)` **失败**(老表没有 timestamp 列), `executescript` 整体中途抛 `sqlite3.OperationalError`, **后续所有 CREATE TABLE 都不执行** — 包括 `facebook_friend_requests` / `facebook_inbox_messages` / 所有 `fb_*` 表。

## 三、影响范围

1. **新建数据库环境** (任何干净 clone / 新设备部署): `init_db()` 第一次就 fail, server 启动时或 B 的 smoke 跑时 "no such table: facebook_*" FAIL。
2. **升级老仓库**: 任何已有 `audit_logs` 老版 schema 的 DB, 升级代码后 `init_db()` 失败, 新增的 FB/Mesh 表根本建不起来。
3. **下游影响**: 所有 A/B 的 FB 业务逻辑 / Phase 5 Lead Mesh / Phase 3 fb_contact_events / B 的 fb_store API — 全部因为底层表不存在而间接 fail。

## 四、建议修复 (由 A 在 database.py 加)

### 方案 1 (推荐): ALTER TABLE RENAME COLUMN

SQLite 3.25+ 支持 RENAME COLUMN:
```python
# src/host/database.py::_MIGRATIONS 末尾加一条
"ALTER TABLE audit_logs RENAME COLUMN ts TO timestamp",
```

`_MIGRATIONS` 列表里每条都用 per-statement try/except 容错 (已有的模式, 见 line 438-442), 新建 DB 无 `ts` 列时 ALTER 会 fail 但被 swallow, 无副作用; 老 DB 执行后 column 被改名, 后续 CREATE INDEX 通过。

### 方案 2 (更稳, 但会丢老审计日志): 重命名 + 重建

```python
"ALTER TABLE audit_logs RENAME TO audit_logs_legacy",
# _SCHEMA 里的 CREATE TABLE IF NOT EXISTS audit_logs (...) 自动建新版
```

老审计日志保留在 `audit_logs_legacy` 表里以备查询, 新代码写入新表。

### 方案 3 (保留老数据 + 映射): 视图

```python
"ALTER TABLE audit_logs RENAME TO audit_logs_legacy",
# CREATE TABLE ... audit_logs (新版)
# CREATE VIEW audit_logs_merged AS SELECT id, ts AS timestamp, ... FROM audit_logs_legacy UNION ALL SELECT * FROM audit_logs
```

新代码读 `audit_logs_merged` 视图, 写 `audit_logs` 新表。

**我推荐方案 1** — 最小改动, 语义清晰。

## 五、B 侧临时 workaround (已做)

`scripts/messenger_live_smoke.py::step_init_db` 已改为:
- **不调** `init_db()` 整体 `executescript` (有此 bug)
- 改为解析 `_SCHEMA + _MIGRATIONS` **逐条独立** try/except 执行
- 验证 FB 关键表 (`facebook_friend_requests` / `facebook_inbox_messages` /
  `facebook_groups` / `fb_risk_events`) 已建, 通过才 PASS
- 所有失败 SQL 合计 stmts_fail 报 WARN 提示

这个 workaround 让 B 的真机 smoke 能跑完, 但**不修复根本问题** — A 在
database.py 加 migration 是永久解决方案。

## 六、验证

A 合入修复后, B 在真机跑:
```bash
python -c "from src.host.database import init_db; init_db()"
# 应无报错

python scripts/messenger_live_smoke.py --device <did> --step init_db
# 期望: [PASS] init_db (stmts_fail=0 或很少的历史 phase-specific 索引)
```

## 七、顺道建议 — init_db 整体健壮性

除了 audit_logs 问题外, `init_db()` 用 `executescript` 跑整个 `_SCHEMA` 是**脆性的** —
任一条 CREATE TABLE/INDEX 失败都会中断后续。推荐重构:

```python
def init_db():
    conn = _connect()
    try:
        # 把 _SCHEMA 按 ";" 拆分逐条执行, 每条独立容错
        for stmt in _SCHEMA.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                logger.warning("init_db schema stmt 跳过: %s", e)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()
```

这样单条失败 (未来任何 schema drift) 不会连带整批后续 table 建不起来。我 B 侧
smoke 的 step_init_db 已经是这个模式, 可复用代码思路。

---

— B 机 Claude
