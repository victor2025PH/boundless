# 智控节点 Agent 代码签名准备（交接事项 E）

> 状态：**准备文档 / 方案草案**。本文只描述方案，**不改任何脚本**。实际接入要等证书到位，并由 Devin / 维护者在对应文件里实现。
> 依据代码：`feat/chengjie-player-care-fleet` @ `fc2e160d`。
> 已有口径：`docs/FLEET_DEPLOY.md` §2 写的是“有证书后在 `build_agent.py` 之后加 `signtool sign ...`，manifest 的 sha256 要在签名后再算”；交接文档 §3 E 的默认方案是“无证书先不签，有了在 build 与 manifest 之间插 signtool”。本文把这个方案细化到具体文件和步骤。
> 铁律：**证书、私钥、PFX、令牌 PIN、云签名凭据都不能进 Git**，也不能写进任何脚本或 manifest（交接文档 §2 第 4 条）。

---

## 1. 现有构建与发布流水线（签名前）

| 步 | 脚本 | 做什么 | 产物 |
|---|---|---|---|
| 1 | `fleet_agent/build_agent.py` | PyInstaller `--onefile` 打包 `fleet_agent/entry.py` → 运行 `--help` 冒烟 → **马上算 sha256** → 复制 `Install-/Uninstall-ChatXAgent.ps1` → 写 `manifest.json`（`version/file/url/sha256/size/...`） | `fleet_agent/dist/chatx-agent.exe`、`.sha256`、`manifest.json` |
| 2 | `fleet_agent/build_setup.ps1` | 用 ISCC 编译 `fleet_agent/setup/ChatXAgent.iss`，把 **`dist/chatx-agent.exe` 原样打进安装包** → **马上算** `ChatXAgentSetup.exe` 的 sha256 → 往 manifest 里补 `setup_file/setup_url/setup_sha256` | `dist/ChatXAgentSetup.exe`、`.sha256`，更新后的 `manifest.json` |
| 3 | `deploy/fleet/publish_agent.ps1` | **只要找到 ISCC.exe（或传了 `-BuildSetup`）就会自动再跑一次第 2 步**，然后 scp 上传 `dist` 里的文件，另存 `chatx-agent-<ver>.exe` | 公网 `/downloads/fleet/` |

sha256 在哪些地方被使用（签名以后必须全部对得上）：

- `manifest.sha256`：远程升级时 Agent 校验下载的 exe（`src/fleet/updater.py`：sha256 必填，不一致就拒绝）；`Install-ChatXAgent.ps1` 自动读取 manifest 做校验。
- `manifest.setup_sha256`：机房安装包 `Install.cmd` 用它校验下载到的 `ChatXAgentSetup.exe`（`src/fleet/roompack.py`；为空直接退出 `setup sha256 is not pinned`）。主控经 `resolve_download` 读 manifest（缓存 60 s，`docs/FLEET_DEPLOY.md` §2）。
- `/fleet/` 下载页展示 `setup_sha256`（`templates/fleet_home.html`）。

**结论：签名必须在“算哈希”之前完成。签名会改变文件字节，签完再算哈希，manifest 才能覆盖到签过名的文件。**

---

## 2. 签名插入点（按顺序）

```
PyInstaller → [S1 签 chatx-agent.exe] → 算 sha256 / 写 manifest
            → ISCC 编译（打进去的是已签名的 chatx-agent.exe）
              → [S2 签 ChatXAgentSetup.exe + 卸载程序] → 算 setup_sha256 / 更新 manifest
            → [S3 发布前校验：签名有效 + 哈希一致] → 上传
```

### S1：`chatx-agent.exe`

