# 无界全域人设总线契约（PERSONA_BUS）

> 版本：v1.2（P5 契约层） · 2026-07-18
> 版本记录：v1.2 2026-07-18 运行时软门控（本地 grant 缓存 + 审计告警，默认 warn 放行）；v1.1 2026-07-18 据实修订（ack 端点对齐实现、软删回收期、缓存失效、多实例数据根）；v1 2026-07-18 初版
> 归属：`platform/identity/`（人设 = 跨产品数字身份，本目录即"跨产品 Profile 总线"的家）
> 配套：`platform/identity/ID_SPEC.md`（`prs` 前缀已注册）、`platform/identity/grant_gate.py`（运行时软门控）、
> `tools/persona_bus/`（引擎侧导出器 + 校验器 + fetch_grants）、
> `website/scripts/ledger-import-personas.mjs`（注册表导入，已交付）
> 适用范围：三引擎（avatarhub / chengjie / huoke）、七产品、集团控制台，凡涉及数字人设的登记、授权与清除，一律走本契约。

---

## ⚠️ 隐私红线（先读这一节，违反即合规事故）

人设的四个槽位中，**脸模与声纹是生物特征数据**，受最严格约束：

- **资产本体绝不出引擎**：脸模图片/视频、声纹参考音/克隆产物、模型权重（RVC `.pth`、DFM `.dfm` 等）以及任何可还原生物特征的数据，**绝不进导出文件、绝不进注册表、绝不进事件流**；
- **指纹只能是摘要**：`fingerprint` 字段只允许是对资产文件字节的 **SHA-256 摘要**（64 位小写十六进制），不允许任何可逆编码；
- **`raw` 不含文件内容**：只放标量元数据（计数、开关、资产 ID/路径引用、质量分）；base64、内嵌文本全文一律禁止；
- 判断口诀（与 `platform/observability/EVENT_CONTRACT.md` 同款）：**"拿到这份导出的人，能不能还原出这个人长什么样、声音是什么样？"** 能，就是事故。

`tools/persona_bus/validate_personas.py` 内置泄漏启发式（base64 长串 / 二进制标记扫描），导入前必须先过校验。

---

## 1. 动机：一个 persona，贯穿全链路

一个 **persona（人设）** = 一套数字身份 = **face / voice / prompt / knowledge 四个槽位**：

| 槽位 | 含义 | 典型资产 |
|---|---|---|
| `face` | 数字形象（脸） | 脸模照片/多角度照片库、整脸模型绑定、风格化脸 |
| `voice` | 数字声音（声纹） | 参考音、克隆产物、变声/TTS 模型绑定 |
| `prompt` | 人格与话术 | 系统提示词、人设卡、开场白/垫话 |
| `knowledge` | 专属知识 | 角色知识库文档、术语库、话术库 |

同一个数字身份应当贯穿集团全链路：**智拓（zhituo）获客 → 智聊（zhiliao）/ 通译（tongyi）承接 → 幻声（huansheng）配音 → 幻影（huanying）开播 → 通传（tongchuan）开会**。今天三套引擎各自建人设、互不知晓，同一个"主播小雅"在获客话术里是一套人格、开播时是另一张脸，客户感知是割裂的；出售/交付时也无法回答"这个客户买走的数字人到底包含哪些资产、授权用在哪几个产品上"。

人设总线解决的就是三件事：

1. **登记**：各引擎把本地人设归一化导出，集团注册表统一登记（元数据 + 指纹，不收本体）；
2. **授权**：persona × product 的使用授权关系（grants）由集团控制台统一管理；
3. **清除**：客户行使删除权时，一键发起全域清除（purge），所有持有该人设资产的引擎删干净并回执。

## 2. 架构与职责边界

```
引擎（资产本体持有方）                     集团（注册表持有方）
┌──────────────────────┐                ┌────────────────────────────┐
│ avatarhub / chengjie │  导出器(只读)   │ website 集团控制台/注册表     │
│ / huoke              │ ──归一化JSON──▶ │  personas（元数据+指纹+状态） │
│  脸模/声纹/权重/知识   │                │  grants（persona×product）   │
│  ＝本体，永不上收      │ ◀──purge指令── │  purges（清除指令+回执）      │
└──────────────────────┘   轮询+ack     └────────────────────────────┘
```

