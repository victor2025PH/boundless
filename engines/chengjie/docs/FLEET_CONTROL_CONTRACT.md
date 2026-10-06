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
| 注册码 `enroll_code` | 运营在控制台生成，**12 位 Crockford base32（不含 I/L/O/U）、一次性、代码默认 15 min**。展示可分成 `XXXX-XXXX-XXXX`，兑码不区分大小写。生产配置里的 `enroll_code_ttl_min` 盖过这个默认，上线前要写成 15。库里尚未到期的旧 8 位数字码在到期前仍能兑。猜错 8 次 / 10 分钟锁该 IP，有效码在锁定期内不消耗。已吊销的 machine_id 拿码不会复活，改入待批准并标 `this machine was revoked`。 |
| `node_id` | `n_<12hex>`，主控分配。同一 `machine_id` 再次注册**复用 node_id、轮换 node_key**（旧 key 立即失效）。 |
| `node_key` | 机器级密钥，只在 enroll 响应里明文出现一次，节点本地存 `%ProgramData%\ChatX\fleet\agent.json`（或 `CHATX_FLEET_STATE_DIR`）；主控只存 sha256 哈希。 |
| 吊销 | `POST /api/fleet/nodes/{id}/revoke` 后该 key 所有请求 401，Agent 收到 401 停止轮询等待重新注册。 |

所有节点端点都带 `Authorization: Bearer <node_key>` 与 `X-Fleet-Proto: <proto_version>`。注册时 Bearer 里放的是注册码
（节点此时还没有 node_key；这条通道复用核心 CSRF 中间件的 Bearer 放行，**不是**关闭 CSRF）。
运营端点走核心登录态（`/fleet/console` 页面角色 master/admin/supervisor/viewer，由域 manifest `web.pages[].roles` 注册）。

## 3. 节点端点（主控实现）

### 3.1 注册

`POST /api/fleet/enroll` 协议版本仍是 1。三条路，旧的注册码路径不变：

1. **注册码**：Bearer = 注册码（或 body.code）。成功立刻签发 node_key。
2. **机房密钥** `rk_` + 256 bit（body.room_key，或 Bearer 以 `rk_` 开头）。次数与有效期内自动进组。库里只存 sha256，明文不进日志。
3. **都没有**（公开安装包）：记一条待批准，**不签发 node_key**，心跳 / 领任务一律 401。请求必须带本机生成的 `enroll_secret`（主控只存 sha256）。同一 machine_id 只有 secret 相符才复用申请；不相符就另开一条，控制台能看到两台。未鉴权请求里的 `label` / `group_name` 丢弃。批准时的分组只用管理员传入的值，缺省 `pending-default`。machine_id 已属于某个节点时，批准必须带 `confirm_rotate`，否则 409，文案是 `approving will rotate key of n_xxx`。

```json
{"code": "...", "machine_id": "m-…", "host_name": "GANZHI-176", "proto_version": 1,
 "agent_version": "0.3.1", "app_version": "1.0.38", "os": "Windows 11", "meta": {"python": "3.12.x"}}
→ 200 {"ok": true, "status": "active", "node_id": "n_…", "node_key": "<一次性明文>", "label": "…", "group_name": "…", "heartbeat_sec": 30}
→ 403 invalid_or_expired_code / code_and_machine_id_required     → 426 proto_incompatible
→ 429 rate_limited
```

无码时 body 不带 code / room_key（Bearer 用字面量 `pending`，只为过 CSRF）：

```json
→ 200 {"ok": true, "status": "pending", "request_id": "req_…", "pairing_code": "K7NQ2M", "expires_at": 0, "retry_after_sec": 15, "heartbeat_sec": 30}
```

`POST /api/fleet/enroll/poll` `{request_id, machine_id, enroll_secret}` 问结果。secret 不对一律 `unknown`。仍待批准 → `status=pending`。批准后在 15 分钟领取窗口内返回 node_key（窗口从 `decided_at` 起算，没来领也会擦掉明文）。拒绝 / 过期 → `status=rejected|expired`，没有 key。

机房密钥如果撞上**已吊销**的节点，或撞上**另一个分组**里的在用节点，不自动激活、不消耗次数，改走待批准（`requested_group` 是机房密钥上的组）。同组重装仍直接换 key。

