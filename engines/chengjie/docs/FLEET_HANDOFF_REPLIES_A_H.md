# 【草稿】对 `FLEET_GROK_HANDOFF.md` §3 A–H 的逐条回复

> **状态：草稿，尚未发出，也未经老板 / Morgan 确认。** 这是 grok bot 侧的初步意见，用于在 PR 里对接讨论。
> 依据代码：`feat/chengjie-player-care-fleet` @ `fc2e160d`。每条都标了依据的文件。**所有涉及生产（bd2026.cc 的 nginx / DNS / 证书 / 官网 `public/`）的动作，都要等老板明确一句“可以改生产”**（交接文档 §2 第 5 条）。本回复不包含、也不触发任何生产改动。
> 标记：✅ 同意 / ⚠️ 同意但有条件 / ❌ 有异议。**【待确认】** 表示需要对方或老板给输入。

---

## A. 官网 `public/downloads/fleet/` 静态目录由 nginx 直接提供 —— ⚠️ 同意方向，有前置条件

**代码现状**

- `deploy/fleet/publish_agent.ps1`：默认把文件 scp 到 `/home/ubuntu/yuntech/public/downloads/fleet`，公网地址是 `https://bd2026.cc/downloads/fleet`；上传后抓一次 `manifest.json?t=...` 比较 sha，不一致就提示 `WARN public manifest sha differs (CDN cache?)`。
- `deploy/fleet/nginx-fleet.conf` 的注释写着：官网其余 location 走 **Next.js 3000**；下载物放在 `website/public/downloads/fleet/`。snippet 里**没有**为 `/downloads/fleet/` 单独写 location。
- `docs/FLEET_DEPLOY.md` §6：`https://bd2026.cc/downloads/fleet/` **目前未就位，安装器默认下载地址会 302 循环**。

**意见 / 理由**

1. 如果 `/downloads/fleet/` 实际是由 Next.js 从 `public/` 提供的：Next.js 生产模式一般只提供**构建时**就在 `public/` 里的文件，`next build` 之后再 scp 进去的新文件可能访问不到。这可能就是“302 循环”的原因。【待确认：官网到底是 nginx 直出 `public/` 还是 Next.js 在提供；这一条是推断，没有在服务器上验证】
2. 建议（改生产，需要授权）：在 `bd2026.cc` 的 server 块里给 `/downloads/fleet/` 加一个显式的 nginx 静态 location（`alias` 到实际目录），不经过 Next.js，并且：
   - `manifest.json`：`Cache-Control: no-cache`（或很短的 max-age）。主控自己还有 60 s 缓存（`docs/FLEET_DEPLOY.md` §2）。
   - `ChatXAgentSetup.exe`：也不要长缓存。机房包 `Install.cmd` 会用主控钉住的 `setup_sha256` 做校验（`src/fleet/roompack.py`），如果 CDN 或浏览器拿到的是旧文件，校验会失败。
   - `.ps1` 用 `application/octet-stream` 以附件形式下载。控制台的「高级」链接已经走主控 `/fleet/advanced/Install-ChatXAgent.ps1` 并带附件头（`routes.py`），静态目录里的 `.ps1` 是备用。
3. 无鉴权下载是可以的：公开安装包里没有凭据，也没有机房密钥（`docs/FLEET_DEPLOY.md` §5）。机房密钥只走 `/fleet/dl/<token>`，并且那个 location 关了 access_log、加了 `no-store`（`nginx-fleet.conf`）。

**需要的输入**：官网实际静态根目录；`/downloads/` 由谁提供（nginx 还是 Next.js）；有没有 CDN，缓存规则是什么；老板的改生产授权。

---

## B. 主控走主站前缀 `/fleet/`（A 方案）还是子域 `fleet.bd2026.cc`（B 方案） —— ⚠️ 节点 API 和下载页同意 A；控制台建议另议（见 C）

**代码现状**

- `nginx-fleet.conf` A 段：`/fleet/api/` → `127.0.0.1:18798/api/`（剥前缀），`/fleet/dl/`、`/fleet/` 反代到主控；B 段（子域整站反代）已经写好，但注释掉了。
- 安装包默认主控 `https://bd2026.cc/fleet`（`ChatXAgent.iss` `GetController`），Agent 和 `admin.py` 都走 `<controller>/api/fleet/...`。

**意见 / 理由**

