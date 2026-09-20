# platform/credpool · 中央凭据池契约（Telegram api_id/api_hash 托管分配）

> 定位：托管凭据池的**实现**长在智控 MatrixX 的
> `tgkz2026/backend/admin/api_pool.py`（SQLite `ApiPoolManager`）+ `admin/handlers.py`
> （aiohttp 主后台，默认 `:8000`）——它已经是可独立运行的成熟池（容量/健康/自动切换/
> 分配策略/多租户/会员分级俱全），把实现搬进 platform 既不可行也不必要。按
> `licensing` / `enable` 既有模式：**引擎持实现，platform 只放『契约 + stdlib 瘦客户端』
> 消费其 HTTP 面。** 智控侧本轮只做**纯增量**改动（新增服务 token 鉴权分支），
> 管理员路径行为零变化。

## 0. 为什么要有这层

新用户在 `my.telegram.org` 申请 api_id/api_hash 是全流程最高的一道坎：

- 官方规则：**一个手机号只能注册一组 api_id**；
- 申请页极脆：无提示 `ERROR`、VPN/广告拦截/IP 与号码归属国不一致/标题短名字符规则/
  24h 风控/429 限流，中国大陆尤其容易全程失败；
- 用户心理：看到 "API ID / API Hash" 直接判定"这是给程序员的"，转化黑洞。

中央池把凭据变成**集团统一注册、统一补给**的后台资源，各产品按需分配，
终端用户**全程无需接触凭据**。

## 1. 能力与归属（端点清单）

实现全部在智控主后台（`admin/handlers.py`，路由注册在 `api/admin_module_routes.py`）：

| 能力 | 端点 | 认证 | 瘦客户端方法 |
|---|---|---|---|
| 健康探针 | `GET /api/health` | 无 | `health()` / `available()` |
| 分配（按手机号粘定） | `POST /api/admin/api-pool/allocate` | 管理员 JWT **或** `credpool:allocate` | `allocate()` |
| 释放额度 | `POST /api/admin/api-pool/release` | 管理员 JWT **或** `credpool:release` | `release()` |
| 查询账号绑定 | `GET /api/admin/api-pool/account` | 管理员 JWT **或** `credpool:account` | `account()` |
| 回报使用结果 | `POST /api/admin/api-pool/result` | 内部调用（历史上免鉴权） | `report()` |

运营侧（仅管理员，产品不消费，故**不进瘦客户端**）：批量导入
`POST /api/admin/api-pool/batch`、容量预测 `GET /api/admin/api-pool/forecast`、
告警 `GET /api/admin/api-pool/alerts`、分组/规则/统计等。

## 2. 认证：产品服务作用域 Token（本轮新增）

原先 allocate/release/account 只认**人类管理员 JWT**。把管理员 JWT 发给各产品
权限过大、无法按产品吊销、审计分不清谁动的。故新增最小权限凭证
（`tgkz2026/backend/admin/service_token.py`）：

- 明文形如 `svc_{product}_{32字节随机}`，**落库只存 sha256**，明文仅签发时返回一次；
- 作用域 `域:动作`，支持 `credpool:*` 通配；
- 请求头 `X-Service-Token: <明文>`；
- 校验通过后回传的 principal 字段刻意与管理员 payload 对齐（`id`/`sub` =
  `service:{product}`），**既有审计日志零改动**即可记录"是哪个产品做的"；
- 可独立吊销（软禁用，保留审计痕迹）。

签发/查看/吊销（管理员）：

```
POST   /api/admin/service-tokens     {product, scopes:[...], note}   # 明文只返回这一次
GET    /api/admin/service-tokens                                      # 不含明文/哈希
DELETE /api/admin/service-tokens/{token_id}
```

消费侧把明文放进环境变量 `CREDPOOL_SERVICE_TOKEN`，**绝不入库、不进配置文件、不落日志**。

## 2b. 会员档位：隔离多开＝专业版权益

池表早有 `min_member_level` / `is_premium`，`ApiPoolManager.allocate_api` 也早支持
`member_level` 过滤——但 HTTP 层此前没传，于是所有调用一律按 `free` 分配，
专属凭据永远发不出去。本轮补上，并定死一条**安全红线**：