运营：`GET /api/fleet/pending`，`POST /api/fleet/pending/{request_id}/approve|reject`。批准接口**不**返回 node_key。
机房：`POST /api/fleet/room-keys` 只在这一次响应里给出 `download_url`；`GET /fleet/dl/<token>` 返回一个小 zip（`Install.cmd` + `room.key`），不是公开下载页上的安装包。`POST /api/fleet/room-keys/{key_id}/revoke` 吊销。nginx 对 `/fleet/dl/` 关 access_log。

### 3.2 心跳

`POST /api/fleet/heartbeat`  默认每 30 s（服务端可通过响应 `heartbeat_sec` 调整）；`offline_after_sec` 120 s 没心跳 = offline。

```json
{"agent_version": "…", "proto_version": 1, "app_version": "…", "host_name": "…", "os": "…", "python": "…",
 "uptime_sec": 123,
 "instances": [{"name": "player", "domain": "player_care", "up": true, "accounts": 3, "player_overview": {…数字摘要…}}],
 "accounts": {"total": 3, "online": 2},
 "metrics": {…}, "fleet_health": {"by_state": {…}},
 "player_overview": {"contacts": 0, "active_7d": 0, "inbound_today": 0, "visible_today": 0, "gate_hits_today": 0, "gateway": "unconfigured"},
 "errors": [],
 "phones": [{"serial": "E6FY…", "state": "device", "model": "23106RN0DA", "transport": "usb"}], "phones_error": ""}
→ 200 {"ok": true, "server_time": 1790177662.0, "server_proto": 1, "has_tasks": false, "heartbeat_sec": 30}
```

只保留白名单顶层键（`src/fleet/protocol.py::HEARTBEAT_KEYS`），其余丢弃。**心跳只有数字与状态，永不含聊天原文、
手机号、联系人名。** `has_tasks=true` 提示 Agent 立刻 pull。

**0.3.6 只读手机清点**（`src/fleet/phones.py`）：心跳多两个键 `phones: [{serial, state, model, transport}]` 与
`phones_error`（空串 = 成功）。节点只跑 `adb devices -l`（参数列表、无 shell、5 s 超时）；先用本机 adb 服务的
`host:version` 只读握手确认服务在跑且与客户端同版本，否则不执行命令（避免 adb 自动起服务 / 版本不符时重启服务），
`phones_error` 写 `adb_server_not_running` / `adb_version_mismatch …` / `adb_not_found` / `adb_timeout` / `adb_exit_N`。
失败时 60 s 内有过成功结果就沿用那份。agent.json 可配 `phones_exclude`（默认空；精确 serial、`*` 结尾为前缀如 `192.168.0.50:*`、`model:CPH2653` 按 `devices -l` 的 model 字段）、`adb_path`、
`phones_enabled`；直播手机 `3B1F4KE5MS140P4X` 固定排除。主控把两个键存进 `nodes.last_heartbeat_json`，
`GET /api/fleet/nodes` 每行多给顶层 `phones` / `phones_error`（老节点 = `[]` / `""`）。不对手机做任何操作。

**0.3.8 机房自带 adb**（`src/fleet/adb_bundle.py`）：`find_adb` 在 `adb_path`、`C:\platform-tools\adb.exe` 之后、PATH 之前，再找安装目录 / `%ProgramData%\ChatX\platform-tools\adb.exe` / `%ProgramFiles%\ChatX Agent\platform-tools\adb.exe`。`C:\platform-tools` 仍优先，直播机继续用它自己的 adb。agent.json `adb_manage_server`（默认缺省 = 关，热加载）为 JSON `true` 时，只有「没有 server 在应答」且找到的是上面这份自带 adb，才会让它把 server 拉起来；直播机（`CHATX_FLEET_LIVE_STREAM` 或 `live-stream.flag`）即使写成 true 也不拉。端口不改，受保护手机 `3B1F4KE5MS140P4X` / `192.168.0.148` 仍不出现在清单里、也不能被操作。安装：`Install-ChatXAgent.ps1 -ManageAdbServer -PlatformToolsDir <含 adb.exe 的目录>`，或 `ChatXAgentSetup.exe /MANAGEADBSERVER=1`（打包前把 platform-tools 放到 `fleet_agent/platform-tools/`，见该目录 README.txt）。

