# 集团账本（Group Ledger）

客户 / 订单 / 授权的统一台账，SQLite 落地，供 `/console` 集团后台读取。本文档覆盖：定位与切主路线、表结构、双写点清单、回填与导入用法、PG 迁移路径、Node 20 兼容性。

## 1. 定位：影子账本（Shadow Ledger）

**现有 JSON 存储仍是业务主真相源**，账本只是它的镜像 + 扩展（客户归属、授权台账、审计）：

- `~/hualing-leads/orders-db.json`（`lib/order-store.ts`）→ 镜像到 `orders` 表
- `~/hualing-leads/leads-db.json`（`lib/lead-store.ts`）→ 镜像到 `leads` 表
- 授权数据来自外部系统（avatarhub / chengjie）的归一化导出，经 CLI 导入 `licenses` 表

写入策略：**双写 + 幂等回填**。

- 双写全部 **best-effort**：钩子不 `await`、失败静默，绝不让下单/留资主链路抛错。
- 回填幂等：按自然键 upsert，任何时候可全量重跑把账本追平 JSON。
- 一切现有 API 行为不变。

### 切主路线（后续阶段，当前不做）

1. **现阶段（影子）**：JSON 主，账本从。console 只读账本；发现缺漏跑一遍回填即可。
2. **对账期**：console 上线后加对账任务（JSON ↔ 账本 diff），观察双写完备性。
3. **切主**：order-store / lead-store 改为直接读写账本（接口签名不变），JSON 降级为导出格式或废弃。届时 `raw` 列已保存完整原始 JSON，无损。
4. **迁 PG**（可选，见 §6）。

## 2. 文件与模块

| 文件 | 作用 |
| --- | --- |
| `lib/ids.ts` | 全局 ID：`<prefix>_<ULID>`（26 字符 Crockford Base32，48bit 时间戳 + 80bit 随机）。前缀：`cust` 客户 / `ord` 订单 / `lic` 授权 / `evt` 事件 / `aud` 审计 / `usr` 控制台用户。校验 `isValidId()`。 |
| `lib/ledger.ts` | better-sqlite3 数据层：单例连接（globalThis 缓存，防 Next dev 热重载重复打开）、WAL、busy_timeout 5000、首次打开自动建表 + 迁移（`meta.schema_version`）。upsert / 查询 / 统计 / 客户归属 / 审计。**console 同事从这里 import。** |
| `lib/console-users.ts` | 控制台实名账号 + 会话（schema v2 的 users / sessions 表）：scrypt 密码散列、用户 CRUD、session token 签发/校验/撤销，全部变更写 audit。 |
| `lib/console-auth.ts` | /console 鉴权门面：session cookie + `x-console-key` 头双通道、`getConsoleUser` / `requireRole`（RBAC）。 |
| `lib/ledger-sync.ts` | store 数据结构 → 账本行的转换；`syncOrderEntry` / `syncLeadEntry`（best-effort 双写入口）；`backfillFromJson`（TS 侧全量回填）。 |
| `scripts/ledger-lib.mjs` | 上述数据层的**纯 JS 等价实现**（CLI 用，不经 TS 编译）。⚠️ DDL / upsert 语义 / inferProductId 与 `lib/ledger.ts` 一一对应，**修改必须两处同步**（两文件头部均有注明）。 |
| `scripts/ledger-backfill.mjs` | 回填 CLI。 |
| `scripts/ledger-import-licenses.mjs` | 授权导入 CLI。 |

数据库文件：`path.join(DATA_DIR, "group-ledger.db")`，env `LEDGER_DB` 可覆盖（`DATA_DIR` 即现有 `lib/data-dir.ts` 的解析结果，env `LEADS_DIR` 可覆盖）。

## 3. 表结构（schema v1 + v2）

设计约束：**PG 兼容的保守 SQL** —— TEXT 主键、ISO8601 时间字符串、`raw` 存 JSON 文本，不用 SQLite 特有类型；仅 `identities.id` 用自增整数。

