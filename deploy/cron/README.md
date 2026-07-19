# deploy/cron/ · 引擎机定时任务接线包 v2（P4/P5 运营手册）

> 让六个周期性动作——**事件补传 uploader**（P4）、**人设清除 purge agent**（P5）、
> **人设/授权台账导出 export**（P4/P5）、**grant 缓存同步 grants_sync**（P5 软门控）、
> **配置快照守护 config_snapshot**（.117 chengjie 专属，§3.4）、
> **KPI 周报 kpi_weekly**（P4 报表，website 机可选装）——在五台 Windows 机上以计划任务方式标准化落地。
> 机器台账单一源：`deploy/machines.json`；双实例契约：`deploy/instances/README.md` §4/§7 与
> `deploy/instances/migrate_117_runbook.md` §4。
>
> **铁律**：本目录的安装/卸载脚本**缺省 WhatIf 演练**——只打印将注册/卸载的任务定义，绝不落地；
> `-Execute` 才真装/真卸，由各机运维在**管理员 PowerShell** 里执行。仓库侧绝不预注册任何计划任务。

---

## 1. 环境变量与密钥

| 变量 | 级别 | 谁来设 | 说明 |
|---|---|---|---|
| `EVENT_INGEST_KEY` | **机器级**（一台厂商机一把 M2M 密钥） | 运维：`setx /M EVENT_INGEST_KEY <密钥>`（管理员），或壳脚本临时 `-IngestKey` | uploader `--key`、purge `--key`、**grants_sync `--key`** 同一把（与 `/api/collect`、`/api/sync/personas/grants` 共用）。**绝不入 git、缺省不进任务定义**（任务 XML 本机管理员可读）；未配置时任务以退出码 2 失败并写明原因。⚠ 没有叫 `EVENT_INGEST_URL` 的变量——同步/上报 URL 走下行 `PERSONA_SYNC_BASE` 或 CLI 参数 |
| `PERSONA_SYNC_BASE` | 机器级（可选） | 可不设 | purge 执行器与 **grants_sync fetch 器**的基址回退链：`--base` > env > `https://bd2026.cc`（uploader 的 endpoint 同源拼 `<base>/api/collect`）；安装器 `-BaseUrl` 显式覆盖 |
| `EVENT_SPOOL_DIR` | 实例/引擎进程（**发射侧**） | chengjie 由 `start_zhiliao/tongyi.ps1` 注入；avatarhub/huoke 引擎缺省自带 | cron 侧**不依赖它**：uploader 一律显式 `--spool-dir`，防机器级残留串目录 |
| `PERSONA_GRANT_CACHE` / `PERSONA_GRANT_ENFORCE` | 引擎进程（**消费侧**） | 引擎启动脚本按需注入 | 与 cron 无关：grants_sync 只负责把缓存写到 `data\persona_bus_out\<engine>_grants.json`，引擎侧 `grant_gate.py` 经 `PERSONA_GRANT_CACHE` 指向它（缺省 warn 不挡业务）；列在此说明产物去向 |
| `EVENTS_DB` / `LEDGER_DB` | 机器级（kpi_weekly 装机才需要） | 运维 `setx /M` | KPI 周报生成器只读打开的双库路径；未设时缺省 `LEADS_DIR`（再缺省 `~\hualing-leads`）下的 `group-events.db` / `group-ledger.db`。库缺失不报错——出「暂无数据」骨架报告退出 0 |
| `CHENGJIE_PRODUCT_ID` / `AITR_DATA_DIR` / `CHENGJIE_LEDGER_OUTBOX` / `LICENSE_KEY` | 实例进程 | 双实例 start 脚本每次启动注入 | 与 cron 无关，**不要设成机器级**（双实例串味源，runbook §1.6）；列在此仅防误配 |
| chengjie 实例数据根清单 | CLI 参数（非环境变量） | 安装器/壳脚本 `-DataRoots`（`-File` 语义下多值用逗号串） | 缺省 `deploy\instances\zhiliao\data` + `deploy\instances\tongyi\data`；迁移前单实例期传 `"engines\chengjie"` |

