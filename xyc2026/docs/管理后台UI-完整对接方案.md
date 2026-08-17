# 信用查 — 管理后台 UI 完整对接方案

> **用途**：提供给 AI Studio（或其它前端开发方）实现「管理后台」网页；开发完成后由本项目的后端 API 对接。  
> **范围**：涵盖当前所有管理端接口与页面功能，便于一次性实现并对接。

---

## 一、概述

### 1.1 目标

实现**仅面向内部管理员**的 Web 后台，具备以下能力：

| 模块 | 功能 |
|------|------|
| 总览 | 运营统计（今日查询、总用户、黑名单、待处理举报、待确认交易、绑定地址数、开放流水用户数、流水快照条数） |
| 举报管理 | 举报列表（用户对用户的举报）、按状态筛选、分页、审核（设为已核实/已驳回） |
| 申诉管理 | 申诉列表（用户纠错/误报反馈）、按状态筛选、通过/驳回 |
| 黑名单 | 将用户加入黑名单（tg_id + 原因 + 来源） |
| 举报人列表 | 举报人信誉（举报数、已核实数、最后举报时间） |
| 交易列表 | 站内交易确认列表、按状态筛选（待确认/已确认/已拒绝） |
| 风险趋势 | 按日统计查询结果风险分布（low/medium/high/extreme），供图表展示 |
| 待发提醒（可选） | 风险提醒待发送队列只读展示 |

### 1.2 技术约定

- **后端**：由本项目提供，**不要求**开发方实现新后端。
- **鉴权**：所有管理接口需在请求头携带 **`Authorization: Bearer <API_INTERNAL_KEY>`**。无该密钥或错误时返回 **403**。
- **Base URL**：由部署时配置（如 `http://127.0.0.1:8001` 或生产域名），前端通过环境变量注入（如 `VITE_ADMIN_API_BASE_URL`），**不要写死**。
- **交付**：前端工程源码 + 构建产物（如 `dist/`），对接方配置 Base URL 与密钥后挂载或反向代理。

---

## 二、鉴权

- **无**用户名/密码接口；鉴权仅靠 **Bearer Token**（即 `API_INTERNAL_KEY`）。
- **登录流程建议**：
  1. 登录页：输入框填写「管理密钥」，提交后请求 **GET /v1/admin/stats**，Header：`Authorization: Bearer <输入的密钥>`。
  2. 若返回 **200**：视为密钥有效，将密钥存入 **sessionStorage**（或内存），跳转总览页。
  3. 若返回 **403**：提示「密钥无效或已失效」。
  4. 后续所有请求统一在 Header 中携带：`Authorization: Bearer <保存的密钥>`。
  5. 任意请求若返回 403，可清除本地密钥并跳回登录页。
- 密钥**不得**写入前端代码或公开仓库；由运营在部署环境中线下提供。

---

## 三、接口清单（管理端）

**公共请求头**（以下所有接口均需）：

```http
Authorization: Bearer <API_INTERNAL_KEY>
```

**Content-Type**：Body 为 JSON 时使用 `Content-Type: application/json`。

---

### 3.1 运营统计

**GET** `/v1/admin/stats`

- **Query**：无  
- **Response 200**（JSON）：

