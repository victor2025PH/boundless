# Fleet 阶段2 · 小组契约样例（ZHUJI-117 本机冒烟）

- 时间（CST/UTC+8）：2026-09-24 07:56:11
- 机器：ZHUJI-117（machineId `1f952fb2-e492-4d91-b7a6-e5ba8531bd93`）
- 仓库 worktree：`D:\boundless-fleet-p2` 分支 `feat/chengjie-player-care-fleet` @ `33c9a052` + 本地未提交 WorkingDirectory 修复
- 主控：`http://127.0.0.1:18798`（`--init fleet_control`，**未**连生产 / bd2026.cc）
- 实例 stub：`http://127.0.0.1:18790`
- 冒烟脚本：`D:\boundless-fleet-p2\engines\chengjie\scripts\fleet_local_smoke.ps1`
- 密钥目录：`D:\tmp\fleet_local_smoke\secrets.env`（**禁止 git add**）

---

## A. @智拓触达 —— 实例健康端点样例

> 来源：本机 stub 实测 + 契约 `docs/FLEET_CONTROL_CONTRACT.md` / `docs/FLEET_GROK_HANDOFF.md` §3F。
> 说明：chengjie 引擎**当前没有**原生 `GET /api/ping`（全仓检索仅 handoff 文档提到）；智拓若挂 fleet，需自备与 stub 同形的端点。`GET /api/accounts/fleet-health` 在智聊侧已有实现（`src/web/routes/unified_inbox_account_routes.py`），下方 stub 形状对齐 Agent `reduce_fleet_health` 消费字段。

### A1. `GET /api/ping`

**请求**

```
GET /api/ping HTTP/1.1
Host: 127.0.0.1:18790
Authorization: Bearer <instance_web_admin_auth_token>
```

**响应 200**（stub 实测）

```json
{"ok": true, "service": "fleet-smoke-stub", "role": "instance", "version": "smoke-1", "uptime_sec": 1}
```

**响应 401**（无/错 Bearer，stub）

```json
{"ok": false, "error": "unauthorized"}
```

**约定（智拓对接）**

| 项 | 约定 |
|---|---|
| 鉴权 | `Authorization: Bearer <token>`（与实例 `web_admin.auth_token` 同口径） |
| 成功 | HTTP 200，JSON 含 `ok: true`；可带 `version` / `uptime_sec` 等非敏感字段 |
| 失败 | 401 unauthorized；其它 5xx 由 Agent 记入心跳 `errors[]`（字符串，无原文） |
| 禁止 | 聊天原文、手机号列表、密钥 |

### A2. `GET /api/accounts/fleet-health`

**请求**

```
GET /api/accounts/fleet-health HTTP/1.1
Host: 127.0.0.1:18790
Authorization: Bearer <instance_web_admin_auth_token>
```

**响应 200**（stub 实测，数字摘要）

```json
{"ok": true, "total": 2, "lifecycle": {"active": 1, "warming": 1, "pending": 0, "restricted": 0, "banned": 0, "offline": 0}, "fleet": {"state": "green", "by_state": {"green": 1, "yellow": 1, "red": 0}, "counts": {"ok": 1, "warn": 1, "bad": 0}}, "accounts": [{"platform": "whatsapp", "account_id": "acc_smoke_1", "stage": "active", "quota": {"used": 3, "cap": 40, "auto_cap": 20, "reserve": 5, "auto_blocked": false}, "profile_churn_7d": 0}, {"platform": "telegram", "account_id": "acc_smoke_2", "stage": "warming", "quota": {"used": 1, "cap": 15, "auto_cap": 8, "reserve": 3, "auto_blocked": false}, "profile_churn_7d": 1}], "profile_churn_hot": []}
```

**Agent 裁剪后的任务 result**（本机 `account_health` 任务 `t_934193450e1c4b45` → `done`）关键字段：

| 字段 | 含义 |
|---|---|
| `total` / `online` | 账号总数 / stage∈{active,warming} 计数 |
| `lifecycle` | pending/warming/active/restricted/banned/offline 分布 |
| `fleet.state` / `fleet.by_state` | 机群灯色与 green/yellow/red 计数 |
| `accounts[]` | 可选：platform/account_id/stage/quota/profile_churn_7d |

**状态码**

| 码 | 含义 |
|---|---|
| 200 | 成功；body `ok: true` + 摘要 |
| 401 | 鉴权失败 |
| 404 | 路径不存在（智拓未实现时） |
| 5xx | 实例异常；Agent 将该任务标 `failed` |