- `meta(key PK, value)` — `schema_version` 等元数据。
- `customers(id PK, display_name, primary_contact, tg_user_id, source, notes, created_at, updated_at)` — 客户主档（`cust_` ID）。
- `identities(id AUTOINCREMENT PK, customer_id → customers, kind CHECK in contact/tg/email/phone/fingerprint, value, created_at, UNIQUE(kind,value))` — 客户身份标识，归属匹配的依据。值经 `normIdentityValue` 归一化（contact/email 小写去空白、phone 去分隔符）。
- `leads(source_key PK, customer_id, name, contact, interest, message, lang, source, utm, status, first_seen, last_seen, count, raw, synced_at)` — 留资镜像。自然键 = 现有留资 key（`tg:xxx` / `c:xxx`）。
- `orders(id PK, source_key UNIQUE NOT NULL, customer_id, product_id, sku_id, plan, edition, period, amount, pay_amount, currency, status, contact, fingerprint, lang, created_at, paid_at, activated_at, notify_chat, code, raw, synced_at)` — 订单镜像。`id` 为 `ord_` 新 ID，自然键 = 旧订单号（`AH-YYYYMMDD-XXXXXX`）。
- `licenses(id PK, source_system NOT NULL, source_key NOT NULL, customer_id, product_id, sku_id, plan, edition, seats, machine_fingerprint, issued_at, expires_at, status, raw, synced_at, UNIQUE(source_system, source_key))` — 授权台账（`lic_` ID），自然键 = 来源系统 + 来源键。
- `audit(id PK, ts, actor, action, entity, entity_id, detail)` — 审计流水（`aud_` ID）。目前记录：建客户、挂身份、手工归属（assign_customer）、自动归属（auto_link）。

`product_id` 枚举：`zhituo`（智拓）/ `zhiliao`（智聊）/ `tongyi`（通译）/ `tongchuan`（通传）/ `huansheng`（幻声）/ `huanying`（幻影）/ `huanyan`（幻颜）/ `website`（官网服务）。订单的 `product_id` 由纯函数 `inferProductId(plan, edition)` 宽松推断：已知 SKU（voice/faceswap/digital-human/video-dubbing/translate）与产品名关键词精确/正则命中才映射；**AvatarHub 会员档（trial/starter/standard/pro/flagship × trial/standard/pro/enterprise）属整机引擎、跨多产品，推断不了 → NULL**，宁缺毋错，后续在 console 人工归类。

订单的 `sku_id`（全域 SKU 关联键，`platform/licensing/sku_registry.json`）：新订单在 `createOrder` 出生即经 `lib/offer-map.ts::resolveOrderSku` 按 plan 静态映射填入 `OrderEntry.sku_id/product_id`（translate-\*→lingox-\*、autochat-\*→chatx-\*、realtime-basic→facex-live-deploy、realtime-creator→livex-creator-deploy；会员档映射不到 → 不写）；订单→账本行时自带值直传，历史订单缺失时回填/双写侧用同一张映射表推断（`scripts/ledger-lib.mjs` 有文本一致的纯 JS 副本，**修改两处同步**）。

### upsert 语义（三表一致）

- 按自然键幂等：不存在 INSERT（生成新 ID），存在 UPDATE。
- **`customer_id` 只用 `COALESCE(customer_id, @新值)` 填充**——账本里已有的客户归属永远不会被后续镜像覆盖。
- `product_id` / `sku_id` 来件为 NULL 时不清空已有值（console 可能已人工修正）。
- 其余镜像字段以来件为准全量覆盖（JSON 是主真相源）。
- `synced_at` 每次刷新。

### 客户归属

- `linkCustomer(kind, value)`：按 identities 精确匹配（值先归一化），命中返回 `customer_id`，否则 null。
- `ensureCustomerForOrder / ensureCustomerForLead`：**不自动建客户**，只匹配已有身份（订单按 fingerprint → contact，留资按 tg → contact），命中且未归属时写回并记 `auto_link` 审计。
- `createCustomer` + `attachIdentity` + `assignCustomer`：console 人工建档 / 挂身份 / 归属（写审计）。`attachIdentity` 遇到身份已属他人时不抢占，返回冲突信息。

### 账号与会话（schema v2）

/console 从共享口令升级为**实名账号 + RBAC**，账号与会话落在账本里（迁移 v1 → v2 由 `meta.schema_version` 驱动，两侧 DDL 同步维护）：

