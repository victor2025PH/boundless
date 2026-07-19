#!/usr/bin/env node
// 集团库恢复演练 CLI：验证一份 scripts/ledger-backup.mjs 产出的备份「真的能用于恢复」。
// 备份不演练等于没备份——每月对最新备份跑一次本脚本（节律见 docs/BACKUP.md §5）。
//
// 用法: node scripts/ledger-restore-verify.mjs <备份文件.db>
// 流程: 拷到临时目录 → sha256 与 .meta.json 比对 → 打开 → PRAGMA integrity_check
//       → 表计数与 meta 快照比对 → 抽样查询（最新一条订单/授权/事件可读）
//       → 打印「该备份可用于恢复」结论 + 恢复步骤指引 → 清理临时目录。
// 退出码: 0 = 可用于恢复；1 = 不可用（或参数/环境错误）。
// 安全: 只读源备份文件与其 .meta.json，所有打开/校验都在临时目录的副本上进行，
//       绝不触碰生产库文件（group-ledger.db / group-events.db 及其 -wal/-shm）。

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import Database from "better-sqlite3";
import { resolveDataDir, resolveLedgerDbPath } from "./ledger-lib.mjs";

function resolveEventsDbPath() {
  return process.env.EVENTS_DB || path.join(resolveDataDir(), "group-events.db");
}

const log = (msg) => console.log(`[restore-verify] ${msg}`);
const KEY_TABLES = ["customers", "orders", "licenses", "personas", "users", "events"];

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const h = createHash("sha256");
    fs.createReadStream(file)
      .on("data", (chunk) => h.update(chunk))
      .on("end", () => resolve(h.digest("hex")))
      .on("error", reject);
  });
}

function tableCounts(db) {
  const tables = db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    .all()
    .map((r) => r.name);
  const counts = {};
  for (const t of tables) counts[t] = db.prepare(`SELECT COUNT(*) AS c FROM "${t}"`).get().c;
  return counts;
}

/** 备份属于哪个库族：meta.kind 优先，其次文件名前缀，最后看表结构。 */
function detectKind(meta, backupName, counts) {
  if (meta?.kind === "ledger" || meta?.kind === "events") return meta.kind;
  if (/^group-ledger-/.test(backupName)) return "ledger";
  if (/^group-events-/.test(backupName)) return "events";
  if (counts && "orders" in counts) return "ledger";
  if (counts && "events" in counts) return "events";
  return "unknown";
}

/** 抽样查询：证明关键业务行在备份里真实可读。空表不算失败（新库合法），查询抛错才算。 */
function sampleRead(db, kind) {
  const lines = [];
  if (kind === "ledger") {
    const order = db
      .prepare(
        "SELECT id, source_key, status, created_at FROM orders ORDER BY COALESCE(created_at, '') DESC, source_key DESC LIMIT 1"
      )
      .get();
    lines.push(order ? `最新订单 ${order.source_key}（status=${order.status} · created_at=${order.created_at}）可读` : "orders 空表（0 行）可读");
    const lic = db
      .prepare(
        "SELECT id, source_system, source_key, status, expires_at FROM licenses ORDER BY COALESCE(issued_at, '') DESC, id DESC LIMIT 1"
      )
      .get();
    lines.push(lic ? `最新授权 ${lic.id}（${lic.source_system}/${lic.source_key} · expires_at=${lic.expires_at}）可读` : "licenses 空表（0 行）可读");
  } else if (kind === "events") {
    const ev = db
      .prepare("SELECT event_id, ts, product_id, name FROM events ORDER BY ts DESC, event_id DESC LIMIT 1")
      .get();
    lines.push(ev ? `最新事件 ${ev.event_id}（${ev.name} @ ${ev.ts}）可读` : "events 空表（0 行）可读");
  } else {
    lines.push("库族未知，跳过业务抽样（integrity 与计数校验仍有效）");
  }
  return lines;
}

function printRestoreGuide(kind, backupPath) {
  const base = kind === "events" ? "group-events.db" : "group-ledger.db";
  const resolved = kind === "events" ? resolveEventsDbPath() : resolveLedgerDbPath();
  console.log(`
── 恢复步骤指引（人工执行；本脚本绝不碰生产库文件）─────────────────
生产目标: ${base}（本机解析为 ${path.resolve(resolved)}；VPS 上以 .env.local 的
LEDGER_DB/EVENTS_DB 覆盖值为准，默认 DATA_DIR 即 ~/hualing-leads）。

① 停服（杜绝恢复窗口内写入）:
     pm2 stop yuntech
② 换文件——旧库连同 -wal/-shm 一起挪走留现场，再放入备份:
     cd ~/hualing-leads
     ts=$(date +%Y%m%d-%H%M%S)
     mv ${base} ${base}.broken-$ts
     [ -f ${base}-wal ] && mv ${base}-wal ${base}-wal.broken-$ts
     [ -f ${base}-shm ] && mv ${base}-shm ${base}-shm.broken-$ts
     cp ${JSON.stringify(backupPath)} ${base}
   注意: 备份是 journal_mode=DELETE 的单文件快照，不需要也不能配 -wal/-shm；
   若把旧 -wal 留在原地，SQLite 会把它当新库的日志回放，直接写坏刚恢复的数据。
③ 起服并自检:
     pm2 start yuntech
     curl -sf http://127.0.0.1:3000/api/health
     （/console 抽查一条订单/授权与恢复前认知一致）

回滚: 恢复后异常 → pm2 stop yuntech → 把 .broken-$ts 三件套挪回原名 → pm2 start yuntech，
即回到恢复动作前的状态。完整手册与演练节律见 website/docs/BACKUP.md。`);
}