- **注册表只存**：元数据（显示名、槽位存在性、标签、时间）+ **指纹**（sha256 摘要）+ **授权关系**（grants）+ 状态机（active / purge_pending / purged）。
- **本体留引擎**：脸模、声纹、权重、知识库全文永远只在引擎本机（avatarhub 侧还有静置加密：`AVATARHUB_ENCRYPT_PROFILES`）。注册表即使整库泄漏，也拼不回任何一张脸、任何一段声音。
- **内部主键**：注册表行主键按 `ID_SPEC.md` 签发 `prs_*`；引擎侧遗留键降级为 `source_key`，幂等键为 **`(source_system, source_key)`** 唯一索引（与授权台账 `tools/license_ledger` 同一套映射策略，见 ID_SPEC §4.2）。
- **依赖方向**：引擎 → 导出文件 → 注册表（单向数据流）；purge 走引擎**主动轮询**集团 API（引擎不开入站口，集团不直连引擎）。platform 不 import engines 代码。

## 3. 人设导出文件格式（已拍板，两侧同时实现，逐字段冻结）

一次导出 = 一个 JSON 文档（UTF-8）：

```json
{"version":1,"source_system":"avatarhub","exported_at":"<ISO8601>","personas":[
  {"source_key":"<引擎内唯一键>","display_name":"<名>","customer_name":"<或null>",
   "slots":{"face":{"present":true,"fingerprint":"<sha256或null>","ref":"<引擎内资产引用路径/ID，非文件内容>","version":"<或null>"},
            "voice":{"present":false,"fingerprint":null,"ref":null,"version":null},
            "prompt":{"present":true,"fingerprint":"<sha256或null>","ref":"<...>","version":null},
            "knowledge":{"present":false,"fingerprint":null,"ref":null,"version":null}},
   "tags":["..."],"created_at":"<ISO或null>","raw":{"...":"非敏感元数据"}}]}
```

### 3.1 顶层字段

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `version` | ✅ | int | 格式版本，固定 `1`。升版走 §9 变更管理 |
| `source_system` | ✅ | string | 来源引擎枚举：`avatarhub` / `chengjie` / `huoke`（新引擎先登记再导出） |
| `exported_at` | ✅ | string | 导出时刻，ISO 8601（UTC，秒精度即可） |
| `personas` | ✅ | array | 人设记录数组，可为空（空导出是合法结果） |

### 3.2 persona 记录

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `source_key` | ✅ | string | **引擎内稳定唯一键**（同引擎内不重复、跨导出稳定）。avatarhub＝profiles 表主键（角色名）；chengjie＝人设 id；huoke＝persona_key。注册表幂等键 `(source_system, source_key)` |
| `display_name` | ✅ | string | 人设显示名（给控制台人读；可与 source_key 相同） |
| `customer_name` | ✅ | string \| null | 该人设归属的客户名（引擎侧记录了才填，否则 `null`。avatarhub v1 无客户绑定字段 → 恒 `null`，归属关系由控制台在注册表侧维护） |
| `slots` | ✅ | object | 四槽位，键固定 `face` / `voice` / `prompt` / `knowledge`，缺一不可，值结构见 §3.3 |
| `tags` | ✅ | array[string] | 引擎侧标签（如 `active`、`voicepack`、`encrypted`），可为空数组 |
| `created_at` | ✅ | string \| null | 人设创建时刻 ISO 8601（引擎有记录才填，转不出 → `null`） |
| `raw` | ✅ | object | **非敏感**元数据（引擎设置、计数、质量分、使用统计等标量/浅层结构）。禁止文件内容、base64、提示词/知识库全文 |

### 3.3 槽位结构（四槽同构）

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `present` | ✅ | bool | **按资产实际存在判断**（引用了但文件丢失 → `false`；配置了空串 → `false`） |
| `fingerprint` | ✅ | string \| null | 资产主文件字节的 **SHA-256**，64 位小写 hex。`present=false` 时必须 `null`；`present=true` 但算不出（如引擎静置加密未解）→ `null` |
| `ref` | ✅ | string \| null | 引擎内资产引用（**相对路径或资产 ID，绝非文件内容**）。`present=true` 时必填非空；`present=false` 时 `null` |
| `version` | ✅ | string \| null | 资产版本号（引擎有版本概念才填，v1 各引擎均可 `null`） |

约定：

