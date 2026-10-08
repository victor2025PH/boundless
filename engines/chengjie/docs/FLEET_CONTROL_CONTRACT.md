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

**远程打开机房 adb**（仍是 0.3.8，直播机一律拒绝）：`upgrade` 在 `setup_url` 与 `setup_sha256` 都非空时不再换单个 exe，改为下载安装包、校验 sha256（64 位十六进制，且必须是 https、不能带账号）、再静默执行 `ChatXAgentSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`。`manage_adb_server` 只有 JSON `true` 才追加 `/MANAGEADBSERVER=1`。agent.json 里已经有 `node_key` 时再加 `/KEEPIDENTITY=1`（安装器转给 `bootstrap.ps1 -KeepIdentity`，不跑 `identity --reinstall`，节点保持原来的 `n_<id>`）。没有 node_key 的新装不带这个开关。校验不符或直播机（`CHATX_FLEET_LIVE_STREAM` / `live-stream.flag`）拒绝执行。没有这两个字段时行为与原来的 exe 升级相同。nginx 对 `/downloads/fleet/` 的版本文件放行 `manifest-<ver>.json`，与带版本号的 exe / sha256 一起从镜像目录提供。已经装好 platform-tools 的节点用任务 `enable_phone_adb`（空 payload）：只对自带 adb 打开 `adb_manage_server` 并 `start-server`，不碰 PATH / `C:\platform-tools`，不带手机序列号；第二次下发仍是 done（`idempotent: true`）。操作端：`admin.py upgrade --manifest <manifest-版本.json> --node <id> --setup --manage-adb-server --yes`，或 `admin.py enable-phone-adb --node <id> --yes`。`publish_agent.ps1 -VersionedOnly` 只上传 `chatx-agent-<ver>.exe`、`ChatXAgentSetup-<ver>.exe` 和 `manifest-<ver>.json`（url / setup_url 都指向带版本号的文件，`channel: canary`），不覆盖公开的 `manifest.json`、未带版本号的安装包和下载页。

**0.3.8 社交动作**（`phone_post` / `phone_like` / `phone_comment` / `phone_follow`，能力 `phone_flows_v1`）：默认关，需 `phone_ops_v1` 且主控已开远程操作。坐标在 `phone_ui_map.json`（或 agent.json `phone_ui_map`）。受保护手机 `3B1F4KE5MS140P4X` / `192.168.0.148` 直接拒绝，不跑 adb。直播机判定（`is_live_stream_host`）不变。

0.3.28 `pm path` 认定已安装的条件是返回码 0，且输出去掉空白后以 `package:` 开头并且后面还有路径。真机输出是 `package:/data/app/.../base.apk`，不是 `package:com.facebook.katana`。`com.facebook.katana` 与 `com.facebook.lite` 都按这个规则查；两个都没有才回执 `fb_not_installed_or_store_redirect`，并且不唤醒、不 `am start`、不退回点图标。返回码非 0 即使文本像路径也不算装了。登录计划任务拉起的面板在拿到锁之后把当前会话号写入 `panel_session.json`，诊断的 `panel_session_id` 可以大于 0。没拿到锁不写。会话 0 仍只结束会话号为 0 的面板进程。Run 键由注册表 API 写入 64 位 `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run\ChatXFleetPanel` 并读回，读回不一致则安装失败。服务启动时补写一次，直播机和坐席机 173 不写。`run_key_present` 仍是真实查询。点赞两信号、±40 像素、频率上限、`dry_run`、0.3.25 派发门和 0.3.13 `push_config` 不变。公开下载页的 latest 仍是 0.3.7。

