# 智控多机部署手册（主控落地 + 节点 Agent 安装）

配套契约：`docs/FLEET_CONTROL_CONTRACT.md`（协议 / 任务 kind / 铁律）。本文只讲"怎么装、怎么发、怎么升"。
与 grok bot 的对接事项见 `docs/FLEET_GROK_HANDOFF.md`。

```
机房电脑（Windows 10/11/Server）                    VPS bd2026.cc
┌──────────────────────────────┐   HTTPS 出站     ┌──────────────────────────────────┐
│ chatx-agent.exe（计划任务 SYSTEM）├───────────────▶│ nginx  /fleet/api/ → 127.0.0.1:18798/api/ │
│   %ProgramData%\ChatX\fleet   │  长轮询 ≤25s     │        /fleet/     → 127.0.0.1:18798/fleet/│
│   ↓ 本机 HTTP                 │                  │        /downloads/fleet/  静态发布物        │
│ 智聊实例 127.0.0.1:18797       │                  │ systemd chatx-fleet：main.py --config      │
└──────────────────────────────┘                  │   /etc/chatx-fleet/config.yaml            │
                                                  │ SQLite /var/lib/chatx-fleet/fleet.db      │
操作端（你的电脑 / 任何浏览器）                        └──────────────────────────────────┘
  https://bd2026.cc/fleet/console   或   python -m src.fleet.admin ...
```

## 0. 一览：文件都在哪

| 用途 | 路径 |
|---|---|
| 节点 Agent 核心（CLI：enroll / add-instance / run / install-service / uninstall-service / service-status / status） | `src/fleet/agent.py` |
| 服务化（schtasks / systemd 命令生成 + 监督循环） | `src/fleet/service.py` |
| 自升级（校验下载 + 换文件脚本） | `src/fleet/updater.py` |
| 操作端 CLI（new-code / nodes / task / tasks / upgrade） | `src/fleet/admin.py` |
| 打包（PyInstaller 单文件 + manifest.json） | `fleet_agent/build_agent.py`、`fleet_agent/entry.py` |
| Windows 一键安装 / 卸载 | `fleet_agent/Install-ChatXAgent.ps1`、`fleet_agent/Uninstall-ChatXAgent.ps1` |
| 主控 systemd 单元 | `deploy/fleet/chatx-fleet.service` |
| 主控 nginx 片段（主站前缀 A / 子域 B 两种） | `deploy/fleet/nginx-fleet.conf` |
| 主控安装 / 升级 / 体检 / 回滚（VPS 上跑） | `deploy/fleet/deploy_controller.sh` |
| 发布物上传 + 主控源码打包上传（开发机跑） | `deploy/fleet/publish_agent.ps1` |
| 主控预设 | `config/presets/fleet_control.yaml` |
| 测试 | `tests/test_fleet_control.py`（P0，27 例）、`tests/test_fleet_p1_service_updater.py`（P1，24 例） |

## 1. 主控落地到 bd2026.cc（改生产，需授权后执行）

前提：VPS 已有官网 nginx（`bd2026.cc` server 块）与 Python ≥ 3.11；主控与官网互不干扰（独立用户 `chatx-fleet`、目录 `/opt/chatx-fleet`、只监听回环 18798）。

```powershell
# 开发机：打包 engines/chengjie 源码（排除 tests/desktop/.venv/config 等）并上传 deploy 三件套到 ~/
powershell -File deploy\fleet\publish_agent.ps1 -PackController          # 不加 -Deploy：只上传，不改生产
```

```bash
# VPS：
sudo bash ~/deploy_controller.sh ~/chatx-fleet-src.tar.gz
#   1 建用户/目录  2 解包到 app.new  3 venv+pip(requirements-ci.txt)  4 首次生成 /etc/chatx-fleet/config.yaml
#     → 随机 auth_token / secret_key **只打印一次**，存密码库（操作端 CHATX_FLEET_ADMIN_TOKEN 就是它）
#   5 原子切换 app（旧版留 app.prev）  6 systemd enable+restart  7 写 /etc/nginx/snippets/chatx-fleet-main.conf
#   8 健康检查失败自动回滚
# 然后人工在 bd2026.cc 的 server{} 里加一行（脚本只提示、不改官网配置）：
#     include /etc/nginx/snippets/chatx-fleet-main.conf;
sudo nginx -t && sudo systemctl reload nginx
sudo bash ~/deploy_controller.sh --check        # 服务 / 本地 /fleet/ / 公网 /fleet/ / enroll 401 探针 / CHANGE_ME 残留
sudo bash ~/deploy_controller.sh --rollback     # 需要时：app ↔ app.prev
```

升级主控 = 重跑同一条命令（config 不覆盖、db 不动）。日志：`journalctl -u chatx-fleet -f`。

**为什么是路径前缀而不是子域**：Agent 只需要 `/fleet/api/*`，主站前缀零 DNS/证书改动；控制台 `/fleet/console` 也在前缀下可用。
若以后要把整套 web_admin（登录页 / 静态资源等非 `/fleet/` 路径）暴露出来，用 `nginx-fleet.conf` 的 B 段起 `fleet.bd2026.cc`，需要另加 DNS + 证书。

## 2. 发布节点 Agent

