# 智控节点通道契约 Fleet Control（主控 ⇄ 节点 Agent）v1

状态：2026-09-23 P0 落地（主控 `domains/fleet_control` + Agent `src/fleet/agent.py`），本机联调已通。
配套文档：`docs/PLAYER_CARE_DOMAIN.md` §6e（看板跨实例汇总的需求来源）、huoke `docs/CHATX_COMMANDBUS_CONTRACT.md` §7（智拓侧）。

## 1. 为什么需要它

要在多台电脑上装智聊 / 智拓并由一台主控统一分派任务。受控机大多在 NAT / 家宽后面，主控**反向连不进去**
（本轮穿透三次断线即是反证），所以采用 **节点出站** 模型：节点 Agent 主动向主控 HTTPS 长轮询，主控永不主动连节点。

```
主控 https://bd2026.cc/fleet  (独立实例, 预设 config/presets/fleet_control.yaml, 端口 18798, 反代到 bd2026.cc)
        ▲ HTTPS / Authorization: Bearer <node_key>
节点 Agent (python -m src.fleet.agent run)
        │ 注册码 enroll → node_key → 心跳 → 长轮询领任务 → 本地 HTTP 调本机智聊实例 → 幂等 ack
本机智聊实例(们)  http://127.0.0.1:18797 …  （/api/accounts/fleet-health、/api/player-care/overview、login、commandbus）
```

沿用已有模型，不另造：任务队列 = player_care commandbus 的 pull/ack 幂等；机器身份 = `src/licensing` 的 machine_id；
健康摘要 = `/api/accounts/fleet-health` + `/api/player-care/overview`。

## 2. 身份与鉴权

| 对象 | 说明 |
|---|---|
| `machine_id` | `m-<16hex>`，`src/fleet/identity.py`：优先 licensing 指纹 → Windows MachineGuid / Linux machine-id → 持久随机 UUID。同一机器重装 Agent 仍是同一 `machine_id`。 |
| 注册码 `enroll_code` | 运营在控制台生成，**一次性、默认 60 min 过期**，可预设 label / group。 |
| `node_id` | `n_<12hex>`，主控分配。同一 `machine_id` 再次注册**复用 node_id、轮换 node_key**（旧 key 立即失效）。 |
| `node_key` | 机器级密钥，只在 enroll 响应里明文出现一次，节点本地存 `%ProgramData%\ChatX\fleet\agent.json`（或 `CHATX_FLEET_STATE_DIR`）；主控只存 sha256 哈希。 |
| 吊销 | `POST /api/fleet/nodes/{id}/revoke` 后该 key 所有请求 401，Agent 收到 401 停止轮询等待重新注册。 |

所有节点端点都带 `Authorization: Bearer <node_key>` 与 `X-Fleet-Proto: <proto_version>`。注册时 Bearer 里放的是注册码
（节点此时还没有 node_key；这条通道复用核心 CSRF 中间件的 Bearer 放行，**不是**关闭 CSRF）。
运营端点走核心登录态（`/fleet/console` 页面角色 master/admin/supervisor/viewer，由域 manifest `web.pages[].roles` 注册）。

## 3. 节点端点（主控实现）

### 3.1 注册

`POST /api/fleet/enroll`  Bearer = 注册码（或 body.code）

```json
{"code": "...", "machine_id": "m-…", "host_name": "GANZHI-176", "proto_version": 1,
 "agent_version": "0.1.0", "app_version": "1.0.38", "os": "Windows 11", "meta": {"python": "3.12.x"}}
→ 200 {"ok": true, "node_id": "n_…", "node_key": "<一次性明文>", "label": "…", "group_name": "…", "heartbeat_sec": 30}
→ 403 invalid_or_expired_code / code_and_machine_id_required     → 426 proto_incompatible
```

### 3.2 心跳

`POST /api/fleet/heartbeat`  默认每 30 s（服务端可通过响应 `heartbeat_sec` 调整）；`offline_after_sec` 120 s 没心跳 = offline。

```json
{"agent_version": "…", "proto_version": 1, "app_version": "…", "host_name": "…", "os": "…", "python": "…",
 "uptime_sec": 123,
 "instances": [{"name": "player", "domain": "player_care", "up": true, "accounts": 3, "player_overview": {…数字摘要…}}],
 "accounts": {"total": 3, "online": 2},
 "metrics": {…}, "fleet_health": {"by_state": {…}},
 "player_overview": {"contacts": 0, "active_7d": 0, "inbound_today": 0, "visible_today": 0, "gate_hits_today": 0, "gateway": "unconfigured"},
 "errors": []}
→ 200 {"ok": true, "server_time": 1790177662.0, "server_proto": 1, "has_tasks": false, "heartbeat_sec": 30}
```

只保留白名单顶层键（`src/fleet/protocol.py::HEARTBEAT_KEYS`），其余丢弃。**心跳只有数字与状态，永不含聊天原文、
手机号、联系人名。** `has_tasks=true` 提示 Agent 立刻 pull。

### 3.3 领任务（长轮询）

`GET /api/fleet/tasks/pull?limit=20&wait=25`  `wait` 上限 25 s，队列为空时挂到超时。

```json
→ 200 {"ok": true, "server_proto": 1, "tasks": [
   {"task_id": "t_…", "kind": "ping", "node_id": "n_…", "target": {}, "payload": {"echo": "hello"},
    "ttl_sec": 900, "created_at": 1790177600.0, "expires_at": 1790178500.0, "proto_version": 1}]}
```

