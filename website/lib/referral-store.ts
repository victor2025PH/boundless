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
export const REFERRAL_TOTAL_CAP = Number(process.env.REFERRAL_TOTAL_CAP || 20);
/** 同一邀请人名下、同一出口 IP 的被邀请人达到此数即标 flagged 人审。 */
const IP_CLUSTER_FLAG_AT = Number(process.env.REFERRAL_IP_CLUSTER_FLAG_AT || 3);

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
      };
    }
  } catch {
    /* fresh */
  }
  return { version: 1, codeByClaim: {}, claimByCode: {}, referrals: {}, byInvitee: {} };
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

export interface InviteStats {
  registered: number;
  qualified: number;
  invitee_rewarded: number;
  inviter_rewarded: number;
  flagged: number;
  chars_earned: number;
  cap_total: number;
  cap_left: number;
}

/** 某邀请人的进度统计（会员页邀请卡）。 */
export async function inviteStats(claimId: string): Promise<InviteStats> {
  const db = await readDb();
  const mine = Object.values(db.referrals).filter(
    (r) => r.inviterClaimId === claimId && r.status !== "rejected"
  );
  const rewarded = mine.filter((r) => !!r.inviterRewardedAt);
  return {
    registered: mine.length,
    qualified: mine.filter((r) => r.status === "qualified").length,
    invitee_rewarded: mine.filter((r) => !!r.inviteeRewardedAt).length,
    inviter_rewarded: rewarded.length,
    flagged: mine.filter((r) => r.status === "flagged").length,
    chars_earned: rewarded.reduce((s, r) => s + (r.inviterChars || 0), 0),
    cap_total: REFERRAL_TOTAL_CAP,
    cap_left: Math.max(0, REFERRAL_TOTAL_CAP - mine.length),
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
