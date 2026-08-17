# TrustCheck V2.0 升级方案（结合当前开发状态优化）

> **来源**：在 OpenAI 提供的 V2.0 升级方向基础上，结合本项目**当前程序与数据库状态**做可行性拆分与实施优化，形成可执行的下一步开发方案。  
> **原则**：不增加收费、不增加复杂操作、不改变核心动线；强化「关系」与「变化」，从一次性查询工具升级为持续关系追踪与信任积累系统。

---

## 一、升级目标（保持与原方案一致）

| 维度 | 当前定位 | 升级后定位 |
|------|----------|------------|
| **产品** | Telegram 风险查询机器人 | Telegram 内的**信任身份与风险追踪系统** |
| **形态** | 一次性查询工具 | **持续关系追踪** + **信任积累** + **群体风控** |

---

## 二、当前开发状态速览（与 V2.0 的差距）

| 能力 | 当前状态 | V2.0 目标 |
|------|----------|-----------|
| **报告结构** | 风险等级 + 信用分 + 提示标签 + 举报总数（待核实/已核实）；无「风险来源占比」、无举报分类、无 7d/30d 时间分布、无关联数量结构化展示 | 多维风险画像：风险来源结构、举报分类、时间分布、关联信息、系统建议 |
| **举报** | Report 表：reason 自由文本，status=pending/verified/rejected；无分类、无举报人权重 | 举报分类（诈骗/跑单/失联等）、举报人可信度、时间分布统计 |
| **信任积累** | 无 | 交易确认系统：双方确认 → 成功笔数/成功率/累计金额 |
| **被查通知** | 有 CheckLog(querier, target)，无对「被查人」的主动通知 | 可选「有人查了你」通知（可关闭） |
| **传播** | 出示我的报告（一次性链接）、转发报告文本 | 增加「信用卡片图」生成（/sharecard），便于群内传播 |
| **群能力** | 无 | 群内 /scan_group：扫描活跃成员风险，输出高风险列表（限频、仅管理员） |
| **订阅粒度** | alert_type：risk_change \| online_offline；事件：new_report、blacklist_added、online、offline | 增加：举报类型变化、近 7 天风险上升、钱包关联变化等订阅类型 |
| **举报可信度** | 无 | 举报人信誉分、核实成功率；举报影响 = 权重 × 举报人可信度 |
| **异常与安全** | 无 | 同 IP/设备批量举报降权、互刷确认降权、自刷检测 |
| **管理后台** | 统计、申诉、加黑；文档就绪，前端可对接 | 举报人信誉、交易确认审核、异常监控、风险趋势图 |
| **API** | 仅 v1 | 新增 v2 接口，保持 v1 兼容 |
| **架构** | 单进程 API + Bot，SQLite/PostgreSQL | 可选：Worker/定时任务拆出推送与重算，Redis 限流/缓存 |

---

## 三、分项优化与实施要点（按当前代码可落地方式）

### 3.1 报告结构升级（从「分数」到「风险画像」）

**当前**：`scoring.py` 规则引擎输出 score/level/tips；`check_by_tg_id` 返回 report_count、report_pending、report_verified；报告展示为「举报：共 N 次（待核实 x，已核实 y）」。

**优化方案**（与现有表兼容优先）：

- **风险来源结构**：不新增表，在**接口层计算**。现有因子：blacklist、report_count、account_age、high_risk_groups、wallet 关联。在 v2 响应中增加 `risk_factor_breakdown`（如举报 40%、黑名单 30%、钱包异常 20%、新账号 10%），权重与现有 `scoring.DEFAULT_WEIGHTS` 对齐，便于前端展示饼图或条形。
- **举报分类**：  
  - **方案 A（推荐）**：在 `reports` 表增加 `category` 字段（枚举：scam / run_order / lost_contact / other 或 诈骗/跑单/失联/其他）；举报提交时 Bot 让用户选择分类（或选填）。  
  - 聚合：按 target_tg_id 统计各 category 的 verified 数量，在报告接口中返回 `report_by_category`（如 诈骗:2, 跑单:1, 失联:3）。
- **时间分布**：不新增表；按 `reports.created_at` 对 target 做「近 7 天」「近 30 天」count，在 v2 报告接口中返回 `last_7d_reports`、`last_30d_reports`。
- **关联信息**：已有 `WalletEntityGraph`、多 TG 关联在报告中有展示；在 v2 中统一为 `linked_wallet_count`、`linked_tg_count` 字段。
- **系统建议**：现有 verdict + next_step 已存在；v2 可扩展为多条「系统建议」列表（如小额测试、使用担保），由规则或配置驱动。

