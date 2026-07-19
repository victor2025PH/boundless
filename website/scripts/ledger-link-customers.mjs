#!/usr/bin/env node
// 集团账本「客户自动归并」CLI：扫 orders / leads / licenses / personas 四表的未归属行
// （customer_id IS NULL），按联系标识保守归并出客户主档（customers + identities），
// 并回填四表的 customer_id。幂等可重复跑：已归属行整行跳过；identities UNIQUE(kind,value)
// 不重复登记；名字弱信号档按 display_name + source='link-customers:name' 找回不重建。
//
// 纪律：绝不删数据；绝不合并两个已存在的 customer —— 只做「无主行 → 建档/并入」；
// 组内标识命中多个不同已有客户时整组跳过（冲突上报，留给人工）。
//
// 用法：node scripts/ledger-link-customers.mjs [--db group-ledger.db] [--dry-run]
//   --dry-run  只扫描并打印归并计划，不写库（无建档/无挂身份/无回填/无 audit）。
//
// 归并规则（保守优先，宁可少并不错并）：
//   a) telegram handle 完全相同（去 @ / t.me 前缀、小写）＝同一客户。
//      仅显式 @xxx 或 t.me/xxx 形态才判为 handle，裸文本不猜（避免误判）。
//   b) email 完全相同（小写）＝同一客户；phone 完全相同（去分隔符）同级安全，一并支持。
//   b2) telegram 数字 id 完全相同＝同一客户：留资 source_key「tg:<id>」/ raw.tg_user_id
//       与订单 notify_chat（客户本人点深链绑定的私聊 chat_id，即其 user id；仅纯数字
//       才算，排除负数群聊）同一取值空间 —— 与现有 ensureCustomerForLead 登记的
//       identities kind='tg' 语义一致。
//   c) license(raw.customer_name) / persona(slots_detail._meta.customer_name) 名字
//      trim 后逐字符相同且非空＝同一客户。纯名字弱信号：只在该行没有任何其它标识时
//      参与；建档标注 source='link-customers:name' + notes 注明；不登记 identities
//      （identities.kind CHECK 无 'name'，不改 schema）；也绝不并入强信号客户。
//   自由文本 contact（非 handle/email/phone 形态）不作为跨行归并键，仅作为该客户的
//   kind='contact' 身份登记（供未来 ensureCustomerForOrder/Lead 钩子精确命中）。
//
// audit 风格沿用现有账本：customer.create / identity.attach（createCustomer /
// attachIdentity 内置）+ 每回填一行写 action='link_customer'（actor=link-script，
// detail={customer_id, via, source_key}）。

import {
  attachIdentity,
  createCustomer,
  linkCustomer,
  normIdentityValue,
  openLedgerDb,
  s,
  writeAudit,
} from "./ledger-lib.mjs";

const ACTOR = "link-script";
const SOURCE_STRONG = "link-customers";
const SOURCE_NAME = "link-customers:name";

// ── 参数 ────────────────────────────────────────────────────────────
function parseArgs(argv) {
  const args = { db: null, dryRun: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--db") args.db = argv[++i];
    else if (a === "--dry-run") args.dryRun = true;
    else if (a === "--help" || a === "-h") {
      console.log("用法: node scripts/ledger-link-customers.mjs [--db group-ledger.db] [--dry-run]");
      process.exit(0);
    } else {
      console.error(`未知参数: ${a}（--help 查看用法）`);
      process.exit(1);
    }
  }
  return args;
}

// ── 联系方式分类（分组键只认 handle / email / phone / tgid 四种精确形态）──
function classifyContact(text) {
  const t = String(text ?? "").trim();
  if (!t) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(t)) return { type: "email", norm: t.toLowerCase() };
  const handle =
    t.match(/^@([A-Za-z0-9_]{3,32})$/) ||
    t.match(/^(?:https?:\/\/)?(?:www\.)?t(?:elegram)?\.me\/@?([A-Za-z0-9_]{3,32})\/?$/i);
  if (handle) return { type: "handle", norm: handle[1].toLowerCase() };
  const digits = t.replace(/[\s\-()]/g, "");
  if (/^\+?\d{7,15}$/.test(digits)) return { type: "phone", norm: digits };
  return { type: "text", norm: t.toLowerCase().replace(/\s+/g, "") };
}

