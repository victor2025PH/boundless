/**
 * 公网 Telegram 凭据池（2026-07-29）——「用户只登录、不填 api_id/api_hash」的公网解。
 *
 * 背景：集团 LAN 凭据池（credpool_stage.py @ 117:8000）只绑 localhost，连局域网都不
 * 派发，公网托管版更够不着 → 桌面首启向导只能退化成让用户自己去 my.telegram.org 申请
 * （接入转化率最大黑洞）。本模块把凭据池搬到官网网关，与 AI 网关同构：
 *   秘密（api_id/api_hash 组）只在服务端 env，客户端凭设备令牌来领。
 *
 * 关键不变量（与 LAN 池一致，照抄其安全设计）：
 * - **粘定**：同一机器指纹永远分到同一 api_id（pyrogram session 与 api_id 绑定，
 *   换组 = Telegram 风控信号）。落 sqlite tg_cred_assign，跨请求恒定。
 * - **容量**：每组 api_id 有 max_accounts 上限（一个 api_id 挂太多号 = 封号信号）；
 *   新指纹按「当前占用最少且未满」的组分配；全满 → 拒发（error=pool_full）。
 * - **服务端持密**：POOL_TG_CREDS（JSON 数组）只在 VPS env；空 = 池禁用（暗态，
 *   与 DEEPSEEK_API_KEY 同：填了才活）。api_hash 绝不进日志。
 *
 * POOL_TG_CREDS 形如：
 *   [{"api_id":"123","api_hash":"abc…","max":50,"name":"grp-1"}, …]
 */
