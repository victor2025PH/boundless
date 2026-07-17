// 集团控制台实名账号 + 会话（schema v2：users / sessions 表）。
//
// 密码散列：node:crypto scryptSync（N=16384, r=8, p=1, 64 字节），参数编码进
// pw_hash（"scrypt$N$r$p$<hex>"），未来升参不破坏旧账号；比对走 timingSafeEqual。
// 会话：登录发 32 字节随机 token（hex，仅存在 cookie 里），库中只存 sha256(token)
// —— 拖库拿不到可用凭证。验证时查 token_hash、未 revoked、未过期、用户未禁用，
// 并滑动刷新 last_seen（expires_at 固定，绝对 12h 生命期）。
// 所有账号/会话变更写 audit（复用 ledger.writeAudit，action 前缀 user./session.）。

import { createHash, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
import type Database from "better-sqlite3";
import { getLedgerDb, writeAudit } from "./ledger";
import { newId } from "./ids";

// ── 角色 ────────────────────────────────────────────────────────────
export const CONSOLE_ROLES = ["viewer", "admin", "master"] as const;
export type ConsoleRole = (typeof CONSOLE_ROLES)[number];

const ROLE_RANK: Record<ConsoleRole, number> = { viewer: 0, admin: 1, master: 2 };

export function isConsoleRole(v: unknown): v is ConsoleRole {
  return typeof v === "string" && (CONSOLE_ROLES as readonly string[]).includes(v);
}

/** viewer < admin < master。 */
export function roleAtLeast(role: ConsoleRole, min: ConsoleRole): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[min];
}

// ── 行类型 ──────────────────────────────────────────────────────────
export interface ConsoleUserRow {
  id: string;
  username: string;
  pw_salt: string;
  pw_hash: string;
  role: ConsoleRole;
  display_name: string | null;
  enabled: number; // 0/1
  created_at: string | null;
  last_login: string | null;
}

/** 对外（列表/接口）安全形态：绝不带散列字段。 */
export interface ConsoleUserPublic {
  id: string;
  username: string;
  role: ConsoleRole;
  display_name: string | null;
  enabled: boolean;
  created_at: string | null;
  last_login: string | null;
}

export interface SessionRow {
  token_hash: string;
  user_id: string;
  created_at: string | null;
  last_seen: string | null;
  expires_at: string | null;
  revoked: number; // 0/1
  ip: string | null;
  ua: string | null;
}

export function toPublicUser(u: ConsoleUserRow): ConsoleUserPublic {
  return {
    id: u.id,
    username: u.username,
    role: u.role,
    display_name: u.display_name,
    enabled: !!u.enabled,
    created_at: u.created_at,
    last_login: u.last_login,
  };
}

// ── 密码散列（scrypt）───────────────────────────────────────────────
const SCRYPT_N = 16384; // 2^14，交互式登录标准参数
const SCRYPT_R = 8;
const SCRYPT_P = 1;
const SCRYPT_KEYLEN = 64;
const SCRYPT_MAXMEM = 64 * 1024 * 1024; // 显式放宽，防参数升档触发默认 32MiB 上限

const nowIso = () => new Date().toISOString();

function scryptHex(password: string, saltHex: string, N: number, r: number, p: number): string {
  return scryptSync(password, Buffer.from(saltHex, "hex"), SCRYPT_KEYLEN, {
    N,
    r,
    p,
    maxmem: SCRYPT_MAXMEM,
  }).toString("hex");
}

/** 生成 {salt, hash}：hash 形如 "scrypt$16384$8$1$<hex>"，参数自描述。 */
export function hashPassword(password: string): { salt: string; hash: string } {
  const salt = randomBytes(16).toString("hex");
  const digest = scryptHex(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P);
  return { salt, hash: `scrypt$${SCRYPT_N}$${SCRYPT_R}$${SCRYPT_P}$${digest}` };
}

