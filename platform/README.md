# platform/ · 无界共享底座（Shared Platform Layer）

> 定位：三系七产品**共享的横切能力**的单一归属地。产品/引擎向这里"接契约"，而不是各自复制一份。
> 依赖铁律：`website / products / engines` → **可依赖 platform**；`platform` **绝不反向依赖**任何产品或引擎。
> 现状：本层已从"纯契约"起步落地两块**可用实现**——`licensing`(SKU 注册表 + stdlib 读取器) 与 `compliance`(契约 + 可降级瘦客户端)；其余(identity/observability/brand)仍为契约+归属地图。按"先立契约、再逐引擎迁移、每步可回归"推进，不做一次性大爆改。

---

## 1. 五个共享契约（子目录）

| 子目录 | 契约（做什么） | 关键接口（建议签名） | 当前实现所在 | 迁移状态 |
|---|---|---|---|---|
| `identity/` | **身份 / 资产总线**：一次克隆声音+形象→跨产品复用的 Profile | `getProfile(userId)` · `bindVoice/Face(profileId, asset)` · `listAssets(profileId)` | avatarhub `avatar_hub.py`(Profile/角色库) · `profile_package.py` | 待抽取 |
| `licensing/` | **授权 / 计量 / SKU 门控**：edition→额度→按量扣减→开通 | `checkLicense(machine)` · `meter(userId, sku, units)` · `gate(feature, edition)` | **`sku_registry.json` + `sku_registry.py`(读取器,已落地)** · avatarhub `license*.py` · chengjie `src/licensing/`(计量/门控待统一) | SKU 单一源✓；计量/门控待统一 |
| `compliance/` | **合规出口**：C2PA 溯源 + Ed25519 验真 + 不可见水印 + 克隆伦理校验 | `verify_audio/verify_media(b64)` · `status()` · `pubkey()`（**签名在 avatarhub 就地，不出机**） | **`CONTRACT.md` + `client.py`(瘦客户端,已落地)** → avatarhub `/api/provenance/*` | 契约+客户端✓（签名留 avatarhub 持钥方） |
| `observability/` | **可观测**：KPI 埋点 / 端到端延迟 / 漏斗 / 遥测（打通 CAC/CPL/ROAS） | `emit(event, props)` · `latency(stage, ms)` · `funnel(step)` | avatarhub `metrics.py` `telemetry*.py` · chengjie `src/monitoring/` | 待统一 |
| `brand/` | **品牌设计令牌**：配色/字体/圆角/产品命名（三系七产品单一真相） | 令牌 `brand.css`/`brand.ts` · 产品表同 `website/lib/brand.ts` | **`platform/brand/tokens.json` + `brand.css` + `tailwind-preset.cjs`（已收敛，融合期落地）** · `brand-assets/`（视觉产物库） | 令牌单一源✓；三端接线待迁移 |

> 产品之间**横向零 import**：任意跨产品交互都经 platform 的契约（HTTP / 注册表 / Profile 总线）完成。

### 1b. 融合期新增两条"产业链条"契约（TG-AI智控王接入）

把"上游获客 → 中游承接 → 赋能层"这条链落到接口层，新增两条与既有五契约同构（**契约 + stdlib 瘦客户端 + 可降级**）的总线：

| 子目录 | 契约（做什么） | 关键接口（建议签名） | 提供方 | 消费方 | 状态 |
|---|---|---|---|---|---|
| `leadbus/` | **线索交接总线**：获客产品把线索标准化交给承接中台；总线可选（无 `BOUNDLESS_BUS_URL` 落本地 outbox）、fail-soft、幂等补投 | `publish(lead)` · `drain_outbox()` · `status()` · `available()` | chengjie `/api/leadbus/ingest`（**已接线**，`src/web/routes/leadbus_routes.py`） | 智控王 `member_extraction_service.py`（**已接线**，默认关 `TG_LEADBUS_ENABLED`）/ huoke | **契约+client+服务端全链路✓**（`chain_live_smoke.py` 实测联通） |
| `enable/` | **赋能能力网关**：承接侧调底层引擎能力——AI 回复转克隆音 / 翻译 / 数字人渲染 | `tts_clone_speak()` · `translate()` · `translate_status()` · `avatar_render()` · `status()` | avatarhub `/api/tts_only`+`/api/enable/status`（探针已接线，TTS 路径已纠偏未验证）/ chengjie `/api/translate`+`/api/enable/status`（**已接线**） | chengjie / 智控王（承接） | **translate 全链路✓**；tts_clone_speak 路径纠偏未验证；avatar_render 仅路径猜测未核实 |
| `replybus/` | **决策回执总线**：智控王收到入站私信 → 问 chengjie『这条怎么回』→ 拿决策在自己 session 上执行；同步问答、**无 outbox**（过时决策不补投）、bus 离线回落本地 AI | `decide(message)` · `status()` · `available()` | chengjie `/api/replybus/decide`（**已接线**，`src/web/routes/replybus_routes.py`，落 draft/silent 不落 send） | 智控王 `private_message_handler.py`（**已接线**，默认关 `TG_AI_BACKEND=local`） | **契约+client+服务端+智控王热路径全链路✓**（子进程级真实回归测试通过） |

