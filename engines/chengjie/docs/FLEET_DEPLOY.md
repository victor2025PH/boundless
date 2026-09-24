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
| Windows 双击安装包（Inno Setup 6，免费） | `fleet_agent/setup/ChatXAgent.iss`、`fleet_agent/build_setup.ps1` → `ChatXAgentSetup.exe` |
| Windows 命令行安装 / 卸载（高级） | `fleet_agent/Install-ChatXAgent.ps1`、`fleet_agent/Uninstall-ChatXAgent.ps1` |
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

## 3. 操作员：一台电脑 / 一整间机房

一台电脑（不用输任何码）：

1. 打开 `https://bd2026.cc/fleet/`，下载 **ChatXAgentSetup.exe**，双击，权限确认里点「是」。
2. 打开主控后台 `https://bd2026.cc/fleet/console`，这台电脑在「待批准」里。安装结束画面上的配对码和控制台同一列对得上，点批准。分组用后台填的；不填就进 `pending-default`。这台电脑如果已经有节点，控制台会先问一句 `approving will rotate key of n_xxx`。
3. 开始菜单「Fleet node status」能看到本机已接入。没装智聊、也没装幻颜，也一样能批，只是这台电脑没有实例。

一整间机房（不用逐台批准）：

1. 后台点「机房安装包」（或 `python -m src.fleet.admin room-key --group 机房A --max-uses 40 --ttl-hours 168`）。
2. 链接只显示这一次。每台电脑打开它，解压后双击 `Install.cmd`（静默批量用 `Install-Silent.cmd`）。装上的电脑直接进这个分组，直到次数用完或到期。
3. 装完这批，在后台吊销这张密钥。链接不是公开下载，不要发到下载页。

安装包会自己找本机智聊（`config.local.yaml` / `127.0.0.1:18799`）和幻颜 AvatarHub（`127.0.0.1:9000`，只做健康检查，不能远程开关）。什么都没有就当纯心跳节点，安装不会失败。

命令行仍可用，但下载页上只是「高级」链接，浏览器会下载脚本而不是打开源码：

```powershell
powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Controller https://bd2026.cc/fleet
powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Controller https://bd2026.cc/fleet -Code 12345678
```

无 `-Code` 时和双击安装包一样，进待批准。一次性注册码是 12 位 Crockford（展示成 `XXXX-XXXX-XXXX`，不区分大小写），代码里的默认有效期是 15 分钟，同一 IP 10 分钟内猜错 8 次进入锁定期（锁定期内连对的码也不消耗）。生产已经在跑：配置文件里如果写了 `enroll_code_ttl_min`，那个值盖过代码默认，所以上线前必须在 `/etc/chatx-fleet/config.yaml` 里改成 15。已经发出、还没到期的 8 位数字码在到期前仍能兑，不是「没有在途 8 位码」。机房批量仍用 `rk_` 机房密钥，签发时分组必填；已有节点如果分组是空的，机房密钥不会自动换 key，改入待批准。已吊销的电脑即使拿对注册码或机房密钥也不会自动复活，控制台和 `admin pending` 会标 `this machine was revoked`，要管理员再点批准（已有节点还要确认换 key）。同一 `machine_id` 有多条待批准时，控制台和 `admin pending` 会标 `duplicate machine_id`。

静默安装（已经有安装包文件时）：`ChatXAgentSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`。机房包里的 `Install-Silent.cmd` 会带上 `/ROOMKEYFILE`。

重装系统：再跑一次安装包即可。machine_id 不变，但批准时必须在控制台或 CLI 显式确认（`approving will rotate key of n_xxx` / `approve --confirm-rotate`），确认后才换新 key。静默重装会先停掉计划任务 `ChatX Fleet Agent` 再覆盖 exe。卸载默认留下 `%ProgramData%\ChatX\fleet`；安装包卸载参数 `/REMOVESTATE=1` 才删它。

