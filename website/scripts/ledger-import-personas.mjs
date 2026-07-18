#!/usr/bin/env node
// 集团账本人设导入 CLI：读「人设导出文件」，按 (source_system, source_key) 幂等 upsert
// 进 personas 表（schema v3，DDL 复用 ledger-lib.mjs）。可重复执行（第二遍 0 新增）。
//
// 用法：node scripts/ledger-import-personas.mjs <导出json路径|outbox.jsonl> [--db 路径] [--system 缺省source_system]
// 导出文件格式：
// {
//   "version": 1,
//   "source_system": "avatarhub",          // 顶层缺省；记录级 source_system 优先
//   "exported_at": "2026-07-01T00:00:00Z",
//   "personas": [{
//     source_key, display_name, customer_name?,
//     slots: { face:    { present: bool, fingerprint?, ref?, version? },
//              voice:   { ... }, prompt: { ... }, knowledge: { ... } },
//     tags?: [], created_at?, raw?: {}
//   }, ...]
// }
// .jsonl（或内容首个非空字符不是 "{"）：一行一条 persona 记录（字段同上，source_system
// 取记录级，--system 可给缺省）；BOM 容错，空行忽略，坏行计数跳过。
//
// upsert 语义与 lib/personas.ts::upsertPersonaRow 一致（修改需两处同步）：
//   - customer_id 不覆盖（导入根本不写 customer_id，归属只在 console 做）；
//   - status 保持既有；purge_pending/purged 的行整行跳过并计 skippedPurged（不复活）；
//   - slots.present → slot_* 整型列；各槽位 fingerprint/ref/version → slots_detail JSON。
// 注册表只存元数据与指纹：raw 资产本体不入库；customer_name 仅留作 slots_detail._meta
// 里的人工归属线索。

import fs from "node:fs";
import path from "node:path";
import { isValidId, newId, openLedgerDb, s } from "./ledger-lib.mjs";

const SLOT_NAMES = ["face", "voice", "prompt", "knowledge"];

function parseArgs(argv) {
  const args = { file: null, db: null, system: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--db") args.db = argv[++i];
    else if (a === "--system") args.system = argv[++i];
    else if (a === "--help" || a === "-h") {
      console.log(
        "用法: node scripts/ledger-import-personas.mjs <导出json路径|outbox.jsonl> [--db group-ledger.db] [--system avatarhub]"
      );
      process.exit(0);
    } else if (!args.file) args.file = a;
    else {
      console.error(`未知参数: ${a}（--help 查看用法）`);
      process.exit(1);
    }
  }
  return args;
}

/** 读入参文件 → { records, defaultSystem, headline, badLines }。 */
function readRecords(file, cliSystem) {
  let content;
  try {
    content = fs.readFileSync(file, "utf-8");
  } catch (e) {
    console.error(`读取导出文件失败: ${file}\n${e?.message ?? e}`);
    process.exit(1);
  }
  content = content.replace(/^\uFEFF/, "");
  const head = content.trimStart();
  const isJsonl = /\.jsonl$/i.test(file) || head[0] !== "{";

  if (isJsonl) {
    const lines = content.split(/\r?\n/).filter((l) => l.trim() !== "");
    let badLines = 0;
    const records = [];
    for (const line of lines) {
      try {
        records.push(JSON.parse(line.replace(/^\uFEFF/, "")));
      } catch {
        badLines++;
      }
    }
    return {
      records,
      defaultSystem: cliSystem || "",
      headline: `personas jsonl · ${lines.length} 行（非空）· 坏行 ${badLines}`,
      badLines,
    };
  }

  let json;
  try {
    json = JSON.parse(content);
  } catch (e) {
    console.error(`解析导出文件失败: ${file}\n${e?.message ?? e}`);
    process.exit(1);
  }
  if (!Array.isArray(json?.personas)) {
    console.error("导出文件缺少 personas 数组，中止。");
    process.exit(1);
  }
  const defaultSystem =
    (typeof json.source_system === "string" ? json.source_system.trim() : "") || cliSystem || "";
  return {
    records: json.personas,
    defaultSystem,
    headline: `version=${json.version ?? "?"} · 顶层 source_system=${defaultSystem || "（无）"} · ${json.personas.length} 条`,
    badLines: 0,
  };
}

/** slots 对象 → { flags: {face..knowledge: 0/1}, detail: slots_detail 用 JSON 对象 }。 */
function buildSlots(slotsRaw) {
  const flags = { face: 0, voice: 0, prompt: 0, knowledge: 0 };
  const detail = {};
  if (slotsRaw && typeof slotsRaw === "object" && !Array.isArray(slotsRaw)) {
    for (const name of SLOT_NAMES) {
      const slot = slotsRaw[name];
      if (!slot || typeof slot !== "object" || Array.isArray(slot)) continue;
      const present = slot.present === true || slot.present === 1 || slot.present === "1";
      flags[name] = present ? 1 : 0;
      const d = { present };
      if (slot.fingerprint != null && String(slot.fingerprint).trim()) d.fingerprint = String(slot.fingerprint).trim();
      if (slot.ref != null && String(slot.ref).trim()) d.ref = String(slot.ref).trim();
      if (slot.version != null && String(slot.version).trim()) d.version = String(slot.version).trim();
      if (present || Object.keys(d).length > 1) detail[name] = d;
    }
  }
  return { flags, detail };
}

