# 无界全域运营事件契约（EVENT_CONTRACT）

> 版本：v1（P0 落盘期） · 2026-07-18
> 归属：`platform/observability/` · 配套文件：`events_registry.json`（事件字典单一真相）、`emitter.py`（参考发射器）
> 适用范围：三系七产品 + 官网 + 平台层，凡是要进集团统一后台的运营指标，一律走本契约。

---

## ⚠️ 隐私红线（先读这一节，违反即合规事故）

集团架构是**联邦制**：产品业务数据留在各产品本地库，向集团数仓上报的只有**运营指标事件**。这是集团对客户与监管的"**内容不上收**"承诺，因此 `props` 里：

**永远不允许出现：**

- 聊天原文、消息内容、回复文本（智聊/智拓/通译的任何对话内容）；
- 翻译原文与译文（通译/通传/智聊翻译链路，只准报**字符数/分钟数**）；
- 人脸图像、声纹、音频样本等生物特征数据（幻声/幻影/幻颜只准报**资产 ID 引用**与耗时/帧数等指标）；
- 完整手机号、证件号、银行卡号等可直接定位个人的标识原文。

**只允许出现：**指标数值、状态/枚举、ID 引用（`cust_*` / `ord_*` / `prs_*` 等，或产品本地记录 ID）、脱敏后的计数与时长。

> 判断口诀：**"拿到这条事件的人，能不能还原出客户说了什么、长什么样、声音是什么样、手机号是多少？"** 能，就不许上报。
> 新事件注册进 `events_registry.json` 前，props 字段表必须过一遍本节评审。

---

## 1. 动机：为什么要这套契约

七个产品各有独立后台，业务闭环在本地完成；但集团要回答的问题是跨产品的——CAC/CPL 多少、哪个渠道的线索转化成了哪款产品的付费、授权池整体健康度、各产品核心漏斗（获客→承接→转化→交付→续费）卡在哪一环。这些问题的最小公倍数不是"上收业务库"，而是**一套全域统一的事件流**：

- **字段统一**：同一个信封结构，集团数仓一套 schema 吃下所有产品；
- **命名统一**：`<namespace>.<domain>.<action>` 三段式，看名字即知归属与含义；
- **口径统一**：事件字典（registry）是单一真相，tier 标注哪些是集团必看；
- **路径解耦**：异步、fail-silent，埋点永远不拖垮业务主路径。

本期（P0）只定契约 + 本地 spool 落盘；网络收集器是下期的事（见 §5 传输演进）。

## 2. 事件信封（Envelope）

一条事件 = 一个 JSON 对象（UTF-8，单行序列化）。字段如下：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `event_id` | ✅ | string | 全局唯一事件 ID，`evt_<26 字符 Crockford Base32 大写 ULID>`；正则 `^evt_[0-9A-HJKMNP-TV-Z]{26}$`。ULID 高 48 bit 为 Unix 毫秒时间戳、低 80 bit 为加密安全随机数，与 `platform/identity/ID_SPEC.md` 全域 ID 规范一致（`evt` 前缀已在该注册表登记）。 |
| `ts` | ✅ | string | 事件发生时刻，ISO 8601 UTC 毫秒精度，`Z` 结尾，如 `2026-07-18T04:00:00.123Z`。由发射方本机时钟产生（见 §7 时钟）。 |
| `product_id` | ✅ | string | 事件归属，枚举九选一：`zhituo` / `zhiliao` / `tongyi` / `tongchuan` / `huansheng` / `huanying` / `huanyan` / `website` / `platform`。 |
| `name` | ✅ | string | 事件名，三段式，见 §3。 |
| `workspace_id` | 可选 | string | 工作区 ID（`wsp_*`），多租户产品必带。 |
| `customer_id` | 可选 | string | 客户 ID（`cust_*`），能关联时尽量带，跨产品漏斗靠它串。 |
| `actor` | 可选 | string | 触发主体：操作员/坐席/系统任务标识（如 `system`、坐席工号引用）。不放真实姓名。 |
| `props` | ✅ | object | 业务属性，**扁平 JSON 对象**，值只允许标量（string / int / float / bool / null）；不允许嵌套对象和数组。字段表在 registry 中登记。 |

约定：

- 未知字段：收集端遇到信封外的多余字段**保留不报错**（向前兼容）；
- `props` 缺省为空对象 `{}`；
- 一切字符串统一 UTF-8；金额用 float + 独立 `currency` 字段（ISO 4217）。

## 3. 事件命名

```
<namespace>.<domain>.<action>
```

- 正则：`^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$`（各段小写字母/数字/下划线，恰好三段）；
- **`namespace` 必须等于信封 `product_id`**，不一致即非法（发射器直接丢弃）；
- `domain`：业务对象（`order` / `session` / `license` / `voice` / `live` / `task` / `persona`…）；
- `action`：动词过去式/完成态优先（`created` / `paid` / `completed` / `started` / `ended`…），事件是"已发生的事实"，不是指令；
- 计量类事件用 `*_metered` 后缀（如 `translation.chars_metered`），表示按批聚合的用量上报；
- 新事件**先注册再发射**：在 `events_registry.json` 增加条目（name/tier/product/props/description）并过隐私评审。未注册事件发射器不拒收（宽松校验，落盘时标 `_unregistered: true`），但集团端会在治理报表里点名。