> 三条新契约同守"总线可选"铁律：**每个产品脱离总线能独立运行（本地兜底/优雅退化），接上总线自动入链**——这是"各自独立又是一条链"的技术保证。隐私边界见 `leadbus/CONTRACT.md §4`：线索业务载荷走 leadbus 点对点，**绝不进 observability 数仓**（数仓只收 `*.lead.captured` 计数指标）；replybus/enable 的入站原文/回复文本同样只作请求载荷，不落日志/事件。
> **防双发红线**（`replybus/CONTRACT.md §5`）：Telegram `.session` 执行权唯一归智控王，chengjie 只返回决策绝不代发（`replybus_routes.py` 决策口径刻意只产出 `draft`/`silent`，从不产出 `send`），承接用号与获客用号分池。
> **鉴权缺口（2026-07-19 三个独立子任务同时发现，已修复客户端侧）**：`leadbus`/`enable`/`replybus` 三个瘦客户端原不发 `Authorization` 头，chengjie 若配置 `web_admin.auth_token` 会一律 401（现象：不报错但总线形同虚设，client 把 401 收敛成 fallback/queued）。三个 client 已加 `auth_token` 参数（缺省读 `BOUNDLESS_BUS_TOKEN` 环境变量），部署时按需配置。

### 1c. licensing 授权/收款：新增消费侧瘦客户端（引擎持实现）

`platform/licensing/` 原有 SKU 注册表读取器（`sku_registry.py/.json`）；融合期新增 `license_client.py` + `LICENSE_CONTRACT.md` + `license_schema.json`：按 avatarhub 模式，**卡密/收款实现留在智控王 `license_server.py`（aiohttp 独立服务），platform 只放契约 + stdlib 瘦客户端**消费其 HTTP 面（validate/activate/heartbeat/quota/usage/products/payment/order）。`LicenseClient` 可降级（`LICENSE_SERVER_URL` 不可达返回 `{"available":False}`），并与 `sku_registry` 按 `sku_id` 对齐。**迁移前必须先修复的服务端高危问题**见 `LICENSE_CONTRACT.md`：orders/coupons 表 schema 漂移、双 JWT/双余额/双优惠券、默认明文密钥。

### 1e. credpool 中央凭据池：Telegram api_id/api_hash 托管分配（集团统一注册与补给）

新用户在 `my.telegram.org` 申请 api_id/api_hash 是接入转化率的最大黑洞——官方规则**一个手机号只能注册一组**，申请页无提示 `ERROR`/VPN/广告拦截/IP 与号码归属国不一致/24h 风控/429 限流，国内尤其易全程失败；且用户看到 "API ID / API Hash" 会直接判定"这是给程序员的"。故新增第七条契约，把凭据变成**集团统一注册、统一补给**的后台资源，终端用户全程无需接触。

| 子目录 | 契约（做什么） | 关键接口 | 提供方 | 消费方 | 状态 |
|---|---|---|---|---|---|
| `credpool/` | **中央凭据池**：按账号分配/释放托管 Telegram 凭据，回报健康分喂池的自动切换 | `allocate(phone)` · `release()` · `report()` · `account()` · `available()` | 智控 `tgkz2026/backend` 主后台 `:8000` 的 `api-pool`（**已接线**，`admin/api_pool.py` 早有成熟实现） | chengjie（智聊/通译，**已接线**，默认关 `platform_login.telegram.credpool.enabled`）/ 智控自身 | **契约+client+服务端鉴权+引擎接线✓** |

三个与既有契约不同、需要知道的点：

- **鉴权是新增的第三种**：原 `api-pool` 只认人类管理员 JWT。给产品发管理员 JWT 权限过大且无法按产品吊销，故智控侧新增**产品服务作用域 token**（`admin/service_token.py`，请求头 `X-Service-Token`，作用域 `credpool:*`，落库只存 sha256，明文仅签发时返回一次，可独立吊销）。管理员路径**行为零变化**，是纯增量分支。
- **粘定键（sticky key）是硬约束**：pyrogram 的 session 与登录时所用的 api_id 绑定，换凭据会把会话跑坏。故每个账号首次扫码生成 `credpool_key` 写进账号注册表 meta，之后 runner 每次拉起用同一 key 取回**同一组**凭据——本地**不存 api_hash 明文**，凭据权威始终在中央池。
- **容量安全默认 `max_accounts = 5`**：Telegram 未公布单 api_id 的账号上限，官方硬规则只有"一号一组""公开 api_id 触发 `API_ID_PUBLISHED_FLOOD`""非官方客户端登录进入观察"三条。故按**压低爆炸半径**取保守值，并**禁止用公共 api_id（如 Telegram Desktop 的 2040）兜底**。补给闭环＝`forecast` 容量预测 + `alerts` 告警 + **每日 Telegram 日报**（`admin/pool_daily_report.py`，复用既有 `AlertService` 的 Telegram 渠道，与其它日报同一个账号，零新增配置，按产品分帐）。