// ── 行信号容器 ──────────────────────────────────────────────────────
function emptySig() {
  return {
    groupKeys: [], // "handle:x" / "email:x" / "phone:x" / "tgid:x" —— 跨行归并键
    idents: [], // [kind, value]（已 norm）→ attachIdentity / linkCustomer 探测
    display: [], // [weight, text] display_name 候选
    primary: [], // [weight, text] primary_contact 候选
    tgUserId: null,
    nameSignal: null, // 纯名字弱信号（licenses/personas）
  };
}

function pushIdent(sig, kind, value) {
  const v = normIdentityValue(kind, value);
  if (!v) return;
  if (!sig.idents.some(([k, x]) => k === kind && x === v)) sig.idents.push([kind, v]);
}

function addTgId(sig, id) {
  const v = String(id ?? "").trim();
  if (!/^\d+$/.test(v)) return;
  if (!sig.groupKeys.includes(`tgid:${v}`)) sig.groupKeys.push(`tgid:${v}`);
  pushIdent(sig, "tg", v);
  if (!sig.tgUserId) sig.tgUserId = v;
}

function addContact(sig, text) {
  const c = classifyContact(text);
  if (!c) return;
  const orig = String(text).trim();
  pushIdent(sig, "contact", orig); // 原文规范化：未来钩子以 contact 原文查 kind='contact'
  if (c.type === "handle") {
    if (!sig.groupKeys.includes(`handle:${c.norm}`)) sig.groupKeys.push(`handle:${c.norm}`);
    pushIdent(sig, "contact", c.norm); // 规范化 handle 也登记（@Alice / t.me/alice 均可命中）
    sig.display.push([60, `@${c.norm}`]);
    sig.primary.push([90, orig]);
  } else if (c.type === "email") {
    if (!sig.groupKeys.includes(`email:${c.norm}`)) sig.groupKeys.push(`email:${c.norm}`);
    pushIdent(sig, "email", c.norm);
    sig.display.push([50, c.norm]);
    sig.primary.push([80, c.norm]);
  } else if (c.type === "phone") {
    if (!sig.groupKeys.includes(`phone:${c.norm}`)) sig.groupKeys.push(`phone:${c.norm}`);
    pushIdent(sig, "phone", c.norm);
    sig.display.push([40, orig]);
    sig.primary.push([70, orig]);
  } else {
    // 自由文本：不作分组键，只登记身份 + 兜底候选
    sig.display.push([30, orig]);
    sig.primary.push([40, orig]);
  }
}

function parseJson(text) {
  if (!text) return null;
  try {
    const v = JSON.parse(text);
    return v && typeof v === "object" ? v : null;
  } catch {
    return null;
  }
}

