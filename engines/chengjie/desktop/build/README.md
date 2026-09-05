# 桌面端打包（P0 本地自包含交付）

目标：让用户**双击安装、零 Python 依赖**即可运行——后端被打成 sidecar 二进制随包分发，
Electron 启动时自动拉起、退出时回收（见 `desktop/backend-launcher.js`）。

## 两步走

### ① 打后端 sidecar（PyInstaller）

在装好 `requirements.txt` 的同款 Python 环境、且**与目标 OS 一致**的机器上跑
（Windows 包在 Windows 上打，mac 包在 mac 上打；PyInstaller 不跨平台）：

```bash
cd desktop
pip install pyinstaller
npm run build:backend          # = python build/build_backend.py
```

产出：`desktop/build/backend-dist/backend(.exe)`（onedir，默认排除 whisper/torch 等重依赖控包体）。
需要本地 ASR/OCR 时加 `python build/build_backend.py --keep-heavy`。

### ② 打桌面安装包（electron-builder）

```bash
cd desktop
npm install
npm run dist:win        # 或 dist:mac / dist（当前平台）
# 改过 src/ 模板后一键：先重打 sidecar 再出安装包
npm run dist:win:fresh
```

`extraResources` 会把 `build/backend-dist/` 复制进安装包的 `resources/backend/`。
运行时 `backend-launcher.js` 在发布态优先用 `resources/backend/backend(.exe)`，
开发态回退系统 Python 跑仓库根 `main.py`。

**安装器界面资产（2026-09-05）**：向导的品牌位图 `installerSidebar.bmp` /
`installerHeader.bmp` 由 `brand-assets/build_installer_art.py` 生成并镜像到本目录（不要手
工 P 图）；「安装前须知」页是按语言的 `license_zh_CN.rtf` / `license_en_US.rtf`，源文件是
同目录的 `installer-notice.<lang>.txt`，改完跑 `node build/write-installer-notice.js` 重新生成
（门禁按源 sha1 核对新鲜度）。`write-build-info.js` 还会写 `build/flavor.nsh`（gitignore）：非
clean 形态的向导带琥珀「内测版」角标（`*-internal.bmp` 变体）与品牌栏后缀，缺文件按内测处理，
所以对外干净包必须走 `npm run dist:win:clean`。页面流、颜色令牌、字体、DPI 等全在
`installer.nsh` 顶部注释与 `.cursor/rules/chatx-desktop-install.mdc`「安装器界面/品牌层」一节。
改完 `installer.nsh` 用隔离测试用户实弹：`build\smoke_uninstall.ps1`（静默通道契约）+
`build\smoke_installer_ui.ps1`（交互向导全程截图），都需管理员、用 Windows PowerShell 5.1 跑。

**新鲜度门禁（2026-08-08）**：`predist` / `predist:win` 会跑
`python build/check_backend_freshness.py`。`build:backend` 成功后在
`backend-dist/.source-fingerprint.json` 落源码内容指纹；若工作树相对该戳已脏，
**拒打安装包**（防再出现「版本号新、sidecar 仍是旧 `_cancelMedia`」）。
手动自检：`npm run check:backend-fresh`。

**增量刷新档（2026-08-18）**：门禁红且变化**只落在 datas 类资产**
（templates / static / shared/copilot / domains / config 种子 / platform 瘦模块——
包内运行时按文件路径读，不进 exe）时，`npm run refresh:datas` 秒级把它们同步进
`backend-dist/_internal` 并重落 stamp，免跑 ~7.5 分钟 PyInstaller。诚实性由
stamp 逐文件明细的 diff 保证：任何编进 exe 的 `.py` 变了 → exit 3 自拒（须全量
`build:backend`）；旧格式 stamp 无明细 → exit 4（全量重打一次即自举）。static/
domains/platform 走与全量构建同一套暂存清洗（隐私/机密剔除断言原样生效）。
门禁 `tests/test_backend_refresh_datas.py`；`--dry-run` 只看判定不动产物。
一键智能档：`npm run dist:win:smart`＝先试增量刷新，被拒/失败自动落
`build:backend`（含冒烟），然后 `dist:win`——不确定改动范围时用它最省心。

