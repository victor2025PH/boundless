import { mkdir, readFile, writeFile, rename, appendFile } from "fs/promises";
import crypto from "crypto";
import path from "path";
import { DATA_DIR } from "./data-dir";
import { getClaim, type TrialClaim } from "./trial-claim-store";

/**
 * 邀请裂变台账（2026-08-11）：`referrals.json`（键控 + 状态机）+ `referrals.jsonl`（追加审计）。
 * 与 trial-claim-store 同款的原子写 + 单进程串行化模式。
 *
 * 设计要点
 * ========
 * - **邀请码属于 claim**：领过免费额度（有 claim）才有邀请码——奖励要有授权可入账，
 *   这同时把「邀请人必须是真实注册用户」变成结构性事实而非校验规则。
 * - **官网只记账，绝不签发**：奖励凭证由厂商机（私钥离线）按 due 清单签发回填到
 *   双方 claim 的 extraVouchers；本店只做归因、防刷判定与状态机。
 * - **奖励两段式**：被邀请人注册成功（license 签发）→ 见面礼；被邀请人真实消耗
 *   ≥ QUALIFY_CHARS（客户端 usage-beacon 水位）→ 邀请人奖励。挡「批量注册即弃」。
 *
 * 防刷（谁都绕不过的在前，启发式的在后）：
 *   1. 一机一 claim（trial-claim-store 指纹去重）→ 被邀请人必须是全新机器；
 *   2. 邀请人/被邀请人指纹相同 → self_invite 拒绝；
 *   3. 每个被邀请人只能被归因一次（byInvitee 唯一索引，幂等返回既有记录）；
 *   4. 邀请人日注册上限 / 累计上限（超限拒绝新归因）；
 *   5. 同一邀请人名下多个被邀请人共用出口 IP → 标 flagged 进人审（客服控制台批准后才发奖）。
 */

const DIR = DATA_DIR;
const DB = process.env.REFERRALS_DB || path.join(DIR, "referrals.json");
const LOG = process.env.REFERRALS_LOG || path.join(DIR, "referrals.jsonl");

// 业务参数（env 可调；与厂商机 chatx_fulfillment.REFERRAL_* 兜底默认对齐）
export const REFERRAL_INVITEE_CHARS = Number(process.env.REFERRAL_INVITEE_CHARS || 100_000);
export const REFERRAL_INVITER_CHARS = Number(process.env.REFERRAL_INVITER_CHARS || 100_000);
export const REFERRAL_QUALIFY_CHARS = Number(process.env.REFERRAL_QUALIFY_CHARS || 10_000);
export const REFERRAL_DAILY_CAP = Number(process.env.REFERRAL_DAILY_CAP || 5);
/** 累计上限（2026-08-21 实施50：20 → 50，给 10 人里程碑留空间；超限新归因仍拒，
 *  真有超级推广者时人工放行/改 env）。 */
export const REFERRAL_TOTAL_CAP = Number(process.env.REFERRAL_TOTAL_CAP || 50);
/** 同一邀请人名下、同一出口 IP 的被邀请人达到此数即标 flagged 人审。 */
const IP_CLUSTER_FLAG_AT = Number(process.env.REFERRAL_IP_CLUSTER_FLAG_AT || 3);

// ── 里程碑阶梯 + 首充返利（2026-08-21 实施50 P1）──────────────────────────────
/** 邀请里程碑：达标人数（qualified 口径）→ 额外奖励字符。每档每人一次。 */
export const REFERRAL_MILESTONES: ReadonlyArray<{ at: number; chars: number }> = [
  { at: 3, chars: 200_000 },
  { at: 5, chars: 500_000 },
  { at: 10, chars: 1_000_000 },
];
/** 被邀请人首笔已付充值的返利比例（%）与封顶（等值 U）。 */
export const REFERRAL_REBATE_PCT = Number(process.env.REFERRAL_REBATE_PCT || 10);
export const REFERRAL_REBATE_CAP_USD = Number(process.env.REFERRAL_REBATE_CAP_USD || 100);
/** 返利延迟发放天数（退款/拒付观察窗——订单状态机没有 refund，延迟是唯一防线）。 */
export const REFERRAL_REBATE_DELAY_DAYS = Number(process.env.REFERRAL_REBATE_DELAY_DAYS || 7);
/** 1U = 1,500 Token = 150,000 字符（与 chatx-pricing / 引擎并账口径同源）。 */
const CHARS_PER_USD = 150_000;

