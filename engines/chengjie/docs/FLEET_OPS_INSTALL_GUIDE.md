# 智控节点（舰队节点）机房运维安装说明

> 面向：机房运维 / 非开发人员。
> 依据代码：`feat/chengjie-player-care-fleet` @ `fc2e160d`（Agent 版本 `AGENT_VERSION = "0.3.1"`，见 `src/fleet/agent.py`）。
> 本文只描述代码里已经实现的流程；标 **【待确认】** 的地方表示代码里没有定论，或者还没有在真机上验证。
> 截图位置用 **【截图占位：…】** 标出，文档里暂时没有放图片。
> 技术细节请看 `docs/FLEET_DEPLOY.md` §3 和 `docs/FLEET_CONTROL_CONTRACT.md`，本文不替代它们。

---

## 0. 先弄清几个名词

| 名词 | 是什么 | 代码位置 |
|---|---|---|
| 舰队节点 / 节点 Agent | 装在机房每台 Windows 电脑上的小程序 `chatx-agent.exe`，以计划任务 **`ChatX Fleet Agent`**（SYSTEM 身份、开机自启）运行，主动连主控 | `src/fleet/agent.py`、`src/fleet/service.py` |
| 主控 / 舰队控制台 | 管理员批准电脑、看在线状态、下发任务的网页：`<主控地址>/console`，默认 `https://bd2026.cc/fleet/console` | `domains/fleet_control/web/routes.py`、`templates/fleet_console.html` |
| 安装包 | `ChatXAgentSetup.exe`（Inno Setup 6 编出的双击安装包） | `fleet_agent/setup/ChatXAgent.iss`、`fleet_agent/build_setup.ps1` |
| 配对码 | 安装完成页上显示的一串码，控制台「待批准」列表里能看到同一个码，用来确认“批准的是哪台电脑”。**配对码可以给人看，不是密码** | `src/fleet/agent.py`（写 `pairing.txt`）、`ChatXAgent.iss`（`ApplyFinishedCopy`） |
| 机房安装码 / 机房密钥（`rk_…`） | 管理员在后台生成的「机房安装包」zip（内含 `room.key`）。用它装上的电脑**自动批准**并进入指定分组，直到次数用完或过期。**这是机密** | `src/fleet/roompack.py`、`routes.py` 的 `/fleet/dl/{token}` |
| 一次性注册码 | 12 位 Crockford 码（显示成 `XXXX-XXXX-XXXX`，不区分大小写），默认 15 分钟有效；只在命令行安装时用 `-Code` 传入 | `docs/FLEET_DEPLOY.md` §3、`fleet_agent/Install-ChatXAgent.ps1` |
| 本机页「舰队节点」 | 只看这一台电脑的页面，地址 `http://127.0.0.1:47321/`（只监听本机）。四种状态：**待批准 / 在线 / 离线 / 已拒绝** | `src/fleet/panel.py`（`PANEL_PORT = 47321`）、`src/fleet/local_status.py` |

---

## 1. 安装前准备

1. **系统**：Windows 10 / 11 / Server，**64 位**（安装包 `ArchitecturesAllowed=x64`，见 `ChatXAgent.iss`）。
2. **管理员权限**：安装包 `PrivilegesRequired=admin`，双击后会弹 UAC，点「是」。命令行安装脚本不是管理员身份运行时会直接拒绝（`Install-ChatXAgent.ps1`：`run this script as Administrator`）。
3. **网络**：电脑能通过 HTTPS **出站**访问主控地址（默认 `https://bd2026.cc/fleet`）。节点是主动连主控，机房不用开入站端口。
4. **本机智聊 / 幻颜不是必需的**：安装时会自动找本机智聊（`config.local.yaml`，或回环端口 `18799` / `18797`）和幻颜 AvatarHub（`127.0.0.1:9000`，只做健康检查），见 `src/fleet/detect.py`。一个都没有也能装，这台电脑就作为只发心跳的节点。
5. **杀毒 / SmartScreen**：目前 exe **没有代码签名**，第一次运行时 Windows SmartScreen 可能拦一下，需要点「更多信息 → 仍要运行」（`docs/FLEET_DEPLOY.md` §2、`docs/FLEET_CONTROL_CONTRACT.md` §6）。签名准备见 `docs/FLEET_CODE_SIGNING_PREP.md`。
   【截图占位：SmartScreen「Windows 已保护你的电脑」→「更多信息」→「仍要运行」】