日志落 `logs\cron\<任务名>.log`（根 `.gitignore` 已忽略 `logs/`，本包不动它）；导出产物与 grant 缓存落
`data\persona_bus_out\`（`data/` 同样已忽略；含显示名/指纹/客户名，属经营数据，勿外传）；
KPI 周报落 `deploy\cron\logs\reports\kpi_weekly_<yyyyMMdd_HHmm>.md`（`logs/` 全局忽略规则同样罩住它）。

## 2. 任务矩阵（机器 × 任务）

六类任务统一口径（config_snapshot 仅 .117 chengjie 装，§3.4）：

| 任务 | 节律 | 底层脚本与参数 | 包装壳 |
|---|---|---|---|
| **uploader** | 每 5 分钟 | `platform/observability/uploader.py --spool-dir <spool> --endpoint <base>/api/collect --batch 200`（`--key` 经环境变量；断点续传、幂等重发） | `run_uploader.ps1` |
| **purge** | 每 10 分钟 | `engines/<engine>/…/persona_purge_agent.py --once --commit --input <根>`（`--base`/`--key` 同上；**软删进 trash**，见 §4） | `run_purge.ps1` |
| **export** | 每日 03:30 | 三段式：`tools/persona_bus/export_*.py` 导出 → `validate_personas.py` / `validate_export.py` 校验 → scp/ssh 传输导入（§5）；license：avatarhub=`tools/license_ledger/export_avatarhub.py`，chengjie=`engines/chengjie/scripts/ledger_outbox.py --export`，huoke=无（自动跳过） | `run_export.ps1` |
| **grants_sync** | 每 30 分钟 | `tools/persona_bus/fetch_grants.py --system <engine> --out data\persona_bus_out\<engine>_grants.json`（`--key` 经环境变量 `EVENT_INGEST_KEY`、`--base` 回退 `PERSONA_SYNC_BASE`；原子写缓存供 `grant_gate.py` 软门控，§5.4） | `run_grants_sync.ps1` |
| **config_snapshot** | 每 10 分钟 | 对每个实例 config 目录维护纯本地 git 快照仓：`git add -A` → 有变更才 `commit -m "snapshot <ISO时间戳>"`（无变更零空提交；`.gitignore` 排除 db/wal/shm/日志/purged_trash，只快照配置文本；无 remote 永不 push，§3.4） | `run_config_snapshot.ps1` |
| **kpi_weekly** | 每周一 09:00 | `node website/scripts/kpi-weekly-report.mjs --week last --format md --out deploy\cron\logs\reports\kpi_weekly_<时间戳>.md`（只读聚合 `EVENTS_DB`/`LEDGER_DB` 双库；库缺失出骨架报告退出 0） | `run_kpi_weekly.ps1` |

分机矩阵（任务名统一 `\Boundless\Boundless-<engine>-<task>[-<实例>]`；kpi_weekly 例外=`Boundless-website-kpi_weekly`）：

| 机器 | 引擎 | uploader（每 5 分钟） | purge（每 10 分钟，--once --commit） | export（每日三段式） | 所需环境变量 |
|---|---|---|---|---|---|
| **.176 幻声** huansheng | avatarhub | 1 个：spool 缺省 `engines\avatarhub\data\events\spool` | `engines/avatarhub/persona_purge_agent.py`；trash=`engines\avatarhub\secrets\purged_trash\` | persona=`export_avatarhub_personas.py`；license=`export_avatarhub.py`（secrets/orders.json、trials.json、license.key） | `EVENT_INGEST_KEY`（机器级） |
| **.117 通译** tongyi（chengjie **双实例**） | chengjie | **每实例 1 个**：`Boundless-chengjie-uploader-zhiliao` / `-tongyi`，spool=`<实例数据根>\events\spool`；**勿并发同一 spool**（§3） | **每引擎 1 份**，一轮覆盖两实例数据根（`--data-roots "R1;R2"`，agent 已支持，§3）；trash=各根 `config\purged_trash\` | persona 每实例一份（`--input <数据根>` → `chengjie_personas_<实例>.json`）；license=每实例 outbox 一份（`ledger_outbox.py --export`，未接线自动跳过） | `EVENT_INGEST_KEY`；实例数据根经 `-DataRoots` 传参（`CHENGJIE_PRODUCT_ID` 等实例变量与 cron 无关） |
| **.198 智拓** zhituo | huoke | 1 个：spool 缺省 `engines\huoke\data\events\spool` | `engines/huoke/src/persona_purge_agent.py`；trash=`engines\huoke\data\purged_trash\` | persona=`export_huoke_personas.py`；license 无数据源自动跳过 | `EVENT_INGEST_KEY` |
| **.104 幻颜节点 / .140 通传节点** | avatarhub（算力节点） | **默认不装**：算力节点只跑推理服务，不产事件 spool；日后本机落 spool 再单装 uploader | 不装（人设资产在 .176 主机） | 不装 | — |
| **VPS 官网机** | website | —（服务端收集器） | —（注册表在服务端） | 导入侧 `ledger-import-personas.mjs` / `ledger-import-licenses.mjs`；KPI 周报/账本备份/订单 SLA 走 Linux crontab（§5.3） | 服务端 `.env`（`EVENT_INGEST_KEY` 等，非本包职责） |

### 2.1 五机安装矩阵（v2 速查：每台装什么、要配什么、装前核对什么）

grants_sync 已纳入引擎机缺省任务集（`install_tasks.ps1` 缺省 `-Tasks uploader,purge,export,grants_sync`）；
kpi_weekly 是独立开关 `-WithKpiWeekly`，只在 **website 所在 Windows 机**（现状=.117 通译，`machines.json` 的
`dev_paths` 含 `website`）可选装——**website 生产在 VPS（165.154.233.121, ubuntu），Windows 计划任务不适用，
VPS 用 §5.3 ① 的 crontab 等价命令**。

| 机器 | 安装命令（先演练，核对后加 `-Execute`） | 装的任务 | 机器级 env（`setx /M`） |
|---|---|---|---|
| **.176 幻声**（avatarhub 引擎机） | `install_tasks.ps1 -Engine avatarhub` | uploader / purge / export / grants_sync | `EVENT_INGEST_KEY` 必填；`PERSONA_SYNC_BASE` 可选（缺省 `https://bd2026.cc`） |
| **.117 通译**（chengjie 引擎机 + website 开发机） | `install_tasks.ps1 -Engine chengjie -WithKpiWeekly` | uploader×2（双实例）/ purge / export / grants_sync / config_snapshot（§3.4，chengjie 缺省任务集自动含） + kpi_weekly（可选装，本机是 website 开发机） | `EVENT_INGEST_KEY` 必填；`PERSONA_SYNC_BASE` 可选；装 kpi_weekly 再加 `EVENTS_DB` / `LEDGER_DB`（不设则报告只有骨架，指向缺省 `~\hualing-leads` 下双库） |
| **.198 智拓**（huoke 引擎机） | `install_tasks.ps1 -Engine huoke` | uploader / purge / export / grants_sync | `EVENT_INGEST_KEY` 必填；`PERSONA_SYNC_BASE` 可选 |
| **.104 幻颜节点**（算力节点，无引擎任务） | 不跑安装器 | **全部不装**（只跑推理服务；无 spool、无人设资产、无 grant 消费点） | — |
| **.140 通传节点**（算力节点，无引擎任务） | 不跑安装器 | **全部不装**（同上） | — |
| **VPS 官网机**（165.154.233.121, ubuntu） | 不适用本安装器（Windows 计划任务） | KPI 周报 / 账本备份 / 订单 SLA / 台账导入走 **Linux crontab**（§5.3；KPI 样例=§5.3 ①） | 服务端 `.env`（`EVENT_INGEST_KEY` 等，非本包职责） |