规则：按 `priority ASC, created_at ASC` 出队；一旦拉走状态 `queued→pulled`，不会重复下发；过期未拉走的自动 `expired`。
节点 `proto_version < MIN_PROTO_VERSION` 时只下发 `ping` / `upgrade`（升级引导），其余任务留在队列不下发。

### 3.4 回执

`POST /api/fleet/tasks/ack`

```json
{"task_id": "t_…", "status": "done|failed|rejected", "detail": "pong", "result": {…结构化结果, 可选…}}
→ 200 {"ok": true, "known": true, "status": "done"}
```

幂等：终态（done/failed/rejected/expired/cancelled）任务再 ack 不改状态；`task_id` 不属于本 `node_id` 或未知 →
`known:false` 但仍 200（fail-soft，Agent 不重试）。`result` 只存数字 / 状态 / 二维码 data URL，不存原文。

## 4. 任务信封与 kind

| kind | 优先级 | payload / target | Agent v1 行为 | result |
|---|---|---|---|---|
| `stop_account` | 0 | `target.instance`, `target.phone` (E.164) 或 `target.account` | 走本机 `POST /api/player-care/commands` 产 `stop` 指令，**入队时作废同号未拉走任务** | commandbus 回执 |
| `upgrade` | 2 | `payload.version`, `payload.url`, `payload.sha256` | v1 **rejected** `not_supported_in_agent_v1`（等安装器/自更新落地） | — |
| `restart_instance` | 3 | `target.instance` | 仅当 Agent 本地 `instances[].restart_cmd` 显式配置才执行，否则 rejected | 退出码 |
| `push_config` | 4 | `payload.patch` | v1 **rejected** `not_supported_in_agent_v1` | — |
| `login_qr` | 5 | `target.instance`, `target.platform` (whatsapp/telegram), `payload.proxy_id?`, `payload.use_fingerprint?` | 调本机 `/api/platforms/{p}/login/start`，回二维码 | `login_id`, `qr`(data URL), `expires_in` |
| `login_status` | 5 | `target.instance`, `payload.login_id` | 调 `/api/platforms/{p}/login/{id}/status` | 登录状态 |
| `ping` | 7 | `payload.echo?` | 本地回 pong | agent/app 版本、machine_id、host、time、echo |
| `account_health` | 8 | `target.instance?` | `GET /api/accounts/fleet-health`（总数/在线/生命周期/红黄绿分） | 数字摘要 |
| `pull_overview` | 9 | `target.instance?` | `GET /api/player-care/overview`（联系人/阶段/明用/数字闸/网关健康） | 看板 JSON |

`ttl_sec` 默认 15 min，上限 24 h；`expires_at = created_at + ttl_sec`，Agent 拉到已过期任务直接 ack `rejected expired_on_arrival`。
运营入口：`POST /api/fleet/nodes/{node_id}/tasks {"kind", "target", "payload", "ttl_sec"}`；吊销/取消：`POST /api/fleet/tasks/{id}/cancel`。

## 5. 语义与铁律

1. **节点只出站**：主控不存节点地址、不反向连；节点 401 即停、426 即提示升级。
2. **一机一 key**：key 落盘只在节点本机，主控只存哈希；泄露即 revoke，重注册轮换。
3. **无原文上云**：心跳 / result 只允许数字、状态、版本、二维码；聊天内容、手机号列表留在节点本机的智聊实例里。
4. **幂等 + TTL**：任务不重复下发、ack 幂等、过期自动作废；STOP 类优先且作废同号排队任务（与 commandbus §4.5 同一语义）。
5. **执行权在本机智聊**：Agent 不直接碰 WhatsApp / 文件，只调本机智聊 HTTP；`stop_account` 最终仍由智聊 commandbus → 手机侧执行。
6. **fail-soft**：主控不可达 → Agent 指数退避 2–60 s 继续跑本机实例不受影响；store 不可用 → 主控端点 503。
7. **版本门**：`PROTO_VERSION` 只在破坏性变更时 +1；主控保留 `MIN_PROTO_VERSION` 以下节点的 `ping`/`upgrade` 通道。

## 6. 现状与边界（2026-09-23）

- 主控：`domains/fleet_control/`（manifest / defaults / web/routes.py / 两张页面 `/fleet/`、`/fleet/console`），
  存储 `src/fleet/store.py`（SQLite：nodes / enroll_codes / node_tasks / node_heartbeats），预设 `config/presets/fleet_control.yaml`。
- Agent：`src/fleet/agent.py`（enroll / add-instance / run [--once] / status / heartbeat），标准库 urllib，无第三方依赖。
- 测试：`tests/test_fleet_control.py`（store / 协议 / 路由 / Agent / CLI，27 例）。
- 本机联调：主控 18798 + player 18797 → enroll → heartbeat（实例 up、看板摘要）→ ping / account_health / pull_overview
  done → ack → revoke 后 401，全通。
- **未做**（P1+）：Windows 服务化 / 安装器打包 / 自动更新 / 代码签名（`upgrade`、`push_config` 因此 v1 拒绝）；
  控制台集中扫码只做了 `login_qr` 任务层；`bd2026.cc` VPS 部署、反代、正式证书属改生产，需单独授权后执行。
- 域名口径：`/fleet/` 主页与下载元数据从 `fleet_control.public_url` / `fleet_control.download.*` 读取，不写死；
  既有 `branding.py`（ai26.sbs）与 updater / 授权 40+ 处硬编码 `bd2026.cc` 的收敛属全局改动，本轮不动，列入下一阶段。
