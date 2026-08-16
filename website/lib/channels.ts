// 渠道账号台账（Channel Accounts）数据层 —— schema v5：channel_accounts。
//
// 定位：多平台对外账号登记（渠道账号架构 2026-07「纪律三条」之 3：新号必须登记
// 哪个平台、哪个实例、哪个用途、谁保管）。台账只存元数据：label/handle 是显示名
// 与号码/用户名，session_ref 是登录态文件/凭据位置的**纯文本备注**（如
// sessions/639952947442.session 在智聊实例数据根）——绝不存密钥、密码、session 本体。
//
// 状态语义（软状态变更，不物理删除）：active 在用 / paused 暂停 / revoked 已弃用
// （封号/换号后留档） / pending 待启用（已注册未挂实例）。
// 写操作全部写 audit（entity="channel_account"），console API 从这里走。

import type Database from "better-sqlite3";
import { getLedgerDb, writeAudit } from "./ledger";
import { newId } from "./ids";

// ── 枚举 ────────────────────────────────────────────────────────────
export const CHANNEL_PLATFORMS = ["telegram", "whatsapp", "messenger", "line", "web", "other"] as const;
export type ChannelPlatform = (typeof CHANNEL_PLATFORMS)[number];

/** 挂载实例：一号一实例是铁律（同一登录态被两个进程用会互踢+风控）。none = 尚未挂载。 */
export const CHANNEL_INSTANCES = ["zhiliao", "tongyi", "avatarhub", "huoke", "website", "none"] as const;
export type ChannelInstance = (typeof CHANNEL_INSTANCES)[number];

export const CHANNEL_PURPOSES = ["总机接待", "交付服务", "测试", "投放专号", "其他"] as const;
export type ChannelPurpose = (typeof CHANNEL_PURPOSES)[number];

export const CHANNEL_STATUSES = ["active", "paused", "revoked", "pending"] as const;
export type ChannelStatus = (typeof CHANNEL_STATUSES)[number];

export function isChannelPlatform(v: string): v is ChannelPlatform {
  return (CHANNEL_PLATFORMS as readonly string[]).includes(v);
}
export function isChannelInstance(v: string): v is ChannelInstance {
  return (CHANNEL_INSTANCES as readonly string[]).includes(v);
}
export function isChannelPurpose(v: string): v is ChannelPurpose {
  return (CHANNEL_PURPOSES as readonly string[]).includes(v);
}
export function isChannelStatus(v: string): v is ChannelStatus {
  return (CHANNEL_STATUSES as readonly string[]).includes(v);
}

