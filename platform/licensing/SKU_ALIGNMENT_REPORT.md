# SKU 对齐报告（官网报价 ↔ 全域 SKU 注册表）

> P0 任务「SKU 对齐」产出。生成日期：2026-07-18。
>
> 对比双方：
> - **注册表侧**：`platform/licensing/sku_registry.json`（generated 2026-07-15，7 产品 / 21 SKU / 5 个 TBD；由 `tools/build_sku_registry.py` 从 `products/*/product.yaml` 汇总生成，消费入口 `platform/licensing/sku_registry.py`）。
> - **官网侧**：`website/lib/pricing.ts`（schema.org JSON-LD 报价唯一真相源，共 3 个导出数组 / 7 个 offer；唯一引用方为 `website/app/layout.tsx` 的结构化数据输出）。
>   注意 `website/lib/avatarhub-pricing.ts` 是客户端订阅/授权档位（trial/starter/…/flagship），与本次对齐的服务类 SKU 是并存的另一套体系，**不在本报告范围**。
>
> 本次已做：给 `PriceOffer` 接口新增可选字段 `skuId?: string` 并为 6 个可对应 offer 回填；新增只读反查函数 `findOfferBySkuId(skuId)`。**未改动任何 price/currency/name/description 现值，未改动 registry / product.yaml。**

---

## 1. 对齐结果总表

### 1.1 官网 offer → registry（7 个 offer，命中 6 / 未命中 1）

| pricing.ts offer id | skuId（已回填） | 官网价格/币种/周期 | registry 价格/币种/周期 | 状态 |
|---|---|---|---|---|
| `realtime-basic` | `facex-live-deploy` | 980 USDT / one-time | from 980 USD / one-time | 价格不一致（币种 USDT↔USD；官网定值 980 vs registry 起价 "from 980"） |
| `realtime-creator` | —（留空） | 2580 USDT / one-time | 无对应 SKU | **registry 缺失** |
| `autochat-team` | `chatx-team` | 198 USDT / month | 198 USD / month | 价格不一致（仅币种 USDT↔USD，数值与周期一致） |
| `autochat-flagship` | `chatx-flagship` | 598 USDT / month | 598 USD / month | 价格不一致（仅币种 USDT↔USD，数值与周期一致） |
| `translate-charpack` | `lingox-charpack` | 39 USD / one-time | TBD USD / one-time | 价格不一致（registry 未定价 TBD；product.yaml 注释建议价 39 与官网一致） |
| `translate-team` | `lingox-team` | 59 USD / month | TBD USD / month | 价格不一致（registry 未定价 TBD；建议价 59 与官网一致） |
| `translate-pro` | `lingox-pro` | 99 USD / month | TBD USD / month | 价格不一致（registry 未定价 TBD；建议价 99 与官网一致） |

说明：
- 7 个 offer 中**没有一对是完全一致的"已对齐"**——6 对能建立关联键，但全部带币种或定价状态差异（详见 §2）；1 个在 registry 无对应。
- `realtime-basic ↔ facex-live-deploy` 的判定依据：两侧同为"实时换脸部署 / one-time / 980 基价"，且 registry 该 SKU 的 note 明确写"定制交付(见幻影 LiveX 旗舰)"，即指向官网 #realtime 旗舰部署服务（`content.ts` faceswap 段"实时换脸部署 980 起，见旗舰"同一口径）。口径差异（官网描述为"换脸**或**换声任选其一"，registry SKU 挂在幻颜 facex 名下且仅表述换脸）已记录，不影响关联键成立。

### 1.2 registry → 官网（21 个 SKU，命中 6 / 官网缺失 15）