```json
{
  "queries_today": 0,
  "total_users": 0,
  "blacklist_count": 0,
  "reports_pending": 0,
  "transactions_pending": 0,
  "bound_wallets_count": 0,
  "users_with_flow_visible": 0,
  "flow_snapshots_count": 0
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| queries_today | int | 今日查询次数 |
| total_users | int | 用户表总数 |
| blacklist_count | int | 黑名单条数 |
| reports_pending | int | 待处理举报数（reports 表 status=pending） |
| transactions_pending | int | 待确认交易数 |
| bound_wallets_count | int | 绑定收款地址总数 |
| users_with_flow_visible | int | 至少有一个「展示流水」开启的用户数 |
| flow_snapshots_count | int | 流水快照表总条数 |

---

### 3.2 举报列表（用户对用户的举报）

**GET** `/v1/admin/reports`

- **Query**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | 筛选：`pending` / `verified` / `rejected`；不传则全部 |
| limit | int | 否 | 条数，默认 100，范围 1–500 |
| offset | int | 否 | 偏移，默认 0 |

- **Response 200**（JSON）：

```json
{
  "items": [
    {
      "id": 1,
      "reporter_tg_id": 123456789,
      "target_tg_id": 987654321,
      "reason": "诈骗不发货",
      "category": "scam",
      "evidence_data": { "tx_hash": "0x...", "description": "..." },
      "status": "pending",
      "created_at": "2025-02-16T12:00:00"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| items[].id | 举报 ID，用于审核接口 |
| items[].reporter_tg_id | 举报人 Telegram ID |
| items[].target_tg_id | 被举报人 Telegram ID |
| items[].reason | 举报理由（可能较长） |
| items[].category | 分类：scam / run_order / lost_contact / other |
| items[].evidence_data | 可选证据（如 tx_hash、截图链接等） |
| items[].status | pending / verified / rejected |
| items[].created_at | 创建时间，ISO 8601 |

---

### 3.3 举报审核（更新举报状态）

**PATCH** `/v1/admin/reports/{report_id}`

- **Path**：`report_id` 为举报列表项中的 `id`。  
- **Body**（JSON）：

```json
{
  "status": "verified"
}
```

- **status** 取值**仅允许**：`verified`（已核实）、`rejected`（已驳回）。  
- **Response 200**（JSON）：`{"ok": true, "message": "verified"}`  
- **Response 404**：举报不存在，如 `{"detail": "Report not found"}`。

---

### 3.4 申诉列表（用户纠错/误报反馈）

**GET** `/v1/admin/feedback`

- **Query**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | 筛选：`pending` / `processed` / `rejected` |
| limit | int | 否 | 条数，默认 100，范围 1–500 |

- **Response 200**（JSON）：

```json
{
  "items": [
    {
      "id": 1,
      "reporter_tg_id": 123456789,
      "target_tg_id": 987654321,
      "reason": "误报，已和解",
      "status": "pending",
      "created_at": "2025-02-16T12:00:00"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| items[].id | 申诉 ID，用于更新状态 |
| items[].reporter_tg_id | 举报人 Telegram ID |
| items[].target_tg_id | 被举报人 Telegram ID |
| items[].reason | 用户填写的理由 |
| items[].status | pending / processed / rejected |
| items[].created_at | 创建时间 |

---

### 3.5 更新申诉状态

**PATCH** `/v1/admin/feedback/{feedback_id}`

- **Path**：`feedback_id` 为申诉列表项中的 `id`。  
- **Body**（JSON）：`{"status": "processed"}` 或 `{"status": "rejected"}`。  
- **Response 200**：`{"ok": true, "message": "processed"}`  
- **Response 404**：`{"detail": "Feedback not found"}`。

---

### 3.6 加入黑名单

**POST** `/v1/admin/blacklist`

- **Body**（JSON）：

```json
{
  "tg_id": 987654321,
  "reason": "多次欺诈举报",
  "source": "admin"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| tg_id | int | 是 | 要拉黑的 Telegram 用户 ID |
| reason | string | 否 | 原因说明 |
| source | string | 否 | 来源，默认 `admin` |

- **Response 200**：`{"ok": true, "added": true}`  
  - `added: true` 表示本次新加入；`added: false` 表示已在黑名单中。

---

### 3.7 举报人列表（信誉聚合）

**GET** `/v1/admin/reporters`

- **Query**：`limit`（可选，默认 100，1–500）。  
- **Response 200**（JSON）：

```json
{
  "items": [
    {
      "reporter_tg_id": 123456789,
      "report_count": 10,
      "verified_count": 6,
      "last_at": "2025-02-16T12:00:00"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| reporter_tg_id | 举报人 Telegram ID |
| report_count | 该用户发起的举报总数 |
| verified_count | 其中已核实数量 |
| last_at | 最后举报时间 |

---

### 3.8 交易列表（站内交易确认）

**GET** `/v1/admin/transactions`

- **Query**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | 筛选：`pending` / `confirmed` / `rejected` |
| limit | int | 否 | 条数，默认 100，1–500 |

- **Response 200**（JSON）：

```json
{
  "items": [
    {
      "id": 1,
      "initiator_tg_id": 111,
      "counterparty_tg_id": 222,
      "amount": "100.50",
      "currency": "USDT",
      "status": "pending",
      "created_at": "2025-02-16T12:00:00",
      "confirmed_at": null
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| id | 交易 ID |
| initiator_tg_id | 发起确认请求的一方 |
| counterparty_tg_id | 被请求确认的一方 |
| amount | 金额字符串 |
| currency | 币种 |
| status | pending / confirmed / rejected |
| created_at | 创建时间 |
| confirmed_at | 对方确认时间；拒绝时为 null |

---

### 3.9 风险趋势（按日风险分布）

**GET** `/v1/admin/stats/risk_trend`

- **Query**：`days`（可选，默认 7，范围 1–90）。  
- **Response 200**（JSON）：

```json
{
  "items": [
    {
      "date": "2025-02-10",
      "low": 10,
      "medium": 20,
      "high": 5,
      "extreme": 1
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| date | 日期 YYYY-MM-DD |
| low / medium / high / extreme | 当日查询结果为该风险等级的次数 |

用于绘制折线图/柱状图（横轴日期，纵轴数量，可多系列 low/medium/high/extreme）。

---

### 3.10 待发提醒列表（可选，只读）

**GET** `/v1/alert/pending`

- **Query**：`limit`（可选，默认 50，1–100）。  
- **Response 200**（JSON）：

```json
{
  "items": [
    {
      "id": 1,
      "subscriber_tg_id": 111,
      "target_tg_id": 222,
      "event_type": "new_report",
      "subscriber_lang": "zh",
      "created_at": "2025-02-16T12:00:00"
    }
  ]
}
```

仅供后台只读展示；「标记已发送」由 Bot 进程调用，前端无需实现。

---

## 四、页面与功能说明（需全部实现）

### 4.1 登录页

- 输入：管理密钥（单行，建议密文）。
- 操作：提交后请求 **GET /v1/admin/stats**，Header 带 `Authorization: Bearer <输入>`；200 则存密钥并跳转总览；403 提示「密钥无效或已失效」。
- 无注册、无忘记密码。

---

### 4.2 总览页（Dashboard）

- 请求 **GET /v1/admin/stats**，展示**全部 8 个**统计卡片：
  - 今日查询、总用户数、黑名单数、待处理举报数、待确认交易数、绑定地址数、开放流水用户数、流水快照条数。
- 导航入口：举报管理、申诉管理、黑名单、举报人列表、交易列表、风险趋势；（可选）待发提醒。

---

### 4.3 举报管理页

- 列表：请求 **GET /v1/admin/reports**，支持 Query：`status`（全部/pending/verified/rejected）、`limit`、`offset`。
- 表格列：ID、举报人 tg_id、被举报人 tg_id、分类(category)、理由(reason)、证据(evidence_data 可折叠或弹窗)、状态、创建时间。
- 分页：通过 `limit`、`offset` 实现上一页/下一页或页码。
- 行内操作：当状态为 `pending` 时显示「设为已核实」「设为驳回」：
  - 设为已核实：**PATCH /v1/admin/reports/{id}**，Body `{"status":"verified"}`。
  - 设为驳回：**PATCH /v1/admin/reports/{id}**，Body `{"status":"rejected"}`。
- 操作前可二次确认；操作后刷新列表或仅更新该行状态。

---

### 4.4 申诉管理页

- 顶部 Tab 或下拉：全部 / 待处理(pending) / 已处理(processed) / 已驳回(rejected)。
- 列表：**GET /v1/admin/feedback**，Query：`status`、`limit`。
- 表格列：ID、举报人 tg_id、被举报人 tg_id、理由、状态、创建时间。
- 行内操作：状态为 `pending` 时显示「通过」「驳回」：
  - 通过：**PATCH /v1/admin/feedback/{id}**，Body `{"status":"processed"}`。
  - 驳回：**PATCH /v1/admin/feedback/{id}**，Body `{"status":"rejected"}`。
- 操作前可二次确认；操作后刷新或更新该行。

---

### 4.5 黑名单页

- 当前后端**仅支持新增**，无黑名单列表接口。
- 表单：tg_id（必填，数字）、reason（选填）、source（选填，默认 `admin`）；提交 **POST /v1/admin/blacklist**。
- 成功且 `added: true` 提示「已加入黑名单」；`added: false` 提示「该用户已在黑名单中」。
- 若后续后端提供黑名单列表接口，可再增加列表与分页（以对接方补充为准）。

---

### 4.6 举报人列表页

- 请求 **GET /v1/admin/reporters?limit=100**。
- 表格列：举报人 tg_id、举报总数、已核实数、最后举报时间。
- 可按举报总数排序（接口已按举报数降序返回）；只读，无行内操作。

---

### 4.7 交易列表页

- 顶部 Tab 或下拉：全部 / 待确认(pending) / 已确认(confirmed) / 已拒绝(rejected)。
- 请求 **GET /v1/admin/transactions**，Query：`status`、`limit`。
- 表格列：ID、发起方 tg_id、对方 tg_id、金额、币种、状态、创建时间、确认时间。
- 只读，无行内操作。

---

### 4.8 风险趋势页

- 请求 **GET /v1/admin/stats/risk_trend?days=7**（或 30），days 可由用户选择（如 7/14/30）。
- 使用返回的 `items` 绘制图表：横轴 `date`，纵轴数量；可多系列：low、medium、high、extreme（建议不同颜色）。
- 图表形式不限（折线图、柱状图、堆叠面积图等）。

---

### 4.9 待发提醒页（可选）

- 只读列表：**GET /v1/alert/pending?limit=100**。
- 表格列：ID、订阅者 tg_id、目标 tg_id、事件类型、订阅者语言、创建时间。
- 无需「标记已发送」等操作。

---

## 五、错误处理与状态码

| HTTP 状态 | 含义 | 前端建议 |
|-----------|------|----------|
| 200 | 成功 | 按 response 更新 UI |
| 400 | 参数错误 | 提示参数错误，必要时展示 detail |
| 403 | 未提供或错误的 Bearer Token | 清除密钥，跳转登录页 |
| 404 | 资源不存在（如 report_id/feedback_id 无效） | 提示不存在并刷新列表 |
| 500 | 服务器错误 | 提示「请稍后重试」 |

---

## 六、技术建议

- **前端框架**：任选（Vue 3 / React / Svelte 等），建议 SPA；构建产物为静态 HTML/JS/CSS，便于 Nginx 或 FastAPI 挂载。
- **API Base URL**：通过环境变量或构建时注入，不写死域名。
- **跨域**：若前后端不同源，由对接方在 API 侧配置 CORS；开发阶段可用代理或同源部署。
- **错误提示**：网络错误或 4xx/5xx 时，在页面或 Toast 中给出明确提示。

---

## 七、交付与对接

### 7.1 开发方交付

| 交付项 | 说明 |
|--------|------|
| 前端工程源码 | 含依赖说明（如 package.json）、README（安装与构建命令） |
| 构建产物 | 如 `dist/` 或 `build/`，可被静态服务器直接托管 |
| 环境变量说明 | 至少含 API Base URL，示例值可为占位符 |

### 7.2 对接方提供

| 项 | 说明 |
|----|------|
| API 服务地址 | 如 `http://127.0.0.1:8001` 或生产域名 |
| 管理密钥 | `API_INTERNAL_KEY`，线下提供，不进入代码库 |
| 本文档 | 作为对接依据；若后端有增改，以补充说明或 OpenAPI 为准 |

### 7.3 对接验证建议

1. 登录 → 总览 8 项数据正确。  
2. 举报管理：列表、筛选、分页、审核（verified/rejected）成功。  
3. 申诉管理：列表、筛选、通过/驳回成功。  
4. 黑名单：提交加黑成功，并区分 added true/false 提示。  
5. 举报人列表、交易列表、风险趋势数据与图表正常展示。

---

**文档版本**：1.0（含举报列表与审核、运营统计扩展、举报人/交易/风险趋势等全部管理功能）  
若本项目新增或调整管理接口，以对接方提供的补充说明或 **GET /openapi.json** 为准。
