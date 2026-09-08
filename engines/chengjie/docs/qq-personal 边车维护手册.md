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
| `ntq/locate.js` | 定位本机 QQNT（runtime → 注册表 → 默认路径）、`SUPPORTED_QQ_BUILDS` 版本锁定表 |
| `ntq/qq-download.js` | 腾讯官方直链按需下载 + NSIS 静默安装 + 进度状态机 |
| `test/locate.test.js` | 定位/锁定/状态机单测（`node --test test/locate.test.js`） |
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

腾讯发布新版 QQNT 后，注入 hook 的偏移可能变化。流程：

1. 在公司验证机安装新版 QQ，跑一次「扫码 + 收发 + 撤回 + 群管理」实测。
2. 通过 → 在 `ntq/locate.js` 的 `SUPPORTED_QQ_BUILDS` 加一行 `{ build: { version, url } }`（`url` 必须是
   `dldir1.qq.com` / `dldir1v6.qq.com` 官方直链），必要时把 `PINNED_QQ_BUILD` 指到新版。
3. 真驱动若需新偏移，在 `qqnt-driver.js` 的版本表补齐。
4. 跑 `node --test test/locate.test.js` + Python `tests/test_qq_milky.py tests/test_qq_e2e_parity.py`。
5. 经边车热更通道（`desktop/hotpatch-stage.js`）先灰度再全量。

用户机上的判定链：`x_qq_status.supported=false` → 弹窗 `qq_not_installed` 卡 →「下载并安装 QQ」把锁定版装进
`QQ_RUNTIME_DIR`（**不动用户自己的 QQ**）→ 定位器优先取 runtime 版本。

## 5. 排障

| 现象 | 看哪里 |
|---|---|
| 弹窗「连接服务未运行」 | 桌面壳 `desktop:sidecar-status`（absent=没随包 / failed=起不来 / port-conflict）；`<dataDir>/logs/qq-sidecar.log` |
| 弹窗要「下载并安装 QQ」但用户明明装了 QQ | `x_qq_status.qq_version` 不在 `SUPPORTED_QQ_BUILDS` → 走 §4 |
| 二维码出不来 | `/health.driver`：`mock`+`driver_reason` 说明真驱动未接入/加载失败 |
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
