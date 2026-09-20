# platform/licensing · 授权/收款契约（卡密 / 心跳 / 配额 / USDT 收款）

> 定位：授权与收款的**实现**长在 TG-AI智控王 的 `license_server.py`
> （`tgkz2026/backend/`，aiohttp 独立服务，默认 `:8080`，SQLite 本机落库）——它已经是
> 可独立启动的卡密/收款服务，把实现搬进 platform 既不可行也不必要。按 avatarhub 模式：
> **引擎持实现，platform 只放『契约 + stdlib 瘦客户端』消费其 HTTP 面。**
> 因此本层是 **契约 + 客户端**，不是搬代码；智控王侧代码零改动，守住
> "platform 不反向依赖 engines/products"。

## 1. 能力与归属（端点清单）

实现全部在智控王 `license_server.py`（行号为盘点时参考位置）：

| 能力 | 端点 | 认证 | 瘦客户端方法 |
|---|---|---|---|
| 健康探针 | `GET /api/health` | 无 | `health()` / `available()` |
| 卡密验证（只验不激活） | `POST /api/license/validate` (L423) | 无 | `validate()` |
| 卡密激活（绑机发 JWT） | `POST /api/license/activate` (L454) | 无 | `activate()` |
| 心跳续签（续 token/等级/配额/过期位） | `POST /api/license/heartbeat` (L512) | token 或 machine_id | `heartbeat()` |
| 卡密状态/时长 | `GET /api/license/status?key=` (L583) | 无 | `license_status()` |
| 等级配额 + 今日 used/remaining | `GET /api/user/quota` (L1057) | Bearer | `quota()` |
| 用量回拉（今日 used/remaining/max） | `GET /api/usage/sync` (L733) | Bearer | `sync_usage()` |
| 用量上报（可拒超额） | `POST /api/usage/log` (L611) | X-Signature(HMAC) + token + nonce | **不封装**（见下注） |
| 价目（level×duration） | `GET /api/products` (L1245) | 无 | `products()` |
| 下单（USDT 地址/金额） | `POST /api/payment/create` (L1274) | 无 | `create_payment()` |
| 支付回调（升会员 + 写 license） | `POST /api/payment/callback` (L1392) | `secret`（服务端设置） | **不封装**（见下注） |
| 订单状态轮询 | `GET /api/order/status?order_id=` (L1552) | 无 | `order_status()` |

> 两个**有意不封装**的端点：
> - `/api/usage/log` 的签名是 `sha256(f"{timestamp}:{nonce}:{machine_id}:{JWT_SECRET}")`
>   （license_server.py L883）——计算签名**需要服务端 JWT_SECRET**，属产品端内嵌调用；
>   瘦客户端封装它等于把服务端密钥散布到所有消费侧，先按 §6④ 拆独立密钥再议。
> - `/api/payment/callback` 是支付网关/管理员 → 服务端方向的回调，凭
>   `payment_callback_secret` 鉴权，消费侧永远不该持有该 secret。

## 2. 契约（stdlib 瘦客户端签名）

`platform/licensing/license_client.py`（纯 stdlib，零第三方依赖，零反向依赖）：

```python
LicenseClient(base_url=None, timeout=8.0)
  # base_url 缺省读环境变量 LICENSE_SERVER_URL（缺省 http://127.0.0.1:8080）
  .health()                     -> dict  # /api/health → {status,server,version}
  .available()                  -> bool  # license_server HTTP 面可达
  .validate(license_key)        -> dict  # 只验不激活 → data{level,durationDays,status}
  .activate(license_key, machine_id, **opt)
                                -> dict  # opt: device_id/email/invite_code
                                         # → data{token,userId,level,expiresAt,quotas,features}
  .heartbeat(token=None, machine_id=None, usage=None)
                                -> dict  # 续签 → data{token,level,expiresAt,isExpired,quotas}
  .license_status(key)          -> dict  # → data{status,level,durationDays,usedAt,expiresAt}
  .quota(token)                 -> dict  # Bearer → data{level,quotas,usage,remaining}
  .sync_usage(token)            -> dict  # Bearer → data{date,used,remaining,max,level}
  .products()                   -> dict  # → data[{id:"{level}_{duration}",price,quotas,...}]
  .create_payment(product_id, payment_method,
                  machine_id=None, user_id=None, coupon_code=None)
                                -> dict  # → data{orderId,amount,usdt{address,amount,memo},...}
  .order_status(order_id)       -> dict  # → data{orderId,status,amount,licenseKey}
  .sku_info(sku_id)             -> dict  # 本地：同目录 sku_registry 取 SKU 行（见 §5）
```

- 请求/响应字段的完整 schema 见同目录 `license_schema.json`。
- **不做能力实现**：客户端不含发卡/验卡/记账/收款逻辑——那些只在智控王服务端发生。

## 3. 依赖方向

`website/products → platform/licensing(契约+client) → (HTTP) → 智控王 license_server`。
platform 不 import 智控王代码，仅通过 HTTP 契约交互；卡密库、用户库、JWT 密钥、
收款地址等机密全部留在智控王服务端本机。本目录**不建 `__init__.py`**（顶层 `platform`
与标准库同名，包式导入会遮蔽标准库；`license_client.py` 内部按文件路径惰性加载
`sku_registry.py`，已绕开该陷阱）。