### tier 分级

- `core`：集团统一后台必看（漏斗/收入/用量/授权），接入产品**必须**发射；
- `optional`：产品深度运营自选，集团端可选消费。

## 4. Spool 落盘（本期传输方式）

- **目录**：环境变量 `EVENT_SPOOL_DIR`；未设置时缺省为 `<仓库根>/data/events/spool/`。目录不存在时发射器自动创建；
- **文件**：按天切分 `events-YYYYMMDD.jsonl`，日期取**事件 `ts` 的 UTC 日期**；
- **格式**：JSON Lines——一行一个事件 JSON，UTF-8，`\n` 结尾，**append-only**（只追加、不改写、不删除）；
- **fail-silent**：发射过程中任何异常（磁盘满、目录只读、序列化失败……）一律吞掉，绝不向业务主路径抛异常、不打断业务流程。埋点丢了可以补，业务挂了是事故；
- **去重**：spool 不做去重。重放/补传造成的重复由**收集端**按 `event_id` 幂等去重（见 §6）；
- **权责**：产品只管往 spool 写；文件的收割、上传、归档、清理是收集器（下期）的职责。本期不要自行删 spool 文件。

## 5. 传输演进路线

| 阶段 | 传输 | 说明 |
|---|---|---|
| **P0（本期）** | 本地 spool 落盘 | 只写 `events-YYYYMMDD.jsonl`，无网络依赖，先把埋点铺进各产品 |
| P1（下期） | HTTP 收集器 + 断点补传 | 各机器跑轻量 shipper：读 spool → 批量 POST 集团收集器 → 记录已上传偏移（byte offset/行号游标）→ 断网/宕机后从游标续传。收集端按 `event_id` 幂等入库 |
| P2（远期） | 消息队列/流式 | 量级上来后 shipper 改推 MQ，数仓消费；契约（信封+字典）不变 |

契约设计保证升级传输层时**发射方代码零改动**：spool 格式即传输格式。

## 6. 幂等与去重

- `event_id` 是幂等键：同一事件不管被补传多少次，收集端只入库一次（`INSERT ... ON CONFLICT (event_id) DO NOTHING` 语义）；
- 发射方**不要**为"同一业务事实"生成两个 event_id——在业务侧就近发射一次即可；若业务代码有重试逻辑，应在重试外层发射，或自行保证只发一次。发多了不会破坏数仓（会被去重），但会浪费带宽；
- ULID 自带毫秒时间戳，`event_id` 字典序 ≈ 生成时间序，数仓可用它做粗排与分区参考，但**业务时间一律以 `ts` 字段为准**。

## 7. 时钟

- `ts` 与 `event_id` 里的时间戳都来自**发射方本机时钟**（UTC）。各产品机器必须开 NTP 同步；
- 允许时钟漂移带来的少量乱序：数仓按 `ts` 排序聚合，不假设事件按到达顺序有序；
- 同毫秒内 ULID 不保证单调递增（与全域 ID 规范一致，不得私自加单调逻辑）；
- 跨天边界：事件写进哪个日文件由 `ts` 的 UTC 日期决定，与本地时区、写入时刻无关——23:59:59.999Z 的事件永远在当天文件里。

## 8. 接入方式（Python 产品）

参考实现 `emitter.py`（仅标准库）。**注意**：顶层目录 `platform` 与 Python 标准库 `platform` 模块同名，**不要** `import platform.observability`（会被标准库遮蔽/解析失败，详见 `emitter.py` 顶部注释与 `platform/identity/ids.py` 的同款警告）。正确姿势：

```python
import sys
sys.path.insert(0, r"D:\workspace\boundless\platform\observability")
import emitter

emitter.emit(
    "tongyi", "tongyi.translation.chars_metered",
    props={"chars": 1280, "src_lang": "zh", "dst_lang": "en"},
    workspace_id="wsp_01KXS8BM00008J4CT4ANK7F24S",
)
```

非 Python 产品（如官网 Next.js）按本契约自行实现：生成合法信封 → 追加写当天 jsonl。信封正则与字典以本文件 + registry 为准。

## 9. 变更管理

- registry 带 `version` 字段；**加事件、加可选 props 字段**是兼容变更（version 不动，改 `updated`）；
- **改名、删字段、改字段类型**是破坏性变更，原则上不做——事件名错了就注册新名、旧名标记废弃（description 注明 `deprecated`），数仓侧做映射；
- 信封结构变更（加必填字段等）必须升 `version` 并全产品同步评审。

---

## 10. 传输层（已上线）：collect 收集器 + uploader 补传

> P1 落地 · 2026-07-18。§5 的承诺兑现：发射方代码零改动，spool 格式即传输格式。

