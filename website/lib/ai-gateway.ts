/**
 * ChatX 托管 AI 网关：设备令牌签发 + 把 OpenAI 形态请求代理到厂商 DeepSeek Key。
 *
 * 安全模型：
 * - 客户端永不持有 DEEPSEEK_API_KEY；只持有短时 HMAC 设备令牌（绑机器指纹）。
 * - 令牌只发给**试用台账里存在的指纹**（trial-claim-store，领取时留联系方式）——
 *   这是唯一防「脚本刷指纹白嫖」的闸门，与 7 天试用同一条生命线。
 * - 生产环境必须显式配 AI_GATEWAY_SECRET（不再从 API Key 派生）；缺失=网关整体禁用。
 * - 双层额度：单机日额度 + 全局日预算（成本熔断），钳制 model 白名单与 max_tokens。
 *
 * 部署清单（VPS）：DEEPSEEK_API_KEY、AI_GATEWAY_SECRET、NEXT_PUBLIC_SITE_URL 三个必配。
 */
import crypto from "crypto";
import Database from "better-sqlite3";
import { appendFile, mkdir, rename, stat } from "fs/promises";
import { mkdirSync } from "fs";
import path from "path";
import { DATA_DIR } from "./data-dir";

const VENDOR_ENDPOINT =
  process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com/chat/completions";
const VENDOR_MODEL = process.env.DEEPSEEK_MODEL || "deepseek-chat";

/** 单机每日字符预算（入站 + 出站），防单机刷爆。 */
export const DAILY_CHAR_BUDGET = Number(process.env.AI_GATEWAY_DAILY_CHARS || 50000);
/** 全局每日字符预算（所有机器合计）——云账单的总熔断闸。 */
export const GLOBAL_DAILY_CHAR_BUDGET = Number(
  process.env.AI_GATEWAY_GLOBAL_DAILY_CHARS || 2_000_000
);
/** 设备令牌有效期（秒）。默认 30 天：桌面端只在启动时换新，太短会在长开机器上过期。 */
export const TOKEN_TTL_SEC = Number(process.env.AI_GATEWAY_TOKEN_TTL || 30 * 24 * 3600);
/** 服务端 max_tokens 钳制（客户端自报不可信）。 */
export const MAX_TOKENS_CAP = Number(process.env.AI_GATEWAY_MAX_TOKENS || 2048);

/** model 白名单：默认只放厂商默认模型；多模型用逗号分隔 env 配置。 */
const MODEL_ALLOWLIST: string[] = (process.env.AI_GATEWAY_MODELS || VENDOR_MODEL)
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

const QUOTA_DB = process.env.AI_GATEWAY_QUOTA_DB || path.join(DATA_DIR, "ai-gateway.db");
const GW_LOG = process.env.AI_GATEWAY_LOG || path.join(DATA_DIR, "ai-gateway.jsonl");
const GW_LOG_MAX_BYTES = 10 * 1024 * 1024; // 超过即轮转一份 .1（保一代，观测数据非审计）
const GLOBAL_KEY = "__GLOBAL__";

export type DeviceClaims = {
  v: 1;
  mid: string;
  iat: number;
  exp: number;
};

// ── 额度库：better-sqlite3（WAL）——多进程/PM2 cluster 下事务原子，
//    替代首版 JSON 文件 + 进程内串行链（只在单进程下安全）。 ──
let _db: Database.Database | null = null;

function db(): Database.Database {
  if (_db) return _db;
  mkdirSync(path.dirname(QUOTA_DB), { recursive: true });
  _db = new Database(QUOTA_DB);
  _db.pragma("journal_mode = WAL");
  _db.exec(
    "CREATE TABLE IF NOT EXISTS gw_quota (" +
    " day TEXT NOT NULL, mid TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0," +
    " PRIMARY KEY (day, mid))"
  );
  return _db;
}

function usedOf(day: string, mid: string): number {
  const row = db().prepare("SELECT used FROM gw_quota WHERE day=? AND mid=?").get(day, mid) as
    | { used: number }
    | undefined;
  return row?.used || 0;
}

function pruneOldDays(): void {
  const cutoff = dayKey(new Date(Date.now() - 3 * 86400000));
  db().prepare("DELETE FROM gw_quota WHERE day < ?").run(cutoff);
}

function secretConfigured(): boolean {
  return Boolean((process.env.AI_GATEWAY_SECRET || "").trim());
}

/** 网关是否可用：有厂商 Key，且生产环境已显式配 HMAC 密钥。 */
export function gatewayEnabled(): boolean {
  if (!process.env.DEEPSEEK_API_KEY) return false;
  if (process.env.NODE_ENV === "production" && !secretConfigured()) return false;
  return true;
}