## 4. 可降级说明（服务端离线时消费侧如何优雅退化）

所有方法**绝不抛异常**：任何失败（HTTP 4xx/5xx、超时、连接失败、JSON 解析失败）都收敛为
`{"available": False, "error": ..., "detail": ...}`。注意区分两层语义：

- `available`（瘦客户端注入）= HTTP 面是否可达（传输层）；
- `success`（服务端返回）= 业务是否成立（卡密有效/未超配额/订单存在...）。
  `available=True` 且 `success=False` 是正常业务拒绝，不是降级。

| 不可用能力 | 消费侧退化行为 |
|---|---|
| `validate` / `activate` / `heartbeat` | 用本地缓存的上次 token/expiresAt 进入**有限宽限期**；宽限期外锁付费能力。授权门控**不得因服务端离线而静默放行** |
| `quota` / `sync_usage` | 按上次已知配额**保守限流**（宁可少发不可超发） |
| `products` / `create_payment` | 展示人工收款指引（联系客服/固定 USDT 地址），不阻塞产品主流程 |
| `order_status` | 提示"订单确认延迟"，退避轮询，不判失败 |

## 5. 与 sku_registry 的关系（授权按 sku_id 对齐）

- 同目录 `sku_registry.json` 是 boundless 全域 SKU 单一真相
  （sku_id/product/category/visibility/price），`sku_registry.py` 是唯一读取器。
- 智控王服务端另有自己的价目：`MEMBERSHIP_LEVELS`（bronze/silver/gold/diamond/star/king
  × week/month/quarter/year/lifetime），`product_id = "{level}_{duration}"`（如 `gold_month`），
  `/api/products` 返回的即这套。
- **对齐规则**：boundless 侧一律以 `sku_id` 为准——下单前先 `sku_info(sku_id)` 核对
  价格/可见性/是否 TBD，再映射到服务端 `product_id` 调 `create_payment()`。