**数据库**：  
- 必选：`reports.category`（VARCHAR 或 ENUM）；  
- 可选：不新增 `report_risk_breakdown` 表，breakdown 只读计算；若后续要做「历史快照」再考虑快照表。

---

### 3.2 交易确认系统（信任积累）

**当前**：无交易、无确认。

**优化方案**：

- **流程**：A 查 B → 线下完成交易 → A 在 Bot 发起「交易确认」：对方 tg_id、金额、可选备注 → B 收到请求（同意/拒绝）→ 双方确认后写入成功记录；若 B 拒绝或超时，不记入成功。
- **数据表**：  
  - `transactions`：id、initiator_tg_id（A）、counterparty_tg_id（B）、amount、currency（如 USDT）、memo、status（pending/confirmed/rejected）、created_at、confirmed_at。  
  - 或单表 `transaction_confirmations`：同上，视是否需「一次交易多次确认」而定；MVP 可单表一笔一记录。
- **用户维度汇总**：在查询/我的信用接口中，按 User 或按 tg_id 统计：`confirmed_transactions_count`、`total_confirmed_amount`、`success_rate`（成功笔数/总确认请求笔数）。可缓存在 User 表（如 user.confirmed_count、user.total_amount、user.success_rate）或每次聚合（数据量大时用汇总表）。
- **报告展示**：在报告块中新增「成功交易笔数」「成功率」「累计金额（可选脱敏）」。

**API**：  
- `POST /v2/transaction/confirm`：发起确认请求（initiator_tg_id, counterparty_tg_id, amount, memo）；  
- `POST /v2/transaction/respond`：对方响应（transaction_id, accept/reject）；  
- 现有 `GET /v1/check`、`GET /v1/mycredit` 可扩展返回交易统计，或新增 `GET /v2/report/breakdown` 含交易与风险画像。

**Bot**：新流程「交易确认」入口；对方收到 Inline 按钮「同意/拒绝」；成功后可简短提示双方。

---

### 3.3 被查通知机制

**当前**：`CheckLog` 已记录 querier_tg_id、target_tg_id、created_at；无对 target 的主动通知。

**优化方案**：

- **逻辑**：在写入 CheckLog 之后，若 target 非 querier 且 target 的「被查通知」开关为开，则向 target 发送一条 Bot 消息：「有人查询了你的信用报告。点击查看详情。」（详情可跳转「我的信用」或带 report_no）。
- **用户设置**：  
  - 新增 `user_settings` 表或 User 扩展字段：`notify_on_checked`（boolean，默认 true）。  
  - Bot 内「我的信用」或设置入口：可关闭「被查时通知我」。
- **隐私**：不暴露「谁查了」，仅提示「有人查了」；与现有「谁查过我」统计（次数、最近时间）一致，不泄露具体 querier。

---

### 3.4 信用卡片图（/sharecard）

**当前**：出示我的报告为「一次性链接」；无图片卡片。

**优化方案**：

- **指令**：Bot 支持 `/sharecard tg_id` 或从结果页按钮「生成分享卡片」；参数可为本人或他人（他人仅展示公开部分：风险等级、信用分、举报数、时间戳）。
- **实现**：  
  - **方案 A**：后端生成 PNG（如用 Pillow 或 reportlab）；接口 `GET /v2/report/sharecard?tg_id=xxx` 返回图片流或 URL。  
  - **方案 B**：返回 HTML 片段，由前端或 Bot 使用「HTML 转图」服务生成图（依赖外部或自建）。  
- **内容**：风险等级、信用分、举报数、生成时间；可加 logo/品牌；不包含敏感详情（如具体举报原因、钱包地址）。

---

### 3.5 群检测模式（/scan_group）

**当前**：Bot 以私聊为主；无群内扫描。

**优化方案**：

- **前提**：Bot 需能接收群消息或管理员命令；Telegram 支持在群内 @bot 或 /scan_group。
- **逻辑**：仅群组内、仅管理员可触发；Bot 获取「最近活跃成员」列表（Telegram 群成员列表 + 可选最近发言，受 API 限制）；对每个成员调用现有 check 逻辑（或批量），汇总风险等级，输出「高风险列表」（如 risk high/extreme 的 tg_id 或用户名列表）。
- **限频**：每群每日 1 次（或每 N 小时 1 次）；用 Redis 或 DB 记录 `group_id + date` 已使用次数。
- **API**：`POST /v2/group/scan`（body：group_id、admin_tg_id、limit）；需鉴权（Admin Key 或 Bot 代发）。