智聊真源形状另见 `fleet_overview()`（`src/skills/account_signals.py`）→ 路由包装 `{"ok": true, **overview}`。

---

## B. @智聊通 —— handoff / 隔离 样例与错误码

### B1. Handoff（Messenger → WA/TG）

**文档**：`docs/PLAYER_CARE_DOMAIN.md` §6c；实现 `domains/player_care/handoff.py` + 核心 `src/contacts/handoff.py`。

**行为摘要（非 HTTP 对外 API）**

1. 入站消息抓 6 位 handoff 短码 → 本实例 `contacts.db` → 可选 `player_care.handoff.source_db_path`（story 库）。
2. 同库命中：`MergeService.apply_token_merge` relink ChannelIdentity。
3. 跨库命中：只消费 token + `handoff_consumed` 事件 + 漏斗推 LINE_ENGAGED，不 relink。
4. hooks 失败**全吞**（不影响回复）；合并当轮注入 `HANDOFF_CONTEXT_BLOCK`。

**核心异常类（Python，非 HTTP 状态码）** — `src/contacts/handoff.py`：

| 异常 | 语义 |
|---|---|
| `TokenNotFound` | 码不存在 / 形状非法 |
| `TokenExpired` | 超过 TTL（默认 72h） |
| `TokenAlreadyConsumed` | 已被消费（含竞态） |
| `TokenRevoked` | 已撤销 |
| `TokenError` | 基类 / 插入重试耗尽等 |

> **文档缺口**：player_care **没有**对外 REST「拒绑/handoff 失败」错误码表；失败不回聊天方结构化码，只打日志。若智拓 executor 需要 HTTP 错误码契约，需在 `docs/PLAYER_CARE_DOMAIN.md` 或 huoke commandbus 契约补一节（路径已标明，当前缺）。

### B2. 联系人合并决策（隔离）

**实现**：`src/contacts/merge.py` + `src/contacts/models.py`

| decision | 阈值（代码常量） |
|---|---|
| `auto_merge` | confidence ≥ `MERGE_AUTO_THRESHOLD` (**0.90**) |
| `manual_review` | ≥ `MERGE_REVIEW_THRESHOLD` (**0.60**) |
| `keep_isolated` | < 0.60 → **保持隔离，不合并** |

> 注：`merge.py` 文件头注释仍写 0.85，以 `models.py` 常量为准（文档漂移，算小缺口）。

### B3. 账号身份隔离（换人检疫）

**实现**：`src/integrations/account_identity.py`；路由 `src/web/routes/unified_inbox_account_routes.py`。

**登录汇入返回**（内部）

```json
{"changed": true, "prev_accounts": ["old_acct"], "quarantined": true}
```

**运营只读清单** `GET /api/admin/account-identity/pending` → 200

```json
{
  "ok": true,
  "pending": [ {"platform": "whatsapp", "account_id": "…", "identity_pending": true} ],
  "recent": [ {"ts": 0, "platform": "whatsapp", "account_id": "…", "reason": "quarantined", "detail": "prev=…"} ],
  "fleet_candidates": []
}
```

**人工转正** `POST /api/admin/account-identity/confirm`

请求 body：

```json
{"platform": "whatsapp", "account_id": "acc_xxx", "persona_id": ""}
```

| 结果 | HTTP | body |
|---|---|---|
| 成功 | 200 | `{"ok": true, "persona_id": "…"}` |
| 缺字段 | 400 | 本地化 `err.ws.field_required` |
| 账号不存在 / confirm 失败 | 404 | 本地化 `err.ws.account_not_found` |
| 无运营写权限 | （中间件）非 manager 角色拒绝 | |

隔离态语义：`identity_pending=true` → AI 只拟稿不自发、不自动挂人设，直到 confirm。

### B4. Gateway lookup「拒绑」

player_care 网关客户端**一律** `bind=false`（`domains/player_care/gateway.py`），聊天里的 uid 不写网关别名表。这是客户端策略，不是 HTTP 错误码。

网关 HTTP 常见码（探针/文档）：200 found / 404 not_found / 401 bad_key；另有 `multi:true` 多账号歧义分支（见 `docs/PLAYER_CARE_DOMAIN.md` §3）。

---

## C. 冒烟结果 / 分支与改动

### C1. pytest

```
cd D:\boundless-fleet-p2\engines\chengjie
python -m pytest tests/test_fleet_control.py tests/test_fleet_p1_service_updater.py -q
→ 55 passed in ~22s
```

