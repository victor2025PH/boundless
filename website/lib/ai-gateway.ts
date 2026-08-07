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

// ── 识图中继（我们自己的 GPU VLM，经 117→VPS 反向隧道暴露到 VPS localhost）──
// VISION_RELAY_URLS 逗号列表 = 多机双活（176/140），按序尝试、失败短冷却降权；
// 回落单数 VISION_RELAY_URL。未设 = 识图禁用（暗态），客户端自行回落。
const VISION_RELAY_URLS: string[] = (
  process.env.VISION_RELAY_URLS || process.env.VISION_RELAY_URL || ""
)
  .split(",")
  .map((s) => s.trim().replace(/\/+$/, ""))
  .filter(Boolean);
const VISION_RELAY_URL = VISION_RELAY_URLS[0] || "";
/** 规范识图模型：两台中继都装的那个。网关统一改写 model —— 已发布客户端
 *  （0.2.7 注入 qwen2.5vl:7b，仅 176 有）也能被任一中继服务，双活才真互换。 */
const VISION_MODEL_CANONICAL = (
  process.env.VISION_MODEL_CANONICAL || "qwen3-vl:8b-instruct"
).trim();
/** 中继失败冷却（毫秒）：某机忙/宕 → 短暂降权，不每次都撞它。 */
const RELAY_COOLDOWN_MS = Number(process.env.VISION_RELAY_COOLDOWN_MS || 60000);
const _relayCooldown = new Map<string, number>();

function orderedRelays(): string[] {
  const now = Date.now();
  const fresh: string[] = [];
  const cooled: string[] = [];
  for (const u of VISION_RELAY_URLS) {
    if ((_relayCooldown.get(u) || 0) > now) cooled.push(u);
    else fresh.push(u);
  }
  return [...fresh, ...cooled]; // 全在冷却也要硬试（比直接失败好）
}
const VISION_MODELS: string[] = (process.env.VISION_MODELS || "qwen2.5vl:7b,qwen2.5vl:32b,qwen2.5vl:72b")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);
// 识图一次调用的额度折算（图片 token 远多于纯文本，按固定成本计，防按字符低估）
export const VISION_CHAR_COST = Number(process.env.AI_GATEWAY_VISION_CHARS || 1500);

export function visionRelayEnabled(): boolean {
  return Boolean(VISION_RELAY_URL);
}

export function isVisionModel(model: unknown): boolean {
  const m = String(model || "").trim().toLowerCase();
  if (!m) return false;
  if (VISION_MODELS.includes(m)) return true;
  // 兜底特征：常见 VLM 名含 vl / llava / -v（glm-4v）
  return /(\bvl\b|vl:|llava|-v\b|4v)/.test(m);
}