/** 恒定时间比对；hash 格式非法时返回 false（不 throw，登录路径要稳）。 */
export function checkPassword(password: string, saltHex: string, stored: string): boolean {
  const parts = stored.split("$");
  if (parts.length !== 5 || parts[0] !== "scrypt") return false;
  const N = Number(parts[1]);
  const r = Number(parts[2]);
  const p = Number(parts[3]);
  const expected = parts[4];
  if (!Number.isFinite(N) || !Number.isFinite(r) || !Number.isFinite(p) || !expected) return false;
  try {
    const actual = scryptHex(password, saltHex, N, r, p);
    const a = Buffer.from(actual, "hex");
    const b = Buffer.from(expected, "hex");
    return a.length === b.length && timingSafeEqual(a, b);
  } catch {
    return false;
  }
}

// ── 用户 ────────────────────────────────────────────────────────────
export const USERNAME_REGEX = /^[a-z0-9][a-z0-9_.-]{1,31}$/;
export const MIN_PASSWORD_LEN = 8;

/** 用户名归一化：trim + 小写（唯一性对大小写不敏感）。 */
export function normUsername(v: unknown): string {
  return String(v ?? "").trim().toLowerCase();
}

export function countUsers(db: Database.Database = getLedgerDb()): number {
  return (db.prepare("SELECT COUNT(*) AS c FROM users").get() as { c: number }).c;
}

export function getUserById(id: string, db: Database.Database = getLedgerDb()): ConsoleUserRow | null {
  const row = db.prepare("SELECT * FROM users WHERE id = ?").get(id) as ConsoleUserRow | undefined;
  return row ?? null;
}

export function getUserByUsername(username: string, db: Database.Database = getLedgerDb()): ConsoleUserRow | null {
  const row = db.prepare("SELECT * FROM users WHERE username = ?").get(normUsername(username)) as
    | ConsoleUserRow
    | undefined;
  return row ?? null;
}

export function listUsers(db: Database.Database = getLedgerDb()): ConsoleUserPublic[] {
  const rows = db
    .prepare("SELECT * FROM users ORDER BY COALESCE(created_at, '') ASC, id ASC")
    .all() as ConsoleUserRow[];
  return rows.map(toPublicUser);
}

/** enabled 的 master 数（excludeUserId 用于"改动后还剩几个"预判）。 */
export function countEnabledMasters(excludeUserId?: string, db: Database.Database = getLedgerDb()): number {
  if (excludeUserId) {
    return (
      db
        .prepare("SELECT COUNT(*) AS c FROM users WHERE role = 'master' AND enabled = 1 AND id != ?")
        .get(excludeUserId) as { c: number }
    ).c;
  }
  return (db.prepare("SELECT COUNT(*) AS c FROM users WHERE role = 'master' AND enabled = 1").get() as { c: number }).c;
}

export interface CreateUserInput {
  username: string;
  password: string;
  role: ConsoleRole;
  display_name?: string | null;
}

/** 建用户（校验用户名格式/密码长度/重名），写 audit user.create（bootstrap 场景由调用方传 action 覆盖）。 */
export function createUser(
  input: CreateUserInput,
  db: Database.Database = getLedgerDb(),
  actor = "console",
  auditAction = "user.create"
): ConsoleUserPublic {
  const username = normUsername(input.username);
  if (!USERNAME_REGEX.test(username)) {
    throw new TypeError("username must be 2-32 chars: a-z 0-9 _ . - (starts with letter/digit)");
  }
  if (typeof input.password !== "string" || input.password.length < MIN_PASSWORD_LEN) {
    throw new TypeError(`password must be at least ${MIN_PASSWORD_LEN} chars`);
  }
  if (!isConsoleRole(input.role)) throw new TypeError(`bad role: ${input.role}`);
  const { salt, hash } = hashPassword(input.password);
  const row: ConsoleUserRow = {
    id: newId("usr"),
    username,
    pw_salt: salt,
    pw_hash: hash,
    role: input.role,
    display_name: input.display_name ? String(input.display_name).trim() || null : null,
    enabled: 1,
    created_at: nowIso(),
    last_login: null,
  };
  const tx = db.transaction(() => {
    const dupe = db.prepare("SELECT id FROM users WHERE username = ?").get(username);
    if (dupe) throw new Error(`username already exists: ${username}`);
    db.prepare(
      `INSERT INTO users (id, username, pw_salt, pw_hash, role, display_name, enabled, created_at, last_login)
       VALUES (@id, @username, @pw_salt, @pw_hash, @role, @display_name, @enabled, @created_at, @last_login)`
    ).run(row);
    writeAudit(
      {
        actor,
        action: auditAction,
        entity: "user",
        entity_id: row.id,
        detail: { username, role: row.role, display_name: row.display_name },
      },
      db
    );
  });
  tx();
  return toPublicUser(row);
}

