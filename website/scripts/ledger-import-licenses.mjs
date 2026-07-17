#!/usr/bin/env node
// 集团账本授权导入 CLI：读「授权归一化导出文件」，按 (source_system, source_key) 幂等
// upsert 进 licenses 表；无 id 时生成 lic_ 新 ID。可重复执行（第二遍 0 新增）。
//
// 用法：node scripts/ledger-import-licenses.mjs <导出json路径|outbox.jsonl> [--db 路径]
// 导出文件格式：
// {
//   "version": 1,
//   "source_system": "avatarhub" | "chengjie",   // 记录级 source_system 优先，顶层做缺省
//   "exported_at": "2026-07-01T00:00:00Z",
//   "records": [{ source_system, source_key, product_id, sku_id, plan, edition, seats,
//                 customer_name, customer_contact, machine_fingerprint,
//                 issued_at, expires_at, status, raw }, ...]
// }
// 实时 outbox 格式（入参以 .jsonl 结尾，或内容首个非空字符不是 "{"）：一行一条归一化
// record 对象（字段同上，source_system 取自每条记录，无顶层缺省）；空行忽略，坏行
// （JSON 解析失败/非对象）跳过并计入无效。
// 容错：缺字段置 null；缺 source_key（或 source_system 记录级+顶层都缺）的记录跳过并计数。
// customer_name / customer_contact 不建客户，只塞进 raw 供 console 后续人工归属。

import fs from "node:fs";
import path from "node:path";
import { getStats, openLedgerDb, upsertLicenseRow } from "./ledger-lib.mjs";

function parseArgs(argv) {
  const args = { file: null, db: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--db") args.db = argv[++i];
    else if (a === "--help" || a === "-h") {
      console.log("用法: node scripts/ledger-import-licenses.mjs <导出json路径|outbox.jsonl> [--db group-ledger.db]");
      process.exit(0);
    } else if (!args.file) args.file = a;
    else {
      console.error(`未知参数: ${a}（--help 查看用法）`);
      process.exit(1);
    }
  }
  return args;
}

/** 读入参文件 → { records, defaultSystem, headline }。
 *  .jsonl 后缀（或内容首个非空字符不是 "{"）按 outbox 行式解析：一行一条 record，
 *  坏行以 null 占位（进入主循环后按 invalid 计数），source_system 只认记录级（无顶层缺省）。 */
function readRecords(file) {
  let content;
  try {
    content = fs.readFileSync(file, "utf-8");
  } catch (e) {
    console.error(`读取导出文件失败: ${file}\n${e?.message ?? e}`);
    process.exit(1);
  }
  const head = content.replace(/^\uFEFF/, "").trimStart();
  const isJsonl = /\.jsonl$/i.test(file) || head[0] !== "{";

  if (isJsonl) {
    const lines = content.split(/\r?\n/).filter((l) => l.trim() !== "");
    const records = lines.map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null; // 坏行 → 主循环按无效跳过计数
      }
    });
    return { records, defaultSystem: "", headline: `outbox jsonl · ${records.length} 行（非空）` };
  }

  let json;
  try {
    json = JSON.parse(content);
  } catch (e) {
    console.error(`解析导出文件失败: ${file}\n${e?.message ?? e}`);
    process.exit(1);
  }
  if (!Array.isArray(json?.records)) {
    console.error("导出文件缺少 records 数组，中止。");
    process.exit(1);
  }
  const defaultSystem = typeof json.source_system === "string" ? json.source_system.trim() : "";
  return {
    records: json.records,
    defaultSystem,
    headline: `version=${json.version ?? "?"} · 顶层 source_system=${defaultSystem || "（无）"} · ${json.records.length} 条`,
  };
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.file) {
    console.error("缺少导出文件路径。用法: node scripts/ledger-import-licenses.mjs <导出json路径|outbox.jsonl> [--db 路径]");
    process.exit(1);
  }
  const file = path.resolve(args.file);
  const { records, defaultSystem, headline } = readRecords(file);

  const db = openLedgerDb(args.db || undefined);
  console.log(`账本 DB: ${db.name}`);
  console.log(`导入文件: ${file}（${headline}）`);

  const stats = { total: records.length, inserted: 0, updated: 0, invalid: 0 };
  for (const rec of records) {
    try {
      if (!rec || typeof rec !== "object") {
        stats.invalid++;
        continue;
      }
      const sourceSystem = String(rec.source_system ?? defaultSystem ?? "").trim();
      const sourceKey = String(rec.source_key ?? "").trim();
      if (!sourceSystem || !sourceKey) {
        stats.invalid++;
        continue;
      }
      // raw 保留完整原始记录（含 customer_name/customer_contact，供 console 人工归属客户）
      const raw = rec.raw !== undefined && rec.raw !== null
        ? typeof rec.raw === "string" ? rec.raw : JSON.stringify(rec.raw)
        : JSON.stringify(rec);
      const res = upsertLicenseRow(
        {
          id: rec.id, // 合法 lic_ ID 才会被采用，否则自动生成
          source_system: sourceSystem,
          source_key: sourceKey,
          product_id: rec.product_id ?? null,
          sku_id: rec.sku_id ?? null,
          plan: rec.plan ?? null,
          edition: rec.edition ?? null,
          seats: rec.seats ?? null,
          machine_fingerprint: rec.machine_fingerprint ?? null,
          issued_at: rec.issued_at ?? null,
          expires_at: rec.expires_at ?? null,
          status: rec.status ?? null,
          raw,
        },
        db
      );
      res.inserted ? stats.inserted++ : stats.updated++;
    } catch {
      stats.invalid++;
    }
  }

  console.log(`导入完成: 共 ${stats.total} 条 · 新增 ${stats.inserted} · 更新 ${stats.updated} · 无效跳过 ${stats.invalid}`);
  const s = getStats(db);
  console.log(`账本现况: licenses=${s.licenses} · 30 天内到期 ${s.licensesExpiringIn30d}`);
  db.close();
}

main();
