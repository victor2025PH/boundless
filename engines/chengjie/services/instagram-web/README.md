# instagram-web — Instagram 个人号网页托管登录微服务（Playwright）

给纯官方的 Instagram 渠道补一条**个人号**接入路径：用 Playwright 驱动隔离持久化 Chromium
加载 `instagram.com`，在服务器窗口内完成官方登录（账密 / 2FA），再用 DOM 收发私信（DM）。
与主进程 `src/integrations/instagram_web_login.py` 通过本地 HTTP 桥接，契约与
`services/messenger-web` 逐一对齐（登录形态＝ `hosted`，**不使用二维码**）。

## ⚠️ 风险与定位

- **非官方接入**（依赖 instagram.com DOM），有账号被限制 / 封禁风险。请：① 用小号；
  ② 一号一独立代理；③ 接受风险。**默认关**，需运营在主配置显式开启。
- 相对官方 Graph API（专业号 + Page + 令牌 + 审核）的优势：零开发者门槛，用自己的号登录即用。
- 官方 Graph API 那条路**原样保留**，两条路并存。

## 启用（三步）

1. 装依赖并起服务（`npm install` 会经 postinstall 自动 `playwright install chromium`）：
   ```bash
   cd services/instagram-web
   npm install
   pwsh -File start.ps1        # 或 PORT=8793 node server.js
   ```
2. 主配置 `config.local.yaml` 打开开关（**需重启实例**）：
   ```yaml
   platform_login:
     orchestrator_enabled: true
     instagram:
       web_enabled: true
       web_url: "http://127.0.0.1:8793"
   ```
3. 收件箱「账号管理 → ＋ 新增账号」选 Instagram → 「账号登录（个人号）」→ 在服务器弹出的
   浏览器窗口内完成官方登录（账密 / 2FA）；本窗口做实时预览、成功后自动确认。

## HTTP 契约（与 messenger-web 对齐）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/login/start` | 弹官方登录页，返回 `{login_id, qr_image(登录页截图), status}` |
| GET | `/login/:id/status` | 轮询 `{status, account_id, qr_image, name, avatar_url, hint_code}` |
| POST | `/login/:id/cancel` | 取消未完成登录 |
| POST | `/accounts/restore` | 恢复磁盘已持久化会话（幂等，headless） |
| GET | `/accounts` | 已登录账号（含 `logged_in`） |
| POST | `/accounts/:id/send` | 发私信 `{thread_id, text}` |
| POST | `/accounts/:id/send-media` | 发媒体 `{thread_id, media_path, media_type, caption}` |
| POST | `/accounts/:id/logout` | 登出并清 profile |
| GET | `/health` | 健康探测 |

- `account_id` ＝ Instagram `ds_user_id` cookie（稳定数字 id，不依赖 DOM）。
- 每账号独立持久化 `sessions/<login_id>/`（userDataDir）→ cookie 持久化免重复登录。
- 入站经 `PY_INGEST_URL`（默认 `/api/internal/protocol/ingest`，带 `PY_API_TOKEN` Bearer）；
  会话健康经 `PY_STATUS_URL`。

## 联调核对清单（首次真号验证 —— DOM 依赖，选择器集中在 server.js `SEL_*` / `URL_*`）

> 本服务为新代码、随包**默认关**，需真号登录联调后再在生产开启。instagram.com 改版频繁，
> 联调时请核对以下选择器（若有出入，改 server.js 顶部集中区即可）：

- [ ] 登录成功锚点 `SEL_LOGGED_IN`（当前 `a[href="/direct/inbox/"]` / Home 图标）。
- [ ] DM 输入框 `SEL_COMPOSER`（当前 `textarea[placeholder]` / `div[contenteditable][role=textbox]`）。
- [ ] 收件箱线程行 `SEL_INBOX_THREAD`（当前 `a[href^="/direct/t/"]`）。
- [ ] 发媒体的 `input[type="file"]` 选择器与「选图后是否需额外确认按钮」。
- [ ] checkpoint / 2FA 的 URL 特征（当前 `/challenge/`、`two_factor`）→ hint_code。
- [ ] `send` 的送达确认（当前按 Enter 后固定等待；可加回读气泡二次确认，见 messenger-web）。

## 待办（后续增强，对齐 messenger-web 的成熟度）

- 入站从「线程预览轮询」升级为「进线程读末条正文 + 方向权威」（当前首版只报预览，
  见 `pollInbox`；参考 messenger-web 的 `MSG_READ_THREAD`）。
- 出站送达回读二次确认（composer 清空 + 气泡回读），失败如实 502。
- 崩溃自愈 / 会话健康看门狗（对齐 messenger-web 的 restore + 慢重试 + session-status）。
- 富媒体入站落地到 Python 静态目录。

## 备选实现（P2.5）

`instagrapi`（Python 私有 API）为高阶/高性能备选：无浏览器、DM API 更全，但需设备指纹
持久化 + 住宅代理 + challenge/checkpoint 处理，`mode=protocol`。本服务（Playwright web）
胜在登录最贴近真人 + 复用 messenger-web 全套 hosted/web-worker 机制。
