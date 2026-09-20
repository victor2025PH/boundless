# qq-personal 边车维护手册（工程）

> 对象：维护「QQ 个人号（协议登录）」自研连接边车 `services/qq-personal/` 的工程师。
> 用户侧说明见 `docs/QQ个人号接入协议与风险须知.md`；产品定位与许可见 `services/qq-personal/NOTICE.md`。

## 1. 它是什么、边界在哪

- 一个随桌面壳打包、由 `desktop/sidecar-launcher.js`（`SPECS.qq`，端口 **8792**）自动拉起的 Node 进程。
- 对上暴露 **Milky 1.3 子集**（`POST /api/{api}` + `GET /event` WS）+ 智聊扩展 `x_*`，供
  `src/integrations/qq_milky.py` 的 `QQPersonalWorker` 与 `qq_protocol_login.py` 消费。
- 对下经 **驱动接口** `ntq/driver.js`（契约已冻结，见其 JSDoc）驱动本机 QQNT 客户端：
  - `ntq/qqnt-driver.js`（真实注入，GPL-2.0 移植，**去风险验证通过后接入**）；
  - `ntq/mock-driver.js`（离线自测/CI；真驱动缺席时 `createDriver` 自动回退并在 `/health.driver_reason` 如实标注）。
- **不做的事**：不连任何外部签名服务、不要任何第三方 token、不随包分发 `QQ.exe`。

## 2. 目录

| 文件 | 作用 |
|---|---|
| `server.js` | Milky 外壳、鉴权、`/health`、API 分发、`x_qq_status`/`x_download_qq` |
| `ntq/driver.js` | 驱动选择与契约（`QQ_DRIVER=qqnt|mock`） |
| `ntq/mock-driver.js` | 假驱动（拉真二维码、自动登录、假收发） |
| `ntq/qqnt-driver.js` | **真驱动**（B 段 2026-09-10）：定位 → 版本表 → 注入运行时 QQ → spawn → 管道握手；契约方法 → agent RPC |
| `ntq/qqnt-versions.js` | `SUPPORTED_QQNT` 真驱动版本表（build → 注入路径 / 入口 / API 档 / 验证记录）；表外 → `UnsupportedQqVersion` |
| `ntq/qqnt-agent.cjs` | QQ 主进程内 agent（被复制进运行时 `resources/app/` 为 `zhiliao_qqnt_agent.cjs`）：挂 wrapper.node、旁路监听、调内核 |
| `ntq/qqnt-ipc.js` | 边车 ⇄ agent 本机命名管道 RPC（随机 token 经环境变量，不落盘） |
| `ntq/qqnt-elements.js` | Milky 段 ⇄ NT 元素纯函数、`imageMeta` |
| `ntq/locate.js` | 定位本机 QQNT（runtime → 注册表 → 默认路径）、`SUPPORTED_QQ_BUILDS` 下载锁定表 |
| `ntq/qq-download.js` | 腾讯官方直链按需下载 + NSIS 静默安装 + 进度状态机 |
| `test/locate.test.js` | 定位/锁定/状态机单测 |
| `test/driver-contract.test.js` | mock 与 qqnt 对同一契约表跑同一组 shape 断言（qqnt 用假 agent）+ 版本表 / 注入 / 回退 |
| `test/ipc.test.js` | 管道 RPC 真连：token 鉴权 / call-ret / 事件 / 断连 / 握手超时 |
| `LICENSE.GPL-2.0.txt` / `NOTICE.md` / `SOURCE_OFFER.md` | GPL 合规三件 |

## 3. 环境变量（由 sidecar-launcher `buildSidecarEnv` 注入）

| 变量 | 含义 |
|---|---|
| `QQ_MILKY_PORT` / `PORT` | 监听端口（8792，与 `qq_milky.DEFAULT_MILKY_URL` 同源，端口漂移门禁钉住） |
| `QQ_MILKY_TOKEN` | Milky Bearer（留空=不校验，仅本机回环） |
| `QQ_DRIVER` | `qqnt`（默认）/ `mock` |
| `QQ_SESSIONS_DIR` | QQ 登录态落盘目录（用户可写区 `<dataDir>/qq-sessions`） |
| `QQ_RUNTIME_DIR` | 按需下载的 QQ 运行时目录（`<dataDir>/qq-runtime`） |
| `PY_INGEST_URL` / `PY_STATUS_URL` / `PY_API_TOKEN` | 与其它边车对齐的回推旁路（默认关，主链路是 Milky `/event`） |

## 4. QQ 升级时怎么办（最常见的维护动作）

腾讯发布新版 QQNT 后，注入点（`resources/app/package.json` 的 `main`、wrapper.node 位置）与内核 service
方法签名都可能变。**两张表、两道闸**：

| 表 | 文件 | 管什么 | 表外行为 |
|---|---|---|---|
| 下载锁定表 `SUPPORTED_QQ_BUILDS` | `ntq/locate.js` | 「能往 `QQ_RUNTIME_DIR` 装哪个版本」 | `x_qq_status.supported=false` → 弹窗给「下载并安装 QQ」 |
| 真驱动版本表 `SUPPORTED_QQNT` | `ntq/qqnt-versions.js` | 「装了的这版能不能注入」（路径 + API 档 + 验证记录） | `resolveQqntTarget` 抛 `unsupported qq version x.y.z` → `driver.js` 回退 mock，`/health.driver_reason` 带原因，工作台 QQ 账号卡显示演示态 |