export type ReferralStatus = "registered" | "qualified" | "flagged" | "rejected";

export interface Referral {
  id: string;
  code: string;
  inviterClaimId: string;
  inviteeClaimId: string;
  inviteeFingerprint: string;
  inviteeIp?: string;
  status: ReferralStatus;
  createdAt: string;
  qualifiedAt?: string;
  flagReason?: string;
  /** 人审批准记录（flagged → registered/qualified 时由谁批的）。 */
  approvedBy?: string;
  // 厂商机发奖回填（幂等标记；凭证本体挂在双方 claim.extraVouchers）
  inviteeRewardedAt?: string;
  inviteeChars?: number;
  inviterRewardedAt?: string;
  inviterChars?: number;
  // ── 首充返利（实施50 P1；每条归因至多一次）──
  rebateRewardedAt?: string;
  rebateChars?: number;
  /** 触发返利的被邀请人首笔已付充值订单号（审计锚）。 */
  rebateOrderId?: string;
}

interface ReferralDb {
  version: 1;
  /** claimId → 邀请码（一 claim 一码，幂等生成）。 */
  codeByClaim: Record<string, string>;
  /** 邀请码 → claimId（查询索引）。 */
  claimByCode: Record<string, string>;
  referrals: Record<string, Referral>;
  /** inviteeClaimId → referralId（一个被邀请人只能被归因一次）。 */
  byInvitee: Record<string, string>;
  /** 里程碑发放台账：inviterClaimId → { "3": {rewardedAt, chars}, ... }（每档一次）。 */
  milestones: Record<string, Record<string, { rewardedAt: string; chars: number }>>;
}

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<ReferralDb> {
  try {
    const raw = await readFile(DB, "utf-8");
    const parsed = JSON.parse(raw);
    if (parsed?.referrals || parsed?.codeByClaim) {
      return {
        version: 1,
        codeByClaim: parsed.codeByClaim || {},
        claimByCode: parsed.claimByCode || {},
        referrals: parsed.referrals || {},
        byInvitee: parsed.byInvitee || {},
        milestones: parsed.milestones || {},
      };
    }
  } catch {
    /* fresh */
  }
  return { version: 1, codeByClaim: {}, claimByCode: {}, referrals: {}, byInvitee: {}, milestones: {} };
}

async function writeDb(db: ReferralDb) {
  await mkdir(DIR, { recursive: true });
  const tmp = DB + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, DB);
}

async function audit(event: string, rec: Partial<Referral>, extra?: Record<string, unknown>) {
  try {
    await mkdir(path.dirname(LOG), { recursive: true });
    await appendFile(
      LOG,
      JSON.stringify({ t: new Date().toISOString(), event, ...rec, ...(extra || {}) }) + "\n"
    );
  } catch {
    /* 审计失败不阻断主链路 */
  }
}

// 与绑定码同一套「念得出、打得对」字母表（剔除 0/O/1/I/L）
const CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";

function genCode(): string {
  return (
    "ZL-" +
    Array.from(crypto.randomBytes(6))
      .map((b) => CODE_ALPHABET[b % CODE_ALPHABET.length])
      .join("")
  );
}

export function normalizeInviteCode(raw: unknown): string {
  const s = String(raw ?? "").trim().toUpperCase().replace(/\s+/g, "");
  return /^ZL-[0-9A-Z]{6}$/.test(s) ? s : "";
}

