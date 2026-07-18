# 通译 LingoX → 生产机 .117 迁移作战手册（P3 生产落地）

> 适用：把本机试点已验证的通译 LingoX 双实例（`deploy/instances/README.md` §10）落到生产机
> **192.168.0.117**（`deploy/machines.json` 条目 `tongyi`，中文名「通译」，Administrator，
> ssh 别名 `tongyi`/`hub117`/`pc117`，工作仓 `D:\workspace\boundless`，`D:\boundless` 为联接）。
> 执行人：.117 运维（本地 PowerShell）；本手册所有命令都在 .117 本机跑，不含任何远程操作。
> 配套脚本：`migrate_117.ps1`（阶段1 编排，缺省 DryRun）+ `verify_instance.ps1`（只读验收）。
> 上游文档：`README.md` §3 初始化 / §8 迁移时序 / §10 核对表；本手册把它们具体化到 .117 并补齐回滚点。

**总原则（先读再动手）**

1. **现网数据属智聊，通译全新起**——本手册主线只做「通译新实例上线」，全程不碰现网智聊的
   任何数据与进程；智聊双实例化是另一个维护窗口的事（README §3.2/§8，本手册末尾只给衔接点）。
2. **先增后减**：先把新实例跑稳（阶段1–3），再动登记与暴露面（阶段4–5）。
3. **破坏性动作只在人工步骤**：停现网、禁计划任务、改 stack.json、开防火墙，全部人手执行，
   不进任何脚本——这是刻意的安全护栏，别把它们「自动化」进 migrate_117.ps1。
4. 每阶段末尾有**验证通过判据**与**失败回滚动作**；判据不过就回滚或停在原地，不带病进下一阶段。

---

## 1. 前置条件核对（= README §10 核对表，具体到 .117）

在 .117 上逐项打勾；任何一项不过，不进入阶段1。

### 1.1 机器与代码

- [ ] 登录 .117（`Administrator`），确认工作仓在位且为最新：

```powershell
cd D:\workspace\boundless
git pull --rebase
git log -1 --oneline    # 确认包含 deploy/instances/ 的 verify/migrate 脚本
```

