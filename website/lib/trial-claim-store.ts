import { mkdir, readFile, writeFile, rename, appendFile } from "fs/promises";
import crypto from "crypto";
import path from "path";
import { DATA_DIR } from "./data-dir";

/**
 * 试用领取台账（P2）：`trial-claims.json`（键控 + 状态机）+ `trial-claims.jsonl`（追加审计）。
 * 与 order-store / unlock-store 同款的原子写 + 单进程串行化模式。
 *
 * 为什么这个文件是整套试用设计的**唯一真闸门**
 * ============================================
 * 客户端那份「首启体验档」是本地状态文件，删掉即重置——这是刻意的取舍（赠量很小、
 * 不值得为它把私钥塞进安装包）。真正防「一份 7 天试用发给一群人 / 同一台机反复领」
 * 的只有这里：**按机器指纹去重**。所以 createClaim 必须是幂等的：同一指纹再来，
 * 返回既有记录，绝不新建第二条。
 *
 * 为什么授权不在官网签
 * ====================
 * Ed25519 私钥留在厂商机（见 engines/avatarhub/fulfill_orders.py 与
 * app/api/activate/route.ts 的安全模型说明）：官网只写 `pending`，厂商机轮询
 * `/api/admin/trial-claims?status=pending` → 本地签发 7 天授权 → 回填 `license`；
 * 客户端经 `/api/trial/claim-status` 取回。服务器被攻破也伪造不出授权。
 *
 * 加客服送额度同理：客服核销绑定码只把 `bindRedeemedAt` 标上，真加量凭证
 * （topup voucher，同一把私钥签）仍由厂商机回填 `topupVoucher`。
 */

const DIR = DATA_DIR;
const DB = process.env.TRIAL_CLAIMS_DB || path.join(DIR, "trial-claims.json");
const LOG = process.env.TRIAL_CLAIMS_LOG || path.join(DIR, "trial-claims.jsonl");

export type ClaimStatus = "pending" | "issued" | "rejected";
export type ContactKind = "telegram" | "whatsapp" | "email" | "other";

export interface TrialClaim {
  /** 客户端持有的查询凭据（随机、不可枚举）。claim-status 必须带它。 */
  id: string;
  /** 机器指纹（去重主键，形如 XXXX-XXXX-XXXX-XXXX）。 */
  fingerprint: string;
  contact: string;
  contactKind: ContactKind;
  /** 归因来源（下载渠道 / 活动），供市场看漏斗。 */
  source?: string;
  /** 产品线（当前只有 chatx，留字段以便后续复用同一台账）。 */
  product: string;
  createdAt: string;
  status: ClaimStatus;

  /** 厂商机回填：base64 编码的 7 天授权。 */
  license?: string;
  issuedAt?: string;
  rejectedReason?: string;

  // ── 加客服送额度（绑定码 → 客服核销 → 厂商机回填加量凭证）──
  bindCode?: string;
  bindCodeIssuedAt?: string;
  bindRedeemedAt?: string;
  /** 核销时客服/运营登记的赠送额度（字符数），供厂商机签凭证时取用。 */
  bindChars?: number;
  /** 厂商机回填：加量凭证（客户端粘贴/自动兑换）。 */
  topupVoucher?: string;
  topupIssuedAt?: string;
}

interface ClaimDb {
  version: 1;
  byId: Record<string, TrialClaim>;
  /** 指纹 → claimId，去重索引（大小写归一后的指纹） */
  byFingerprint: Record<string, string>;
  /** 厂商机最后一次来取待办的时间（ISO）。见 touchFulfiller。 */
  fulfillerLastSeen?: string;
}

// ── 单进程写串行化（避免并发重写互相覆盖）──
let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<ClaimDb> {
  try {
    const raw = await readFile(DB, "utf-8");
    const parsed = JSON.parse(raw);
    if (parsed?.byId) {
      return {
        version: 1,
        byId: parsed.byId,
        byFingerprint: parsed.byFingerprint || {},
        fulfillerLastSeen: parsed.fulfillerLastSeen || undefined,
      };
    }
  } catch {
    /* fresh */
  }
  return { version: 1, byId: {}, byFingerprint: {} };
}