（原目标 51–53；本轮补 3 例 WorkingDirectory 相关后为 55。）

### C2. 本机闭环（127.0.0.1）

| 步骤 | 结果 |
|---|---|
| 主控 `/health` | 200（degraded 占位，可接受） |
| enroll | `node_id=n_db2dd7c86c86` `machine_id=m-bc00c74631a0ff9b` |
| heartbeat | `ok: true` |
| 任务 ping | **done** / detail=pong / echo=zhuji117-smoke |
| 任务 account_health | **done** / detail=ok / total=2 online=2 |

任务 dump：`D:\tmp\fleet_local_smoke\logs\tasks_dump.json`

### C3. 本机分支 / 未提交改动

- 分支：`feat/chengjie-player-care-fleet` tracking `origin/feat/chengjie-player-care-fleet`
- HEAD：`33c9a052`（远端已有 P1）；**云端 VM 上的 WorkingDirectory 修复未在远端**，本机已复刻：
  - `engines/chengjie/src/fleet/service.py`：`service_workdir()` + `WorkingDirectory=` + install 传入
  - `engines/chengjie/tests/test_fleet_p1_service_updater.py`：Linux install / frozen 省略 / unit 带 working_dir
- **未 push**；未改 `protocol.py`；密钥不在仓内
- 新增（未要求提交）：`scripts/fleet_local_smoke.ps1`、`scripts/fleet_smoke_instance_stub.py`、`scripts/README_fleet_local_smoke.md`

### C4. 阻塞项

| 项 | 状态 |
|---|---|
| 云额度 / 改生产 | 已规避（本机模拟） |
| `/api/ping` 智聊原生实现 | **缺口**：仅 stub + 文档要求智拓自备；不影响本机 ping **任务**（任务 ping 是 Agent 本地 pong） |
| handoff HTTP 错误码表 | **文档缺口**（见 B1） |
| merge 阈值注释 0.85 vs 常量 0.90 | 小文档漂移 |
| 依赖/缺代码 | 无；冒烟绿 |

---

## 复跑

```powershell
powershell -ExecutionPolicy Bypass -File D:\boundless-fleet-p2\engines\chengjie\scripts\fleet_local_smoke.ps1
```

结束进程可用不加 `-KeepRunning`（默认清理 18798/18790）。当前会话若仍占端口，先停再跑。

---

## D. 阶段3 · `pull_overview` / `GET /api/player-care/overview`（ZHUJI-117 本机）

- 时间（CST/UTC+8）：2026-09-24 08:05:43
- 冒烟：`fleet_local_smoke.ps1` → ping / account_health / **pull_overview** 均 **done**
- 任务样例：`t_5bd326d581784d12` kind=`pull_overview` status=`done` detail=`ok` node=`n_db2dd7c86c86`
- stub 端点：`GET http://127.0.0.1:18790/api/player-care/overview`（Bearer = 实例 auth_token）
- 对齐：`docs/FLEET_CONTROL_CONTRACT.md` §任务表 `pull_overview`；`docs/PLAYER_CARE_DOMAIN.md` §6d/6e；Agent `reduce_player_overview`

### D1. 请求

```
GET /api/player-care/overview HTTP/1.1
Host: 127.0.0.1:18790
Authorization: Bearer <instance_web_admin_auth_token>
```

### D2. 响应 200（stub 实测，数字摘要，**无聊天原文**）