- 收方（注册表导入）遇到**未知的多余字段保留不报错**（向前兼容，与 EVENT_CONTRACT §2 同策）；四个必填键缺失或类型错 → 整文件拒收；
- 多资产槽位（如脸模主照 + 照片库）：`fingerprint`/`ref` 取**主资产**，其余进 `raw` 计数；
- 引擎行被静置加密无法解出时：整条 persona 仍导出（注册表需要知道它存在），四槽 `present=false` + `tags` 含 `encrypted` + `raw.encrypted=true`，语义为"存在但槽位未知"。

## 4. 授权（grants）语义

**grant = persona × product 的使用授权**，一行 grant 表示"允许产品 P 使用人设 A"。

- **管理面**：grants 只在**集团控制台**创建/吊销（引擎与产品无权自行授权），控制台操作留审计事件（`platform.persona.grant_created` / `platform.persona.grant_revoked`，进 EVENT_CONTRACT 事件流）；
- **product 枚举**：`zhituo` / `zhiliao` / `tongyi` / `tongchuan` / `huansheng` / `huanying` / `huanyan`（与 EVENT_CONTRACT `product_id` 同源，`website`/`platform` 不作为被授权方）；
- **执行面（v1 现实口径）**：grants 在 v1 是**登记与对账层**——回答"这个客户的数字人授权给了哪几款产品"，供交付清单、合同对账、清除范围计算使用。产品侧**硬拒绝**加载仍是后续阶段；自 v1.2 起提供**运行时软门控**（§4.1：本地缓存 + 默认 warn 放行 + 可选 enforce），不阻塞业务、可回滚；
- **与授权台账的关系**：license（`tools/license_ledger`，产品使用权）和 grant（人设使用权）是两条正交的授权轴——客户可以有产品授权但把某个 persona 只授权给其中两款产品用；
- 生命周期：`granted → revoked`；persona 进入 `purge_pending/purged` 时其全部 grants 自动失效（清除优先于授权）。

### 4.1 运行时软门控（v1.2）

比强制拒绝更安全的可回滚方案：**先审计告警，后按需强制**。无 grant / 缓存缺失时默认放行并打警告；仅当运维显式打开 enforce 才拒绝加载。

#### 语义

| 模式 | 如何开启 | 无有效 grant | 缓存缺失 / 损坏 / 系统不匹配 | 缓存过期（默认 >24h） |
|---|---|---|---|---|
| **warn**（默认） | 不设 env，或 `PERSONA_GRANT_ENFORCE` 非真值 | **放行** + `[PERSONA_GRANT_AUDIT]` 警告 | **放行** + 警告（断网不挡业务） | 仍用缓存判定，reason 带 `stale:` 前缀并警告 |
| **enforce** | `PERSONA_GRANT_ENFORCE=1`（或 `true`/`yes`/`on`），或 `check(..., enforce=True)` | **拒绝**（`allowed=false`） | **仍放行**（缺证据 ≠ 无授权，避免误拦） | 仍用缓存判定；无 grant 则拒绝 |

有效 grant = 缓存中存在 `(source_key, product_id)` 且 `status=granted`（`revoked` 不计）。

#### 集团侧只读导出 API

```
GET /api/sync/personas/grants?system=<avatarhub|chengjie|huoke>
Authorization: Bearer <EVENT_INGEST_KEY>
→ 200 {"ok":true,"system":"<engine>","count":<n>,
       "grants":[{"source_key":"<键>","product_id":"huanying","status":"granted|revoked"}]}
```

- 只返回该 `source_system` 下 **status=active** 的 persona 的 grants（含已撤销行，供对账；门控只认 `granted`）；
- 响应不含客户数据；鉴权与 `/api/sync/personas/purges` 同款（未配置 key → 503，不匹配 → 401）；
- 实现：`website/app/api/sync/personas/grants/route.ts` + `website/lib/personas.ts::listActiveGrantsForSystem`。

#### 本地缓存格式与路径

```json
{"version":1,"fetched_at":"<ISO8601>","system":"avatarhub",
 "grants":[{"source_key":"<键>","product_id":"huanying","status":"granted"}]}
```

| 约定 | 路径 |
|---|---|
| 拉取写出 | `tools/persona_bus/fetch_grants.py --system <engine> --out <path>` |
| 引擎缺省缓存 | `<引擎根>/data/persona_grants_cache.json` |
| 覆盖 | env `PERSONA_GRANT_CACHE`（绝对或相对路径） |
| staging 亦可 | `data/persona_bus_out/<engine>_grants.json`（与 export staging 同级，已 gitignore） |