/** 校验用户名+密码：匹配返回用户行（含 enabled=0 的，禁用判定交给调用方）；
 *  用户不存在时也跑一次假散列，抹平时间差防枚举。 */
export function verifyPassword(
  username: string,
  password: string,
  db: Database.Database = getLedgerDb()
): ConsoleUserRow | null {
  const user = getUserByUsername(username, db);
  if (!user) {
    checkPassword(password, "00000000000000000000000000000000", `scrypt$${SCRYPT_N}$${SCRYPT_R}$${SCRYPT_P}$00`);
    return null;
  }
  return checkPassword(password, user.pw_salt, user.pw_hash) ? user : null;
}

/** 重置密码（写 audit user.reset_password）。同时撤销该用户全部会话，旧凭证立即作废。 */
export function setPassword(
  userId: string,
  newPassword: string,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): void {
  if (typeof newPassword !== "string" || newPassword.length < MIN_PASSWORD_LEN) {
    throw new TypeError(`password must be at least ${MIN_PASSWORD_LEN} chars`);
  }
  const { salt, hash } = hashPassword(newPassword);
  const tx = db.transaction(() => {
    const changes = db
      .prepare("UPDATE users SET pw_salt = ?, pw_hash = ? WHERE id = ?")
      .run(salt, hash, userId).changes;
    if (!changes) throw new Error(`user not found: ${userId}`);
    revokeAllForUser(userId, db, actor, "reset_password");
    writeAudit({ actor, action: "user.reset_password", entity: "user", entity_id: userId }, db);
  });
  tx();
}

/** 启用/禁用（写 audit user.set_enabled）。禁用时撤销全部会话——被禁账号的会话立即失效。 */
export function setEnabled(
  userId: string,
  enabled: boolean,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): void {
  const tx = db.transaction(() => {
    const changes = db.prepare("UPDATE users SET enabled = ? WHERE id = ?").run(enabled ? 1 : 0, userId).changes;
    if (!changes) throw new Error(`user not found: ${userId}`);
    if (!enabled) revokeAllForUser(userId, db, actor, "user_disabled");
    writeAudit({ actor, action: "user.set_enabled", entity: "user", entity_id: userId, detail: { enabled } }, db);
  });
  tx();
}

/** 改角色（写 audit user.set_role）。降级 master 时会话保留——权限即时生效由 verifySession 带回实时 role 保证。 */
export function setRole(
  userId: string,
  role: ConsoleRole,
  db: Database.Database = getLedgerDb(),
  actor = "console"
): void {
  if (!isConsoleRole(role)) throw new TypeError(`bad role: ${role}`);
  const tx = db.transaction(() => {
    const changes = db.prepare("UPDATE users SET role = ? WHERE id = ?").run(role, userId).changes;
    if (!changes) throw new Error(`user not found: ${userId}`);
    writeAudit({ actor, action: "user.set_role", entity: "user", entity_id: userId, detail: { role } }, db);
  });
  tx();
}

/** 登录成功后刷新 last_login。 */
export function touchLogin(userId: string, db: Database.Database = getLedgerDb()): void {
  db.prepare("UPDATE users SET last_login = ? WHERE id = ?").run(nowIso(), userId);
}

// ── 会话 ────────────────────────────────────────────────────────────
export const SESSION_TTL_MS = 12 * 60 * 60 * 1000; // 12h，与 cookie maxAge 一致