> **档位必须服务端解析，绝不信客户端自报。**

- 产品只送 `license_key`（客户卡密）→ 服务端查 `licenses` 表定档
  （`admin/member_tier.py`）；卡密不存在 / 被禁用 / 已过期 → **一律降到 `free`**。
- 产品在 payload 里自报的 `member_level` **被忽略**（否则任何客户端都能给自己发
  钻石档，专属凭据白送、付费用户的隔离资源被挤占）。
- 只有**管理员 JWT** 调用方才允许直接指定 `member_level`（后台手动指派的既有用法）。
- 响应回传实际生效的 `member_level`，产品据此如实展示"你当前享有的隔离等级"。

**卡密绑机（收口"一张卡密贴几台机器"）**：`status='unused'` 的卡密也认档位——
不认的话 ChatX 客户拿到卡密无处激活（ChatX 不集成智控的 activate 流），功能直接不可用。
代价原本是被转发的卡密可多处同时享权益，现已收口：

- 产品必须随 `license_key` 一起送 `machine_id`（本机稳定标识）；
- 卡密**首次使用即绑机**（写 `licenses.machine_id`），之后换机 → 降到 `free`；
- **服务 token 调用方不送 `machine_id` 一律按 `free`**——否则"不送"就成了绕过绑机的后门；
- 管理员 JWT 路径不受影响（不需要 machine_id）。

ChatX 的 `machine_id` 存在数据目录（`config/.credpool_machine_id`），重启/重装不变，
纯随机、不含主机名或序列号等可反查身份的信息。客户真换机 → 走客服解绑（清空该列）。

## 2c. 独立出口 IP（三隔离的第三件套）

只发凭据不给出口，一批号仍会因同 IP 被聚类连坐。故付费档在分配凭据时**一并下发
独立出口**（智控 `proxy_pool` 分配，按同一粘定键绑定），响应体 `data.proxy` 形如
`{scheme, host, port, username, password}`。

- **免费档不给**——这正是专业版权益的一部分；
- 代理池为空（尚未采买）→ 不给，产品回落自身代理配置，**不阻塞登录**；
- 消费侧优先级：**运营显式指定的代理 > 池下发的出口**（显式配置代表人的意图）；
- 出口字段不完整（缺 host/port）→ 整个丢弃走直连，**半个代理比没有更糟**（残缺配置
  会让整条连接起不来，而没有代理只是直连）；
- 🔒 `proxy` 里的账密与 `api_hash` 同级：禁止落日志。

第三件套**设备指纹**不经中央池——它由 chengjie 本地种子派生并落账号 meta
（`device_fingerprint.py`），无需服务端参与。

配套的**产品归属**：`telegram_api_allocations` 增 `product` 列（幂等 ALTER），
取自鉴权主体（服务 token 带 `product`），不信 payload——集团多产品共池后，
"哪个产品吃了多少容量"才有账可查，日报按产品分帐即源于此。

## 3. 契约（stdlib 瘦客户端签名）

`platform/credpool/credpool_client.py`（纯 stdlib，零第三方依赖，零反向依赖）：

```python
CredPoolClient(base_url=None, service_token=None, timeout=8.0)
  # base_url     ← 环境变量 CREDPOOL_BASE_URL（缺省 http://127.0.0.1:8000）
  # service_token← 环境变量 CREDPOOL_SERVICE_TOKEN
  .health()                          -> dict  # /api/health
  .available()                       -> bool  # HTTP 面可达
  .configured()                      -> bool  # 是否已配服务 token
  .allocate(phone, account_id=None, api_id=None, strategy=None, license_key=None)
                                     -> dict  # data{api_id, api_hash, member_level, ...}
  .release(phone)                    -> dict
  .account(phone)                    -> dict  # 只读绑定关系
  .report(api_id, success, error=None, phone=None) -> dict
```

字段完整 schema 见同目录 `credpool_schema.json`。
**不做能力实现**：客户端不含分配算法/健康计分/容量管理——那些只在智控服务端发生。

