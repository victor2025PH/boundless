# 集团库备份与恢复演练（Backup & Restore Drill）

对象：`group-ledger.db`（客户/订单/授权/人设/控制台账号——钱权数据）与 `group-events.db`（全域运营事件流）。P0 时代订单还躺在 JSON 文件里；现在账本成了离钱最近的单点，本文件是它的最后一道防线：**每日备份、备份即校验、轮转有保底、每月演练恢复**。

工具：`scripts/ledger-backup.mjs`（备份 + 校验 + 轮转）、`scripts/ledger-restore-verify.mjs`（恢复演练）。本文档独立于 `docs/LEDGER.md`（那边讲数据层/回填/迁移，这里只讲备份纪律）。

## 1. 为什么不能 cp / tar 了事

两库常年 WAL 模式（`lib/ledger.ts` / `lib/events-db.ts` 打开即 `journal_mode = WAL`）：最新已提交的写入可能还在 `-wal` 文件里、尚未合并进主 `.db`。直接 `cp group-ledger.db` 或把目录 `tar` 走：

- 丢掉 `-wal` 里的最新订单/授权；
- 拷贝瞬间正逢写入，可能得到撕裂页——文件打得开、`integrity_check` 报 malformed。

`scripts/leads-backup.sh` 的全目录 tar（每日 03:10）对 JSON 文件依然有效，但对两只 `.db` 只是"聊胜于无"的原始拷贝。**权威备份以 `ledger-backup.mjs` 的产物为准**：走 SQLite Online Backup API（better-sqlite3 `db.backup()`），源库带并发写入也能产出一致快照，无需停服、无需碰 `-wal/-shm`；产物随后转成 `journal_mode=DELETE` 的单文件快照，无伴生文件，拷去哪都自洽。

## 2. 工具与产物

用法（website 目录下 node 直跑，纯 JS 不经 TS 编译）：

```bash
node scripts/ledger-backup.mjs                        # 两库都备（默认目录 DATA_DIR/backups）
node scripts/ledger-backup.mjs --dir /mnt/backup/hl   # 指定目录（优先级 --dir > env LEDGER_BACKUP_DIR > 默认）
node scripts/ledger-backup.mjs --ledger-only          # 只备账本；--events-only 只备事件库
node scripts/ledger-backup.mjs --keep-days 14 --keep-min 5   # 轮转参数（即默认值）
node scripts/ledger-backup.mjs --out-summary          # 末行追加单行 JSON 摘要（cron 日志/采集用）
```

产物（每库每次两件）：

- `group-ledger-YYYYMMDD-HHmmss.db` / `group-events-YYYYMMDD-HHmmss.db` —— 一致性快照本体；
- 同名 `.meta.json` —— 源库路径、大小、sha256、`integrity_check` 结论、schema_version、全表计数快照（关键表 customers / orders / licenses / personas / users 与 events 总数都在内）。这是恢复演练的比对基准。

行为要点：

- 备份后**立即对备份文件本体**（而非源库）跑 `PRAGMA integrity_check` + 全表计数；不 ok 的产物当场删除并退出 1，绝不把坏备份留在盘上被轮转/异地同步当成好份；
- 轮转：删除超过 `--keep-days`（默认 14）的旧份（连 `.meta.json`），但每库无论多旧至少保留 `--keep-min`（默认 5）份——就算误把 keep-days 配成 0 也清不空；只轮转本轮备份成功的库族，备份失败时旧份一份不动；
- 库文件不存在 → 警告并跳过、整体退出 0（VPS 初期 events 库可能还没建，不该让 cron 天天误报）；
- 退出码：任一库**备份或校验失败 = 1**，其余 = 0。cron 拿退出码接告警（§7）。

## 3. 每日备份纪律（cron）

VPS（ubuntu 用户，app 在 `/home/ubuntu/yuntech`）`crontab -e` 样例：

```cron
# 03:05 集团库每日备份——排在 03:10 leads 全目录 tar 与 03:30 的 117 异地拉取之前，
# 这样两者当天带走的都是最新一致快照。失败追加 FAILED 行便于 grep 巡检（告警版见 §7）。
5 3 * * * cd /home/ubuntu/yuntech && /usr/bin/node scripts/ledger-backup.mjs --out-summary >> /home/ubuntu/ledger-backup.log 2>&1 || echo "$(date '+\%F \%T') ledger-backup FAILED" >> /home/ubuntu/ledger-backup.log
```

注意：脚本只读进程 env、不读 `.env.local`——若在 `.env.local` 覆盖过 `LEDGER_DB` / `EVENTS_DB` / `LEADS_DIR` / `LEDGER_BACKUP_DIR`，cron 行里要带同样的 env 前缀。

RPO：每日一备 = 最多丢 24 小时。订单峰值期可加一行中午备份（`5 13 * * *` 同命令），账本很小，成本可忽略；账本时代还有兜底红利——恢复后跑 `ledger-backfill.mjs` 能从 JSON 主真相源把订单/留资追平（见 §6 末尾）。

## 4. 备份目录选址与异地副本

选址原则：**备份不与库同盘/同挂载**，否则磁盘一死正副本一起陪葬。

