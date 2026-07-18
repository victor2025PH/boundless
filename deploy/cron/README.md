# deploy/cron/ · 引擎机定时任务接线包（P4/P5 运营手册）

> 让三个周期性动作——**事件补传 uploader**（P4）、**人设清除 purge agent**（P5）、
> **人设/授权台账导出 export**（P4/P5）——在五台 Windows 引擎机上以计划任务方式标准化落地。
> 机器台账单一源：`deploy/machines.json`；双实例契约：`deploy/instances/README.md` §4/§7 与
> `deploy/instances/migrate_117_runbook.md` §4。
>
> **铁律**：本目录的安装/卸载脚本**缺省 WhatIf 演练**——只打印将注册/卸载的任务定义，绝不落地；
> `-Execute` 才真装/真卸，由各机运维在**管理员 PowerShell** 里执行。仓库侧绝不预注册任何计划任务。

---

## 1. 环境变量与密钥

| 变量 | 级别 | 谁来设 | 说明 |
|---|---|---|---|
| `EVENT_INGEST_KEY` | **机器级**（一台厂商机一把 M2M 密钥） | 运维：`setx /M EVENT_INGEST_KEY <密钥>`（管理员），或壳脚本临时 `-IngestKey` | uploader `--key` 与 purge `--key` 同一把（与 `/api/collect` 共用）。**绝不入 git、缺省不进任务定义**（任务 XML 本机管理员可读）；未配置时任务以退出码 2 失败并写明原因 |
| `PERSONA_SYNC_BASE` | 机器级（可选） | 可不设 | purge 执行器基址回退链：`--base` > env > `https://bd2026.cc`；安装器 `-BaseUrl` 显式覆盖 |
| `EVENT_SPOOL_DIR` | 实例/引擎进程（**发射侧**） | chengjie 由 `start_zhiliao/tongyi.ps1` 注入；avatarhub/huoke 引擎缺省自带 | cron 侧**不依赖它**：uploader 一律显式 `--spool-dir`，防机器级残留串目录 |
| `CHENGJIE_PRODUCT_ID` / `AITR_DATA_DIR` / `CHENGJIE_LEDGER_OUTBOX` / `LICENSE_KEY` | 实例进程 | 双实例 start 脚本每次启动注入 | 与 cron 无关，**不要设成机器级**（双实例串味源，runbook §1.6）；列在此仅防误配 |
| chengjie 实例数据根清单 | CLI 参数（非环境变量） | 安装器/壳脚本 `-DataRoots`（`-File` 语义下多值用逗号串） | 缺省 `deploy\instances\zhiliao\data` + `deploy\instances\tongyi\data`；迁移前单实例期传 `"engines\chengjie"` |