**0.3.8 社交动作**（`phone_post` / `phone_like` / `phone_comment` / `phone_follow`，能力 `phone_flows_v1`）：默认关，需 `phone_ops_v1` 且主控已开远程操作。坐标在 `phone_ui_map.json`（或 agent.json `phone_ui_map`）。受保护手机 `3B1F4KE5MS140P4X` / `192.168.0.148` 直接拒绝，不跑 adb。直播机判定（`is_live_stream_host`）不变。核对与节奏默认关，避免改掉原来的点击序列和 0.5 s 最短间隔：`robust.verify`（默认 false）、`robust.retries`（默认 2，最多 3 次额外重试）、`robust.jitter_ms`（默认 `[0, 0]`，每端最多 2000 ms，加在每步原有最短间隔之上，不缩短它，也不放开该手机的锁）。agent.json `phone_flow_verify` / `phone_flow_jitter_ms` 热加载，可盖过文件；`phone_flow_verify` 只有 JSON `true` 才强制核对，抖动形状不对就仍用文件。打开核对后，标了 `expect` 的步骤做完再截一张图：画面没变 → `screen_not_reached`；颜色对不上 → `anchor_mismatch`。点开应用（`preflight.after_anchor`，随包是 `app_icon`）之后先看登录标记：登录页颜色在 → `not_logged_in`，已登录颜色不在 → `app_not_ready`，停在这一步，不再发帖或点赞。

**养号 / 私信 / 看短视频**（`phone_warmup` / `phone_dm` / `phone_watch`，能力 `phone_flows_v2`）：同一开关 `phone_flows_enabled`。打开后心跳同时带 `phone_flows_v1` 和 `phone_flows_v2`；关着时两项都不声明。仍走 `POST /api/fleet/nodes/{node_id}/phones/{serial}/social/{warmup|dm|watch}`，不要走通用 `/tasks`。坐标仍在各应用的 `phone_ui_map.json`（`warmup` / `dm` / `watch`）。受保护手机与直播机判定不变。payload：养号 `{app, scrolls?:1..8 默认 4, likes?:0..scrolls 默认 0}`；看视频 `{app, watches?:1..8 默认 3, likes?:0..watches 默认 0}`；私信 `{app, handle, text}`，文字规则与 `phone_text` 相同（ASCII）。回执只留次数和字数，不回私信原文、不回账号名。打开核对后，私信点开会话（`preflight.thread.after_anchor`，随包是 `dm_open`）再看会话标记，没有就 `thread_not_open`，不再输入私信正文。养号不发帖。停留只在节点上睡眠，不新增 adb 动词；每台手机锁、0.5 s 最短间隔、同时最多 2 路都不放宽。

**每日计划**（`src/fleet/phone_schedule.py` + `phone_schedule.json`，总开关默认关）：主控在活跃时间内把养号 / 发帖 / 点赞 / 评论 / 关注 / 看视频 / 私信排开，到点才入队已有的 `phone_*` 任务。不在节点上另起一套执行器。

计划字段（文件是默认值；库里的覆盖按 全局 → 节点 → 手机 叠上去，只写要改的键）：

```json
{"active_hours": {"start": "08:00", "end": "23:00"}, "tz_offset_min": 480,
 "jitter_sec": [90, 600], "min_gap_sec": 900,
 "apps": ["facebook", "instagram", "tiktok"], "pause_holds": ["post"],
 "daily": {"warmup": {"count": 2, "scrolls": 4, "likes": 1},
           "post": {"min": 1, "max": 2, "texts": [], "media": 0},
           "like": {"count": 3, "scrolls": 1}, "comment": {"count": 1, "texts": [], "scrolls": 0},
           "watch": {"count": 2, "watches": 3, "likes": 0},
           "follow": {"count": 0, "handles": []}, "dm": {"count": 0, "handles": [], "texts": []}}}
```

`tz_offset_min` 是本地时区相对 UTC 的分钟数（默认 480 = UTC+8），不跟服务器时区走。活跃窗口是左闭右开，不跨过午夜。`jitter_sec` 把每条动作放进窗口里均分的格子再抖动；`min_gap_sec` 是两条之间的最短间隔，排不下的记 `window_full`。时刻按 `日|节点|手机` 做种子，同一天不随墙钟重摇。当天还没有任何一条离开 `pending` 时，改次数会重排；已经发出去过就保持原槽（`texts` / `handles` 不参与指纹，改文案不会改时刻，派发当刻再取当前文案）。

`post.min`/`post.max` 默认 1–2（每台每天 1–2 条）。`texts` 为空就不会发，原因码 `post_text_missing`，直到操作员写入 ASCII 文案。评论、关注、私信同样：缺文案或账号名只挂起，不发明文案。关注和私信默认 0 条。

