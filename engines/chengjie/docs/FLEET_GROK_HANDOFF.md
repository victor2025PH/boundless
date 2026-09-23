# 智控（fleet）多机主控 —— 与 grok bot 的对接说明（2026-09-24）

> 本文给并行开发的 grok bot（以及任何接手的人）读。目的：**不重复造轮子、不盲改对方的东西**，把剩下的活分清。
> 代码真源：`https://github.com/victor2025PH/boundless` → `engines/chengjie/`，分支 `feat/chengjie-player-care-fleet`（PR #26）。

## 1. 已经做完、可直接用的（Devin，PR #26）

| 层 | 内容 | 位置 | 状态 |
|---|---|---|---|
| 协议 | enroll / heartbeat / pull（长轮询）/ ack；任务 kind 9 种、优先级、TTL、幂等；PROTO_VERSION=1 | `src/fleet/protocol.py`、`docs/FLEET_CONTROL_CONTRACT.md` | 定稿 v1，改动需两边同意 |
| 主控 | 路由 + SQLite 存储 + `/fleet/` 下载页 + `/fleet/console` 控制台 | `domains/fleet_control/`、`src/fleet/store.py` | 可用 |
| 节点 Agent | enroll / heartbeat / 任务执行（ping、account_health、pull_overview、login_qr、login_status、stop_account、restart_instance、**upgrade**）| `src/fleet/agent.py` v0.2.0 | 可用 |
| 服务化 | Windows 计划任务（ONSTART/SYSTEM）/ Linux systemd；监督循环（未注册 / 吊销 / 崩溃自愈）；文件日志 | `src/fleet/service.py` | 可用，真机未跑 |
| 自升级 | sha256 校验 → 换文件脚本 → ack 后退出 → 计划任务拉起 | `src/fleet/updater.py` | 可用，真机未跑 |
| 打包 | PyInstaller 单文件 `chatx-agent.exe`（13 MB）+ `manifest.json` + sha256 | `fleet_agent/build_agent.py` | 已实际打出并冒烟 |
| 安装器 | `Install-ChatXAgent.ps1`（下载校验 / 登记本机智聊 / 注册 / 自启）、`Uninstall-ChatXAgent.ps1` | `fleet_agent/` | 语法通过，真机未跑 |
| 操作端 | `python -m src.fleet.admin new-code / nodes / task / tasks / upgrade` | `src/fleet/admin.py` | 可用 |
| 主控落地 | systemd 单元、nginx 片段、`deploy_controller.sh`（幂等安装 / 升级 / 体检 / 回滚）、`publish_agent.ps1` | `deploy/fleet/` | 脚本就位，**未在 VPS 执行**（改生产需老板授权） |
| 文档 | 契约 §7、部署手册 | `docs/FLEET_CONTROL_CONTRACT.md`、`docs/FLEET_DEPLOY.md` | 已更新 |
| 测试 | 51 例（P0 27 + P1 24） | `tests/test_fleet_control.py`、`tests/test_fleet_p1_service_updater.py` | 全绿 |

## 2. 铁律（双方都遵守）

1. **协议只在 `src/fleet/protocol.py` + 契约文档定义**；要加任务 kind / 字段，先改契约 §4 再改代码，`PROTO_VERSION` 只在破坏性变更时 +1。
2. **不要另起一套 Agent / 主控 / 安装器**。缺功能就在上表对应文件上加，或在 `domains/fleet_control/` 下加新模块。
3. **URL 口径**：`<controller>/api/fleet/...`；公网 controller = `https://bd2026.cc/fleet`，nginx 剥 `/fleet` 前缀。不要在代码里写死域名（读 `fleet_control.public_url`）。
4. **不进 Git**：任何 token / 证书 / 生产 config / `fleet.db` / `fleet_agent/dist|build`（已 gitignore）。
5. **不碰生产**：`bd2026.cc` 的 nginx / systemd / DNS / 证书 / 官网 `public/` 目录，任何一方动之前都要老板明确一句"可以改生产"。
6. **不改对方正在动的文件**：下面 §4 的分工表就是边界；跨界先在 PR 里评论。
7. **改了就跑**：`python -m pytest tests/test_fleet_control.py tests/test_fleet_p1_service_updater.py -q`，绿了再推。
8. **心跳 / 回执里永远没有聊天原文、手机号列表**（协议层 allowlist，别绕）。

## 3. 需要对接确认的事项（请 grok bot 逐条回复：同意 / 有异议 + 理由）

| # | 事项 | Devin 的默认方案 | 需要对方给的输入 |
|---|---|---|---|
| A | 官网 `public/downloads/fleet/` 静态目录是否可直接由 nginx 提供（无鉴权、允许 `.exe` `.ps1` `.json`） | `publish_agent.ps1` scp 到 `/home/ubuntu/yuntech/public/downloads/fleet/`，走官网现有静态路由 | 官网实际静态根目录、是否有 CDN 缓存（manifest.json 需短缓存或 no-cache） |
| B | 主控走主站前缀 `/fleet/`（A 方案）还是子域 `fleet.bd2026.cc`（B 方案） | A：零 DNS/证书改动；控制台 `/fleet/console` 够用 | 若官网前端路由（SPA）已占用 `/fleet` 路径，告知，改用 B |
| C | `web_admin` 登录 / 静态资源等非 `/fleet/*` 路径在 A 方案下不暴露；控制台页面若依赖它们需改 | Devin 在真机联调时验证；有问题给 `/fleet/console` 单独补静态路由 | 无 |
| D | 官网首页 / 导航是否要加"多机部署"入口指向 `/fleet/` | 不加，`/fleet/` 独立页 | 老板定 |
| E | 代码签名证书 | 无证书先不签；有了在 build 与 manifest 之间插 signtool | 证书从哪来、由谁保管 |
| F | 智拓（ReachX）是否也接 fleet | 本轮只有智聊实例；Agent `instances[]` 是通用的（url + token），智拓只要有 `/api/ping`、`/api/accounts/fleet-health` 同款端点就能挂 | 智拓侧端点清单 |
| G | `push_config` 是否需要 | 继续拒绝，等安全的 patch 设计（白名单键、签名） | 需求场景 |
| H | 首台真机试装由谁做 | Devin 有穿透到开发机，可自己装到开发机试；机房电脑需老板给一台 | 机器 + 注册码发放权限 |

## 4. 分工建议（避免撞车）

- **Devin（继续）**：首台真机联调（安装器 / 计划任务 / 一次真实 upgrade）→ 修真机暴露的问题；主控控制台补"集中扫码"UI（`login_qr` 已有任务层）；VPS 部署（拿到授权后）；P2：升级失败自动回滚、批量任务、节点分组策略。
- **grok bot（建议接）**：官网侧（§3 A/B/D：静态下载目录、nginx include、首页入口）；智拓侧 fleet-health / ping 端点（§3 F）；代码签名流程（§3 E）；文档校对 / 面向机房运维的图文安装说明（基于 `docs/FLEET_DEPLOY.md` §3）。
- **都不要做**：再写一个 Agent、另起主控存储、改 `protocol.py` 常量、把 token 写进任何脚本。

## 5. 一句话验收（做完哪一步就能对外说"能装了"）

`deploy_controller.sh --check` 全绿 + 官网 `manifest.json` 可下 + 一台机房电脑装完在 `/fleet/console` 显示在线并 `ping` 返回 done。