> **2026-07-20 第七阶段·纠正（原"智控王品牌位是 zhiliao"的假设不成立）**：处理"变现能力
> 上移"相关任务时核实 `products/zhiliao/product.yaml` 与 `engines/chengjie/src/inbox/`，
> 发现与本节原文（"智控王在 boundless 的品牌位是 zhiliao（`chatx-*`）；`chatx-entry/
> team/flagship` ↔ silver/gold/diamond 档的具体映射在接线时敲定并回写本节"）不符的
> 关键事实：
>
> - `zhiliao`/`chatx` 的 `product.yaml` 明确写着 `engine: chengjie`、
>   `engine_services: [main.py]`，定位是"多平台统一收件箱 + AI 人设承接 + 翻译 +
>   转人工"——**实现落在 chengjie 自己的 `src/inbox/`**（真实存在：`unified_inbox.
>   html`、`unified_inbox_login_routes.py`、`telegram_protocol_login.py`——chengjie
>   有自己独立的 Telegram 登录能力），**不是智控王的品牌马甲**。
> - 价格数字曾造成误导：`chatx-entry/team/flagship`（$58/$198/$598 月费）与智控王
>   `diamond/star/king` 月费（$59.9/$199/$599）幾乎逐一对上，第一眼像是"显然的映射
>   关系"。但把配额也摆出来对比就会发现这是**纯数字巧合**：`chatx-flagship` 卖点写
>   明"50 账号"，与智控王 `diamond` 档的 `tg_accounts=50` 配额刚好一致——但 `diamond`
>   月费只要 $59.9，是 `chatx-flagship` 售价 $598 的十分之一。反过来价格对得上的
>   `king` 档（$599）配额却是"无限账号"，跟"50 账号"文案对不上。也就是说**价格数列
>   与配额数列不是同一条映射关系**，说明两套 SKU 体系的定价逻辑本就独立，不能用价格
>   接近去反推产品对应关系。
> - **修正后的理解**：`zhiliao`/`chatx`（chengjie 原生的多平台统一收件箱产品）与
>   `TG-AI智控王`（tgkz2026，Telegram 专用群控执行引擎）是 boundless 产业链里**两个
>   独立的产品**，恰好都触达 Telegram，但不是"一个产品的两个品牌马甲"。二者现有的
>   真实连接点是 `platform/replybus`（chengjie 可选择性地为智控王提供"决策大脑"），
>   与 SKU/计费体系无关。
> **2026-07-20 第八阶段·业务已拍板并落地**：智控王进 boundless 官方 SKU 目录，
> 开独立 `product.yaml`（不复用 `chatx-*`）。已交付 `products/zhikong/product.yaml`
> （`id=zhikong`，`brand_key=matrixx`——取自代码内部一贯的"TG-Matrix"/`tgmatrix.db`
> 命名，非"智控王"直译；如需改用其它品牌名，改这一个字段即可，不影响其它任何
> 代码），`category=growth`，`engine=tgkz2026`（**特别说明**：与本目录其它产品不同，
> 智控王实现不在 boundless 仓库的 `engines/` 目录下，而是独立仓库/部署——`engine`
> 字段的值只是标签，`build_sku_registry.py` 不会据此尝试 import/解析任何路径，
> 已用真实构建验证不受影响）。
>
> 收录 3 档月费代表 SKU（`matrixx-silver/gold/diamond`，$4.99/$19.9/$59.9，对应
> 智控王自己白银/黄金/钻石三级会员）——**不是**智控王全部六级会员 × 五种时长的
>完整矩阵，是刻意的"目录发现用精简摘要"，与本目录其它产品的既有做法一致（如
> `zhiliao` 也只挂 3 档，不是它自己后台的完整价目）；完整计费矩阵仍以智控王自身
> `/api/products` 为履约口径，`sku_registry` 只作跨产品的发现/展示口径，两者定位
> 不同，不构成"两份互相矛盾的权威价目表"。
>
> `visibility=gated`、`risk=high` 是本轮基于"MTProto 群控自动化，与 zhituo(智拓)
> 风险类型相近"给出的**建议起点分类**，不是不可更改的最终结论——如果业务侧对
> 智控王的合规定位有更准确的判断，直接改这两个字段即可，不涉及任何代码改动。
>
> **验证**：`python tools/build_sku_registry.py` 重新生成后 `products=8, skus=25`
> （较此前 +1 产品 +3 SKU）；用真实 `sku_registry.py`/`license_client.py` 的
> `get_sku()`/`sku_info()` 方法（不是直接读 JSON）查询三个新 SKU，字段完整、
> `available=True`；`skus_for_product('zhikong')`/`gated_skus()` 均能正确检索到。
>
> **2026-07-20 第九阶段·官网可视化上架 + 品牌重塑（用户拍板：真正融入官网，重新
> 命名，换新 logo）**：`sku_registry` 层的接入只是"跨系统能查到"，本轮进一步做到
> "官网用户能看到、能点进去"——中文名改为**智控**、英文名改为**MatrixX**（`product.
> yaml` 的 `name` 字段同步更新，不再是"智控王"/"TG Matrix"这两个引擎侧历史名称；
> 引擎侧内部代号与数据库文件名等不受影响，改名只发生在 boundless 官网品牌层）。
> 已注册进 `website/lib/brand.ts` 的 `PRODUCT_ORDER`（七款→八款，`FAMILY_PITCH`
> 文案同步为"破七道边界"，新边界"规模"不与既有六道重叠）、生成全套 brand-assets
> 图标资产、新增 `/matrix` + `/en/matrix` 独立落地页。合规处理：因 `tgkz2026`
> 引擎侧既有市场材料把加密货币/博彩/成人产业列为目标客户（详见融合方案文档
> §9.24），落地页按 `lib/isolation.ts` 登记为 `gated`（noindex + robots disallow），
> 处理水平与本仓唯一先例 `/face` 对齐——不隐藏导航/矩阵入口，只限制搜索引擎收录。
> 完整过程、四视角分析、取舍记录见融合方案文档 §9.24。
- 远期：服务端价目改为从 `sku_registry` 生成（单一定价源）。此前两套价目并存，
  **对客报价以 `sku_registry` 为事实**，`/api/products` 仅作服务端内部履约口径。

## 6. 迁移前必须先修复的服务端问题（高危，跨产品接入前置条件）

以下问题在智控王侧已存在（只读盘点核实，行号以其当前代码为准）。**修复前，boundless
的 website/products 不得把真实收款流量切到该服务**；本层瘦客户端会把由此产生的 500
降级为 `available=False`，但那只是止损，不是修复。

### ① orders / coupons 表 CREATE 与 INSERT/SELECT 字段不一致（schema 漂移）

- 下单写库 `INSERT INTO orders (..., product_id, duration_type, duration_days, coupon_id, ...)`
  （license_server.py L1346-1353）与建表 `CREATE TABLE orders`（database.py L590-632）
  字段集对不上：建表是 `product_type/product_duration/coupon_code`，且**没有**
  `duration_days/coupon_id` 列；支付回调还 `UPDATE orders SET tx_hash=?, paid_amount=?`
  （L1426-1430），两列同样不在建表里；`orders.user_id` 声明 `NOT NULL` 但匿名下单传 `None`。
- 优惠券查询 `SELECT * FROM coupons WHERE code=? AND status='active' AND expires_at ...
  AND max_uses ...`（L1302-1306，另读 `min_amount`）与建表 `CREATE TABLE coupons`
  （database.py L931-958：`coupon_code/is_active/expire_at/total_count/min_order_amount`）
  **列名整套对不上**——带券下单必然 `sqlite3.OperationalError` → 500。
- 同库还有**多份异构建表**：`scripts/merge_db_init.py` L267、`core/tenant_schema.py`
  L833/L873、`core/coupon_service.py` L159 各自 CREATE orders/coupons——运行时实际列集
  取决于哪条初始化路径先跑。附带一例同类漂移：`/api/usage/sync` 读
  `user_quotas.ai_calls`（L758-761），建表列名是 `ai_calls_used`（database.py L679）。
- **修复要求**：一份 migration 收敛 orders/coupons/user_quotas 的唯一 schema，删除重复
  建表路径，补"带券下单→回调→发卡"全链路回归测试。

> **2026-07-19 第四阶段·独立复核（不依赖上面的既有盘点，重新逐行读码验证）**：亲自重读
> `database.py` L590-634（`orders` CREATE）+ L931-958（`coupons` CREATE）+
> `license_server.py` L1274-1390（`handle_create_payment` 完整实现）后，**确认上述①的
> 核心结论成立**，并补充两点运行时细节：
> 1. INSERT 语句本身占位符数量与参数元组是**自洽**的（12 个 `?` 对 12 个参数 + 1 个字面量
>    `'pending'`），不是"少传参数"这类更粗浅的 bug——纯粹是**列名对不上表**，
>    `sqlite3.OperationalError: table orders has no column named product_id` 这类错误
>    会在 `cursor.execute()` 那一行立即抛出；
> 2. 整段逻辑（含优惠券 SELECT 与 orders INSERT）都包在 `handle_create_payment` 唯一的
>    `try:`/`except Exception as e: return web.json_response({'success': False, 'message':
>    str(e)}, status=500)`（L1389-1390）里——即**不会打崩整个 aiohttp 服务进程**，
>    但意味着"带优惠券下单"与"不带优惠券下单"两条路径**都会**在当前 `database.py`
>    schema 下于 INSERT/SELECT 处失败，对客户端表现为下单 500。
>
> **2026-07-20·二次核实（不再只读代码，直接只读查询真实数据库文件，结论从"推测"升级为
> "实证"）**：本机 `D:\aixyc2026\tgkz2026\backend\data\tgmatrix.db`（`config.py` L57
> `DATABASE_PATH` 指向的即是这个文件，几小时前刚被写过，是当前实际在用的库，非孤立测试
> 库残留）用只读模式（`sqlite3.connect("file:...?mode=ro", uri=True)`，纯 `PRAGMA`/
> `SELECT` 查询，不可能写入或改动任何数据）直接查得：
>
> | 表 | 真实建表 SQL（来自 `sqlite_master`，即建表时实际执行的语句） | 历史行数 |
> |---|---|---|
> | `orders` | 与 `database.py` L590-633 的 `CREATE TABLE` **逐字节完全一致**（`product_type`/`product_level`/`product_duration`，**没有** `product_id`/`duration_type`/`duration_days`/`coupon_id` 任何一列） | **0** |
> | `coupons` | 与 `database.py` L931+ 的 `CREATE TABLE` **逐字节完全一致**（`coupon_code`/`is_active`/`expire_at`/`total_count`，**没有** `code`/`status`/`expires_at`/`max_uses` 任何一列） | **0** |
> | `users`（对照组） | — | 10 |
> | `licenses` / `activations`（对照组） | — | 0 / 0 |
>
> **结论从"可能命中生产"升级为"实证命中当前实际使用的库"**：① 不存在任何未纳入版本控制的
> 历史 `ALTER TABLE`——真实建表语句与当前代码完全对应；② `orders`/`coupons` 历史行数均为
> 0，而 `users` 表有 10 行真实账号数据——即**这套系统里已经有人真的注册使用，但从未有一笔
> 订单/优惠券成功写入过**，与"下单请求每次都会在 INSERT/SELECT 处被 `OperationalError`
> 打断、被 try/except 吞掉变成 500"这一假设的行为特征完全吻合（有调用尝试的迹象——用户在
> 用系统——但订单表始终空）。
>
> **2026-07-20·已修复（用户拍板方向：改代码对齐现有表结构，不改表）**：`license_server.py`
> 全部 8 个函数、11 处涉及 orders/coupons 列名不匹配的代码已修复，逐处清单：
>
> | # | 位置 | 修复内容 |
> |---|---|---|
> | 1 | `handle_create_payment` 优惠券查询 | `code/status/expires_at/max_uses` → `coupon_code/is_active/expire_at/total_count`(-1=不限次) |
> | 2 | 同上，字段访问 | `coupon['min_amount']` → `coupon['min_order_amount']`；`coupon_id=coupon['id']` → `coupon_code_used=coupon['coupon_code']` |
> | 3 | 同上，`INSERT INTO orders` | 去掉不存在的 `product_id`/`duration_type`/`duration_days`/`coupon_id`；新增必填 `product_type`(暂填常量 `'membership'`，见下方决策记录)；`coupon_id`→`coupon_code`；**新增**匿名结账兜底 `user_id_for_order = 解析出的user\|\|machine_id\|\|f"anon_{order_id}"`（真实表 `user_id` 是 `NOT NULL`，原代码在无法解析用户时传 `None` 会违反约束，这是本次修复过程中新发现的第 3 个问题，未在此前的审计版本记录里出现） |
> | 4 | `handle_payment_callback` 更新订单 | `tx_hash`→`transaction_id`；`paid_amount` 无对应列，不重复存储（与 `final_price` 同源，见决策记录） |
> | 5 | 同上，优惠券用量 | `order['coupon_id']`/`WHERE id=?` → `order['coupon_code']`/`WHERE coupon_code=?` |
> | 6 | 同上，会员到期计算 | `order['duration_days']` 不存在，改用 `order['product_duration']` 查同一份 `{'week':7,...}` 映射现算 |
> | 7 | 同上，写入 `licenses` 表 | 来源 `order['duration_type']`/`order['duration_days']` → `order['product_duration']`/现算的 `duration_days`（**目标** `licenses.duration_type`/`duration_days` 两列本身真实存在，只是原代码的**来源**字段名错了） |
> | 8 | 收入分析（按时长） | `SELECT/GROUP BY duration_type` → `product_duration AS duration_type`（**保留对外 JSON 键名不变**，只修后端查询，不改管理后台前端可能依赖的字段名） |
> | 9 | 订单 CSV 导出 | `o['duration_type']` → `o['product_duration']` |
> | 10 | `handle_admin_create_coupon` | 全量重写 INSERT 对齐 `coupon_code/name/min_order_amount/total_count/expire_at/is_active`；`name`(NOT NULL，原请求从未收集)暂用 `coupon_code` 本身兜底；`created_by`(真实表无此列)改交由既有审计日志承载，不重复开列 |
> | 11 | `handle_admin_disable_coupon` | `SET status='disabled'` → `SET is_active=0` |
>
> **两处需要知晓的产品决策（本次采用最小风险选项，未来如有需要应作为独立产品决策升级）**：
> - `product_type` 目前该业务只有"会员时长"一种商品概念，暂填常量 `'membership'`；将来若要上"按次计费"等新品类，需要在下单入口真正区分并传入。
> - `paid_amount`（实付金额，可能因 USDT 汇率滑点与应付 `final_price` 不同）本次未另开存储位置——如需精确记录"实付≠应付"，需要作为独立需求另开字段，不属于本次"修复列名不匹配"的范围。
>
> **验证方式**：① `ast.parse` 全文语法检查通过；② 真实 `import license_server` 成功；③ **复制真实 `tgmatrix.db` 到临时目录**（原文件全程只读，用后即删，验证后重新核对原文件行数仍为 0 未被污染），对拷贝执行全部 8 处修复后的真实 SQL 语句，**8/8 全部正确执行且数据正确落库/可读回**（含 INSERT 优惠券/下单、SELECT 优惠券条件查询、UPDATE 支付回调与用量、INSERT 授权记录现算 duration_days、分析查询别名保留对外字段名、禁用优惠券），非仅语法层面的验证。`git diff --stat` 确认改动范围精确限定在 `backend/license_server.py` 一个文件（81 行新增/27 行删除），未涉及 `database.py`（未改表结构，符合拍板方向）。

### ②（2026-07-20 第六阶段·彻底探索后修正结论）双 JWT / 双余额 / 双优惠券——重新定性

原判断（下方保留原文存档）预设"三者都需要统一"。派出彻底探索 agent 逐一核实 encode/decode 路径、余额读写路径、优惠券入口后，**结论有实质性修正**：

| 议题 | 原判断 | 修正后判断 | 依据 |
|---|---|---|---|
| 双 JWT | 需统一 | **本该独立，不建议统一**；真正的债是客户端硬编码密钥 | license/wallet/admin 三套 token 各自独立进程、独立签发校验、独立字段模型，从未有跨系统消费；统一默认值不解决任何功能问题，反而可能把已公开的字符串当"唯一密钥"用，安全性更差。真债在 `src/security-client.service.ts` L13-14 硬编码 `JWT_SECRET`（明文提交进前端代码，任何反编译桌面端都能拿到） |
| 双余额 | 需统一 | **本该独立，不建议合并** | `users.balance`（REAL，邀请返佣现金，无提现路径）与 `user_wallets.balance`（整数分账本，有完整充值/消费/提现）单位、用途、生命周期完全不同；无同步代码、无"未完成迁移"痕迹；UI 从不合并展示 |
| 双优惠券 | 需统一 | **license 与 wallet 两套本该独立**；但**发现了第三套、且是真 bug** | license 会员折扣券 vs wallet 钱包消费券业务语义不同，UI 已天然隔离；**新发现**：`core/coupon_service.py` 自己又建了第三套 `coupons` 表（`code`/`status`/`min_purchase`/`applicable_products`/`current_uses` 等字段），与 `database.py` 的 `coupons` 表**同名同库不同 schema**，`CREATE TABLE IF NOT EXISTS` 谁先跑赢，另一方全部读写必炸 |

**第三套优惠券系统（`CouponService`）已用真实数据实证确认损坏**：复制真实 `tgmatrix.db` 到临时目录，实例化 `CouponService` 指向该副本，调用其真实业务方法（非直接写 SQL）：

```
Init coupon DB error: no such column: code
Create coupon error: table coupons has no column named code
Get coupon error: no such column: code
```

`create_coupon()`/`get_coupon()` 均返回 `None`（异常被内部 `except` 吞掉，日志记了一行 error，业务方完全无感知），可达路由 `/api/v1/coupon/validate`、`/apply`、`/campaigns`（`business_routes_mixin.py` L160-220，经 `get_coupon_service()`）。

**2026-07-20·已修复（用户拍板：改表名避让，保留完整数据模型）**：`core/coupon_service.py` 的 `coupons` 表整表改名为 `campaign_coupons`（连带索引 `idx_coupons_code`→`idx_campaign_coupons_code`），全文件 7 处引用（CREATE/INSERT/SELECT×2/UPDATE×2/INDEX）逐一核对修复，`coupon_usages`/`campaigns` 两张不冲突的表未改名。**验证**：复制真实 `tgmatrix.db` 到临时目录，实例化改名后的 `CouponService` 真实调用 `create_coupon()`→`get_coupon()`→`use_coupon()` 全链路，4 组 8 项检查全部通过；额外验证新表与 `database.py` 原 `coupons` 表在同一个库里和平共存、原表列结构（`coupon_code`/`is_active`）分毫未被触碰。

---

<details><summary>原判断（2026-07-18 版，已被上方修正，保留存档）</summary>

### ② 双 JWT / 双余额 / 双优惠券（跨产品调用前必须统一）

- **双 JWT**：license 侧默认 `tgai-license-secret-2026`（license_server.py L42），
  wallet/auth 侧默认 `tgmatrix-jwt-secret-2026`（auth/utils.py L23 及 wallet/*handlers.py）。
  两边读同一个环境变量 `JWT_SECRET`，但**默认值分叉**——env 未注入时两套 token 互不可验，
  license 签出的 token 打不通 wallet 面，反之亦然。
- **双余额**：`users.balance`（license_server 邀请返傭直接 `UPDATE users SET balance=...`，
  L1493-1516）与 `user_wallets.balance`（wallet 模块）两本账，无对账机制——会出现
  "返傭进了 A 账本、提现看 B 账本"的资损口径。
- **双（实为三）套优惠券**：database.py 的 `coupons`、`core/coupon_service.py` 的
  `coupons`（又一份异构建表）、`wallet/coupon_service.py` 的 `user_coupons`。
- **修复要求**：统一为单一 secret（或显式双密钥+互验网关）、单一余额账本（另一处只读
  视图化）、单一券系统后，boundless 才能把多产品收款汇到该服务。

*(以上"修复要求"已被 2026-07-20 的彻底探索否定——不建议统一 JWT/余额/license-vs-wallet 优惠券；
真正要修的是客户端硬编码密钥与 `CouponService` 的第三套表名冲突，见上方修正结论。)*

</details>

### ③（2026-07-20·已修复）JWT_SECRET / payment_callback_secret 默认明文

- 原状：`JWT_SECRET` 缺省 `"tgai-license-secret-2026"`（license_server.py L42）；
  `payment_callback_secret` 缺省 `"tgai-payment-2026"`（L1402，管理员手动确认支付
  L1602 同用）。默认值等于公开：知道默认 secret 即可**伪造支付回调**给任意订单升会员
  并签发卡密（L1392 起整条链路）。

**修复方式（比原"修复要求"里的 fail-fast 更安全）**：没有采用"强制环境注入、缺失拒绝启动"——那样做若当前从未设过环境变量（大概率如此），下次重启会直接启动失败，等于自己制造一次计划外停机。改为**自动生成一次、之后持久复用**：
- `JWT_SECRET`：新增 `_load_or_generate_secret()`，未设环境变量时生成 `secrets.token_urlsafe(48)` 强随机值，写入本机 `data/.secrets/jwt_secret.key`（已确认被 `.gitignore` 现有 `data/` 规则覆盖，不会被提交）；下次启动读回同一个值，重启不受影响；显式设环境变量仍优先生效。
- `payment_callback_secret`：新增 `_get_or_create_payment_callback_secret()`，复用已有的 `db.get_setting`/`set_setting`（settings 表）持久化，同一套"自动生成一次、持久复用"逻辑，不必另开存储位置。
- **限制**：这是**本机持久化**，不是**跨部署共享**——多机场景下每台机器会生成不同的值；如果未来有"多台 license_server 互验对方 token"的真实需求，仍应显式设环境变量统一。

**验证**：`_load_or_generate_secret` 用子进程隔离测试（因为 `JWT_SECRET` 是模块 import 时算一次的全局变量），确认①未设环境变量时生成值≠旧硬编码默认值且长度像强随机值②重启（第二个子进程）读回同一个值，不会每次变③显式设环境变量时优先生效——3 组 6 项检查全过。`README-server.md` 的环境变量说明表已同步更新。

**范围外，未处理**：前端 `src/security-client.service.ts` 硬编码 `JWT_SECRET` 明文用于 HMAC 签名（L13-14）——这是不同性质的问题（客户端代码硬编码，需要重新设计签名机制或改用其它凴证分发方式，涉及 Angular/Electron 端改动+重新打包分发），本轮不在范围内。

### ④（2026-07-20·已修复，且过程中发现并修正了上一轮引入的一个回归）usage/log 签名密钥复用 JWT_SECRET

原状：`/api/usage/log`、`/api/token/refresh` 的请求签名（`_verify_request_signature`，原 L947）直接拿 `JWT_SECRET` 参与散列——意味着任何要上报用量的客户端都得持有签 token 的同一密钥；前端 `src/security-client.service.ts` L13-14 因此硬编码了这个密钥的明文拷贝。

**⚠️ 重要：处理这一条时发现，上一轮（§6③）把 `JWT_SECRET` 从固定默认值改成随机生成，意外引入了一个新回归**——由于签名验证当时仍在复用 `JWT_SECRET`，而前端硬编码的还是旧值，服务端换成随机值后，**所有已部署桌面客户端的 `/api/usage/log`/`/api/token/refresh` 请求签名会立即验证失败（401）**。这是本轮处理 §6④ 时顺带发现并一并修复的，不是独立问题——如果没有借这次机会检查，这个回归会一直潜伏到下次真实重启才暴露。

**修复**：拆出独立的 `USAGE_SIGNING_SECRET`（同一套 `_load_or_generate_secret` 机制，新增 `fallback_value` 参数）：
- 首次生成（无环境变量、无持久化文件）时，`fallback_value` 直接给旧版硬编码字符串 `"tgai-license-secret-2026"`——**保持与已部署客户端的向后兼容**，效果等同"复用 JWT_SECRET 的旧行为"，但从这一刻起是独立管理的值；
- `_verify_request_signature` 改用 `USAGE_SIGNING_SECRET`，不再读 `JWT_SECRET`；
- 未来如需真正轮换（配合桌面端重新打包发布新的签名密钥），显式设 `USAGE_SIGNING_SECRET` 环境变量即可单独操作，不再牵连 JWT 签发逻辑。

**验证**：子进程隔离测试 4 组场景——①首次生成值确实等于旧硬编码字符串（向后兼容）②与 `JWT_SECRET`（仍是随机值）互相独立③**用完全模拟前端算法**（`f"{ts}:{nonce}:{machine_id}:tgai-license-secret-2026"` → sha256）计算出的签名，服务端 `_verify_request_signature` 真实验证通过（这是回归修复的关键证据）④显式环境变量优先生效。6 项检查全过。

**2026-07-20 第七阶段·彻底重设计（用户拍板"三项全做"里的第一项）：静态密钥升级为会话级派生密钥 + 顺带发现并修复一个更严重的独立 bug**

深入到前端 `security-client.service.ts` 逐行核实时，发现两个问题，其中第二个比原计划要修的"密钥架构"问题本身更严重：

1. **架构问题（原定范围）**：`USAGE_SIGNING_SECRET` 仍是全局静态值，写死在前端一份反编译即永久泄露、影响全量用户。
2. **算法问题（意外发现，独立于①）**：`generateRequestSignature` 调用的 `hashString()`/`sha256()` 根本不是真正的 SHA-256——是一个 32 位滚动哈希（`hash = ((hash<<5)-hash)+char`，逐字符累加后 `&hash`），把结果的 8 位十六进制重复拼接 8 次凑成"看起来像"64 字符的哈希。真正的 `crypto.subtle.digest('SHA-256',...)` 实现是**另一个已存在但从未被调用**的方法 `sha256Async`——像是历史上有人开始修但没改完调用点就搁置了。这意味着：只要客户端和服务端各自忠实执行自己的代码，**这个签名机制在算法层面就永远不可能验证通过**，与密钥是明文还是随机值无关。

进一步核实"这个 bug 影响面多大"：`generateRequestSignature`/`createSecureHeaders`/`createSignedRequestBody` 全局唯一真实调用方是 `license-client.service.ts::refreshToken()`（对应 `/api/token/refresh`）；`/api/usage/log` 在当前前端代码里**没有任何调用方**（死代码，服务端实现在，前端从未发起）。而 `refreshToken()` 本身在实际运行中大概率从未被察觉失败过——因为**心跳**（`/api/license/heartbeat`，每 5 分钟一次，不需要签名）已经在更高频率地续签 token，20 小时一次的 `token/refresh` 即使一直悄悄 401，也被心跳这条并行、不设防的续签路径盖过去了，且 `refreshToken()` 的返回值在两处调用点都未被上层检查（失败无感）。

**重新设计（同时修两个问题，不是缝缝补补）**：

- **算法**：`generateRequestSignature`/`createSecureHeaders` 改为 `async`，改用已存在但从未接入的 `sha256Async`（真 `crypto.subtle`）；设备指纹/机器码继续用旧的快速哈希（那两处本就只需本机自洽的模糊标识，不需要跟服务端算出的值比对，不属于本次问题范围，维持原样不做不必要改动）。
- **密钥架构**：服务端在 `activate`/`heartbeat`/`token/refresh` 三处核发 token 时，新增 `_derive_request_signing_key(token)`（`HMAC(USAGE_SIGNING_SECRET, token)`），随 token 一起以 `requestSigningKey` 字段返回；前端新增 `setRequestSigningKey()` 存入本机（`security-client.service.ts`），后续签名改用这个"会话级"派生值，不再用写死的常量。派生值与 token 同生命周期（≤24 小时），反编译一次不再是"永久对全量用户有效"，而是"最多影响到当前这一份 token 过期为止"。
- **双轨验证兼容旧客户端**：`_verify_request_signature` 同时尝试"新版（按请求体里的 token 派生密钥）"与"旧版（全局静态 `USAGE_SIGNING_SECRET`）"，任一匹配即通过——尚未升级到新版安装包的已部署客户端不受影响，行为不倒退。
- **移除硬编码常量**：前端不再有任何写死的全局密钥字符串；`FALLBACK_SIGNING_KEY` 只在"还没收到服务端派发的会话级密钥"这个几秒钟窗口内临时使用（值与旧常量相同，纯粹为了让这个短窗口内的请求仍能通过服务端"旧版兼容"分支，不影响功能）。

**验证（分层，每层都是真实代码路径，不是只看类型检查）**：
- 后端：5 项检查——派生函数确定性/不同 token 结果不同/新版签名验证通过/旧版签名仍验证通过（向后兼容）/跨 token 串用被正确拒绝/乱填签名被正确拒绝。
- 前端算法：整个项目按真实 `tsconfig.json`（非放宽设置）跑 `tsc --noEmit`，零错误。
- **跨语言算法一致性**（本次验证的关键一环）：把 `sha256Async` 原样逻辑搬到 Node.js（真 `crypto.subtle`）与 Python `hashlib.sha256` 对同一字符串计算，结果逐字节相同——证明"假哈希"问题在算法层面已被真正修复，不是编译通过就假设正确。
- **端到端集成**：Python 端派生出一个真实签名密钥 → Node.js（模拟前端）用该密钥算出真实签名 → 交给 Python 服务端 `_verify_request_signature` 验证 → 通过。完整闭环，不是分段各自测试后"应该能接得上"。

**局限（如实记录）**：会话级派生密钥仍需要客户端本机持有（存在 localStorage），若攻击者能完整读出本机存储会同时拿到 token 与派生密钥——这是所有"客户端必须能独立签名"方案的共同限制，没有彻底解法；但相比"单一全局常量、反编译一次永久对全量用户有效"的旧方案，真实安全性有实质提升（洩露影响范围从"永久全量"收斂到"最多一个 token 周期"，且不再需要在客户端代码里写死任何密钥）。

**2026-07-20 第八阶段·进一步加固（用户拍板"三个问题全做"，本项属工程判断，直接实施）**：上面"完整读出本机存储"这一局限，本轮用 Electron `safeStorage`（Windows 对应 DPAPI，绑定当前系统用户账户）收窄了一层——`token` 与 `requestSigningKey` 落盘前先经 `safeStorage.encryptString` 加密（新增 `src/utils/secure-storage.util.ts` 统一封装，`electron.js` 新增两个 `ipcMain.handle` 承接实际加解密），效果：**只把本机存储文件整份拷到另一台机器或另一个系统用户下，解不开**——这类"拷文件搬家"式的攻击被挡住；非 Electron 环境（纯浏览器 Web 模式）/ 加密不可用时自动降级明文，不影响功能。

**仍未解决、如实记录（同一系统用户权限下的本机攻击）**：与当前登录用户同权限运行的恶意程序，仍可调用同一份 `safeStorage` API 自行解密——这是操作系统级信任边界（"同用户即互信"），应用层没有绕开的办法；真要防这一类威胁需要硬件级密钥隔离（如 TPM/Secure Enclave 绑定），对一个桌面订阅制工具而言投入产出比不划算，本轮判断到此为止，不再往这个方向加码。

**验证**：真实起 Electron 主进程（非 Node 模拟）测试 `safeStorage` 核心能力 + 用隐藏窗口模拟渲染进程真实走 `ipcRenderer.invoke` 全链路加解密（不是直接调用主进程函数），6 项检查全过；`secure-storage.util.ts` 编译后用 4 组 shim 场景测试（非 Electron 降级/Electron 成功加密/解密失败返回 null 不返回密文/兼容旧版明文遗留值），8 项检查全过。

## 7. 落地状态与下一步

- 契约端点：**全部在智控王侧**（`license_server.py`，默认 `:8080`，`python
  license_server.py` 即起）。本层交付：`LICENSE_CONTRACT.md`（本文件）+
  `license_client.py`（瘦客户端）+ `license_schema.json`（字段契约）；智控王代码零改动。
- 自检：`python license_client.py --selftest`——服务端不在线属正常，输出降级说明并 exit 0。
- 运维注意：`LICENSE_SERVER_URL` 缺省 `:8080` 与 chengjie 面的缺省端口相同
  （见 `platform/enable/client.py`），同机部署必须用环境变量分开。
- 待接线（下一阶段）：(a) §6①②③ 修复并回归后，website 收款页/products 门控再切真实
  流量；(b) `chatx-* ↔ {level}_{duration}` 映射表敲定回写 §5；(c) 心跳/配额接入
  products 侧的授权缓存与宽限期策略（§4 表）。
