/**
 * 安装包下载台账 + 扒包防护（2026-09-10）。
 *
 * 背景：/dl 分流器和 public/downloads 静态目录此前都不记 IP；/api/track 埋点也不记 IP。
 * 想知道「谁在下载」只能翻 nginx access.log（只留 14 天，且 R2 302 后看不到完成态）。
 * 本模块把所有安装包 GET 收口成一条台账（downloads.jsonl），并在同一处做三道闸：
 *   1. 爬虫/扫描器 UA 直接 403（GPTBot、Bingbot、TG 链接预览、腾讯云伪 iPhone 扫描…）；
 *   2. 按 IP 限「同小时内不同安装包数」（不是按请求数——electron-updater 差量下载会对同一
 *      个 exe 发几十上百个 Range 请求，按请求限流会误杀自动更新）；
 *   3. 一小时内横扫 ≥3 个产品家族的 IP 打 sweep 标记（只标不拦：销售线索确有可能三件都要）。
 * 办公室出口（DL_TRUSTED_IPS）免闸、只记账、标 office。
 *
 * 台账去重：同 IP + 同文件在 DEDUPE_WINDOW_MS 内只记第一条（后续 Range 请求同属一次下载）。
 * 限流状态在进程内存（多 worker 时各算各的，上限只是「每 worker 上限」——防的是批量作业，
 * 不追求精确配额）。
 */
import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR, DATA_DIR } from "./data-dir";

export const DOWNLOAD_LOG = process.env.DOWNLOAD_LOG || path.join(ANALYTICS_DIR, "downloads.jsonl");

export type DlVia = "r2" | "local" | "blocked";
export type DlFlag = "office" | "bot" | "ratelimit" | "sweep" | "noua" | "range";

export interface DlRecord {
  t: string;
  ip: string;
  ua: string;
  ref: string;
  /** 请求路径（不含 /dl 前缀），如 downloads/internal/ChatX-Setup-1.0.79.exe */
  path: string;
  product: string;
  version: string;
  channel: "public" | "internal" | "lite" | "other";
  via: DlVia;
  status: number;
  flags: DlFlag[];
  /** blocked 时的原因短码 */
  reason?: string;
}

// ── 分类 ────────────────────────────────────────────────────────────
export function classifyPath(p: string): Pick<DlRecord, "product" | "version" | "channel"> {
  const base = p.split("/").pop() || p;
  let product = "other";
  if (/^ChatX-/i.test(base)) product = "chatx";
  else if (/^AvatarHub-/i.test(base)) product = "avatarhub";
  else if (/^MatrixX-/i.test(base)) product = "matrixx";
  else product = base.replace(/[-_.].*$/, "").toLowerCase() || "other";
  const version = base.match(/(\d+\.\d+\.\d+)/)?.[1] || "";
  const channel: DlRecord["channel"] = /\/internal\//.test(p)
    ? "internal"
    : /\/lite\//.test(p)
      ? "lite"
      : /^downloads\/|^releases\//.test(p)
        ? "public"
        : "other";
  return { product, version, channel };
}

/** 安装包 = 会被当作「下载」计数与设闸的文件；yml/blockmap/json 不算。 */
export function isInstallerPath(p: string): boolean {
  return /\.(exe|msi|dmg|pkg|zip|7z|appimage|deb|rpm)$/i.test(p);
}

// ── 爬虫 / 扫描器 ───────────────────────────────────────────────────
// 覆盖 2026-08-27~09-10 nginx 里实际出现的全部非人类 UA。安装包是 Windows exe，
// 「iPhone OS 13_2_3」这种腾讯云链接安全扫描的伪 UA 不可能是真下载者，一并挡。
export const DL_BOT_UA =
  /bot\b|bot\/|crawl|spider|slurp|GPTBot|OAI-SearchBot|ChatGPT-User|ClaudeBot|Claude-Web|anthropic-ai|PerplexityBot|Bytespider|Amazonbot|Applebot|Googlebot|bingbot|YandexBot|DuckDuckBot|Baiduspider|AhrefsBot|SemrushBot|MJ12bot|DotBot|PetalBot|TelegramBot|facebookexternalhit|Twitterbot|WhatsApp\/|Discordbot|Slackbot|LinkedInBot|Pinterest|jscrawler|python-requests|Scrapy|Go-http-client|HeadlessChrome|Java\/|libwww|XMPP Tiscali|iPhone OS 13_2_3|Windows NT 5\.1|MSIE /i;

export function isBotUa(ua: string): boolean {
  return DL_BOT_UA.test(ua);
}