**注意**：Telegram 群成员列表与「最近发言」的获取有权限与限频；实现时需按 Telegram API 能力做裁剪（例如仅扫描最近 N 条消息中的用户）。

---

### 3.6 订阅粒度增强

**当前**：`risk_alert_subscriptions.alert_type` = risk_change | online_offline；`risk_alert_pending.event_type` = new_report | blacklist_added | online | offline。

**优化方案**：

- **新增订阅类型（alert_type 扩展）**：在现有枚举上增加，如：  
  - `report_spike`：近 7 天举报数突增时提醒；  
  - `wallet_change`：关联钱包/多 TG 变化时提醒；  
  - 保留 `risk_change`（新举报/拉黑）、`online_offline`。
- **实现**：  
  - 报告/用户数据更新时（如新举报、钱包关联变更），在服务层判断是否满足「近 7 天突增」「钱包变化」，满足则对订阅了 `report_spike` / `wallet_change` 的订阅者入队 `risk_alert_pending`（event_type 可细化为 report_spike、wallet_change）。  
  - 订阅表与待推送表已支持 event_type 扩展，只需在入队逻辑与 Bot 推送文案上区分。
- **不改变核心动线**：仍为「在结果页或我的订阅里选类型 → 订阅 → 收到推送」；仅多几种可选类型。

---

### 3.7 举报可信度与权重

**当前**：举报不分举报人权重；report_count 为简单计数（verified）。

**优化方案**：

- **举报人维度**：  
  - 新增「举报人统计」：按 reporter_tg_id 统计其发起的举报中 verified / rejected 数量，计算 `reporter_success_rate`（verified / (verified + rejected)）；可选 `reporter_score`（如 0–100，由成功率与次数综合）。  
  - 存储：可新建 `reporter_stats` 表（reporter_tg_id、verified_count、rejected_count、updated_at）或每次从 Report 表聚合（数据量大时用汇总表）。
- **举报对分数的影响**：  
  - 当前 scoring 仅用「被举报次数」；可改为「加权举报影响」：每条 verified 举报贡献 = 基础权重 × 举报人可信度（如 reporter_success_rate）。  
  - 在 `check_by_tg_id` 或 scoring 输入中，传入「加权后的有效举报数」或「按举报人权重汇总的 risk_contribution」，保持 score 仍为 0–100。
- **管理后台**：举报人信誉列表（reporter_tg_id、成功率、笔数）；可选「标记恶意举报人」降权或排除。

---

### 3.8 异常检测与安全

**当前**：无同 IP/设备/互刷检测。

**优化方案**：

- **维度**：  
  - 同 IP/设备批量举报：需能获取 IP 或设备标识（Bot 上报或 API 请求头/代理）；同一来源短时间多笔举报 → 对这批举报降权或标记待审。  
  - 互刷交易确认：同一对 A–B 在短时间大量「确认」→ 对 success_rate 计算时降权或只计前 N 笔。  
  - 自刷：自己查自己、自己给自己确认 → 直接不计入或拦截。
- **实现**：先做「自刷」与「同对频繁确认」的规则（基于现有 transactions 与 CheckLog）；IP/设备需有数据来源再接入，可放在 Phase 4。

---

### 3.9 管理后台扩展

**当前**：管理 API 有统计、申诉、黑名单；对接文档已有；前端可独立实现。

**优化方案**：

- 在现有管理 API 与对接文档基础上**扩展**：  
  - 举报人信誉列表：`GET /v2/admin/reporters`（或 v1 扩展）；  
  - 交易确认审核（若有审核需求）：列表与通过/驳回；  
  - 异常行为监控：如「短时大量举报」「同一对大量确认」的列表或告警；  
  - 风险趋势图：按日统计「当日 high/extreme 查询数」「新增举报数」等，接口返回按天聚合数据，前端绘图。
- 不改变现有鉴权方式（Bearer 管理密钥）；若后续做多管理员角色再扩展。

---

### 3.10 API 与版本策略

- **保持 v1 完全兼容**：现有所有 v1 接口不变；v1 响应可保持现状，仅在 v2 中增加字段或新端点。
- **新增 v2 接口（建议）**：  
  - `GET /v2/report/breakdown`：在 v1 check 数据基础上增加 risk_factor_breakdown、report_by_category、last_7d_reports、last_30d_reports、linked_wallet_count、linked_tg_count、交易统计等。  
  - `GET /v2/report/sharecard`：返回分享卡片图（PNG 或 URL）。  
  - `POST /v2/transaction/confirm`、`POST /v2/transaction/respond`：交易确认。  
  - `POST /v2/group/scan`：群扫描（需鉴权）。  