- 时机：`build_agent.py` 里 PyInstaller 和 `--help` 冒烟之后、`sha256(exe)` 之前。
- **不改代码就能用的过渡办法**：现有的 `--skip-build` 参数“只重算 sha256 / manifest（已有 exe）”。

  ```powershell
  python fleet_agent\build_agent.py --clean --base-url https://bd2026.cc/downloads/fleet/
  signtool sign ... fleet_agent\dist\chatx-agent.exe          # 手工签
  python fleet_agent\build_agent.py --skip-build --base-url https://bd2026.cc/downloads/fleet/   # 重算 sha256 + manifest
  powershell -File fleet_agent\build_setup.ps1                  # 再编安装包（这时打进去的是签过名的 exe）
  ```

  注意：`--skip-build` 会**重写整份** manifest.json，之前 `build_setup.ps1` 补进去的 `setup_*` 字段会丢失，所以必须**先重算 manifest，再编安装包**，顺序不能反。
- 为什么 S1 必须早于 ISCC：安装包里装的就是 `dist/chatx-agent.exe`；远程 `upgrade` 下载的也是它。只签安装包、不签内层 exe，装好以后的 `%ProgramFiles%\ChatX Agent\chatx-agent.exe` 仍然是未签名的。

### S2：`ChatXAgentSetup.exe`（和 Inno 卸载程序）

- 推荐做法：用 Inno Setup 自带的 **`SignTool` 机制**。在 `[Setup]` 里加 `SignTool=<名字>`，编译时 ISCC 会签安装包本体，也会签 Inno 生成的卸载程序（`unins000.exe`）；签名命令用 ISCC 的 `/S<名字>=<命令>` 参数传入，证书信息不写进 `.iss`。【待确认：以 Inno Setup 6.3.x 文档的具体语法为准，并在构建机上实测】
- 不用 Inno SignTool 的替代做法：ISCC 编译完之后再签 `ChatXAgentSetup.exe`。但 `build_setup.ps1` 在 ISCC 结束后**马上**算 setup sha256 并写进 manifest，所以要么改脚本（在 ISCC 和算哈希之间插入签名），要么签完以后重算 `setup_sha256`。另外，这种做法**签不到卸载程序**。
- `bootstrap.ps1`、`Open-Panel.vbs`、`Open-Status.cmd` 会被打进安装包：
  - `bootstrap.ps1` 由安装器以 `-ExecutionPolicy Bypass` 调用，签不签都不影响执行，签名只是可选加固；签了以后文件不能再改动。
  - `.vbs` 支持 Authenticode（可选）；`.cmd` 不支持签名。

### S3：发布前校验

- `publish_agent.ps1` 上传之前，校验 `chatx-agent.exe` 和 `ChatXAgentSetup.exe` 的签名都有效，并且 manifest 里的 `sha256` / `setup_sha256` 等于当前文件的哈希。
- **风险点**：`publish_agent.ps1` 在找得到 ISCC 时会**无条件重编安装包**。手工签过的 `ChatXAgentSetup.exe` 会被一个没签名的新包覆盖。接入签名之前，要么手工流程里不在装有 ISCC 的机器上跑 publish，要么改脚本（见 §6 清单）。

### 不在签名范围内 / 不用签

- 机房安装包里的 `Install.cmd` / `Install-Silent.cmd` 是主控**按请求动态生成**的（`roompack.py`），不能也不需要签；它的安全靠 https + 钉住的 `setup_sha256`。
- `Install-ChatXAgent.ps1` / `Uninstall-ChatXAgent.ps1`（下载页的「高级」链接）：可以签，但 `build_agent.py` 会把它们原样复制到 dist，签名要在复制之后做，或者直接签源文件。签源文件的话，行尾或编码一变签名就失效。【待确认：要不要签这两个脚本，建议第一期不签】

---

## 3. 证书选项