// ── 办公室 / 内部出口 ───────────────────────────────────────────────
function trustedIps(): Set<string> {
  return new Set(
    (process.env.DL_TRUSTED_IPS || "")
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

export function isTrustedIp(ip: string): boolean {
  return trustedIps().has(ip);
}

// ── 进程内窗口状态 ──────────────────────────────────────────────────
const DEDUPE_WINDOW_MS = 30 * 60_000;
const RATE_WINDOW_MS = 60 * 60_000;
const RATE_MAX_FILES = Math.max(1, Number(process.env.DL_RATE_MAX_FILES_PER_HOUR || 8) || 8);
const SWEEP_MIN_PRODUCTS = 3;

interface IpState {
  /** path → 首次命中时间（去重 + 限流共用） */
  files: Map<string, number>;
  /** product → 首次命中时间（sweep） */
  products: Map<string, number>;
}
const state = new Map<string, IpState>();

function prune(st: IpState, now: number) {
  for (const [k, t] of st.files) if (now - t > RATE_WINDOW_MS) st.files.delete(k);
  for (const [k, t] of st.products) if (now - t > RATE_WINDOW_MS) st.products.delete(k);
}

function ipState(ip: string, now: number): IpState {
  let st = state.get(ip);
  if (!st) {
    st = { files: new Map(), products: new Map() };
    state.set(ip, st);
    if (state.size > 20_000) {
      for (const [k, v] of state) {
        prune(v, now);
        if (!v.files.size && !v.products.size) state.delete(k);
      }
    }
  } else {
    prune(st, now);
  }
  return st;
}

/** 供测试复位。 */
export function _resetDlState() {
  state.clear();
}

export interface GateInput {
  ip: string;
  ua: string;
  path: string;
  hasRange: boolean;
  now?: number;
}

export interface GateDecision {
  /** 放行 / 拦截 */
  allow: boolean;
  status: number;
  reason?: string;
  flags: DlFlag[];
  /** 本次是否是该 IP+文件在去重窗口内的首次命中（决定是否落台账） */
  first: boolean;
}

/**
 * 闸门 + 计数。安装包才设闸；非安装包（yml/blockmap）只做 first 判定不限流。
 * 顺序：办公室免闸 → 空 UA/爬虫 403 → 按不同文件数限流 429 → sweep 标记。
 */
export function gate(input: GateInput): GateDecision {
  const now = input.now ?? Date.now();
  const flags: DlFlag[] = [];
  if (input.hasRange) flags.push("range");
  const st = ipState(input.ip, now);
  const seenAt = st.files.get(input.path);
  const first = seenAt === undefined || now - seenAt > DEDUPE_WINDOW_MS;
  const installer = isInstallerPath(input.path);
  const { product } = classifyPath(input.path);

  if (isTrustedIp(input.ip)) {
    flags.push("office");
    if (first) st.files.set(input.path, now);
    return { allow: true, status: 200, flags, first };
  }

  if (installer) {
    if (!input.ua.trim()) {
      flags.push("noua");
      return { allow: false, status: 403, reason: "empty-ua", flags, first };
    }
    if (isBotUa(input.ua)) {
      flags.push("bot");
      return { allow: false, status: 403, reason: "bot-ua", flags, first };
    }
    // 限流按「窗口内不同安装包数」：新文件才占额度，同一文件的续传/差量 Range 不计。
    if (first && !st.files.has(input.path)) {
      const distinct = [...st.files.keys()].filter(isInstallerPath).length;
      if (distinct >= RATE_MAX_FILES) {
        flags.push("ratelimit");
        return { allow: false, status: 429, reason: "too-many-files", flags, first };
      }
    }
    if (!st.products.has(product)) st.products.set(product, now);
    if (st.products.size >= SWEEP_MIN_PRODUCTS) flags.push("sweep");
  }

  if (first) st.files.set(input.path, now);
  return { allow: true, status: 200, flags, first };
}

// ── 落盘 ────────────────────────────────────────────────────────────
export async function appendDownload(rec: DlRecord): Promise<void> {
  try {
    await mkdir(path.dirname(DOWNLOAD_LOG), { recursive: true });
    await appendFile(DOWNLOAD_LOG, JSON.stringify(rec) + "\n");
  } catch {
    /* 可观测性不反噬下载主链路 */
  }
}

export function buildRecord(
  input: { ip: string; ua: string; ref: string; path: string },
  via: DlVia,
  status: number,
  flags: DlFlag[],
  reason?: string,
): DlRecord {
  const rec: DlRecord = {
    t: new Date().toISOString(),
    ip: input.ip,
    ua: input.ua.slice(0, 250),
    ref: input.ref.slice(0, 300),
    path: input.path,
    ...classifyPath(input.path),
    via,
    status,
    flags,
  };
  if (reason) rec.reason = reason;
  return rec;
}

// ── 读取 / 聚合（控制台）────────────────────────────────────────────
export interface LedgerQuery {
  days: number;
  /** 是否包含爬虫与办公室（默认只看外部真人） */
  includeNoise?: boolean;
  product?: string;
  limit?: number;
}

export interface LedgerRow extends DlRecord {
  /** 该 IP 是否出现过桌面端信标（装过并启动过），及其设备指纹 */
  fps: string[];
  kind: "human" | "office" | "bot" | "blocked";
}

export interface LedgerSummary {
  days: number;
  log_present: boolean;
  total: number;
  human: number;
  office: number;
  bot: number;
  blocked: number;
  by_product: Array<{ product: string; human: number }>;
  by_day: Array<{ day: string; human: number; noise: number }>;
  /** 外部真人 IP 去重 */
  human_ips: number;
  /** 安装包中「下载过且后来装机启动过」的外部 IP 数 */
  human_ips_installed: number;
  rows: LedgerRow[];
}

function rowKind(r: DlRecord): LedgerRow["kind"] {
  if (r.flags.includes("office")) return "office";
  if (r.via === "blocked") return r.flags.includes("bot") || r.flags.includes("noua") ? "bot" : "blocked";
  return "human";
}

async function readJsonl(file: string, maxBytes = 8 * 1024 * 1024): Promise<string[]> {
  try {
    const buf = await readFile(file);
    const slice = buf.length > maxBytes ? buf.subarray(buf.length - maxBytes) : buf;
    const text = slice.toString("utf8");
    const lines = text.split("\n");
    if (buf.length > maxBytes) lines.shift(); // 截断的半行
    return lines.filter(Boolean);
  } catch {
    return [];
  }
}

/** client-logs.jsonl → ip → 设备指纹集合（装机交叉参照）。 */
export async function beaconIpMap(): Promise<Map<string, Set<string>>> {
  const file = process.env.CLIENT_LOG_PATH || path.join(DATA_DIR, "client-logs.jsonl");
  const out = new Map<string, Set<string>>();
  for (const line of await readJsonl(file)) {
    try {
      const o = JSON.parse(line) as { ip?: string; fp?: string };
      if (!o.ip || !o.fp || o.fp.startsWith("TEST-")) continue;
      let s = out.get(o.ip);
      if (!s) out.set(o.ip, (s = new Set()));
      s.add(o.fp);
    } catch {
      /* skip */
    }
  }
  return out;
}

export async function readLedger(q: LedgerQuery): Promise<LedgerSummary> {
  const since = Date.now() - q.days * 86400_000;
  const lines = await readJsonl(DOWNLOAD_LOG, 32 * 1024 * 1024);
  const log_present = lines.length > 0;
  const fpMap = await beaconIpMap();
  const recs: DlRecord[] = [];
  for (const line of lines) {
    try {
      const r = JSON.parse(line) as DlRecord;
      if (!r.t || Date.parse(r.t) < since) continue;
      if (!Array.isArray(r.flags)) r.flags = [];
      if (q.product && r.product !== q.product) continue;
      recs.push(r);
    } catch {
      /* skip */
    }
  }
  recs.sort((a, b) => (a.t < b.t ? 1 : -1));

  const byProduct = new Map<string, number>();
  const byDay = new Map<string, { human: number; noise: number }>();
  const humanIps = new Set<string>();
  const humanInstalled = new Set<string>();
  let human = 0, office = 0, bot = 0, blocked = 0;
  const rows: LedgerRow[] = [];
  const limit = q.limit ?? 300;

  for (const r of recs) {
    const kind = rowKind(r);
    const day = r.t.slice(0, 10);
    const d = byDay.get(day) || { human: 0, noise: 0 };
    if (kind === "human") {
      human++;
      d.human++;
      if (isInstallerPath(r.path)) {
        byProduct.set(r.product, (byProduct.get(r.product) || 0) + 1);
        humanIps.add(r.ip);
        if (fpMap.has(r.ip)) humanInstalled.add(r.ip);
      }
    } else {
      d.noise++;
      if (kind === "office") office++;
      else if (kind === "bot") bot++;
      else blocked++;
    }
    byDay.set(day, d);
    if ((q.includeNoise || kind === "human") && rows.length < limit) {
      rows.push({ ...r, fps: [...(fpMap.get(r.ip) || [])], kind });
    }
  }

  return {
    days: q.days,
    log_present,
    total: recs.length,
    human, office, bot, blocked,
    by_product: [...byProduct].map(([product, n]) => ({ product, human: n })).sort((a, b) => b.human - a.human),
    by_day: [...byDay].map(([day, v]) => ({ day, ...v })).sort((a, b) => (a.day < b.day ? -1 : 1)),
    human_ips: humanIps.size,
    human_ips_installed: humanInstalled.size,
    rows,
  };
}