排障：`"%ProgramFiles%\ChatX Agent\chatx-agent.exe" --state-dir "%ProgramData%\ChatX\fleet" status`；
日志 `%ProgramData%\ChatX\fleet\logs\agent.log`；前台调试 `... run -v`（Ctrl-C 退出，不影响计划任务）；
`schtasks /Query /TN "ChatX Fleet Agent" /V`。卸载：`Uninstall-ChatXAgent.ps1 [-PurgeState]`（默认保留状态目录，保节点身份）。

## 3.0 上线前核对（生产已经在跑）

这次不要手改库，但下面几项要在 `deploy_controller.sh` 之前看完。`deploy_controller.sh --rollback` 只把 `app` 和 `app.prev` 对调再重启服务，**不回滚** systemd unit 文件。unit 改坏了要手工把 `/etc/systemd/system/chatx-fleet.service` 换回去再 `daemon-reload`。

1. `/etc/chatx-fleet/config.yaml` 里写上 `enroll_code_ttl_min: 15`。预设和代码默认已经是 15，但文件里已有的值（例如 60）优先，不改的话新码仍按旧 TTL 发。
2. `ProtectSystem=strict` 下进程只能写 `ReadWritePaths`。确认 `fleet_control.db_path` 是 `/var/lib/chatx-fleet/fleet.db`（空值会落到配置目录，strict 下写不进去）。`ReadWritePaths` 只有 `/var/lib/chatx-fleet`、`/opt/chatx-fleet/app/logs`、`/opt/chatx-fleet/app/config`。运行时不要写到程序目录的其他位置，也不要写 `/etc/chatx-fleet`。配置目录保持 750、只读。
3. 生产 nginx 和 fail2ban **已经手工装过**，不在这次 deploy 里重装，也不要用仓库里的示例覆盖它们：
   - `limit_req` 6r/m、burst 5，挂在 `location = /fleet/api/fleet/enroll`
   - 文件：`/etc/nginx/snippets/chatx-fleet-enroll-ratelimit.conf`、`/etc/nginx/conf.d/fleet-ratelimit.conf`
   - fail2ban jail `chatx-fleet-enroll`：`/etc/fail2ban/filter.d/chatx-fleet-enroll.conf`、`/etc/fail2ban/jail.d/chatx-fleet-enroll.conf`
   - 仓库 `deploy/fleet/nginx-fleet-enroll-limit.conf` 和 `deploy/fleet/fail2ban/` 只是对照用的例子（示例 enroll 是 10r/m burst 4，比现网松）。`deploy_controller.sh` 不会安装它们。
   - 机房批量安装必须压在 **每个出口 IP 每分钟 6 次以内**，否则现网 `limit_req` 直接 429，这间机房后面的已装节点也会受影响。
4. fail2ban 只计 `reason=bad_code`、`reason=bad_room_key` 和注册码 / 机房密钥的 `reason=lockout`。不计 `exhausted`、`expired`、`revoked`，也不计待批准队列的限速。一间机房共用一个 NAT、反复兑一张过期密钥时，不应把 80/443 封掉。

## 3.1 发到服务器时运维要做的

代码合进去之后**不要**手改库。新表（待批准、机房密钥、失败计数）在主控进程启动时自己建好，旧节点和已发出的 node_key 不用迁移。