- `users(id PK /*usr_ULID*/, username UNIQUE NOT NULL, pw_salt NOT NULL, pw_hash NOT NULL, role CHECK in master/admin/viewer, display_name, enabled DEFAULT 1, created_at, last_login)` — 控制台账号。密码散列：`node:crypto` scrypt（N=16384, r=8, p=1, 64 字节；参数编码进 `pw_hash`，形如 `scrypt$16384$8$1$<hex>`，升参不破坏旧账号），比对走 `timingSafeEqual`。用户名统一小写（`normUsername`）。
- `sessions(token_hash PK, user_id → users, created_at, last_seen, expires_at, revoked DEFAULT 0, ip, ua)` — 登录会话。cookie `console_session` 存 **32 字节随机原始 token**；库里只存 `sha256(token)`（拖库拿不到可用凭证）。生命期 12h 绝对（`expires_at` 固定，与 cookie maxAge 一致），命中滑动刷新 `last_seen`。登出置 `revoked=1`；禁用账号 / 重置密码会撤销该用户全部会话，立即生效。
- 索引：`idx_sessions_user(user_id)`、`idx_users_username(username)`。

**RBAC**：`viewer < admin < master`。GET 类接口 viewer 可用；建客户 / 挂身份 / 归属（POST/PATCH）需 admin+；用户管理（`/api/console/users`，列表/建号/改角色/禁用/重置密码）仅 master。安全底线：**不能禁用或降级最后一个 enabled master**（API 先查再改）。

**引导（bootstrap）**：users 表为空时，`POST /api/console/login` 接受 `{bootstrap:true, key:<CONSOLE_KEY>, username, password}` 创建首个 master 并直接登录（audit `user.bootstrap`）；users 非空后 bootstrap 与页面口令登录一律关闭。`CONSOLE_KEY` 仅剩 `x-console-key` 头通道（脚本/巡检用，视为内置 master，不占用户表）。

**审计动作**：`user.bootstrap` / `user.create` / `user.login` / `user.set_role` / `user.set_enabled` / `user.reset_password` / `session.revoke`，actor 统一 `console:<username>`。

### 人设总线（schema v3）

同一个数字身份贯穿获客→承接→变现的集团注册表。一个 persona = 四个可选槽位：`face`（形象/脸模）、`voice`（声纹克隆）、`prompt`（语言人格/话术）、`knowledge`（术语库/知识库）。**注册表只存元数据与指纹，资产本体永不进集团库**（脸模/声纹/话术文件留在各引擎侧）。数据层：`lib/personas.ts`（TS）；导入 CLI `scripts/ledger-import-personas.mjs`（纯 JS，DDL 复用 `scripts/ledger-lib.mjs`，upsert 语义与 TS 侧一致，修改两处同步）。

**表结构**（迁移 v2 → v3，`meta.schema_version` 驱动，两侧 DDL 逐字同步）：

- `personas(id PK /*prs_ULID*/, customer_id NULL → customers, source_system NOT NULL, source_key NOT NULL, display_name, slot_face/slot_voice/slot_prompt/slot_knowledge INTEGER DEFAULT 0, slots_detail /*JSON：各槽位 fingerprint/ref/version（+ 可选 _meta.customer_name 归属线索）*/, tags /*JSON数组*/, status CHECK in active/archived/purge_pending/purged DEFAULT active, created_at, updated_at, synced_at, UNIQUE(source_system, source_key))` — 人设注册表，自然键 = 来源引擎 + 引擎内键。
- `persona_grants(id AUTOINCREMENT PK, persona_id → personas, product_id NOT NULL, scope, granted_by, granted_at, revoked_at NULL, UNIQUE(persona_id, product_id))` — 授权矩阵（人设可被哪些产品使用；撤销置 `revoked_at`，再授权清空之）。
- `persona_purges(id AUTOINCREMENT PK, persona_id NOT NULL, requested_by, requested_at, target_system NOT NULL, acked_at NULL, ack_detail NULL)` — 全域清除指令，一行 = 对一个引擎的一条删除指令。
- 索引：`personas(customer_id)`、`personas(status)`、`persona_grants(persona_id)`、`persona_purges(target_system, acked_at)`。

