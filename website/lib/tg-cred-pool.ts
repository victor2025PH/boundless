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

export type TgProxy = {
  scheme: string; host: string; port: number; username?: string; password?: string;
};
export type TgCred = {
  api_id: string; api_hash: string; max: number; name: string;
  /** P2-⑨ 组级出口：带出口的组优先派给直连不通（tg_direct=false）的机器。 */
  proxy?: TgProxy;
};

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
  // 无感换发（2026-08-10 API_ID_INVALID 事故闭环）：
  //   reports = 客户端举报「这组凭据 Telegram 不认」的流水（按 mid×api_id 记）；
  //   quarantine = 被 ≥N 台不同机器举报的组 → 自动隔离停发，等运营跑探针核实后处置。
  _db.exec(
    "CREATE TABLE IF NOT EXISTS tg_cred_reports (" +
    " mid TEXT NOT NULL, api_id TEXT NOT NULL, ts INTEGER NOT NULL)"
  );
  _db.exec(
    "CREATE INDEX IF NOT EXISTS idx_tg_cred_reports_api ON tg_cred_reports(api_id, ts)"
  );
  _db.exec(
    "CREATE TABLE IF NOT EXISTS tg_cred_quarantine (" +
    " api_id TEXT PRIMARY KEY, since INTEGER NOT NULL," +
    " reporters INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '')"
  );
  return _db;
}

// 举报聚类窗口与隔离阈值：N 台**不同**机器在窗口内举报同一组 → 该组停发。
// 阈值刻意 >1：单机举报可能是本机环境怪相；多机同报才是「组废了」的高置信信号。
// 单机举报者本身**当场就被换组**（见 reportInvalid 删粘定），不受阈值影响。
const REPORT_WINDOW_MS = 24 * 3600 * 1000;
function quarantineThreshold(): number {
  const n = Number(process.env.POOL_TG_QUARANTINE_THRESHOLD || 3);
  return Number.isFinite(n) && n >= 1 ? Math.floor(n) : 3;
}

/** 当前被隔离（停发）的 api_id 集合。 */
export function quarantinedIds(): Set<string> {
  const rows = db().prepare("SELECT api_id FROM tg_cred_quarantine").all() as Array<{ api_id: string }>;
  return new Set(rows.map((r) => r.api_id));
}

export type ReportResult = {
  accepted: boolean;
  reason?: "not_assigned" | "bad_fingerprint";
  quarantined: boolean;
  distinct: number;
};

/**
 * 客户端举报「粘定给我的这组凭据无效（API_ID_INVALID）」。
 * 防滥用锚点：**只接受与粘定表一致的举报**——举报者必须真的被分到该组，
 * 否则任何持令牌客户端都能凭空举报把整个池逐组打烊。
 * 接受后：删该机粘定（调用方随后 assignCred 重分配，排除被举报组）；
 * 窗口内不同机器数达阈值 → 整组隔离（quarantine 表）等运营核实。
 */
export function reportInvalid(fingerprint: string, badApiId: string): ReportResult {
  const mid = normalizeFingerprint(fingerprint);
  const bad = String(badApiId || "").trim();
  if (!mid || mid.length < 8 || !bad) {
    return { accepted: false, reason: "bad_fingerprint", quarantined: false, distinct: 0 };
  }
  const tx = db().transaction((): ReportResult => {
    const prev = assignedApiId(mid);
    if (prev !== bad) {
      return { accepted: false, reason: "not_assigned", quarantined: false, distinct: 0 };
    }
    const now = Date.now();
    db().prepare("INSERT INTO tg_cred_reports (mid, api_id, ts) VALUES (?, ?, ?)")
      .run(mid, bad, now);
    db().prepare("DELETE FROM tg_cred_assign WHERE mid=?").run(mid);
    const row = db()
      .prepare("SELECT COUNT(DISTINCT mid) AS n FROM tg_cred_reports WHERE api_id=? AND ts>=?")
      .get(bad, now - REPORT_WINDOW_MS) as { n: number };
    const distinct = row?.n || 0;
    let quarantined = false;
    if (distinct >= quarantineThreshold()) {
      db().prepare(
        "INSERT OR IGNORE INTO tg_cred_quarantine (api_id, since, reporters, reason)" +
        " VALUES (?, ?, ?, ?)"
      ).run(bad, now, distinct, "client_reports");
      db().prepare("UPDATE tg_cred_quarantine SET reporters=? WHERE api_id=?").run(distinct, bad);
      quarantined = true;
    }
    return { accepted: true, quarantined, distinct };
  });
  return tx();
}

/** 运营解除隔离（探针复核为误报时用；配合 /console/trial 手工操作或 sqlite 直改）。 */
export function unquarantine(apiId: string): boolean {
  const r = db().prepare("DELETE FROM tg_cred_quarantine WHERE api_id=?").run(String(apiId).trim());
  return r.changes > 0;
}