#### 引擎侧组件

| 组件 | 路径 | 职责 |
|---|---|---|
| 门控核心 | `platform/identity/grant_gate.py` | `load_cache` / `check`；`--selftest` |
| 拉取器 | `tools/persona_bus/fetch_grants.py` | HTTP 拉清单写缓存；失败非 0，可重试 |
| avatarhub 薄适配 | `engines/avatarhub/grant_check.py` | 包装门控 + 文档接线指南（**不改热路径**） |
| chengjie 薄适配 | `engines/chengjie/scripts/grant_check.py` | 同上 |
| huoke 薄适配 | `engines/huoke/src/grant_check.py` | 同上 |

加载人设调用点接线（约 2 行，详见各 `grant_check.py` 文件头）：

```python
from grant_check import check_persona_grant
r = check_persona_grant(source_key, product_id)  # warn 下恒 allowed=True
if not r["allowed"]:
    raise PermissionError(r["reason"])  # 仅 enforce 命中
```

#### 与 cron 的关系

建议挂在 `deploy/cron` **export 成功之后**（人设已导入注册表、控制台授权已落库时，拉取才有意义）：

```powershell
# 示例：export 三段式完成后拉 grants 写引擎本地缓存
python tools/persona_bus/fetch_grants.py --system avatarhub `
  --out engines/avatarhub/data/persona_grants_cache.json
```

拉取失败勿阻断业务进程：旧缓存继续离线使用；门控默认 warn。正式打开 `PERSONA_GRANT_ENFORCE=1` 前，先观察一段时间 AUDIT 日志里的 `no_grant` 告警量。

## 5. 全域清除协议（purge，删除权的工程落地）

客户行使删除权（或合同终止）时，其数字身份必须在**所有持有资产的引擎**上删干净。流程状态机：

```
console 发起 purge
  → 注册表 personas.status = purge_pending，并按引擎生成清除指令（purges 表，一系统一行）
  → 各引擎周期轮询   GET  /api/sync/personas/purges?system=<engine>     （Bearer EVENT_INGEST_KEY）
  → 引擎本地删除资产（文件 + 缓存 + 衍生物，见 §5.3）
  → 引擎回执          POST /api/sync/personas/purges（同一 URL）          （Bearer EVENT_INGEST_KEY）
  → 全部目标引擎 ack 后：personas.status = purged，指纹随之从注册表删除（只留墓碑与审计）
```

### 5.1 API 契约（website 侧已上线实现，`website/app/api/sync/personas/purges/route.ts`，逐字对齐）

**拉取待办**（引擎 → 集团，轮询）：

```
GET /api/sync/personas/purges?system=<engine>
Authorization: Bearer <EVENT_INGEST_KEY>
→ 200 {"ok":true,"system":"<engine>","count":<n>,
       "purges":[{"purge_id":<int>,"persona_id":"prs_*","source_system":"avatarhub",
                  "source_key":"<键>","requested_at":"<ISO8601或null>",
                  "slots":{"face":true,"voice":false,"prompt":true,"knowledge":false}}]}
```

- 只返回该 `system` 名下**未 ack** 的指令（已 ack 的不再出现）；无待办 → `count=0` + `purges:[]`；
- `purge_id` 是**正整数**（persona_purges 表自增主键，不是字符串）；`slots` 为注册表登记的四槽位布尔，**仅供参考**（见 §5.3-2 全域清除口径）；响应不含客户数据（customer_id / display_name / 联系方式一概不带）；
- 缺 `system` 参数 → 400；鉴权复用 EVENT_CONTRACT 传输层的机器对机器密钥 `EVENT_INGEST_KEY`（一台厂商机一把 M2M 钥匙，与 `/console` 人类口令体系完全独立；密钥未配置 → 503，同 `/api/collect` 语义）。该 key 在本路由只能拉取指令与回执，不能读人设列表、不能发起清除（发起权只在 `/console`）。

**回执**（引擎 → 集团，**POST 同一 URL**，幂等）：

```
POST /api/sync/personas/purges
Authorization: Bearer <EVENT_INGEST_KEY>
body: {"purge_id":<int>,"detail":{...}}
→ 200 {"ok":true,"purge_id":<int>,"persona_id":"prs_*","target_system":"<engine>",
       "already":false,"all_acked":true,"persona_status":"purged"}
