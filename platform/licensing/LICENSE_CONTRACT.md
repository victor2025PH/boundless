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
  智控王在 boundless 的品牌位是 zhiliao（`chatx-*`）；`chatx-entry/team/flagship` ↔
  silver/gold/diamond 档的具体映射在接线时敲定并回写本节。
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
> **本轮仍未对 `license_server.py`/`database.py` 做任何改动**——这个决定（改 INSERT 对齐
> 建表，还是改建表对齐 INSERT 及其下游 `handle_payment_callback`/管理后台报表等所有读
> `orders`/`coupons` 的代码）涉及"要不要顺带把 `duration_days`/`coupon_id` 这类目前不存在
> 但业务上明显需要的字段正式补进表里"这类产品决策，不是我能单方面拍板的代码风格选择；但
> **"这个接口是否真的坏了"这个事实性问题，现在已经不是推测，是本机实证**。

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

### ③ JWT_SECRET / payment_callback_secret 默认明文（上线前强制环境注入）

- `JWT_SECRET` 缺省 `"tgai-license-secret-2026"`（license_server.py L42）；
  `payment_callback_secret` 缺省 `"tgai-payment-2026"`（L1402，管理员手动确认支付
  L1602 同用）。默认值等于公开：知道默认 secret 即可**伪造支付回调**给任意订单升会员
  并签发卡密（L1392 起整条链路）。
- **修复要求**：两个 secret 上线前强制环境注入并 fail-fast（缺失即拒绝启动，参考其
  `core/env_validator.py` 已有骨架）；回调另加来源白名单，callback secret 与 JWT_SECRET
  分离为独立随机值。

### ④（衍生）usage/log 签名密钥复用 JWT_SECRET

`/api/usage/log` 的请求签名直接拿 JWT_SECRET 参与散列（L883）——意味着任何要上报用量的
客户端都得持有签 token 的同一密钥。若平台侧将来要代理用量上报，必须先拆出独立的
usage 签名密钥；在此之前本层不封装该端点（见 §1 注）。

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