async function writeDb(db: ClaimDb) {
  await mkdir(DIR, { recursive: true });
  const tmp = DB + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, DB);
}

async function audit(event: string, claim: TrialClaim, extra?: Record<string, unknown>) {
  try {
    await mkdir(path.dirname(LOG), { recursive: true });
    // 审计流水刻意不写 license / voucher 原文：它们是可用凭证，落进日志等于多一份泄露面。
    const rec = {
      t: new Date().toISOString(),
      event,
      id: claim.id,
      fingerprint: claim.fingerprint,
      contact: claim.contact,
      status: claim.status,
      ...(extra || {}),
    };
    await appendFile(LOG, JSON.stringify(rec) + "\n");
  } catch {
    /* 审计失败不阻断主链路 */
  }
}

/** 机器指纹归一 + 形状校验。脏值直接拒，防垃圾键把去重索引撑爆。 */
export function normalizeFingerprint(raw: unknown): string {
  const s = String(raw ?? "").trim().toUpperCase();
  if (!/^[0-9A-F]{4}(-[0-9A-F]{4}){3}$/.test(s)) return "";
  return s;
}

export function classifyContact(raw: unknown): { value: string; kind: ContactKind } {
  const s = String(raw ?? "").trim().slice(0, 120);
  if (!s) return { value: "", kind: "other" };
  if (/^@[A-Za-z0-9_]{4,32}$/.test(s)) return { value: s, kind: "telegram" };
  if (/^https?:\/\/t\.me\//i.test(s)) return { value: s, kind: "telegram" };
  if (/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(s)) return { value: s, kind: "email" };
  if (/^\+?[0-9][0-9\s-]{6,19}$/.test(s)) return { value: s, kind: "whatsapp" };
  return { value: s, kind: "other" };
}

function genId(): string {
  return crypto.randomBytes(16).toString("hex");
}

// 绑定码字母表刻意剔除易混字符（0/O/1/I/L）：用户要念给客服听，或手打进对话框。
const BIND_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";

function genBindCode(): string {
  const pick = () =>
    Array.from(crypto.randomBytes(4))
      .map((b) => BIND_ALPHABET[b % BIND_ALPHABET.length])
      .join("");
  return `BC-${pick()}-${pick()}`;
}

export function normalizeBindCode(raw: unknown): string {
  const s = String(raw ?? "").trim().toUpperCase().replace(/\s+/g, "");
  return /^BC-[0-9A-Z]{4}-[0-9A-Z]{4}$/.test(s) ? s : "";
}

export type CreateClaimInput = {
  fingerprint: string;
  contact: string;
  source?: string;
  product?: string;
};

export type CreateClaimResult =
  | { ok: true; claim: TrialClaim; deduped: boolean }
  | { ok: false; reason: "bad_fingerprint" | "contact_required" };

/**
 * 建一条试用领取（**按机器指纹幂等**）。
 *
 * 同一台机器再来 → 返回既有记录并 `deduped: true`，绝不新建第二条。这是「一机一份
 * 试用」的唯一执行点：客户端本地状态可以被删，台账不能。
 */
export async function createClaim(input: CreateClaimInput): Promise<CreateClaimResult> {
  const fp = normalizeFingerprint(input.fingerprint);
  if (!fp) return { ok: false, reason: "bad_fingerprint" };
  const { value: contact, kind } = classifyContact(input.contact);
  if (!contact) return { ok: false, reason: "contact_required" };

  return serialize(async () => {
    const db = await readDb();
    const existingId = db.byFingerprint[fp];
    const existing = existingId ? db.byId[existingId] : undefined;
    if (existing) {
      // 只**补空**、不覆盖：机器指纹不是秘密（界面上就显示、工单截图里常见），
      // 允许覆盖等于「知道指纹就能改掉别人 claim 的联系方式」，把赠量导给自己。
      // 用户真填错了走客服改，别在无鉴权的公开口上开这个洞。
      let changed = false;
      if (contact && !existing.contact) {
        existing.contact = contact;
        existing.contactKind = kind;
        changed = true;
      }
      if (input.source && !existing.source) {
        existing.source = String(input.source).slice(0, 60);
        changed = true;
      }
      if (changed) {
        await writeDb(db);
        await audit("claim_reused_updated", existing);
      }
      return { ok: true, claim: { ...existing }, deduped: true } as CreateClaimResult;
    }

    const claim: TrialClaim = {
      id: genId(),
      fingerprint: fp,
      contact,
      contactKind: kind,
      source: input.source ? String(input.source).slice(0, 60) : undefined,
      product: String(input.product || "chatx").slice(0, 24),
      createdAt: new Date().toISOString(),
      status: "pending",
    };
    db.byId[claim.id] = claim;
    db.byFingerprint[fp] = claim.id;
    await writeDb(db);
    await audit("claim_created", claim);
    return { ok: true, claim: { ...claim }, deduped: false } as CreateClaimResult;
  });
}

export async function getClaim(id: string): Promise<TrialClaim | null> {
  const key = String(id || "").trim();
  if (!key) return null;
  const db = await readDb();
  const rec = db.byId[key];
  return rec ? { ...rec } : null;
}

export async function getClaimByFingerprint(fp: string): Promise<TrialClaim | null> {
  const key = normalizeFingerprint(fp);
  if (!key) return null;
  const db = await readDb();
  const id = db.byFingerprint[key];
  return id && db.byId[id] ? { ...db.byId[id] } : null;
}

/** 待签发队列（厂商机轮询用）。 */
export async function listClaims(
  opts?: { status?: ClaimStatus; limit?: number; needsTopup?: boolean }
): Promise<TrialClaim[]> {
  const db = await readDb();
  let all = Object.values(db.byId);
  if (opts?.status) all = all.filter((c) => c.status === opts.status);
  if (opts?.needsTopup) {
    // 已核销但凭证还没签 → 厂商机该干活的那一批
    all = all.filter((c) => !!c.bindRedeemedAt && !c.topupVoucher);
  }
  return all
    .sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1))
    .slice(0, Math.max(1, Math.min(500, opts?.limit ?? 100)));
}