- [ ] `engines\chengjie\`、`deploy\instances\` 目录齐全；运维对 `engines\` **只读**（改代码走 git，README §9）。

### 1.2 Python 与依赖

- [ ] `python` 在 PATH（版本以预检输出为准；本机试点为 3.13.9）。
- [ ] 依赖预检（见 1.5 一并跑）：`google-genai` 缺失可接受（软依赖，引擎降级，通译不用 gemini）；
  其余 MISS 对照 `engines\chengjie\requirements.txt` 补齐。要全绿可按 README §10 建
  `.venv-pilot`（`python -m venv --system-site-packages engines\chengjie\.venv-pilot`，装缺包后
  预检与后续启动都传该 venv 的 python）。

### 1.3 端口（.117 的既有占用要心里有数）

- [ ] 通译主位 **18899**、备用位 **18887** 空闲；同时核对智聊现网 18799 的现状（谁在听）：

```powershell
netstat -ano | findstr "18899 18887 18799"
```

- [ ] .117 已占端口对照（勿冲突）：`7852`（emotion_tts）、`7858`（qwen3_tts）——见
  `deploy/cluster_map.json` hosts `192.168.0.117`；全局已占参照 README 附录端口表。
- [ ] `deploy/cluster_map.json` 与 `deploy/stack.json` 的端口登记仍与 README 附录一致（漂移即先纠登记）。

### 1.4 数据根选址与磁盘

- [ ] 定稿数据根（二选一，后续所有命令的 `<数据根>` 都用它）：
  - 仓内缺省：`D:\workspace\boundless\deploy\instances\tongyi\data`（git 已忽略 `data/`）；
  - 仓外（推荐生产）：如 `D:\chengjie-instances\tongyi\data`——启动/探测/停止/验收全部带数据根参数。
- [ ] 所在盘余量：全套 SQLite + `logs\` + `events\spool`（append-only 只增不删）会持续增长，
  建议预留 ≥ 20 GB 并纳入磁盘监控：

```powershell
Get-PSDrive D | Select-Object Used, Free
```

### 1.5 一键预检（1.2/1.3/1.4 的机器化复核）

```powershell
powershell -ExecutionPolicy Bypass -File deploy\instances\preflight_instance.ps1 -DataDir <数据根> -Ports 18899,18887
# 用 venv 时加：-PythonExe D:\workspace\boundless\engines\chengjie\.venv-pilot\Scripts\python.exe
# 退出码 0（允许 WARN）才继续；FAIL 处置见 §5 应急速查
```

### 1.6 环境变量与密钥（谁的变量、放哪里，别搞混）

| 变量 | 归属 | 谁来设 | .117 上要做的事 |
|---|---|---|---|
| `AITR_DATA_DIR` / `EVENT_SPOOL_DIR` / `CHENGJIE_PRODUCT_ID` / `CHENGJIE_LEDGER_OUTBOX` / `LICENSE_KEY` | 实例进程 | `start_tongyi.ps1` 每次启动自动注入（产品号=tongyi，spool/outbox 指向实例数据根） | **不要**设成机器级环境变量——机器级残留是双实例串味源；核对系统环境变量里没有遗留的 `AITR_*`/`EVENT_SPOOL_DIR`/`CHENGJIE_*` |
| `EVENT_INGEST_KEY` | 集团上报（机器级 M2M 密钥，一台厂商机一把） | 运维从集团侧（官网 env 同款）取得，交给 uploader/purge 定时任务用 | 本阶段可后置：不配则事件只落本地 spool 不上传，不丢（阶段3 再接 uploader cron）。密钥不入 git、不写进任何仓内文件 |
| 集团 API 基址（预留名 `PERSONA_SYNC_BASE`，以引擎侧接线为准） | 人设 purge 执行器 | purge 执行器接线时（实施11 §七.1，尚未接线）配置，如 `https://bd2026.cc` | 本轮无需配置；见 §4.3 归属澄清 |

- [ ] 通译授权码已取得并存 `<数据根>\config\license.key`（**与智聊不同 sub/lic_id**，§4.1）；
  社区模式试运行可暂缺（verify 会给 WARN 不给 FAIL）。
- [ ] 实例 overlay 必填密钥已备好待填（阶段1 初始化时写入实例文件，勿回写模板）：
  `ai.api_key/base_url/model` + 强随机 `web_admin.auth_token`/`secret_key`。

---

## 2. 分阶段时序（每阶段：动作 → 通过判据 → 回滚动作）

### 阶段0 · 备份现网智聊数据（灾难兜底快照，不停机）

现网智聊此刻还是单实例形态，通译上线不碰它——但动手前先留一份快照，这是整场迁移的兜底。

0.1 **核实现网实例位置**（两种可能：老路径 `D:\workspace\telegram-mtproto-ai`，或已在
`boundless\engines\chengjie` 里跑）：

```powershell
netstat -ano | findstr ":18799"
Get-CimInstance Win32_Process -Filter "ProcessId=<上行的PID>" | Select-Object ProcessId, ExecutablePath, CommandLine
# CommandLine 里的 main.py 路径 = 现网实例位置；record 下来，下文 $src 用它
```