**upsert 语义**（`upsertPersonaRow`，自然键 `(source_system, source_key)`）：`customer_id` 只 COALESCE 填充（console 归属不被覆盖）；`status` 不随来件变化；**`purge_pending` / `purged` 的行整行跳过并计数 `skippedPurged`** —— 已清除/清除中的人设不会被同步或导入复活。

**产品 → 引擎映射**（purge 指令下发目标）：`huansheng/huanying/huanyan/tongchuan → avatarhub`；`zhiliao/tongyi → chengjie`；`zhituo → huoke`（`website` 不承载人设资产，不在授权矩阵内）。

**purge 协议时序**（合规删除 / 客户要求抹除数字分身）：

1. console（admin+）在人设详情页发起「全域清除」→ `requestPurge`：status → `purge_pending`，并为**每个已知承载系统**（persona 的 `source_system` + 全部 grants——含已撤销，撤销 ≠ 引擎已删——推导出的引擎）各插一条 `persona_purges` 指令行，audit `persona.purge_request`；
2. 各引擎轮询 `GET /api/sync/personas/purges?system=<自己>`（Bearer `EVENT_INGEST_KEY`，与 `/api/collect` 同一把机器密钥）拉未 ack 指令（`purge_id / persona_id / source_key / 槽位布尔`，**不含客户数据**）；
3. 引擎本地删除资产后 `POST { purge_id, detail? }` 回执（幂等，重复 ack 返回 `already=true`），audit `persona.purge_ack`；
4. 该 persona 全部指令 ack 后集团侧自动置 status → `purged`，audit `persona.purged`。授权在 `purge_pending`/`purged` 状态下冻结。

**清除队列监控（P5 运营收尾）**：`/console/personas/purges` 全域指令一览（只读，viewer+；读 API `GET /api/console/personas/purges?target=&state=(pending|acked)&q=&limit=&offset=` → `{stats, rows, total}`，与机器通道 `/api/sync/personas/purges` 分离，本视图无任何写操作）。统计卡：待回执 / 滞留告警（>24h 琥珀、>72h 玫红，提示检查该引擎清除执行器与 EVENT_INGEST_KEY）/ 已回执累计+近 7 天 / 平均回执时延；逐引擎积压表（待回执 / 已回执 / 最早滞留）+ 指令队列表（引擎、回执状态、人设模糊搜索三过滤；待回执且等得最久的置顶，已回执按回执时间倒序垫底）。数据层 `lib/personas.ts::listPurgeQueue / getPurgeQueueStats`（纯只读查询，无 DDL / upsert 变更，`ledger-lib.mjs` 侧无需同步）。指令行回执后保留作审计证据。入口：人设列表页头「清除队列」按钮（带待回执计数）与人设详情清除进度分区。

**导入用法**：

```bash
# 人设导出文件（幂等，可重复执行；--system 给 jsonl 提供缺省 source_system）
node scripts/ledger-import-personas.mjs /path/personas-export.json [--db /path/group-ledger.db]
node scripts/ledger-import-personas.mjs <outbox.jsonl> --system avatarhub
```

导出文件格式：`{"version":1,"source_system":"avatarhub","exported_at":"...","personas":[{source_key, display_name, customer_name?, slots:{face:{present,fingerprint?,ref?,version?},voice:{...},prompt:{...},knowledge:{...}}, tags?:[], created_at?, raw?:{}},...]}`。`slots.*.present` 映射到 `slot_*` 整型列，槽位细节进 `slots_detail`；`customer_name` 不建客户，只留在 `slots_detail._meta` 供 console 人工归属；`raw` 资产本体**不入库**。`.jsonl` 逐行同格式（BOM 容错，坏行计数）。统计输出含 `skippedPurged`。

**审计动作**：`persona.grant` / `persona.revoke` / `persona.assign_customer` / `persona.purge_request` / `persona.purge_ack` / `persona.purged`，console 侧 actor = `console:<username>`，机器通道 ack 的 actor = `sync:engine`。

### 商机跟进（schema v4）

跨售商机（`lib/opportunities.ts` 三类规则：`persona_cross_sell` / `product_gap_cross_sell` / `expiring_renewal`）**本体始终是只读推导、不落库**——每次从 personas / orders / licenses 全量重算。schema v4 只落**销售跟进动作**，把商机看板升级为可管理的销售队列（迁移 v3 → v4，`meta.schema_version` 驱动，两侧 DDL 逐字同步）：