| 选项 | 说明 | 适用 |
|---|---|---|
| OV 代码签名证书 | 验证组织身份。按 CA/B 论坛基线要求（2023-06 起），**私钥必须存放在硬件里**（USB 令牌 / HSM / 云 HSM），**不再签发可以导出的 .pfx** | 预算有限、签名频率低 |
| EV 代码签名证书 | 组织验证更严格，同样是硬件或云 HSM。**微软已经调整 SmartScreen 策略，EV 不再自动获得“立即信任”**，信誉仍然要靠积累【待确认：以采购时微软的最新说明为准】 | 需要内核驱动签名等场景；本项目不需要 |
| 云签名服务（如 Azure Trusted Signing / Artifact Signing、DigiCert KeyLocker、SSL.com eSigner 等） | 私钥托管在云 HSM，signtool 通过插件 / dlib 调用；方便在构建机上自动化 | 【待确认：各家服务对中国大陆主体 / 香港主体的开放情况，Azure Trusted Signing 有地域和主体限制】 |

需要老板 / Morgan 决定的事：

1. **用哪个主体申请**（公司名会显示在 UAC 和 SmartScreen 里）。【待确认】
2. **OV、EV 还是云签名**，以及预算。【待确认】
3. **谁保管**：USB 令牌放在谁手里、PIN 由谁掌握、云签名账号的最小权限角色给谁。【待确认】
4. **在哪台构建机上签**：需要固定一台打包机（装 Windows SDK 的 signtool + Inno Setup 6）。【待确认】

保管原则（不论选哪种证书）：

- 证书和私钥**永远不进 Git**，不进 `fleet_agent/dist` / `build`（这两个目录已 gitignore，但也不要放），不写进脚本、manifest 和环境变量文件。
- 脚本只引用证书**指纹**（证书存储里的 SHA1 thumbprint），或者云签名的**元数据文件路径**；PIN 和密码交互输入，或者由令牌驱动管理。
- 签名日志里不打印 PIN 和凭据。

---

## 4. 建议的开关（默认关闭，不影响现有流程）

描述接口，**暂不实现**：

| 名称 | 类型 | 作用 |
|---|---|---|
| `CHATX_SIGN` | 环境变量，`1` 开启 | 没有设置 = 保持现在的行为（不签） |
| `CHATX_SIGNTOOL` | 环境变量 | `signtool.exe` 的完整路径；不设就按 Windows SDK 常见路径查找 |
| `CHATX_SIGN_CERT_SHA1` | 环境变量 | 证书存储里的证书指纹（令牌证书一般也能在存储里看到）→ `signtool sign /sha1 <指纹>` |
| `CHATX_SIGN_DLIB` / `CHATX_SIGN_DMDF` | 环境变量 | 云签名用：`/dlib <dll> /dmdf <metadata.json>`（元数据文件放在仓库外面） |
| `CHATX_SIGN_TIMESTAMP_URL` | 环境变量 | 时间戳服务器，默认使用所购 CA 的 RFC 3161 地址 |
| `build_agent.py --sign` | 参数 | 在 S1 位置调用签名，签完再算 sha256 |
| `build_setup.ps1 -Sign` | 参数 | 通过 ISCC `/S` 启用 `.iss` 里的 `SignTool`（S2） |
| `publish_agent.ps1 -RequireSigned` | 参数 | 上传前做 S3 校验，没签名或哈希不一致就拒绝上传；同时**不再自动重编安装包**，或者重编时也签名 |

签名命令样例（参数以实际证书和 CA 为准）：

```powershell
signtool sign /fd sha256 /sha1 <CERT_SHA1> /tr <RFC3161_TIMESTAMP_URL> /td sha256 /d "ChatX Fleet Agent" fleet_agent\dist\chatx-agent.exe
```

---

## 5. 时间戳与校验

- **一定要加时间戳**（`/tr <RFC3161 URL> /td sha256`）。不加的话，证书一过期签名就失效；加了以后，签名在证书过期后仍然有效。时间戳服务器的地址用 CA 提供的那个。
- 摘要算法用 `sha256`（`/fd sha256`）。

校验命令：