6. **目录说明**（不用手动创建）：
   - 程序：`%ProgramFiles%\ChatX Agent\`（`DefaultDirName={autopf}\ChatX Agent`）
   - 状态和身份：`%ProgramData%\ChatX\fleet\`（`agent.json`、`pairing.txt`、`logs\agent.log` 等）。安装器会把这个目录锁成只有 SYSTEM 和 Administrators 能访问；它的上一级 `%ProgramData%\ChatX` 对普通用户只读。**不要手动改这两个目录的权限。** 如果本机还有其它软件要往 `%ProgramData%\ChatX` 下面写文件，会写不进去（`docs/FLEET_DEPLOY.md` §5）。【待确认：机房电脑上是否有其它 ChatX 组件依赖这个目录可写】

---

## 2. 下载

有两种方式，**二选一**。

### 2.1 单台电脑：公开下载页

1. 浏览器打开 `https://bd2026.cc/fleet/`（下载页，不用登录）。
2. 点「下载安装包（ChatXAgentSetup.exe）」。页面上会写版本号和安装包的 SHA-256（`templates/fleet_home.html`）。

【截图占位：/fleet/ 下载页，标出「下载安装包」按钮和 SHA-256】

> **【待确认】** 公网下载目录 `https://bd2026.cc/downloads/fleet/` 现在还没有放发布文件（`docs/FLEET_DEPLOY.md` §6 写着“目前未就位，安装器默认下载地址会 302 循环”）。上线之前如果打不开下载页或下载失败，请直接联系管理员，不要从别的来源找安装包。

（可选）核对文件完整性，在 PowerShell 里运行：

```powershell
(Get-FileHash .\ChatXAgentSetup.exe -Algorithm SHA256).Hash.ToLower()
```

结果应该和下载页上显示的 SHA-256 一致。

### 2.2 整间机房：机房安装包（管理员提供）

1. 管理员在控制台点「机房安装包」生成一个链接（`/fleet/dl/<token>`，**只显示这一次**），下载得到 `ChatXAgent-room.zip`。
2. zip 里有：`room.key`、`Install.cmd`、`Install-Silent.cmd`、`README.txt`（`src/fleet/roompack.py`）。
3. **这个 zip 是机密**：不要发邮件、不要贴到聊天群、不要放到公开下载页。整间机房装完以后删掉 zip，并请管理员在后台吊销这把密钥（`roompack.py` 里 README 的原文就是这个要求）。

【截图占位：解压后的 zip 目录，四个文件】

---

## 3. 安装

### 3.1 单台电脑：双击安装包（走「待批准」）

1. 双击 `ChatXAgentSetup.exe`，弹出 UAC 时点「是」。
   【截图占位：UAC 提示】
2. 按向导点「下一步」。可以勾选「在桌面创建「舰队节点」快捷方式」（默认勾选）。
   【截图占位：安装向导 — 附加图标页】
3. 安装器会复制文件，锁定状态目录，连接主控，并安装计划任务（`CurStepChanged` → `bootstrap.ps1`）。
4. **完成页**会显示：
   - **配对码 XXXX…**，以及三步提示：①把配对码发给管理员，舰队控制台里能看到同一个码；②管理员批准后，点「打开舰队节点」并刷新本页；③状态变为在线后，可以从本页打开舰队控制台。
   - 两个按钮：「打开舰队节点」（本机页）、「打开舰队控制台」。
   【截图占位：安装完成页，标出配对码和两个按钮】
5. **记下配对码，发给管理员**（配对码不是密码，可以发）。

如果完成页显示 **「安装未完成」**（“文件已复制，但没有连上主控，计划任务没有安装”），请**用管理员身份重新运行安装包**，并参考第 8 节排障。

### 3.2 整间机房：机房安装包（免逐台批准）

在每台电脑上：