- `opportunities_log(id PK /*opl_ULID*/, opp_key UNIQUE NOT NULL, kind CHECK in persona_cross_sell/product_gap_cross_sell/expiring_renewal, customer_id NOT NULL → customers, to_product, status CHECK in open/contacted/won/dismissed DEFAULT open, note, acted_by, acted_at, created_at)` — 跟进流水，一行 = 一条商机指纹的最新跟进态。
- 索引：`opportunities_log(customer_id)`、`opportunities_log(status)`。

**商机指纹 `opp_key`**（推导商机 ↔ 落库跟进的关联键，`oppKey(o)` 纯函数）：`kind|customerId|toProduct`；`expiring_renewal` 再拼 `|<evidence.licenseId>` —— 同客户多张到期授权可分别跟进。指纹可再生：规则引擎每次重算，同一商机总能对回 `opportunities_log` 里同一行。

**标记（`markOpportunity(input, actor)`，本模块唯一写路径）**：按 `opp_key` UPSERT —— 首标记 INSERT（新 `opl_` ID），再标记 UPDATE 状态/备注（不新增行，幂等）；每次写 audit `opportunity.mark`（entity=opportunity，entity_id=opp_key）。`note` 走隐私纪律：只存运营跟进备注，不存客户聊天原文；请求缺省 `note` 时保留已有备注。

**清单口径（`listOpportunities`）**：输出侧按指纹 LEFT JOIN 跟进表，每行附 `oppKey` 与 `log: {status, note, acted_by, acted_at} | null`；`won` / `dismissed` 默认**不出现在清单**（`includeClosed=true` / API `?include_closed=1` 才带出，且排序沉底）；`contacted` 保留在列但信号值降权 −20。`getOpportunityStats` 补 `byLogStatus`（open/contacted/won/dismissed，未标记 = open，四态之和 = total）。

**API**：`GET /api/console/opportunities?kind=&customerId=&limit=&include_closed=1`（viewer+）；`POST` 同路由 body `{opp_key, kind, customer_id, to_product?, status, note?}`（admin+，actor = `console:<username>`）。UI：/console 总览商机卡与客户 360 商机分区的行尾「跟进/赢单/忽略」操作 + 状态徽章（`app/console/opportunities-ui.tsx`，admin+ 才渲染操作件）。

## 4. 双写点清单

| 文件 | 函数 | 时机 |
| --- | --- | --- |
| `lib/order-store.ts` | `createOrder` | 写 JSON + 审计流水成功后 |
| `lib/order-store.ts` | `setOrderStatus` | 状态落盘后（含 paid_at/activated_at/code 变更） |
| `lib/order-store.ts` | `bindOrderNotify` | notify_chat 落盘后 |
| `lib/lead-store.ts` | `upsertLead` | upsert 落盘后 |
| `lib/lead-store.ts` | `setLeadStatus` | 状态落盘后 |

钩子统一形如：

```ts
void import("./ledger-sync").then((m) => m.syncOrderEntry(entry)).catch(() => {});
```

不 `await`、不改返回值；`syncOrderEntry`/`syncLeadEntry` 内部再套一层 try/catch 全吞。动态 import 同时避免了模块加载期就触碰原生依赖。

覆盖面说明：`runOrderSla` 只改通知去重标记（`notified.expiring` / `sla_alerted`），业务字段不变，未加钩子；这些标记进不了账本镜像列，`raw` 的轻微滞后由下一次回填抹平。

## 5. 回填与导入用法

```bash
# 全量回填（默认路径：DATA_DIR 下 orders-db.json / leads-db.json → group-ledger.db）
node scripts/ledger-backfill.mjs

# 显式指定路径
node scripts/ledger-backfill.mjs --orders /path/orders-db.json --leads /path/leads-db.json --db /path/group-ledger.db

# 授权导入（归一化导出文件）
node scripts/ledger-import-licenses.mjs /path/licenses-export.json [--db /path/group-ledger.db]

# 授权导入（实时 outbox，.jsonl：一行一条归一化 record）
node scripts/ledger-import-licenses.mjs <outbox.jsonl>
```