/** 该 claim 的邀请码（幂等：一 claim 永远同一码；首次调用生成）。 */
export async function getOrCreateInviteCode(claimId: string): Promise<string> {
  const key = String(claimId || "").trim();
  if (!key) return "";
  return serialize(async () => {
    const db = await readDb();
    const existing = db.codeByClaim[key];
    if (existing) return existing;
    let code = genCode();
    for (let i = 0; i < 20 && db.claimByCode[code]; i += 1) code = genCode();
    db.codeByClaim[key] = code;
    db.claimByCode[code] = key;
    await writeDb(db);
    await audit("invite_code_issued", { inviterClaimId: key, code });
    return code;
  });
}

export type RegisterReferralResult =
  | { ok: true; referral: Referral; already: boolean }
  | {
      ok: false;
      reason:
        | "invalid_code"
        | "self_invite"
        | "cap_daily"
        | "cap_total"
        | "inviter_gone";
    };

/**
 * 被邀请人建单成功后归因（**只对全新 claim 调用**——指纹去重命中的旧机器不算新用户）。
 * 幂等：同一被邀请人重复归因返回既有记录。
 */
export async function registerReferral(input: {
  code: string;
  invitee: TrialClaim;
  inviteeIp?: string;
}): Promise<RegisterReferralResult> {
  const code = normalizeInviteCode(input.code);
  if (!code) return { ok: false, reason: "invalid_code" };
  const invitee = input.invitee;
  return serialize(async () => {
    const db = await readDb();
    const inviterClaimId = db.claimByCode[code];
    if (!inviterClaimId) return { ok: false, reason: "invalid_code" } as RegisterReferralResult;

    const existingId = db.byInvitee[invitee.id];
    if (existingId && db.referrals[existingId]) {
      return { ok: true, referral: { ...db.referrals[existingId] }, already: true } as RegisterReferralResult;
    }
    if (inviterClaimId === invitee.id) {
      return { ok: false, reason: "self_invite" } as RegisterReferralResult;
    }
    const inviter = await getClaim(inviterClaimId);
    if (!inviter) return { ok: false, reason: "inviter_gone" } as RegisterReferralResult;
    if (inviter.fingerprint === invitee.fingerprint) {
      return { ok: false, reason: "self_invite" } as RegisterReferralResult;
    }

    const mine = Object.values(db.referrals).filter(
      (r) => r.inviterClaimId === inviterClaimId && r.status !== "rejected"
    );
    if (mine.length >= REFERRAL_TOTAL_CAP) {
      return { ok: false, reason: "cap_total" } as RegisterReferralResult;
    }
    const today = new Date().toISOString().slice(0, 10);
    if (mine.filter((r) => r.createdAt.slice(0, 10) === today).length >= REFERRAL_DAILY_CAP) {
      return { ok: false, reason: "cap_daily" } as RegisterReferralResult;
    }

    // IP 聚集启发：同邀请人名下已有 ≥N-1 个同 IP 被邀请人 → 本条进人审。
    const ip = String(input.inviteeIp || "").trim();
    let status: ReferralStatus = "registered";
    let flagReason = "";
    if (ip) {
      const sameIp = mine.filter((r) => r.inviteeIp && r.inviteeIp === ip).length;
      if (sameIp >= IP_CLUSTER_FLAG_AT - 1) {
        status = "flagged";
        flagReason = "ip_cluster";
      }
    }

    const rec: Referral = {
      id: crypto.randomBytes(12).toString("hex"),
      code,
      inviterClaimId,
      inviteeClaimId: invitee.id,
      inviteeFingerprint: invitee.fingerprint,
      inviteeIp: ip || undefined,
      status,
      createdAt: new Date().toISOString(),
      flagReason: flagReason || undefined,
    };
    db.referrals[rec.id] = rec;
    db.byInvitee[invitee.id] = rec.id;
    await writeDb(db);
    await audit("referral_registered", rec);
    return { ok: true, referral: { ...rec }, already: false } as RegisterReferralResult;
  });
}