**隔离＝专业版权益（三隔离已齐）**：档位由**服务端**查 `licenses` 表判定（`admin/member_tier.py`），产品只送 `license_key` + `machine_id`、自报档位一律忽略；卡密**首次使用即绑机**，换机降回 `free`，且**不送 `machine_id` 一律按 `free`**（否则"不送"就成了绕过绑机的后门）。付费档拿到的是三件套：① `min_member_level` 受限的**专属凭据**；② 智控代理池按同一粘定键下发的**独立出口 IP**（代理池为空则不给，回落自身配置不阻塞登录；运营显式指定的代理永远优先）；③ chengjie 侧 `device_fingerprint.py` 派生的**自洽且恒定的设备指纹**（指纹本体落账号 meta，机型表将来刷新不会改掉存量账号身份）。生效情况经 `credpool_stats.py` → `/api/workspace/metrics.credpool` + Prometheus + ops「🔑 中央凭据池」卡观测：`pool_share` 回答"池有没有在生效"，`proxy_share` 回答"隔离是不是只做了一半"。

### 1d. 链条总闸自测（两个互补脚本）

- `platform/chain_selftest.py`（纯 stdlib）自动发现 `platform/*/` 下所有瘦客户端，在**清空全部总线环境变量**的单机模式下逐个跑降级自测，断言"全部优雅降级、无一抛异常、exit 0"——证明"断总线各自独立运行"。当前 7 条契约（compliance/leadbus/enable/replybus/licensing/observability/credpool）全绿。新契约落地后自动纳入，无需改本文件。
- `platform/chain_live_smoke.py`（2026-07-19 新增）反过来证明"**接上总线，客户端与 chengjie 真实路由字节级兼容**"：起一个只挂 `leadbus_routes.py`/`enable_routes.py`/`replybus_routes.py` 三条新路由的裸 FastAPI 服务（同 chengjie 自己 `tests/test_*_routes.py` 的隔离范式，不碰 Telegram/GPU/生产库），用真实瘦客户端对它发起真实 HTTP 请求。两个脚本互补：一个证明"没有总线也能活"，一个证明"有总线时真的能通"。
- `platform/credpool/credpool_live_smoke.py`（2026-07-27 新增）是 credpool 契约的同款"真实接线"演练，但更进一步：它跑的是**智控真 aiohttp + 真 admin 处理器 + 真落库**（临时数据目录 + 随机端口 + **起服前自证隔离**），覆盖鉴权拒绝/会员定档/卡密绑机/换机降级/独立出口/粘定复用/成败回报/释放/结构化错误/产品归属/真数据日报。**首次运行即揪出 5 个既有缺陷**（跨库定档、30 秒自锁、`sqlite3.Row.get` 三处、未定义的 `ErrorCode.OPERATION_FAILED` 被 48 处引用），全部已修——这就是"替身测试全绿也不等于链路能通"的实证。

---

## 2. 为什么先立契约、不先搬代码

三个引擎（avatarhub/chengjie/huoke）体量大（合计 ~2500 py + 160 tsx）、且当前**在多机在跑**（见 `deploy/` 集群拓扑）。直接把 `provenance/license/metrics` 从引擎里挖出来会牵动运行中的服务，风险高、且本地无法一键回归。故采用：

1. **立契约**（本文件）：先定义接口 + 归属，让新代码只依赖 platform 抽象。
2. **薄适配**：platform 下先放"转发到现有引擎实现"的适配层（thin adapter），产品改为依赖 platform。
3. **逐引擎迁移**：一次搬一个能力（如先 compliance），每步跑该引擎自检/回归（avatarhub `doctor.py` / chengjie `pytest` / `tools/claims_lint`）。
4. **删重复**：三处重复实现（如各自的 gate/metrics）收敛为一份后，删旧留新。

---

## 3. 落地顺序（建议）

`compliance`（无状态、最独立，先行）→ `brand`（收敛为一份令牌）→ `observability`（统一埋点 schema）→ `licensing`（统一计量表 + SKU 门控中间件）→ `identity`（资产总线，最重、最后）。

> 里程碑：`licensing` + `identity` 通了，官网 ↔ SKU ↔ license ↔ 交付 的业务闭环才真正闭合。
