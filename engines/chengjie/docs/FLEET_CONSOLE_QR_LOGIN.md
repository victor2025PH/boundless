# 控制台集中扫码登录（设计 + 实现说明）

状态：本地实现（分支 `fix/fleet-local-hardening`，未提交、未部署）。契约见 `FLEET_CONTROL_CONTRACT.md` §4 `login_qr` / `login_status`。

## 1. 目标

运营在主控控制台（`/fleet/console`）选一台节点电脑、一个实例、一个平台，点「生成二维码」，
页面上直接出现该电脑本机实例生成的登录二维码；用手机扫完后页面显示「登录成功」。
不需要远程桌面到那台电脑，也不需要知道那台电脑的内网地址。

不做：密码 / 短信验证码 / 2FA 的远程输入（仍在那台电脑上完成）；主控不保存任何账号凭据。

## 2. 为什么走任务通道

节点只有出站连接（心跳 + 长轮询领任务），主控不能直接访问节点的 127.0.0.1 实例。
Agent 已有 `login_qr` / `login_status` 两个任务（调本机 `/api/platforms/{p}/login/start` 与
`/login/{id}/status`），所以只需在主控加两个下发端点和一个控制台面板，不改协议版本。

## 3. 流程

```
控制台                         主控                              节点 Agent                  本机实例
  |--POST nodes/{n}/login-qr--->| enqueue login_qr (ttl 300s)      |                          |
  |                             |<----------- pull (长轮询) --------|                          |
  |                             |                                  |--POST login/start------->|
  |                             |<-- ack {login_id, qr_data_url} --|<--{login_id, qr_image}---|
  |--GET tasks/{id} (每 2s)---->| 返回 result（含二维码）           |                          |
  |--POST .../{login_id}/status>| enqueue login_status (ttl 90s)   |--GET login/{id}/status-->|
  |--GET tasks/{id}------------>| {status, qr_data_url?}           |                          |
  |   每 4s 重复 status，直到 authorized / failed / expired / cancelled，或 5 分钟上限           |
```

## 4. 接口

| 端点 | 权限 | 请求 | 结果 |
|---|---|---|---|
| `POST /api/fleet/nodes/{node_id}/login-qr` | `fleet_control` 写 | `{platform, instance?, account_id?, label?, group?, proxy_id?, use_fingerprint?, phone?, mode?}` | `{ok, task}`，kind=`login_qr`，ttl 300s |
| `POST /api/fleet/nodes/{node_id}/login-qr/{login_id}/status` | `fleet_control` 写 | `{platform, instance?}` | `{ok, task}`，kind=`login_status`，ttl 90s |
| `GET /api/fleet/tasks/{task_id}` | 读 | — | 任务结果，**含** `qr_data_url` |
| `GET /api/fleet/tasks` | 读 | — | 列表里 `qr_data_url` 置空并标 `has_qr: true` |

节点不存在 / 已吊销 → 409；平台名、实例名、login_id 形状不对 → 400，且不入队。

同一节点、同一 login_id / 平台 / 实例已有排队或已领未回的 `login_status` 时，状态接口直接返回那个任务（`deduped: true`），不叠加新任务（多开标签、多人同时看时）。

## 5. 安全约束（代码在 `src/fleet/login_qr.py`，主控和 Agent 共用）

- `platform` 必须匹配 `^[a-z][a-z0-9_]{1,23}$`，`login_id` 必须匹配 `^[A-Za-z0-9_.:-]{1,96}$`：
  两者都拼进 Agent 的本机 URL 路径，防 `../` 路径穿越到本机其他接口。Agent 端再校验一次（旧主控 / 手工任务也挡住）。
- 下发到 `/login/start` 的选项只取白名单键（account_id、label、group、proxy_id、use_fingerprint、phone、mode），逐项截到 128 字符。
- 二维码只接受 `data:image/(png|jpeg|gif|webp);base64,...`，不超过 256 KB：Agent 取回时过滤一次，主控收 ack 时再过滤一次
  （`sanitize_login_result`），控制台赋给 `<img src>` 前用同一个正则再判一次。svg、`javascript:`、http(s) 链接一律丢弃。
- 二维码不进任务列表（每 15 秒刷新、多人可见），只在单个任务详情里返回；成功、失败、停止时页面清掉图片。
- 停止 / 关闭面板时取消还在排队的任务，不让节点事后再去起登录。
- 直播端口的实例 Agent 本来就不访问（第一块 (a)），这里不另开口子。

## 6. 控制台面板

节点行的操作里用「扫码登录」代替原来只弹 `prompt` 的 `login_qr` 按钮。面板字段：
节点名、实例下拉（来自最近心跳，排除 `role=health`，「自动」= Agent 自己选）、平台（带常用候选）。
状态文字：已下发 → 请用手机扫码 → 等待扫码…（状态 / 说明）→ 登录成功 / 已结束：原因 / 超时。

## 7. 测试

`tests/test_fleet_console_qr_login.py`：
- 校验函数（二维码 data URL、平台 / login_id 形状、选项白名单、结果清洗、列表去图）；
- 两个端点：入队内容与 ttl、坏输入 400 且不入队、无权限 401、节点不存在 409；
- 经 Agent 的端到端：起登录 → 取回二维码 → 查状态到 authorized；主控丢掉 ack 里的非图片 URL；
  Agent 对路径穿越直接 rejected 且不发本机请求；Agent 丢非图片二维码、保留 reason_code / retry_after_sec；
- 控制台页面：结构、正则与服务端一致；并用 node 真跑页面脚本（假 DOM + 假 apiFetch）走完
  「排队 → 出码 → authorized」、不安全二维码不进 `<img>`、节点拒绝时停止。

本地端到端：`python tools/fleet_qr_login_e2e.py`（真实主控路由 + 真实 Agent + 假实例 + Chromium 里的真实控制台页面），
`tests/test_fleet_qr_login_e2e.py` 跑 authorized / failed 两个场景。真机测试见 `FLEET_QR_LOGIN_LIVE_TEST.md`。

## 8. 上线前还要做

- 真机联调：一台装了新版 Agent 的节点 + 真实 whatsapp / telegram 实例（这是生产操作，需另行批准）。
- 旧版 Agent（0.2.x）也能执行 `login_qr`，但没有 Agent 端的形状校验；主控端校验已经挡住非法输入。
- 目前每 4 秒一次 `login_status` 任务，每次登录最多约 75 个小任务（重复轮询已去重）；以后如需要可改为 Agent 端一次任务内连续轮询。