export type FulfillInput = {
  id: string;
  license?: string;
  topupVoucher?: string;
  topupChars?: number;
  status?: ClaimStatus;
  reason?: string;
};

/**
 * 厂商机回填（幂等）：签好的授权 / 加量凭证写回台账。
 *
 * 幂等语义：同一条 claim 重复回填同一份 license 视为成功（履约脚本重跑是常态）；
 * 已 issued 的 claim 不因再次回填而重置 issuedAt。
 */
export async function fulfillClaim(input: FulfillInput): Promise<TrialClaim | null> {
  const id = String(input.id || "").trim();
  if (!id) return null;
  return serialize(async () => {
    const db = await readDb();
    const rec = db.byId[id];
    if (!rec) return null;
    let changed = false;
    if (input.license && input.license !== rec.license) {
      rec.license = String(input.license);
      rec.issuedAt = rec.issuedAt || new Date().toISOString();
      rec.status = "issued";
      changed = true;
    }
    if (input.topupVoucher && input.topupVoucher !== rec.topupVoucher) {
      rec.topupVoucher = String(input.topupVoucher);
      rec.topupIssuedAt = new Date().toISOString();
      if (input.topupChars) rec.bindChars = Number(input.topupChars) || rec.bindChars;
      changed = true;
    }
    if (input.status && input.status !== rec.status) {
      rec.status = input.status;
      if (input.status === "rejected") rec.rejectedReason = String(input.reason || "").slice(0, 200);
      changed = true;
    }
    if (changed) {
      await writeDb(db);
      await audit("claim_fulfilled", rec, {
        has_license: !!rec.license,
        has_voucher: !!rec.topupVoucher,
      });
    }
    return { ...rec };
  });
}

/** 生成/返回一次性绑定码（幂等：同一 claim 永远同一码）。 */
export async function issueBindCode(id: string): Promise<TrialClaim | null> {
  const key = String(id || "").trim();
  if (!key) return null;
  return serialize(async () => {
    const db = await readDb();
    const rec = db.byId[key];
    if (!rec) return null;
    if (!rec.bindCode) {
      // 极小概率撞码：整库扫一遍确保唯一（量级 ≤ 万，够用且比引入依赖简单）
      const used = new Set(Object.values(db.byId).map((c) => c.bindCode).filter(Boolean));
      let code = genBindCode();
      for (let i = 0; i < 20 && used.has(code); i += 1) code = genBindCode();
      rec.bindCode = code;
      rec.bindCodeIssuedAt = new Date().toISOString();
      await writeDb(db);
      await audit("bind_code_issued", rec, { bind_code: rec.bindCode });
    }
    return { ...rec };
  });
}