export async function proxyVision(
  body: Record<string, unknown>,
  signal?: AbortSignal
): Promise<Response> {
  const relays = orderedRelays();
  if (!relays.length) throw new Error("no_vision_relay");
  // 中继是 Ollama /v1（keyless）；model 统一改写成规范 VLM（两机都装 → 可互换）
  const payload = {
    ...body,
    model: VISION_MODEL_CANONICAL,
    stream: false,
    max_tokens: clampMaxTokens(body.max_tokens),
  };
  let lastErr: unknown = null;
  for (const base of relays) {
    try {
      const r = await fetch(`${base}/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer ollama" },
        body: JSON.stringify(payload),
        signal,
      });
      // 5xx = 该机有问题（忙/模型缺）→ 冷却降权，试下一台
      if (r.status >= 500 && relays.length > 1) {
        _relayCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
        lastErr = new Error(`relay_${r.status}`);
        continue;
      }
      return r;
    } catch (e) {
      // 网络级失败（隧道断）→ 冷却降权，试下一台
      _relayCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
      lastErr = e;
    }
  }
  throw lastErr || new Error("all_vision_relays_failed");
}

// ── 语音中继（2026-08-03）：克隆 TTS(7852) 与 GPU ASR(8765) 经同一条 117→VPS
//    反向隧道暴露到 VPS localhost，网关按设备令牌鉴权转发——把「非局域网机器用
//    集群算力生成语音 / 听懂语音」补齐到与识图同一安全模型。
//    TTS_RELAY_URLS  逗号列表（117 主 / 140 备）：http://127.0.0.1:18413,http://127.0.0.1:18414
//    ASR_RELAY_URLS  须含 /v1 后缀（OpenAI 形态）：http://127.0.0.1:18415/v1
//    AH_SERVICE_TOKEN 集群内部 X-AH-Svc 令牌——只在 VPS env，**永不下发客户端**
//    （客户端只持 cx.* 设备令牌；集群令牌由网关侧代注）。
const TTS_RELAY_URLS: string[] = (process.env.TTS_RELAY_URLS || "")
  .split(",")
  .map((s) => s.trim().replace(/\/+$/, ""))
  .filter(Boolean);
const ASR_RELAY_URLS: string[] = (process.env.ASR_RELAY_URLS || "")
  .split(",")
  .map((s) => s.trim().replace(/\/+$/, ""))
  .filter(Boolean);
const AH_SERVICE_TOKEN = (process.env.AH_SERVICE_TOKEN || "").trim();
/** TTS 一次合成的最低计额（字符）：短句也占 GPU 一轮串行合成，纯按字符会低估。 */
export const TTS_CHAR_MIN_COST = Number(process.env.AI_GATEWAY_TTS_MIN_CHARS || 60);
/** ASR 一次转写的固定计额（字符）：语音时长网关不可靠可知，按次折算。 */
export const ASR_CHAR_COST = Number(process.env.AI_GATEWAY_ASR_CHARS || 200);

const _ttsCooldown = new Map<string, number>();
const _asrCooldown = new Map<string, number>();

function orderedOf(urls: string[], cooldown: Map<string, number>): string[] {
  const now = Date.now();
  const fresh: string[] = [];
  const cooled: string[] = [];
  for (const u of urls) {
    if ((cooldown.get(u) || 0) > now) cooled.push(u);
    else fresh.push(u);
  }
  return [...fresh, ...cooled]; // 全在冷却也要硬试（比直接失败好）
}

export function ttsRelayEnabled(): boolean {
  return TTS_RELAY_URLS.length > 0;
}

export function asrRelayEnabled(): boolean {
  return ASR_RELAY_URLS.length > 0;
}

/** 中继请求头：代注集群内部令牌（7852@140 等跨机节点要 X-AH-Svc 才放行）。 */
function relayHeaders(extra: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...extra };
  if (AH_SERVICE_TOKEN) h["X-AH-Svc"] = AH_SERVICE_TOKEN;
  return h;
}

/** 从请求头取设备令牌：Authorization Bearer 或 X-AH-Svc（avatar_voice 客户端走后者）。 */
export function extractDeviceToken(headers: Headers): string {
  const auth = headers.get("authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
  if (m && m[1].trim().startsWith("cx.")) return m[1].trim();
  const svc = (headers.get("x-ah-svc") || "").trim();
  return svc.startsWith("cx.") ? svc : "";
}

/** TTS 转发：JSON 原样透传到 7852 中继（117 主 / 140 备），5xx/网络失败冷却降权。 */
export async function proxyTts(
  path: string,
  rawJson: string,
  signal?: AbortSignal
): Promise<Response> {
  const relays = orderedOf(TTS_RELAY_URLS, _ttsCooldown);
  if (!relays.length) throw new Error("no_tts_relay");
  let lastErr: unknown = null;
  for (const base of relays) {
    try {
      const r = await fetch(`${base}${path}`, {
        method: "POST",
        headers: relayHeaders({ "Content-Type": "application/json" }),
        body: rawJson,
        signal,
      });
      if (r.status >= 500 && relays.length > 1) {
        _ttsCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
        lastErr = new Error(`relay_${r.status}`);
        continue;
      }
      return r;
    } catch (e) {
      _ttsCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
      lastErr = e;
    }
  }
  throw lastErr || new Error("all_tts_relays_failed");
}

/** ASR 转发：multipart 原样透传（含 boundary 的 Content-Type 一并带过去）。 */
export async function proxyAsr(
  body: ArrayBuffer,
  contentType: string,
  signal?: AbortSignal
): Promise<Response> {
  const relays = orderedOf(ASR_RELAY_URLS, _asrCooldown);
  if (!relays.length) throw new Error("no_asr_relay");
  let lastErr: unknown = null;
  for (const base of relays) {
    try {
      const r = await fetch(`${base}/audio/transcriptions`, {
        method: "POST",
        headers: relayHeaders(
          contentType ? { "Content-Type": contentType } : {}
        ),
        body,
        signal,
      });
      if (r.status >= 500 && relays.length > 1) {
        _asrCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
        lastErr = new Error(`relay_${r.status}`);
        continue;
      }
      return r;
    } catch (e) {
      _asrCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
      lastErr = e;
    }
  }
  throw lastErr || new Error("all_asr_relays_failed");
}

// TTS /health 透传（15s 缓存）：客户端 AvatarVoiceClient 健康预检据此决定端点
// 次序——必须真探中继（隧道断/服务半死时如实报不健康，客户端才会回落），
// 但带缓存防「每台客户机每 30s 一探」打穿隧道。
let _ttsHealth: { ts: number; ok: boolean } = { ts: 0, ok: false };

export async function ttsRelayHealth(): Promise<{ ok: boolean; models_loaded: boolean }> {
  if (!TTS_RELAY_URLS.length) return { ok: false, models_loaded: false };
  const now = Date.now();
  if (now - _ttsHealth.ts < 15000) {
    return { ok: _ttsHealth.ok, models_loaded: _ttsHealth.ok };
  }
  let ok = false;
  for (const base of orderedOf(TTS_RELAY_URLS, _ttsCooldown)) {
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 4000);
      const r = await fetch(`${base}/health`, {
        headers: relayHeaders({ accept: "application/json" }),
        signal: ac.signal,
      });
      clearTimeout(t);
      if (r.ok) {
        const j = (await r.json().catch(() => null)) as
          | { ok?: unknown; models_loaded?: unknown }
          | null;
        if (j && j.ok === true && j.models_loaded !== false) {
          ok = true;
          break;
        }
      }
    } catch {
      /* try next relay */
    }
  }
  _ttsHealth = { ts: now, ok };
  return { ok, models_loaded: ok };
}

/** 观测：识图中继与冷却态（console 卡/排障用；不含任何密钥）。 */
export function visionRelayStatus(): {
  enabled: boolean;
  canonical_model: string;
  relays: Array<{ url: string; cooling: boolean }>;
} {
  const now = Date.now();
  return {
    enabled: VISION_RELAY_URLS.length > 0,
    canonical_model: VISION_MODEL_CANONICAL,
    relays: VISION_RELAY_URLS.map((u) => ({
      url: u,
      cooling: (_relayCooldown.get(u) || 0) > now,
    })),
  };
}

const QUOTA_DB = process.env.AI_GATEWAY_QUOTA_DB || path.join(DATA_DIR, "ai-gateway.db");
const GW_LOG = process.env.AI_GATEWAY_LOG || path.join(DATA_DIR, "ai-gateway.jsonl");
const GW_LOG_MAX_BYTES = 10 * 1024 * 1024; // 超过即轮转一份 .1（保一代，观测数据非审计）
const GLOBAL_KEY = "__GLOBAL__";

export type DeviceClaims = {
  v: 1;
  mid: string;
  /** 托管实例 ID（可选）。有则额度键用实例维度，同机多租户互不挤额度。 */
  iid?: string;
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
  // Telegram 凭据粘定表：机器指纹 → 分到的 api_id（同机永远同 api_id，
  // 因 pyrogram session 与 api_id 绑定，换组会触发 Telegram 风控——与 LAN 池同不变量）。
  _db.exec(
    "CREATE TABLE IF NOT EXISTS tg_cred_assign (" +
    " mid TEXT PRIMARY KEY, api_id TEXT NOT NULL, assigned_at INTEGER NOT NULL)"
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

/** 托管 instance_id：小写 [a-z0-9_-]，最长 64；非法 → 空（不写入 claims）。 */
export function normalizeInstanceId(raw: string): string {
  return String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, "")
    .slice(0, 64);
}

/**
 * 额度主体键：有合法 iid → ``IID:<iid>``（按租户实例计量）；
 * 否则回落机器指纹 mid（桌面试用旧口径，向后兼容）。
 */
export function quotaSubject(claims: Pick<DeviceClaims, "mid" | "iid"> | string): string {
  if (typeof claims === "string") {
    // 兼容旧调用：consumeQuota(fingerprint, n) 仍可直接传 mid / IID:…
    return String(claims || "").trim();
  }
  const iid = normalizeInstanceId(claims.iid || "");
  if (iid) return `IID:${iid}`;
  return claims.mid;
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

export function mintDeviceToken(
  fingerprint: string,
  nowSec = Math.floor(Date.now() / 1000),
  instanceId?: string
): {
  token: string;
  claims: DeviceClaims;
} {
  const mid = normalizeFingerprint(fingerprint);
  const iid = normalizeInstanceId(instanceId || "");
  const claims: DeviceClaims = {
    v: 1,
    mid,
    iat: nowSec,
    exp: nowSec + TOKEN_TTL_SEC,
  };
  if (iid) claims.iid = iid;
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

export async function quotaSnapshot(
  subject: string | Pick<DeviceClaims, "mid" | "iid">
): Promise<QuotaSnapshot> {
  // subject 可以是指纹、IID:…、或 claims；库键不再强制指纹格式（托管实例键是 IID:）
  const mid = quotaSubject(subject);
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
 * 计费（主体 + 全局同一 sqlite 事务）。chars=0 只做快照式检查不落账。
 * subject = 机器指纹 mid，或托管 ``IID:<instance_id>``（经 quotaSubject(claims)）。
 * 返回 which：超的是哪一层（machine|global），前端据此分「明天恢复」vs「通道繁忙」。
 */
export async function consumeQuota(
  subject: string | Pick<DeviceClaims, "mid" | "iid">,
  chars: number
): Promise<{ ok: boolean; used: number; remaining: number; which?: "machine" | "global" }> {
  const mid = quotaSubject(subject);
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