// ── 四表扫描 → 统一行模型 { table, pk, entity, entityId, sourceKey, label, sig } ──
function scanRows(db) {
  const rows = [];
  const linked = {};
  const count = (table) =>
    db.prepare(`SELECT COUNT(*) AS c FROM ${table} WHERE customer_id IS NOT NULL`).get().c;

  linked.orders = count("orders");
  for (const r of db
    .prepare("SELECT id, source_key, contact, notify_chat FROM orders WHERE customer_id IS NULL")
    .all()) {
    const sig = emptySig();
    addContact(sig, r.contact);
    const chat = String(r.notify_chat ?? "").trim();
    if (/^\d+$/.test(chat)) addTgId(sig, chat);
    rows.push({ table: "orders", pk: r.id, entity: "order", entityId: r.source_key, sourceKey: r.source_key, label: `orders:${r.source_key}`, sig });
  }

  linked.leads = count("leads");
  for (const r of db
    .prepare("SELECT source_key, name, contact, raw FROM leads WHERE customer_id IS NULL")
    .all()) {
    const sig = emptySig();
    if (r.source_key.startsWith("tg:")) addTgId(sig, r.source_key.slice(3));
    else if (r.source_key.startsWith("c:")) addContact(sig, r.source_key.slice(2));
    addContact(sig, r.contact);
    const raw = parseJson(r.raw);
    if (raw?.tg_user_id != null) addTgId(sig, raw.tg_user_id);
    if (s(r.name)) sig.display.push([100, String(r.name).trim()]);
    rows.push({ table: "leads", pk: r.source_key, entity: "lead", entityId: r.source_key, sourceKey: r.source_key, label: `leads:${r.source_key}`, sig });
  }

  linked.licenses = count("licenses");
  for (const r of db
    .prepare("SELECT id, source_system, source_key, raw FROM licenses WHERE customer_id IS NULL")
    .all()) {
    const sig = emptySig();
    const raw = parseJson(r.raw);
    // 导入器把 customer_name/customer_contact 留在 raw：顶层（raw=整条 rec）或
    // 再嵌一层 raw.raw（上游原始记录）都探一下，宁缺毋错。
    const contact = s(raw?.customer_contact) ?? s(raw?.raw?.customer_contact);
    const name = s(raw?.customer_name) ?? s(raw?.raw?.customer_name);
    if (contact) addContact(sig, contact);
    if (name) {
      sig.display.push([90, name]);
      sig.nameSignal = name;
    }
    rows.push({ table: "licenses", pk: r.id, entity: "license", entityId: r.id, sourceKey: `${r.source_system}:${r.source_key}`, label: `licenses:${r.source_system}:${r.source_key}`, sig });
  }

  linked.personas = count("personas");
  for (const r of db
    .prepare("SELECT id, source_system, source_key, slots_detail FROM personas WHERE customer_id IS NULL")
    .all()) {
    const sig = emptySig();
    const detail = parseJson(r.slots_detail);
    const name = s(detail?._meta?.customer_name);
    if (name) {
      sig.display.push([85, name]);
      sig.nameSignal = name;
    }
    rows.push({ table: "personas", pk: r.id, entity: "persona", entityId: r.id, sourceKey: `${r.source_system}:${r.source_key}`, label: `personas:${r.source_system}:${r.source_key}`, sig });
  }

  return { rows, linked };
}

// ── 并查集（groupKey 为节点）────────────────────────────────────────
function makeUnionFind() {
  const parent = new Map();
  const ensure = (x) => {
    if (!parent.has(x)) parent.set(x, x);
  };
  const find = (x) => {
    ensure(x);
    while (parent.get(x) !== x) {
      parent.set(x, parent.get(parent.get(x)));
      x = parent.get(x);
    }
    return x;
  };
  const union = (a, b) => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent.set(ra, rb);
  };
  return { find, union };
}

function pickBest(candidates) {
  let best = null;
  for (const [w, text] of candidates) {
    const t = s(text);
    if (!t) continue;
    if (!best || w > best[0]) best = [w, t];
  }
  return best ? best[1] : null;
}

// ── 回填一行 + audit ────────────────────────────────────────────────
function backfillRow(db, row, customerId, via) {
  let changes = 0;
  if (row.table === "orders") {
    changes = db.prepare("UPDATE orders SET customer_id = ? WHERE id = ? AND customer_id IS NULL").run(customerId, row.pk).changes;
  } else if (row.table === "leads") {
    changes = db.prepare("UPDATE leads SET customer_id = ? WHERE source_key = ? AND customer_id IS NULL").run(customerId, row.pk).changes;
  } else if (row.table === "licenses") {
    changes = db.prepare("UPDATE licenses SET customer_id = ? WHERE id = ? AND customer_id IS NULL").run(customerId, row.pk).changes;
  } else if (row.table === "personas") {
    // 与 lib/personas.ts::assignPersonaCustomer 同款：归属时刷新 updated_at
    changes = db
      .prepare("UPDATE personas SET customer_id = ?, updated_at = ? WHERE id = ? AND customer_id IS NULL")
      .run(customerId, new Date().toISOString(), row.pk).changes;
  }
  if (changes) {
    writeAudit(
      {
        actor: ACTOR,
        action: "link_customer",
        entity: row.entity,
        entity_id: row.entityId,
        detail: { customer_id: customerId, via, source_key: row.sourceKey },
      },
      db
    );
  }
  return changes;
}

