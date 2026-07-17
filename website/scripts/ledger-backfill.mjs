#!/usr/bin/env node
// 集团账本回填 CLI：读现有 orders-db.json / leads-db.json，全量幂等 upsert 进 group-ledger.db。
// 用法：node scripts/ledger-backfill.mjs [--orders 路径] [--leads 路径] [--db 路径]
// 默认路径：ORDERS_DB / LEADS_DB env → DATA_DIR（LEADS_DIR env 或 ~/hualing-leads）下同名文件；
//          账本 DB 为 LEDGER_DB env 或 DATA_DIR/group-ledger.db。
// 幂等：可重复执行，第二遍 inserted 应为 0（只刷新镜像字段与 synced_at）。
// TS 侧等价实现：website/lib/ledger-sync.ts::backfillFromJson（语义变更两处同步）。

import fs from "node:fs";
import path from "node:path";
import {
  ensureCustomerForLead,
  ensureCustomerForOrder,
  leadEntryToRow,
  openLedgerDb,
  orderEntryToRow,
  resolveDataDir,
  upsertLeadRow,
  upsertOrderRow,
  getStats,
} from "./ledger-lib.mjs";

function parseArgs(argv) {
  const args = { orders: null, leads: null, db: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--orders") args.orders = argv[++i];
    else if (a === "--leads") args.leads = argv[++i];
    else if (a === "--db") args.db = argv[++i];
    else if (a === "--help" || a === "-h") {
      console.log("用法: node scripts/ledger-backfill.mjs [--orders orders-db.json] [--leads leads-db.json] [--db group-ledger.db]");
      process.exit(0);
    } else {
      console.error(`未知参数: ${a}（--help 查看用法）`);
      process.exit(1);
    }
  }
  return args;
}

function readJsonFile(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf-8"));
  } catch {
    return undefined;
  }
}

function backfillOrders(file, db) {
  const stats = { file, found: false, scanned: 0, inserted: 0, updated: 0, skipped: 0, errors: 0 };
  const json = readJsonFile(file);
  if (!json?.orders || typeof json.orders !== "object") return stats;
  stats.found = true;
  for (const [key, value] of Object.entries(json.orders)) {
    stats.scanned++;
    try {
      const entry = { ...value, id: value?.id || key };
      if (!entry.id) {
        stats.skipped++;
        continue;
      }
      const res = upsertOrderRow(orderEntryToRow(entry), db);
      res.inserted ? stats.inserted++ : stats.updated++;
      ensureCustomerForOrder(entry.id, db);
    } catch {
      stats.errors++;
    }
  }
  return stats;
}

function backfillLeads(file, db) {
  const stats = { file, found: false, scanned: 0, inserted: 0, updated: 0, skipped: 0, errors: 0 };
  const json = readJsonFile(file);
  if (!json?.leads || typeof json.leads !== "object") return stats;
  stats.found = true;
  for (const [key, value] of Object.entries(json.leads)) {
    stats.scanned++;
    try {
      const entry = { ...value, id: value?.id || key };
      if (!entry.id) {
        stats.skipped++;
        continue;
      }
      const res = upsertLeadRow(leadEntryToRow(entry), db);
      res.inserted ? stats.inserted++ : stats.updated++;
      ensureCustomerForLead(entry.id, db);
    } catch {
      stats.errors++;
    }
  }
  return stats;
}

function main() {
  const args = parseArgs(process.argv);
  const dataDir = resolveDataDir();
  const ordersFile = path.resolve(args.orders || process.env.ORDERS_DB || path.join(dataDir, "orders-db.json"));
  const leadsFile = path.resolve(args.leads || process.env.LEADS_DB || path.join(dataDir, "leads-db.json"));

  const db = openLedgerDb(args.db || undefined);
  console.log(`账本 DB: ${db.name}`);

  const fmt = (label, r) =>
    console.log(
      `${label}: ${r.found ? "" : "（文件缺失/不可读）"}扫描 ${r.scanned} · 新增 ${r.inserted} · 更新 ${r.updated} · 跳过 ${r.skipped} · 出错 ${r.errors}\n  ← ${r.file}`
    );

  const o = backfillOrders(ordersFile, db);
  fmt("订单", o);
  const l = backfillLeads(leadsFile, db);
  fmt("留资", l);

  const stats = getStats(db);
  console.log(
    `账本现况: customers=${stats.customers} leads=${stats.leads} orders=${stats.orders} licenses=${stats.licenses} · 订单状态 ${JSON.stringify(stats.ordersByStatus)}`
  );
  db.close();
}

main();
