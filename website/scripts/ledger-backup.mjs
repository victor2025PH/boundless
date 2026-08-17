#!/usr/bin/env node
// 集团库每日备份 CLI（纯 JS + better-sqlite3，node 直跑，不经 TS 编译）。
// 对象：group-ledger.db（客户/订单/授权/人设/控制台账号——钱权数据）与 group-events.db（事件流）。
//
// 为什么必须走 SQLite Online Backup API（db.backup()）而不是 cp：两库常年 WAL 模式，
// 未 checkpoint 的最新写入还躺在 -wal 里，直接 cp 主文件会丢数据、甚至拷出撕裂页；
// Online Backup API 在源库带并发写入时也能产出一致快照，无需停服、无需碰 -wal/-shm。
// 备份产物随后转成 journal_mode=DELETE 的单文件（无 -wal/-shm 伴生，拷去哪都自洽）。
//
// 用法: node scripts/ledger-backup.mjs [--dir 备份目录] [--keep-days 14] [--keep-min 5]
//                                      [--ledger-only|--events-only] [--out-summary]
// 路径解析（与 lib/ledger.ts / lib/events-db.ts 一致）：
//   账本 = env LEDGER_DB 或 DATA_DIR/group-ledger.db；事件 = env EVENTS_DB 或 DATA_DIR/group-events.db
//   备份目录 = --dir > env LEDGER_BACKUP_DIR > DATA_DIR/backups
// 每份备份写同名 <备份>.meta.json（源路径/大小/sha256/integrity/全表计数快照），
// 供 scripts/ledger-restore-verify.mjs 恢复演练比对。
// 轮转：删除超过 --keep-days 的旧备份（连 .meta.json），但每库无论多旧至少保留 --keep-min 份。
// 退出码：任一库备份或校验失败 = 1（cron 接告警）；库文件不存在 = 警告跳过、不算失败。
// 备份纪律 / cron 样例 / 目录选址 / 恢复手册见 docs/BACKUP.md。

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import Database from "better-sqlite3";
import { resolveDataDir, resolveLedgerDbPath } from "./ledger-lib.mjs";

const BASENAMES = { ledger: "group-ledger", events: "group-events" };

/** 事件库路径（与 lib/events-db.ts::resolveEventsDbPath 一致；ledger-lib 无此函数，就地实现）。 */
function resolveEventsDbPath() {
  return process.env.EVENTS_DB || path.join(resolveDataDir(), "group-events.db");
}

function parseArgs(argv) {
  const args = { dir: null, keepDays: 14, keepMin: 5, ledgerOnly: false, eventsOnly: false, outSummary: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dir") args.dir = argv[++i];
    else if (a === "--keep-days") args.keepDays = Number(argv[++i]);
    else if (a === "--keep-min") args.keepMin = Number(argv[++i]);
    else if (a === "--ledger-only") args.ledgerOnly = true;
    else if (a === "--events-only") args.eventsOnly = true;
    else if (a === "--out-summary") args.outSummary = true;
    else if (a === "--help" || a === "-h") {
      console.log(
        "用法: node scripts/ledger-backup.mjs [--dir 备份目录] [--keep-days 14] [--keep-min 5] [--ledger-only|--events-only] [--out-summary]"
      );
      process.exit(0);
    } else {
      console.error(`未知参数: ${a}（--help 查看用法）`);
      process.exit(1);
    }
  }
  if (args.ledgerOnly && args.eventsOnly) {
    console.error("参数冲突: --ledger-only 与 --events-only 只能选一个");
    process.exit(1);
  }
  if (!Number.isFinite(args.keepDays) || args.keepDays < 0) {
    console.error("--keep-days 必须是 >= 0 的数字");
    process.exit(1);
  }
  if (!Number.isInteger(args.keepMin) || args.keepMin < 0) {
    console.error("--keep-min 必须是 >= 0 的整数");
    process.exit(1);
  }
  return args;
}

const pad = (n) => String(n).padStart(2, "0");