/** persona 幂等 upsert（语义与 lib/personas.ts::upsertPersonaRow 一致）。 */
function upsertPersona(db, rec, sourceSystem) {
  const sourceKey = String(rec.source_key ?? "").trim();
  const { flags, detail } = buildSlots(rec.slots);
  const customerName = s(rec.customer_name);
  if (customerName) detail._meta = { customer_name: customerName };
  const nowIsoStr = new Date().toISOString();
  const p = {
    source_system: sourceSystem,
    source_key: sourceKey,
    display_name: s(rec.display_name),
    slot_face: flags.face,
    slot_voice: flags.voice,
    slot_prompt: flags.prompt,
    slot_knowledge: flags.knowledge,
    slots_detail: Object.keys(detail).length ? JSON.stringify(detail) : null,
    tags: Array.isArray(rec.tags) && rec.tags.length ? JSON.stringify(rec.tags.map(String)) : null,
    created_at: s(rec.created_at),
    updated_at: nowIsoStr,
    synced_at: nowIsoStr,
  };
  const tx = db.transaction(() => {
    const existing = db
      .prepare("SELECT id, status FROM personas WHERE source_system = ? AND source_key = ?")
      .get(sourceSystem, sourceKey);
    if (!existing) {
      const id = rec.id && isValidId(rec.id, "prs") ? rec.id : newId("prs");
      db.prepare(
        `INSERT INTO personas (id, customer_id, source_system, source_key, display_name, slot_face, slot_voice, slot_prompt, slot_knowledge, slots_detail, tags, status, created_at, updated_at, synced_at)
         VALUES (@id, NULL, @source_system, @source_key, @display_name, @slot_face, @slot_voice, @slot_prompt, @slot_knowledge, @slots_detail, @tags, 'active', COALESCE(@created_at, @updated_at), @updated_at, @synced_at)`
      ).run({ ...p, id });
      return { inserted: true, skippedPurged: false };
    }
    if (existing.status === "purge_pending" || existing.status === "purged") {
      return { inserted: false, skippedPurged: true };
    }
    db.prepare(
      `UPDATE personas SET
         display_name = @display_name,
         slot_face = @slot_face, slot_voice = @slot_voice,
         slot_prompt = @slot_prompt, slot_knowledge = @slot_knowledge,
         slots_detail = @slots_detail, tags = @tags,
         created_at = COALESCE(created_at, @created_at),
         updated_at = @updated_at, synced_at = @synced_at
       WHERE source_system = @source_system AND source_key = @source_key`
    ).run(p);
    return { inserted: false, skippedPurged: false };
  });
  return tx();
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.file) {
    console.error(
      "缺少导出文件路径。用法: node scripts/ledger-import-personas.mjs <导出json路径|outbox.jsonl> [--db 路径] [--system 缺省source_system]"
    );
    process.exit(1);
  }
  const file = path.resolve(args.file);
  const { records, defaultSystem, headline, badLines } = readRecords(file, args.system ? String(args.system).trim() : "");

  const db = openLedgerDb(args.db || undefined);
  console.log(`账本 DB: ${db.name}`);
  console.log(`导入文件: ${file}（${headline}）`);

  const stats = { total: records.length, inserted: 0, updated: 0, skippedPurged: 0, invalid: badLines };
  for (const rec of records) {
    try {
      if (!rec || typeof rec !== "object" || Array.isArray(rec)) {
        stats.invalid++;
        continue;
      }
      const sourceSystem = String(rec.source_system ?? defaultSystem ?? "").trim();
      const sourceKey = String(rec.source_key ?? "").trim();
      if (!sourceSystem || !sourceKey) {
        stats.invalid++;
        continue;
      }
      const res = upsertPersona(db, rec, sourceSystem);
      if (res.skippedPurged) stats.skippedPurged++;
      else if (res.inserted) stats.inserted++;
      else stats.updated++;
    } catch {
      stats.invalid++;
    }
  }

  console.log(
    `导入完成: 共 ${stats.total} 条 · 新增 ${stats.inserted} · 更新 ${stats.updated} · skippedPurged ${stats.skippedPurged} · 无效跳过 ${stats.invalid}`
  );
  const total = db.prepare("SELECT COUNT(*) AS c FROM personas").get().c;
  const pending = db.prepare("SELECT COUNT(*) AS c FROM personas WHERE status = 'purge_pending'").get().c;
  const purged = db.prepare("SELECT COUNT(*) AS c FROM personas WHERE status = 'purged'").get().c;
  console.log(`账本现况: personas=${total} · purge_pending=${pending} · purged=${purged}`);
  db.close();
}

main();
