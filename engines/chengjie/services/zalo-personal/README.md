# zalo-personal — Zalo 个人号扫码登录微服务（zca-js）

给纯官方的 Zalo 渠道补一条**个人号**接入路径：用逆向的 Zalo Web 协议（[zca-js](https://www.npmjs.com/package/zca-js)）
模拟浏览器会话、扫码登录、收发消息。与主进程 `src/integrations/zalo_personal_login.py`
通过本地 HTTP 桥接，契约与 `services/whatsapp-baileys`、`services/messenger-web` 逐一对齐。

## ⚠️ 风险与定位

- **zca-js 是非官方 API**（模拟 Zalo Web），有账号被限制 / 封禁风险。请：① 用小号；
  ② 一号一独立代理；③ 接受风险。**默认关**，需运营在主配置显式开启。
- 相对官方 OA API（仅文字 + 7 天互动窗）的优势：个人号能收发**文字 / 图片 / 语音 / 贴纸**，
  且零开发者门槛（扫码即用）。两条路并存，官方 OA 不受影响。

## 启用（三步）

1. 装依赖并起服务：
   ```bash
   cd services/zalo-personal
   npm install
   pwsh -File start.ps1        # 或 PORT=8792 node server.js
   ```
2. 主配置 `config.local.yaml` 打开开关（**需重启实例**）：
   ```yaml
   platform_login:
     orchestrator_enabled: true
     zalo:
       web_enabled: true
       zca_url: "http://127.0.0.1:8792"
   ```
3. 收件箱「账号管理 → ＋ 新增账号」选 Zalo → 「扫码登录（个人号）」→ 手机 Zalo 扫码。

## HTTP 契约（与 baileys/messenger-web 对齐）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/login/start` | 发起扫码，返回 `{login_id, qr_image, status}` |
| GET | `/login/:id/status` | 轮询 `{status, account_id, qr_image, display_name, avatar_url}` |
| POST | `/login/:id/cancel` | 取消该登录 |
| POST | `/accounts/restore` | 恢复磁盘已持久化会话（幂等） |
| GET | `/accounts` | 已登录账号（含 `logged_in`） |
| POST | `/accounts/:id/send` | 发文字 `{thread_id, text, chat_type?}` |
| POST | `/accounts/:id/send-media` | 发媒体 `{thread_id, media_path, media_type, caption}` |
| POST | `/accounts/:id/logout` | 登出并清 context |
| GET | `/health` | 健康探测 |

会话持久化：`sessions/<account_id>/context.json`（zca-js `getContext()`）→ 免重复扫码。
入站消息经 `PY_INGEST_URL`（默认 `/api/internal/protocol/ingest`，带 `PY_API_TOKEN` Bearer）
回流统一收件箱；会话健康经 `PY_STATUS_URL`。

## 联调核对清单（首次真号验证）

> 本服务为新代码、随包**默认关**，需真号扫码联调后再在生产开启。zca-js 的 API 细节随大版本
> 变化，联调时请对照当前安装版本核对以下点（若签名有出入，改 `server.js` 对应处即可）：

- [ ] `zalo.loginQR(options, cb)` 回调事件里 QR 图字段（本实现取 `event.data.image`，base64 PNG）。
- [ ] 登录成功后 `api.getOwnId()` / `api.getContext()` / `api.fetchAccountInfo()` 的返回结构。
- [ ] `zalo.login({cookie, imei, userAgent})` 恢复登录的入参键名。
- [ ] `api.sendMessage({msg, attachments}, threadId, ThreadType.User|Group)` 的返回消息 id 字段。
- [ ] `api.listener.on("message", ...)` 的 `message.data`（content/dName/uidFrom/msgId/ts）字段名。
- [ ] 代理透传入口（`new Zalo` 构造 vs `loginQR` options）——本实现走 `loginQR` options.proxy。

## 待办（后续增强）

- 富媒体入站落地到 Python 静态目录（当前入站媒体先以 `[媒体]` 占位，仅文字全量回流）。
- 出站消息镜像（`direction:"out"` 回流，让坐席看到自己/AI 发出的话）。
- 崩溃自愈 / 无头保活的看门狗（对齐 messenger-web 的 restore + 慢重试）。