0.3.27 打开 Facebook 仍按包名 `am start`，随后轮询前台约 9 秒。启动前灭屏发 keyevent 224，锁屏则上滑。轮询结束前台仍是别的应用时，按 Home 再点图标；两种都失败才回执。`pm path` 找不到包时回执 `fb_not_installed_or_store_redirect`，不发 `am start`。Play 商店仍是这个码，别的应用仍是 `wrong_app_launched:<package>`。`phone_app_restart` 的 force-stop 复探只在先前结果是 `app_not_ready`、`timeout` 或 `adb_timeout` 时进行；`like_row_not_found` 以及其它已进入信息流的回执拒绝，原因 `restart_not_warranted`。主控入队和节点执行用 `app_restart_prior_block`。没有先前点赞结果的操作员重启不变。面板锁若由会话 0 的旧进程占用，结束该进程后在活动控制台会话拉起；Run 键写入 `/reg:64`，读取同时看 64 位视图和 Wow6432Node。诊断字段仍是 `task_account`、`run_key_present`、`panel_session_id`。点赞两信号、±40 像素、频率上限、`dry_run`、0.3.25 派发门和 0.3.13 `push_config` 不变。公开下载页的 latest 仍是 0.3.7。

0.3.26 打开 Facebook 点赞改为按包名 `am start`（`com.facebook.katana` / `com.facebook.lite`）。前台已经是这两个包时不按 Home、不点壁纸图标。前台是别的包时回执 `wrong_app_launched:<package>`；Play 商店（`com.android.vending` 等）回执 `fb_not_installed_or_store_redirect`。这两种都不是 `not_logged_in`。推断 Like（动作条只有 Comment/Share）时，截图信号只在该点 ±40 像素且位于动作条行内才计入；`like_diag` 增加 `screenshot_outside` 与 `screenshot_window_px`（0 或 40），主控白名单保留。现场面板计划任务主体是交互组 `S-1-5-4`，不是 SYSTEM。服务在会话 0 时用控制台用户令牌拉起；会话 0 里任务启动成功不算已显示。`operator_alert_diag` 增加 `task_account`（`INTERACTIVE` / `SYSTEM` / 空）、`run_key_present`、`panel_session_id`，不含序列号和密钥。已领取且 `expires_at` 已过、仍无回执的任务变为 `failed`，detail 为 `timeout`。横屏先写 `accelerometer_rotation=0` 与 `user_rotation=0`（白名单只放行这两个值），仍横屏则 `landscape_orientation`。0.3.25 的 fail-closed 派发门不变。公开下载页的 latest 仍是 0.3.7。`push_config` 与 0.3.13 的行为不变。

0.3.12 单文件把 `phone_ui_map.json` 放在冻结的 `phone_flows` 旁边（`_MEIPASS/src/fleet`）。那份文件不在时再读 `%ProgramData%\ChatX\fleet\phone_ui_map.json`。安装器只在该文件还不存在时放一份默认坐标。agent.json `phone_ui_map` 仍优先。公开下载页的 latest 不因这一版改掉。

0.3.13 `push_config` 的补丁可以是 JSON 布尔 `phone_flows_enabled`，和/或下面三种坐标形式里的恰好一种：`phone_ui_map`（本机已有文件的绝对路径）、`phone_ui_map_json`（字符串或对象）、`phone_ui_map_b64`。正文先过 `validate_ui_map`，上限 256KiB，写到 `<state_dir>/phone_ui_map.remote.json`（Windows 上是 `%ProgramData%\ChatX\fleet\phone_ui_map.remote.json`），再把 `phone_ui_map` 设成这个路径。空补丁、未知键、非布尔、两种正文同时给、或正文再加路径：`not_supported_in_agent_v1`，不写文件。路径不存在：`ui_map_missing`。JSON 或形状不对：`ui_map_invalid`。超过 256KiB：`ui_map_too_large`。补丁形状认出来之后，直播机在任何写入之前拒绝（`live_stream_host`）。回执只有布尔和/或路径加 `phone_ui_map_bytes`，不回坐标正文，日志也不打全文。受保护手机与直播机判定不变。公开下载页的 latest 不因这一版改掉。

0.3.14 节点启动构造 `PhoneFlows` 时必须把 `state_dir` 按名传入（`PhoneFlows.from_agent_settings`）。0.3.13 现场包在这一步因 `__init__` 不收 `state_dir` 而退出。`push_config` 行为与 0.3.13 相同。公开下载页的 latest 不因这一版改掉。