function hmacSecret(): string {
  const explicit = (process.env.AI_GATEWAY_SECRET || "").trim();
  if (explicit) return explicit;
  // 仅开发环境兜底派生；生产缺 secret 时 gatewayEnabled 已整体禁用，不会走到这里。
  const key = process.env.DEEPSEEK_API_KEY || "";
  return crypto.createHash("sha256").update(`chatx-gw-dev|${key}`).digest("hex");
}

export function normalizeFingerprint(raw: string): string {
  return String(raw || "")
    .trim()
    .toUpperCase()
    .replace(/[^A-Z0-9-]/g, "")
    .slice(0, 64);
}

/** model 钳制：白名单外一律回落默认（不拒——改造过的客户端也能用，只是用不了贵模型）。 */
export function clampModel(m: unknown): string {
  const s = String(m || "").trim();
  return MODEL_ALLOWLIST.includes(s) ? s : MODEL_ALLOWLIST[0];
}

export function clampMaxTokens(n: unknown): number {
  const v = Math.floor(Number(n));
  if (!Number.isFinite(v) || v <= 0) return Math.min(1024, MAX_TOKENS_CAP);
  return Math.min(v, MAX_TOKENS_CAP);
}

function b64url(buf: Buffer | string): string {
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf, "utf8");
  return b
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function fromB64url(s: string): Buffer {
  const pad = s.length % 4 === 0 ? "" : "=".repeat(4 - (s.length % 4));
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + pad;
  return Buffer.from(b64, "base64");
}

export function mintDeviceToken(fingerprint: string, nowSec = Math.floor(Date.now() / 1000)): {
  token: string;
  claims: DeviceClaims;
} {
  const mid = normalizeFingerprint(fingerprint);
  const claims: DeviceClaims = {
    v: 1,
    mid,
    iat: nowSec,
    exp: nowSec + TOKEN_TTL_SEC,
  };
  const body = b64url(JSON.stringify(claims));
  const sig = b64url(crypto.createHmac("sha256", hmacSecret()).update(body).digest());
  return { token: `cx.${body}.${sig}`, claims };
}

export function verifyDeviceToken(token: string, nowSec = Math.floor(Date.now() / 1000)): DeviceClaims | null {
  const t = String(token || "").trim();
  if (!t.startsWith("cx.")) return null;
  const parts = t.split(".");
  if (parts.length !== 3) return null;
  const [, body, sig] = parts;
  const expect = b64url(crypto.createHmac("sha256", hmacSecret()).update(body).digest());
  const a = Buffer.from(sig);
  const b = Buffer.from(expect);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  try {
    const claims = JSON.parse(fromB64url(body).toString("utf8")) as DeviceClaims;
    if (claims?.v !== 1 || !claims.mid || !claims.exp) return null;
    if (claims.exp < nowSec) return null;
    if (!normalizeFingerprint(claims.mid)) return null;
    return claims;
  } catch {
    return null;
  }
}

function dayKey(d = new Date()): string {
  return d.toISOString().slice(0, 10);
}

export type QuotaSnapshot = {
  used: number;
  budget: number;
  remaining: number;
  global_used: number;
  global_budget: number;
  global_remaining: number;
  /** 全局预算耗尽（=试用通道繁忙，与单机额度用尽区分展示）。 */
  busy: boolean;
};

export async function quotaSnapshot(fingerprint: string): Promise<QuotaSnapshot> {
  const mid = normalizeFingerprint(fingerprint);
  const day = dayKey();
  const used = usedOf(day, mid);
  const gUsed = usedOf(day, GLOBAL_KEY);
  return {
    used,
    budget: DAILY_CHAR_BUDGET,
    remaining: Math.max(0, DAILY_CHAR_BUDGET - used),
    global_used: gUsed,
    global_budget: GLOBAL_DAILY_CHAR_BUDGET,
    global_remaining: Math.max(0, GLOBAL_DAILY_CHAR_BUDGET - gUsed),
    busy: gUsed >= GLOBAL_DAILY_CHAR_BUDGET,
  };
}

/**
 * 计费（单机 + 全局同一 sqlite 事务）。chars=0 只做快照式检查不落账。
 * 返回 which：超的是哪一层（machine|global），前端据此分「明天恢复」vs「通道繁忙」。
 */