1. 解压 `ChatXAgent-room.zip`。
2. 双击 **`Install.cmd`**，会提示提权，点「是」；要静默批量安装就改用 **`Install-Silent.cmd`**。
3. 脚本做的事（`src/fleet/roompack.py`）：
   - 只接受 https 下载地址，把 `ChatXAgentSetup.exe` 下载到临时目录下的一个**随机新文件名**（不会复用 TEMP 里已经有的 exe）；
   - 用主控钉住的 **SHA-256** 校验，对不上就删除并退出；
   - 用 `/CONTROLLER=... /ROOMKEYFILE=...` 调起安装包，完成后删除临时 exe 和临时 key 文件。
4. 每装成功一台消耗一次使用次数。装上的电脑会**直接进入管理员指定的分组，不用在后台点批准**。

> 注意（`docs/FLEET_DEPLOY.md` §3）：
> - 已经被吊销的电脑，拿对了机房密钥也**不会自动复活**，会进「待批准」，控制台标 `this machine was revoked`，需要管理员再批一次。
> - 已有节点如果分组是空的，机房密钥不会自动换 key，也会进「待批准」。
> - 如果机房包提示 `setup sha256 is not pinned` 或 `installer URL must be https`，说明主控那边的发布配置不完整，请联系管理员，**不要自己改 cmd 文件**。

### 3.3 命令行安装（高级，一般不用）

以管理员身份打开 PowerShell（`fleet_agent/Install-ChatXAgent.ps1`）：

```powershell
# 走待批准（和双击安装包一样）
powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Controller https://bd2026.cc/fleet
# 有管理员给的一次性注册码时（跳过待批准）
powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Controller https://bd2026.cc/fleet -Code XXXX-XXXX-XXXX
# 离线：用本地 exe，不下载
powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Exe .\chatx-agent.exe
```

已经装好的安装包也能静默安装：`ChatXAgentSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`（`ChatXAgent.iss` 文件头注释）。

---

## 4. 输入「机房安装码」

- **双击安装包的时候不用输任何码。** 代码里没有让人手工输入安装码的界面。
- 所谓“机房安装码”，实际上是**机房安装包 zip 里的 `room.key` 文件**，`Install.cmd` 会自动把它交给安装包（`/ROOMKEYFILE=`），**全程不用手动复制粘贴**。
- 也可以把 `room.key` 放在 `ChatXAgentSetup.exe` 同一个目录里，再双击安装包，安装包会自动读取（`CurStepChanged`：`{src}\room.key`）。
- 命令行对应的参数是 `Install-ChatXAgent.ps1 -RoomKeyFile .\room.key`（只从文件读取，不在命令行里明文传递）。
- **一次性注册码**（12 位，15 分钟有效）只用于命令行 `-Code`。同一个 IP 10 分钟内输错 8 次会被锁定一段时间，锁定期间输对也不算（`docs/FLEET_DEPLOY.md` §3）。

---

## 5. 「待批准」与批准（管理员操作）

1. 管理员打开舰队控制台 `https://bd2026.cc/fleet/console`，在「待批准」列表里找到**配对码一致**的那台电脑，列表里还能看到主机名、IP 和探测到的实例。
   【截图占位：控制台「待批准」列表，标出配对码列和批准按钮】
2. 点「批准」，可以填分组；不填就进 `pending-default`。
3. 这台电脑之前如果已经有节点（例如重装了系统），控制台会先提示 `approving will rotate key of n_xxx`，确认后才会换新 key。
4. 同一个 `machine_id` 有多条待批准时，会标 `duplicate machine_id`，请核实后只批一条。
5. 命令行等价操作（`src/fleet/admin.py`，需要环境变量 `CHATX_FLEET_CONTROLLER` 和 `CHATX_FLEET_ADMIN_TOKEN`，**token 只由管理员持有**）：

```powershell
python -m src.fleet.admin pending
python -m src.fleet.admin approve <request_id> --group 机房A
python -m src.fleet.admin approve <request_id> --confirm-rotate   # 已有节点时
python -m src.fleet.admin reject <request_id>
```