```

请求字段：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `purge_id` | ✅ | int | 待回执指令 id（非正整数 → 400；注册表不认识 → 404） |
| `detail` | 可选 | object \| string | 删除明细，建议必带：`deleted`（实际删除项）/ `missing`（找不到＝早已不在，幂等视同已删）/ `errors`（按本契约恒为空数组，见下）+ 计数摘要。只放**相对路径/资产 ID 引用**，不放内容、不放指纹全文。多余字段（如 `system`/`ok`/`completed_at`）服务端不消费、不报错 |

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | 恒 `true`（失败走 400/401/404/500 错误响应，无 `ok=false` 半失败回执） |
| `purge_id` | int | 回显指令 id |
| `persona_id` | string | 该指令对应的 `prs_*` 主键 |
| `target_system` | string | 该指令的目标引擎 |
| `already` | bool | `true` = 该指令此前已 ack 过（幂等重放；**首次回执保留**，本次 detail 不覆盖） |
| `all_acked` | bool | 该 persona 名下全部指令是否都已 ack（`true` 时集团侧已自动置 `status=purged`） |
| `persona_status` | string \| null | 回执后的 persona 状态（收口时为 `purged`） |

- **没有"半失败回执"**：服务端一收到合法 ack 即关单，不看请求体里的任何成败标志。因此**引擎删除失败（哪怕只失败一项）就绝不 POST**——指令保留待办，已删项不回滚，下轮幂等重试（v1 草案的 `ok=false` 保留待办语义已废弃）；
- 同一 `purge_id` 重复 ack 幂等：返回 `already=true`，首次回执的 `detail` 与 `acked_at` 保留（后到不覆盖）。

### 5.2 指令生成范围

- 指令目标（已上线口径，`website/lib/personas.ts::computePurgeTargets`）= **`source_system`（资产本体持有方）∪ 全部 grants 推导的承载引擎**，去重后一系统一行。product → engine 映射：`huansheng`/`huanying`/`huanyan`/`tongchuan` → avatarhub，`zhiliao`/`tongyi` → chengjie，`zhituo` → huoke；
- **已撤销的 grants 同样计入目标**——撤销授权不等于引擎侧已删资产，凡授权过的引擎都要收到清除指令；
- 若该 persona 曾跨机分发（如 avatarhub 配置包 `.ahpkg` 导入到另一台授权机），控制台可为相应 system/机器**手动追加**指令行（预留能力，v1.1 控制台尚未提供入口）；
- persona 一旦 `purge_pending`：其 grants 全部失效、后续导入（§7）不得把该 `(source_system, source_key)` 复活为 active。

### 5.3 引擎实现者的义务（接入即承诺）

1. **轮询**：周期 ≤ 1 小时（建议 5–15 分钟，可挂在既有守护/自检任务上）；无待办时这是一次零成本 GET；
2. **删除范围 = 资产文件 + 缓存 + 衍生物**，一个都不留。指令里的 `slots` 布尔只是注册表登记时的标注、**仅供参考**——全域清除 = 该 `(source_system, source_key)` 的**全部本机资产**（含注册表不知道的缓存与衍生物），不按槽位挑食。以 avatarhub 为例（其余引擎按 §6 映射表对应资产类推）：
   - 文件本体：角色行内嵌的脸/声 base64（删角色行）、`voice_clones/` 克隆产物、`alltalk_tts/voices/` 中该角色专属参考音、每角色绑定的 DFM/RVC 私有权重、`声音包/` 中该角色的源音频与文本；
   - 缓存：试听缓存（`voice_previews/`）、缩略图、开场白/垫话预合成缓存、预计算口型/待机视频底、OG 分享封面；
   - 衍生物：风格化脸、身体照（`data/body_photo/`）、知识库文档（`avatar_kb.db` 中 `meta.profile` 归属行）、质量基线/趋势数据、**软删回收站（`voice_clones/_trash/`）中的同源文件**；
   - 备份：承诺范围内的本机备份（如黄金包快照）一并删除；异地备份按轮转策略到期消失，需在交付文档中向客户说明轮转周期；
3. **幂等**：找不到的项不算失败——记入 `detail.missing`，照常回执；重复收到同一指令重复执行无副作用；
4. **部分失败**：删了一半出错 → 本轮**不回执**（§5.1：服务端一收到 ack 即关单，无半失败回执），**已删的不回滚**，失败项留执行器日志/state，下轮幂等重试剩余项；删除完成但**回执未达**（网络中断/5xx）时绝不重复破坏性删除——把 detail 暂存本地下轮补发（如 avatarhub 的 state `pending_acks`），或下轮幂等重扫后回执（已删项自然落入 `missing`），两法均可，不得谎报状态；
5. **防复活**：删除完成后，引擎侧导出器不得再输出该 source_key（资产没了自然 `present=false`，行没了自然不出现）；引擎不得从回收站/缓存自动恢复已清除资产；
6. **审计**：删除动作经现有 telemetry 发射 `<product|platform>.persona.purged`（EVENT_CONTRACT / events_registry；props 仅 persona_id/source_key/计数，fail-silent）；
7. **软删除回收缓冲**：清除不直接物理删除，先移入引擎本地回收缓冲 `purged_trash/<日期>/`（连同被删数据行的 JSON 快照与本次 manifest 清单；位置引擎自定——avatarhub＝`secrets/purged_trash/`、chengjie＝各数据根 `config/purged_trash/`、huoke＝`data/purged_trash/`——但必须被 .gitignore 覆盖，**绝不入 git**），供人工复核与误删挽回。回收期建议 **≤ 30 天**，到期物理删除由运维定期清空 trash，这是客户删除权的最终兑现点；**对客户承诺的删除时限 = 回执时限 + 回收期上限**，须在交付文档中如此表述（异地备份轮转周期另行说明，见 §5.3-2 备份条）。trash 内资产受 §5.3-5 约束：只许人工恢复决策，执行器与引擎绝不自动恢复；
8. **运行时缓存失效**：磁盘资产删净不等于清除完成——引擎**进程内存中的人设副本**（已加载的人设卡、参考音句柄、预热/预合成缓存）在重启或触发重载前仍然存活。"清除后重启引擎进程、或提供运行时失效钩子（通知引擎重载人设清单）"是引擎义务，交付/运维文档须写明本引擎采用哪种方式；
9. **多实例数据根**：同一引擎多实例部署时（如 chengjie 智聊/通译双实例，人设资产落在各实例自己的数据根下），集团队列按引擎只有一份指令——`system=<engine>` 是**引擎枚举不是产品枚举**。purge 执行器全机只跑一份，但必须**遍历全部实例数据根**，把同一 `source_key` 在每个根下都删净（删除或确认缺失）才可回执；只删一个实例就 ack = 另一实例成漏网，违反本节义务（约束出处与运维口径：`deploy/instances/migrate_117_runbook.md` §4.4；chengjie 执行器以 `--data-roots`/`CHENGJIE_DATA_ROOTS` 承接，detail 带分根摘要）。

### 5.4 注册表侧的收尾义务

该 persona 名下全部指令 ack → `status=purged`（`ackPurge` 收口时自动置位并写 `persona.purged` 审计），同时**从注册表删除该 persona 的全部 `fingerprint`**（生物特征资产的哈希在最严格口径下仍可能构成个人数据），墓碑行只留：`prs_*` 主键、`(source_system, source_key)`、状态、时间线、ack 审计。`purged` 是终态，不可逆转回 active。

#### §清除后墓碑字段

`ackPurge` 收口将 `status→purged` 时必须 scrub `slots_detail` 内各槽的 `fingerprint`/`ref`（实现：`website/lib/personas.ts` → `scrubSlotsDetailFingerprints`）；墓碑可保留 `display_name`、`source_key`、`status` 与槽位壳/`version` 供审计，**不得**再留生物特征哈希或资产路径引用。

## 6. 各引擎 source_key 与槽位映射（已交付口径）

| 引擎 | source_key（引擎内唯一键） | face | voice | prompt | knowledge |
|---|---|---|---|---|---|
| **avatarhub**（脸/声/角色库） | `avatar_profiles.db` → `profiles` 表主键＝角色名 | 角色行 `face_b64`（主照；照片库/风格化脸/DFM 绑定进 `raw` 计数） | `voice_name` → `alltalk_tts/voices/<名>.wav`，或行内 `voice_b64`（＝`voice_clones/` 同字节）；RVC/Fish 多段参考进 `raw` 计数 | 角色行 `system_prompt`（开场白/垫话进 `raw` 计数） | `avatar_kb.db` → `kb_docs` 中 `meta.profile=<角色名>` 的文档集；或 `声音包/<角色名>.txt` 源文件 |
| **chengjie**（AI 人设，智聊/通译共用引擎） | `config/profiles_runtime.yaml` 人设 id（runtime 层最高优先，缺失回退 `config/personas.yaml`） | `persona_media.db` 中该 persona 最早 enabled 相册照（行内已存 sha256；多数 persona 此槽缺席。`appearance` 外貌锚点是文本配置 → `raw.has_appearance_anchor`） | `voice_profile.reference_audio_path` 显式指向优先，缺配置自动发现 `config/voice_refs/<id>.<ext>`（wav/mp3/m4a/ogg/flac）；预渲染语音产物 `assets/voices/<id>/` 进 `raw` 计数 | 人设卡＝`profiles_runtime.yaml` 该 persona 原文块（指纹＝块字节 CRLF→LF 归一 sha256） | `config/prerender_lines/<id>.txt` 专属台词/话术库（引擎级知识库/术语库/`translation_memory.db` 是共享资产、无 persona 归属，不进槽位） |
| **huoke**（养号人设，两个家族） | fb_target 家族＝`fb_target_personas.persona_key`；studio 内容人设家族＝`config/personas.yaml` 键加前缀 `studio:` | 恒缺席（`present=false`） | 恒缺席（`present=false`） | fb_target：打招呼话术包 `chat_messages.yaml#countries.<cc>`（无国家块回退顶层 legacy 话术节）；studio：`personas.yaml#personas.<key>` 人设定义块 | fb_target：画像定义块 `fb_target_personas.yaml#personas.<key>`（L1 规则/兴趣/关键词/match_criteria；`persona_knowledge.yaml` 词表只进 `raw` 计数）；studio：恒缺席 |