- Bot 与前端可逐步从 v1 迁到 v2（如先报告页用 v2 breakdown，其余仍 v1）。

---

### 3.11 技术架构（可选演进）

**当前**：单进程 API（FastAPI）+ 单进程 Bot；SQLite 或 PostgreSQL；无 Redis、无独立 Worker。

**优化方案（分阶段）**：

- **Phase 1–3**：可不拆进程；推送仍由 Bot 轮询 `risk_alert_pending`；风险计算与 7d/30d 统计在 API 请求内或短时缓存完成；DB 用现有 PostgreSQL/SQLite。
- **Phase 4 或后续**：  
  - **Redis**：用于「每群每日 1 次」、限流、或报告 breakdown 缓存，减少 DB 压力。  
  - **Worker**：将「订阅入队」「风险重算」「群扫描」等耗时操作放入异步任务（如 asyncio 后台任务、或 Celery/RQ），API/Bot 只发请求不阻塞；队列可用 DB 表或 Redis。  
- 文档中「FastAPI + PostgreSQL + Redis + Celery」可作为**目标架构参考**，实施时按需先上 Redis 再上 Worker，避免一步到位导致复杂度激增。

---

## 四、数据库变更汇总（最小必要）

| 类型 | 内容 |
|------|------|
| **reports** | 增加 `category`（VARCHAR/ENUM：scam/run_order/lost_contact/other）。 |
| **users** | 可选：`notify_on_checked`（BOOLEAN，默认 true）；或新建 `user_settings` 表（user_id/tg_id, key, value）。 |
| **transactions** | 新表：initiator_tg_id, counterparty_tg_id, amount, currency, memo, status, created_at, confirmed_at；可选在 User 上冗余 confirmed_count、total_amount、success_rate 或单独汇总表。 |
| **reporter_stats（可选）** | 新表：reporter_tg_id, verified_count, rejected_count, updated_at；用于举报人可信度。 |
| **其他** | 不新增 report_risk_breakdown、report_time_stats 表；breakdown 与 7d/30d 由查询时计算。 |

---

## 五、阶段性实施计划（按当前状态优化）

| 阶段 | 周期 | 内容 |
|------|------|------|
| **第 1 阶段** | 约 2 周 | ① 报告结构升级：v2 breakdown 接口（风险来源占比、7d/30d 举报、关联数量）；② 举报分类：reports.category + 提交时选分类 + 报告内按类展示；③ 举报人统计与权重：reporter_stats 或聚合、scoring 接入「加权举报」可选。 |
| **第 2 阶段** | 约 2 周 | ① 交易确认系统：transactions 表、confirm/respond API、Bot 流程；② 报告与我的信用中展示「成功笔数/成功率/累计金额」；③ 自刷与同对频繁确认拦截。 |
| **第 3 阶段** | 约 2 周 | ① 被查通知：user_settings.notify_on_checked、CheckLog 后发 Bot 消息；② 信用卡片：/sharecard 或按钮 + GET /v2/report/sharecard；③ 群扫描：/scan_group + POST /v2/group/scan，限频与权限。 |
| **第 4 阶段** | 优化期 | ① 订阅粒度：report_spike、wallet_change 类型与入队逻辑；② 管理后台：举报人信誉、交易审核、异常监控、风险趋势图；③ 异常检测：同 IP/设备举报降权（有数据源再做）；④ 架构可选：Redis 限流/缓存、Worker 异步任务。 |

---

## 六、升级后的产品形态（与原文一致）

升级后，TrustCheck 将变为：

- **Telegram 信任身份 + 风险追踪 + 社交博弈系统**
- 不再只是「查一下」，而是：**信用可增长、风险会变化、身份可积累、群体可扫描**。

---

## 七、最重要的实施原则（与原文一致）

- **不增加收费**  
- **不增加复杂操作**（新功能尽量一键或选填）  
- **不改变核心动线**（查 → 看报告 → 举报/订阅/出示 主路径不变）  
- **强化「关系」和「变化」**（被查通知、订阅粒度、交易确认、风险画像与时间分布）

---

## 八、与现有文档的衔接

- 本方案与《产品功能与应用场景说明-供市场与商业化分析》中的现有功能清单兼容；V2.0 为在其上的**扩展**。  
- 管理后台、订阅、上下线监控、多监控端等已有方案文档，本方案中涉及管理后台与订阅的部分与之**叠加**，不替代。  
- 实施时优先保证 v1 接口与现有 Bot 行为不变，v2 与新增能力作为**增量**上线。