**每台机 `-Execute` 前核对清单**（详版见 §7）：

- [ ] 演练输出的任务清单与本表一致，逐条 `[检查]` 行无意外黄字；
- [ ] `[Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY','Machine')` 非空（uploader/purge/grants_sync 共用）；
- [ ] python 可用（SYSTEM 账户看机器级 PATH；不在则 `-PythonExe` 全路径）；装 kpi_weekly 的机器再核 node 可用
      （不在则 `-NodeExe` 全路径）且 `website\node_modules` 已 `npm install`（缺 better-sqlite3 任务必败）；
- [ ] .117 专属：spool/数据根形态与迁移进度一致（§3）；装 kpi_weekly 前确认 `EVENTS_DB`/`LEDGER_DB` 指向本机
      真实库位，否则周报只有「暂无数据」骨架。

## 3. 双实例注意（.117 chengjie 专属）

1. **uploader：每 spool 目录一个任务实例，勿并发跑同一 spool**（`uploader.py` 文件头纪律；断点游标
   `<spool>\.upload_state.json` 单写者）。双实例=两个任务，各自 `--spool-dir <数据根>\events\spool`；
   任务级再加一道闸 `MultipleInstances=IgnoreNew`（上轮没跑完，新触发直接跳过，绝不同目录双开）。
   ⚠ 现网**单实例**形态（未迁双实例前）的 spool 在 `engines\chengjie\config\events\spool`
   （telemetry 解析序：`EVENT_SPOOL_DIR` > `<config 目录>\events\spool` > `data\events\spool`）——
   这时装 uploader 须显式 `-SpoolDirs "engines\chengjie\config\events\spool"`，别用双实例缺省。
