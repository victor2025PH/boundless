/**
 * 试用→付费转化聚合（P1-4，2026-08-18）——「试用→付费 ≥30%」目标的读数面。
 *
 * 既有激活漏斗（lib/activation-funnel.ts，P1-⑧）止步 first_reply；本模块补
 * 商业段：**窗口内领试用的机器，有多少最终付了钱**（cohort 口径：claim 在
 * 窗口内，转化看「截至现在」——转化天然滞后于领取，按窗口截转化会系统性低估）。
 *
 * 付费真相=集团库 orders.paid_at 非空（真金白银口径）。**刻意不把 licenses 表
 * 当付费证据**：licenses 收录一切签发（含试用/测试签发），拿它当付费会虚高——
 * 它只经 identities 身份归并参与「找到这台机器属于哪个客户」的 join。
 *
 * 三级 join（每条 claim 只计一次，优先级从确到疏）：
 *   fingerprint  claim.fingerprint 直接出现在已付订单 orders.fingerprint
 *   contact      claim.contact 归一后命中已付订单 contact（小写/去 @/trim）
 *   identity     claim 的 fp/contact 经 identities 表归并到 customer，
 *                该 customer 名下存在已付订单
 *
 * 只读、纯聚合；集团库缺席/查询失败 → ledger_present=false + paid=0（页面
 * 显式空态，绝不装 0 当真读数）。冒烟：npx tsx lib/trial-paid-funnel.test.ts。
 */
import { listClaims } from "./trial-claim-store";

export interface TrialPaidWindow {
  days: number;
  claimed: number;
  paid: number;
  /** paid/claimed 一位小数百分比；claimed=0 → null */
  rate: number | null;
  matched: { fingerprint: number; contact: number; identity: number };
  ledger_present: boolean;
}

function normContact(v: string): string {
  return String(v || "").trim().toLowerCase().replace(/^@+/, "");
}

function rate1(part: number, base: number): number | null {
  if (!base) return null;
  return Math.round((part / base) * 1000) / 10;
}

interface PaidUniverse {
  fps: Set<string>;
  contacts: Set<string>;
  paidCustomers: Set<string>;
  identFpToCustomer: Map<string, string>;
  identContactToCustomer: Map<string, string>;
}

function loadPaidUniverse(): PaidUniverse | null {
  try {
    // 惰性 require：集团库/better-sqlite3 缺席时整模块仍可用（ledger_present=false）
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { getLedgerDb } = require("./ledger") as typeof import("./ledger");
    const db = getLedgerDb();
    const fps = new Set<string>();
    const contacts = new Set<string>();
    const paidCustomers = new Set<string>();
    for (const r of db.prepare(
      "SELECT fingerprint, contact, customer_id FROM orders " +
      "WHERE paid_at IS NOT NULL AND paid_at != ''").iterate() as Iterable<{
        fingerprint?: string; contact?: string; customer_id?: string }>) {
      const fp = String(r.fingerprint || "").trim().toUpperCase();
      if (fp) fps.add(fp);
      const c = normContact(String(r.contact || ""));
      if (c) contacts.add(c);
      const cid = String(r.customer_id || "").trim();
      if (cid) paidCustomers.add(cid);
    }
    const identFpToCustomer = new Map<string, string>();
    const identContactToCustomer = new Map<string, string>();
    for (const r of db.prepare(
      "SELECT kind, value, customer_id FROM identities").iterate() as Iterable<{
        kind: string; value: string; customer_id: string }>) {
      const cid = String(r.customer_id || "").trim();
      if (!cid) continue;
      if (r.kind === "fingerprint") {
        identFpToCustomer.set(String(r.value || "").trim().toUpperCase(), cid);
      } else if (["contact", "tg", "email", "phone"].includes(String(r.kind))) {
        identContactToCustomer.set(normContact(String(r.value || "")), cid);
      }
    }
    return { fps, contacts, paidCustomers, identFpToCustomer,
             identContactToCustomer };
  } catch {
    return null;
  }
}

export async function trialPaidFunnel(days: number): Promise<TrialPaidWindow> {
  const sinceMs = Date.now() - days * 86400_000;
  let claims: Array<{ fingerprint: string; contact: string;
                      createdAt: string }> = [];
  try {
    const all = await listClaims({ limit: 2000 });
    claims = all.filter((c) => {
      const t = Date.parse(c.createdAt);
      return Number.isFinite(t) && t >= sinceMs;
    });
  } catch {
    claims = [];
  }

  const uni = loadPaidUniverse();
  const matched = { fingerprint: 0, contact: 0, identity: 0 };
  let paid = 0;
  if (uni) {
    for (const c of claims) {
      const fp = String(c.fingerprint || "").trim().toUpperCase();
      const ct = normContact(String(c.contact || ""));
      if (fp && uni.fps.has(fp)) {
        matched.fingerprint += 1; paid += 1; continue;
      }
      if (ct && uni.contacts.has(ct)) {
        matched.contact += 1; paid += 1; continue;
      }
      const cid = (fp && uni.identFpToCustomer.get(fp))
        || (ct && uni.identContactToCustomer.get(ct)) || "";
      if (cid && uni.paidCustomers.has(cid)) {
        matched.identity += 1; paid += 1;
      }
    }
  }
  return {
    days,
    claimed: claims.length,
    paid,
    rate: rate1(paid, claims.length),
    matched,
    ledger_present: uni !== null,
  };
}