| sku_id | 产品 | registry 价格/周期 | visibility | 状态 |
|---|---|---|---|---|
| `voicex-starter` | 幻声 VoiceX | 18 USD / month | public | 官网缺失（content.ts 有展示价"18 / 月"，未进 pricing.ts JSON-LD） |
| `voicex-std` | 幻声 VoiceX | 78 USD / month | public | 官网缺失（同上，"78 / 月"） |
| `voicex-pro` | 幻声 VoiceX | 198 USD / month | public | 官网缺失（同上，"198 / 月"） |
| `voicex-usage` | 幻声 VoiceX | 10 USD / per-10k-chars | public | 官网缺失（同上；且单位超出 PriceUnit 类型，见 §4.2） |
| `facex-image` | 幻颜 FaceX | 1 USD / per-image | gated | 官网缺失（content.ts 有"1 / 张"；gated 合规准入线） |
| `facex-video` | 幻颜 FaceX | 4 USD / per-min | gated | 官网缺失（content.ts 有"4 / 分钟"；gated） |
| `facex-live-deploy` | 幻颜 FaceX | from 980 USD / one-time | gated | **已建关联** → `realtime-basic`（见 §1.1） |
| `facex-avatar` | 幻颜 FaceX | from 398 USD / one-time | gated | 官网缺失（content.ts 有"398 起"；gated） |
| `livex-avatar-sub` | 幻影 LiveX | from 198 USD / month | mixed | 官网缺失（content.ts 有"198 起 / 月"） |
| `livex-avatar-buy` | 幻影 LiveX | 798 USD / one-time | mixed | 官网缺失（content.ts 有"798 形象买断"） |
| `livex-dub-min` | 幻影 LiveX | 6 USD / per-min | mixed | 官网缺失（content.ts 有"6 / 分钟"） |
| `livex-dub-matrix` | 幻影 LiveX | 398 USD / month | mixed | 官网缺失（content.ts 有"398 / 月"） |
| `voxx-meeting` | 通传 VoxX | TBD / per-session | public | 官网缺失（未定价，content.ts 只显示"按需报价"） |
| `voxx-selfhost` | 通传 VoxX | TBD / one-time | public | 官网缺失（未定价，同上） |
| `lingox-charpack` | 通译 LingoX | TBD / one-time | public | **已建关联** → `translate-charpack` |
| `lingox-team` | 通译 LingoX | TBD / month | public | **已建关联** → `translate-team` |
| `lingox-pro` | 通译 LingoX | TBD / month | public | **已建关联** → `translate-pro` |
| `chatx-entry` | 智聊 ChatX | 58 USD / month | mixed | 官网缺失（content.ts plans 有"入门 58/月"，pricing.ts autochatOffers 未收录） |
| `chatx-team` | 智聊 ChatX | 198 USD / month | mixed | **已建关联** → `autochat-team` |
| `chatx-flagship` | 智聊 ChatX | 598 USD / month | mixed | **已建关联** → `autochat-flagship` |
| `reachx-deploy` | 智拓 ReachX | 按规模报价 / one-time | gated | 官网缺失（高风险线，product.yaml 明确"不进公开货架/sitemap"，缺席合理） |

---

## 2. 价格 / 币种 / 周期不一致清单（一律只记录，本次未修改任何数值）

| # | 关联对 | 不一致项 | 两侧现值 | 备注 |
|---|---|---|---|---|
| 1 | `realtime-basic` ↔ `facex-live-deploy` | 币种 + 价格表达 | 官网 980 **USDT** 定值 ↔ registry "from 980" **USD** 起价 | 数值基点一致；USDT 为 legacy 结算轨道（pricing.ts 接口注释自述），registry 统一记 USD |
| 2 | `autochat-team` ↔ `chatx-team` | 币种 | 官网 **USDT** ↔ registry **USD**（均 198/月） | products/zhiliao/product.yaml 口径为"单位 USD、结算支持 USDT（dual）"，官网把结算币当报价币 |
| 3 | `autochat-flagship` ↔ `chatx-flagship` | 币种 | 官网 **USDT** ↔ registry **USD**（均 598/月） | 同上 |
| 4 | `translate-charpack` ↔ `lingox-charpack` | 定价状态 | 官网 **39 USD** 实卖 ↔ registry **TBD** | product.yaml 注释"建议 39 USD / 150万字符"与官网一致，待拍板回填 |
| 5 | `translate-team` ↔ `lingox-team` | 定价状态 | 官网 **59 USD/月** ↔ registry **TBD** | 注释建议 59，与官网一致 |
| 6 | `translate-pro` ↔ `lingox-pro` | 定价状态 | 官网 **99 USD/月** ↔ registry **TBD** | 注释建议 99，与官网一致 |