```json
{
  "ok": true,
  "ts": 1790208330,
  "day": "2026-09-24",
  "contacts": {
    "total": 42,
    "with_phone": 28,
    "handoff": 3,
    "active_7d": 11,
    "by_account": {
      "acc_smoke_1": 25,
      "acc_smoke_2": 17
    }
  },
  "stages": {
    "order": [
      "new_friend",
      "chatting",
      "mentioned_game",
      "registered",
      "depositing",
      "active",
      "dormant"
    ],
    "totals": {
      "new_friend": 8,
      "chatting": 12,
      "mentioned_game": 5,
      "registered": 7,
      "depositing": 2,
      "active": 6,
      "dormant": 2
    },
    "by_account": {
      "acc_smoke_1": {
        "new_friend": 5,
        "chatting": 7,
        "mentioned_game": 3,
        "registered": 4,
        "depositing": 1,
        "active": 4,
        "dormant": 1
      },
      "acc_smoke_2": {
        "new_friend": 3,
        "chatting": 5,
        "mentioned_game": 2,
        "registered": 3,
        "depositing": 1,
        "active": 2,
        "dormant": 1
      }
    }
  },
  "today": {
    "inbound": 14,
    "lookups": 9,
    "found": 4,
    "visible": 3,
    "gate_hits": 2,
    "new_profiles": 1,
    "stage_ups": 2,
    "accounts": [
      {
        "account": "acc_smoke_1",
        "inbound": 9,
        "visible": 2,
        "gate_hits": 1
      },
      {
        "account": "acc_smoke_2",
        "inbound": 5,
        "visible": 1,
        "gate_hits": 1
      }
    ]
  },
  "gateway": {
    "status": "ok",
    "enabled": true,
    "url": "http://127.0.0.1:19999/gw",
    "key_set": true,
    "key_env": "GATEWAY_KEY",
    "timeout_sec": 5,
    "lookups_total": 120,
    "last_lookup_at": 1790208240,
    "recent_24h": {
      "total": 9,
      "found": 4,
      "not_found": 4,
      "errors": {
        "timeout": 1
      }
    }
  },
  "sync": {
    "enabled": true,
    "interval_min": 15,
    "last": {
      "ts": 1790208030,
      "ok": true,
      "upserted": 2
    }
  },
  "commandbus": {
    "enabled": true,
    "stats": {
      "queued": 0,
      "pulled": 1,
      "acked": 1,
      "failed": 0
    }
  },
  "profile_enabled": true,
  "notes": []
}
```

### D3. Agent 心跳裁剪（`reduce_player_overview`，非 full）

契约心跳顶层 `player_overview` 形状：

```json
{
  "contacts": 42,
  "active_7d": 11,
  "inbound_today": 14,
  "visible_today": 3,
  "gate_hits_today": 2,
  "gateway": "ok"
}
```

| 字段 | 来源 |
|---|---|
| `contacts` | `contacts.total` |
| `active_7d` | `contacts.active_7d` |
| `inbound_today` | `today.inbound` |
| `visible_today` | `today.visible` |
| `gate_hits_today` | `today.gate_hits` |
| `gateway` | `gateway.status`（字符串：unconfigured/idle/ok/degraded/down） |

### D4. 任务 `pull_overview` ack result（`reduce_player_overview(..., full=True)`）

Agent 回执：`{"instance": "<name>", "overview": <完整看板去掉 ok>}`。

关键字段（与 D2 同形，已去掉顶层 `ok`）：`ts` / `day` / `contacts` / `stages` / `today` / `gateway` / `sync` / `commandbus` / `profile_enabled` / `notes`。

**禁止**：聊天原文、手机号列表、网关密钥明文（仅 `key_set` / `key_env` 名）。

### D5. 状态码

| 码 | 含义 |
|---|---|
| 200 | 成功；`ok: true` + 数字摘要 |
| 401 | 鉴权失败 |
| 404 | 路径不存在（非 player_care 实例） |
| 5xx | 实例异常；任务标 `failed` |

### D6. 阶段3 冒烟结果

| 步骤 | 结果 |
|---|---|
| enroll / heartbeat | ok |
| ping | **done** |
| account_health | **done** |
| pull_overview | **done** / detail=ok / instance=stub |

复跑：

```powershell
powershell -ExecutionPolicy Bypass -File D:\boundless-fleet-p2\engines\chengjie\scripts\fleet_local_smoke.ps1
```

---

## E. 阶段5 · 获客链最小联调（handoff / commandbus / 自然流缺口）

> ZHUJI-117 worktree `D:\boundless-fleet-p2`；**不动生产、不 clone、不 push**；不起真 WhatsApp；本阶段只挂智聊形状，智拓不真挂进程。
> 文档入口：`docs/PLAYER_CARE_DOMAIN.md` §6b/§6c。`CHATX_COMMANDBUS_CONTRACT.md` 在智拓仓（本 worktree 的 `engines/huoke/docs/` **未收录**该文件 → 以 PLAYER_CARE §6b 摘要 + 本仓 `domains/player_care/commandbus.py` 为准）。

### E1. 测了什么 / 结果

