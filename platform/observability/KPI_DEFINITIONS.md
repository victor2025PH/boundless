# 无界全域 KPI 口径定义（KPI_DEFINITIONS）

> 版本：v1 · 2026-07-18 · 归属：`platform/observability/`
> 定位：`events_registry.json` 的**语义附录**——事件字典回答"有哪些事件"，本文件回答"指标怎么数"。
> 消费方：`website/scripts/kpi-weekly-report.mjs`（全矩阵周报，本口径的参考实现）、`/console/kpi` 看板、以及一切要出数的报表。
> **铁律：任何报表要算数，一律引用本文件；禁止各报表各算（变更流程见 §8）。**

---

## 0. 数据来源边界（先读）

| 数据 | 库 | 表 | 说明 |
|---|---|---|---|
| 行为 | `group-events.db` | `events` | EVENT_CONTRACT §10 收集器入库的全域事件流（env `EVENTS_DB`，缺省 `DATA_DIR/group-events.db`） |
| 钱 / 授权 | `group-ledger.db` | `orders` / `leads` / `licenses` / `customers` | 集团影子账本（env `LEDGER_DB`，缺省 `DATA_DIR/group-ledger.db`） |

- **内容与生物特征永不参与**：呼应 EVENT_CONTRACT 隐私红线——所有 KPI 只由计数、金额、时长、状态、ID 引用聚合得出。聊天原文、翻译原文/译文、人脸/声纹/音频等数据本来就不在这两个库里，任何指标计算也**不允许**绕道产品本地库去读内容（不存在"抽内容做质检指标"的旁路）。
- 两库分工：行为量（活跃/用量/任务）看事件流；钱与授权（订单/支付/激活/到期）以账本为**结算口径**，事件流中的 `website.order.*` / `platform.license.issued` 只作交叉校验（事件受 uploader 部署与补传时延影响，周报数字不吃它，避免抖动）。唯一例外是**续费**（§2.7）：账本 licenses 是"当前态镜像"（upsert 覆盖 `expires_at`），不保留续费历史，续费只能数事件。

## 1. 通用规则

- **时间**：一律 UTC。窗口一律**半开区间 `[since, until)`**，按 UTC 日 00:00:00Z 对齐；"周"= ISO 周（周一 00:00Z 起 7 天）。
- **窗口归属**：事件按信封 `ts` 归属；账本按**阶段时间戳列**归属（`leads.first_seen`、`orders.created_at`、`orders.paid_at`、`orders.activated_at`、`licenses.issued_at`），**不按当前 status 归属**——status 会继续流转，时间戳不会。时间戳为 NULL 的行不计入对应阶段（属数据质量问题，进数据健康项）。
- **事件去重**：`event_id` 主键，入库已幂等（EVENT_CONTRACT §6/§10.3），指标层直接按行数即可。
- **账本去重**：订单按 `source_key`（表主键唯一），授权按 `(source_system, source_key)`，线索按 `source_key`（同一来源人多次提交在账本已合并为一行）。
- **跨产品串联**：靠 `customer_id`（`cust_*`）。
- **活跃主体键**：`workspace_id` 优先（多租户产品必带），缺失退化为 `customer_id`；两者皆缺 → 记"匿名活跃"（计入事件量与活跃日，**不计入**活跃主体数与留存）。
- **产品归属**：orders/licenses 按 `product_id` 列；leads 按 `interest` 经 `inferProductId`（`lib/ledger.ts` 与 `scripts/ledger-lib.mjs` 同源映射）推断。归属不了的进"(未归属)"桶单列——**全域总数按行计不受影响**，分产品数按归属计。

## 2. 全域漏斗（唯一口径）

阶段链：**曝光 → 线索 → 订单 → 支付 → 激活 → 活跃 → 留存 → 续费**

| # | 阶段 | 计数源（唯一） | 去重键 | 窗口归属 |
|---|---|---|---|---|
| 2.1 | 曝光 | **v1 暂缺**（registry 无 pageview 事件；官网 RUM 另有体系）。全域漏斗从"线索"起算，待注册 `website.page.viewed` 后升版接入 | — | — |
| 2.2 | 线索 | 账本 `leads` 行数；事件 `website.lead.submitted` 仅交叉校验不并入 | `source_key` | `first_seen ∈ [since, until)` |
| 2.3 | 订单 | 账本 `orders` 行数（含后续取消的单——进漏斗即算） | `source_key` | `created_at ∈ 窗口` |
| 2.4 | 支付 | 账本 `orders` 中 `paid_at` 非空的行；金额 = Σ`pay_amount` 按 `currency` 分组、不折算汇率 | `source_key` | `paid_at ∈ 窗口` |
| 2.5 | 激活 | 订单激活 + 授权签发**合并去重**，精确定义见 §3 | 见 §3 | `activated_at` / `issued_at ∈ 窗口` |
| 2.6 | 活跃 / 留存 | 见 §4 / §5 | 活跃主体键 | 事件 `ts ∈ 窗口` |
| 2.7 | 续费 | 事件 `platform.license.renewed` 条数（=续费笔数）；补充指标：distinct `props.license_id` = 续费授权数 | `event_id` | `ts ∈ 窗口` |

环比口径：与**紧前等长窗口**比较（`[since−len, since)`）；上期为 0 时不出百分比，报"新增（上期 0）"。

## 3. 激活：精确定义与跨引擎合并去重

**语义**：客户从"付了钱"到"真的可用"。两条产生路径——官网订单交付完成（`orders.activated_at` 落值）、引擎侧签发授权（`licenses.issued_at` 落值）。同一次交付可能两处都留痕（订单激活时签发授权），必须合并防重计。

**唯一口径**：`激活数 = A + B′`