2. **purge：每引擎一份任务，但 ack 是引擎级回执，必须一轮覆盖全部实例数据根**（runbook §4.4）：
   指令按 `source_system=chengjie`（引擎枚举）下发，只删一个实例就回执=指令关单、另一实例成漏网，
   违反 PERSONA_BUS §5.3 义务。因此：
   - chengjie 执行器已支持 `--data-roots "R1;R2"`（分号分隔，或 env `CHENGJIE_DATA_ROOTS`；
     agent 对每条指令遍历全部根、**全部根成功才 ack**，detail.roots 带分根摘要；未给多根时
     回退 `--input` 单根，向后兼容）——以
     `engines/chengjie/scripts/persona_purge_agent.py` 文件现状为准（本包只读不改它）；
   - `run_purge.ps1` 的守门逻辑：多根时先读 agent 脚本现状确认含 `--data-roots`——支持则
     多根合成**一次** `--data-roots` 调用（引擎级回执）；不支持（旧版仓库/异引擎）且带
     `-Commit` 则**退出码 3 拒绝**（绝不逐根 commit——删完首根就 ack=其余根漏网），
     多根 dry-run 放行逐根演练；单根（迁移前 `-DataRoots "engines\chengjie"`）`--input` 直通；
   - 未迁双实例前 .117 建议单根装（`-DataRoots "engines\chengjie"`），迁移完成后重装任务
     换成两实例数据根。
3. **export：persona 每实例导出一份**（产物 `chengjie_personas_zhiliao.json` / `_tongyi.json`，
   导入侧按 `(source_system, source_key)` 幂等 upsert 合并）；**license outbox 每实例一份**
   （`CHENGJIE_LEDGER_OUTBOX` 分实例落盘；壳脚本按 `<数据根>\ledger_outbox\ledger_outbox.jsonl` →
   `<数据根>\ledger_outbox`（文件形态）→ `<数据根>\config\ledger_outbox.jsonl` 顺序找源，
   未接线/无签发记录时跳过不算失败）。
4. **config_snapshot：实例配置目录写保护快照守护**（`Boundless-chengjie-config_snapshot`，每 10 分钟，
   壳=`run_config_snapshot.ps1`，chengjie 缺省任务集自动含；下文引称 §3.4）。

   **用途**：多写者竞态兜底 + 误改回滚证据链。实施 18 复盘：多个写者（人工/agent/热重载写回）同时碰
   `config.local.yaml`，agent 把内存里的旧内容写回，静默覆盖了刚做的 DeepSeek 切换，直到日志巡检才发现。
   快照守护每 10 分钟对两实例 config 目录 `git add -A`，**有变更才** `commit -m "snapshot <ISO时间戳>"`
   （无变更零空提交），提交后把 `diff --stat` 摘要打进任务日志——任何配置文本变化都留痕可溯源、可单文件回滚。

   **机制与纪律**：快照仓是 config 目录内的纯本地 `.git`（首轮自动 `git init -b main` + 写 `.gitignore`），
   **无 remote、永不 push**——`config.local.yaml` 含 auth_token/api_key，快照历史绝不能离开本机数据根，
   更不能进 `D:\workspace\boundless` 这类会 push 的仓库；`.gitignore` 排除 `*.db`/`*.db-shm`/`*.db-wal`/
   `*.db.bak*`/`*.log`/`logs/`/`purged_trash/`（SQLite 与日志是运行数据；purged_trash 进了 git 历史就物理
   删不净，违背 §4 客户删除权），只快照 yaml/json/key 等配置文本；历史不清理（配置文本极小，十年也没多少）；
   守护只加 `.git`/`.gitignore`，绝不改写配置内容，对运行进程只读。

   **怎么看历史**（`<dir>` = `D:\chengjie-instances\<实例>\data\config`）：

```powershell
git -C D:\chengjie-instances\zhiliao\data\config log --oneline               # 快照清单（新→旧）
git -C D:\chengjie-instances\zhiliao\data\config diff HEAD~1                 # 最近一次快照改了什么
git -C D:\chengjie-instances\zhiliao\data\config log -p -- config.local.yaml # 单文件全变更史（含密钥，勿外传屏幕）
```

   **怎么回滚**（单文件恢复到上一快照；实例进程约 30s 热重载自动生效，无须重启）：

```powershell
git -C D:\chengjie-instances\zhiliao\data\config checkout HEAD~1 -- config.local.yaml
# 等 30s 热重载；回滚本身会被下一轮快照记录成新 commit，证据链不断
```

   ⚠ 回滚目标不确定时先 `log --oneline` + `diff <hash> -- <文件>` 锁定「好版本」，再
   `checkout <hash> -- <文件>`；绝不 `git reset --hard`（会连带其他文件一起回退，且丢工作区未快照变更）。

## 4. 安全说明（purge --commit 与 trash 回收期）