6. 批准之后，节点会自己轮询领到 key（`/api/fleet/enroll/poll`），不需要重装。

> **【待确认】** 公网控制台 `https://bd2026.cc/fleet/console` 能不能正常打开并登录，还需要真机联调确认。原因见 `docs/FLEET_HANDOFF_REPLIES_A_H.md` 的 C 项：控制台页面依赖 `/static/`、`/login` 和根路径下的 `/api/fleet/...`。在确认之前，管理员可以先用上面的 `admin` 命令行批准。

---

## 6. 确认上线：控制台在线 + ping

1. **在本机看**：双击桌面或开始菜单的「舰队节点」（`Open-Panel.vbs` → `chatx-agent ui`），打开 `http://127.0.0.1:47321/`，状态应该从「待批准」变成「**在线**」。
   【截图占位：本机页「在线」状态】
2. **在控制台看**：节点列表里这台电脑显示在线。
3. **ping 测试**：控制台在节点上选任务 `ping` 下发（控制台支持的任务：`ping`、`pull_overview`、`account_health`、`login_qr`、`stop_account`、`restart_instance`，见 `fleet_console.html`），任务状态变成 `done` 就是通了。命令行：

```powershell
python -m src.fleet.admin nodes
python -m src.fleet.admin task <node_id> ping
python -m src.fleet.admin tasks --node <node_id> --status done
```

   【截图占位：控制台任务列表，ping 状态为 done】
4. **验收标准**（`docs/FLEET_GROK_HANDOFF.md` §5）：一台机房电脑装完，在 `/fleet/console` 显示在线，并且 `ping` 返回 done。

命令行查看本机状态（管理员 CMD）：

```bat
"%ProgramFiles%\ChatX Agent\chatx-agent.exe" --state-dir "%ProgramData%\ChatX\fleet" status
"%ProgramFiles%\ChatX Agent\chatx-agent.exe" --state-dir "%ProgramData%\ChatX\fleet" service-status
```

---

## 7. 升级与卸载

### 7.1 升级

- **远程升级（推荐，由管理员发起）**：`python -m src.fleet.admin upgrade --manifest https://bd2026.cc/downloads/fleet/manifest.json --group 机房A --yes`。
  - 只会发给**在线**、并且版本和 manifest 不同的节点。
  - 节点校验 sha256 → 换文件（旧 exe 留作 `.bak`）→ 通过一次性计划任务 `ChatX Fleet Agent Upgrade` 重新拉起，几十秒后 `nodes` 里显示新版本（`src/fleet/updater.py`、`docs/FLEET_CONTROL_CONTRACT.md` §7）。
  - 控制台的任务下拉里**没有** `upgrade`，目前只能用命令行发起。
- **本机升级**：直接再运行一次新版的 `ChatXAgentSetup.exe`。安装器会先停掉 `ChatX Fleet Agent` 和 `ChatX Fleet Agent Upgrade` 两个任务，再覆盖文件（`PrepareToInstall` → `StopOurAgent`）。已注册的电脑会保留身份，不用重新批准。
- **回滚**：Agent 里**没有自动回滚**（列为 P2）。人工回滚的办法是：让管理员用指向旧版 `chatx-agent-<旧版本>.exe` 的 manifest 再下发一次 upgrade（`docs/FLEET_DEPLOY.md` §4）；或者在本机运行旧版安装包。【待确认：旧版 `ChatXAgentSetup.exe` 目前没有按版本号另存，`publish_agent.ps1` 只另存了 `chatx-agent-<ver>.exe`】
- 从旧版（目录没有锁定的版本）升级后，`%ProgramData%\ChatX` 下会多出 `fleet.legacy-<guid>` 目录，**确认新节点正常后再请管理员决定是否删除**，并建议在主控侧轮换这台电脑的 node_key；本机实例需要重新探测，`restart_cmd` 需要管理员重新配置（`docs/FLEET_DEPLOY.md` §5）。

### 7.2 卸载

