# 无界全域人设总线契约（PERSONA_BUS）

> 版本：v1（P5 契约层） · 2026-07-18
> 归属：`platform/identity/`（人设 = 跨产品数字身份，本目录即"跨产品 Profile 总线"的家）
> 配套：`platform/identity/ID_SPEC.md`（`prs` 前缀已注册）、`tools/persona_bus/`（引擎侧导出器 + 校验器）、
> `website/scripts/ledger-import-personas.mjs`（注册表导入，website 侧并行开发）
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
- **执行面（v1 现实口径）**：grants 在 v1 是**登记与对账层**——回答"这个客户的数字人授权给了哪几款产品"，供交付清单、合同对账、清除范围计算使用。产品侧运行时强制（无 grant 拒绝加载人设资产）是后续阶段，接入方式届时另立契约，不阻塞本期登记；
- **与授权台账的关系**：license（`tools/license_ledger`，产品使用权）和 grant（人设使用权）是两条正交的授权轴——客户可以有产品授权但把某个 persona 只授权给其中两款产品用；
- 生命周期：`granted → revoked`；persona 进入 `purge_pending/purged` 时其全部 grants 自动失效（清除优先于授权）。

## 5. 全域清除协议（purge，删除权的工程落地）

客户行使删除权（或合同终止）时，其数字身份必须在**所有持有资产的引擎**上删干净。流程状态机：

```
console 发起 purge
  → 注册表 personas.status = purge_pending，并按引擎生成清除指令（purges 表，一系统一行）
  → 各引擎周期轮询   GET  /api/sync/personas/purges?system=<engine>     （Bearer EVENT_INGEST_KEY）
  → 引擎本地删除资产（文件 + 缓存 + 衍生物，见 §5.3）
  → 引擎回执          POST /api/sync/personas/purges/ack                 （Bearer EVENT_INGEST_KEY）
  → 全部目标引擎 ack ok 后：personas.status = purged，指纹随之从注册表删除（只留墓碑与审计）
```

### 5.1 API 契约（website 侧实现）

**拉取待办**（引擎 → 集团，轮询）：

```
GET /api/sync/personas/purges?system=<engine>
Authorization: Bearer <EVENT_INGEST_KEY>
→ 200 {"purges":[{"purge_id":"<id>","source_system":"avatarhub","source_key":"<键>",
                   "requested_at":"<ISO8601>","reason":"customer_erasure"}]}
```

- 只返回该 `system` 名下**未完成**的指令（已 ack ok 的不再出现）；无待办 → `{"purges":[]}`；
- 鉴权复用 EVENT_CONTRACT 传输层的机器对机器密钥 `EVENT_INGEST_KEY`（一台厂商机一把 M2M 钥匙，与 `/console` 人类口令体系完全独立；密钥未配置 → 503，同 `/api/collect` 语义）。

**回执**（引擎 → 集团，幂等）：

```
POST /api/sync/personas/purges/ack
Authorization: Bearer <EVENT_INGEST_KEY>
body: {"purge_id":"<id>","system":"avatarhub","ok":true,
       "completed_at":"<ISO8601>",
       "detail":{"deleted":["<相对路径或资产ID>",...],"missing":[...],"errors":[...]}}
```

- `ok=true`：该引擎完成 → 该指令关闭；`ok=false`：保留待办，引擎修复后下轮重试；
- **ack 必须带 `detail`**：`deleted`（实际删除项）/ `missing`（找不到＝早已不在，幂等视同已删）/ `errors`（失败项+原因）。detail 里只放**路径/资产 ID 引用**，不放内容；
- 同一 `purge_id` 重复 ack 幂等（后到覆盖，以最新为准）。

### 5.2 指令生成范围

- v1 默认按 **`source_system`**（资产本体持有方）生成一行指令；
- 若该 persona 曾跨机分发（如 avatarhub 配置包 `.ahpkg` 导入到另一台授权机），控制台可为相应 system/机器**手动追加**指令行；
- persona 一旦 `purge_pending`：其 grants 全部失效、后续导入（§7）不得把该 `(source_system, source_key)` 复活为 active。

### 5.3 引擎实现者的义务（接入即承诺）