// ── 主流程 ──────────────────────────────────────────────────────────
function main() {
  const args = parseArgs(process.argv);
  const db = openLedgerDb(args.db || undefined);
  const dry = args.dryRun;
  console.log(`账本 DB: ${db.name}`);
  console.log(`模式: ${dry ? "DRY-RUN（只看不写）" : "实跑（写库）"}`);

  const { rows, linked } = scanRows(db);
  const byTable = { orders: 0, leads: 0, licenses: 0, personas: 0 };
  for (const r of rows) byTable[r.table]++;
  console.log(
    `扫描（未归属 / 已归属跳过）: orders ${byTable.orders}/${linked.orders} · leads ${byTable.leads}/${linked.leads} · ` +
      `licenses ${byTable.licenses}/${linked.licenses} · personas ${byTable.personas}/${linked.personas}`
  );

  const strongRows = rows.filter((r) => r.sig.groupKeys.length > 0);
  const nameRows = rows.filter((r) => r.sig.groupKeys.length === 0 && r.sig.nameSignal);
  const noSignal = rows.filter((r) => r.sig.groupKeys.length === 0 && !r.sig.nameSignal);

  // 强信号分组
  const uf = makeUnionFind();
  for (const r of strongRows) {
    for (let i = 1; i < r.sig.groupKeys.length; i++) uf.union(r.sig.groupKeys[0], r.sig.groupKeys[i]);
  }
  const groups = new Map(); // root -> { rows, keys:Set, idents:Map }
  for (const r of strongRows) {
    const root = uf.find(r.sig.groupKeys[0]);
    let g = groups.get(root);
    if (!g) groups.set(root, (g = { rows: [], keys: new Set(), idents: new Map() }));
    g.rows.push(r);
    for (const k of r.sig.groupKeys) g.keys.add(k);
    for (const [kind, value] of r.sig.idents) g.idents.set(`${kind}\u0000${value}`, [kind, value]);
  }

  // 名字弱信号分组（trim 后逐字符相同）
  const nameGroups = new Map();
  for (const r of nameRows) {
    const key = r.sig.nameSignal;
    let g = nameGroups.get(key);
    if (!g) nameGroups.set(key, (g = []));
    g.push(r);
  }

  console.log(
    `分组: 强信号 ${groups.size} 组（覆盖 ${strongRows.length} 行）· 纯名字 ${nameGroups.size} 组（覆盖 ${nameRows.length} 行）· 无标识跳过 ${noSignal.length} 行`
  );

  const stats = {
    createdStrong: 0,
    createdName: 0,
    mergedIntoExisting: 0,
    identAttached: 0,
    identConflicts: 0,
    backfilled: { orders: 0, leads: 0, licenses: 0, personas: 0 },
    conflictGroups: 0,
  };

  // ── 强信号组处理 ──
  for (const [, g] of groups) {
    const via = [...g.keys].sort().join(" + ");
    const rowLabels = g.rows.map((r) => r.label).join(", ");

    // 探测已有客户（identities 精确匹配）；命中多个不同客户 = 冲突，整组跳过
    const hits = new Set();
    for (const [kind, value] of g.idents.values()) {
      const cid = linkCustomer(kind, value, db);
      if (cid) hits.add(cid);
    }
    if (hits.size > 1) {
      stats.conflictGroups++;
      console.log(`[冲突跳过] via ${via} 命中多个已有客户（${[...hits].join(", ")}）← ${rowLabels}`);
      continue;
    }
    const existing = hits.size === 1 ? [...hits][0] : null;

    const displayName = pickBest(g.rows.flatMap((r) => r.sig.display));
    const primaryContact = pickBest(g.rows.flatMap((r) => r.sig.primary));
    const tgUserId = g.rows.map((r) => r.sig.tgUserId).find(Boolean) ?? null;

    if (dry) {
      console.log(
        `[计划] ${existing ? `并入已有客户 ${existing}` : `新建客户「${displayName ?? "（未命名）"}」`} via ${via} ← ${rowLabels}`
      );
      if (existing) stats.mergedIntoExisting++;
      else stats.createdStrong++;
      for (const r of g.rows) stats.backfilled[r.table]++;
      continue;
    }

    const tx = db.transaction(() => {
      let cid = existing;
      if (!cid) {
        const cust = createCustomer(
          {
            display_name: displayName,
            primary_contact: primaryContact,
            tg_user_id: tgUserId,
            source: SOURCE_STRONG,
            notes: `自动归并（${via}）：覆盖 ${g.rows.length} 行（${g.rows.map((r) => r.table).join("/")}）`,
          },
          db,
          ACTOR
        );
        cid = cust.id;
        stats.createdStrong++;
      } else {
        stats.mergedIntoExisting++;
      }
      for (const [kind, value] of g.idents.values()) {
        const res = attachIdentity(cid, kind, value, db, ACTOR);
        if (res.ok && !res.existed) stats.identAttached++;
        else if (!res.ok) {
          stats.identConflicts++;
          console.log(`  [身份冲突] ${kind}=${value} 已属于 ${res.conflictCustomerId}，不抢占`);
        }
      }
      for (const r of g.rows) {
        if (backfillRow(db, r, cid, via)) stats.backfilled[r.table]++;
      }
      return cid;
    });
    const cid = tx();
    // 并入已有客户时不打印组候选名（并入不改已有档案的 display_name）
    console.log(
      existing
        ? `[并入] ${cid} via ${via} ← ${rowLabels}`
        : `[建档] ${cid} 「${displayName ?? "（未命名）"}」 via ${via} ← ${rowLabels}`
    );
  }

  // ── 纯名字弱信号组处理 ──
  for (const [name, groupRows] of nameGroups) {
    const via = "name";
    const rowLabels = groupRows.map((r) => r.label).join(", ");
    // 幂等找回：只认本脚本名字来源建的档，绝不并入手工/强信号客户
    const existing = db
      .prepare("SELECT id FROM customers WHERE display_name = ? AND source = ?")
      .get(name, SOURCE_NAME);

    if (dry) {
      console.log(
        `[计划] ${existing ? `并入已有名字档 ${existing.id}` : `新建客户「${name}」（纯名字弱信号）`} ← ${rowLabels}`
      );
      if (existing) stats.mergedIntoExisting++;
      else stats.createdName++;
      for (const r of groupRows) stats.backfilled[r.table]++;
      continue;
    }

    const tx = db.transaction(() => {
      let cid = existing?.id ?? null;
      if (!cid) {
        const cust = createCustomer(
          {
            display_name: name,
            source: SOURCE_NAME,
            notes: `纯名字弱信号建档（license/persona 的 customer_name 完全相同）：覆盖 ${groupRows.length} 行；无联系方式标识，待人工核实`,
          },
          db,
          ACTOR
        );
        cid = cust.id;
        stats.createdName++;
      } else {
        stats.mergedIntoExisting++;
      }
      for (const r of groupRows) {
        if (backfillRow(db, r, cid, via)) stats.backfilled[r.table]++;
      }
      return cid;
    });
    const cid = tx();
    console.log(`[${existing ? "并入" : "建档"}] ${cid} 「${name}」 via name（弱信号）← ${rowLabels}`);
  }

  // ── 摘要 ──
  const bf = stats.backfilled;
  const bfTotal = bf.orders + bf.leads + bf.licenses + bf.personas;
  console.log("── 摘要 ──────────────────────────────────");
  console.log(
    `${dry ? "（DRY-RUN 计划值，未写库）" : ""}新建客户 ${stats.createdStrong + stats.createdName}（强信号 ${stats.createdStrong} + 纯名字 ${stats.createdName}）· 并入已有客户 ${stats.mergedIntoExisting} 组`
  );
  console.log(
    `归并身份 ${dry ? "（dry-run 不登记）" : stats.identAttached + " 条"}${stats.identConflicts ? ` · 身份冲突不抢占 ${stats.identConflicts} 条` : ""}`
  );
  console.log(`回填行 ${bfTotal}: orders ${bf.orders} · leads ${bf.leads} · licenses ${bf.licenses} · personas ${bf.personas}`);
  console.log(
    `跳过: 已归属 ${linked.orders + linked.leads + linked.licenses + linked.personas} 行（orders ${linked.orders}/leads ${linked.leads}/licenses ${linked.licenses}/personas ${linked.personas}）· 无标识 ${noSignal.length} 行 · 冲突组 ${stats.conflictGroups}`
  );
  if (noSignal.length) {
    for (const r of noSignal) console.log(`  [无标识] ${r.label}`);
  }
  const c = db.prepare("SELECT COUNT(*) AS c FROM customers").get().c;
  const i = db.prepare("SELECT COUNT(*) AS c FROM identities").get().c;
  console.log(`账本现况: customers=${c} identities=${i}`);
  db.close();
}

main();