安全：

* 总开关在 `phone_schedule` 作用域 `global` 的 `enabled`。只有 JSON `true` 写成 1；缺省、`false`、`1` 都不开。关着时一轮调度什么都不写。
* 受保护手机 `3B1F4KE5MS140P4X` / `192.168.0.148` 不建可执行槽，只记 `protected_phone`。
* 心跳 `live_stream: true` 的节点当天不排，并取消还在排队、由计划发出的任务。`live_stream` 缺席或不是明确的 false（老 agent）按 `live_stream_unknown` 整天不排，等它明确报 false 再排。这只读心跳，不改变 `is_live_stream_host`。
* 全局 `paused`、该节点 `paused`，或心跳 `live_session: true`（agent.json `phone_live_session` 只有 JSON true，热加载）时，`pause_holds` 里的动作让路。默认只有 `post`。`"pause_holds": ["*"]` 全部让路。窗口内恢复后续上；窗口结束后记 `missed_while_paused`，不在夜里补发。
* 节点离线、手机不是 `device`、没开远程操作、缺能力：窗口内挂起，结束后记对应原因码。同一节点同时在队的远程手机任务不超过 `MAX_CONCURRENT`（2），同一部手机同时只排一条。0.5 秒最短间隔和每机锁仍只在节点执行时生效，调度器不睡眠。
* 执行记录和 `POST /api/fleet/schedule/tick` 的返回只有时间、种类、应用、状态、原因码、`task_id`。不回文案、不回账号名。

操作面：`GET/POST /api/fleet/schedule`（总开关、全局暂停、计划片段），`GET/POST /api/fleet/nodes/{node_id}/schedule`（这台电脑的暂停、计划和今天的槽），`POST /api/fleet/nodes/{node_id}/phones/{serial}/schedule`（一部手机的计划；受保护手机 403），`GET /api/fleet/schedule/runs?node_id=&day=`，`POST /api/fleet/schedule/tick`（立刻走一轮）。后台每分钟走一轮；`CHATX_FLEET_SCHEDULE_WATCH=0` 可停。控制台在已声明 `phone_flows_v1` 且远程操作已开的节点上多一个「每日计划」，点开才拉取，不在页面加载时请求。

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
| `stop_account` | 0 | `target.instance`, `target.phone` (E.164) 或 `target.account`；huoke 另收 `target.device_id` | 走本机 `POST /api/player-care/commands` 产 `stop` 指令，**入队时作废同号未拉走任务**；本机若登记了 `domain: huoke` 实例，同时 `POST /outreach/stop-account`（X-API-Key = 实例 auth_token）把号码/账号写进 huoke 全局 STOP 表、暂停对应设备的触达（指名 huoke 实例时只落 huoke） | commandbus 回执 + `huoke[]`（只有计数） |
| `upgrade` | 2 | `payload.version`, `payload.url`, `payload.sha256`（必填） | **Agent ≥0.2.0（打包版）执行**：下载到 `<state>/updates/`、校验 sha256、写换文件脚本、ack `done` 后退出，由计划任务 / systemd 拉起新版；源码运行 rejected `not_frozen`；缺 sha256 rejected `sha256_required` | `staged`, `version`, `exit: true` |
| `restart_instance` | 3 | `target.instance` | 仅当 Agent 本地 `instances[].restart_cmd` 显式配置才执行，否则 rejected | 退出码 |
| `push_config` | 4 | `payload.patch` | **rejected** `not_supported_in_agent_v1`（无安全的配置 patch 设计前不开） | — |
| `login_qr` | 5 | `target.instance`, `target.platform` / `payload.platform`（`^[a-z][a-z0-9_]{1,23}$`），`payload` 白名单：account_id/label/group/proxy_id/use_fingerprint/phone/mode | 调本机 `/api/platforms/{p}/login/start`，回二维码（只留 base64 位图 data URL）；控制台入口 `POST /api/fleet/nodes/{id}/login-qr`，见 FLEET_CONSOLE_QR_LOGIN.md | `login_id`, `qr`(data URL), `expires_in` |
| `login_status` | 5 | `target.instance`, `payload.login_id`（`^[A-Za-z0-9_.:-]{1,96}$`）, `payload.platform` | 调 `/api/platforms/{p}/login/{id}/status`；控制台入口 `POST /api/fleet/nodes/{id}/login-qr/{login_id}/status` | 登录状态、`reason_code`、`retry_after_sec`、刷新后的二维码 |
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
- **P1 已做（2026-09-24）**：Agent 服务化（`src/fleet/service.py`：Windows 计划任务 ONSTART/SYSTEM，Linux systemd；
  `run --service` 监督循环，未注册/被吊销/崩溃都退避重试不退出）；`upgrade` 落地（`src/fleet/updater.py`）；
  单文件打包 `fleet_agent/build_agent.py` → `chatx-agent.exe` + `manifest.json`；一键安装 / 卸载 PowerShell；
  操作端 CLI `src/fleet/admin.py`；主控落地包 `deploy/fleet/`（systemd / nginx / deploy / publish）。部署手册见 `docs/FLEET_DEPLOY.md`。
