# platform/leadbus · 线索交接总线契约（获客 → 承接）

> 版本：v1（融合期新增） · 2026-07-19
> 归属：`platform/leadbus/` · 配套：`client.py`（stdlib 瘦客户端 + 本地 outbox）、`lead_schema.json`（线索信封）
> 定位：把**上游获客**（TG-AI智控王 / huoke 真机 RPA）捕获的线索，标准化交给**中游承接**（chengjie 智聊坐席）。这是"产业链条"从获客到成交那一环落到接口层的体现。

---

## 0. 为什么是新的一条契约（不是并进 observability）

`observability` 是**遥测**：fire-and-forget、只报计数/状态、内容不上收、进集团数仓。
`leadbus` 是**业务通道**：线索的联系方式/画像是要**送达承接坐席去成交**的业务数据，需要投递/排队/补投语义，且**点对点、不进数仓**。两者隐私边界与投递语义都不同，故独立成契约。

> 判断口诀：**"这条数据是给机器算 KPI 的，还是给坐席去接客成交的？"** 前者走 observability，后者走 leadbus。

## 1. 两条铁律（对接的骨架）

| 铁律 | 含义 | 实现 |
|---|---|---|
| **总线可选** | `BOUNDLESS_BUS_URL` 未配置 → 单机模式：线索落本地 outbox、上游照常独立运行；配置了 → 联网投递 | `client.py` 构造时探测环境变量 |
| **fail-soft** | `publish()` 任何情况都不抛异常——投递不了就排队，获客主路径绝不被拖垮 | 失败收敛为 dict + 落 outbox |

这两条保证：**上游获客产品(智控王)脱离总线能独立跑，接上总线自动入链，一条线索都不丢。**

## 2. 契约（stdlib 瘦客户端签名）

`platform/leadbus/client.py`（纯 stdlib，零第三方依赖，零反向依赖）：

```python
LeadBusClient(bus_url=None, outbox_dir=None, timeout=8)   # bus_url 缺省读环境变量 BOUNDLESS_BUS_URL
  .publish(lead)        -> dict   # 投递一条线索；失败/单机落 outbox（永不抛）
  .drain_outbox(max=500)-> dict   # 总线恢复后补投积压线索（幂等键 lead_id）
  .status()             -> dict   # /api/leadbus/status（不可达则 available=False）
  .available()          -> bool   # 承接中台线索入口是否就绪
  .envelope_error(lead) -> str|None  # 线索信封校验（静态方法）
```

- **投递端点**：`POST {BOUNDLESS_BUS_URL}/api/leadbus/ingest`，body `{"lead": <信封>}`；由 chengjie 承接侧实现。
- **可降级**：承接中台不在线时不抛异常，`publish` 落 outbox 返回 `{"available":False,"queued":True}`；上游可继续获客，`drain_outbox()` 待恢复后补投。
- **幂等**：每条线索带 `lead_id`（客户端自动补 `lead_<hex>`，调用方也可自带）；承接端按 `lead_id` 幂等去重，补投安全。

## 3. 线索信封（Envelope，详见 lead_schema.json）

```jsonc
{
  "lead_id": "lead_ab12…",          // 幂等键，客户端自动补
  "ts": "2026-07-19T12:00:00Z",
  "source": { "product": "zhituo", "platform": "telegram", "campaign": "utm_x" },  // 必填 product+platform
  "lead": {
    "external_id": "tg:123456789",  // 必填，平台内唯一
    "handle": "@someone",
    "profile": { "lang": "en", "funnel_stage": "new", "intent_score": 0.72 }        // intent_score ∈ [0,1]
  },
  "assign_hint": { "domain": "ecommerce", "persona": "sales" }                       // 分配提示（可选）
}
```

## 4. 隐私边界（红线，与 observability EVENT_CONTRACT 配合）

- 线索信封里的 `external_id / handle / profile` 是**点对点业务载荷**，只在"获客产品 ↔ 承接中台"两端流转，**绝不可转发进 observability 事件 spool / 集团数仓**。
- 线索的**计数/意向分/来源渠道**才由调用方**另发** observability 事件（`zhituo.lead.captured` / `zhiliao.lead.ingested`，见 `events_registry.json`）——数仓只拿到"某渠道进了 N 条线索、意向分布如何"，拿不到"这条线索是谁、联系方式是什么"。
- 因此 `leadbus/client.py` 刻意**只做投递、不发遥测**（单一职责）：指标由调用方就近 `emit`，两条管道物理分离。

## 5. 依赖方向

`zhituo / huoke（获客）→ platform/leadbus(契约+client) →(HTTP)→ chengjie /api/leadbus/ingest（承接）`。
platform 不 import 任何产品/引擎，仅通过 HTTP 契约交互。

## 6. 落地状态与下一步

- 本层交付：`CONTRACT.md`（本文件）+ `client.py`（瘦客户端 + outbox + `--selftest` 全通过）+ `lead_schema.json`。
- 待接线（下一阶段）：
  1. chengjie 侧实现 `POST /api/leadbus/ingest` + `GET /api/leadbus/status`（按 `lead_id` 幂等入承接队列 + 坐席/人设分配）；
  2. 智控王 / huoke 在"捕获线索"处调 `LeadBusClient().publish(...)`，并**另发** observability 的 `*.lead.captured` 指标事件；
  3. 五机 cron 定时 `drain_outbox()`（对齐 observability uploader 的 5 分钟节律），把单机/断网期间积压的线索补投。
