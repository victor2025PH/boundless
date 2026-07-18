// 人设总线（Persona Bus）数据层 —— schema v3：personas / persona_grants / persona_purges。
//
// 定位：同一个数字身份（persona）贯穿获客→承接→变现的集团注册表。一个 persona
// 有四个可选槽位：face（形象/脸模）、voice（声纹克隆）、prompt（语言人格/话术）、
// knowledge（术语库/知识库）。注册表**只存元数据与指纹**（slots_detail JSON 记各
// 槽位 fingerprint/ref/version），资产本体（脸模文件、声纹模型、话术库）永不进集团库。
//
// purge 协议（全域清除）：console 发起 requestPurge → 状态置 purge_pending，并为
// 每个已知承载系统（persona 的 source_system + grants 推导出的各产品所属引擎）各插
// 一条 persona_purges 指令 → 引擎经 /api/sync/personas/purges 轮询未 ack 指令并在
// 本地删除资产后回 ack → 全部 target ack 后状态置 purged。purge_pending / purged
// 的行不会被后续同步/导入复活（upsert 跳过并计数 skippedPurged）。
//
// 写操作全部写 audit（entity="persona"）。console API / 导入脚本从这里走；
// scripts/ledger-import-personas.mjs 是纯 JS CLI，upsert 语义与本文件
// upsertPersonaRow 一致（DDL 复用 scripts/ledger-lib.mjs），修改需两处同步。

import type Database from "better-sqlite3";
import { getLedgerDb, writeAudit } from "./ledger";
import { isValidId, newId } from "./ids";

// ── 枚举 ────────────────────────────────────────────────────────────
export const PERSONA_STATUSES = ["active", "archived", "purge_pending", "purged"] as const;
export type PersonaStatus = (typeof PERSONA_STATUSES)[number];

export const PERSONA_SLOTS = ["face", "voice", "prompt", "knowledge"] as const;
export type PersonaSlot = (typeof PERSONA_SLOTS)[number];

/** 可对人设授权的产品（website 属官网服务，不承载人设资产，不在矩阵内）。 */
export const PERSONA_PRODUCT_IDS = [
  "zhituo",
  "zhiliao",
  "tongyi",
  "tongchuan",
  "huansheng",
  "huanying",
  "huanyan",
] as const;
export type PersonaProductId = (typeof PERSONA_PRODUCT_IDS)[number];

/** 产品 → 承载引擎（purge 指令的下发目标系统）。 */
export const PRODUCT_ENGINE_MAP: Record<PersonaProductId, string> = {
  huansheng: "avatarhub",
  huanying: "avatarhub",
  huanyan: "avatarhub",
  tongchuan: "avatarhub",
  zhiliao: "chengjie",
  tongyi: "chengjie",
  zhituo: "huoke",
};

// ── 行类型 ──────────────────────────────────────────────────────────
export interface PersonaRow {
  id: string;
  customer_id: string | null;
  source_system: string;
  source_key: string;
  display_name: string | null;
  slot_face: number;
  slot_voice: number;
  slot_prompt: number;
  slot_knowledge: number;
  /** JSON：{face|voice|prompt|knowledge: {fingerprint?, ref?, version?}, _meta?: {...}}。 */
  slots_detail: string | null;
  /** JSON 数组文本。 */
  tags: string | null;
  status: PersonaStatus;
  created_at: string | null;
  updated_at: string | null;
  synced_at: string | null;
}

export interface PersonaGrantRow {
  id: number;
  persona_id: string;
  product_id: string;
  scope: string | null;
  granted_by: string | null;
  granted_at: string | null;
  revoked_at: string | null;
}

export interface PersonaPurgeRow {
  id: number;
  persona_id: string;
  requested_by: string | null;
  requested_at: string | null;
  target_system: string;
  acked_at: string | null;
  ack_detail: string | null;
}

/** upsert 入参：自然键必填；status 不收——状态只由集团侧（console/purge 流程）管理。 */
export type PersonaRowInput = Partial<
  Omit<PersonaRow, "source_system" | "source_key" | "status" | "synced_at">