0.2 **冷拷快照**（运行中拷贝，SQLite 可能拿到中间态——本快照定位是灾难兜底；
智聊正式迁移那个维护窗口会在**停机后**另取一致性快照，README §8）：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd_HHmm'
$src   = "D:\workspace\telegram-mtproto-ai"          # ← 按 0.1 核实结果替换
$dst   = "D:\backups\zhiliao_pre_tongyi_$stamp"      # 备份位置按 .117 实际磁盘选
robocopy "$src\config"   "$dst\config"   /E /R:2 /W:2 /NFL /NDL
robocopy "$src\sessions" "$dst\sessions" /E /R:2 /W:2 /NFL /NDL
# robocopy 退出码 < 8 即成功（1=有文件拷贝，属正常）
```

0.3 **登记自启任务现状**（只登记不动作，处置在阶段5）：

```powershell
schtasks /Query /FO LIST /V | findstr /i "main chengjie telegram tongyi zhiliao"
```

- **通过判据**：`$dst` 下 config/sessions 齐全；任务清单已记录进值班日志。
- **回滚动作**：无（本阶段零改动）。

### 阶段1 · .117 拉起通译新实例（独立数据根，不碰现网）

1.1 先干跑编排，核对步骤与路径：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\instances\migrate_117.ps1 -DataDir <数据根>
# 缺省即 DryRun：打印 preflight→初始化闸→start→wait→verify 的完整命令清单，零副作用
```

1.2 **人工初始化数据根**（DryRun 的「初始化闸」会打印带真实路径的命令样板；即 README §3.1）：
建骨架 → `config.example.yaml` 起底 → 拷通译 overlay 模板 → **替换式**填入 `ai.api_key` 等与强随机
`auth_token`/`secret_key`（勿在文件尾追加第二个 `web_admin:` 块，YAML 重复顶层键会整段覆盖、
端口回落 18787）→ 建 domains junction → 授权码存 `config\license.key`。

1.3 实跑编排（= preflight → 初始化闸 → start_tongyi → 等就绪 → verify）：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\instances\migrate_117.ps1 -Execute -DataDir <数据根>
```

- **通过判据**：编排 5 步全 OK（退出码 0）；即 preflight 0、`start_tongyi.ps1` 拉起、
  约 25s 内 18899 有 HTTP 响应、`verify_instance.ps1` 无 FAIL。复核双实例总览：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\instances\status_instances.ps1 -TongyiData <数据根>
# 期望：tongyi=GO；zhiliao 按现状（现网未双实例化时显示 DOWN/未初始化属预期）
```

- **失败回滚**：`stop_instance.ps1 -Instance tongyi` 停实例（防呆：验明持有者才杀树，幂等）；
  数据根是全新数据，可原地修复后重跑（编排幂等），也可整目录删掉重来。**现网零影响。**

### 阶段2 · 灰度验证（品牌 / 登录 / 翻译 / 事件上报 / 授权）

机器化部分（可重复跑、可出 JSON 存档）：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\instances\verify_instance.ps1 -Base http://127.0.0.1:18899 -Instance tongyi -DataDir <数据根> -CheckSpoolGrowth
powershell -ExecutionPolicy Bypass -File deploy\instances\verify_instance.ps1 -Base http://127.0.0.1:18899 -Instance tongyi -DataDir <数据根> -Json > logs\verify_117_$(Get-Date -Format yyyyMMdd_HHmm).json
```

人工清单（浏览器操作；实例只绑 127.0.0.1，从工位机验证用 ssh 端口转发
`ssh -L 18899:127.0.0.1:18899 tongyi` 后开 `http://127.0.0.1:18899/login`）：

- [ ] **品牌**：登录页 title「登录 · 通译 LingoX」，全页无「智聊」字样；`/manifest.webmanifest`
  品牌=通译（verify 已断言，人眼再过一遍）。
- [ ] **登录**：用实例 overlay 里的 `auth_token` 能登录后台。
- [ ] **翻译**：坐席工作台发起一次真实翻译（web_chat widget 入站或收件箱粘贴均可），
  确认走「本地 MT→AI 兜底」链路出译文；无本地 MT 时回落 `ai` 引擎属预期。
- [ ] **事件上报**：翻译/登录操作后 spool 有新事件且产品号正确（也可用 verify 的 `-CheckSpoolGrowth`）：