周期（unit）方面：6 对关联的 one-time/month 全部一致，无周期冲突。

另附两条口径观察（非本次对齐对象，仅记录）：
- schema.org 的 `priceCurrency` 期望 ISO 4217 代码，**USDT 不是 ISO 4217**，layout.tsx 输出的 JSON-LD 中 `priceCurrency: "USDT"` 对 SEO/富结果有潜在影响——属价格治理决策，未动。
- `sku_registry.json` 顶部 note 写"Regenerate via platform/licensing/build_sku_registry.py"，实际构建器在 `tools/build_sku_registry.py`（sku_registry.py 文档串也指向 tools/）。下次重新生成时可顺手修正 note 文案（改 tools/build_sku_registry.py 里的字符串，本次只读未动）。

---

## 3. 官网有但 registry 没有的 offer

| offer id | 官网现值 | 定性 |
|---|---|---|
| `realtime-creator` | 2580 USDT / one-time，"换脸 + 换声 + 数字人"全能打包 | **legacy USDT 定制部署报价**：跨 facex（换脸）/ voicex（换声）/ livex（数字人）三产品能力的打包 SKU，registry 按单产品建 SKU 的结构里没有它的位置。skuId 留空未编造。 |

范围外提示：`website/lib/content.ts` 的定制部署段还有"全家桶 3980 USDT"、"运维订阅 198/月"、"远程协助 160/小时"等展示价，它们不在 pricing.ts（无 JSON-LD 挂牌），故不列入本对齐表；若后续把 content.ts 展示价也纳入台账，需另行盘点。

---

## 4. 补齐建议（谁该改什么；本次一律未动 product.yaml / registry）

### 4.1 product.yaml 加 SKU（产品 owner 改，改完跑 `tools/build_sku_registry.py` 重新生成 registry）

- **`realtime-creator`（2580 USDT 创作者全能打包）**：二选一——
  1. 若该打包会继续售卖：建议在 `products/huanying/product.yaml`（幻影 LiveX）下新增打包类 SKU（如 `livex-creator-deploy`，one-time），因官网 layout.tsx 即把 realtimeOffers 挂在 livex 名下；registry 生成后回填官网 skuId。
  2. 若判定为纯历史轨道：**仅历史保留**，维持 skuId 留空 + pricing.ts 内注释标注（本次已标注），不污染 registry。
- **lingox 三档 TBD 回填**：官网已按 39/59/99 USD 实卖，建议老板拍板后把 `products/tongyi/product.yaml` 的三个 `price: TBD` 改为实价（与 product.yaml 内建议价一致），消除不一致 #4–#6。

### 4.2 官网加挂牌（官网侧改 `website/lib/pricing.ts`，可另行排期）

- **`chatx-entry`（入门 58/月）**：content.ts plans 已展示，建议补进 `autochatOffers`，让 ChatX 三档完整进 JSON-LD。
- **voicex 三个月付档（starter/std/pro）**：public 可直售、registry 已定价、content.ts 已展示，建议新建 `voiceOffers` 数组挂牌（挂 voicex 产品的 Service 节点）。
- **livex 的 `livex-avatar-buy`（798 买断）/ `livex-dub-matrix`（398/月）**：mixed 中可公开的部分，可挂牌。
- **暂不建议挂牌**：`voicex-usage`（per-10k-chars）、`facex-image/video`（per-image/per-min）、`livex-dub-min`（per-min）、`voxx-meeting`（per-session）——这些计量单位超出现有 `PriceUnit = "one-time" | "month"` 类型，挂牌前需先扩展 PriceUnit（会触碰导出类型，应单独评审）；且 facex 系 gated、voxx 系未定价（TBD），本就不满足公开挂牌条件。
- **不应挂牌**：`reachx-deploy`（gated 高风险，product.yaml 明确不进公开货架/sitemap）、`facex-avatar` 与 `facex-image/video`（gated 准入线，公开 JSON-LD 与合规隔离策略冲突）。