0.3.15 本机操作员告警（`src/fleet/operator_alert.py`，不进心跳）：默认关。agent.json `operator_alert_enabled` 只有 JSON `true` 才打开，其余值都是关。`operator_alert_refresh_sec` 默认 180，夹在 30 到 3600；状态变化马上写，没变化时最多按这个间隔重写。`operator_alert_language` 为 `zh`（默认）或 `en`，只在操作员还没在窗口里切换过时生效。`operator_alert_fail_streak` 默认 3，夹在 2 到 20，连续 `failed` 才算，`done` 清零，`rejected` 不计。`wallpaper_map` 可以是 `{"7":"SERIAL"}`、`{"SERIAL":"07"}` 或 `[{"wallpaper_no":"07","serial":"SERIAL"}]`。窗口和气泡只显示壁纸编号；没有编号的显示「未编号」，不显示序列号。adb 离线、未授权、断开、连续失败，以及 adb 服务没运行、找不到 adb、一台手机都没有，都会进名单；恢复后从名单去掉。快照在 `<state_dir>/operator_alert.json`。语言切换写 `<state_dir>/operator_alert_lang.json`，有这份文件就盖过 agent.json。服务在 session 0 时把面板拉到已登录用户的桌面；拉不起、或设了 `CHATX_OPERATOR_ALERT_HEADLESS`、或不是 Windows，就只留快照。直播机（`is_live_stream_host`）不跑这段。受保护手机 `3B1F4KE5MS140P4X` / `192.168.0.148` 不会出现。公开下载页的 latest 不因这一版改掉。

0.3.23 去掉 0.3.21 在 dump 前点上一张截图上半部的那一下。停画面只留 `cmd media_session dispatch pause` 和 `input keyevent 127`。dump 整段重试结束之后读一次 `dumpsys activity activities`：前台是 `com.facebook.katana` 或 `com.facebook.lite`，并且 activity 明确是帖子详情、全屏、沉浸、故事或播放器时，按一次返回（keyevent 4）。`FbMainTabActivity`、看不清的页面、其它应用不按。只按一次，不插在重试中间。

`like_diag.hierarchy` 不参与点击：`node_count`、`button_count`（过尺寸的按钮）、`clickable_count`、`row_counts`（每行按钮个数，长的在前，最多 12 个）、`top_package`（第一个带包名的节点，用来确认是不是 `com.facebook.katana`）、`classes`（最多 6 个最常见类名）。`region_matched` 表示截图动作条矩形里找到了节点。找节点时不套用按钮尺寸上限。有 Like / Comment / Share，或 3 到 4 个间距接近的可点节点，才和动作条结构算两路。这一路只和截图信号（模板、结构、轮廓）一致才算，单独不点，也不能和标签凑成两路。两路一致才点、点完复核、`like_probe` 只定位、`dry_run` 不碰手机，都不变。主控 `sanitize_phone_result` 只留这些已知键。公开下载页的 latest 仍是 0.3.7。

`phone_app_restart` 走 `POST /api/fleet/nodes/{node_id}/phones/{serial}/app_restart`，不要走通用 `/tasks`。能力仍是 `phone_ops_v1`，并且受远程操作开关限制。手机必须在该节点最近心跳里且 `state=device`，否则 `phone_not_reported` 或 `phone_not_ready:<state>`。包名缺省 `com.facebook.katana`，只允许它和 `com.facebook.lite`，其它是 `bad_package`。执行是 `am force-stop` 再 `am start` 启动器。白名单里 Facebook 的 force-stop 是 `app_restart`，只有这个任务会传 `allow_app_restart`。`allow_guarded_writes` 仍然没有任务会传，它也打不开 Facebook 的 force-stop；`allow_app_restart` 也打不开其它包的 force-stop。节点在动手前拒绝直播机（`live_stream_host`）和坐席机 173（`seat_173`：主机名 `173` / `yuyan-173` / `192.168.0.173`，或以 `-173` 结尾）。主控对分组、主机名或标签里带直播标记的节点，以及主机名是 173 的节点，同样拒绝。受保护手机 `3B1F4KE5MS140P4X` / `192.168.0.148` 仍是 `protected_phone`。任务行记下 `created_by`、目标手机和包名；日志里的手机写成壁纸号，没有壁纸号就写成 `[redacted]`，不写序列号。