/** 被邀请人消耗水位更新时的达标推进（usage-beacon 调用）。 */
export async function noteInviteeUsage(inviteeClaimId: string, usedChars: number): Promise<void> {
  const key = String(inviteeClaimId || "").trim();
  const used = Math.max(0, Math.round(Number(usedChars) || 0));
  if (!key || used < REFERRAL_QUALIFY_CHARS) return;
  await serialize(async () => {
    const db = await readDb();
    const rid = db.byInvitee[key];
    const rec = rid ? db.referrals[rid] : undefined;
    if (!rec || rec.status !== "registered") return;
    rec.status = "qualified";
    rec.qualifiedAt = new Date().toISOString();
    await writeDb(db);
    await audit("referral_qualified", rec, { used_chars: used });
  });
}

/** 客服人审：flagged → 放行（消耗已达标则直接 qualified）。 */
export async function approveReferral(id: string, by: string): Promise<Referral | null> {
  const key = String(id || "").trim();
  if (!key) return null;
  return serialize(async () => {
    const db = await readDb();
    const rec = db.referrals[key];
    if (!rec || rec.status !== "flagged") return rec ? { ...rec } : null;
    rec.approvedBy = String(by || "").slice(0, 60);
    rec.flagReason = undefined;
    const invitee = await getClaim(rec.inviteeClaimId);
    const used = invitee?.usedChars || 0;
    if (used >= REFERRAL_QUALIFY_CHARS) {
      rec.status = "qualified";
      rec.qualifiedAt = rec.qualifiedAt || new Date().toISOString();
    } else {
      rec.status = "registered";
    }
    await writeDb(db);
    await audit("referral_approved", rec, { by });
    return { ...rec };
  });
}

export async function listReferrals(opts?: {
  status?: ReferralStatus;
  inviterClaimId?: string;
  limit?: number;
}): Promise<Referral[]> {
  const db = await readDb();
  let all = Object.values(db.referrals);
  if (opts?.status) all = all.filter((r) => r.status === opts.status);
  if (opts?.inviterClaimId) all = all.filter((r) => r.inviterClaimId === opts.inviterClaimId);
  return all
    .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
    .slice(0, Math.max(1, Math.min(500, opts?.limit ?? 100)));
}

export interface DueReward {
  id: string;
  invitee_due: boolean;
  inviter_due: boolean;
  invitee_claim_id: string;
  invitee_fingerprint: string;
  invitee_contact: string;
  invitee_chars: number;
  inviter_claim_id: string;
  inviter_fingerprint: string;
  inviter_contact: string;
  inviter_chars: number;
}

/**
 * 厂商机的发奖待办清单：
 * - 见面礼 due：归因成立（registered/qualified）+ 被邀请人 license 已签发 + 未发过；
 * - 邀请人奖励 due：qualified + 未发过。
 * flagged/rejected 一律不入清单（人审批准后自然回到 registered/qualified）。
 */
export async function listDueRewards(limit = 100): Promise<DueReward[]> {
  const db = await readDb();
  const out: DueReward[] = [];
  for (const rec of Object.values(db.referrals)) {
    if (rec.status !== "registered" && rec.status !== "qualified") continue;
    const inviteeDueMaybe = !rec.inviteeRewardedAt;
    const inviterDue = rec.status === "qualified" && !rec.inviterRewardedAt;
    if (!inviteeDueMaybe && !inviterDue) continue;
    const [invitee, inviter] = await Promise.all([
      getClaim(rec.inviteeClaimId),
      getClaim(rec.inviterClaimId),
    ]);
    if (!invitee || !inviter) continue;
    const inviteeDue = inviteeDueMaybe && invitee.status === "issued" && !!invitee.license;
    if (!inviteeDue && !inviterDue) continue;
    out.push({
      id: rec.id,
      invitee_due: inviteeDue,
      inviter_due: inviterDue,
      invitee_claim_id: invitee.id,
      invitee_fingerprint: invitee.fingerprint,
      invitee_contact: invitee.contact,
      invitee_chars: REFERRAL_INVITEE_CHARS,
      inviter_claim_id: inviter.id,
      inviter_fingerprint: inviter.fingerprint,
      inviter_contact: inviter.contact,
      inviter_chars: REFERRAL_INVITER_CHARS,
    });
    if (out.length >= limit) break;
  }
  return out;
}