> & {
  source_system: string;
  source_key: string;
};

// ── 取值规整（与 ledger.ts 同款约定）────────────────────────────────
function s(v: unknown): string | null {
  if (v === undefined || v === null) return null;
  const t = String(v).trim();
  return t === "" ? null : t;
}
function slotInt(v: unknown): number {
  return v === true || v === 1 || v === "1" || (typeof v === "number" && v > 0) ? 1 : 0;
}
const nowIso = () => new Date().toISOString();

// ── 幂等 upsert（自然键 (source_system, source_key)）────────────────
export interface PersonaUpsertResult {
  id: string;
  inserted: boolean;
  /** true = 行处于 purge_pending/purged，来件被整体跳过（不复活、不刷新镜像字段）。 */
  skippedPurged: boolean;
}

/** 人设 upsert：不存在 INSERT（status=active）；存在 UPDATE 镜像字段。
 *  - customer_id 只 COALESCE 填充，console 已做的归属绝不被覆盖；
 *  - status 不随来件变化（保持既有）；purge_pending/purged 的行**整行跳过**，
 *    返回 skippedPurged=true —— 已清除/清除中的人设不被同步复活。 */
export function upsertPersonaRow(
  row: PersonaRowInput,
  db: Database.Database = getLedgerDb()
): PersonaUpsertResult {
  const sourceSystem = s(row.source_system);
  const sourceKey = s(row.source_key);
  if (!sourceSystem || !sourceKey) {
    throw new TypeError("upsertPersonaRow: source_system + source_key required");
  }
  const p = {
    source_system: sourceSystem,
    source_key: sourceKey,
    customer_id: s(row.customer_id),
    display_name: s(row.display_name),
    slot_face: slotInt(row.slot_face),
    slot_voice: slotInt(row.slot_voice),
    slot_prompt: slotInt(row.slot_prompt),
    slot_knowledge: slotInt(row.slot_knowledge),
    slots_detail: s(row.slots_detail),
    tags: s(row.tags),
    created_at: s(row.created_at),
    updated_at: nowIso(),
    synced_at: nowIso(),
  };
  const tx = db.transaction((): PersonaUpsertResult => {
    const existing = db
      .prepare("SELECT id, status FROM personas WHERE source_system = ? AND source_key = ?")
      .get(sourceSystem, sourceKey) as { id: string; status: PersonaStatus } | undefined;
    if (!existing) {
      const id = row.id && isValidId(row.id, "prs") ? row.id : newId("prs");
      db.prepare(
        `INSERT INTO personas (id, customer_id, source_system, source_key, display_name, slot_face, slot_voice, slot_prompt, slot_knowledge, slots_detail, tags, status, created_at, updated_at, synced_at)
         VALUES (@id, @customer_id, @source_system, @source_key, @display_name, @slot_face, @slot_voice, @slot_prompt, @slot_knowledge, @slots_detail, @tags, 'active', COALESCE(@created_at, @updated_at), @updated_at, @synced_at)`
      ).run({ ...p, id });
      return { id, inserted: true, skippedPurged: false };
    }
    if (existing.status === "purge_pending" || existing.status === "purged") {
      return { id: existing.id, inserted: false, skippedPurged: true };
    }
    db.prepare(
      `UPDATE personas SET
         customer_id = COALESCE(customer_id, @customer_id),
         display_name = @display_name,
         slot_face = @slot_face, slot_voice = @slot_voice,
         slot_prompt = @slot_prompt, slot_knowledge = @slot_knowledge,
         slots_detail = @slots_detail, tags = @tags,
         created_at = COALESCE(created_at, @created_at),
         updated_at = @updated_at, synced_at = @synced_at
       WHERE source_system = @source_system AND source_key = @source_key`
    ).run(p);
    return { id: existing.id, inserted: false, skippedPurged: false };
  });
  return tx();
}