export type RedeemBindResult =
  | { ok: true; claim: TrialClaim; alreadyRedeemed: boolean }
  | { ok: false; reason: "bad_code" | "not_found" };

/**
 * 客服核销绑定码（幂等）。
 *
 * 只标「已核销 + 应赠多少字符」，**不签凭证**——真凭证由厂商机回填（私钥不上服务器）。
 * 重复核销返回 `alreadyRedeemed: true` 而非报错：客服手滑点两次不该变成两笔赠量。
 */
export async function redeemBindCode(
  code: string, opts?: { chars?: number; by?: string }
): Promise<RedeemBindResult> {
  const norm = normalizeBindCode(code);
  if (!norm) return { ok: false, reason: "bad_code" };
  return serialize(async () => {
    const db = await readDb();
    const rec = Object.values(db.byId).find((c) => c.bindCode === norm);
    if (!rec) return { ok: false, reason: "not_found" } as RedeemBindResult;
    const alreadyRedeemed = !!rec.bindRedeemedAt;
    if (!alreadyRedeemed) {
      rec.bindRedeemedAt = new Date().toISOString();
      if (opts?.chars) rec.bindChars = Math.max(0, Math.round(Number(opts.chars) || 0));
      await writeDb(db);
      await audit("bind_code_redeemed", rec, {
        bind_code: norm, chars: rec.bindChars, by: opts?.by || "",
      });
    }
    return { ok: true, claim: { ...rec }, alreadyRedeemed } as RedeemBindResult;
  });
}

/**
 * 记一次「厂商机来过」。
 *
 * 心跳不另开端点：履约脚本本来就每轮 GET 一次待办队列，**那次 GET 就是心跳**。
 * 单独做个 /heartbeat 反而要求脚本额外配合，而且「取待办」比「发心跳」更贴近
 * 真实存活语义——能取待办才是真的还能干活。
 *
 * 只在跨过一分钟时才落盘：轮询很密，每次都写等于把台账当日志刷。
 */
export async function touchFulfiller(): Promise<void> {
  const db = await readDb();
  const last = db.fulfillerLastSeen ? Date.parse(db.fulfillerLastSeen) : 0;
  if (Number.isFinite(last) && Date.now() - last < 60_000) return;
  await serialize(async () => {
    const fresh = await readDb();
    fresh.fulfillerLastSeen = new Date().toISOString();
    await writeDb(fresh);
  });
}

export async function claimStats(): Promise<{
  total: number;
  pending: number;
  issued: number;
  rejected: number;
  bindIssued: number;
  bindRedeemed: number;
  topupIssued: number;
  /** 厂商机最后活跃（ISO，从未来过为空串）——签发链停摆的唯一可观测信号。 */
  fulfillerLastSeen: string;
  /** 最老待办等了多少分钟（无待办为 0）。与心跳合看即可判断「是没活干还是挂了」。 */
  oldestPendingMin: number;
}> {
  const db = await readDb();
  const all = Object.values(db.byId);
  const now = Date.now();
  const pendingAges = all
    .filter((c) => c.status === "pending")
    .map((c) => (now - Date.parse(c.createdAt)) / 60_000)
    .filter((n) => Number.isFinite(n) && n > 0);
  return {
    total: all.length,
    pending: all.filter((c) => c.status === "pending").length,
    issued: all.filter((c) => c.status === "issued").length,
    rejected: all.filter((c) => c.status === "rejected").length,
    bindIssued: all.filter((c) => !!c.bindCode).length,
    bindRedeemed: all.filter((c) => !!c.bindRedeemedAt).length,
    topupIssued: all.filter((c) => !!c.topupVoucher).length,
    fulfillerLastSeen: db.fulfillerLastSeen || "",
    oldestPendingMin: pendingAges.length ? Math.floor(Math.max(...pendingAges)) : 0,
  };
}
