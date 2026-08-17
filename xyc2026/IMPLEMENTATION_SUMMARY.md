# TrustCheck Bot — MVP 实施总结与优化说明

## 一、已交付内容

### 1. 运行方式

- **API**：`python run_api.py` → http://0.0.0.0:8001（可通过环境变量 `PORT` 修改）  
- **Bot**：先启动 API，再 `python run_bot.py`；`.env` 中 `API_BASE_URL` 需与 API 端口一致（如 `http://127.0.0.1:8001`）  
- **环境**：复制 `.env.example` 为 `.env`，填写 `BOT_TOKEN`、可选 `ADMIN_TG_IDS`  
- **Bot 链接**：[https://t.me/xyc2026_bot](https://t.me/xyc2026_bot)  
- **测试时重建库**：`python rebuild_db.py` 删除所有表，再重启 API 即按当前模型重建，无需迁移。

### 2. 已实现功能

| 模块 | 内容 |
|------|------|
| **Bot** | `/start` 欢迎与说明、`/check <id\|@username\|wallet>`、`/mycredit`、`/help`、结果卡片「Report / Incorrect?」、举报流程（FSM）、`/stats`（管理员） |
| **API** | `GET /v1/check`（user_id/username/wallet）、`GET /v1/mycredit`、`POST /v1/report`、`GET /v1/admin/stats`、`GET /health` |
| **数据** | SQLAlchemy 模型：users、blacklist、reports、wallet_entity_graph、feedback、query_log；启动时自动建表 |
| **评分** | 规则引擎：0–100 分、四档风险等级、tips 规则（账号年龄、Premium、高危群、黑名单、举报次数） |
| **安全** | Token 与密钥仅从 `.env` 读取；`.env` 已加入 `.gitignore`；管理接口可选用 `API_INTERNAL_KEY` |

### 3. 项目结构

```
d:\xyc2026\
  .env.example, .env, .gitignore
  config.py              # 配置（pydantic-settings）
  database.py            # 异步 DB 引擎与会话
  models.py              # User, Blacklist, Report, WalletEntityGraph, Feedback, QueryLog
  scoring.py             # 规则引擎（纯函数）
  services.py            # 解析 username/wallet、查/建用户、评分、举报、统计
  api/
    main.py              # FastAPI + v1 路由 + lifespan 建表
    schemas.py           # 请求/响应模型
  bot/
    main.py              # aiogram Dispatcher + 轮询
    api_client.py        # 调用 API 的 HTTP 客户端
    i18n.py, keyboards.py
    handlers/            # start, check, mycredit, help_cmd, report, admin
  run_api.py, run_bot.py
  README.md
```

---

## 二、实施过程中的优化（相对原方案）

### 2.1 架构与实现

- **API 与 Bot 分离**：Bot 仅通过 HTTP 调 API，无直接 DB 依赖，便于日后多实例、Mini App 共用同一 API。  
- **username 解析在 API**：`/v1/check?username=@x` 在服务端用 Telegram `getChat` 解析为 `tg_id`，Bot 无需传 token，逻辑集中在一处。  
- **数据库 URL 兼容**：`database.py` 自动把 `sqlite://` 转为 `sqlite+aiosqlite://`，把 `postgresql://` 转为 `postgresql+asyncpg://`，避免同步驱动错误；MVP 默认 SQLite，生产可切 PostgreSQL。  
- **启动时建表**：FastAPI lifespan 内 `Base.metadata.create_all`，并 `import models` 保证表注册，无需单独跑迁移即可跑通。

### 2.2 数据与评分

- **首次查询即建用户**：查一个不存在的 tg_id 时自动插入 `users` 并打分，避免“查无此人”时无法展示结果；无记录的钱包仍返回友好说明。  
- **评分与黑名单一致**：每次 `check_by_tg_id` 会 `recompute_user_score`，并依据当前 `blacklist` 表重算分数与 tips，保证展示与数据一致。  
- **匿名查询日志**：`query_log` 只存 `query_hash(target_id+day)` 与 `result_risk_level`，不存真实 tg_id，符合方案中的“可选匿名日志”。

### 2.3 交互与体验

- **结果卡片**：风险等级、分数、tips（列表前加「•」）、黑名单提示、免责说明，并带「Report / Incorrect?」按钮。  
- **举报流程**：点按钮后 FSM 收集“一条消息”作为 reason，再调 `POST /v1/report`，回复“Report received”。  
- **查无此人/钱包**：返回方案中的友好话术（如“No record does not mean safe”），不直接报错。

### 2.4 安全与配置

- **Token 不入库**：仅从环境变量读取，`.env.example` 作模板，README 提醒勿提交 `.env` 与勿泄露 token。  
- **管理接口**：`/v1/admin/stats` 在设置 `API_INTERNAL_KEY` 时需 `Authorization: Bearer <key>`；未设置时 MVP 允许无 key（便于本地调试）。  
- **管理员判定**：Bot 侧 `/stats` 仅对 `ADMIN_TG_IDS` 中的 tg_id 返回统计，与 API 的 Bearer 校验分离。

### 2.5 未在 MVP 实现（留待下一阶段）

- 限流（Redis）：方案有设计，MVP 未接 Redis，可下一阶段加。  
- 黑名单写入：仅预留 `blacklist` 表与 `users.blacklist` 同步逻辑，暂无 `/blacklist add|remove` 或管理后台，需 V1.1 做。  
- 举报核实与自动入黑名单：report 仅 `status=pending`，未实现“多源核实 → verified → 更新 users.report_count / blacklist”。  
- 多语言：i18n 结构已留（`bot/i18n.py`），当前仅 EN，Tagalog 在 V1.1 扩展。  
- 订阅提醒：表结构可扩展，未实现推送逻辑。

---

## 三、相对原方案的“再次优化”小结

| 方面 | 原方案 | 实施中的优化 |
|------|--------|----------------|
| 建表 | 需迁移或手动建表 | 应用启动时自动建表，零配置跑通 |
| 数据库 | 需区分 SQLite/PostgreSQL 与驱动 | 根据 URL 自动选用 aiosqlite/asyncpg |
| 查无此人 | 仅“无记录” | 区分 username 解析失败与钱包无关联，并给出不同提示 |
| 评分一致性 | 查询时以表为准 + 缓存 | 每次查询前重算该用户的 score/level/tips，与 blacklist 同步 |
| 举报 | 仅提交接口 | Bot 上 FSM 引导输入 reason，并明确“已收到”反馈 |
| 密钥 | 配置外置 | Token/API key 仅 .env，README 安全提示 |

---

## 四、下一阶段建议（V1.1 及之后）

### 4.1 立即可做（V1.1）

1. **管理命令**  
   - 实现 `/blacklist add <tg_id> <reason>`、`/blacklist remove <tg_id>`（仅管理员），并写 `blacklist` 表与更新 `users.blacklist`。  
   - 实现举报审核：管理端将 report 标为 verified/rejected；verified 时更新对应用户 `report_count`，达阈值可自动入黑名单。

2. **限流与安全**  
   - 引入 Redis，对 `/check`、`/report` 按 tg_id 限流（如方案中的每分钟次数）。  
   - 管理接口在生产环境强制要求 `API_INTERNAL_KEY`（未设置时返回 403）。

3. **多语言**  
   - 在 `i18n.py` 增加 Tagalog，Bot 根据 `user.language_code` 或命令选择文案。

4. **纠错与反馈**  
   - 结果页增加“信息有误”入口，写入 `feedback` 表，并可选简单管理列表（仅管理员可见）。

### 4.2 后续阶段

- **Mini App**：复用 `/v1/check`、`/v1/mycredit`，前端做信用名片、雷达图、“用担保交易”跳转。  
- **订阅提醒**：黑名单/风险等级变更时扫 `subscriptions` 表并发送 TG 消息，带 24h 限频。  
- **AI/风控**：规则引擎接口已纯函数化，便于替换为模型调用或规则+模型并行 A/B。

### 4.3 运维与部署

- 生产环境将 `DATABASE_URL` 改为 PostgreSQL，并配置备份与监控。  
- 使用 systemd/supervisor 或 Docker 同时跑 API 与 Bot，并做健康检查（如 `/health`）。  
- 准备多 Bot Token 轮换与 Mini App 主入口，降低单点封号风险。

---

以上为本次 MVP 实施总结及在现有方案基础上的细化与优化；下一阶段可按上表逐项实施并再迭代方案文档。