// ── 查询 ────────────────────────────────────────────────────────────
export interface PersonaFilter {
  /** 模糊匹配 source_key / display_name / id。 */
  q?: string;
  status?: string;
  customerId?: string;
  /** 经 persona_grants（未撤销）过滤：授权给某产品的人设。 */
  productId?: string;
  limit?: number;
  offset?: number;
}

export interface PersonaListRow extends PersonaRow {
  /** 未撤销的授权产品数。 */
  grant_count: number;
}

export interface PersonaListResult {
  rows: PersonaListRow[];
  total: number;
  limit: number;
  offset: number;
}

export function listPersonas(
  filter: PersonaFilter = {},
  db: Database.Database = getLedgerDb()
): PersonaListResult {
  const where: string[] = [];
  const params: Record<string, unknown> = {};
  if (s(filter.status)) {
    where.push("status = @status");
    params.status = s(filter.status);
  }
  if (s(filter.customerId)) {
    where.push("customer_id = @customerId");
    params.customerId = s(filter.customerId);
  }
  if (s(filter.q)) {
    where.push("(source_key LIKE @q OR display_name LIKE @q OR id LIKE @q)");
    params.q = `%${s(filter.q)}%`;
  }
  if (s(filter.productId)) {
    where.push(
      "EXISTS (SELECT 1 FROM persona_grants g WHERE g.persona_id = personas.id AND g.product_id = @productId AND g.revoked_at IS NULL)"
    );
    params.productId = s(filter.productId);
  }
  const cond = where.length ? ` WHERE ${where.join(" AND ")}` : "";
  const limit = Math.min(Math.max(1, Math.trunc(filter.limit ?? 100)), 500);
  const offset = Math.max(0, Math.trunc(filter.offset ?? 0));
  const total = (db.prepare(`SELECT COUNT(*) AS c FROM personas${cond}`).get(params) as { c: number }).c;
  const rows = db
    .prepare(
      `SELECT personas.*,
              (SELECT COUNT(*) FROM persona_grants g WHERE g.persona_id = personas.id AND g.revoked_at IS NULL) AS grant_count
       FROM personas${cond}
       ORDER BY COALESCE(created_at, '') DESC, id DESC
       LIMIT @limit OFFSET @offset`
    )
    .all({ ...params, limit, offset }) as PersonaListRow[];
  return { rows, total, limit, offset };
}

export interface PersonaDetail {
  persona: PersonaRow;
  /** 全部授权行（含已撤销，撤销行 revoked_at 非 NULL）。 */
  grants: PersonaGrantRow[];
  /** 全部 purge 指令行（发起过全域清除才有）。 */
  purges: PersonaPurgeRow[];
}

export function getPersona(id: string, db: Database.Database = getLedgerDb()): PersonaDetail | null {
  const persona = db.prepare("SELECT * FROM personas WHERE id = ?").get(id) as PersonaRow | undefined;
  if (!persona) return null;
  const grants = db
    .prepare("SELECT * FROM persona_grants WHERE persona_id = ? ORDER BY product_id ASC")
    .all(persona.id) as PersonaGrantRow[];
  const purges = db
    .prepare("SELECT * FROM persona_purges WHERE persona_id = ? ORDER BY id ASC")
    .all(persona.id) as PersonaPurgeRow[];
  return { persona, grants, purges };
}

// ── 授权（grant / revoke）───────────────────────────────────────────
export interface PersonaActionResult {
  ok: boolean;
  /** grant：授权本已存在（幂等）；revoke：确有生效授权被撤销。 */
  existed?: boolean;
  error?: string;
}

function isPersonaProductId(v: string): v is PersonaProductId {
  return (PERSONA_PRODUCT_IDS as readonly string[]).includes(v);
}