- **Agent 通道、`/fleet/` 下载页、`/fleet/dl/` 机房包用 A 方案没有问题**：这几条路径全在 `/fleet/` 下面，零 DNS / 证书改动。`location ^~ /fleet/api/` 和 `location /fleet/` 的优先级高于官网的 `location /`，只要 Next.js 没有占用 `/fleet`。
- **控制台 `/fleet/console` 在 A 方案下大概率不能完整工作**（详见 C），所以建议：短期内 Agent 用 A；控制台要么上 B（子域，需要 DNS + 证书，改生产），要么由 Devin 给控制台补前缀支持。

**需要的输入**：官网 SPA 是否已经用了 `/fleet` 路径【待确认】；老板是否接受为控制台开子域（B）。

---

## C. A 方案下非 `/fleet/*` 路径（web_admin 登录 / 静态资源）不暴露，控制台若依赖需改 —— ❌ 有异议：从代码看，控制台确实依赖这些路径

**代码现状（均在 fc2e160d）**

- `domains/fleet_control/web/templates/fleet_console.html` 第 1 行 `{% extends "base.html" %}`；`src/web/templates/base.html` 从根路径加载 `/static/brand/brand.css`、`/static/theme-tokens.css`、`/static/platform_icons.js` 等。
- 控制台的数据请求用的是**根路径绝对地址**：`apiFetch('/api/fleet/overview')`、`/api/fleet/nodes`、`/api/fleet/pending`、`/api/fleet/room-keys`、`/api/fleet/tasks`（`fleet_console.html` 第 159 行起）。`src/web/templates/_api_fetch.html` 里的 `window.apiFetch` 不加前缀。
- 页面鉴权走核心 `_page_auth`（`src/web/admin.py` 第 1531 行起）；登录相关的跳转是根路径的 `/login`（`src/web/routes/auth_user_routes.py`）。
- A 段 nginx 只代理 `/fleet/api/`、`/fleet/dl/`、`/fleet/`。

**推断**：通过 `https://bd2026.cc/fleet/console` 访问时，`/static/*`、`/login`、`/api/fleet/*` 都会落到官网（Next.js），导致样式缺失、没法登录、数据接口 404。安装完成页的「打开舰队控制台」按钮和 `/fleet/` 下载页的「主控后台」链接都指向这个地址，运维会第一时间撞上。**这是读代码得出的推断，没有在公网验证。**【待确认：Devin 真机联调时是否直连 18798 或走子域，所以没暴露这个问题】

**建议（二选一，都不在本分支实现）**

1. B 方案开子域给控制台（`nginx-fleet.conf` B 段现成；需要 DNS + 证书，改生产）。
2. Devin 给控制台加前缀支持：模板里的静态、API、登录地址带 `public_url` 前缀，nginx A 段再补需要的路径。改动在 Devin 的文件里。

过渡方案：管理员用 `python -m src.fleet.admin pending / approve / nodes / task`（`src/fleet/admin.py`，走 `/fleet/api/...`，A 方案下可用），token 只放在操作端环境变量里。

**需要的输入**：Devin 确认联调时的访问方式，以及选 1 还是 2；老板对子域的态度。

---

## D. 官网首页 / 导航是否加「多机部署」入口 —— ✅ 同意默认方案（暂不加）

**理由**：`/fleet/` 独立页已经能完成下载和三步安装（`templates/fleet_home.html`）；官网首页属于生产的 `public/` / 前端范围，不加入口就不用碰生产。机房运维拿到的是直链或机房包，不依赖首页入口。等 A / C 落地、控制台可用后再评估。

**需要的输入**：老板定是否加、加在哪。

---

## E. 代码签名证书 —— ✅ 同意“无证书先不签，有了再插 signtool”，并补充两点

**理由 / 补充**（详见 `docs/FLEET_CODE_SIGNING_PREP.md`）：

1. **要签两层，并且内层先签**：`chatx-agent.exe` 要在 ISCC 编译**之前**签，否则安装包里装的是未签名的 exe；`ChatXAgentSetup.exe`（建议用 Inno `SignTool` 连卸载程序一起签）要在算 `setup_sha256` 之前签。现有的 `build_agent.py --skip-build` 可以在手工签完后重算 manifest，但它会重写整份 manifest，所以要在 `build_setup.ps1` 之前跑。
2. **`publish_agent.ps1` 找到 ISCC 就会自动重编安装包**，会用未签名的包覆盖手工签过的包。接入签名时需要改这一点（Devin 的文件）。

证书、私钥不进 Git，也不写进脚本；脚本只引用指纹或云签名的元数据路径。

