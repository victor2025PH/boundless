# 集团库备份与恢复手册（group-ledger / group-events）

> 对象：`group-ledger.db`（客户 / 订单 / 授权 / 人设 / 渠道账号 / 控制台账号——**钱权皇冠数据**）
> 与 `group-events.db`（全域事件流）。这两库是集团控制台的真相源，丢失=客户与交易历史归零。
> 工具：`website/scripts/ledger-backup.mjs`（备份）+ `website/scripts/ledger-restore-verify.mjs`（恢复演练）。

---

## 1. 为什么不能用 cp

两库常年 WAL 模式，最新写入躺在 `-wal` 里未 checkpoint。直接 `cp` 主文件会丢数据、甚至拷出
撕裂页。备份必须走 SQLite **Online Backup API**（`db.backup()`）——源库带并发写入也能产出一致
快照，无需停服、无需碰 `-wal/-shm`。产物转 `journal_mode=DELETE` 单文件，异地拷贝自洽。

## 2. 备份工具用法

```bash
node scripts/ledger-backup.mjs --dir <备份目录> --keep-days 30 --keep-min 10 [--ledger-only|--events-only] [--out-summary]
```

- 路径解析：账本=`LEDGER_DB` 或 `DATA_DIR/group-ledger.db`；事件=`EVENTS_DB` 或 `DATA_DIR/group-events.db`；
  备份目录=`--dir` > `LEDGER_BACKUP_DIR` > `DATA_DIR/backups`。
- 每份备份写同名 `<备份>.meta.json`（源路径/大小/sha256/integrity/全表计数快照），供恢复演练比对。
- 备份后**立即对备份文件本体**跑 `integrity_check`——不过则删备份、退出 1（cron 接告警）。
- 轮转：删超 `--keep-days` 的旧份，但每库至少保留最新 `--keep-min` 份（防误配清空）；备份失败/
  跳过时**绝不删旧份**（旧份此刻就是全部身家）。

## 3. 生产部署现状（2026-07-19 实施24）

| 机器 | 动作 | 节律 |
|---|---|---|
| VPS bd2026 | `scripts/ledger-backup-cron.sh`（包装 ledger-backup.mjs，失败即 Telegram 告警） | 每日 03:15 |
| .117（本地） | `D:\ledger-backups\pull_from_vps.ps1` scp 拉 VPS 备份到 `D:\ledger-backups\from-vps`（计划任务） | 每日 04:30 |

- **两机地理冗余**：VPS 本机备份 + .117 异地副本，单盘/单机损毁不致数据全失。
- 日志：VPS `~/ledger-backup.log`；.117 `D:\ledger-backups\pull.log`。
- **失败告警（实施25）**：`ledger-backup-cron.sh` 备份失败（退出码非 0）即经
  `scripts/notify-telegram.sh` 发 Telegram 给绑定管理员（复用 health-watchdog 同款
  token/收件人：`.env.local` 的 `TELEGRAM_BOT_TOKEN`/`ALERT_CHAT_ID` ∪ `admin_chats.json`）；
  落 `~/.ledger-backup.down` 幂等标志，下次成功补发「已恢复」并清标志。成功静默不刷屏。
- **最后一层交人工**：季度把 `D:\ledger-backups\from-vps` 复制到离线介质（U 盘/加密云盘）。

## 4. 恢复步骤（真出事时照做）

```bash
node scripts/ledger-restore-verify.mjs <备份文件.db>   # ① 先验证这份备份可用
pm2 stop yuntech                                        # ② 停服
cd <DATA_DIR>
ts=$(date +%Y%m%d-%H%M%S)
mv group-ledger.db group-ledger.db.broken-$ts
[ -f group-ledger.db-wal ] && mv group-ledger.db-wal group-ledger.db-wal.broken-$ts
[ -f group-ledger.db-shm ] && mv group-ledger.db-shm group-ledger.db-shm.broken-$ts
cp <备份文件.db> group-ledger.db                        # ③ 备份转正（勿留旧 -wal！）
pm2 start yuntech && curl -sf http://127.0.0.1:3000/api/health
```

⚠ 备份是 `journal_mode=DELETE` 单文件，**不要**把旧 `-wal/-shm` 留在原地——SQLite 会当新库
日志回放，直接写坏刚恢复的数据。
回滚：恢复后异常 → `pm2 stop` → 把 `.broken-$ts` 三件套挪回原名 → `pm2 start`。

## 5. 恢复演练节律（备份不演练等于没备份）

**每月**对最新备份跑一次 `ledger-restore-verify.mjs`：拷临时目录 → sha256 与 meta 比对 →
`integrity_check` → 表计数与 meta 快照比对 → 抽样查询最新订单/授权/事件可读 → 结论。
退出码 0=可用于恢复。演练只读备份、绝不碰生产库。首次演练：2026-07-19 通过
（group-ledger 14 表，customers=2 orders=9 licenses=6 personas=32）。

## 6. 相关

- `scripts/ledger-backup.mjs` / `scripts/ledger-restore-verify.mjs`
- `lib/ledger.ts`（schema 单一源）/ `lib/events-db.ts`
- 老 JSON 备份（`leads-backup.sh`）是订单/留资 JSON 主真相源的另一条线，与本手册互补不重叠。