### 10.1 收集器 API

- **端点**：`POST https://bd2026.cc/api/collect`（官网 Next.js，源码 `website/app/api/collect/route.ts`；GET 返回 405）；
- **请求体**：`{"events": [<信封>, ...]}`，单批 ≤ 500 条（超限 413 `batch_too_large`，请拆批重发）；
- **鉴权**：`Authorization: Bearer <EVENT_INGEST_KEY>`——机器对机器上报密钥（官网 env 配置，与 /console 人类口令体系完全独立）。服务端未配置密钥 → 503 `collector_not_configured`（附配置说明）；密钥错误/缺失 → 401；
- **来源标识**：可选请求头 `X-Event-Source`（建议传发起机器 hostname），入库记入 `source` 列；缺省 `unknown`；
- **响应**（200）：`{"ok": true, "accepted": n, "ignoredDuplicates": n, "rejected": [{"index": i, "reason": "…"}]}`
  - `accepted`：本批新入库条数；
  - `ignoredDuplicates`：`event_id` 已存在、被幂等忽略的条数（补传/重发属正常现象，不是错误）；
  - `rejected`：信封校验失败的条目（批内下标 + 原因）。**重发不会变合法**，收到即应回发射方排查，不阻塞其余条目入库；
- **校验口径**（与 §2/§3 一致）：`event_id` 正则 `^evt_[0-9A-HJKMNP-TV-Z]{26}$`、`ts` ISO8601 UTC 毫秒、`product_id` 九枚举、`name` 三段式且 namespace === product_id、`props` 扁平标量对象且序列化后 ≤ 8KB。信封外未知字段容忍不报错（§2 向前兼容）；带 `_unregistered: true` 的事件照常入库并记 `unregistered = 1` 供治理。

### 10.2 存储：事件独立成库

事件量远大于账本量，**独立成库** `group-events.db`（`website/lib/events-db.ts`；路径默认 `DATA_DIR/group-events.db`，env `EVENTS_DB` 可覆盖），不进 group-ledger.db——账本备份保持轻小。表结构：`events(event_id TEXT PRIMARY KEY, ts, product_id, name, workspace_id, customer_id, actor, props /*JSON*/, unregistered, received_at, source)`，索引 `(product_id, ts)`、`(name, ts)`、`(ts)`。消费端：集团控制台 `/console/kpi` 看板。

### 10.3 幂等语义

`event_id` 主键 + `INSERT OR IGNORE`（§6 承诺的落地）：同一事件不管补传多少次只入库一次，重复到达计入 `ignoredDuplicates`（首次入库的 `received_at` / `source` 保留不覆盖）。因此 uploader 断网/宕机后**重发整批是安全的**，不需要精确一次投递。

### 10.4 uploader 补传器

`platform/observability/uploader.py`（仅标准库）：读 spool 日文件 → 从上次字节偏移续读完整行 → 批量 POST 收集器 → 成功才推进偏移。

```
python platform/observability/uploader.py \
    --endpoint https://bd2026.cc/api/collect --key <EVENT_INGEST_KEY>
```

- `--spool-dir` 缺省 env `EVENT_SPOOL_DIR`，再缺省 `./data/events/spool`；`--key` 缺省 env `EVENT_INGEST_KEY`；`--batch` 默认 200（收集端上限 500）；`--source` 自定义 X-Event-Source（默认 hostname）；
- **断点续传**：游标文件 `--state-file`（缺省 `<spool 目录>/.upload_state.json`）记录每个文件已上传字节偏移；先读后传、传成功再原子写 state——崩溃最多重发一批，靠 §10.3 幂等兜底；state 丢失/损坏只会全量重传，不会丢数、不会重计；
- **健壮性**：只上传 `\n` 结尾的完整行（尾部半行留待下次）；无法解析的脏行跳过并推进偏移（不卡队列）；5xx/网络错/超时指数退避重试 3 次，仍失败则保留偏移退出码 1（下次 cron 续传）；401/413 等 4xx 配置类错误不重试直接报错；
- `--dry-run` 只统计将上传行数（不联网不写 state）；`--selftest` 线程内起本地 mock 收集器自测批量与断点逻辑（不连外网）。

### 10.5 部署备注

- **官网侧**：部署环境设置 `EVENT_INGEST_KEY`（强随机，独立于 CONSOLE_KEY/ADMIN_KEY，见 `website/.env.example`）；事件库路径可用 `EVENTS_DB` 覆盖；
- **产品机器侧**：cron 每 5 分钟跑一次 uploader（单实例，勿并发跑同一 spool 目录）：

```
*/5 * * * * cd /path/to/boundless && python platform/observability/uploader.py --endpoint https://bd2026.cc/api/collect --key "$EVENT_INGEST_KEY" >> logs/uploader.log 2>&1
```

- spool 权责不变（§4）：uploader 只读 spool 与自己的 state 文件，不删不改事件行；日文件归档/清理策略另行制定——轮转删除旧文件前，确认 state 中该文件偏移已到文件末尾（即已全量上传）。