/** 给人设授权某产品（幂等）。purge_pending/purged 的人设冻结授权。写 audit persona.grant。 */
export function grantProduct(
  personaId: string,
  productId: string,
  db: Database.Database = getLedgerDb(),
  actor = "console",
  scope: string | null = null
): PersonaActionResult {
  if (!isPersonaProductId(productId)) {
    return { ok: false, error: `unknown product_id: ${productId}` };
  }
  const tx = db.transaction((): PersonaActionResult => {
    const persona = db.prepare("SELECT id, status FROM personas WHERE id = ?").get(personaId) as
      | { id: string; status: PersonaStatus }
      | undefined;
    if (!persona) return { ok: false, error: "persona not found" };
    if (persona.status === "purge_pending" || persona.status === "purged") {
      return { ok: false, error: `persona is ${persona.status}, grants frozen` };
    }
    const existing = db
      .prepare("SELECT id, revoked_at FROM persona_grants WHERE persona_id = ? AND product_id = ?")
      .get(personaId, productId) as { id: number; revoked_at: string | null } | undefined;
    if (existing && existing.revoked_at === null) {
      return { ok: true, existed: true };
    }
    const t = nowIso();
    if (existing) {
      db.prepare(
        "UPDATE persona_grants SET scope = ?, granted_by = ?, granted_at = ?, revoked_at = NULL WHERE id = ?"
      ).run(s(scope), actor, t, existing.id);
    } else {
      db.prepare(
        "INSERT INTO persona_grants (persona_id, product_id, scope, granted_by, granted_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)"
      ).run(personaId, productId, s(scope), actor, t);
    }
    writeAudit(
      {
        actor,
        action: "persona.grant",
        entity: "persona",
        entity_id: personaId,
        detail: { product_id: productId, scope: s(scope), re_grant: !!existing },
      },
      db
    );
    return { ok: true, existed: false };
  });
  return tx();
}

/** 撤销人设对某产品的授权（幂等：无生效授权时 no-op）。写 audit persona.revoke。 */
export function revokeProduct(
  personaId: string,
  productId: string,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): PersonaActionResult {
  const tx = db.transaction((): PersonaActionResult => {
    const persona = db.prepare("SELECT id FROM personas WHERE id = ?").get(personaId) as
      | { id: string }
      | undefined;
    if (!persona) return { ok: false, error: "persona not found" };
    const changes = db
      .prepare(
        "UPDATE persona_grants SET revoked_at = ? WHERE persona_id = ? AND product_id = ? AND revoked_at IS NULL"
      )
      .run(nowIso(), personaId, productId).changes;
    if (!changes) return { ok: true, existed: false };
    writeAudit(
      { actor, action: "persona.revoke", entity: "persona", entity_id: personaId, detail: { product_id: productId } },
      db
    );
    return { ok: true, existed: true };
  });
  return tx();
}

// ── 客户归属 ────────────────────────────────────────────────────────
/** 人设归属到客户（console 手工操作，可改归属）。写 audit persona.assign_customer。 */
export function assignPersonaCustomer(
  personaId: string,
  customerId: string,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): boolean {
  const tx = db.transaction((): boolean => {
    const cust = db.prepare("SELECT id FROM customers WHERE id = ?").get(customerId) as
      | { id: string }
      | undefined;
    if (!cust) return false;
    const changes = db
      .prepare("UPDATE personas SET customer_id = ?, updated_at = ? WHERE id = ?")
      .run(customerId, nowIso(), personaId).changes;
    if (!changes) return false;
    writeAudit(
      {
        actor,
        action: "persona.assign_customer",
        entity: "persona",
        entity_id: personaId,
        detail: { customer_id: customerId },
      },
      db
    );
    return true;
  });
  return tx();
}

// ── 全域清除（purge 协议）───────────────────────────────────────────
/** 计算 purge 目标系统：persona 的 source_system + grants（含已撤销——撤销不等于
 *  引擎侧已删资产）推导的各产品承载引擎，去重。 */
export function computePurgeTargets(
  persona: Pick<PersonaRow, "source_system">,
  grants: Pick<PersonaGrantRow, "product_id">[]
): string[] {
  const targets = new Set<string>([persona.source_system]);
  for (const g of grants) {
    if (isPersonaProductId(g.product_id)) targets.add(PRODUCT_ENGINE_MAP[g.product_id]);
  }
  return [...targets].sort();
}