/** 窗口内被派发过凭据的机器指纹（激活漏斗「dispatched」段取数口）。 */
export function assignedMidsSince(sinceMs: number): string[] {
  const rows = db()
    .prepare("SELECT mid FROM tg_cred_assign WHERE assigned_at >= ?")
    .all(Math.floor(sinceMs / 1000)) as Array<{ mid: string }>;
  return rows.map((r) => r.mid);
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

/** 出口校验：宁可不用代理也不能用半个代理（与引擎侧 _sanitize_proxy 同口径）。 */
function parseProxy(raw: unknown): TgProxy | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const p = raw as Record<string, unknown>;
  const host = String(p.host || "").trim();
  const port = Math.floor(Number(p.port) || 0);
  if (!host || port <= 0 || port > 65535) return undefined;
  const out: TgProxy = {
    scheme: String(p.scheme || "socks5").trim() || "socks5",
    host,
    port,
  };
  if (String(p.username || "").trim()) out.username = String(p.username).trim();
  if (String(p.password || "").trim()) out.password = String(p.password);
  return out;
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
      const proxy = parseProxy((e as TgCred)?.proxy);
      out.push(proxy ? { api_id, api_hash, max, name, proxy }
                     : { api_id, api_hash, max, name });
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
  | { ok: true; api_id: string; api_hash: string; name: string; reused: boolean;
      proxy?: TgProxy }
  | { ok: false; error: "pool_disabled" | "pool_full" | "bad_fingerprint" };

/**
 * 给一台机器分配（粘定）一组 Telegram 凭据。
 * 已分配过 → 返回同一组（reused）；否则挑「未满且当前占用最少」的组，落粘定表。
 * 隔离组（quarantine）视同撤下：既不给新机，粘在其上的老机下次来领也自动迁走。
 * ``opts.excludeApiId``：本次分配额外排除的组（举报换发场景——组未达隔离阈值时，
 * 也绝不能把举报者刚说废的那组再发回去）。
 * ``opts.preferProxy``（P2-⑨ 智能派发）：true=优先带出口的组（直连不通的大陆机），
 * false=优先无出口的组（直连通畅的机器别浪费出口容量）；undefined=不偏好。
 * **软偏好**：只影响候选排序，绝不硬过滤——偏好组满员时照样分另一类，接入优先。
 * 粘定永远最高优先（session×api_id 绑定不变量），偏好只作用于新分配/重分配。
 */
export function assignCred(
  fingerprint: string,
  opts?: { excludeApiId?: string; preferProxy?: boolean }
): AssignResult {
  const creds = loadPoolCreds();
  if (!creds.length) return { ok: false, error: "pool_disabled" };
  const mid = normalizeFingerprint(fingerprint);
  if (!mid || mid.length < 8) return { ok: false, error: "bad_fingerprint" };
  const quarantined = quarantinedIds();
  const exclude = String(opts?.excludeApiId || "").trim();
  const prefer = opts?.preferProxy;

  const tx = db().transaction((): AssignResult => {
    const prev = assignedApiId(mid);
    if (prev) {
      const c = creds.find((x) => x.api_id === prev);
      if (c && !quarantined.has(prev) && prev !== exclude) {
        return { ok: true, api_id: c.api_id, api_hash: c.api_hash, name: c.name,
                 reused: true, ...(c.proxy ? { proxy: c.proxy } : {}) };
      }
      // 该组已撤下/被隔离/被本次排除 → 清粘定，走重新分配（下面）
      db().prepare("DELETE FROM tg_cred_assign WHERE mid=?").run(mid);
    }
    const usage = usageByApiId();
    const mismatch = (c: TgCred): number =>
      prefer === undefined ? 0 : (!!c.proxy === prefer ? 0 : 1);
    const candidates = creds
      .map((c) => ({ c, used: usage[c.api_id] || 0 }))
      .filter((x) => x.used < x.c.max
        && !quarantined.has(x.c.api_id)
        && x.c.api_id !== exclude)
      .sort((a, b) => (mismatch(a.c) - mismatch(b.c)) || (a.used - b.used));
    if (!candidates.length) return { ok: false, error: "pool_full" };
    const pick = candidates[0].c;
    db()
      .prepare("INSERT OR REPLACE INTO tg_cred_assign (mid, api_id, assigned_at) VALUES (?, ?, ?)")
      .run(mid, pick.api_id, Math.floor(Date.now() / 1000));
    return { ok: true, api_id: pick.api_id, api_hash: pick.api_hash, name: pick.name,
             reused: false, ...(pick.proxy ? { proxy: pick.proxy } : {}) };
  });
  return tx();
}

/** 运营观测：每组 api_id 的占用/容量/隔离态/出口（api_hash 与代理账密绝不出现）。 */
export function poolStats(): {
  enabled: boolean;
  groups: Array<{ name: string; api_id_tail: string; used: number; max: number;
                  quarantined: boolean; has_proxy: boolean; api_id: string }>;
  total_used: number;
  total_cap: number;
  quarantined_groups: number;
} {
  const creds = loadPoolCreds();
  const usage = creds.length ? usageByApiId() : {};
  const q = creds.length ? quarantinedIds() : new Set<string>();
  const groups = creds.map((c) => ({
    name: c.name,
    // 完整 api_id 供 console 解除隔离操作定位（api_id 非密钥——客户端配置里本就明文）
    api_id: c.api_id,
    api_id_tail: c.api_id.slice(-4),
    used: usage[c.api_id] || 0,
    max: c.max,
    quarantined: q.has(c.api_id),
    has_proxy: !!c.proxy,
  }));
  return {
    enabled: creds.length > 0,
    groups,
    total_used: groups.reduce((s, g) => s + g.used, 0),
    total_cap: groups.reduce((s, g) => s + g.max, 0),
    quarantined_groups: groups.filter((g) => g.quarantined).length,
  };
}

/** 与 ai-gateway 同一把 HMAC 密钥验设备令牌（避免重复实现）。 */
export function hmacSecret(): string {
  const explicit = (process.env.AI_GATEWAY_SECRET || "").trim();
  if (explicit) return explicit;
  const key = process.env.DEEPSEEK_API_KEY || "";
  return crypto.createHash("sha256").update(`chatx-gw-dev|${key}`).digest("hex");
}
