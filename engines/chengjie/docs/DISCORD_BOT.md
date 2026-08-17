# Discord Bot 接入（platform=`discord`, mode=`protocol`）

> 一句话：Discord 走**官方 Bot 应用**接入（填 Token，不扫码），能力上限由平台规则封顶——
> **Bot 不能主动私信陌生人**。它是「被动接待 + 服务器内运营」渠道，不是拉新私聊渠道。
> 买之前先读第 2 节，别把它当 Telegram 用。

## 1. 五分钟接入

1. 打开 <https://discord.com/developers/applications> → **New Application**。
2. 左侧 **Bot** → **Reset Token** → 复制（只显示一次）。
3. 同页往下，**Privileged Gateway Intents** 里打开 **MESSAGE CONTENT INTENT**。
   不开这个，Bot 收到的每条消息 `content` 都是空字符串——表现为「有新会话、内容全空」。
4. 左侧 **OAuth2 → URL Generator**：scopes 勾 `bot`，权限勾 `Send Messages` /
   `Attach Files` / `Read Message History`，用生成的链接把 Bot 邀请进你的服务器。
5. 填配置（见第 3 节）→ 后台「接入平台」选 Discord → 点接入。
   校验通过会回显 Bot 用户名与已加入的服务器数。

## 2. 硬限制（平台规则，不是我们没做）

| 限制 | 后果 | 代码里怎么体现 |
|---|---|---|
| Bot 不能主动 DM 陌生人 | 直接 403 / error code `50007` | `platform_capabilities.NO_PROACTIVE_PLATFORMS` 把 discord 排除出主动触达候选；`proactive_topic` 筛选时跳过 |
| 对方需与 Bot 有共同服务器且允许「服务器成员私信」，或先私信过 Bot | 满足其一才发得出 DM | `send()` 撞 403 时返回投递失败（**不抛异常、不喂封号信号**），detail 是人话 |
| 已读态是用户端私有 | Bot 无 API 可写已读 | `HARD_LIMITS[("discord","mark_read")]`，能力矩阵里显式标注 |
| 单条消息 ≤ 2000 字符 | 超长直接 400 | `split_discord_text()` 按段落/句子/硬切三级降级拆分后连发；**已发出的分片算已投递**，不整条重发（否则对方会收到重复前半段） |
| 附件 CDN URL 24 小时过期 | 隔天再下载必 404 | 入站即下载落 `/static`，库里存本地 URL 而非 Discord 链接 |
| 上传体积上限随服务器加成等级变化（免费 25MB） | 超限 413 / code `40005` | 上传**前**按 `guild.filesize_limit` 本地预检，超限直接返回 `media_too_large`（不白耗一次带宽、不堵出站队列）；服务端仍拒时同样按投递失败返回，不抛栈 |

**运营含义**：Discord 适合「服务器里已有社群 → 承接咨询/自动答疑/群内@响应」，
以及「对方先来私信 → 全自动接待」。不适合「导入一批 user id 主动开聊」——那条路平台封死了。

## 3. 配置

```yaml
platform_login:
  discord:
    bot_enabled: true          # 总开关，默认 false（新子系统约定）
    bot_token: ""              # 建议留空，见下
    guild_mode: mention        # all | mention | off，见下
    ignore_bots: true          # 忽略其他 Bot 的消息，防两个 Bot 互相对轰
    download_media: true
    max_media_bytes: 16777216  # 16MB，超限只落类型占位不下载
    connect_timeout: 45        # 秒，含 Gateway 握手 + intent 校验
```

**Token 放哪**：优先**逐号填**（接入时随请求带上，落账号注册表 `meta.bot_token`，
随实例数据走）；写进 `config` 里则所有 Discord 账号共用同一个 Bot。两处都全程掩码，
不进日志。worker 取值顺序：账号 `meta.bot_token` → 配置。

`guild_mode` 决定**服务器频道**里怎么接（私聊永远全量接）：