流程（一个 build 从「能下载」到「能注入」）：

1. 先只进下载表：`SUPPORTED_QQ_BUILDS` 加 `{ build: { version, url } }`（`url` 必须是 `dldir1.qq.com` / `dldir1v6.qq.com`
   官方直链）。此时用户装上新版后真驱动仍回退 mock（如实标演示态）——**这是设计，不是 bug**。
2. 在公司验证机（**测试号**，不用任何人的主号）：`QQ_RUNTIME_DIR` 指向新版运行时，`QQ_DRIVER=qqnt`，
   `ZHILIAO_QQ_DEBUG=1`（agent 控制台日志）。先在 `SUPPORTED_QQNT` **本地临时**加一行（`verified: ""`），
   看 `/health.driver` 是否变 `qqnt`：握手超时 = 注入点变了（对照 `resources/app/package.json` 的 `main` 与
   wrapper.node 路径改 `appRel / entryMain / wrapperRel`）；握手通过但方法报错 = 内核签名变了，在
   `qqnt-agent.cjs` 的 `API_PROFILES` **另开一档**（不改旧档），版本表该行 `api` 指到新档。
3. 跑 C 段清单：扫码 → 收一条 / 发一条 / 收图 / 发图 → 撤回 → 断网重连 → 连续 72 h 挂机不掉线、无风控提示。
   出现风控提示即停并记录到落点表，该 build **不进**版本表。
4. 通过 → 版本表该行填 `verified: "YYYY-MM-DD 谁 挂机 xx h"`，必要时把 `PINNED_QQ_BUILD` 指到新版。
5. 跑 §6 门禁（含 `node --test test/locate.test.js test/driver-contract.test.js test/ipc.test.js`）。
6. 经边车热更通道（`desktop/hotpatch-stage.js`）先灰度再全量。

用户机上的判定链：`x_qq_status.supported=false` → 弹窗 `qq_not_installed` 卡 →「下载并安装 QQ」把锁定版装进
`QQ_RUNTIME_DIR`（**不动用户自己的 QQ**：真驱动只改运行时目录那份的 `package.json`，对 `source=registry/default`
的用户自装 QQ 一律拒绝注入并回退 mock）→ 定位器优先取 runtime 版本 → 版本表命中才注入。

## 5. 排障

| 现象 | 看哪里 |
|---|---|
| 弹窗「连接服务未运行」 | 桌面壳 `desktop:sidecar-status`（absent=没随包 / failed=起不来 / port-conflict）；`<dataDir>/logs/qq-sidecar.log` |
| 弹窗要「下载并安装 QQ」但用户明明装了 QQ | `x_qq_status.qq_version` 不在 `SUPPORTED_QQ_BUILDS` → 走 §4 |
| 二维码出不来 | `/health.driver`：`mock`+`driver_reason` 说明真驱动未接入/加载失败 |
| `driver_reason` = `qq not installed` | 运行时目录空：走「下载并安装 QQ」 |
| `driver_reason` = `qq install is user-owned (source=registry)` | 定位到的是用户自己的 QQ，按红线不注入；让弹窗把锁定版装进 `QQ_RUNTIME_DIR` |
| `driver_reason` = `unsupported qq version 9.9.x-xxxxx` | 版本不在 `SUPPORTED_QQNT`：走 §4，别硬加 |
| `driver_reason` = `agent handshake timeout` | QQ 起了但 agent 没连回：`resources/app/package.json.main` 被 QQ 自更新还原（重跑即幂等再注入）/ 注入点变了（§4 第 2 步） |
| 真驱动在线但发图/发文件失败 | agent `stageFile` 依赖 `getRichMediaFilePathForGuild` 签名；对照 `API_PROFILES` 另开档 |
| 收图无 `temp_url` | 本版只给 `resource_id`（NT 原图链接需 rkey，C 段按真机回包补） |
| 扫码后一直 pending | `get_login_info` 仍 -403：手机端未确认，或注入未成功（看边车日志） |
| 账号栏「需重新登录」 | `bot_offline(reason)`：被踢/异地登录；点账号菜单重新扫码 |
| 运维面看不到版本/驱动 | `worker.status()` 透出 `qq_version / login_state / driver`（来自 `get_impl_info` 扩展字段） |

## 6. 门禁（改动前后必跑）

- `tests/test_qq_milky.py`（provider 三闸：风险须知 / 服务不可达 / QQ 未安装；端点；worker）
- `tests/test_qq_e2e_parity.py`（真 worker→编排器→InboxStore 全链 + 与其它平台对齐）
- `desktop/test/sidecar-launcher.test.js`（SPECS.qq、svcId 与 `/health.svc` 一致、restart）
- `desktop/test/package-layout.test.js`（`extraResources` 覆盖 REQUIRED 表；`qq-sessions/qq-runtime` 绝不随包）
- `tests/test_desktop_seed_deliverable.py`（QQ **不进**桌面种子默认开表——它是 opt-in + 风险确认）
- `tests/test_i18n_coverage.py`（`inbox.connect.qq_*` 五语；sealed 页禁内联 CJK）