/** 本机时间戳 YYYYMMDD-HHmmss（与 scripts/leads-backup.sh 的 stamp 同风格）。 */
function stamp(d = new Date()) {
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const h = createHash("sha256");
    fs.createReadStream(file)
      .on("data", (chunk) => h.update(chunk))
      .on("end", () => resolve(h.digest("hex")))
      .on("error", reject);
  });
}

/** 全部用户表计数（sqlite_master 枚举，天然覆盖 customers/orders/licenses/personas/users/events）。 */
function tableCounts(db) {
  const tables = db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    .all()
    .map((r) => r.name);
  const counts = {};
  for (const t of tables) counts[t] = db.prepare(`SELECT COUNT(*) AS c FROM "${t}"`).get().c;
  return counts;
}

function readSchemaVersion(db) {
  try {
    const row = db.prepare("SELECT value FROM meta WHERE key = 'schema_version'").get();
    return row ? Number(row.value) || null : null;
  } catch {
    return null; // 无 meta 表（非本项目产的库）也不算错
  }
}

/** 单库备份 + 落盘校验 + meta 写盘。失败 throw（调用方计失败退出 1）；库缺失返回 skipped。 */
async function backupOne(kind, sourcePath, backupDir, ts) {
  const t0 = Date.now();
  const src = path.resolve(sourcePath);
  if (!fs.existsSync(src)) return { kind, status: "skipped_missing", source: src };
  const sourceSize = fs.statSync(src).size;

  let dest = path.join(backupDir, `${BASENAMES[kind]}-${ts}.db`);
  for (let i = 2; fs.existsSync(dest); i++) dest = path.join(backupDir, `${BASENAMES[kind]}-${ts}-${i}.db`);

  // 源库只读打开：备份是纯读操作，杜绝对生产库的任何建表/迁移/写入副作用
  const srcDb = new Database(src, { readonly: true, fileMustExist: true });
  try {
    srcDb.pragma("busy_timeout = 5000");
    await srcDb.backup(dest);
  } catch (err) {
    fs.rmSync(dest, { force: true });
    throw new Error(`在线备份失败: ${err?.message || err}`);
  } finally {
    srcDb.close();
  }

  // 备份后立即校验「备份文件本体」（而不是源库）：integrity_check + 全表计数
  let integrity = null;
  let counts = null;
  let schemaVersion = null;
  const chk = new Database(dest, { fileMustExist: true });
  try {
    chk.pragma("journal_mode = DELETE"); // 快照转平模式：单文件自洽，异地拷贝不缺 -wal/-shm
    const rows = chk.pragma("integrity_check");
    integrity =
      rows.length === 1 && String(rows[0].integrity_check).toLowerCase() === "ok"
        ? "ok"
        : rows.map((r) => r.integrity_check).join("; ");
    counts = tableCounts(chk);
    schemaVersion = readSchemaVersion(chk);
  } finally {
    chk.close();
    for (const suffix of ["-wal", "-shm"]) fs.rmSync(dest + suffix, { force: true });
  }
  if (integrity !== "ok") {
    fs.rmSync(dest, { force: true }); // 坏备份不留盘，防止被轮转/异地同步当成好份
    throw new Error(`备份文件 integrity_check 未通过: ${integrity}`);
  }

  const backupSize = fs.statSync(dest).size;
  const sha256 = await sha256File(dest);
  const meta = {
    version: 1,
    tool: "scripts/ledger-backup.mjs",
    kind,
    created_at: new Date().toISOString(),
    host: os.hostname(),
    source_db: src,
    source_size_bytes: sourceSize,
    backup_file: path.basename(dest),
    backup_size_bytes: backupSize,
    sha256,
    integrity: "ok",
    schema_version: schemaVersion,
    counts,
  };
  fs.writeFileSync(`${dest}.meta.json`, JSON.stringify(meta, null, 2) + "\n");
  return {
    kind,
    status: "ok",
    source: src,
    backup_file: path.basename(dest),
    backup_path: dest,
    backup_size_bytes: backupSize,
    sha256,
    integrity: "ok",
    schema_version: schemaVersion,
    counts,
    ms: Date.now() - t0,
  };
}