function hashToken(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

export interface CreateSessionResult {
  /** 原始 token（只出现在 Set-Cookie，库里不存）。 */
  token: string;
  expiresAt: string;
}

export function createSession(
  userId: string,
  opts: { ip?: string | null; ua?: string | null } = {},
  db: Database.Database = getLedgerDb()
): CreateSessionResult {
  const token = randomBytes(32).toString("hex");
  const t = nowIso();
  const expiresAt = new Date(Date.now() + SESSION_TTL_MS).toISOString();
  db.prepare(
    `INSERT INTO sessions (token_hash, user_id, created_at, last_seen, expires_at, revoked, ip, ua)
     VALUES (?, ?, ?, ?, ?, 0, ?, ?)`
  ).run(hashToken(token), userId, t, t, expiresAt, opts.ip ?? null, (opts.ua ?? "").slice(0, 256) || null);
  return { token, expiresAt };
}

export interface VerifiedSession {
  user: ConsoleUserRow;
  session: SessionRow;
}

/** 会话校验：token_hash 命中 + 未 revoked + 未过期 + 用户未禁用。
 *  命中即滑动刷新 last_seen（expires_at 固定不延，12h 绝对生命期）。 */
export function verifySession(token: string, db: Database.Database = getLedgerDb()): VerifiedSession | null {
  if (!token || token.length < 32) return null;
  const th = hashToken(token);
  const row = db
    .prepare(
      `SELECT s.token_hash, s.user_id, s.created_at, s.last_seen, s.expires_at, s.revoked, s.ip, s.ua,
              u.id AS u_id, u.username, u.pw_salt, u.pw_hash, u.role, u.display_name, u.enabled,
              u.created_at AS u_created_at, u.last_login
       FROM sessions s JOIN users u ON u.id = s.user_id
       WHERE s.token_hash = ?`
    )
    .get(th) as
    | (SessionRow & {
        u_id: string;
        username: string;
        pw_salt: string;
        pw_hash: string;
        role: ConsoleRole;
        display_name: string | null;
        enabled: number;
        u_created_at: string | null;
        last_login: string | null;
      })
    | undefined;
  if (!row) return null;
  if (row.revoked) return null;
  if (!row.expires_at || row.expires_at <= nowIso()) return null;
  if (!row.enabled) return null;
  db.prepare("UPDATE sessions SET last_seen = ? WHERE token_hash = ?").run(nowIso(), th);
  return {
    user: {
      id: row.u_id,
      username: row.username,
      pw_salt: row.pw_salt,
      pw_hash: row.pw_hash,
      role: row.role,
      display_name: row.display_name,
      enabled: row.enabled,
      created_at: row.u_created_at,
      last_login: row.last_login,
    },
    session: {
      token_hash: row.token_hash,
      user_id: row.user_id,
      created_at: row.created_at,
      last_seen: row.last_seen,
      expires_at: row.expires_at,
      revoked: row.revoked,
      ip: row.ip,
      ua: row.ua,
    },
  };
}

/** 按原始 token 撤销单个会话（登出）。写 audit session.revoke。 */
export function revokeSession(token: string, db: Database.Database = getLedgerDb(), actor = "console"): boolean {
  const th = hashToken(token);
  const row = db.prepare("SELECT user_id FROM sessions WHERE token_hash = ? AND revoked = 0").get(th) as
    | { user_id: string }
    | undefined;
  if (!row) return false;
  db.prepare("UPDATE sessions SET revoked = 1 WHERE token_hash = ?").run(th);
  writeAudit(
    { actor, action: "session.revoke", entity: "user", entity_id: row.user_id, detail: { reason: "logout" } },
    db
  );
  return true;
}

/** 撤销某用户全部未撤销会话（禁用/重置密码时调用）。写 audit session.revoke（带条数与原因）。 */
export function revokeAllForUser(
  userId: string,
  db: Database.Database = getLedgerDb(),
  actor = "console",
  reason = "revoke_all"
): number {
  const changes = db.prepare("UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0").run(userId).changes;
  if (changes > 0) {
    writeAudit(
      { actor, action: "session.revoke", entity: "user", entity_id: userId, detail: { reason, count: changes } },
      db
    );
  }
  return changes;
}