async function main() {
  const argv = process.argv.slice(2);
  if (!argv.length || argv[0] === "--help" || argv[0] === "-h") {
    console.log("用法: node scripts/ledger-restore-verify.mjs <备份文件.db>");
    process.exit(argv.length ? 0 : 1);
  }
  if (argv.length > 1) {
    console.error(`未知参数: ${argv.slice(1).join(" ")}（只接受一个备份文件路径）`);
    process.exit(1);
  }

  const backupPath = path.resolve(argv[0]);
  if (!fs.existsSync(backupPath) || !fs.statSync(backupPath).isFile()) {
    console.error(`[restore-verify] 备份文件不存在: ${backupPath}`);
    process.exit(1);
  }
  const backupName = path.basename(backupPath);
  const backupSize = fs.statSync(backupPath).size;

  const metaPath = `${backupPath}.meta.json`;
  let meta = null;
  if (fs.existsSync(metaPath)) {
    try {
      meta = JSON.parse(fs.readFileSync(metaPath, "utf-8"));
    } catch {
      meta = null;
    }
  }

  log(`演练对象: ${backupPath}（${backupSize} 字节）`);
  log(meta ? `meta: ${metaPath}` : `meta: 缺失/不可解析（${metaPath}）——跳过 sha256 与计数比对；每份正规备份都应有 meta，请检查备份脚本`);

  const failures = [];
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ledger-restore-verify-"));
  let db = null;
  let kind = "unknown";
  try {
    // 1) 拷贝到临时目录——之后所有操作只碰这个副本
    const workCopy = path.join(tmpDir, backupName);
    fs.copyFileSync(backupPath, workCopy);
    log(`1/5 拷贝到临时目录: ${workCopy} … 通过`);

    // 2) sha256 与 meta 比对（在打开数据库之前算，保证摘要对应原始字节）
    const digest = await sha256File(workCopy);
    if (meta?.sha256) {
      if (digest === meta.sha256) {
        log(`2/5 sha256 与 meta 一致（${digest.slice(0, 16)}…）… 通过`);
      } else {
        failures.push(`sha256 不匹配: 实际 ${digest} ≠ meta ${meta.sha256}（备份文件已损坏或被改动）`);
        log(`2/5 sha256 与 meta 比对 … 失败`);
      }
    } else {
      log(`2/5 sha256 = ${digest.slice(0, 16)}…（无 meta 可比，跳过）`);
    }

    // 3) 打开 + integrity_check（损坏文件在这两步现形：打开抛错 / 返回 malformed）
    let counts = null;
    try {
      db = new Database(workCopy, { fileMustExist: true });
      const rows = db.pragma("integrity_check");
      const verdict =
        rows.length === 1 && String(rows[0].integrity_check).toLowerCase() === "ok"
          ? "ok"
          : rows.map((r) => r.integrity_check).join("; ");
      if (verdict === "ok") {
        log("3/5 打开 + PRAGMA integrity_check = ok … 通过");
      } else {
        failures.push(`integrity_check 未通过: ${verdict}`);
        log("3/5 打开 + PRAGMA integrity_check … 失败");
      }
      counts = tableCounts(db);
    } catch (err) {
      failures.push(`无法打开/校验备份副本: ${err?.message || err}`);
      log("3/5 打开 + PRAGMA integrity_check … 失败（文件无法作为 SQLite 库打开）");
    }

    kind = detectKind(meta, backupName, counts);

    // 4) 表计数与 meta 快照比对（备份时点的行数必须原样读回）
    if (counts && meta?.counts) {
      const tables = [...new Set([...Object.keys(meta.counts), ...Object.keys(counts)])].sort();
      const diffs = [];
      for (const t of tables) {
        if ((meta.counts[t] ?? "缺表") !== (counts[t] ?? "缺表")) {
          diffs.push(`${t}: meta=${meta.counts[t] ?? "缺表"} 实际=${counts[t] ?? "缺表"}`);
        }
      }
      if (!diffs.length) {
        const key = KEY_TABLES.filter((t) => t in counts)
          .map((t) => `${t}=${counts[t]}`)
          .join(" ");
        log(`4/5 表计数与 meta 快照一致（${tables.length} 张表；${key}）… 通过`);
      } else {
        failures.push(`表计数与 meta 快照不一致: ${diffs.join("; ")}`);
        log("4/5 表计数与 meta 快照比对 … 失败");
      }
    } else if (counts) {
      log(`4/5 表计数（无 meta 可比，跳过比对）: ${JSON.stringify(counts)}`);
    } else {
      log("4/5 表计数比对 … 跳过（库打不开）");
    }

    // 5) 抽样查询
    if (db && counts) {
      try {
        for (const line of sampleRead(db, kind)) log(`5/5 抽样: ${line} … 通过`);
      } catch (err) {
        failures.push(`抽样查询失败: ${err?.message || err}`);
        log("5/5 抽样查询 … 失败");
      }
    } else {
      log("5/5 抽样查询 … 跳过（库打不开）");
    }
  } finally {
    try {
      db?.close();
    } catch {
      /* 副本连接关不上也不影响结论 */
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
    log(`临时目录已清理: ${tmpDir}`);
  }

  if (failures.length) {
    console.log(`\n[restore-verify] 结论: 该备份【不可】用于恢复（exit 1）`);
    for (const f of failures) console.log(`  - ${f}`);
    console.log("  处置: 不要用这份备份恢复。取更早一份重跑本脚本；同时检查备份盘与源库健康（docs/BACKUP.md §7）。");
    process.exit(1);
  }
  console.log(`\n[restore-verify] 结论: 该备份可用于恢复（exit 0）`);
  printRestoreGuide(kind, backupPath);
  process.exit(0);
}

main().catch((err) => {
  console.error(`[restore-verify] 未预期异常: ${err?.stack || err}`);
  process.exit(1);
});