`not_logged_in` 和 `app_not_ready` 的回执多 `foreground.package` 和 `foreground.activity`（dumpsys 里恢复的组件，可打印字符，不含 `<>`）。入库前按壁纸号或 `[redacted]` 去掉序列号。其它失败不带这个字段。

0.3.21 找赞前的层次读取改成先让自动播放停一下，再 dump。停画面用 `cmd media_session dispatch pause` 和 `input keyevent 127`（暂停，不是播放），外加上一张截图上半部中点的一次轻点。这不是点赞坐标。dump 先写 `/sdcard/chatx_like_hierarchy.xml` 再 `adb pull` 到本机临时文件（文件名不含序列号；路径不能有空格或 `..`）。退出码非 0 也 pull。失败短退避后按这个顺序再试：未压缩文件、未压缩文件、`--compressed` 文件、`dump /dev/tty`、`dump --compressed /dev/tty`。`--compressed` 在路径前面才放行，写在后面仍拒绝。`/sdcard/window_dump.xml` 和其他路径仍拒绝。`like_probe` 仍只定位不点赞。两个信号一致才点、点完复核、`dry_run` 不碰手机，都不变。

`like_diag.uiautomator` 增加 `attempts`（0–20 的整数）、`via`（`file` / `stdout` / 空）、`compressed`（布尔）、`error`（报错首行，可打印字符，不含 `<>`，最长 160）。`error` 和节点文字一样，在节点回执前和主控入库前去掉原始序列号：壁纸号对得上就写成壁纸号，否则 `[redacted]`。另外增加 `shape_matched`、`shape_score`（0–1，没命中也写，未过轮廓门的最高 0.99）、`structure_bounds`（`structure_matched` 为真时是命中的动作条整行 `[x0,y0][x1,y1]`，否则空）。主控 `sanitize_phone_result` 只留这些已知键。公开下载页的 latest 仍是 0.3.7。

0.3.20 `like_probe` 的回执多一个 `like_diag`（真点赞不带这个字段，点的规则不变）。`uiautomator.dump` 是 `ok` / `empty` / `bad`；`like_found` 为真时 `match` 给出 `label`、`by`（`content-desc` / `text` / `resource-id`）、以及该节点的 content-desc、text、resource-id、bounds。`action_bar` 是这块区域里出现过的 content-desc / text / resource-id。`template_score` 是最高模板分（低于 0.50 也写）；`template_matched` / `structure_matched` / `position_matched` 是这三个信号有没有进入融合。`nodes` 最多 24 个候选动作条节点，每个只有 `bounds`、`class`、`content_desc`、`resource_id`、`text`。没有截图、没有序列号。主控 `sanitize_phone_result` 只在 `like_probe` 为真时留下这份诊断，其它键丢掉。

图标条的标签增加 Gustuhin、点赞、讚、喜欢、thumbs up，resource-id 含 like 或 react（含 `feed_story_…like`）也算；单独的 `feed_story` 不算。手机宽度上按钮可以宽过 300px，上限大约是屏宽的 42% 且不超过 480，整条横幅仍然不算。同一行有 Comment/Komento/评论 或 Share/Ibahagi/分享 时，最左格是位置信号，必须再和截图上的模板、动作条或拇指轮廓一致才点。标签没有模板时不点，也不能拿来否决「位置 + 截图」这一对。已赞、评论在最左、Love/Haha/Wow、以及没有评论/分享邻居的空白按钮都不点。点完仍要复核。

手机任务的 `stderr` / `error` 和回执 `detail` 在节点发送前、主控入库前都会去掉原始序列号。目标或 `wallpaper_map` 里有壁纸号就换成壁纸号，否则换成 `[redacted]`。`device '…' not found` 即使调用方没另传序列号也会换掉。`error: device offline` 不变。结果里的 `serial` 字段仍是这台手机的标识，不写进错误句子。公开下载页的 latest 仍是 0.3.7。