日志落 `logs\cron\<任务名>.log`（根 `.gitignore` 已忽略 `logs/`，本包不动它）；导出产物落
`data\persona_bus_out\`（`data/` 同样已忽略；含显示名/指纹/客户名，属经营数据，勿外传）。

## 2. 任务矩阵（机器 × 任务）

三类任务统一口径：

| 任务 | 节律 | 底层脚本与参数 | 包装壳 |
|---|---|---|---|
| **uploader** | 每 5 分钟 | `platform/observability/uploader.py --spool-dir <spool> --endpoint <base>/api/collect --batch 200`（`--key` 经环境变量；断点续传、幂等重发） | `run_uploader.ps1` |
| **purge** | 每 10 分钟 | `engines/<engine>/…/persona_purge_agent.py --once --commit --input <根>`（`--base`/`--key` 同上；**软删进 trash**，见 §4） | `run_purge.ps1` |
| **export** | 每日 03:30 | 三段式：`tools/persona_bus/export_*.py` 导出 → `validate_personas.py` / `validate_export.py` 校验 → scp/ssh 传输导入（§5）；license：avatarhub=`tools/license_ledger/export_avatarhub.py`，chengjie=`engines/chengjie/scripts/ledger_outbox.py --export`，huoke=无（自动跳过） | `run_export.ps1` |

分机矩阵（任务名统一 `\Boundless\Boundless-<engine>-<task>[-<实例>]`）：

| 机器 | 引擎 | uploader（每 5 分钟） | purge（每 10 分钟，--once --commit） | export（每日三段式） | 所需环境变量 |
|---|---|---|---|---|---|
| **.176 幻声** huansheng | avatarhub | 1 个：spool 缺省 `engines\avatarhub\data\events\spool` | `engines/avatarhub/persona_purge_agent.py`；trash=`engines\avatarhub\secrets\purged_trash\` | persona=`export_avatarhub_personas.py`；license=`export_avatarhub.py`（secrets/orders.json、trials.json、license.key） | `EVENT_INGEST_KEY`（机器级） |
| **.117 通译** tongyi（chengjie **双实例**） | chengjie | **每实例 1 个**：`Boundless-chengjie-uploader-zhiliao` / `-tongyi`，spool=`<实例数据根>\events\spool`；**勿并发同一 spool**（§3） | **每引擎 1 份**，一轮覆盖两实例数据根（`--data-roots "R1;R2"`，agent 已支持，§3）；trash=各根 `config\purged_trash\` | persona 每实例一份（`--input <数据根>` → `chengjie_personas_<实例>.json`）；license=每实例 outbox 一份（`ledger_outbox.py --export`，未接线自动跳过） | `EVENT_INGEST_KEY`；实例数据根经 `-DataRoots` 传参（`CHENGJIE_PRODUCT_ID` 等实例变量与 cron 无关） |
| **.198 智拓** zhituo | huoke | 1 个：spool 缺省 `engines\huoke\data\events\spool` | `engines/huoke/src/persona_purge_agent.py`；trash=`engines\huoke\data\purged_trash\` | persona=`export_huoke_personas.py`；license 无数据源自动跳过 | `EVENT_INGEST_KEY` |
| **.104 幻颜节点 / .140 通传节点** | avatarhub（算力节点） | **默认不装**：算力节点只跑推理服务，不产事件 spool；日后本机落 spool 再单装 uploader | 不装（人设资产在 .176 主机） | 不装 | — |
| **VPS 官网机** | website | —（服务端收集器） | —（注册表在服务端） | 导入侧 `ledger-import-personas.mjs` / `ledger-import-licenses.mjs`；KPI 周报/账本备份/订单 SLA 走 Linux crontab（§5.3） | 服务端 `.env`（`EVENT_INGEST_KEY` 等，非本包职责） |

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
10 8 * * 1 cd /home/ubuntu/yuntech && mkdir -p /home/ubuntu/reports/kpi && /usr/bin/node scripts/kpi-weekly-report.mjs --week last --format md --out /home/ubuntu/reports/kpi/last-week.md >> /home/ubuntu/kpi-weekly.log 2>&1

# ② 账本备份：每日 03:50（website/scripts/ledger-backup.mjs，同事本轮交付：SQLite Online Backup
#    一致性快照 + integrity_check + 轮转，缺省保留 14 天/至少 5 份；手册 website/docs/BACKUP.md）
50 3 * * * cd /home/ubuntu/yuntech && /usr/bin/node scripts/ledger-backup.mjs --out-summary >> /home/ubuntu/ledger-backup.log 2>&1

# ③ 订单 SLA 扫描：已有 cron（website/scripts/setup_order_sla_cron.sh 装的每 10 分钟 curl），保持不动。

# ④ 台账导入（按需/每日，推荐先手工导人眼过校验；要自动化再放开下两行——导入幂等）：
# 20 4 * * * cd /home/ubuntu/yuntech && for f in /home/ubuntu/persona_inbox/*_personas*.json; do [ -e "$f" ] && node scripts/ledger-import-personas.mjs "$f"; done >> /home/ubuntu/ledger-import.log 2>&1
# 25 4 * * * cd /home/ubuntu/yuntech && for f in /home/ubuntu/persona_inbox/*_licenses*.json; do [ -e "$f" ] && node scripts/ledger-import-licenses.mjs "$f"; done >> /home/ubuntu/ledger-import.log 2>&1
```

### 5.4 可选：grant 缓存刷新

export 三段式（§5.1）成功后，可另挂一条本机定时任务，把集团侧授权/grant 缓存拉到引擎机本地（供运行时只读命中，与 uploader/purge/export **矩阵无关**，本包 `install_tasks.ps1` **不自动注册**）：

```powershell
# 示例（参数以 C4 交付脚本的 --help 为准；建议日志同样落 logs\cron\）
python tools/persona_bus/fetch_grants.py --out data\persona_bus_out\grants_cache.json
# 或：在 export 成功钩子/独立日计划里跑，失败不影响已校验的 export 产物
```

**当前仓库尚无 `tools/persona_bus/fetch_grants.py`——待 C4 交付后启用。** 交付后把上述命令（或同事文档里的正式参数）写进各机独立计划任务即可；勿把 fetch 塞进 `run_export.ps1`（导出失败不应挡住缓存刷新节奏，反之亦然）。

## 6. 安装 / 卸载 / 巡检（Windows 侧用法）

### 6.1 安装器 `install_tasks.ps1`

```powershell
cd D:\workspace\boundless

# ① 演练（缺省 WhatIf）：打印任务定义（文件夹/触发器/账户/工作目录/日志/完整动作命令行 + 环境体检），不注册
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine avatarhub                 # .176
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine chengjie                  # .117（缺省双实例）
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine huoke -Tasks uploader,export

# ② 演练输出核对无误后，管理员 PowerShell 真注册（-Execute 由各机运维执行）
powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine huoke -Execute
```

