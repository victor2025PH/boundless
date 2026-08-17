# messenger-web — Messenger 网页模式登录/收发微服务

用 **Playwright 驱动持久化 Chromium 加载官方 `messenger.com`**（`web` 模式，等同竞品"内嵌
隔离浏览器 + 官方网页版"的做法），为 `telegram-mtproto-ai` 主进程提供 Messenger 的网页登录与
收发，功能对齐官方网页版。与 Python 主进程通过本地 HTTP 桥接
（`src/integrations/messenger_web_login.py`），契约对齐 `services/whatsapp-baileys`。

## 为什么是这条路

Messenger 没有像 WhatsApp Baileys 那样的干净协议库，但它**有官方网页版 `messenger.com`**。
所以「网页扫码/登录、功能和官方一致」= 内嵌一个隔离浏览器加载官方网页版，运营在里面用官方方式
登录（扫码 / 账密 / 2FA 都在官方页完成），程序只负责 **profile 持久化 + 检测登录成功 + DOM
收发**。这正是易翻译 / 云译等竞品对多平台的通用实现。

## 安装 & 运行

```powershell
cd services/messenger-web
npm install            # postinstall 会自动 playwright install chromium
pwsh -File start.ps1   # 或 node server.js
```

主进程侧开闸（`config.local.yaml`）：

```yaml
platform_login:
  orchestrator_enabled: true
  messenger:
    web_enabled: true
    web_url: http://127.0.0.1:8791
```

## 登录方式（重要）

- 默认 **headed**（`MSG_HEADLESS=0`）：`/login/start` 会弹出真实浏览器窗口，运营在窗口里
  按官方方式登录。前端弹窗同时轮询展示登录页**截图**（`qr_image`）作为进度可视化。
- 登录成功 → cookie（`c_user`）持久化到 `sessions/<login_id>/`，`account_id` 取 `c_user`。
- 之后可用 `MSG_HEADLESS=1` + `MSG_RESTORE_ON_BOOT=1` 后台常驻保活（免窗口）。

## HTTP 接口（契约对齐 baileys）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/login/start` | 发起登录（弹登录页）→ `{login_id, qr_image, status}` |
| GET | `/login/:id/status` | 轮询 → `{status, account_id, name, avatar_url, qr_image, reason_code, hint_code}` |
| POST | `/login/:id/cancel` | 取消并关闭该登录上下文 |
| POST | `/accounts/restore` | 恢复磁盘已持久化登录（幂等） |
| GET | `/accounts` | 已登录账号列表 |
| POST | `/accounts/:id/send` | 发消息（`{jid=thread_id, text}`，DOM 自动化） |
| POST | `/accounts/:id/logout` | 登出并清 profile 目录 |

`status`：`pending | scanned | authorized | expired | failed`。

### `reason_code` / `hint_code`（登录归因）

托管登录是「人在服务器窗口里手动完成」，Node 只能旁观 URL / cookie / 错误框。
`login_classify.js`（纯函数，`npm test` 有门禁）把旁观信号归成三段可操作原因，
两个字段**共用同一套词汇**，也与 Python 侧 `login_funnel_stats._REASON_CODES` 同源：

| 码 | 含义 | 运维动作 |
|---|---|---|
| `two_factor` | 密码已过、卡在第二因子 | 去服务器窗口输验证码 |
| `checkpoint` | Facebook 安全检查点（风控/异常登录/受限） | 账号问题，非代码故障 |
| `password_error` | 登录表单挂着错误框 | 核对账号密码 |
| `login_failed` | 冷启失败（Chromium/依赖异常） | 看服务日志 |

- `hint_code`：**非终态**，实时回报「此刻登录页在要什么」，授权后清空；
  正常等待态（干净登录表单 / 加载中）一律空串——认不出就不报，不硬凑原因。
- `reason_code`：终态原因。窗口期结束仍未登上时，取最后观察到的分段。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8791` | 监听端口（须与主进程 `web_url` 一致） |
| `PY_INGEST_URL` | 空 | 入站桥（统一收件箱 `/api/internal/protocol/ingest`）；空=不上报 |
| `PY_API_TOKEN` | 空 | 入站桥 Bearer（须与 `web_admin.auth_token` 一致） |
| `MSG_HEADLESS` | `0` | **交互登录**是否无头；`0`=弹窗（需人工过 2FA），`1`=无头（一般不用） |
| `MSG_RESTORE_HEADLESS` | `1` | **restore/开机恢复/崩溃自愈已授权会话**是否无头。默认 `1`=稳态零可见窗口（多账号规模化靠此）；`0`=restore 也 headed（调试用） |
| `MSG_POLL_MS` | `4000` | 入站轮询间隔；`0` 关闭入站同步 |
| `MSG_BACKFILL` | `20` | 首连回填最近会话末条数 |
| `MSG_SYNC` | `1` | `0` 关闭入站上报 |
| `MSG_BASE_URL` | `https://www.messenger.com/` | 官方网页版基址 |
| `MSG_SESSIONS_DIR` | `./sessions` | 持久化 profile 根目录 |
| `MSG_RESTORE_ON_BOOT` | 随 headless | 开机是否自动 restore |

## 待真号联调

DOM 选择器（`SEL_CONV_LINKS` / `SEL_MSG_ROWS` / `SEL_COMPOSER`）与入站去重是 best-effort，
messenger.com 改版或多语言 UI 下可能需要微调（集中在 `server.js` 顶部选择器区）。入站当前用
"线程+文本前缀"近似去重（无稳定 msg_id 时的降级），真号联调时建议换成 DOM 的 `data-*` 消息 id。

## 合规

网页自动化违反平台 ToS，存在风控/封号风险。请配套一号一指纹一代理 + 养号，控制发送频率。