约定：导出器一引擎一个（`tools/persona_bus/export_<engine>_personas.py`），三引擎均已交付，本表即交付口径（细化处以导出器文件头为准）；`ref` 一律写**相对引擎根的路径**或 `库文件#行键#字段` 形式的稳定 ID；文本块资产（YAML 原文块/台词文本）的 fingerprint＝按导出器约定归一后的**块字节** sha256（chengjie CRLF→LF 归一；huoke 行以 LF 连接、去尾空行），清除执行器与导出器同一约定，注册表可跨侧对账。

## 7. 与注册表导入的衔接

- 导入脚本：`website/scripts/ledger-import-personas.mjs`（已交付），输入即本契约 §3 文档；
- 幂等：按 `(source_system, source_key)` upsert，重复导入不重登；首见签发 `prs_*` 内部主键（ID_SPEC §4.2 三元组映射）；
- 导入顺序：`export → validate_personas.py → import`，**校验不过不导入**；
- 状态规则：新键 → `active`；已 `purge_pending` / `purged` 的键再次出现 → **不复活**，标记异常供人工核查（引擎侧防复活义务见 §5.3-5）；
- 指纹变化（同键不同 fingerprint）→ 正常更新（人设换了脸/声），可选发 `platform.persona.updated` 审计事件。