- 默认 `DATA_DIR/backups`（即 `~/hualing-leads/backups`）：与库同盘，只防逻辑损坏（误删/写坏/程序 bug），是底线不是终点。好处是现有 03:30「117 拉取」（`scripts/pull_leads_to_117.ps1` 每日 scp 拉整个 hualing-leads）**自动把备份目录带去异地**，一行不用改。
- VPS 若有第二块盘/独立挂载（如 `/mnt/backup`），把 env `LEDGER_BACKUP_DIR` 指过去（`.env.example` 已留占位），并把该目录纳入 117 拉取或 rclone 清单。
- 异地一份是硬要求（3-2-1 的"1"）：当前由内网 117 机每日拉取承担；也可在 VPS 侧 `rclone sync`/`scp` 推到对象存储或内网机。二选一即可，但不能都不做——旧服务器删库的教训就是异地副本换来的。

## 5. 恢复演练节律（每月）

备份不演练等于没备份。**每月 1 日**对最新一份账本备份跑一次恢复演练（事件库每季度一次即可）：

```bash
cd /home/ubuntu/yuntech
node scripts/ledger-restore-verify.mjs "$(ls -1t ~/hualing-leads/backups/group-ledger-*.db | head -n1)"
```

脚本流程：拷到临时目录 → sha256 与 meta 比对 → 打开 → `integrity_check` → 表计数与 meta 快照比对 → 抽样读最新一条订单/授权（事件库读最新事件）→ 打印「该备份可用于恢复」结论与恢复步骤指引 → 清理临时目录。**只读源备份文件，全程不碰生产库。** 退出码 0 = 可用于恢复，1 = 不可用。

也可入 crontab 自动演练（失败告警同 §7）：

```cron
# 每月 1 日 03:40 恢复演练（对最新账本备份）
40 3 1 * * cd /home/ubuntu/yuntech && /usr/bin/node scripts/ledger-restore-verify.mjs "$(ls -1t /home/ubuntu/hualing-leads/backups/group-ledger-*.db 2>/dev/null | head -n1)" >> /home/ubuntu/ledger-restore-verify.log 2>&1 || echo "$(date '+\%F \%T') restore-verify FAILED" >> /home/ubuntu/ledger-restore-verify.log
```

## 6. 恢复实操手册（三步 + 回滚）

前提：先对要用的备份跑一遍 restore-verify（§5）拿到「可用于恢复」。以账本为例（事件库把文件名换成 `group-events.db` 同理；路径以 `.env.local` 的 `LEDGER_DB`/`EVENTS_DB` 覆盖值为准，默认在 `~/hualing-leads`）：

```bash
# ① 停服（杜绝恢复窗口内写入）
pm2 stop yuntech

# ② 换文件：旧库连同 -wal/-shm 一起挪走留现场，再放入备份
cd ~/hualing-leads
ts=$(date +%Y%m%d-%H%M%S)
mv group-ledger.db group-ledger.db.broken-$ts
[ -f group-ledger.db-wal ] && mv group-ledger.db-wal group-ledger.db-wal.broken-$ts
[ -f group-ledger.db-shm ] && mv group-ledger.db-shm group-ledger.db-shm.broken-$ts
cp ~/hualing-leads/backups/group-ledger-YYYYMMDD-HHmmss.db group-ledger.db

# ③ 起服并自检
pm2 start yuntech
curl -sf http://127.0.0.1:3000/api/health
# /console 抽查订单/授权/客户与认知一致；应用首次打开会自动切回 WAL，属正常
```

`-wal/-shm` 注意：备份是 `journal_mode=DELETE` 的单文件快照，**不需要也不能**给它配旧的 `-wal/-shm`——旧 `-wal` 留在原地会被 SQLite 当作"新库"的日志回放，直接写坏刚恢复的数据，所以 ② 必须连它们一起挪走，不能只换 `.db`。

回滚：恢复后发现更糟 → `pm2 stop yuntech` → 把 `.broken-$ts` 三件套挪回原名 → `pm2 start yuntech`，即回到恢复动作前状态。现场文件排障完再删。

恢复后补数：账本目前是影子库，JSON 仍是订单/留资主真相源——恢复完跑一遍 `node scripts/ledger-backfill.mjs` 即可把备份时点之后的订单/留资追平（授权/人设用对应 import 脚本重放最近导出）。这是影子账本阶段独有的红利，切主后就没有了，届时备份纪律只会更重要。

## 7. 监控与告警

抓手就一个：**退出码**（备份或校验失败 = 1）。接现有 Telegram 告警（与 health-watchdog 同款思路，token/chat 从 `.env.local` 读）：

```cron
5 3 * * * cd /home/ubuntu/yuntech && /usr/bin/node scripts/ledger-backup.mjs --out-summary >> /home/ubuntu/ledger-backup.log 2>&1 || (T=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env.local | cut -d= -f2- | tr -d '\r'); C=$(grep -E '^ALERT_CHAT_ID=' .env.local | cut -d= -f2- | tr -d '\r'); [ -n "$T" ] && [ -n "$C" ] && curl -s "https://api.telegram.org/bot$T/sendMessage" --data-urlencode "chat_id=$C" --data-urlencode "text=[华灵运维] ❌ 集团库备份失败，速查 ledger-backup.log" >/dev/null)
```

周巡检口径（一眼三查）：

- `tail -n 3 /home/ubuntu/ledger-backup.log` —— 末次摘要 `"ok":true`；
- `ls -lt ~/hualing-leads/backups | head` —— 最新文件名日期是今天/昨天，两库都有；
- 117 侧 `D:\web_backups` 最新快照里带 `backups/` 目录（异地副本在流动）。

## 8. 与 deploy/cron/README 的关系

本文件的 crontab 行同时会出现在 deploy/cron/README 的全站定时任务矩阵里；两处不一致时**以本文件为准源**——备份纪律的变更先改这里，再同步矩阵。