0.3.19 Facebook 的 `phone_post` / `phone_like` / `phone_comment` / `phone_follow` 按账号限速，配置在 `engines/chengjie/config/compliance.yaml` 的 `facebook` 段（缺文件时用同一组内置值）。默认：赞 6/小时、40/天；评论 3/小时、15/天；关注 3/小时、15/天；发帖 1/小时、4/天。同一账号任意两种计数动作至少隔 60 秒，再加 0–20 秒由账号和上一次时间算出的固定间隔（把动作摊开，不是躲检测）。本地时区 UTC+8，活跃时段 `[8, 22)`（8 点整可以，22 点整不行）。`enabled: false` 暂停这些动作。主控在远程操作开关和受保护手机判断之后检查：超上限 `rate_capped`，非活跃时段 `outside_active_hours`，间隔不够 `min_delay`，总开关关 `compliance_disabled`，都是 HTTP 409，不入队。远程操作没开仍是 `remote_ops_disabled`，优先于限速。`like_probe` 和 `dry_run` 不计数、不拒绝。计数键是节点 + 账号，没有账号就用壁纸号，再没有用序列号。成功入队就占一次额度（失败的已下发动作也算，跳过的不算）。`GET /api/fleet/social-pace` 和 `overview.social_pace` 返回每个账号今天相对上限的次数。节点执行前再用本机账本查一次。养号 / 看视频里的赞不计。控制台页面还没画。Instagram / TikTok 不走这段。节点版本是 0.3.19，同时包含下面的只读 `net_health` 和只有图标的赞定位。公开下载页的 latest 不因这一版改掉。

0.3.18 只读任务 `net_health`（不进 `REMOTE_PHONE_KINDS`，不要求 caps，旧节点领不到）。对 `adb devices` 里 `state=device` 的手机报告 `reachable` / `latency_ms` / `reach_via`、`transport`（`wifi` / `mobile` / `none`）、`mobile_data`、`sim`、`signal`、`airplane`、移动 `mobile_rx_bytes` / `mobile_tx_bytes`（netstats 近似，不是账单）、`fb_installed`、`fb_screen`（`login` / `app` / `other`）、`screen`（`wm size`）。回执和本机告警只含壁纸编号。紧凑状态例：`网络✓ wifi`、`网络✗ 无流量`、`移动数据弱信号`、`Facebook未安装`、`Facebook未登录`。命令不在白名单或本机没有该二进制时，该字段为 `unavailable`，其余字段照常返回。

剩余流量/话费：Android 没有稳定 adb 接口。`remaining_data.available` 默认 false，`note` 为 `carrier-specific, not available by default`。`net_health_ussd_enabled` 只有 JSON `true` 且 `net_health_ussd_codes` 里该壁纸号是 `*数字#` 才拨一次 USSD（`experimental_ussd`，默认关）；回执里的文本是运营商原文，不是解析出来的余量。`foreground_facebook` 只有 JSON `true` 才 `am start` Facebook 启动器，直播机不拨 USSD 也不拉起应用。白名单见 `src/fleet/adb_allowlist.py`：`read_only` 与 `app_launch` 默认放行；`guarded_write`（`settings put`、`svc data/wifi`、reboot、`pm uninstall/clear`、非 Facebook 的 `am force-stop`）要 `allow_guarded_writes=True`，没有任务会传这个标志。0.3.23 起 Facebook 的 force-stop 改走单独的 `app_restart` 门，只有 `phone_app_restart` 会传 `allow_app_restart`。只读诊断含 `shell uiautomator dump /dev/tty`（不写文件）。不改 `adb_manage_server` 的默认值。公开下载页的 latest 不因这一版改掉。

0.3.18 只有图标的 Facebook 动作条也能定位赞：评论框上方 3–4 个均匀图标的最左格，加上赞模板或拇指轮廓。无障碍标签 Like / 赞 / いいね / React 必须和赞模板落在同一处才点；标签单独不点。dump 读不到就用动作条那条路径。打开应用后轮询信息流大约 9 秒（顶部蓝点消失时向上滑把顶栏带回来），再滑回顶部、停 3 秒，然后才找动作条。仍要两个信号一致或模板分足够高，点完仍要复核。默认安装包不含 rapidocr。公开下载页的 latest 仍是 0.3.7。