/** 厂商机发奖回填后的幂等标记（凭证本体已挂到双方 claim.extraVouchers）。 */
export async function markRewarded(
  id: string,
  opts: { invitee?: boolean; inviteeChars?: number; inviter?: boolean; inviterChars?: number }
): Promise<Referral | null> {
  const key = String(id || "").trim();
  if (!key) return null;
  return serialize(async () => {
    const db = await readDb();
    const rec = db.referrals[key];
    if (!rec) return null;
    let changed = false;
    if (opts.invitee && !rec.inviteeRewardedAt) {
      rec.inviteeRewardedAt = new Date().toISOString();
      rec.inviteeChars = Math.max(0, Math.round(Number(opts.inviteeChars) || 0));
      changed = true;
    }
    if (opts.inviter && !rec.inviterRewardedAt) {
      rec.inviterRewardedAt = new Date().toISOString();
      rec.inviterChars = Math.max(0, Math.round(Number(opts.inviterChars) || 0));
      changed = true;
    }
    if (changed) {
      await writeDb(db);
      await audit("referral_rewarded", rec, {
        invitee: !!opts.invitee,
        inviter: !!opts.inviter,
      });
    }
    return { ...rec };
  });
}

// ── 里程碑 / 首充返利：厂商机发奖待办与幂等回填（实施50 P1）─────────────────
// 与见面礼/邀请奖励同一协议形态：官网只算 due 清单，凭证由厂商机签、经
// POST /api/admin/referrals {bonus:[…]} 挂到邀请人 claim.extraVouchers 后回填标记。

export interface BonusDue {
  /** 回填幂等键：mile:{inviterClaimId}:{at} 或 rebate:{referralId}。 */
  key: string;
  kind: "milestone" | "rebate";
  claim_id: string;
  fingerprint: string;
  contact: string;
  chars: number;
  /** 凭证兑换幂等 ref（厂商机原样用作 voucher.ref）。 */
  ref: string;
  note: string;
}

function rebateCharsForOrder(o: { amount?: number; pay_amount?: number }): number {
  // 返利基数=实收金额（USDT 尾数几分钱忽略，取挂牌 amount；amount 缺失回退 pay_amount 取整）
  const usd = Math.max(0, Number(o.amount) || Math.floor(Number(o.pay_amount) || 0));
  const rebateUsd = Math.min((usd * REFERRAL_REBATE_PCT) / 100, REFERRAL_REBATE_CAP_USD);
  return Math.round(rebateUsd * CHARS_PER_USD);
}

/** 厂商机的加码发奖待办：里程碑（qualified 人数达标）+ 首充返利（被邀请人首笔
 *  已付充值、过了退款观察窗）。flagged/rejected 归因不参与返利。 */