1. 构建机先有免费的 Inno Setup 6（`winget install --id JRSoftware.InnoSetup -e`），再跑 `python fleet_agent\build_agent.py` 和 `powershell -File deploy\fleet\publish_agent.ps1`。脚本在找得到 `ISCC.exe` 时会打出 `ChatXAgentSetup.exe` 并一起上传。下载目录里要同时有 `chatx-agent.exe`、`ChatXAgentSetup.exe`、`manifest.json`（manifest 里带 `setup_url` / `setup_sha256`）。
2. `publish_agent.ps1 -PackController` 打主控源码包时会带上 `config/presets`（其余 `config/` 仍排除）。VPS 上 `sudo bash deploy_controller.sh ~/chatx-fleet-src.tar.gz`。`/etc/chatx-fleet` 是 `750`（root:chatx-fleet，配置只读）；可写的是 `/var/lib/chatx-fleet`（`770`，库文件）。systemd 开了 `ProtectSystem=strict`，`ReadWritePaths` 只含数据目录和日志，不含配置目录。`config.yaml` 仍是 `640`。`--check` 只打 `GET /api/fleet/overview`（期望 401），不会往注册接口塞假码。
3. 把更新后的 `deploy/fleet/nginx-fleet.conf` 再装一次（`deploy_controller.sh` 会重写 snippet）。新增的是 `/fleet/dl/`：反代到主控，并且 **access_log off**，避免机房链接里的密钥进 nginx 日志。没有新的监听端口。装完 `nginx -t && systemctl reload nginx`，然后 `systemctl restart chatx-fleet`。
4. 主控进程给 uvicorn / httpx 访问日志加了过滤器，把 `/fleet/dl/<token>` 收成 `/fleet/dl/<redacted>`。应用自己的日志只记 key id。注册失败另打一行 `fleet enroll_fail ip=1.2.3.4 reason=bad_code`（注册码或机房密钥锁定期是 `reason=lockout`，猜错机房密钥是 `reason=bad_room_key`）。对照示例在 `deploy/fleet/fail2ban/` 和 `deploy/fleet/nginx-fleet-enroll-limit.conf`。现网的 limit 和 jail 已经装在第 3.0 节列出的路径里，这次不要覆盖。

## 4. 日常操作（操作端 CLI）

```powershell
python -m src.fleet.admin pending                         # 待批准：配对码 / 申请分组 / 生效分组 / IP
python -m src.fleet.admin approve <request_id> --group 机房A
python -m src.fleet.admin approve <request_id> --confirm-rotate   # 已有节点时才会换 key
python -m src.fleet.admin reject <request_id>
python -m src.fleet.admin room-key --group 机房A --max-uses 40 --ttl-hours 168
python -m src.fleet.admin revoke-room <key_id>
python -m src.fleet.admin nodes                            # 在线 / 离线 / 版本 / 实例数（先列出待批准）
python -m src.fleet.admin task <node_id> ping
python -m src.fleet.admin task <node_id> account_health
python -m src.fleet.admin task <node_id> stop_account --target '{"instance":"main","phone":"+8613800000000"}'
python -m src.fleet.admin tasks --node <node_id> --status done
python -m src.fleet.admin upgrade --manifest https://bd2026.cc/downloads/fleet/manifest.json --group 机房A --yes
```

`upgrade` 只会下发给 `agent_version != manifest.version` 且在线的节点；Agent 校验 sha256 → 换文件 → 自动重启，几十秒后 `nodes` 里版本变新。
回滚 = 用旧版 manifest（`chatx-agent-<旧版>.exe` 仍在 downloads 目录，手写一个 manifest.json 指向它）再下发一次 upgrade。

## 5. 安全与边界（勿破）