- 计划任务的 purge 带 `--commit`：**收到集团清除指令即自动删除**。三个执行器均为**软删除**——
  资产先移入引擎 trash（含 DB 行 JSON 快照与 manifest），不是物理删除：
  - avatarhub：`engines\avatarhub\secrets\purged_trash\<日期>\<角色>__purge<id>\`
  - chengjie：`<实例数据根>\config\purged_trash\<YYYYMMDD>\<人设id>\`
  - huoke：`engines\huoke\data\purged_trash\<YYYY-MM-DD>\<key>\`
- **回收期清理 trash（物理删除，客户删除权的最终兑现点）是月度人工任务，缺省不装**：
  物理删除不可逆，必须人眼核对 manifest 后执行；建议回收期 ≤30 天。月度窗口内人工跑：

```powershell
# 先看：列出 30 天前的 trash 批次（绝不脚本自动删）
Get-ChildItem engines\avatarhub\secrets\purged_trash, engines\huoke\data\purged_trash -Directory -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Select-Object FullName, LastWriteTime
# chengjie 的在各实例数据根 config\purged_trash\ 下。核对 manifest 无误后逐批次 Remove-Item -Recurse
```

- 其余护栏（执行器自带，本包不改变）：source_key 白名单防路径穿越；删除钉死引擎根内；共享资产
  （共用声线/共享国家码话术）自动跳过记 skipped；删除有失败**不回执**、下轮幂等重试；
  壳脚本不带 `-Commit` 即 dry-run 演练。
- 密钥纪律：`-IngestKey` 显式传给安装器会**嵌进任务定义**（本机管理员可读 XML），演练输出里已掩码，
  但仍推荐机器级 `setx /M EVENT_INGEST_KEY`，让任务运行时从环境读取。

## 5. export 三段式与 VPS 侧

### 5.1 三段式（引擎机侧）

`run_export.ps1`：① 导出（persona 必做；license 按引擎，见 §2 矩阵）→ ② 校验
（`validate_personas.py` / `validate_export.py`，**不过不许传输**）→ ③ 传输+导入
（`-Transfer` scp 到 VPS；`-Import` 再 ssh 远端执行导入，隐含 `-Transfer`）。
缺省只做 ①+②，产物留 `data\persona_bus_out\`；SSH 通道用 `deploy\ssh_config.boundless`
的 `vps-bd2026` 别名（要先在运行账户下布好免密钥，`ssh -F deploy\ssh_config.boundless vps-bd2026 true` 通了再开 `-ExportTransfer`）。

### 5.2 VPS 侧手工导入（引擎机只传不导时）

```bash
cd /home/ubuntu/yuntech
node scripts/ledger-import-personas.mjs /home/ubuntu/persona_inbox/avatarhub_personas.json
node scripts/ledger-import-licenses.mjs /home/ubuntu/persona_inbox/avatarhub_licenses.json
# chengjie 产物带实例后缀（chengjie_personas_zhiliao.json 等），逐个导；导入幂等，第二遍 0 新增
```

### 5.3 VPS 侧 Linux crontab 样例

```cron
# ① KPI 周报：每周一 08:10 生成上一完整 ISO 周（库缺失时输出骨架报告退出 0，可常开）
#    ——即 Windows 侧 kpi_weekly 任务（run_kpi_weekly.ps1）的 VPS 等价命令；生产报表以本条为准，
#      Windows 侧 -WithKpiWeekly 仅供 website 开发机（现状 .117）自查（§2.1）
10 8 * * 1 cd /home/ubuntu/yuntech && mkdir -p /home/ubuntu/reports/kpi && /usr/bin/node scripts/kpi-weekly-report.mjs --week last --format md --out /home/ubuntu/reports/kpi/last-week.md >> /home/ubuntu/kpi-weekly.log 2>&1

# ② 账本备份：每日 03:50（website/scripts/ledger-backup.mjs，同事本轮交付：SQLite Online Backup
#    一致性快照 + integrity_check + 轮转，缺省保留 14 天/至少 5 份；手册 website/docs/BACKUP.md）
50 3 * * * cd /home/ubuntu/yuntech && /usr/bin/node scripts/ledger-backup.mjs --out-summary >> /home/ubuntu/ledger-backup.log 2>&1

# ③ 订单 SLA 扫描：已有 cron（website/scripts/setup_order_sla_cron.sh 装的每 10 分钟 curl），保持不动。

# ③b 卡支付双重对账兜底：每日 04:10 拉 Stripe 最近 25h 已付 session 与订单库比对，
#     webhook 停摆窗口漏单自动补账（幂等；未配 STRIPE_SECRET_KEY 时返回 not_configured 无害）。
#     <ADMIN_KEY> 用 ~/yuntech/.env.local 的 ADMIN_KEY（website/_setup_cron.sh 会自动注册本条）
10 4 * * * curl -fsS -m 120 "http://127.0.0.1:3000/api/admin/stripe-reconcile?key=<ADMIN_KEY>&hours=25" >> /home/ubuntu/stripe-reconcile.log 2>&1