## 4. 可降级说明（中央池离线时消费侧如何优雅退化）

所有方法**绝不抛异常**：任何失败（超时/连接失败/JSON 解析失败）收敛为
`{"available": False, ...}`；服务端明确拒绝（4xx/5xx）则 `available=True` +
`success=False`（业务拒绝，不是降级）。

| 不可用能力 | 消费侧退化行为 |
|---|---|
| `allocate` | **分两种号，规则不同**（见 4b，封号级区别）：新登录 → 回落自带凭据（BYO）；已在池内的号 → 回落该号的凭据缓存，缓存也没有则**拒绝启动**，绝不用自带凭据 |
| `release` | 跳过（额度由池侧的容量巡检兜底回收），不阻塞退登 |
| `report` | 跳过（健康分少一次样本，不影响本次登录） |
| `account` | 看板显示"未知"，不阻塞主流程 |

**幂等性**：`allocate` 对同一 phone 反复调用返回同一组凭据（池侧按手机号粘定）。

## 4b. 「session 与 api_id 必须同源」——本契约最硬的一条

一个 pyrogram session 是用**某一组** api_id/api_hash 建立的。让这条 session 报出
另一个 api_id，Telegram 侧是明确的风控信号。所以粘定不只是"省事"，它是安全边界。

2026-07-27 真实环境审计（7 个协议号）暴露了两个方向的破口，**都已修**：

1. **池不可达 → 池内号回落自带凭据**。原设计"只存 key 不存凭据、池是唯一权威"
   在池离线时正好踩这个坑。修法＝该号成功分配时把凭据缓存进账号 meta
   （`credpool_cred`），池离线时用它继续在线；没有缓存则拒绝启动。
   *为什么缓存不是安全降级*：注册表里本来就存 `session_string`（等于账号完全
   访问权），`api_hash` 是应用级凭据，敏感度远低于它。
2. **池的分配台账丢了记录 → 悄悄换成另一组凭据**。台账会因临时模式/换库/人工
   清理而丢（实测就有一例）；丢了池就把老号当新号重分配。修法＝消费侧把缓存里
   的 api_id 作为 `api_id` 参数**钉**给池；万一池仍返回另一组，**以 session 绑定
   的那组为准**并打 ERROR 日志（凭据真被停用就是连不上，运营重扫即可——这比带着
   错配的 api_id 连上去安全）。

**存量号**（开池之前登录的、meta 里没有粘定键）：一律不碰池、保持本地凭据与
pyrogram 默认指纹。"不动"就是对存量号最安全的处理。

对应门禁：`engines/chengjie/tests/test_credpool_wiring.py` 的
`test_legacy_account_keeps_local_credentials` /
`test_pooled_account_refuses_config_creds_when_no_cache` /
`test_cached_api_id_is_pinned_to_pool` /
`test_mismatched_pool_answer_loses_to_session_binding` 等。

## 4c. 池服务的健康：只探 /health 是不够的

`/api/health` 返回 200 只说明进程活着。2026-07-27 这轮实测抓到两个"health 全绿
但 allocate 已坏"的真 bug（`report` 写事务里自死锁 30 秒；归还后重分配撞
`account_phone` 唯一键）——与 2026-07-14 EmotionTTS 静默降级 2h20m 同一形态。

因此看门狗（`deploy/instances/credpool_watchdog.py`，计划任务 `CredPoolWatchdog`
每 5 分钟）**真打业务口**：一次性 key 走完 `allocate → release`，拿不到凭据即判
不健康（`kind=hang`），与端口是否活着无关。两振确认才重启、30 分钟重启冷却、
状态落 `credpool_watchdog.state.json` 并经 `credpool.watchdog_state_path`
（**默认空，仅供应商侧填**）进 ops 卡。

演练教训：重启子进程**不能用管道捕获输出**——被拉起的池服务会继承 PowerShell
的输出句柄，管道要等孙进程退出才关闭，`subprocess` 连 timeout 都救不了（超时后
Python 会 kill 子进程再次 communicate，仍卡在同一管道）。首次演练实测池已重启
成功而看门狗自己挂死 5 分钟。现改为重定向到文件、事后读取。