1. **轮询**：周期 ≤ 1 小时（建议 5–15 分钟，可挂在既有守护/自检任务上）；无待办时这是一次零成本 GET；
2. **删除范围 = 资产文件 + 缓存 + 衍生物**，一个都不留。以 avatarhub 为例（其余引擎按 §6 映射表对应资产类推）：
   - 文件本体：角色行内嵌的脸/声 base64（删角色行）、`voice_clones/` 克隆产物、`alltalk_tts/voices/` 中该角色专属参考音、每角色绑定的 DFM/RVC 私有权重、`声音包/` 中该角色的源音频与文本；
   - 缓存：试听缓存（`voice_previews/`）、缩略图、开场白/垫话预合成缓存、预计算口型/待机视频底、OG 分享封面；
   - 衍生物：风格化脸、身体照（`data/body_photo/`）、知识库文档（`avatar_kb.db` 中 `meta.profile` 归属行）、质量基线/趋势数据、**软删回收站（`voice_clones/_trash/`）中的同源文件**；
   - 备份：承诺范围内的本机备份（如黄金包快照）一并删除；异地备份按轮转策略到期消失，需在交付文档中向客户说明轮转周期；
3. **幂等**：找不到的项不算失败——记入 `detail.missing`，`ok=true` 照常回执；重复收到同一指令重复执行无副作用；
4. **部分失败**：删了一半出错 → `ok=false` + `errors` 逐项说明，**已删的不回滚**，下轮重试剩余项；
5. **防复活**：删除完成后，引擎侧导出器不得再输出该 source_key（资产没了自然 `present=false`，行没了自然不出现）；引擎不得从回收站/缓存自动恢复已清除资产；
6. **审计**：删除动作建议发射 `<engine>.persona.purged` 事件（EVENT_CONTRACT）留全域审计线。

### 5.4 注册表侧的收尾义务

全部 ack ok → `status=purged`，同时**从注册表删除该 persona 的全部 `fingerprint`**（生物特征资产的哈希在最严格口径下仍可能构成个人数据），墓碑行只留：`prs_*` 主键、`(source_system, source_key)`、状态、时间线、ack 审计。`purged` 是终态，不可逆转回 active。

## 6. 各引擎 source_key 与槽位映射建议

| 引擎 | source_key（引擎内唯一键） | face | voice | prompt | knowledge |
|---|---|---|---|---|---|
| **avatarhub**（脸/声/角色库） | `avatar_profiles.db` → `profiles` 表主键＝角色名 | 角色行 `face_b64`（主照；照片库/风格化脸/DFM 绑定进 `raw` 计数） | `voice_name` → `alltalk_tts/voices/<名>.wav`，或行内 `voice_b64`（＝`voice_clones/` 同字节）；RVC/Fish 多段参考进 `raw` 计数 | 角色行 `system_prompt`（开场白/垫话进 `raw` 计数） | `avatar_kb.db` → `kb_docs` 中 `meta.profile=<角色名>` 的文档集；或 `声音包/<角色名>.txt` 源文件 |
| **chengjie**（AI 人设/术语库） | `config/profiles_runtime.yaml` 人设 id（persona id） | 外貌锚点/自拍资产（`appearance` 锚点与 companion_selfie 产物） | `voice_profile.reference_audio_path` → `config/voice_refs/`（预渲染台词 `config/prerender_lines/<id>.txt` 进 raw） | 人设卡（persona 定义/系统提示） | 术语库/知识库（RAG 文档、话术库） |
| **huoke**（养号人设） | `fb_target_personas.persona_key`（画像/养号人设键） | 通常缺席（`present=false`） | 通常缺席 | 画像定义（`config/fb_target_personas.yaml` 行） | 关键词/规则库 |

约定：导出器一引擎一个（`tools/persona_bus/export_<engine>_personas.py`），本期交付 avatarhub，chengjie / huoke 按本表后续跟进；`ref` 一律写**相对引擎根的路径**或 `库文件#行键#字段` 形式的稳定 ID。

## 7. 与注册表导入的衔接

- 导入脚本：`website/scripts/ledger-import-personas.mjs`（website 侧同事开发），输入即本契约 §3 文档；
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