- 两个脚本都幂等：重复跑第二遍 `新增 0`，只刷新镜像字段与 `synced_at`。
- 回填不会覆盖 console 已做的客户归属（见 upsert 语义）。
- 授权导出文件格式：`{"version":1,"source_system":"avatarhub"|"chengjie","exported_at":"...","records":[{source_system,source_key,product_id,sku_id,plan,edition,seats,customer_name,customer_contact,machine_fingerprint,issued_at,expires_at,status,raw},...]}`。记录级 `source_system` 优先，顶层做缺省；缺字段置 null；缺自然键的记录跳过并计数。`customer_name/customer_contact` 不自动建客户，保留在 `raw` 里供 console 人工归属。
- 实时 outbox（`.jsonl`）：入参以 `.jsonl` 结尾（或内容首个非空字符不是 `{`）时按「一行一条归一化 record 对象」解析，`source_system` 取自每条记录（无顶层缺省）；空行忽略、坏行跳过并计入无效，幂等 upsert 与统计输出同上。
- TS 侧等价入口：`ledger-sync.ts::backfillFromJson({ordersDbPath, leadsDbPath, dbPath})`（console 可做「一键回填」按钮）。

建议运维节奏：部署双写后先跑一遍回填补历史；之后每天/每周 cron 跑一次回填兜底即可。

## 6. PG 迁移路径

DDL 已按 PG 兼容写法收敛，迁移时：

1. `identities.id INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY`（或 `GENERATED ALWAYS AS IDENTITY`）。
2. `REAL` → `NUMERIC(12,2)`（金额）；TEXT 时间列可保留或改 `timestamptz`（值本就是 ISO8601 UTC）。
3. `raw`/`detail` TEXT → `JSONB`（值本就是 JSON 文本，`ALTER ... USING raw::jsonb` 即可）。
4. upsert 改写为 `INSERT ... ON CONFLICT (natural_key) DO UPDATE`（语义与现有 COALESCE 规则一致）。
5. 数据搬迁：逐表 `SELECT *` → COPY；或直接在 PG 上重跑回填/导入脚本的等价逻辑（自然键幂等，天然支持增量追平）。

## 7. Node 20 兼容性（VPS 部署）

- 依赖 `better-sqlite3@^12`：支持 Node 20/22，`npm install` 时 `prebuild-install` 自动下载对应平台预编译二进制（Linux x64 glibc / Windows x64 都有），**1C1G VPS 无需本地编译**。
- 若预编译下载失败（网络受限），会回退源码编译，需要工具链：
  ```bash
  sudo apt install -y python3 make g++
  cd website && npm rebuild better-sqlite3
  ```
- `next.config.mjs` 已加 `experimental.serverComponentsExternalPackages: ["better-sqlite3"]`，Next 构建时不打包原生模块、运行期走外部 `require`（Next 14 的正确写法；Next 15 起改名 `serverExternalPackages`，届时同步调整）。
- WAL 模式下 SQLite 会伴生 `group-ledger.db-wal` / `-shm` 文件，属正常现象；备份时优先用 `sqlite3 group-ledger.db ".backup ..."` 或先 `PRAGMA wal_checkpoint(TRUNCATE)`。
- pm2 单进程 + WAL + busy_timeout 5000：官网进程与 CLI 脚本可安全并发读写。

## 8. console 对接速查

```ts
import {
  getLedgerDb, getStats,
  listOrders, listLeads, listLicenses, listCustomers,
  createCustomer, attachIdentity, assignCustomer, linkCustomer, writeAudit,
} from "@/lib/ledger";

const stats = getStats();                                  // 各表计数 + 订单状态分布 + 30 天内到期授权数
const page1 = listOrders({ status: "paid", limit: 50 });   // { rows, total, limit, offset }
const cust = createCustomer({ display_name: "张三", primary_contact: "tg @zhangsan" }, undefined, "admin:alice");
attachIdentity(cust.id, "tg", "123456789", undefined, "admin:alice");
assignCustomer("order", "AH-20260701-ABC123", cust.id, undefined, "admin:alice"); // 自动写 audit
```

所有查询函数带简单过滤 + 分页（limit ≤ 500）；写操作均为同步（better-sqlite3 特性），Route Handler 里注意 `export const runtime = "nodejs"`。