- `A` = `orders.activated_at ∈ 窗口` 的订单数（去重 `source_key`）；
- `B′` = `licenses.issued_at ∈ 窗口` 且**未被合并**的授权数（去重 `(source_system, source_key)`）；
- **合并规则**：授权行 `customer_id` 非空，且存在**同 `customer_id`** 的订单其 `activated_at` 与该授权 `issued_at` 落在**同一 UTC 日** → 判为同一次激活（订单先计，该授权不再计）；
- `customer_id` 缺失无法判合并 → 授权单独计一次（**宁略高不漏计**），同时计入数据健康"归属缺失"。归属修复（console 手工 assign / 自动 auto_link）后口径自动收敛。

事件 `website.order.activated` / `platform.license.issued` 作交叉校验源，不并入计数（理由见 §0）。

## 4. 活跃（按产品）与北极星

**活跃定义**：窗口内该产品发生**活跃判定事件集**中任一事件。两个计量维度：

- **活跃日**：有活跃事件的 UTC 自然日数（周窗口 0..7）；
- **活跃主体数**：按 §1 主体键去重（匿名活跃不计主体）。

| 产品 | 活跃判定事件集（任一即活跃） | 北极星指标（每周） |
|---|---|---|
| 智拓 zhituo（获客） | `zhituo.friend.added`（有加友的自然日即活跃） | **周开口数** = `zhituo.prospect.replied` 条数——加友是供给量，开口才是可转化线索，直接喂给下游智聊 |
| 智聊 zhiliao | `zhiliao.session.ai_engaged`（有 AI 承接的自然日即活跃） | **周 AI 承接会话数** = `ai_engaged` 条数 |
| 通译 tongyi | `tongyi.translation.chars_metered`（有翻译计量即活跃） | **周翻译字符数** = Σ`props.chars` |
| 通传 tongchuan | `tongchuan.session.started` / `.ended`（有同传任务即活跃） | **周同传分钟数** = Σ`props.audio_minutes`（缺失回退 `duration_s/60`，取自 `.ended`） |
| 幻声 huansheng | `huansheng.voice.clone_completed` / `huansheng.tts.chars_metered` | **周 TTS 合成字符数** = Σ`props.chars`（对齐按量 SKU 计费口径） |
| 幻影 huanying | `huanying.live.started`（有开播即活跃） | **周开播场次** = `live.started` 条数（`live.ended` 属 optional tier，不做唯一口径） |
| 幻颜 huanyan | `huanyan.faceswap.completed`（有换脸完成任务即活跃） | **周换脸完成任务数** = `completed` 条数（`submitted` 属 optional tier 且未必成功，不计） |

- 北极星均按窗口聚合，`event_id` 天然去重；计量求和时 `props` 解析失败或为负值按 0 处理并计入数据健康"计量异常"。
- `persona.created`、`session.started`（智聊/通译）、`task.started` 等是**过程指标**，不判活跃、不做北极星，产品自选消费。

## 5. 留存

- **定义（次周仍活跃）**：`留存率 = |A_prev ∩ A_cur| / |A_prev|`，其中 `A_prev` / `A_cur` 为上一窗口 / 本窗口的活跃主体集合（主体键同 §1）。`A_prev` 为空 → 无值（报"—"，不报 0%）。
- **全域留存**：主体在任一产品活跃即算（跨产品 union）；**分产品留存**：主体限定该产品内的活跃事件。
- 匿名活跃不参与留存（无法跨周识别同一主体）。

## 6. 跨产品 LTV

- `LTV(customer) = 同 customer_id 名下 orders.pay_amount 合计`，限定 `paid_at` 非空且 `status ≠ 'cancelled'`（取消/退款单计 0——账本无负向冲销行；未来支持部分退款时升版）。
- 币种不折算，按 `currency` 分组呈现；汇率折算属报表展示层，不进口径。
- 无 `customer_id` 的订单不计入任何客户的 LTV（进"归属缺失"健康项）；跨引擎授权不直接参与 LTV（钱只在 orders 里）。

## 7. 数据健康与缺口告警口径

周报（§见 EVENT_CONTRACT §11）固定输出：

- **事件量**：本期/上期条数 + 库累计与首末事件时间；
- **上报源**：本期 distinct `source`（X-Event-Source）及各自条数——判断哪台机器断传；
- **未注册事件**：本期 `unregistered = 1` 条数（治理点名，EVENT_CONTRACT §3）；
- **归属缺失**：本期新订单缺 `product_id` / 缺 `customer_id`、新签授权缺 `customer_id`、匿名活跃事件数；
- **缺口告警**：七产品中任一产品本期 **0 事件**（任何事件名）→ 点名"埋点未上报或无业务"，需人工判断是 uploader 断了还是真没业务；`website` / `platform` 0 事件只降级为提示（漏斗钱侧走账本，不受影响，但交叉校验缺失）。

## 8. 口径变更流程

- **改口径 = 改本文件 + 通告**：升版本号（v1 → v2…）、在下方变更记录写明"改了什么、为什么、影响哪些指标"，并同步通告全员；周报/看板等消费方在同一变更内对齐。**禁止任何报表私改算法各算各的**；发现口径分叉，以本文件为准回归。
- **兼容性**：新增指标 = 兼容变更（版本不动，改 `updated` 日期）；修改既有指标的计数源/去重键/窗口规则 = **破坏性变更**，必须升版本，且周报在标题处标注"口径 vN 起"，跨版本环比须注明不可比。
- 事件信封与字典本身的变更走 EVENT_CONTRACT §9，不在本文件重复管理。

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1 | 2026-07-18 | 首版：固化全域漏斗七阶段、激活合并规则、七产品活跃/北极星、留存、LTV、数据健康告警口径 |