- **未做**：代码签名（exe 未签，SmartScreen 会拦一次）；`push_config`；
  `bd2026.cc` 上的实际部署 / 反代 include / 证书属改生产，脚本已备好，需单独授权后执行。
- 域名口径：`/fleet/` 主页与下载元数据从 `fleet_control.public_url` / `fleet_control.download.*` 读取，不写死；
  `download.manifest_url` 指向 `build_agent.py` 产出的 `manifest.json`（publish 后自动带出版本 / url / sha256，60 s 缓存）；
  既有 `branding.py`（ai26.sbs）与 updater / 授权 40+ 处硬编码 `bd2026.cc` 的收敛属全局改动，本轮不动，列入下一阶段。

## 7. 服务化 / 升级 / 部署口径（P1，2026-09-24）

- **URL 口径**：Agent 与操作端 CLI 一律 `<controller>/api/fleet/...`；公网 `controller = https://bd2026.cc/fleet`，
  nginx 把 `/fleet/api/` 剥前缀转到 `127.0.0.1:18798/api/`，`/fleet/` 原样转 `/fleet/`（`deploy/fleet/nginx-fleet.conf`）。长轮询 `wait ≤ 25 s`，反代 `proxy_read_timeout ≥ 60 s`。
- **Windows 服务化**：不引入 pywin32 / NSSM，用 `schtasks /SC ONSTART /RU SYSTEM /RL HIGHEST` 跑 `chatx-agent.exe --state-dir %ProgramData%\ChatX\fleet run --service`；
  `install-service / uninstall-service / service-status` 三个子命令；日志 `<state>/logs/agent.log`（轮转 5 MB×3）。
- **监督循环**（`service.supervise`）：未注册 → 每 30 s 重试；被吊销（401）→ 每 60 s 重建 Agent 重试（重注册后自动恢复）；异常 → 5 s 起指数退避到 300 s；`upgrade` 请求退出 → 返回码 3，交给计划任务 / systemd 拉起。
- **upgrade 流程**：主控 `POST /nodes/{id}/tasks {kind: upgrade, payload: manifest}`（`admin.py upgrade --manifest <url|path> [--group G | --node ID...] --yes`）→ Agent 校验下载 → 写 `<state>/updates/swap-*.ps1|.sh`（等旧进程退出 → 备份 `.bak` → 覆盖 → `schtasks /Run`）→ ack `done {exit:true}` → 退出。
  升级后换版脚本等新版写出 `last_heartbeat.json`（`at` 晚于换版时刻），超时（payload `health_timeout_sec`，默认 300，限 60–1800，0 = 关闭）就自动回滚：停任务、新 exe 另存 `.failed-<ver>`、`.bak` 拷回、若换版中挪走了旧状态目录则挪回、agent.json 丢失时从快照写回，再启动任务；过程记到状态目录上一级的 `fleet-upgrade.log`。
- **发布物**（`fleet_agent/build_agent.py --base-url https://bd2026.cc/downloads/fleet/`）：`chatx-agent.exe`、`.sha256`、`manifest.json {name, version, file, url, sha256, size, os, built_at, installer}`、两份 ps1；
  `deploy/fleet/publish_agent.ps1` 上传到官网 `public/downloads/fleet/`；主控 `download.manifest_url` 读同一份 manifest。
- **安全边界**：安装器不含任何主控管理凭据；注册码一次性；本机智聊 `auth_token` 优先从 `-ConfigPath` 读、`-AuthToken` 仅兜底；
  `service-status / status` 不输出 node_key；升级包 sha256 不符即删除、拒绝落盘。