### 4.3 币种治理（价格委员会决策，两侧一起定）

- autochatOffers / realtimeOffers 的 **USDT 报价币种**建议统一切 USD（与 registry、product.yaml 的"单位 USD、结算支持 USDT"口径一致），USDT 降级为"结算方式"表述；同时修复 schema.org priceCurrency 非 ISO 4217 的问题。属改价现值范畴，本次未动。

---

## 5. 本次代码改动清单（仅 `website/lib/pricing.ts`）

1. `PriceOffer` 接口新增**可选**字段 `skuId?: string`，注释说明其为"官网报价 ↔ 集团授权台账（sku_registry.json）的关联键"，且不进 toSchemaOffer 的 schema.org 输出（JSON-LD 结构零变化）。
2. 6 个 offer 回填 skuId：`realtime-basic→facex-live-deploy`、`autochat-team→chatx-team`、`autochat-flagship→chatx-flagship`、`translate-charpack→lingox-charpack`、`translate-team→lingox-team`、`translate-pro→lingox-pro`；`realtime-creator` 留空并加注释说明原因。
3. 新增只读纯函数 `findOfferBySkuId(skuId: string): PriceOffer | undefined`：遍历全部导出 offer 数组（realtimeOffers / autochatOffers / translateOffers，经模块内常量 `ALL_OFFER_ARRAYS` 登记）按 skuId 反查，供集团后台/台账对账使用。
4. **未改动**：所有 price/currency/name/description 现值、导出结构、toSchemaOffer 行为；引用方 `app/layout.tsx` 无需任何修改。

## 6. 验证结果

- `website/` 目录 `npx tsc --noEmit`：**退出码 0，无任何类型错误**（含既有代码，无需记录忽略项）。
- 未运行 `npm run build`（按分工由同事统一执行）。

---

## 7. 2026-07-18 定价决议落地

> 老板已拍板三项定价决策（口径：竞品价 ×2 + 品牌尾数惯例，基准已调研锁定），本节记录落地结果。
> 落地改动：`products/tongyi/product.yaml`（TBD→实价）、`products/huanying/product.yaml`（新增
> `livex-creator-deploy`）、重跑 `tools/build_sku_registry.py` 再生成 `sku_registry.json`
> （generated 2026-07-18，7 产品 / **22 SKU** / TBD 由 5 降为 2——仅剩 voxx 两项，非本次范围）、
> `website/lib/pricing.ts`（改价 + 回填 skuId + 币种统一 USD + `currency` 类型收窄为 `"USD"`）、
> `website/lib/content.ts`（zh/en 展示文案同步）。

### 7.1 三项决议与计算过程

1. **通译 LingoX 三档定价**（基准：NexScrm，×2）：
   - `lingox-charpack` 字符包（1.5M chars，one-time）＝ **59 USD**（NexScrm Character Pack $30/1.5M ×2 = 60 → 品牌尾数 9 惯例 59）；
   - `lingox-team`（month）＝ **99 USD/月**（NexScrm Standard $48/mo ×2 = 96 → 99）；
   - `lingox-pro`（month）＝ **198 USD/月**（NexScrm Pro $90/mo ×2 = 180 → 品牌尾数 8 惯例 198）。
2. **realtime-creator 打包 SKU 归属幻影 LiveX**：`products/huanying/product.yaml` 新增
   `livex-creator-deploy`（创作者全能包：实时换脸+声音克隆+数字人 私有化部署，one-time）＝ **5580 USD**
   （基准栈：Synthesia 定制 Studio Avatar $1000/年 + ElevenLabs Pro 语音克隆 ≈$1188/年 +
   HeyGen LiveAvatar 坐席 ≈$588/年 ≈ $2776/年 ×2 = 5552 → 尾数 8 惯例 5580）。
   官网 `realtime-creator` offer 同步改价 2580→5580 并回填 `skuId: "livex-creator-deploy"`，
   §3"registry 缺失"项就此消除。