| 参数 | 说明 |
|---|---|
| `-Engine avatarhub\|chengjie\|huoke` | 必填，按机器选（.176=avatarhub .117=chengjie .198=huoke） |
| `-Tasks uploader,purge,export` | 多选，缺省全装三类 |
| `-BaseUrl` | 集团基址覆盖（缺省壳脚本走 env `PERSONA_SYNC_BASE` / `https://bd2026.cc`） |
| `-IngestKey` | 显式嵌密钥进任务定义（不推荐，见 §4；缺省运行时读机器级 `EVENT_INGEST_KEY`） |
| `-SpoolDirs` / `-DataRoots` | 覆盖缺省 spool / 数据根清单（`-File` 语义下多值用逗号串成一个字符串） |
| `-RunAs SYSTEM\|CurrentUser` | 运行账户，缺省 SYSTEM（ServiceAccount/Highest）；CurrentUser 走 S4U 不存密码。所选账户必须能找到 python（SYSTEM 看机器级 PATH；python 装在用户目录就用 `-RunAs CurrentUser` 或 `-PythonExe` 全路径） |
| `-PythonExe` | 传给壳脚本的 python 全路径 |
| `-UploaderEveryMinutes` / `-PurgeEveryMinutes` / `-ExportDailyAt` | 节律覆盖，缺省 5 / 10 / 03:30 |
| `-ExportTransfer` | export 任务带第 3 段（`-Transfer -Import`；先打通 §5.1 的 SSH） |
| `-Execute` | 真注册。缺省=演练只打印 |

统一规格：任务夹 `\Boundless\`；动作 = `cmd.exe /d /c <mkdir 日志目录> & powershell -File <壳> … >> logs\cron\<任务名>.log 2>&1`；
工作目录=仓库根；`MultipleInstances=IgnoreNew` + `StartWhenAvailable` + 时限 1h；
触发器 uploader/purge 用 `-Once` 起点 + Repetition（持续 3650 天），export 用 `-Daily`。

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
```

## 7. 上线核对单（每台机装完过一遍）

- [ ] `install_tasks.ps1 -Engine <engine>`（不带 `-Execute`）演练输出与 §2 矩阵一致，[检查] 行无意外；
- [ ] 机器级密钥已配：`[Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY','Machine')` 非空；
- [ ] 管理员 `-Execute` 实装 → `list_tasks.ps1` 任务齐全、状态 Ready；
- [ ] 手动触发一次 uploader（`Start-ScheduledTask -TaskPath '\Boundless\' -TaskName 'Boundless-<engine>-uploader'`），
      `logs\cron\` 出日志且 `list_tasks.ps1` 显示上次运行=成功；
- [ ] purge 先手工 dry-run（`run_purge.ps1 -Engine <engine>`）看将删清单，再放行 `-Commit` 任务；
- [ ] .117：两个 uploader 的 spool 指向不同实例；purge 多根守门行为已知（§3.2）；
- [ ] export 产物在 `data\persona_bus_out\` 且校验 OK；确认不入 git（`data/` 已忽略）；
      开 `-ExportTransfer` 前先验 `ssh -F deploy\ssh_config.boundless vps-bd2026 true`。

## 8. 退出码与故障排查

壳脚本统一退出码（计划任务 LastTaskResult 直接可见，`list_tasks.ps1` 已翻译）：

| 退出码 | 含义 | 处置 |
|---|---|---|
| 0 | 成功（含「spool 尚不存在/无新数据」「无待办指令」等正常空转） | — |
| 1 | 业务失败：上传中断（偏移已保留自动续传）/ purge 有指令未回执（下轮幂等重试）/ 导出某步失败 | 看 `logs\cron\<任务名>.log` 尾部；连续失败再人工介入 |
| 2 | 配置错误：缺 `EVENT_INGEST_KEY` / 缺 `-SpoolDir` / 数据根不存在 / python 不可用 | 按日志首行提示修配置；不修不会自愈 |
| 3 | 多根 + `-Commit` 守门拒绝：执行器不含 `--data-roots`（旧版检出未 pull，或给 avatarhub/huoke 误传多根） | `git pull` 取到多根版执行器，或改单根（§3.2） |
| 0x41303 / 0x41301 | 尚未运行过 / 正在运行（系统状态码，非失败） | — |
| 0x800710E0 | 上一轮还在跑，本轮被 IgnoreNew 跳过 | 常见于首轮大积压，观察即可 |

常见故障：`HTTP 401` = 密钥错（换对 `EVENT_INGEST_KEY`）；`HTTP 503` = 服务端未配置收集器；
SYSTEM 账户找不到 python = 用 `-PythonExe` 全路径或 `-RunAs CurrentUser` 重装任务。

---

## 附录 · 接线包自检记（可提交前）

- WhatIf 缺省、`-Execute` 才注册；矩阵与 `deploy/machines.json` 五机角色一致（.176 avatarhub / .117 chengjie 双实例 / .198 huoke；.104/.140 算力节点默认不装）。
- `logs\cron\`、`data\persona_bus_out\` 分别落在根 `.gitignore` 的 `logs/`、`data/` 下，不入 git。
- 修复：`install_tasks.ps1` 多根 WhatIf 体检在执行器文件缺失时不再因 `ReadAllText` 抛错中断演练（先 `Test-Path`）。