export async function listBonusDue(limit = 100): Promise<BonusDue[]> {
  const db = await readDb();
  const out: BonusDue[] = [];
  const all = Object.values(db.referrals);

  // ① 里程碑：按邀请人聚合 qualified 数
  const qualifiedBy = new Map<string, number>();
  for (const r of all) {
    if (r.status !== "qualified") continue;
    qualifiedBy.set(r.inviterClaimId, (qualifiedBy.get(r.inviterClaimId) || 0) + 1);
  }
  for (const [inviterClaimId, count] of qualifiedBy) {
    const done = db.milestones[inviterClaimId] || {};
    for (const m of REFERRAL_MILESTONES) {
      if (count < m.at || done[String(m.at)]) continue;
      const inviter = await getClaim(inviterClaimId);
      if (!inviter) continue;
      out.push({
        key: `mile:${inviterClaimId}:${m.at}`,
        kind: "milestone",
        claim_id: inviter.id,
        fingerprint: inviter.fingerprint,
        contact: inviter.contact,
        chars: m.chars,
        ref: `refmile-${inviterClaimId.slice(0, 16)}-${m.at}`,
        note: `referral-milestone-${m.at}`,
      });
      if (out.length >= limit) return out;
    }
  }

  // ② 首充返利：被邀请人首笔已付充值单（recharge-* 含新人包），过观察窗才 due。
  //    动态 import 防模块环（order-store 不依赖本文件，纯保守写法）。
  const rebateCandidates = all.filter(
    (r) => (r.status === "registered" || r.status === "qualified") && !r.rebateRewardedAt
  );
  if (rebateCandidates.length) {
    const { listOrders } = await import("./order-store");
    const { contactCore } = await import("./newbie-gate");
    let orders: Awaited<ReturnType<typeof listOrders>> = [];
    try {
      orders = await listOrders();
    } catch {
      return out; // 订单台账不可读：返利段本轮跳过（下轮自愈）
    }
    const normFp = (s: unknown) =>
      String(s ?? "").trim().toUpperCase().replace(/[-\s]/g, "");
    const now = Date.now();
    for (const rec of rebateCandidates) {
      const invitee = await getClaim(rec.inviteeClaimId);
      if (!invitee) continue;
      const fp = normFp(invitee.fingerprint);
      const cc = contactCore(invitee.contact);
      const mine = orders
        .filter((o) => {
          const sku = String(o.sku_id || o.plan || "");
          if (!sku.startsWith("recharge")) return false;
          if (o.status !== "paid" && o.status !== "activated") return false;
          if (/e2e/i.test(String(o.contact || "")) ||
              normFp(o.fingerprint).startsWith("E2E")) return false;
          const of = normFp(o.fingerprint);
          const oc = contactCore(o.contact);
          return (fp && of && of === fp) || (cc && oc && oc === cc);
        })
        .sort((a, b) => (Date.parse(a.paid_at || a.t) || 0) - (Date.parse(b.paid_at || b.t) || 0));
      const first = mine[0];
      if (!first) continue;
      const paidAt = Date.parse(first.paid_at || first.t) || 0;
      if (!paidAt || now - paidAt < REFERRAL_REBATE_DELAY_DAYS * 86_400_000) continue;
      const chars = rebateCharsForOrder(first);
      if (chars <= 0) continue;
      const inviter = await getClaim(rec.inviterClaimId);
      if (!inviter) continue;
      out.push({
        key: `rebate:${rec.id}`,
        kind: "rebate",
        claim_id: inviter.id,
        fingerprint: inviter.fingerprint,
        contact: inviter.contact,
        chars,
        ref: `refrebate-${rec.id}`,
        note: `referral-rebate-${first.id}`,
      });
      if (out.length >= limit) break;
    }
  }
  return out;
}

/** 厂商机加码发奖回填（幂等）：key 决定标记落点。返回是否新标记。 */
export async function markBonusRewarded(key: string, chars: number, note = ""): Promise<boolean> {
  const k = String(key || "").trim();
  const n = Math.max(0, Math.round(Number(chars) || 0));
  if (!k) return false;
  return serialize(async () => {
    const db = await readDb();
    if (k.startsWith("mile:")) {
      const [, claimId, atRaw] = k.split(":");
      const at = String(Number(atRaw) || "");
      if (!claimId || !at) return false;
      const slot = (db.milestones[claimId] ||= {});
      if (slot[at]) return false;
      slot[at] = { rewardedAt: new Date().toISOString(), chars: n };
      await writeDb(db);
      await audit("referral_milestone_rewarded", { inviterClaimId: claimId }, { at, chars: n });
      return true;
    }
    if (k.startsWith("rebate:")) {
      const rid = k.slice("rebate:".length);
      const rec = db.referrals[rid];
      if (!rec || rec.rebateRewardedAt) return false;
      rec.rebateRewardedAt = new Date().toISOString();
      rec.rebateChars = n;
      rec.rebateOrderId = note.replace(/^referral-rebate-/, "") || undefined;
      await writeDb(db);
      await audit("referral_rebate_rewarded", rec, { chars: n });
      return true;
    }
    return false;
  });
}