# ④ 台账导入（按需/每日，推荐先手工导人眼过校验；要自动化再放开下两行——导入幂等）：
# 20 4 * * * cd /home/ubuntu/yuntech && for f in /home/ubuntu/persona_inbox/*_personas*.json; do [ -e "$f" ] && node scripts/ledger-import-personas.mjs "$f"; done >> /home/ubuntu/ledger-import.log 2>&1
# 25 4 * * * cd /home/ubuntu/yuntech && for f in /home/ubuntu/persona_inbox/*_licenses*.json; do [ -e "$f" ] && node scripts/ledger-import-licenses.mjs "$f"; done >> /home/ubuntu/ledger-import.log 2>&1

# ⑤ Stripe 对账巡检：每日 04:10 拉最近 25h 已支付 Checkout Session 比对订单库——双重对账兜底：
#    webhook 停摆窗口漏单自动补账（幂等；未配 STRIPE_SECRET_KEY 返回 not_configured 无害，可常开；
#    本条由 website/_setup_cron.sh 自动注册，key 取 .env.local 的 ADMIN_KEY、回退 TELEGRAM_SETUP_KEY）
10 4 * * * curl -fsS -m 120 "http://127.0.0.1:3000/api/admin/stripe-reconcile?key=<ADMIN_KEY>&hours=25" >> /home/ubuntu/stripe-reconcile.log 2>&1
```

### 5.4 grant 缓存同步（v2 已接线：grants_sync 任务）

C4 已交付 `tools/persona_bus/fetch_grants.py`（`GET <base>/api/sync/personas/grants?system=<engine>`，
Bearer=`EVENT_INGEST_KEY`），v2 起 **grants_sync 纳入引擎机缺省任务集**（每 30 分钟，
`Boundless-<engine>-grants_sync`，壳=`run_grants_sync.ps1`）：把集团侧授权清单原子写到
`data\persona_bus_out\<engine>_grants.json`，供 `platform\identity\grant_gate.py` 运行时软门控只读命中
（缺省 warn 不挡业务；引擎进程经 `PERSONA_GRANT_CACHE` 指向缓存文件，`PERSONA_GRANT_ENFORCE=1` 才强制）。

```powershell
# 人工排障（缺 EVENT_INGEST_KEY 时明确报错退出码 2，绝不静默空跑）：
powershell -ExecutionPolicy Bypass -File deploy\cron\run_grants_sync.ps1 -Engine avatarhub
```

纪律不变：fetch **独立于** `run_export.ps1`（导出失败不应挡住缓存刷新节奏，反之亦然）；
拉取失败（网络/5xx）退出码 1 可重试，**旧缓存继续供门控离线使用**——门控缺省 warn 放行，断更不挡业务。

## 6. 安装 / 卸载 / 巡检（Windows 侧用法）

### 6.1 安装器 `install_tasks.ps1`

```powershell
cd D:\workspace\boundless

# ① 演练（缺省 WhatIf）：打印任务定义（文件夹/触发器/账户/工作目录/日志/完整动作命令行 + 环境体检），不注册
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine avatarhub                 # .176
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine chengjie -WithKpiWeekly   # .117（缺省双实例；website 开发机加装 KPI 周报）
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine huoke -Tasks uploader,export

# ② 演练输出核对无误后，管理员 PowerShell 真注册（-Execute 由各机运维执行）
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine huoke -Execute
```

| 参数 | 说明 |
|---|---|
| `-Engine avatarhub\|chengjie\|huoke` | 必填，按机器选（.176=avatarhub .117=chengjie .198=huoke） |
| `-Tasks uploader,purge,export,grants_sync` | 多选，缺省全装四类；`-Engine chengjie` 未显式 `-Tasks` 时自动追加 config_snapshot（§3.4；avatarhub/huoke 显式传入也跳过）；kpi_weekly 不在缺省，可显式列入或用 `-WithKpiWeekly` |
| `-WithKpiWeekly` | 追加 KPI 周报任务 `Boundless-website-kpi_weekly`（website 所在 Windows 机可选装，§2.1；VPS 用 §5.3 ① crontab） |
| `-BaseUrl` | 集团基址覆盖（缺省壳脚本走 env `PERSONA_SYNC_BASE` / `https://bd2026.cc`） |
| `-IngestKey` | 显式嵌密钥进任务定义（不推荐，见 §4；缺省运行时读机器级 `EVENT_INGEST_KEY`） |
| `-SpoolDirs` / `-DataRoots` / `-ConfigDirs` | 覆盖缺省 spool / 数据根 / 快照配置目录清单（`-File` 语义下多值用逗号串成一个字符串；`-ConfigDirs` 缺省双实例生产 config 目录 `D:\chengjie-instances\<实例>\data\config`） |
| `-RunAs SYSTEM\|CurrentUser` | 运行账户，缺省 SYSTEM（ServiceAccount/Highest）；CurrentUser 走 S4U 不存密码。所选账户必须能找到 python（SYSTEM 看机器级 PATH；python 装在用户目录就用 `-RunAs CurrentUser` 或 `-PythonExe` 全路径） |
| `-PythonExe` / `-NodeExe` / `-GitExe` | 传给壳脚本的 python / node / git 全路径（node 仅 kpi_weekly 壳用；git 仅 config_snapshot 壳用） |
| `-UploaderEveryMinutes` / `-PurgeEveryMinutes` / `-GrantsSyncEveryMinutes` / `-ConfigSnapshotEveryMinutes` / `-ExportDailyAt` / `-KpiWeeklyAt` | 节律覆盖，缺省 5 / 10 / 30 / 10 / 03:30 / 09:00（kpi 固定周一，只调时刻） |
| `-ExportTransfer` | export 任务带第 3 段（`-Transfer -Import`；先打通 §5.1 的 SSH） |
| `-Execute` | 真注册。缺省=演练只打印 |