## 4d. 消费侧最容易漏的一格：**接了不走的那条路**

三隔离全都写好、门禁全绿、契约自检 27 项通过 —— 然后 2026-07-27 的真实环境审计
发现**它们在生产里全是空转的**。

根因：ChatX 的协议号有两条 worker 路径，由 `platform_login.telegram.companion_runtime`
二选一：

| 开关 | worker | 原本是否接隔离 |
|---|---|---|
| `false` | `TelegramProtocolWorker`（裸 pyrogram） | ✅ 接了凭据池 + 指纹 |
| `true` | `TelegramCompanionWorker` → A 线 `TelegramClient` | ❌ 从全局 `telegram.*` 取凭据，完全不带设备字段 |

本机开的是 `true` —— 也就是接了的那条根本没在跑。后果两层：

- **凭据**：扫码用池凭据、重连用配置那组。当时没出事只因为配置里那组恰好就是池里
  唯一那组（**巧合，不是设计**）；池里一旦有第二组凭据，就是 §4b 说的错配。
- **指纹**：扫码那刻向 Telegram 报派生机型（MacBook Pro），之后每次重连又回
  pyrogram 默认值 —— **每次重连都在换设备**，比根本不做指纹更可疑。

修法：`TelegramCompanionWorker._isolation_overlay()` 在连接前现场解析
（池凭据 + 落库指纹本体 + 池出口）并合进 `account_cfg`；`TelegramClient` 把
`device_model/system_version/app_version` 与池出口真正喂给 `pyrogram.Client`。
**只在凭据确实来自池时才覆盖**（`source in {credpool, credpool_cache}`）——存量号
保持 `TelegramClient` 原读法，不动我不拥有的东西。

教训（对所有 platform 契约通用）：**"功能写了"和"生产在走的那条路上生效了"是两件
事，中间的失效完全静默**。所以除了单元门禁，还要有一条对着**真实环境**跑的验证：
`deploy/instances/isolation_verify.py`（读实例真配置 + 注册表真账号 + 打真池，逐号
打印连接那一刻的凭据/指纹/出口，并已纳入 `credpool_chain_check.py`），
以及 `tests/test_companion_worker_isolation.py::test_both_worker_paths_wire_isolation`
（钉住"两条 worker 路径都必须接"）。

## 5. 依赖方向

`engines/* → platform/credpool(契约+client) → (HTTP) → 智控主后台 api-pool`。
platform 不 import 智控代码；凭据明文、池库、分配策略全部留在智控服务端。
本目录**不建 `__init__.py`**（顶层 `platform` 与标准库同名，包式导入会遮蔽标准库）。

## 6. 容量与安全默认值（`max_accounts`）

Telegram **没有**公布"一组 api_id 能带多少账号"的硬上限。官方明确的只有三条：

1. 一个手机号只能注册一组 api_id；
2. **公开/泄露**的 api_id 会触发 `API_ID_PUBLISHED_FLOOD`，登录直接失败；
3. 用非官方 API 客户端登录的账号**一律进入观察**，滥用即封。

因此"最安全"不是去猜一个大数，而是**压低单组凭据的爆炸半径**：

- 集团默认 `max_accounts = 5`（与智控池自身的保守默认
  `TelegramApiCredential.max_accounts` 一致），宁可多注册几组，也不把鸡蛋堆一篮；
- **绝不使用公共 api_id 兜底**（如 Telegram Desktop 的 `2040`）——那正是第 2 条的
  典型触发源。池空时应排队/提示补给，而不是回落公共凭据；
- 凭据隔离必须与**独立代理 IP + 独立设备指纹**同时上（智控 `process_login_payload`
  的 Step1+Step2 已实现双池），只给凭据不给 IP/指纹反而更容易被聚类连坐。

补给闭环：`forecast`（容量预测）+ `alerts`（告警）+ 每日 Telegram 日报 → 运营按需
`batch` 批量导入新凭据。

## 7. 开通步骤（运营 / 部署）

