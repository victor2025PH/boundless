# tiktok-web — TikTok 个人号网页托管登录 + 只读收件箱边车（Playwright，TK-3 ②-A 阶段 1）

给 TikTok 个人号私信补一条**备用读路**：用 Playwright 驱动隔离持久化 Chromium 加载 `tiktok.com`，
在服务器窗口内完成官方登录（账密 / 2FA / 验证码），再轮询网页收件箱把私信读进智聊统一收件箱。
**阶段 1 assistOnly：不代发。** 发送由 huoke 真机路承担（`src/integrations/tiktok_huoke_bridge.py`）。
Python 侧桥接 `src/integrations/tiktok_web_login.py`，契约对齐 `services/instagram-web`。

## 定位与红线

- **非官方接入**（依赖 tiktok.com DOM / 页面水合数据），有账号被限制 / 封禁风险：小号、一号一代理、养号。
  **默认关**，需主配置 `platform_login.tiktok.web_enabled: true` 显式开启（不随桌面默认开）。
- 主路是真机（客户全球、TikTok 私信网页版在部分地区不可用）；边车只在真机不可用时补读。
- **永不自动发**；人工可发（②-B）要等 ① 真机 + ②-A 边车跑满 72 小时自有账号验证再开。
  `/accounts/:id/send*` 端点存在但恒 `501 assist_only`（可判定的拒绝，不是 404 误判「没挂路由」）。
- 智聊只接管**对方开口之后**的对话：边车读到的会话若从未有对方入站，列表挂「消息请求 · 对方未回」，
  发送被 `policy_message_request_pending` 拦——这条判断在智聊侧（`tiktok_source_badge`），边车不猜关系态。

## 启用（三步）

1. 装依赖并起服务（`npm install` 经 postinstall 自动 `playwright install chromium`）：
   ```bash
   cd services/tiktok-web
   npm install
   pwsh -File start.ps1        # 或 PORT=8794 node server.js
   ```
2. 主配置 `config.local.yaml`（**需重启实例**）：
   ```yaml
   platform_login:
     orchestrator_enabled: true
     tiktok:
       web_enabled: true
       web_url: "http://127.0.0.1:8794"
   ```
3. 阶段 1 登录入口：接入页 TikTok 第 4 步「网页托管 · 边车」页签（②-B 前只显示占位与就绪态），
   或直接调 Python provider（`tiktok_web_login.make_provider`）。账号登记为 `mode=web`（来源徽标「网页」）。

## HTTP 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/login/start` | 弹官方登录页，返回 `{login_id, qr_image(登录页截图), status}` |
| GET | `/login/:id/status` | 轮询 `{status, account_id, qr_image, name, username, avatar_url, hint_code}` |
| POST | `/login/:id/cancel` | 取消未完成登录 |
| POST | `/accounts/restore` | 恢复磁盘已持久化会话（幂等，headless） |
| GET | `/accounts` | 已登录账号（含 `logged_in`、`assist_only: true`） |
| POST | `/accounts/:id/send` | **501 `assist_only`**（阶段 1 刻意不实现） |
| POST | `/accounts/:id/send-media` | **501 `assist_only`** |
| POST | `/accounts/:id/logout` | 登出并清 profile |
| GET | `/health` | `{ok, svc:"tiktok-web", assist_only:true}` |

- `account_id` ＝ TikTok 数字 uid（水合数据 `__UNIVERSAL_DATA_FOR_REHYDRATION__ → webapp.app-context.user.uid`；回落 cookie `uid_tt`）。
- 入站经 `PY_INGEST_URL`（`/api/internal/protocol/ingest`，Bearer `PY_API_TOKEN`），payload `platform: "tiktok"`，
  `source: {source: "tiktok_web", mode: "web", assist_only: true}`；会话键：能拿到 `@uniqueId` → `tiktok:user:<uniqueId>`
  （与官方 worker / 真机桥**同键**，三路合并成一条会话）；只有数字 → `tiktok:web:<u>`；都没有 → 标题 slug（联调后应消失）。
- 自己发的那条（预览 `You:` 类前缀）**不上报**——出站镜像以智聊自己的为准（真机路已写镜像）。
- 会话健康经 `PY_STATUS_URL`（`/session-status`），与 messenger/instagram 同一 `platform_session_health` 通道。

## 联调核对清单（首次真号验证——**必须用私信网页版可用地区的账号**）

> 选择器 / 字段名尚未用真实账号采样（本仓开发环境所在地区 TikTok 私信网页版不可用），全部集中在
> `server.js` 顶部 `SEL_* / URL_* / HYDRATION_*` 与 `tt_threads.js`。

- [ ] 登录成功锚点 `SEL_LOGGED_IN`（`a[href="/messages"]` / `[data-e2e="profile-icon"]`）。
- [ ] 水合数据路径 `HYDRATION_USER_PATH`（`__DEFAULT_SCOPE__ → webapp.app-context → user`）与字段名 `uid / uniqueId / nickName / avatarUri`。
- [ ] 收件箱行 `SEL_INBOX_ROW`（`[data-e2e="chat-list-item"]` / `a[href^="/messages?u="]`）与 href 形态（`?u=` 数字还是 `/@uniqueId`）。
- [ ] 行内叶子文本顺序是否 [对方名, 末条预览, 相对时间]（`tt_threads.parseTtThreadRow` 的假设）。
- [ ] 自己发的预览前缀词表（`You:` / `你：`）——界面语言相关。
- [ ] 验证码 / 2FA 的 URL 特征（`captcha` / `verify` / `two`）→ `hint_code`。
- [ ] 轮询间隔 `TT_POLL_MS`（默认 15s）在该账号上是否触发风控；必要时拉长。

## 与真机路的关系（三路同键）

| 路 | 读 | 发 | 键 | 来源徽标 |
|---|---|---|---|---|
| 官方 Business Messaging | webhook | API（48h/10 窗） | `tiktok:user:<username>` | 官方 |
| huoke 真机（主路） | `check_inbox` → `/api/tiktok/huoke/dm` | 真机 `send_dm`（认领队列） | `tiktok:user:<username>` | 真机 |
| 本边车（备用读路） | 网页收件箱轮询 | **不发**（②-B 人工可发） | `tiktok:user:<uniqueId>`（取到时） | 网页 |

## 待办（②-B 及以后）

- `TikTokWebWorker`（编排器 worker：健康 / 人工发送）+ 能力矩阵行；`platform_login` 表登记 `tiktok:web`（登录弹窗可选）。
- 进线程读末条正文 + 方向权威（当前只报列表预览；进线程有已读回执副作用，要与产品确认）。
- 崩溃自愈 / restore 慢重试 / 富媒体入站落地。