统一规格：任务夹 `\Boundless\`；动作 = `cmd.exe /d /c <mkdir 日志目录> & powershell -File <壳> … >> logs\cron\<任务名>.log 2>&1`；
工作目录=仓库根；`MultipleInstances=IgnoreNew` + `StartWhenAvailable` + 时限 1h；
触发器 uploader/purge/grants_sync/config_snapshot 用 `-Once` 起点 + Repetition（持续 3650 天），
export 用 `-Daily`，kpi_weekly 用 `-Weekly Monday`。

### 6.2 卸载与巡检

```powershell
# 卸载：同样缺省演练；只认 \Boundless\ 下 Boundless-* 前缀，绝不碰引擎自启等其他任务
powershell -ExecutionPolicy Bypass -File deploy\cron\uninstall_tasks.ps1 -Engine chengjie            # 演练
powershell -ExecutionPolicy Bypass -File deploy\cron\uninstall_tasks.ps1 -Engine chengjie -Execute   # 真卸（管理员）

# 巡检：现状 + 上次运行结果（Get-ScheduledTaskInfo）；无任务时优雅输出「未安装」
powershell -ExecutionPolicy Bypass -File deploy\cron\list_tasks.ps1
```

### 6.3 壳脚本手工跑（排障）

```powershell
powershell -ExecutionPolicy Bypass -File deploy\cron\run_uploader.ps1 -SpoolDir engines\avatarhub\data\events\spool -DryRun
powershell -ExecutionPolicy Bypass -File deploy\cron\run_purge.ps1 -Engine huoke                      # dry-run 演练
powershell -ExecutionPolicy Bypass -File deploy\cron\run_export.ps1 -Engine avatarhub -Kinds persona  # 导出+校验
powershell -ExecutionPolicy Bypass -File deploy\cron\run_grants_sync.ps1 -Engine chengjie             # 拉 grant 缓存（缺密钥退出码 2）
powershell -ExecutionPolicy Bypass -File deploy\cron\run_kpi_weekly.ps1 -Week this                    # 本周至今的 KPI 报告
powershell -ExecutionPolicy Bypass -File deploy\cron\run_config_snapshot.ps1                          # 双实例 config 快照一轮（无变更退出 0 零空提交）
```

## 7. 上线核对单（每台机装完过一遍）

- [ ] `install_tasks.ps1 -Engine <engine>`（不带 `-Execute`）演练输出与 §2/§2.1 矩阵一致，[检查] 行无意外；
- [ ] 机器级密钥已配：`[Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY','Machine')` 非空；
- [ ] 管理员 `-Execute` 实装 → `list_tasks.ps1` 任务齐全、状态 Ready；
- [ ] 手动触发一次 uploader（`Start-ScheduledTask -TaskPath '\Boundless\' -TaskName 'Boundless-<engine>-uploader'`），
      `logs\cron\` 出日志且 `list_tasks.ps1` 显示上次运行=成功；
- [ ] purge 先手工 dry-run（`run_purge.ps1 -Engine <engine>`）看将删清单，再放行 `-Commit` 任务；
- [ ] grants_sync 手动跑一次（`run_grants_sync.ps1 -Engine <engine>`）：`data\persona_bus_out\<engine>_grants.json`
      出现且 `fetched_at` 是刚刚——缺密钥时应见退出码 2 + 明确报错，配好再装；
- [ ] 装 kpi_weekly 的机器（§2.1，现状 .117 可选）：`run_kpi_weekly.ps1 -Week this` 手动跑一次，
      `deploy\cron\logs\reports\` 出 `kpi_weekly_*.md`；报告若是「暂无数据」骨架，先核 `EVENTS_DB`/`LEDGER_DB`；
- [ ] .117：两个 uploader 的 spool 指向不同实例；purge 多根守门行为已知（§3.2）；
- [ ] .117：config_snapshot 手动跑一次（`run_config_snapshot.ps1`）：两实例 config 目录出 `.git`（首轮各一个
      初始 commit），`git -C D:\chengjie-instances\zhiliao\data\config ls-files` 只见配置文本、无 `*.db`（§3.4）；
- [ ] export 产物在 `data\persona_bus_out\` 且校验 OK；确认不入 git（`data/` 已忽略）；
      开 `-ExportTransfer` 前先验 `ssh -F deploy\ssh_config.boundless vps-bd2026 true`。

## 8. 退出码与故障排查

壳脚本统一退出码（计划任务 LastTaskResult 直接可见，`list_tasks.ps1` 已翻译）：

| 退出码 | 含义 | 处置 |
|---|---|---|
| 0 | 成功（含「spool 尚不存在/无新数据」「无待办指令」「KPI 库缺失出骨架报告」等正常空转） | — |
| 1 | 业务失败：上传中断（偏移已保留自动续传）/ purge 有指令未回执（下轮幂等重试）/ 导出某步失败 / grants 拉取失败（旧缓存仍供门控离线用）/ KPI 生成失败 / 快照某目录 git 操作失败（其余目录不受影响，下轮幂等重试） | 看 `logs\cron\<任务名>.log` 尾部；连续失败再人工介入 |
| 2 | 配置错误：缺 `EVENT_INGEST_KEY` / 缺 `-SpoolDir` / 数据根或配置目录不存在 / python、node 或 git 不可用 / 底层脚本缺失 | 按日志首行提示修配置；不修不会自愈 |
| 3 | 多根 + `-Commit` 守门拒绝：执行器不含 `--data-roots`（旧版检出未 pull，或给 avatarhub/huoke 误传多根） | `git pull` 取到多根版执行器，或改单根（§3.2） |
| 0x41303 / 0x41301 | 尚未运行过 / 正在运行（系统状态码，非失败） | — |
| 0x800710E0 | 上一轮还在跑，本轮被 IgnoreNew 跳过 | 常见于首轮大积压，观察即可 |

常见故障：`HTTP 401` = 密钥错（换对 `EVENT_INGEST_KEY`）；`HTTP 503` = 服务端未配置收集器；
SYSTEM 账户找不到 python = 用 `-PythonExe` 全路径或 `-RunAs CurrentUser` 重装任务；
kpi_weekly 报 `ERR_MODULE_NOT_FOUND` = website 依赖未装（`cd website && npm install`，需 better-sqlite3）。

---

## 附录 · 接线包自检记（可提交前）

- WhatIf 缺省、`-Execute` 才注册；矩阵与 `deploy/machines.json` 五机角色一致（.176 avatarhub / .117 chengjie 双实例 / .198 huoke；.104/.140 算力节点默认不装）。
- `logs\cron\`、`data\persona_bus_out\`、`deploy\cron\logs\reports\` 分别落在根 `.gitignore` 的 `logs/`、`data/` 下，不入 git。
- 修复：`install_tasks.ps1` 多根 WhatIf 体检在执行器文件缺失时不再因 `ReadAllText` 抛错中断演练（先 `Test-Path`）。
- config_snapshot 自检（实施 18 复盘接线）：仅 chengjie（未显式 `-Tasks` 自动入其任务集，avatarhub/huoke 显式传入
  也警告跳过）；快照仓 = config 目录内纯本地 `.git`（无 remote），`.gitignore` 先于首次 add 写入（db/wal/shm/
  日志/purged_trash 永不进索引）；无变更零空提交退出 0；壳只吃 `-ConfigDirs`/`-GitExe`，不透传密钥/python；
  SYSTEM 账户跨属主操作用命令行 `-c safe.directory=<dir>` 豁免（protected config，不落盘全局配置）。
- v2 自检：grants_sync 入引擎机缺省任务集（每 30 分钟）、kpi_weekly 走 `-WithKpiWeekly` 独立开关（每周一 09:00，
  任务名 `Boundless-website-kpi_weekly`）；两壳缺 env/工具时明确报错退非 0（grants 缺密钥=2、拉取失败=1，
  kpi 缺 node/脚本=2），绝不静默；`fetch_grants.py` / `kpi-weekly-report.mjs` 本包只读不改。
  已知口径差：kpi 生成器文件头写「缺省 DATA_DIR 下同名库」，代码实际读 env `LEADS_DIR`
  （`website/scripts/ledger-lib.mjs` 的 `resolveDataDir`，缺省 `~\hualing-leads`）——以代码为准，本包 README 按实际 env 名写。