0.3.17 Facebook `phone_like` 在截图上找赞，不点 `phone_ui_map` 里的固定 `like_button`。`feed_tab` 是顶部 Home，随包坐标 `[82, 124]`。定位要至少两个信号一致，或赞图标模板分足够高：文字（Like / Gusto / I-like / 赞，同一行还有 Comment/Komento/评论 和 Share/Ibahagi/分享）、浅色/深色模板（多尺寸）、计数行（如 `92k`）下面 2–4 个均匀图标的最左格。只有一个弱信号不点。点一次后复查变蓝或变成 Liked；否则 `not_verified`，不连点。`like_swipes`（JSON 整数 0–8，缺省 3，仅 facebook）是寻找时的上滑次数，方向仍是 `feed_swipe` 的 750→280 千分比；`scrolls` 不拿来当这个次数。划完仍没有帖子：`empty_feed`（壁纸 03 那种 “Something went wrong / Stories couldn't load” 也算）。有帖但不敢点：`like_row_not_found`。`like_probe`（JSON `true`，仅 facebook）同一次 `phone_like`，受同一个远程操作开关限制，打开并滑动、截图、定位，不点赞，回执带 `like_x` / `like_y` / `swipes`。和 `dry_run` 同时给时 `dry_run` 优先，不碰手机。`dry_run` 计划里仍编译旧的 `like_button`，并带 `like_button_deprecated: true`。模板目录和文字引擎都不可用时，真实点赞在 adb 之前拒绝（`ocr_unavailable`）。养号 / 看视频里的 `like_button` 不变。受保护手机、直播机、0.5 秒间隔、默认关闭都不变。公开下载页的 latest 不因这一版改掉。

0.3.16 `push_config` 的补丁还可以带上面五个操作员键：`operator_alert_enabled`（JSON 布尔）、`operator_alert_refresh_sec`（JSON 整数）、`operator_alert_language`（`zh` 或 `en`）、`operator_alert_fail_streak`（JSON 整数）、`wallpaper_map`（对象或数组，上限 64KiB）。它们经 `write_operator_keys` 写入 agent.json，下一次观察周期生效。刷新间隔和失败次数仍由观察时夹紧。类型不对或语言不是 `zh`/`en`：`not_supported_in_agent_v1`，不写文件。未知键照旧拒绝。补丁认出来之后，直播机在任何写入之前拒绝（`live_stream_host`），告警保持关闭。回执带布尔、整数、语言和 `wallpaper_map_entries`（条数），不带壁纸表正文，日志也不打序列号。只读任务 `operator_alert_diag`（不要求远程操作开关）返回 `enabled` / `language` / `refresh_sec` / `fail_streak`，以及快照里不能用的手机的 `wallpaper_no` 和 `reason`。不返回 adb 序列号。公开下载页的 latest 不因这一版改掉。

Social payloads (`phone_post` / `phone_like` / `phone_comment` / `phone_follow` / `phone_warmup` / `phone_dm` / `phone_watch`) accept `dry_run` or `predict_only`. JSON `true` compiles the UI map and returns planned taps/text in permille space (`space: "permille"`) without adb input or screenshots. JSON `false` or a missing flag keeps real execution. `phone_flows_enabled` still has to be JSON `true`; a dry_run on a disabled node is `phone_flows_disabled` and does not touch adb. A non-bool flag, or the two flags disagreeing, is `bad_dry_run`. `enable-phone-flows --node <id> [--yes]` queues `push_config` `{"patch":{"phone_flows_enabled":true}}`.

核对与节奏默认关，避免改掉原来的点击序列和 0.5 s 最短间隔：`robust.verify`（默认 false）、`robust.retries`（默认 2，最多 3 次额外重试）、`robust.jitter_ms`（默认 `[0, 0]`，每端最多 2000 ms，加在每步原有最短间隔之上，不缩短它，也不放开该手机的锁）。agent.json `phone_flow_verify` / `phone_flow_jitter_ms` 热加载，可盖过文件；`phone_flow_verify` 只有 JSON `true` 才强制核对，抖动形状不对就仍用文件。打开核对后，标了 `expect` 的步骤做完再截一张图：画面没变 → `screen_not_reached`；颜色对不上 → `anchor_mismatch`。点开应用（`preflight.after_anchor`，随包是 `app_icon`）之后先看登录标记：登录页颜色在 → `not_logged_in`，已登录颜色不在 → `app_not_ready`，停在这一步，不再发帖或点赞。