## 运行时行为（生命周期）

- 启动：先探活 `backend.base_url/login`——**已在跑则复用、不重复拉起**（对「先手动起后端」零回归）；
  否则解析命令并 spawn，日志落 `userData/logs/backend.log`。
- 就绪门控：renderer 既有「正在连接后台→自动重连」遮罩自动衔接；
  `desktop:backend-spawn-status` 暴露细化状态（starting/ready/failed…）。
- 退出：`before-quit` 回收后端（Windows `taskkill /T /F`；posix 杀进程组）。
- 关闭自拉起：`config.json::backend.spawn.enabled=false`（完全由用户自管后端）。

## 可写数据目录（已实现）

发布态后端的 config + 运行数据**不写只读安装包**，而是落用户可写目录：

- launcher 在打包态把 `AITR_DATA_DIR=<userData>/data`、`AITR_CONFIG_PATH=<dataDir>/config/config.yaml`
  注入后端 env，并设后端 cwd=`<dataDir>`（使 `logs/`、`*.db`、tmp 等 cwd 相对路径也落可写区）。
- 后端 `ConfigManager` 识别这两个 env（见 `src/utils/config_manager.py`），**首次运行自播种**：
  桌面模式（`AITR_DESKTOP_MODE=1`）优先拷内置**最小种子** `config.desktop.min.yaml`
  （P0-1 A1：无 `YOUR_*` 占位、`translation.engines.order: ["ai"]`，只差首启向导补 AI Key），
  否则回落完整 `config.example.yaml`，落到 `<dataDir>/config/config.yaml`，无需 launcher 搬运。
- 开发态（无 env）行为完全不变（仍用仓库 `config/config.yaml`），零回归。
- PyInstaller bootloader 经 exe 路径定位 `_internal`，与 cwd 无关，故改 cwd 安全；
  `domains/` 等代码目录在打包态从可写区找不到时**优雅降级**（已有 `exists()` 守护）。

## 桌面可启动（无凭证开机，已实现）

纯桌面用户（只用统一收件箱 / 内嵌网页翻译，不用 Telegram 协议号）**无需任何凭证即可开机**：

- 真正的拦路是 `main.py` 旧版**无条件**初始化 config-Telegram 协议客户端——用 example 占位
  `api_id` 连接会失败/挂起，挡住整个进程。（`ConfigManager._validate_config` 的返回值在 `main.py`
  其实**未被用于 gate**，故非根因。）
- 现已门控：`main._telegram_configured()` 判定占位/缺省即「未配置」→ **跳过协议客户端初始化**；
  `create_app()` / `start()` / `stop()` 全部对 `telegram_client=None` 守护。
- 显式桌面模式：launcher 在打包态注入 `AITR_DESKTOP_MODE=1`（或 config `app.desktop_mode: true`），
  即便填了真凭证也强制跳过 config-Telegram，用于「纯收件箱/翻译」形态。
- **serve↔talk 强一致（否则连不上后端）**：随包 `config.example.yaml` 的 `web_admin.port`(18787)/
  占位令牌与桌面默认(18799/`admin`)不符，全新安装会「后端起了但 renderer 连不上」。故 launcher
  按桌面 `config.json::backend.{base_url,token}` 注入 `AITR_WEB_HOST/PORT/TOKEN`，`ConfigManager`
  据此覆盖 `web_admin.{host,port,auth_token}`；并在桌面模式**强制** `web_admin.enabled=true`
  （统一收件箱 / 翻译 / D1 选择器热更新 / D4 受控外发路由都挂在 web 后台下）。
- 不受影响：统一收件箱、内嵌网页翻译、LINE/Messenger/WhatsApp RPA（各自 `enabled` 门控）、
  **QR 扫码登录协议号**（走 orchestrator，不依赖 config 账号）。

## 待硬化（后续迭代）

1. **代码签名**：Windows Authenticode / mac notarization（否则 SmartScreen / Gatekeeper 拦截）。
2. **更新源**：`package.json::build.publish.url` 现为占位，需指向真实静态更新服务器。
3. **包体瘦身**：确认排除重依赖后体积可接受；如需 whisper/OCR 建议改走在线后端而非进包。