export interface InviteStats {
  registered: number;
  qualified: number;
  invitee_rewarded: number;
  inviter_rewarded: number;
  flagged: number;
  chars_earned: number;
  cap_total: number;
  cap_left: number;
  /** 里程碑进度（会员页阶梯条）：各档达标人数要求 / 奖励 / 是否已发。 */
  milestones: Array<{ at: number; chars: number; rewarded: boolean }>;
  /** 下一个里程碑还差几人（全部达成 = null）。 */
  next_milestone_need: number | null;
  /** 里程碑 + 首充返利累计到手字符（chars_earned 之外的加码部分）。 */
  bonus_chars_earned: number;
}

/** 某邀请人的进度统计（会员页邀请卡）。 */
export async function inviteStats(claimId: string): Promise<InviteStats> {
  const db = await readDb();
  const mine = Object.values(db.referrals).filter(
    (r) => r.inviterClaimId === claimId && r.status !== "rejected"
  );
  const rewarded = mine.filter((r) => !!r.inviterRewardedAt);
  const qualified = mine.filter((r) => r.status === "qualified").length;
  const done = db.milestones[claimId] || {};
  const milestones = REFERRAL_MILESTONES.map((m) => ({
    at: m.at,
    chars: m.chars,
    rewarded: !!done[String(m.at)],
  }));
  const nextM = REFERRAL_MILESTONES.find((m) => qualified < m.at);
  const mileChars = Object.values(done).reduce((s, v) => s + (v.chars || 0), 0);
  const rebateChars = mine.reduce((s, r) => s + (r.rebateChars || 0), 0);
  return {
    registered: mine.length,
    qualified,
    invitee_rewarded: mine.filter((r) => !!r.inviteeRewardedAt).length,
    inviter_rewarded: rewarded.length,
    flagged: mine.filter((r) => r.status === "flagged").length,
    chars_earned: rewarded.reduce((s, r) => s + (r.inviterChars || 0), 0),
    cap_total: REFERRAL_TOTAL_CAP,
    cap_left: Math.max(0, REFERRAL_TOTAL_CAP - mine.length),
    milestones,
    next_milestone_need: nextM ? nextM.at - qualified : null,
    bonus_chars_earned: mileChars + rebateChars,
  };
}

/** 全局聚合（客服控制台 / 厂商 ops 卡；纯计数，零 PII）。 */
export async function referralAggregate(): Promise<{
  codes: number;
  registered: number;
  qualified: number;
  flagged: number;
  invitee_rewarded: number;
  inviter_rewarded: number;
  chars_granted: number;
}> {
  const db = await readDb();
  const all = Object.values(db.referrals);
  return {
    codes: Object.keys(db.codeByClaim).length,
    registered: all.filter((r) => r.status !== "rejected").length,
    qualified: all.filter((r) => r.status === "qualified").length,
    flagged: all.filter((r) => r.status === "flagged").length,
    invitee_rewarded: all.filter((r) => !!r.inviteeRewardedAt).length,
    inviter_rewarded: all.filter((r) => !!r.inviterRewardedAt).length,
    chars_granted: all.reduce(
      (s, r) => s + (r.inviteeChars || 0) + (r.inviterChars || 0),
      0
    ),
  };
}