**养号 / 私信 / 看短视频**（`phone_warmup` / `phone_dm` / `phone_watch`，能力 `phone_flows_v2`）：同一开关 `phone_flows_enabled`。打开后心跳同时带 `phone_flows_v1` 和 `phone_flows_v2`；关着时两项都不声明。仍走 `POST /api/fleet/nodes/{node_id}/phones/{serial}/social/{warmup|dm|watch}`，不要走通用 `/tasks`。坐标仍在各应用的 `phone_ui_map.json`（`warmup` / `dm` / `watch`）。受保护手机与直播机判定不变。payload：养号 `{app, scrolls?:1..8 默认 4, likes?:0..scrolls 默认 0}`；看视频 `{app, watches?:1..8 默认 3, likes?:0..watches 默认 0}`；私信 `{app, handle, text}`，文字规则与 `phone_text` 相同（ASCII）。回执只留次数和字数，不回私信原文、不回账号名。打开核对后，私信点开会话（`preflight.thread.after_anchor`，随包是 `dm_open`）再看会话标记，没有就 `thread_not_open`，不再输入私信正文。养号不发帖。停留只在节点上睡眠，不新增 adb 动词；每台手机锁、0.5 s 最短间隔、同时最多 2 路都不放宽。

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
| `push_config` | 4 | `payload.patch` | `phone_flows_enabled` as a JSON bool, and/or exactly one of `phone_ui_map` (absolute path of an existing file), `phone_ui_map_json` (string or object), or `phone_ui_map_b64`, and/or `operator_alert_enabled` (JSON bool), `operator_alert_refresh_sec` (JSON int), `operator_alert_language` (`zh` or `en`), `operator_alert_fail_streak` (JSON int), `wallpaper_map` (object or array, 64KiB). Map content is checked with `validate_ui_map`, capped at 256KiB, written to `<state_dir>/phone_ui_map.remote.json`, then `phone_ui_map` is that path. Operator-alert keys are written with `write_operator_keys`. Empty patch, unknown keys, a bad type, both content forms, or content plus a path: **rejected** `not_supported_in_agent_v1`, no write. Missing path: `ui_map_missing`. Bad JSON or schema: `ui_map_invalid`. Over 256KiB: `ui_map_too_large`. A recognized patch on a live-stream host is **rejected** `live_stream_host` before any write. | bool and/or `phone_ui_map` plus `phone_ui_map_bytes` (not the map body), and/or the operator-alert scalars plus `wallpaper_map_entries` (not the serials) |
| `operator_alert_diag` | 8 | empty | Read-only. Returns the redacted operator-alert snapshot: enabled, language, refresh_sec, fail_streak, and wallpaper_no / reason for phones that cannot work. No adb serials. A live-stream host reports enabled false and an empty phone list. | redacted summary |
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
- **未做**：代码签名（exe 未签，SmartScreen 会拦一次）；
  `bd2026.cc` 上的实际部署 / 反代 include / 证书属改生产，脚本已备好，需单独授权后执行。
- `push_config` writes `phone_flows_enabled` (JSON bool) and/or `phone_ui_map`, and as of 0.3.16 also `operator_alert_enabled`, `operator_alert_refresh_sec`, `operator_alert_language`, `operator_alert_fail_streak`, and `wallpaper_map`. A path must already exist on the node. `phone_ui_map_json` / `phone_ui_map_b64` are validated (`validate_ui_map`, 256KiB) and materialized as `<state_dir>/phone_ui_map.remote.json`; the ack returns that path and a byte count, not the map. The wallpaper ack is an entry count, not the serials. Live-stream hosts refuse a recognized patch before any write. Other patch keys stay `not_supported_in_agent_v1`. `operator_alert_diag` is read-only and never returns an adb serial. Protected-phone rules are unchanged.
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