- **通过「设置 → 应用」或「控制面板 → 程序和功能」卸载**：找到 **ChatX Fleet Agent** 卸载。卸载会先停止并删除计划任务，再删除程序文件；**默认保留** `%ProgramData%\ChatX\fleet`（里面有节点身份，保留的话重装后同一台电脑复用 node_id）。
- 要连状态一起删：用卸载参数 `/REMOVESTATE=1`（`CurUninstallStepChanged`）。静默卸载可执行文件是 Inno 默认的 `%ProgramFiles%\ChatX Agent\unins000.exe`【待确认：以真机上的实际文件名为准】，例如 `unins000.exe /VERYSILENT /REMOVESTATE=1`。
- 命令行卸载（`fleet_agent/Uninstall-ChatXAgent.ps1`）：`powershell -ExecutionPolicy Bypass -File Uninstall-ChatXAgent.ps1 [-PurgeState]`。
- **卸载只影响本机。** 要让主控那边也不再接受这台电脑，请管理员在控制台「吊销」该节点。

---

## 8. 排障

| 现象 | 先看什么 | 说明 / 处理 |
|---|---|---|
| 完成页显示「安装未完成」 | 用管理员身份重新运行安装包 | 文件复制了，但没连上主控，计划任务没装（`ChatXAgent.iss`：`BootstrapOk=False`）。检查出站 HTTPS 和主控地址 |
| 安装时提示 state directory / ACL / reparse point 之类的错误 | 截图发给管理员 | 安装器发现状态目录或它的上级目录权限不安全，或者是链接（junction），会在写任何密钥之前中止（`LockStateDir` / `AssertStateParent`）。**不要自己改权限后硬装** |
| 本机页一直「待批准」 | 配对码有没有发给管理员；管理员有没有在控制台批准 | 批准后刷新本机页 |
| 本机页「已拒绝」 | 联系管理员 | 管理员拒绝了这次申请；需要重新发起时由管理员决定 |
| 本机页「离线」 | 计划任务是否在运行；网络 | `schtasks /Query /TN "ChatX Fleet Agent" /V`；日志 `%ProgramData%\ChatX\fleet\logs\agent.log` |
| 机房包提示 `setup sha256 is not pinned` / 下载失败 / 校验失败 | 联系管理员 | 主控发布配置或下载目录的问题，不是本机的问题 |
| 控制台里看不到实例 | 本机智聊是否在运行 | 只识别 `config.local.yaml` / `18799` / `18797` 和 AvatarHub `9000`；没有实例也能在线 |
| 需要前台调试 | 管理员 CMD 运行 `"%ProgramFiles%\ChatX Agent\chatx-agent.exe" --state-dir "%ProgramData%\ChatX\fleet" run -v` | Ctrl-C 退出，不影响计划任务（`docs/FLEET_DEPLOY.md` §3） |
| 注册码提示锁定 | 等锁定期过去 | 同一 IP 10 分钟内输错 8 次会被锁定 |

**发给管理员的信息**：主机名、完成页或本机页的截图、`status` 命令的输出。**不要发** `agent.json`、`room.key`，也不要发任何 token。

---

## 9. 安全须知（运维必读）

- 公开安装包里**不含**主控管理凭据，也不含机房密钥（`docs/FLEET_DEPLOY.md` §5）。
- `room.key` / 机房 zip / 注册码不要进聊天群、邮件、网盘公开链接；机房装完就吊销。
- 节点身份（`node_key`）只在 `%ProgramData%\ChatX\fleet\agent.json`，不要拷贝，不要外发。
- 心跳和回执里没有聊天原文，也没有手机号列表（协议层白名单）。

---

## 附：本文未覆盖 / 待确认清单

1. 公网下载目录 `/downloads/fleet/` 还没就位（`docs/FLEET_DEPLOY.md` §6）。
2. 公网控制台在路径前缀方案（A）下能否正常登录和使用（见 A–H 回复 C 项）。
3. 双击安装包的 UAC 和完成页还没在真机上完整点过一遍（`docs/FLEET_DEPLOY.md` §6 写着“要在 Windows 上点一次才算真机确认”）。
4. 卸载程序的实际文件名（Inno 默认是 `unins000.exe`）。
5. 旧版安装包是否按版本号保留，以便回滚。
6. 所有截图位置等真机联调后补上。