## 8. 隐私与合规红线（汇总）

1. **生物特征本体不出引擎**（本文最高铁律，见文首红线节）；导出文件里最敏感的东西只能是显示名与 sha256 指纹；
2. **删除权**：客户删除请求 → §5 全域清除协议是唯一正规通道，任何"只删注册表不删引擎"或反之的半吊子删除都是违约；
3. **导出文件密级**：含显示名/客户名/指纹，按内部经营数据处理，不入 git、不外发（与 `tools/license_ledger` 导出物同级）；
4. **与 platform/compliance（C2PA/水印）的关系一句话**：compliance 契约证明"**这段产物**出自谁之手"（合成溯源），人设总线管"**这个数字身份**有哪些资产、授权给谁、如何被清除"（身份资产治理）——前者作用于产物，后者作用于身份，二者以 `prs_*` ID 与资产指纹互相印证。

## 9. 变更管理

- 本格式 `version=1` 为冻结契约：新增可选字段（收方保留未知字段）不升版；改动必填字段/槽位键名/指纹算法视同 v2，须双侧（导出器 + 导入脚本）同一批次落地并全量评估存量注册表兼容性；
- 新引擎接入：§3.1 `source_system` 枚举 + §6 映射表 + 新导出器，三处同一提交；
- purge API 路径与鉴权方式变更须同步修改本文件 §5.1 与全部引擎轮询实现。