```powershell
Get-Content "<数据根>\events\spool\events-$((Get-Date).ToUniversalTime().ToString('yyyyMMdd')).jsonl" -Tail 5
# 期望：行内 "product_id":"tongyi"，事件名 tongyi.* 前缀（文件按 UTC 日期分片）
```

- [ ] **授权**：后台设置页显示实例授权档位/额度正常。
  ⚠ **绝不在后台用「粘贴激活」**——它写共享的引擎根 `config\license.key`，会同时影响两个实例
  （README §5）；换授权 = 替换 `<数据根>\config\license.key` 后重启该实例。

- **通过判据**：verify 无 FAIL + 人工清单五项全勾。
- **失败回滚**：同阶段1（停实例、修复、重跑）；品牌/端口类问题多半是 overlay 填写问题，
  改 `<数据根>\config\config.local.yaml` 后 30s 热重载（端口变更需重启实例）。

### 阶段3 · 观察期（建议 ≥ 1 周，实施11 §七.3）

- 日巡检（可挂计划任务，退出码进监控）：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\instances\status_instances.ps1 -Json -TongyiData <数据根>
powershell -ExecutionPolicy Bypass -File deploy\instances\verify_instance.ps1 -Base http://127.0.0.1:18899 -Instance tongyi -DataDir <数据根> -Json
```

- 观察项：`<数据根>\logs\app.log` 无持续报错、进程内存平稳、spool 正常增长、磁盘余量、
  （若已接 uploader）上传日志无 4xx。
- 本阶段可顺手接**事件补传 cron**（拿到 `EVENT_INGEST_KEY` 后；单实例单 spool，勿并发跑同一目录）：

```powershell
python platform\observability\uploader.py --endpoint https://bd2026.cc/api/collect --key <EVENT_INGEST_KEY> --spool-dir "<数据根>\events\spool"
# 先加 --dry-run 看将上传行数；正式跑挂计划任务每 5 分钟一次
```

- **通过判据**：连续 7 天巡检 GO、无未解释的 FAIL/重启。
- **失败回滚**：停实例排查；观察期内通译无对外依赖，停多久都不影响现网。

### 阶段4 · 登记翻正 + 对外暴露（人工步骤，不进脚本）

4.1 **stack.json 翻 enabled**（git 文件，改动走 git 提交）：`deploy/stack.json` 里
`chengjie_tongyi` 条目 `enabled` 改 `true`。⚠ 只动通译条目——旧 `chengjie` 条目与
`chengjie_zhiliao` 属智聊迁移窗口（README §8.6），本轮不碰。

4.2 **对外暴露**（按需，二选一或都做）：

- 内网直用：实例 overlay `web_admin.host` 改 `0.0.0.0`（host/port 是启动期绑定，热重载
  不会重绑监听——改完**必须重启实例**）+ 防火墙放行：

```powershell
netsh advfirewall firewall add rule name="chengjie_tongyi_18899" dir=in action=allow protocol=TCP localport=18899
```

- 子域反代（如 `lingox.<主域>`）：在能同时到达公网与内网的反代机（如官网 VPS 经隧道/组网，
  网络打通方案按公司拓扑另定）配 nginx；样例：

```nginx
server {
    listen 443 ssl;
    server_name lingox.bd2026.cc;           # 正式子域按域名规划定
    # ssl_certificate / ssl_certificate_key ...;
    location / {
        proxy_pass http://192.168.0.117:18899;   # 反代机须可达 .117 内网
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;                   # 工作台有流式/长连接
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

- **通过判据**：`deploy\deploy.ps1 -Action status`（默认剖面不含 chengjie-dual，加
  `-Profile chengjie-dual` 或 `-Only chengjie_tongyi`）看到通译 GO；对外入口（内网 IP 或子域）
  可达且登录页品牌正确、HTTPS 证书有效（走反代时）。
- **失败回滚**：撤防火墙规则/摘 nginx server 块 → overlay `host` 改回 `127.0.0.1` 并重启实例 →
  stack.json `enabled` 改回 `false`。实例本体与数据不动。

### 阶段5 · 老自启任务处置

结合阶段0.3 的任务清单：

- **本轮（只上通译）**：老的智聊/单实例自启任务（`start_main.ps1` 或老路径下的登录触发任务）
  **暂不禁用**——它管的是 18799 现网，禁了反而影响现网自愈。只需确认：无任何任务会碰
  18899/18887（有则先改掉），并把清单登记在值班日志。
- **智聊迁移窗口（另择窗，README §8.2–8.3）**：停现网
  `powershell -File deploy\deploy.ps1 -Action down -Only chengjie -Force` 后**必须**禁用老任务
  `schtasks /Change /TN <任务名> /DISABLE`——老脚本开机会按「清幽灵端口」逻辑强杀 18799
  持有者（= 杀掉未来的智聊新实例）再抢启动。
- **（可选）给通译配自启**：观察期结束后再上（期间手动管理便于控制变量）：

```powershell
schtasks /Create /TN "Chengjie_Tongyi_Boot" /SC ONSTART /RU Administrator /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\workspace\boundless\deploy\instances\start_tongyi.ps1 -DataDir <数据根>"
# start_tongyi.ps1 幂等 + 绝不清杀端口，作为自启任务是安全的（与老 start_main.ps1 的关键差异）
```

- **通过判据**：重启 .117 一次，通译自动/手动拉起后 verify 通过；老任务行为与预期一致。
- **失败回滚**：`schtasks /Change /TN Chengjie_Tongyi_Boot /DISABLE`（或 `/Delete`）；实例照常手动起停。

### 后续衔接 · 智聊双实例化（本手册不覆盖，只给入口）

另择维护窗口，按 README §3.2 + §8 执行：停现网 → 禁老任务 → 现网 `config\`+`sessions\` 迁入
`zhiliao\data` → 并入品牌模板（**手工并入**，绝不整文件覆盖现网 overlay）→ `start_zhiliao.ps1`
→ 双 GO → 观察后 stack.json 旧 `chengjie` 条目 `enabled=false`、`chengjie_zhiliao` 改 `true`。
**回滚点：老目录数据原地保留不删**（回滚 = 停新实例、重启老任务）。阶段0 的快照另作双保险。

---

## 3. 迁移时序总览（含回滚点）

| 阶段 | 动作 | 回滚点 |
|---|---|---|
| 0 | 备份现网智聊 config/sessions（不停机快照）+ 登记自启任务 | 零改动，无需回滚 |
| 1 | preflight → 人工初始化数据根 → start → verify（`migrate_117.ps1 -Execute`） | 停通译实例即回到迁移前；现网全程未动 |
| 2 | 灰度验证：verify 机器断言 + 品牌/登录/翻译/事件/授权人工清单 | 同阶段1 |
| 3 | 观察期 ≥1 周：status/verify 日巡检（-Json 存档）；可接 uploader cron | 停实例即可，无对外依赖 |
| 4 | stack.json `chengjie_tongyi` 翻 true；对外暴露（0.0.0.0+防火墙 或 lingox 子域反代） | 撤规则/摘反代 → host 回 127.0.0.1 → enabled 回 false |
| 5 | 老自启任务盘点（本轮不禁用）；可选给通译建自启 | 禁用/删除新任务即可 |

---

## 4. 数据与授权（双实例归属速查）

### 4.1 授权（license）

- 通译用**独立** `license.key`：`<数据根>\config\license.key`，启动脚本读文件注入 `LICENSE_KEY`
  环境变量（优先于共享的引擎根文件，README §0 例外①/§5）。
- 与智聊**必须不同 sub/lic_id**：`license_quota.db` 是两实例共享文件（README §0 例外②），
  记账按 `lic_id` 分键——同 lic_id 会串额度。
- 换授权 = 换实例的 `license.key` 文件 + 重启该实例；**禁用后台「粘贴激活」**（写共享文件，殃及两实例）。

### 4.2 telemetry（事件上报）——**按实例**

- spool：`EVENT_SPOOL_DIR=<数据根>\events\spool`，启动脚本按实例注入；两实例两个 spool，互不混写。
- 产品号：`CHENGJIE_PRODUCT_ID` 由启动脚本注入（`start_tongyi.ps1`=tongyi、`start_zhiliao.ps1`=zhiliao；
  引擎缺省 zhiliao，所以通译**必须**靠脚本显式设，直接手跑 `python main.py` 会把事件记到智聊头上）。
- 上传：uploader **每个 spool 目录跑一份**（`--spool-dir` 显式传，勿并发跑同一目录；state 游标
  在各自 spool 内）；`EVENT_INGEST_KEY` 是机器级 M2M 密钥，.117 的两个实例共用同一把。

### 4.3 outbox（授权台账钩子）——**按实例**

- `CHENGJIE_LEDGER_OUTBOX=<数据根>\ledger_outbox`，启动脚本按实例注入；引擎侧钩子（实施09 §五.3，
  接线中）上线后自动分实例落盘，脚本零改动。收割/上报侧任务与 spool 同理按目录分开。

### 4.4 人设 purge 执行器——**按引擎一份，删除范围必须覆盖全部实例数据根**

- 契约（`platform/identity/PERSONA_BUS.md` §5）：purge 指令按 `source_system` 下发，
  **system=chengjie 是引擎枚举、不是产品枚举**——智聊/通译同引擎，集团队列里只有一份
  `system=chengjie` 的待办，执行器（定时任务）也只跑一份，轮询
  `GET /api/sync/personas/purges?system=chengjie`（Bearer `EVENT_INGEST_KEY`）。
- **双实例的关键语义**：chengjie 的人设资产（`profiles_runtime.yaml`、`voice_refs\` 等，
  PERSONA_BUS §6 映射）落在**各实例自己的 config 目录**。ack 是引擎级回执——执行器必须
  **遍历两个实例的数据根**（`zhiliao\data`、`tongyi\data`，含未来新增实例）把同一 `source_key`
  的资产全部删净后才 `ok=true`；只删一个实例就回执 = 另一实例成漏网，违反 PERSONA_BUS §5.3 义务。
  找不到的项记 `detail.missing` 照常 ok（幂等语义）。
- **现状**：引擎侧执行器尚未接线（实施11 §七.1），清除指令在集团队列排队等待、不丢失——
  **不阻塞本次迁移**。接线时的实现形态：一个计划任务，输入=实例数据根清单，集团 API 基址
  （预留名 `PERSONA_SYNC_BASE`）+ `EVENT_INGEST_KEY` 从运维密钥管理取。

---

## 5. 应急速查

### 5.1 端口冲突（preflight/start 报端口被占）

```powershell
netstat -ano | findstr ":18899"
Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" | Select-Object ProcessId, Name, CommandLine
```

- 持有者是 `python …main.py` → 通译已在跑（start 幂等退出 0，属正常）；要重启就先
  `stop_instance.ps1 -Instance tongyi`。
- 持有者是别的进程 → **绝不清杀**（双实例铁律）。人工判断：挪走占用方，或（最后手段）给通译换端口
  ——同时改三处：实例 overlay `web_admin.port`、`deploy/stack.json` 条目、README 附录端口表，
  再重启实例（端口登记单一源纪律，README §9）。

### 5.2 依赖缺失（preflight DEP_MISS / 全链烟测 FAIL）

- 只有 `google-genai` MISS → 放行（软依赖，`import main` 烟测过即可）；要全绿走 `.venv-pilot`
  （§1.2），之后 preflight/启动都带 `-PythonExe`。
- 其他包 MISS 且烟测 FAIL → `python -m pip install -r engines\chengjie\requirements.txt`
  后复跑预检；企业代理环境记得 pip 源配置。
- `import main` FAIL 而依赖全 OK → 看烟测输出末三行定位（通常是引擎代码/环境问题，找引擎同事，
  不要在生产机改引擎代码）。

### 5.3 健康不过（端口在听但 verify FAIL / status DEGRADED）

排查顺序：

1. `<数据根>\logs\boot_*.err.log`（启动期异常直接在这）；
2. `<数据根>\logs\app.log`（运行期报错）；
3. 常见原因对照：

| 症状 | 多半是 | 处置 |
|---|---|---|
| 端口回落 18787 | overlay 没生效：文件尾追加了第二个 `web_admin:` 块（YAML 重复顶层键整段覆盖） | 修 overlay 为单一 `web_admin:` 块，重启实例 |
| 品牌断言 FAIL（还是智聊/默认品牌） | overlay 没拷进实例 config 目录，或 brand 段缺失 | 补拷/补段，30s 热重载后复跑 verify |
| health 200 无鉴权（verify WARN） | `auth_token` 未填 | 填强随机 token，重启实例 |
| HTTP 全无响应但进程在 | 引擎还在初始化（约 25s）或启动挂死 | 等 60s 复测；仍无响应看 boot_*.err.log |
| `_unregistered` 事件 | 事件未注册进 registry（fail-silent 不丢） | 不阻塞；登记给 P4 同事 |

- 回滚：任何阶段健康不过都可 `stop_instance.ps1 -Instance tongyi` 后修复重来，现网无感。

### 5.4 数据目录权限 / junction

```powershell
# 目录可写探针（preflight 第 5 项就是它）：
powershell -ExecutionPolicy Bypass -File deploy\instances\preflight_instance.ps1 -DataDir <数据根> -Ports 18899,18887
# 权限修复（服务账号按实际运行者）：
icacls "<数据根>" /grant "Administrator:(OI)(CI)F"
# junction 体检与重建（目标必须指向引擎 domains；跨盘 junction 合法）：
Get-Item "<数据根>\domains" | Select-Object FullName, LinkType, Target
Remove-Item "<数据根>\domains" -Force
New-Item -ItemType Junction -Path "<数据根>\domains" -Target "D:\workspace\boundless\engines\chengjie\domains"
```

⚠ 删 junction 用 `Remove-Item <junction本体> -Force`（不带 `-Recurse` 指向本体即安全）；
绝不要对着 junction **内部**删东西——那是在删引擎的 domains 真身。

---

## 6. 参考索引

| 文档/脚本 | 用途 |
|---|---|
| `deploy/instances/README.md` §3/§8/§10 | 初始化步骤、双实例迁移时序、试点记录与核对表（本手册的上游） |
| `deploy/instances/migrate_117.ps1` | 阶段1 编排（缺省 DryRun；-Execute 实跑；初始化永远人工） |
| `deploy/instances/verify_instance.ps1` | 阶段1/2/3 只读验收（-Json 存档、-CheckSpoolGrowth 事件观察、-WhatIf 干跑） |
| `deploy/instances/preflight_instance.ps1` | 前置预检（-DataDir/-Ports/-PythonExe） |
| `deploy/instances/start_tongyi.ps1` / `stop_instance.ps1` / `status_instances.ps1` | 起 / 停 / 双实例健康 |
| `deploy/machines.json` + `docs/MACHINE_SSH.md` | .117 台账（IP/账号/别名/工作仓）与 SSH 约定 |
| `deploy/cluster_map.json` / `deploy/stack.json` | .117 已占端口、chengjie_tongyi 服务登记（阶段4 翻 enabled） |
| `platform/observability/EVENT_CONTRACT.md` §10 | 收集器 + uploader 部署（阶段3 接 cron） |
| `platform/identity/PERSONA_BUS.md` §5/§6 | purge 协议与 chengjie 槽位映射（§4.4 的依据） |