- 公开安装包不含主控管理凭据，也不含机房密钥。没被批准的节点没有 node_key，不能领任务。`web_admin.auth_token` 只在 VPS `/etc/chatx-fleet/config.yaml`（640）与操作端环境变量。机房链接只在签发时出现一次。
- 节点 node_key 只在 `%ProgramData%\ChatX\fleet\agent.json`。父目录 `%ProgramData%\ChatX` 的所有者必须是 SYSTEM（S-1-5-18）或 Administrators（S-1-5-32-544），并且父目录和 `fleet` 都不能是 junction / 重解析点。锁 `fleet` 时先只对目录 `/setowner`，再对目录本身 `/reset`（不带 `/T`，清掉 Everyone 这类显式 ACE），然后 `/inheritance:r /grant:r` 写成只含 SYSTEM 和 Administrators 的受保护 DACL，并用同一套「已锁定」判断做后置校验。通过之后才看子文件：所有者是 SYSTEM 或 Administrators 的 `agent.json` / `machine_id` / `room.key` 保留（所以老安装里已经是这两个所有者的 node_key 升级后还在）；其他所有者删掉，这台机器需要重新批准。删除失败就中止，绝不继续 `/setowner /T`。最后 `/setowner /T`、对子项 `/reset /T`、再 `/grant /T`，然后再校验一次。读取时目录没锁，或者文件所有者不可信，都不采用这份文件。`/grant:r` 单独用不会清掉其他 SID 的显式 ACE。`restart_cmd` 由 SYSTEM 用 `shell=True` 执行，所以不能信一份来历不明的 `agent.json`。主控只存哈希。待批准申请另有一把安装时生成的 `enroll_secret`，主控只存它的 sha256；轮询必须带上它。配对码可以给人看，不是凭证。
- 心跳 / 回执无聊天原文（协议层 allowlist）。
- `push_config` 仍拒绝；`restart_instance` 只在本机显式配置 `restart_cmd` 时执行。
- 本仓库不放任何生产配置 / 证书 / token；`deploy_controller.sh` 生成的 token 只在 VPS 上。

## 6. 已验证 / 未验证

已验证（开发机 Windows + 隔离 worktree，Agent 0.2.x 当时）：51 例 fleet 测试通过（P0 27 + P1 24）；`pyflakes`、`compileall`、PowerShell 三份脚本语法解析、`bash -n`、preset YAML、
`main.py --init fleet_control --config <tmp> --set ...` + `--check` 通过（deploy 脚本同款命令）；PyInstaller 实际打出 `chatx-agent.exe`（13.0 MB），
冻结版 `--help / status / service-status` 正常，sha256 与 manifest 一致；安装器非管理员运行时干净拒绝。

已在 Windows Server 2022 真机（管理员）跑通全链路：本机主控 + 静态下载目录 → `new-code` → 安装器下载 exe / 读 manifest / sha256 → enroll →
计划任务 `ChatX Fleet Agent` 以 SYSTEM 运行 → 节点 online → `ping` / `account_health` ack done → `admin upgrade --manifest` 一次真实换文件
（0.2.1 → 0.2.2：下载校验 → 独立一次性计划任务跑 swap → 旧 exe 留 `.bak` → 任务重新拉起 → 心跳上报新版本）→ 卸载器默认保留 `%ProgramData%\ChatX\fleet`，
重装同机复用 node_id。真机踩到并修掉的坑：PS 5.1 下原生命令 stderr 在 `$ErrorActionPreference=Stop` 会变终止错误（安装器统一 `Native` 包装）；
cp1252 控制台打中文帮助崩（entry 强制 UTF-8）；Task Scheduler 会连带杀掉 agent 派生的 swap 子进程（改为注册 `ChatX Fleet Agent Upgrade` 一次性任务）；
`admin` 过滤在线节点应看 `state` 而非 `status`。

本次（安装包 / 待批准 / 机房密钥，以及随后的 enroll_secret、确认换钥、目录 ACL）在仓库里用 `tests/test_fleet_enroll_v2.py` 加上原有的两份 fleet 测试一起跑。`ChatXAgentSetup.exe` 要在 Windows 构建机上装好免费的 Inno Setup 6 再编，这个环境不产生那个 exe。同一出口 IP 一小时内的待批准上限是 240（一整间机房共用一个公网 IP）；单机仍是 3 次。

未验证（需要生产授权）：VPS 上跑 `deploy_controller.sh`；nginx include 后公网探针；`https://bd2026.cc/downloads/fleet/` 目前未就位（安装器默认下载地址会 302 循环，需官网侧放置发布物）。双击安装包的 UAC / 完成页要在 Windows 上点一次才算真机确认。