export async function consumeQuota(
  fingerprint: string,
  chars: number
): Promise<{ ok: boolean; used: number; remaining: number; which?: "machine" | "global" }> {
  const mid = normalizeFingerprint(fingerprint);
  const n = Math.max(0, Math.floor(chars));
  const day = dayKey();
  const tx = db().transaction(() => {
    const cur = usedOf(day, mid);
    const gCur = usedOf(day, GLOBAL_KEY);
    if (n > 0 && cur + n > DAILY_CHAR_BUDGET) {
      return { ok: false, used: cur, remaining: Math.max(0, DAILY_CHAR_BUDGET - cur), which: "machine" as const };
    }
    if (n > 0 && gCur + n > GLOBAL_DAILY_CHAR_BUDGET) {
      return { ok: false, used: cur, remaining: Math.max(0, DAILY_CHAR_BUDGET - cur), which: "global" as const };
    }
    if (n > 0) {
      const up = db().prepare(
        "INSERT INTO gw_quota (day, mid, used) VALUES (?, ?, ?)" +
        " ON CONFLICT(day, mid) DO UPDATE SET used = used + excluded.used"
      );
      up.run(day, mid, n);
      up.run(day, GLOBAL_KEY, n);
    }
    const used = usedOf(day, mid);
    return { ok: true, used, remaining: Math.max(0, DAILY_CHAR_BUDGET - used) };
  });
  const out = tx();
  if (Math.random() < 0.01) pruneOldDays(); // 概率式清理，避免每请求一次 DELETE
  return out;
}

/** 当日聚合（console 观测卡）：多少台机器在用、烧了多少字符、全局水位。 */
export async function gatewayDayStats(): Promise<{
  enabled: boolean;
  day: string;
  machines: number;
  chars: number;
  global_budget: number;
  machine_budget: number;
}> {
  const day = dayKey();
  let machines = 0;
  let chars = 0;
  try {
    const row = db()
      .prepare("SELECT COUNT(*) AS n, COALESCE(SUM(used),0) AS s FROM gw_quota WHERE day=? AND mid<>?")
      .get(day, GLOBAL_KEY) as { n: number; s: number } | undefined;
    machines = row?.n || 0;
    chars = row?.s || 0;
  } catch {
    /* 表还没建（零流量）→ 全 0 */
  }
  return {
    enabled: gatewayEnabled(),
    day,
    machines,
    chars,
    global_budget: GLOBAL_DAILY_CHAR_BUDGET,
    machine_budget: DAILY_CHAR_BUDGET,
  };
}

export function estimateRequestChars(body: unknown): number {
  try {
    const msgs = (body as { messages?: Array<{ content?: unknown }> })?.messages;
    if (!Array.isArray(msgs)) return 0;
    let n = 0;
    for (const m of msgs) {
      const c = m?.content;
      if (typeof c === "string") n += c.length;
      else if (Array.isArray(c)) {
        for (const part of c) {
          if (typeof part === "string") n += part.length;
          else if (part && typeof part === "object" && typeof (part as { text?: string }).text === "string") {
            n += String((part as { text: string }).text).length;
          }
        }
      }
    }
    return n;
  } catch {
    return 0;
  }
}

/** 观测流水（JSONL，绝不写消息内容——只有量级与状态）。超 10MB 轮转一份 .1。 */
export async function logGateway(rec: Record<string, unknown>): Promise<void> {
  try {
    await mkdir(path.dirname(GW_LOG), { recursive: true });
    try {
      const st = await stat(GW_LOG);
      if (st.size > GW_LOG_MAX_BYTES) {
        await rename(GW_LOG, GW_LOG + ".1"); // 覆盖旧 .1，保一代
      }
    } catch {
      /* 文件还不存在 */
    }
    await appendFile(GW_LOG, JSON.stringify({ t: new Date().toISOString(), ...rec }) + "\n");
  } catch {
    /* 观测失败不阻断主链路 */
  }
}

export async function proxyChatCompletions(
  body: Record<string, unknown>,
  signal?: AbortSignal
): Promise<Response> {
  const key = process.env.DEEPSEEK_API_KEY;
  if (!key) throw new Error("no_vendor_key");
  const payload = {
    ...body,
    model: clampModel(body.model),
    max_tokens: clampMaxTokens(body.max_tokens),
    stream: false, // 首版关流式：契约明确，非流式客户端零影响
  };
  return fetch(VENDOR_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
    },
    body: JSON.stringify(payload),
    signal,
  });
}

export function publicGatewayBase(siteOrigin: string): string {
  return `${siteOrigin.replace(/\/+$/, "")}/api/ai/v1`;
}

export function publicModel(): string {
  return MODEL_ALLOWLIST[0];
}