| # | 项 | 方法 | 结果 |
|---|---|---|---|
| 1 | handoff token **成功消费**（同库 relink + 跨库 source_db 只消费不 relink） | `tests/test_player_care_handoff.py` | **绿** |
| 2 | 四类失败 `TokenNotFound` / `TokenExpired` / `TokenAlreadyConsumed` / `TokenRevoked`：`consume` 抛类；`try_consume_from_text` / player_care `locate` **仅日志+异常类名**（不虚构 HTTP） | `tests/test_contacts_handoff.py`（含 `TestLogOnlyFailures`）+ `test_player_care_handoff.py` locate 日志例 | **绿** |
| 3 | commandbus `stop` 入队 → pull（stop 优先）→ ack 幂等；**同号未拉走** reengage/note → `cancelled` | `tests/test_player_care_commandbus.py`（`test_stop_first_cancels_pending…`、`test_routes_pull_ack…`、hook STOP） | **绿** |
| 4 | 自然流缺 `click_id` 不挡进线（可空 + `content_id`/`post_url`；归因不参与绑号） | 全仓检索 + 冒烟脚本缺口检查 | **缺口**（见 E3） |

冒烟脚本（不并进阶段2/3 主控脚本，避免拖起 18798）：

```powershell
powershell -ExecutionPolicy Bypass -File D:\boundless-fleet-p2\engines\chengjie\scripts\fleet_stage5_huoke_smoke.ps1
```

等价 pytest：

```
cd D:\boundless-fleet-p2\engines\chengjie
$env:PYTHONPATH=""; python -m pytest tests/test_contacts_handoff.py tests/test_player_care_handoff.py tests/test_player_care_commandbus.py -q -p no:cacheprovider
```

### E2. 给智聊通的对表摘要

| 能力 | 智聊侧现状 | 联调态 |
|---|---|---|
| Handoff 短码签发/消费 | `src/contacts/handoff.py` + `domains/player_care/handoff.py`；hooks 失败全吞 | 本机 pytest **绿**（tmp db） |
| 失败可观测 | 日志字段带 `TokenExpired` 等**类名**；**无** REST 拒绑 HTTP 码表（与 §B1 一致，按约定不虚构） | 日志断言 **绿** |
| commandbus pull/ack | `GET /api/commandbus/pull`、`POST /api/commandbus/ack`；STOP 先发并作废同号 queued | 出箱+路由测试 **绿**；手机 poll 可用 mock（测试内 pull），未启真机 |
| 会话硬卡 `lead_id` + `handoff_target` + 冷启动隔离 | 智拓 lead_mesh / 会话层约定；本仓 player_care 用画像 `profile_key` + handoff 字段 | **未在本阶段测**（属智拓会话硬卡；建议阶段外对表） |
| 自然流 `click_id` 可空 | **本仓无 `click_id` 符号** | **红/缺口** → E3 |

### E3. 缺口与建议补测点

1. **`click_id` / 自然流进线**：`engines/chengjie`（及本 worktree `engines/huoke` 可见树）**零命中** `click_id`。建议智拓侧（或 story 进线适配层）补：
   - 进线 payload：`click_id` 缺省或 `""` + 有 `content_id` 和/或 `post_url` → **仍建会话**，不 4xx；
   - 断言归因字段可写台账但 **不参与** ChannelIdentity / 账号绑号；
   - 负例：缺 `lead_id` / `handoff_target` 才硬拒（与小组会话硬卡一致）。
2. **`CHATX_COMMANDBUS_CONTRACT.md`**：不在本 worktree；联调以 PLAYER_CARE §6b + 本仓实现为准。若需逐字段对表，从智拓仓拷只读副本到 `engines/huoke/docs/`（勿改协议常量）。
3. **真 WhatsApp / 真手机 poll**：本阶段刻意不做；e2e 已有 `scripts/ops/chatx_commandbus_e2e.py` 路径（文档 §6b），需 player 实例 + 智拓 `chatx_command_poll` 另排。

### E4. 本阶段改动文件（未要求 push）

| 路径 | 说明 |
|---|---|
| `domains/player_care/handoff.py` | `_token_usable`：Expired/Consumed/Revoked 打日志+类名（NotFound 仍静默） |
| `tests/test_contacts_handoff.py` | `TestLogOnlyFailures` caplog 断言 |
| `tests/test_player_care_handoff.py` | locate 四类失败日志断言 |
| `scripts/fleet_stage5_huoke_smoke.ps1` | 阶段5一键冒烟 |
| `scripts/README_fleet_local_smoke.md` | 链到阶段5 |
| `docs/FLEET_STAGE2_CONTRACT_SAMPLES.md` | 本 §E |

未改：`src/fleet/protocol.py`、生产配置、密钥、bd2026.cc。