1. **注册托管凭据**（集团侧，一次性 + 定期补给）：用集团维护的手机号在
   `my.telegram.org` 逐个申请 api_id/api_hash（**一号一组**），在智控后台
   `POST /api/admin/api-pool/batch` 批量导入，`max_accounts` 用默认 **5**。
2. **签发产品服务 token**（管理员）：
   `POST /api/admin/service-tokens {"product":"chatx","scopes":["credpool:*"],"note":"智聊桌面端"}`
   → 响应里的 `token` **只显示这一次**，立刻保存。
3. **产品侧配置**（以 chengjie 为例）：
   - 环境变量：`CREDPOOL_BASE_URL=http://<智控主机>:8000`、`CREDPOOL_SERVICE_TOKEN=svc_chatx_…`
     （🔒 token 走环境变量，别写进 `config.local.yaml`）；
   - 配置开关：`platform_login.telegram.credpool.enabled: true`
     （与 `platform_login.telegram.protocol_enabled: true` 一起才生效）；
   - 重启该实例后生效（凭据解析在 .py 里，不属于热更新范围）。
4. **验收**：`python platform/credpool/credpool_client.py --selftest` 应显示
   `transport = UP`；扫码新增一个 Telegram 账号，注册表 meta 里应出现 `credpool_key`，
   智控后台 `GET /api/admin/api-pool/account?phone=<该 key>` 能查到绑定。
5. **日常补给**：每天 09:00（`CREDPOOL_REPORT_HOUR` 可调）Telegram 收到「凭据池日报」，
   出现「两周内可能耗尽」就提前注册新凭据再走第 1 步——**别等用光**，
   my.telegram.org 有 24h 风控延迟。

## 8. 落地状态

- 智控侧（本轮增量，管理员路径行为零变化）：`admin/service_token.py`（新增）、
  `admin/handlers.py`（`_verify_service_or_admin` + 三个 token 管理处理器）、
  `api/admin_module_routes.py`（三条路由）、`admin/pool_daily_report.py`（新增日报）、
  `admin/alert_service.py`（`send_alert` 增 `force` 形参）、`admin/scheduler.py`（注册日报任务）。
  自检：`python backend/tests/selftest_service_token.py`、
  `python backend/tests/selftest_pool_daily_report.py`（均用临时库 + 假告警服务，
  不碰生产库、不真发 Telegram）。
- 本层交付：`CREDPOOL_CONTRACT.md`（本文件）+ `credpool_client.py` + `credpool_schema.json`。
  已登记进 `platform/chain_selftest.py`（第 7 条契约，断总线降级自测全绿）。
- chengjie 侧：`src/integrations/credpool_bridge.py`（新增适配层）+
  `telegram_protocol_login.py`（登录按账号取凭据 / 落粘定键 / 回报）+
  `account_orchestrator.py`（runner 用粘定键取回同一组）+ `account_registry.py`
  （删号还容量）+ `desktop/build/build_backend.py`（把瘦客户端打进桌面包）。
  门禁：`tests/test_credpool_bridge.py`、`tests/test_credpool_wiring.py`。
- 自检：`python credpool_client.py --selftest`——服务端不在线属正常，输出降级说明并 exit 0。
- **真实接线演练**：`python platform/credpool/credpool_live_smoke.py`——起真 aiohttp
  挂智控 admin 路由（临时数据目录 + 随机端口 + 起服前自证隔离，绝不碰生产库/Telegram），
  用真实瘦客户端走完整链路：鉴权拒绝 → 免费档 → 粘定复用 → 卡密定档 → 首次绑机 →
  换机降级 → 独立出口下发 → 成/败回报 → 释放 → 结构化错误 → 落库核对 → 真数据日报。
  **前三期全是单测与替身，这条链从未真打过 HTTP；首次演练即揪出 5 个真 bug**（详见 §9）。
- **运维工具**：`deploy/instances/credpool_stage.py`（`serve` 常驻/演练两用 ·
  `add-cred` / `add-proxy` / `add-license` / `unbind-license` / `token` /
  `verify` 档位×出口矩阵验收 / `ledger` 台账 / `status` / `enable` / `disable`）
  + `credpool_service.ps1`（`-Start` / `-Stop` / `-Status` / `-InstallTask` 开机自启）。
  常驻数据目录 `D:\chengjie-instances\.ops\credpool`。