/** 文件名时间戳 → 毫秒（本机时区）；解析不了退回 mtime（拷贝/rclone 可能改 mtime，文件名优先）。 */
function stampToMs(name, fullPath) {
  const m = name.match(/-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-\d+)?\.db$/);
  if (m) return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
  try {
    return fs.statSync(fullPath).mtimeMs;
  } catch {
    return Date.now();
  }
}

/** 轮转：删除超龄备份（连 .meta.json），但该库族至少保留最新 keepMin 份（防误配清空）。
 *  只碰本工具命名模式的文件，目录里其他东西一概不动。 */
function rotate(kind, backupDir, keepDays, keepMin) {
  const re = new RegExp(`^${BASENAMES[kind]}-\\d{8}-\\d{6}(?:-\\d+)?\\.db$`);
  const entries = fs
    .readdirSync(backupDir)
    .filter((name) => re.test(name))
    .map((name) => ({ name, t: stampToMs(name, path.join(backupDir, name)) }))
    .sort((a, b) => b.t - a.t); // 新 → 旧
  const cutoff = Date.now() - keepDays * 86400000;
  const deleted = [];
  entries.forEach((e, idx) => {
    if (idx < keepMin) return; // 保底：无论多旧，最新 keepMin 份不动
    if (e.t > cutoff) return; // 未超龄
    for (const f of [e.name, `${e.name}.meta.json`, `${e.name}-wal`, `${e.name}-shm`]) {
      fs.rmSync(path.join(backupDir, f), { force: true });
    }
    deleted.push(e.name);
  });
  return { kind, kept: entries.length - deleted.length, deleted };
}

async function main() {
  const args = parseArgs(process.argv);
  const backupDir = path.resolve(args.dir || process.env.LEDGER_BACKUP_DIR || path.join(resolveDataDir(), "backups"));
  fs.mkdirSync(backupDir, { recursive: true });

  const wanted = args.ledgerOnly ? ["ledger"] : args.eventsOnly ? ["events"] : ["ledger", "events"];
  const sources = { ledger: resolveLedgerDbPath(), events: resolveEventsDbPath() };
  const ts = stamp();
  console.log(`[ledger-backup] 备份目录: ${backupDir} · keep-days=${args.keepDays} keep-min=${args.keepMin}`);

  const targets = [];
  let failed = false;
  for (const kind of wanted) {
    try {
      const r = await backupOne(kind, sources[kind], backupDir, ts);
      targets.push(r);
      if (r.status === "skipped_missing") {
        console.warn(`[ledger-backup] 警告: ${kind} 库不存在，跳过（VPS 初期可能未建）: ${r.source}`);
      } else {
        console.log(
          `[ledger-backup] ${kind}: ${r.source} → ${r.backup_file}（${r.backup_size_bytes} 字节 · integrity ok · ${r.ms}ms）`
        );
        console.log(`[ledger-backup]   计数: ${JSON.stringify(r.counts)}`);
      }
    } catch (err) {
      failed = true;
      targets.push({ kind, status: "error", source: path.resolve(sources[kind]), error: String(err?.message || err) });
      console.error(`[ledger-backup] 失败: ${kind}: ${err?.message || err}`);
    }
  }

  // 只轮转本轮成功备份过的库族——备份失败/跳过时绝不删旧份（旧份此刻就是全部身家）
  const rotation = [];
  for (const t of targets) {
    if (t.status !== "ok") continue;
    const r = rotate(t.kind, backupDir, args.keepDays, args.keepMin);
    rotation.push(r);
    if (r.deleted.length) {
      console.log(`[ledger-backup] 轮转 ${t.kind}: 删除 ${r.deleted.length} 份过期备份，保留 ${r.kept} 份`);
    }
  }

  const summary = {
    ok: !failed,
    backupDir,
    keepDays: args.keepDays,
    keepMin: args.keepMin,
    targets,
    rotation,
    generatedAt: new Date().toISOString(),
  };
  if (args.outSummary) console.log(JSON.stringify(summary));
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error(`[ledger-backup] 未预期异常: ${err?.stack || err}`);
  process.exit(1);
});