- `off`：只接私聊，频道消息全丢（最安静，适合纯客服）
- `mention`（默认）：频道里**被叫到**才接——避免把整个服务器的闲聊灌进收件箱
- `all`：频道消息全接（活跃服务器慎用，收件箱会被淹）

`mention` 的「被叫到」含三种，缺一种运营就会觉得「@了没人理」：

1. `@Bot` 本身；
2. **`@Bot 所属的身份组`** —— 社群里召唤客服的常态是 `@客服` 而不是 `@某个机器人`。
   给 Bot 的角色配上你们的客服身份组即可生效；
3. 回复 Bot 的消息（Discord 的回复自带作者提及）。

**不含 `@everyone` / `@here`**：那是广播不是召唤，认了等于每次全员通知都往收件箱灌一条。
（`@everyone @Bot 帮忙看下` 这种仍然算——直接点名优先于广播过滤。）

## 4. 会话键约定

- 私聊：`dm:<user_id>` —— **钉在用户 id 上，不是 DM 频道 id**。
  DM 频道 id 对 Bot 不是稳定入口（没缓存时得先 `create_dm` 才拿得到），
  用它当键会导致重启后回不到同一个会话。
- 频道：`ch:<channel_id>`，落库 `chat_type=group`，走「群组动态」而非 SLA 告警。
  群里「谁说的」落 `sender_id`/`sender_name`，与会话键分开。
- **群组私信**（多人 DM）同样是 `ch:<channel_id>`。它没有 guild，但**不是 1:1**——
  按发言人建 `dm:` 键会把一个群聊裂成 N 个会话，回复还会私发给某个人而不是发回群里。
  判据是 `recipients`（复数，仅群组私信有）。群组私信**不**自动算「点名」：
  Bot 被拉进一个群聊不代表每句话都在跟它说话。

## 5. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 接入时报「缺少 MESSAGE CONTENT INTENT」 | 开发者后台没开特权 intent | 见第 1 节第 3 步，改完**要重连** |
| 有会话但消息内容全空 | 同上 | 同上 |
| 私信发不出，detail 说「无法私信该用户」 | 平台规则（`50007`），见第 2 节 | 引导对方加入服务器并开放 DM，或让对方先私信 |
| 频道发不出，detail 说「在该频道没有发言权限」 | 频道权限覆盖或邀请时没勾权限（`50013` 等） | 改频道权限，或重邀 Bot 时勾 Send Messages / Attach Files。**两类 403 文案刻意分开**——照着「对方不让私信」去排查频道权限会白费一天 |
| 接入显示成功但一条消息都收不到 | 早期版本会在 token 写库失败时仍报「已连接」 | 已修：写库失败直接返回 `registry_write_failed` 并说明原因；若仍遇到，查实例数据目录是否可写 |
| Bot 在线但收不到任何消息 | `guild_mode=mention` 而没人 @ ；或 Bot 没被邀请进服务器 | 先用私聊测通，再调 `guild_mode` |
| 群里 `@客服` 没反应，`@Bot` 才有 | Bot 的角色里没有那个客服身份组 | 服务器设置 → 身份组，把客服身份组加给 Bot（判据见第 3 节） |
| 发图报「文件超过上传上限」 | 超出该服务器的加成档上限（免费 25MB） | 压缩后再发，或改发链接；服务器提升加成等级后上限自动跟涨，无需改配置 |
| 客户发了图，会话里却是空消息 | 附件超限或下载失败 | 已修：这两种情况都会保留媒体类型渲染占位（坐席看得见「对方发了图但没取到」），不再是彻底的空消息 |
| 接入按钮点了没反应 | `bot_enabled: false` 或没装 `discord.py` | `platform_readiness` 会给出确切 blocker，看后台平台卡的提示 |

依赖：`discord.py>=2.3`。没装时**不报错**——`is_discord_available()` 挡在前面，
平台卡显示「缺依赖」blocker，其余平台完全不受影响。