- 运维注意：`CREDPOOL_BASE_URL` 指向智控**主后台** `:8000`；`LICENSE_SERVER_URL` 指向
  license_server `:8080`，两者是不同服务，勿混。

## 9. 真实演练揪出的既有缺陷（均已修复）

单测用替身，替身按"我以为的样子"回应，所以这些只有真打服务端才会现形：

| # | 现象 | 根因 | 影响 |
|---|---|---|---|
| 1 | 付费卡密恒定判成 `free` | `schema_adapter.get_db_path()` 优先 `config.DATABASE_PATH`，而池走 `resolve_db_path()`（env 优先）——**查卡密与发凭据不在同一个库** | 专业版权益完全发不出去 |
| 2 | `report` 每次耗时 **32.95 秒** | `_record_hourly_stat` 在外层写事务未提交时**另开连接**写同库 → 自锁到 SQLite 30s busy_timeout | 每次登录回报卡 30 秒，并**级联拖垮**同期其它写请求；且小时统计**从未写入成功**（异常被吞） |
| 3 | `release` 一律回报失败 | `alloc.get('account_id')` —— `sqlite3.Row` **没有 `.get()`** | 容量其实已还，但接口报错，调用方可能重试/告警；释放审计从未记录 |
| 4 | 登录失败回报必崩 | 同 3，`api_row.get('consecutive_failures')` 在**失败分支**（成功分支正常，故极难发现） | 坏凭据的健康分永远累计不上，自动切换形同虚设 |
| 5 | 错误响应是裸 500 HTML | `ErrorCode.OPERATION_FAILED` **从未定义**，却被 handlers 引用 **48 处** | 所有这些错误分支抛 AttributeError，客户端拿不到结构化错误 |
| 6 | 同一个号**释放后再也分配不回来** | `account_phone` 有 UNIQUE 约束，而释放只把行标成 `released` 不删行 → 二次分配撞唯一键 | 换机 / 删号重加 / 容量回收后重新接入，全部 500 失败（只有跑第二遍才现形，首轮表是空的看不出来） |
| 7 | 放弃扫码会**永久吃掉一个账号位** | 登录一开始就申请凭据，用户关页面 / 码过期 / 取消时没人归还 | `max_accounts=5` 时五次放弃就占满一组凭据，之后新号静默回落自带凭据（读台账时发现：只成功扫 1 个号却占了 3 个位） |

第 7 条的修复在 chengjie 侧（`telegram_protocol_login`）：`expired` / `failed` / `cancelled` /
`login.start()` 抛异常 四条路径全部归还容量；**授权成功的不归还**——那笔分配后面
runner 还要凭同一个粘定键取回同一组凭据。

**尚未处理（同类，但不在本契约链路上，留给智控负责人决定）**：`OPERATION_FAILED`
目前映射到 HTTP 500。想改成 4xx，但它覆盖 48 处调用，语义上既有「池已空」（可重试）
也有「已存在」（不可重试），任何单一状态码都不对；而响应体已是结构化 JSON
（`code` + 明确消息），瘦客户端也已正确处理。为语义模糊的收益去动 48 处错误路径，
风险大于价值。

## 10. 观测的两个口径（别只看一个）

| 口径 | 数据源 | 特点 |
|---|---|---|
| 三盾比率 `pool_share` / `proxy_share` / `fp_share` | 进程级计数（credpool_stats） | 反映**近期行为**；**重启即归零**——那时看板显示 0% 但账号其实都隔离着 |
| 在册覆盖 `shields` | 账号注册表（isolation_shields） | 反映**当下实况**，重启不受影响；只算协议号、剔除已移除号，避免分母被撑大 |

两个都要有：只看比率会在每次重启后误报「隔离没生效」；只看覆盖率则看不出
「最近新扫的号有没有走池」。`shields.weakest` 直接指出三件套里最该补的那一件。