3. **官网报价币种 USDT→USD 统一**：`pricing.ts` 全部 offer 的 `currency` 改 `"USD"`
   （realtime-basic 980、realtime-creator 5580、autochat-team 198、autochat-flagship 598 数值不变只换币种；
   translate 三档本就 USD、数值改 59/99/198）；`PriceOffer.currency` 类型收窄为 `"USD"`（删除 `"USDT"`
   联合成员），schema.org priceCurrency 非 ISO 4217 问题（§2 口径观察）同步修复。
   **USDT 保留为"结算方式"表述**：文案中"支持 USDT 结算 / USDT 收款"等支付轨道描述不动，
   仅替换作为报价币种出现的 USDT。

### 7.2 生效后的对齐状态表（7 offer 全部已对齐）

| pricing.ts offer id | skuId | 官网价格/币种/周期 | registry 价格/币种/周期 | 状态 |
|---|---|---|---|---|
| `realtime-basic` | `facex-live-deploy` | 980 USD / one-time | from 980 USD / one-time | **已对齐**（§2 #1 币种差异消除；registry "from 980" 起价表达 vs 官网定值为既有口径差异，维持只记录） |
| `realtime-creator` | `livex-creator-deploy` | 5580 USD / one-time | 5580 USD / one-time | **已对齐**（原"registry 缺失"，本次新增 SKU + 回填 skuId + 改价 2580→5580） |
| `autochat-team` | `chatx-team` | 198 USD / month | 198 USD / month | **已对齐**（§2 #2 币种差异消除） |
| `autochat-flagship` | `chatx-flagship` | 598 USD / month | 598 USD / month | **已对齐**（§2 #3 币种差异消除） |
| `translate-charpack` | `lingox-charpack` | 59 USD / one-time | 59 USD / one-time | **已对齐**（§2 #4 TBD 消除；39→59） |
| `translate-team` | `lingox-team` | 99 USD / month | 99 USD / month | **已对齐**（§2 #5 TBD 消除；59→99） |
| `translate-pro` | `lingox-pro` | 198 USD / month | 198 USD / month | **已对齐**（§2 #6 TBD 消除；99→198） |

§2 所列 6 个价格不一致项全部消除；§3 的 `realtime-creator` 由"registry 缺失"转为已对齐。
注册表汇总：22 SKU（21+1），`livex-creator-deploy` visibility 随幻影产品级 mixed。

### 7.3 展示文案同步范围与遗留记录

- **已同步**（`website/lib/content.ts` zh/en 各 4 处，共 8 处）：translate 三档展示价 39/59/99 → 59/99/198
  （zh"字符包/团队/专业" + en"Char pack/Team/Pro"）；realtime 套餐卡"创作者全能/Creator all-in"
  2580 USDT → **5580 USD**；"基础部署/Basic deploy" 980 USDT 起 → **980 USD 起**；定制部署段
  自购硬件档"一次性 980 USDT 起 / one-time from 980 USDT" → **USD**（数值不变）。
- **有意保留**（支付轨道语境）：content.ts"单位：USD · 支持 USDT 等结算""全程 USDT 结算""USDT 收款"、
  legal-content.ts"服务以 USDT 结算"、各页 metadata"USDT 结算"等。