```powershell
# 1) signtool（Windows SDK）
signtool verify /pa /v fleet_agent\dist\chatx-agent.exe
signtool verify /pa /v fleet_agent\dist\ChatXAgentSetup.exe

# 2) PowerShell 自带
Get-AuthenticodeSignature fleet_agent\dist\chatx-agent.exe | Format-List Status,StatusMessage,SignerCertificate,TimeStamperCertificate
Get-AuthenticodeSignature fleet_agent\dist\ChatXAgentSetup.exe | Format-List Status,SignerCertificate,TimeStamperCertificate
# 期望 Status = Valid，TimeStamperCertificate 不为空

# 3) 哈希与 manifest 一致（签完之后）
$m = Get-Content fleet_agent\dist\manifest.json -Raw | ConvertFrom-Json
(Get-FileHash fleet_agent\dist\chatx-agent.exe -Algorithm SHA256).Hash.ToLower() -eq $m.sha256
(Get-FileHash fleet_agent\dist\ChatXAgentSetup.exe -Algorithm SHA256).Hash.ToLower() -eq $m.setup_sha256

# 4) 真机装完后，确认落盘的 exe 也是签过名的（说明 S1 早于 ISCC）
Get-AuthenticodeSignature "$env:ProgramFiles\ChatX Agent\chatx-agent.exe" | Select Status
```

---

## 6. 以后需要改的脚本（清单，只描述，不实现）

| # | 文件 | 改动 | 归属 |
|---|---|---|---|
| 1 | `fleet_agent/build_agent.py` | 加 `--sign`（或读 `CHATX_SIGN`）：冒烟之后、`sha256(exe)` 之前调用 signtool；签名失败返回非 0；打印签名者信息，但不打印凭据 | Devin 的文件，需要先在 PR 里评论 |
| 2 | `fleet_agent/setup/ChatXAgent.iss` | `[Setup]` 里加条件化的 `SignTool=`（用 `#ifdef` 控制，默认不启用），让安装包和卸载程序一起签名 | Devin 的文件 |
| 3 | `fleet_agent/build_setup.ps1` | 加 `-Sign`：把 `/S<name>=...` 传给 ISCC；编译前校验 `dist/chatx-agent.exe` 已签名（开启签名时，没签就拒绝编译）；setup 的 sha256 在签名之后再算（用 Inno SignTool 时天然满足） | Devin 的文件 |
| 4 | `deploy/fleet/publish_agent.ps1` | 加 `-RequireSigned`：上传前做 S3 校验；**找到 ISCC 时不要悄悄重编一个未签名的安装包**（开启签名时要么跳过重编，要么重编时带签名） | Devin 的文件 |
| 5 | `src/fleet/updater.py`（可选，P2） | `upgrade` 除了 sha256，再校验 Authenticode（签名者指纹白名单）。协议 payload 不用改；指纹白名单放在 Agent 内置常量或本机配置里 | Devin 的文件；属于行为变更，要双方同意 |
| 6 | `fleet_agent/Install-ChatXAgent.ps1`（可选） | 下载后除了 sha256，再做 `Get-AuthenticodeSignature` 检查（开启时） | Devin 的文件 |
| 7 | `docs/FLEET_DEPLOY.md` §2 | 把“代码签名”那一段更新为最终流程 | 文档 |
| 8 | 构建机准备 | 固定一台打包机，装 Windows SDK（signtool）、Inno Setup 6、令牌驱动或云签名插件；记录在运维手册里（不记录凭据） | 运维 |

---

## 7. 未知项 / 待确认

1. 证书主体、类型（OV / EV / 云）、预算、采购周期。
2. 保管人和使用审批流程（谁能按下“签名”）。
3. 固定的构建机是哪台（本文**不指定**）。
4. Inno Setup 6.3.x 下 `SignTool` 指令和 `/S` 参数的具体写法（在构建机上实测）。
5. 首批签名后 SmartScreen 信誉的积累情况：签名以后仍可能提示一段时间，要观察。
6. 要不要在 `upgrade` 链路里加签名者校验（P2，要和 Devin 商定）。