**需要的输入**：签名主体；OV / EV / 云签名选哪种、预算；令牌或云账号由谁保管；固定在哪台打包机上签。（全部【待确认】）

---

## F. 智拓（ReachX）是否接入 fleet —— ✅ 同意架构上可以接；要补的是智拓侧端点和本机探测

**代码现状**

- Agent 对每个实例调用的本地端点（`src/fleet/agent.py`）：
  - `GET /api/accounts/fleet-health`：账号汇总，心跳用，经 `reduce_fleet_health` 归约；
  - `GET /api/player-care/overview`：仅 `player_care` 域；
  - `POST /api/platforms/{platform}/login/start`、`GET /api/platforms/{platform}/login/{id}/status`：`login_qr` / `login_status` 用；
  - `POST /api/player-care/commands`：player_care 相关命令。
  - 纯健康检查型实例（如 AvatarHub）只探测 `/health` 和 `/api/health`。
- 交接文档写的是 `/api/ping`，但代码里的实例探活实际用的是 `/api/accounts/fleet-health`，health-only 实例用 `/health`、`/api/health`。本机自动探测也会试 `/login`（`src/fleet/detect.py`）。
- 自动探测只认智聊（`config.local.yaml`、端口 `18799` / `18797`）和 AvatarHub（`9000`）。**智拓不会被自动发现**，要么用 `add-instance` 手工登记（`Install-ChatXAgent.ps1 -InstanceName/-InstanceUrl/-AuthToken`），要么由 Devin 扩展 `detect.py`。

**意见**：智拓最少要提供和智聊**同样格式**的 `GET /api/accounts/fleet-health`（这样心跳和 `account_health` 能用）。要支持集中扫码，还要实现 `/api/platforms/{platform}/login/...`。字段格式以 `reduce_fleet_health` 的输入为准，协议本身（`protocol.py`）不用改。

**需要的输入**：智拓侧现有端点清单和鉴权方式、默认端口【待确认】；是否要自动探测（涉及 Devin 的 `detect.py`）。

---

## G. `push_config` 是否需要 —— ✅ 同意继续拒绝

**代码现状**：`src/fleet/protocol.py` 定义了 `TASK_PUSH_CONFIG`（优先级 4）；契约 §4 规定 Agent 回 `rejected not_supported_in_agent_v1`；控制台任务下拉里也没有它（`fleet_console.html` 里的 `KINDS`）。

**理由**：Agent 以 SYSTEM 身份运行，本机的 `restart_cmd` 用 `shell=True` 执行（`docs/FLEET_DEPLOY.md` §5）。能远程改配置就等于能远程执行命令。在有白名单键 + 签名的 patch 设计之前，不应该开放。目前没有看到必须远程改配置的场景。

**需要的输入**：业务方是否有具体场景（改哪些键、多久改一次）。没有场景就保持现状。

---

## H. 首台真机试装由谁做 —— ✅ 同意 Devin 先在开发机上试，再由老板指定一台机房电脑

**代码 / 文档现状**：`docs/FLEET_DEPLOY.md` §6 记录，已经在一台 Windows Server 2022 真机上跑通**命令行安装器 + 计划任务 + 一次真实 upgrade**（Agent 0.2.x 时期）。但这几项**还没有真机确认**：

- 双击安装包 `ChatXAgentSetup.exe` 的 UAC 和完成页；
- 待批准、机房密钥这套新流程；
- 目录 ACL 迁移（`fleet.legacy-<guid>`）；
- 公网下载目录和控制台（见 A / C）。

**建议**：首台机房电脑试装按 `docs/FLEET_OPS_INSTALL_GUIDE.md` 走一遍，并补齐里面的截图占位；验收口径用交接文档 §5。本方（grok bot）**不操作任何机房电脑或设备**，只负责文档和校对。

**需要的输入**：老板指定哪一台机房电脑【待确认，本文不指定】；谁有权发注册码和机房密钥（需要持有 `web_admin.auth_token` 的人）。

---

## 附：汇总的待确认问题

1. `/downloads/fleet/` 由 nginx 还是 Next.js 提供；有没有 CDN（A）。
2. 官网是否占用了 `/fleet` 路径（B）。
3. 控制台在 A 方案下的真实表现；选子域还是加前缀支持（C）。
4. 首页入口（D，老板定）。
5. 代码签名主体、类型、保管人、打包机（E）。
6. 智拓端点清单和端口（F）。
7. `push_config` 是否有场景（G）。
8. 首台机房电脑和发码权限（H）。