```powershell
cd engines\chengjie
pip install pyinstaller                                    # 打包机一次
python fleet_agent\build_agent.py --clean --base-url https://bd2026.cc/downloads/fleet/
#  → fleet_agent\dist\chatx-agent.exe (~13 MB) / chatx-agent.exe.sha256 / manifest.json / Install-*.ps1 / Uninstall-*.ps1
#    构建会跑一次 --help 冒烟；manifest.version 取自 src/fleet/agent.py 的 AGENT_VERSION
powershell -File deploy\fleet\publish_agent.ps1            # scp 到官网 public/downloads/fleet/，另存 chatx-agent-<ver>.exe 供回滚
```

主控 `/fleet/` 下载页与 `/api/fleet/overview` 的 `download` 段读 `fleet_control.download.manifest_url`（默认指向上面的 manifest.json，60 s 缓存），
所以**发布后不需要改主控配置**；想钉死版本就在 config 里显式填 `download.version / installer_url / sha256`（显式值覆盖 manifest）。

代码签名：exe 未签名，首次运行 SmartScreen 会拦一次（"仍要运行"）。有证书后在 `build_agent.py` 之后加 `signtool sign ...` 即可，manifest 的 sha256 要在签名后再算——把签名步骤插到 build 与 manifest 之间（`--skip-build` 可只重算 manifest）。

## 3. 机房电脑接入（每台 2 分钟）

1. 操作端发注册码（任一）：
   - 浏览器 `https://bd2026.cc/fleet/console` → 生成注册码；
   - CLI：`set CHATX_FLEET_CONTROLLER=https://bd2026.cc/fleet` + `set CHATX_FLEET_ADMIN_TOKEN=...` 后
     `python -m src.fleet.admin new-code --label 机房A-01 --group 机房A --ttl-min 60`
2. 机房电脑，管理员 PowerShell：
   ```powershell
   powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Controller https://bd2026.cc/fleet -Code 12345678
   ```
   安装器做的事：下载 `chatx-agent.exe`（或 `-Exe` 本地包）→ 按 manifest / `-Sha256` 校验 → 放 `%ProgramFiles%\ChatX Agent\` →
   建 `%ProgramData%\ChatX\fleet` → 自动找本机智聊 `config.yaml` 登记实例（`-ConfigPath` 指定 / `-NoInstance` 跳过）→ `enroll` →
   `install-service`（计划任务 `ChatX Fleet Agent`，ONSTART / SYSTEM / 最高权限，立即启动）→ 打印 `service-status`。
3. 主控后台几秒内节点上线。重装系统：新注册码重跑安装器即可，machine_id 不变、节点身份不变。

排障：`"%ProgramFiles%\ChatX Agent\chatx-agent.exe" --state-dir "%ProgramData%\ChatX\fleet" status`；
日志 `%ProgramData%\ChatX\fleet\logs\agent.log`；前台调试 `... run -v`（Ctrl-C 退出，不影响计划任务）；
`schtasks /Query /TN "ChatX Fleet Agent" /V`。卸载：`Uninstall-ChatXAgent.ps1 [-PurgeState]`（默认保留状态目录，保节点身份）。

## 4. 日常操作（操作端 CLI）

```powershell
python -m src.fleet.admin nodes                            # 在线 / 离线 / 版本 / 实例数
python -m src.fleet.admin task <node_id> ping
python -m src.fleet.admin task <node_id> account_health
python -m src.fleet.admin task <node_id> stop_account --target '{"instance":"main","phone":"+8613800000000"}'
python -m src.fleet.admin tasks --node <node_id> --status done
python -m src.fleet.admin upgrade --manifest https://bd2026.cc/downloads/fleet/manifest.json --group 机房A --yes
```

`upgrade` 只会下发给 `agent_version != manifest.version` 且在线的节点；Agent 校验 sha256 → 换文件 → 自动重启，几十秒后 `nodes` 里版本变新。
回滚 = 用旧版 manifest（`chatx-agent-<旧版>.exe` 仍在 downloads 目录，手写一个 manifest.json 指向它）再下发一次 upgrade。

## 5. 安全与边界（勿破）

- 安装包 / 安装器不含主控管理凭据；只有一次性注册码。`web_admin.auth_token` 只在 VPS `/etc/chatx-fleet/config.yaml`（640）与操作端环境变量。
- 节点 node_key 只在 `%ProgramData%\ChatX\fleet\agent.json`（SYSTEM 与管理员可读）；主控只存哈希。
- 心跳 / 回执无聊天原文（协议层 allowlist）。
- `push_config` 仍拒绝；`restart_instance` 只在本机显式配置 `restart_cmd` 时执行。
- 本仓库不放任何生产配置 / 证书 / token；`deploy_controller.sh` 生成的 token 只在 VPS 上。

## 6. 已验证 / 未验证

已验证（开发机 Windows + 隔离 worktree）：51 例 fleet 测试通过（P0 27 + P1 24）；`pyflakes`、`compileall`、PowerShell 三份脚本语法解析、`bash -n`、preset YAML、
`main.py --init fleet_control --config <tmp> --set ...` + `--check` 通过（deploy 脚本同款命令）；PyInstaller 实际打出 `chatx-agent.exe`（13.0 MB），
冻结版 `--help / status / service-status` 正常，sha256 与 manifest 一致；安装器非管理员运行时干净拒绝。

未验证（需要真机 / 生产授权）：管理员真实执行安装器 + 计划任务开机自启 + 一次真实 `upgrade` 换文件；VPS 上跑 `deploy_controller.sh`；nginx include 后公网探针。
建议：先用一台闲置 Windows 机做首台试装（`-Controller http://<开发机IP>:18798`），跑通 enroll → 心跳 → ping → upgrade 再上机房。