// ── 行类型 ──────────────────────────────────────────────────────────
export interface ChannelAccountRow {
  id: string;
  platform: ChannelPlatform;
  /** 显示名，如「官方总机」。 */
  label: string;
  /** 号码/用户名，如 +639757135247、@boundless_hq。 */
  handle: string | null;
  instance: ChannelInstance;
  purpose: ChannelPurpose;
  /** 保管人（谁拿着手机号/收验证码）。 */
  holder: string | null;
  status: ChannelStatus;
  /** 登录态文件/凭据位置备注（纯文本，不存任何密钥）。 */
  session_ref: string | null;
  notes: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ── 取值规整（与 ledger.ts 同款约定）────────────────────────────────
function s(v: unknown): string | null {
  if (v === undefined || v === null) return null;
  const t = String(v).trim();
  return t === "" ? null : t;
}
const nowIso = () => new Date().toISOString();

// ── 查询 ────────────────────────────────────────────────────────────
export interface ChannelAccountFilter {
  platform?: string;
  status?: string;
  /** 模糊匹配 label / handle / holder / session_ref / id。 */
  q?: string;
  limit?: number;
  offset?: number;
}

export interface ChannelAccountListResult {
  rows: ChannelAccountRow[];
  total: number;
  limit: number;
  offset: number;
}

export function listChannelAccounts(
  filter: ChannelAccountFilter = {},
  db: Database.Database = getLedgerDb()
): ChannelAccountListResult {
  const where: string[] = [];
  const params: Record<string, unknown> = {};
  if (s(filter.platform)) {
    where.push("platform = @platform");
    params.platform = s(filter.platform);
  }
  if (s(filter.status)) {
    where.push("status = @status");
    params.status = s(filter.status);
  }
  if (s(filter.q)) {
    where.push("(label LIKE @q OR handle LIKE @q OR holder LIKE @q OR session_ref LIKE @q OR id LIKE @q)");
    params.q = `%${s(filter.q)}%`;
  }
  const cond = where.length ? ` WHERE ${where.join(" AND ")}` : "";
  const limit = Math.min(Math.max(1, Math.trunc(filter.limit ?? 100)), 500);
  const offset = Math.max(0, Math.trunc(filter.offset ?? 0));
  const total = (db.prepare(`SELECT COUNT(*) AS c FROM channel_accounts${cond}`).get(params) as { c: number }).c;
  const rows = db
    .prepare(
      `SELECT * FROM channel_accounts${cond}
       ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,
                COALESCE(created_at, '') DESC, id DESC
       LIMIT @limit OFFSET @offset`
    )
    .all({ ...params, limit, offset }) as ChannelAccountRow[];
  return { rows, total, limit, offset };
}

export function getChannelAccount(id: string, db: Database.Database = getLedgerDb()): ChannelAccountRow | null {
  const row = db.prepare("SELECT * FROM channel_accounts WHERE id = ?").get(id) as ChannelAccountRow | undefined;
  return row ?? null;
}

// ── 统计（页面统计卡：按平台 / 按状态计数）──────────────────────────
export interface ChannelAccountStats {
  total: number;
  byPlatform: Record<string, number>;
  byStatus: Record<string, number>;
  generatedAt: string;
}

export function getChannelAccountStats(db: Database.Database = getLedgerDb()): ChannelAccountStats {
  const total = (db.prepare("SELECT COUNT(*) AS c FROM channel_accounts").get() as { c: number }).c;
  const byPlatform: Record<string, number> = {};
  for (const r of db
    .prepare("SELECT platform, COUNT(*) AS c FROM channel_accounts GROUP BY platform")
    .all() as { platform: string; c: number }[]) {
    byPlatform[r.platform] = r.c;
  }
  const byStatus: Record<string, number> = {};
  for (const r of db
    .prepare("SELECT status, COUNT(*) AS c FROM channel_accounts GROUP BY status")
    .all() as { status: string; c: number }[]) {
    byStatus[r.status] = r.c;
  }
  return { total, byPlatform, byStatus, generatedAt: nowIso() };
}

// ── 创建 ────────────────────────────────────────────────────────────
export interface CreateChannelAccountInput {
  platform: string;
  label: string;
  handle?: string | null;
  instance?: string | null;
  purpose?: string | null;
  holder?: string | null;
  status?: string | null;
  session_ref?: string | null;
  notes?: string | null;
}

/** 登记新渠道账号。platform/label 必填，枚举字段非法值抛 TypeError（API 层转 400）。
 *  写 audit channel_account.create。 */
export function createChannelAccount(
  input: CreateChannelAccountInput,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): ChannelAccountRow {
  const platform = s(input.platform);
  const label = s(input.label);
  if (!platform || !isChannelPlatform(platform)) {
    throw new TypeError(`createChannelAccount: bad platform ${platform ?? "(empty)"} (expect ${CHANNEL_PLATFORMS.join("|")})`);
  }
  if (!label) throw new TypeError("createChannelAccount: label required");
  const instance = s(input.instance) ?? "none";
  if (!isChannelInstance(instance)) {
    throw new TypeError(`createChannelAccount: bad instance ${instance} (expect ${CHANNEL_INSTANCES.join("|")})`);
  }
  const purpose = s(input.purpose) ?? "其他";
  if (!isChannelPurpose(purpose)) {
    throw new TypeError(`createChannelAccount: bad purpose ${purpose} (expect ${CHANNEL_PURPOSES.join("|")})`);
  }
  const status = s(input.status) ?? "active";
  if (!isChannelStatus(status)) {
    throw new TypeError(`createChannelAccount: bad status ${status} (expect ${CHANNEL_STATUSES.join("|")})`);
  }
  const t = nowIso();
  const row: ChannelAccountRow = {
    id: newId("cha"),
    platform,
    label,
    handle: s(input.handle),
    instance,
    purpose,
    holder: s(input.holder),
    status,
    session_ref: s(input.session_ref),
    notes: s(input.notes),
    created_at: t,
    updated_at: t,
  };
  const tx = db.transaction(() => {
    db.prepare(
      `INSERT INTO channel_accounts (id, platform, label, handle, instance, purpose, holder, status, session_ref, notes, created_at, updated_at)
       VALUES (@id, @platform, @label, @handle, @instance, @purpose, @holder, @status, @session_ref, @notes, @created_at, @updated_at)`
    ).run(row);
    writeAudit(
      { actor, action: "channel_account.create", entity: "channel_account", entity_id: row.id, detail: row },
      db
    );
  });
  tx();
  return row;
}

// ── 更新（部分字段；status 走 setChannelAccountStatus）──────────────
export interface UpdateChannelAccountPatch {
  platform?: string;
  label?: string;
  handle?: string | null;
  instance?: string;
  purpose?: string;
  holder?: string | null;
  session_ref?: string | null;
  notes?: string | null;
}

const UPDATABLE_FIELDS = ["platform", "label", "handle", "instance", "purpose", "holder", "session_ref", "notes"] as const;

/** 更新渠道账号资料字段（传入哪个改哪个；handle/holder/session_ref/notes 传空串 = 清空）。
 *  枚举字段非法值抛 TypeError；行不存在返回 null。写 audit channel_account.update（只记变更字段）。 */
export function updateChannelAccount(
  id: string,
  patch: UpdateChannelAccountPatch,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): ChannelAccountRow | null {
  const tx = db.transaction((): ChannelAccountRow | null => {
    const existing = getChannelAccount(id, db);
    if (!existing) return null;
    const changed: Record<string, string | null> = {};
    for (const field of UPDATABLE_FIELDS) {
      if (!(field in patch)) continue;
      const raw = s((patch as Record<string, unknown>)[field]);
      if (field === "platform") {
        if (!raw || !isChannelPlatform(raw)) {
          throw new TypeError(`updateChannelAccount: bad platform ${raw ?? "(empty)"} (expect ${CHANNEL_PLATFORMS.join("|")})`);
        }
      } else if (field === "label") {
        if (!raw) throw new TypeError("updateChannelAccount: label required");
      } else if (field === "instance") {
        if (!raw || !isChannelInstance(raw)) {
          throw new TypeError(`updateChannelAccount: bad instance ${raw ?? "(empty)"} (expect ${CHANNEL_INSTANCES.join("|")})`);
        }
      } else if (field === "purpose") {
        if (!raw || !isChannelPurpose(raw)) {
          throw new TypeError(`updateChannelAccount: bad purpose ${raw ?? "(empty)"} (expect ${CHANNEL_PURPOSES.join("|")})`);
        }
      }
      if (raw !== existing[field]) changed[field] = raw;
    }
    if (Object.keys(changed).length === 0) return existing;
    const sets = Object.keys(changed).map((k) => `${k} = @${k}`).join(", ");
    db.prepare(`UPDATE channel_accounts SET ${sets}, updated_at = @updated_at WHERE id = @id`).run({
      ...changed,
      updated_at: nowIso(),
      id,
    });
    writeAudit(
      { actor, action: "channel_account.update", entity: "channel_account", entity_id: id, detail: { changed } },
      db
    );
    return getChannelAccount(id, db);
  });
  return tx();
}

// ── 软状态变更 ──────────────────────────────────────────────────────
export interface SetChannelStatusResult {
  ok: boolean;
  /** true = 状态本来就是目标值（幂等 no-op，不写 audit）。 */
  unchanged?: boolean;
  row?: ChannelAccountRow;
  error?: string;
}

/** 软状态变更（active/paused/revoked/pending，不物理删除）。写 audit channel_account.set_status
 *  （detail 含 from/to）。 */
export function setChannelAccountStatus(
  id: string,
  status: string,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): SetChannelStatusResult {
  if (!isChannelStatus(status)) {
    return { ok: false, error: `unknown status: ${status} (expect ${CHANNEL_STATUSES.join("|")})` };
  }
  const tx = db.transaction((): SetChannelStatusResult => {
    const existing = getChannelAccount(id, db);
    if (!existing) return { ok: false, error: "channel account not found" };
    if (existing.status === status) return { ok: true, unchanged: true, row: existing };
    db.prepare("UPDATE channel_accounts SET status = ?, updated_at = ? WHERE id = ?").run(status, nowIso(), id);
    writeAudit(
      {
        actor,
        action: "channel_account.set_status",
        entity: "channel_account",
        entity_id: id,
        detail: { from: existing.status, to: status },
      },
      db
    );
    return { ok: true, unchanged: false, row: getChannelAccount(id, db) ?? undefined };
  });
  return tx();
}