export interface RequestPurgeResult {
  ok: boolean;
  error?: string;
  /** 本次下发清除指令的目标系统列表。 */
  targets?: string[];
}

/** 发起全域清除：status → purge_pending，并为每个目标系统插一条 persona_purges 指令。
 *  写 audit persona.purge_request（detail 含 targets 与授权产品）。 */
export function requestPurge(
  personaId: string,
  actor: string,
  db: Database.Database = getLedgerDb()
): RequestPurgeResult {
  const tx = db.transaction((): RequestPurgeResult => {
    const persona = db
      .prepare("SELECT id, source_system, status FROM personas WHERE id = ?")
      .get(personaId) as Pick<PersonaRow, "id" | "source_system" | "status"> | undefined;
    if (!persona) return { ok: false, error: "persona not found" };
    if (persona.status === "purged") return { ok: false, error: "persona already purged" };
    if (persona.status === "purge_pending") return { ok: false, error: "purge already pending" };
    const grants = db
      .prepare("SELECT product_id FROM persona_grants WHERE persona_id = ?")
      .all(personaId) as Pick<PersonaGrantRow, "product_id">[];
    const targets = computePurgeTargets(persona, grants);
    const t = nowIso();
    const ins = db.prepare(
      "INSERT INTO persona_purges (persona_id, requested_by, requested_at, target_system, acked_at, ack_detail) VALUES (?, ?, ?, ?, NULL, NULL)"
    );
    for (const target of targets) ins.run(personaId, actor, t, target);
    db.prepare("UPDATE personas SET status = 'purge_pending', updated_at = ? WHERE id = ?").run(t, personaId);
    writeAudit(
      {
        actor,
        action: "persona.purge_request",
        entity: "persona",
        entity_id: personaId,
        detail: { targets, granted_products: grants.map((g) => g.product_id) },
      },
      db
    );
    return { ok: true, targets };
  });
  return tx();
}

export interface AckPurgeResult {
  ok: boolean;
  error?: string;
  purgeId?: number;
  personaId?: string;
  targetSystem?: string;
  /** true = 该指令此前已 ack 过（幂等重放，首次回执保留）。 */
  already?: boolean;
  /** 该 persona 全部指令是否都已 ack。 */
  allAcked?: boolean;
  personaStatus?: PersonaStatus;
}

/** 引擎回执：标记单条 purge 指令已执行。该 persona 全部指令 ack 后 status → purged。
 *  写 audit persona.purge_ack（每次）与 persona.purged（收口时）。 */
export function ackPurge(
  purgeId: number,
  detail: unknown,
  db: Database.Database = getLedgerDb(),
  actor = "sync"
): AckPurgeResult {
  const tx = db.transaction((): AckPurgeResult => {
    const row = db.prepare("SELECT * FROM persona_purges WHERE id = ?").get(purgeId) as
      | PersonaPurgeRow
      | undefined;
    if (!row) return { ok: false, error: "purge not found" };
    const statusOf = () =>
      (db.prepare("SELECT status FROM personas WHERE id = ?").get(row.persona_id) as
        | { status: PersonaStatus }
        | undefined)?.status;
    const unackedCount = () =>
      (db
        .prepare("SELECT COUNT(*) AS c FROM persona_purges WHERE persona_id = ? AND acked_at IS NULL")
        .get(row.persona_id) as { c: number }).c;
    if (row.acked_at) {
      return {
        ok: true,
        already: true,
        purgeId: row.id,
        personaId: row.persona_id,
        targetSystem: row.target_system,
        allAcked: unackedCount() === 0,
        personaStatus: statusOf(),
      };
    }
    const t = nowIso();
    const ackDetail =
      detail === undefined || detail === null
        ? null
        : typeof detail === "string"
          ? detail
          : JSON.stringify(detail);
    db.prepare("UPDATE persona_purges SET acked_at = ?, ack_detail = ? WHERE id = ?").run(t, ackDetail, row.id);
    writeAudit(
      {
        actor,
        action: "persona.purge_ack",
        entity: "persona",
        entity_id: row.persona_id,
        detail: { purge_id: row.id, target_system: row.target_system, ack_detail: ackDetail },
      },
      db
    );
    const allAcked = unackedCount() === 0;
    if (allAcked) {
      const changes = db
        .prepare("UPDATE personas SET status = 'purged', updated_at = ? WHERE id = ? AND status = 'purge_pending'")
        .run(t, row.persona_id).changes;
      if (changes > 0) {
        writeAudit(
          { actor, action: "persona.purged", entity: "persona", entity_id: row.persona_id },
          db
        );
      }
    }
    return {
      ok: true,
      already: false,
      purgeId: row.id,
      personaId: row.persona_id,
      targetSystem: row.target_system,
      allAcked,
      personaStatus: statusOf(),
    };
  });
  return tx();
}