import crypto from "crypto";
import Database from "better-sqlite3";
import { mkdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { DATA_DIR } from "./data-dir";

const DB_PATH = process.env.AI_GATEWAY_QUOTA_DB || path.join(DATA_DIR, "ai-gateway.db");

export type TgCred = { api_id: string; api_hash: string; max: number; name: string };

let _db: Database.Database | null = null;
function db(): Database.Database {
  if (_db) return _db;
  mkdirSync(path.dirname(DB_PATH), { recursive: true });
  _db = new Database(DB_PATH);
  _db.pragma("journal_mode = WAL");
  _db.exec(
    "CREATE TABLE IF NOT EXISTS tg_cred_assign (" +
    " mid TEXT PRIMARY KEY, api_id TEXT NOT NULL, assigned_at INTEGER NOT NULL)"
  );
  return _db;
}

// 文件源缓存：86 组凭据每请求重读+解析太费；按 mtime 失效缓存。
let _fileCache: { mtimeMs: number; creds: TgCred[] } | null = null;

function readPoolRaw(): string {
  // 优先文件（POOL_TG_CREDS_FILE，600 权限、不进 env dump，适合几十上百组）；
  // 回落 env（POOL_TG_CREDS，少量组时方便）。
  const file = (process.env.POOL_TG_CREDS_FILE || "").trim();
  if (file) {
    try {
      return readFileSync(file, "utf8");
    } catch {
      return "";
    }
  }
  return (process.env.POOL_TG_CREDS || "").trim();
}

/** 解析凭据组（文件或 env）。非法条目跳过（宁缺勿错）。空数组 = 池禁用。 */
export function loadPoolCreds(): TgCred[] {
  const file = (process.env.POOL_TG_CREDS_FILE || "").trim();
  if (file) {
    try {
      const m = statSync(file).mtimeMs;
      if (_fileCache && _fileCache.mtimeMs === m) return _fileCache.creds;
      const parsed = parseCreds(readFileSync(file, "utf8"));
      _fileCache = { mtimeMs: m, creds: parsed };
      return parsed;
    } catch {
      return [];
    }
  }
  return parseCreds(readPoolRaw());
}

function parseCreds(raw: string): TgCred[] {
  if (!raw || !raw.trim()) return [];
  let arr: unknown;
  try {
    arr = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(arr)) return [];
  const out: TgCred[] = [];
  for (const e of arr) {
    const api_id = String((e as TgCred)?.api_id || "").trim();
    const api_hash = String((e as TgCred)?.api_hash || "").trim();
    const max = Math.max(1, Math.floor(Number((e as TgCred)?.max) || 50));
    const name = String((e as TgCred)?.name || `api-${api_id}`).slice(0, 40);
    if (/^\d{4,}$/.test(api_id) && /^[0-9a-f]{32}$/i.test(api_hash)) {
      out.push({ api_id, api_hash, max, name });
    }
  }
  return out;
}

export function poolEnabled(): boolean {
  return loadPoolCreds().length > 0;
}

export function normalizeFingerprint(raw: string): string {
  return String(raw || "").trim().toUpperCase().replace(/[^A-Z0-9-]/g, "").slice(0, 64);
}

function assignedApiId(mid: string): string | null {
  const row = db().prepare("SELECT api_id FROM tg_cred_assign WHERE mid=?").get(mid) as
    | { api_id: string }
    | undefined;
  return row?.api_id || null;
}

function usageByApiId(): Record<string, number> {
  const rows = db()
    .prepare("SELECT api_id, COUNT(*) AS n FROM tg_cred_assign GROUP BY api_id")
    .all() as Array<{ api_id: string; n: number }>;
  const m: Record<string, number> = {};
  for (const r of rows) m[r.api_id] = r.n;
  return m;
}

export type AssignResult =
  | { ok: true; api_id: string; api_hash: string; name: string; reused: boolean }
  | { ok: false; error: "pool_disabled" | "pool_full" | "bad_fingerprint" };

/**
 * 给一台机器分配（粘定）一组 Telegram 凭据。
 * 已分配过 → 返回同一组（reused）；否则挑「未满且当前占用最少」的组，落粘定表。
 */
export function assignCred(fingerprint: string): AssignResult {
  const creds = loadPoolCreds();
  if (!creds.length) return { ok: false, error: "pool_disabled" };
  const mid = normalizeFingerprint(fingerprint);
  if (!mid || mid.length < 8) return { ok: false, error: "bad_fingerprint" };

  const tx = db().transaction((): AssignResult => {
    const prev = assignedApiId(mid);
    if (prev) {
      const c = creds.find((x) => x.api_id === prev);
      if (c) return { ok: true, api_id: c.api_id, api_hash: c.api_hash, name: c.name, reused: true };
      // 该组已从 env 撤下 → 清粘定，走重新分配（下面）
      db().prepare("DELETE FROM tg_cred_assign WHERE mid=?").run(mid);
    }
    const usage = usageByApiId();
    const candidates = creds
      .map((c) => ({ c, used: usage[c.api_id] || 0 }))
      .filter((x) => x.used < x.c.max)
      .sort((a, b) => a.used - b.used);
    if (!candidates.length) return { ok: false, error: "pool_full" };
    const pick = candidates[0].c;
    db()
      .prepare("INSERT OR REPLACE INTO tg_cred_assign (mid, api_id, assigned_at) VALUES (?, ?, ?)")
      .run(mid, pick.api_id, Math.floor(Date.now() / 1000));
    return { ok: true, api_id: pick.api_id, api_hash: pick.api_hash, name: pick.name, reused: false };
  });
  return tx();
}

/** 运营观测：每组 api_id 的占用/容量（api_hash 绝不出现）。 */
export function poolStats(): {
  enabled: boolean;
  groups: Array<{ name: string; api_id_tail: string; used: number; max: number }>;
  total_used: number;
  total_cap: number;
} {
  const creds = loadPoolCreds();
  const usage = creds.length ? usageByApiId() : {};
  const groups = creds.map((c) => ({
    name: c.name,
    api_id_tail: c.api_id.slice(-4),
    used: usage[c.api_id] || 0,
    max: c.max,
  }));
  return {
    enabled: creds.length > 0,
    groups,
    total_used: groups.reduce((s, g) => s + g.used, 0),
    total_cap: groups.reduce((s, g) => s + g.max, 0),
  };
}

/** 与 ai-gateway 同一把 HMAC 密钥验设备令牌（避免重复实现）。 */
export function hmacSecret(): string {
  const explicit = (process.env.AI_GATEWAY_SECRET || "").trim();
  if (explicit) return explicit;
  const key = process.env.DEEPSEEK_API_KEY || "";
  return crypto.createHash("sha256").update(`chatx-gw-dev|${key}`).digest("hex");
}