- **未动并记录**（非本次五个 offer 范围）：content.ts"全家桶 3980 USDT""托管 from 1980 USDT/月"
  "分红合作 from 20,000 USDT"及 ROI 试算 USDT 计价、RoiCalculator 输入单位 USDT；
  manualContent.ts / bot-knowledge.ts / DownloadSection.tsx"99 USDT 远程代部署"（独立服务，
  与 lingox-team 99 仅数字巧合）；app/order、app/en/order 与 download 页的 AvatarHub 会员档
  JSON-LD `priceCurrency: "USDT"`（39–699 USDT/月，属 avatarhub-pricing.ts 会员体系，本报告
  范围外，见前言）；catalog-posts.ts"价格（USDT）"标签、components/**（OrderPanel/Plans/
  RoiCalculator/ProductLanding 等）的 USDT 表述——组件区与 bot 侧为同事施工区，未触碰；
  app/layout.tsx 注释"LiveX（…USDT 遗留轨道）"已过时但该文件不在本次许可清单，留待 owner 顺手更正。
  （→ 上述"未动并记录"项已在 §8 治理收尾中处理，禁区文件除外，见 §8.5。）

---

## 8. 2026-07-18 治理收尾

> 同日第二轮：把 §7 决议延伸到全站——修复全家桶倒挂、其余报价币种 USDT→USD 收尾、
> §4.2 挂牌建议落地。改动文件：`website/lib/content.ts`、`lib/avatarhub-pricing.ts`（仅注释）、
> `lib/catalog-posts.ts`、`lib/manualContent.ts`、`lib/pricing.ts`、`app/layout.tsx`、
> `components/`（仅 git 干净文件：DownloadSection / OrderPanel / ClientAppCTA / Plans /
> RoiCalculator）。**数值除全家桶外一律不变，只换报价币种标注。**

### 8.1 全家桶倒挂修复（唯一改数值项）

`content.ts` 定制部署段"全家桶 / Everything"（face+voice+数字人全套，内容 ≥ 创作者包）
**3980 USDT → 7980 USD**（zh/en 两处，grep 全站确认无其余"3980"）。依据：§7 创作者包
2580→5580 后全家桶出现"内容更多反而更便宜"的商业倒挂，按同一"竞品 ×2"决议延伸
（3980×2=7960 → 品牌尾数 8 惯例 7980），改动处已加注释。

### 8.2 报价币种 USDT→USD 收尾清单（数值全部不变）

| 文件 | 位置 | before → after |
|---|---|---|
| `lib/content.ts` | 托管月费（zh/en） | "from 1980 USDT / 月(mo)" → "from 1980 USD / 月(mo)" |
| `lib/content.ts` | 分红合作起投（zh/en） | "from 20,000 USDT" → "from 20,000 USD" |
| `lib/content.ts` | 投资 ROI 试算标题+3 行（zh/en 共 8 处） | "50,000 / 8,000–12,000 / 6,000–9,000 / 4,200–6,300 USDT" → 同数值 USD |
| `lib/content.ts` | ROI 计算器输入单位 salary/aov（zh/en） | "USDT" → "USD" |
| `lib/catalog-posts.ts` | 商品帖价格标签（zh/en） | "价格（USDT）/ Pricing (USDT)" → "价格（USD）/ Pricing (USD)" |
| `lib/manualContent.ts` | 远程代部署 ×3（FAQ zh/en + 支持章 zh/en） | "99 USDT" → "99 USD" |
| `lib/avatarhub-pricing.ts` | `Tier.monthly` 注释 | 标注挂牌币种 USD、链上结算走 USDT（会员档 39–699 本身是纯数字字段，无币种串可改） |
| `components/DownloadSection.tsx` | 远程代部署（zh/en） | "99 USDT" → "99 USD" |
| `components/OrderPanel.tsx` | 结算条选中价、档位卡单位、首年 8 折约数、授权表副标题、远程代部署价 | "… USDT（/月/年）" → "… USD（/月/年）" |
| `components/ClientAppCTA.tsx` | 会员起价（zh/en） | "${from} USDT/月起(mo)" → "USD" |
| `components/Plans.tsx` | ChatX 套餐卡币种角标 | "USDT" → "USD" |
| `components/RoiCalculator.tsx` | 净增/年化/推荐套餐价 3 处 | "USDT" → "USD" |

### 8.3 有意保留（支付轨道语境，非报价币种）

- content.ts："单位：USD · 支持 USDT 等结算"、"全程 USDT 结算"、"USDT 收款"、FAQ 付款方式；
- legal-content.ts："服务以 USDT 结算（TRC20/ERC20）"；catalog-posts.ts："全程 USDT 结算 / USDT only"；
- ProductLanding / TranslateDemo："USDT 结算"表述与示例问句；
- OrderPanel / OrderStatusLookup 的**付款流金额**（"应付金额 X USDT""请精确转账 X USDT""应付 X USDT"）：
  这是链上实付金额（`lib/order-store.ts` 台账 `currency:"USDT"`、小数尾数对账机制），属结算轨道
  而非挂牌报价，且 order-store 不在本次许可清单——挂牌价（USD）→ 实付（USDT）正是"报价 USD、
  结算 USDT"的双轨口径；
- koVoice / jaVoice / ogTemplate / telegram-bot / tg-broadcast / app 各页 metadata 的"USDT 结算(決済/결제)"。

### 8.4 新挂牌清单（§4.2 建议落地，价格均已与 registry 核对）

`lib/pricing.ts` 新增 2 个导出数组 + autochatOffers 补 1 档，均带 skuId（挂牌总数 7→13）：

| offer id | skuId | 价格 | 周期 |
|---|---|---|---|
| `voice-starter` | `voicex-starter` | 18 USD | month |
| `voice-std` | `voicex-std` | 78 USD | month |
| `voice-pro` | `voicex-pro` | 198 USD | month |
| `livex-avatar-buy` | `livex-avatar-buy` | 798 USD | one-time |
| `livex-dub-matrix` | `livex-dub-matrix` | 398 USD | month |
| `autochat-entry` | `chatx-entry` | 58 USD | month |

`app/layout.tsx` 接入：跟随既有 `PRODUCT_OFFERS` 模式——`voicex: voiceOffers`（VoiceX Service
节点新挂 offers，锚点 #realtime 已存在）；`livex: [...realtimeOffers, ...livexOffers]`（LiveX
Service 节点在原定制部署 2 档之上加挂买断/矩阵）；新数组同步登记进 pricing.ts 的
`ALL_OFFER_ARRAYS` 供 findOfferBySkuId 反查；顺手更正了 layout.tsx 过时的"USDT 遗留轨道"注释
（§7.3 遗留项）。**不挂牌**（维持 §4.2 结论）：`facex-image/video`、`facex-avatar`、
`reachx-deploy`（gated 合规准入线，不进公开 JSON-LD）；`voicex-usage`、`livex-dub-min`、
`voxx-meeting`（per-usage 单位超出 `PriceUnit = "one-time" | "month"`，扩类型需单独评审）；
`livex-avatar-sub`（"from 198" 起价非定值，schema.org Offer.price 需数值）。

### 8.5 待老板侧收尾清单（禁区文件里的报价币种 USDT，本次未动）

1. `lib/bot-knowledge.ts`（在途施工）：L74/L248 ChatX 档位模板串 "`p.priceMonthly` USDT/月(mo)"、
   L94 英文价格速览标题 "(USDT)"、L156/168/277/285 "99 USDT 远程代部署" ——报价币种应改 USD；
   L82"（USDT 结算）"及 L120/127/236 等结算表述可保留。
2. `app/order/page.tsx` + `app/en/order/page.tsx`（app/** 禁区）：openGraph 描述
   "会员套餐 39–699 USDT/月 / Plans from 39 to 699 USDT/mo" → USD；会员档 JSON-LD
   `priceCurrency: "USDT"`（39–699）→ "USD"（同步修复非 ISO 4217 问题）。
3. `app/download/page.tsx` + `app/en/download/page.tsx`（禁区）：SoftwareApplication JSON-LD
   `offers.priceCurrency: "USDT"`（price "0" 免费试用档）→ "USD"。
4. 无需处理（结算轨道，记录备查）：`lib/order-store.ts` `currency:"USDT"` 与通知文案的
   "应付 X USDT"（链上台账）、`app/api/order` / `app/api/admin/order-status` 的应付金额、
   `lib/ops-summary.ts` "入账 X USDT"、telegram-bot / tg-broadcast 的"USDT 结算"简介。

### 8.6 验证

- `website/` 目录 `npx tsc --noEmit`：**退出码 0，零类型错误**。
- grep 复核：本次许可文件中剩余 "USDT" 全部为 §8.3 所列支付轨道语义；"3980" 全站已清零。
- 未运行 `npm run build`（按分工统一执行）。