/** 某引擎的未 ack 清除指令（机器通道 GET 消费）。只带 persona 元数据与槽位布尔，
 *  不带客户数据。 */
export interface PendingPurgeDirective {
  purge_id: number;
  persona_id: string;
  source_system: string;
  source_key: string;
  requested_at: string | null;
  slots: Record<PersonaSlot, boolean>;
}

export function listPendingPurges(
  targetSystem: string,
  db: Database.Database = getLedgerDb()
): PendingPurgeDirective[] {
  const rows = db
    .prepare(
      `SELECT pp.id AS purge_id, pp.persona_id, pp.requested_at,
              p.source_system, p.source_key,
              p.slot_face, p.slot_voice, p.slot_prompt, p.slot_knowledge
       FROM persona_purges pp
       JOIN personas p ON p.id = pp.persona_id
       WHERE pp.target_system = ? AND pp.acked_at IS NULL
       ORDER BY pp.id ASC`
    )
    .all(targetSystem) as {
    purge_id: number;
    persona_id: string;
    requested_at: string | null;
    source_system: string;
    source_key: string;
    slot_face: number;
    slot_voice: number;
    slot_prompt: number;
    slot_knowledge: number;
  }[];
  return rows.map((r) => ({
    purge_id: r.purge_id,
    persona_id: r.persona_id,
    source_system: r.source_system,
    source_key: r.source_key,
    requested_at: r.requested_at,
    slots: {
      face: !!r.slot_face,
      voice: !!r.slot_voice,
      prompt: !!r.slot_prompt,
      knowledge: !!r.slot_knowledge,
    },
  }));
}

// ── 统计 ────────────────────────────────────────────────────────────
export interface PersonaStats {
  total: number;
  byStatus: Record<string, number>;
  /** 各槽位点亮的人设数。 */
  slots: Record<PersonaSlot, number>;
  purgePending: number;
  generatedAt: string;
}

export function getPersonaStats(db: Database.Database = getLedgerDb()): PersonaStats {
  const total = (db.prepare("SELECT COUNT(*) AS c FROM personas").get() as { c: number }).c;
  const byStatus: Record<string, number> = {};
  for (const r of db
    .prepare("SELECT status, COUNT(*) AS c FROM personas GROUP BY status")
    .all() as { status: string; c: number }[]) {
    byStatus[r.status] = r.c;
  }
  const slotAgg = db
    .prepare(
      `SELECT COALESCE(SUM(slot_face), 0) AS face, COALESCE(SUM(slot_voice), 0) AS voice,
              COALESCE(SUM(slot_prompt), 0) AS prompt, COALESCE(SUM(slot_knowledge), 0) AS knowledge
       FROM personas`
    )
    .get() as Record<PersonaSlot, number>;
  return {
    total,
    byStatus,
    slots: { face: slotAgg.face, voice: slotAgg.voice, prompt: slotAgg.prompt, knowledge: slotAgg.knowledge },
    purgePending: byStatus.purge_pending ?? 0,
    generatedAt: nowIso(),
  };
}
