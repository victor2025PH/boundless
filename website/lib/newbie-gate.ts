// 新人 6U 大礼包下单预检闸（2026-08-21 实施50 P1）。
//
// 双闸设计：本闸在**下单时**拦明确不合格的单（少收一笔注定要人工退的钱），
// 厂商机履约端（engines/chengjie/src/licensing/chatx_fulfillment.py::newbie_eligibility）
// 在**签发时**做同口径终审——本闸挂了/漏了都不会多发凭证，只影响体验不影响资金安全。
//
// 口径（与引擎端逐条对齐，改一处必须同批改另一处）：
//  · 每账号一次：同人（contactCore / 指纹）已有 paid/activated 的新人包单 → 拒；
//  · 72h 窗：注册锚 = 该人**最早**的试用 claim.createdAt；超窗 → 拒；
//  · 查无 claim（先付费后装机的全新客）→ 放行（首触即注册语义）；
//  · 台账读取异常 → 放行（fail-open：预检闸绝不阻断结账主链）。
import { listOrders } from "./order-store";
import { getClaimByFingerprint, listClaims, normalizeFingerprint } from "./trial-claim-store";
import { NEWBIE_PACK } from "./chatx-pricing";

/** 联系方式 → 可比对核心标识。移植自引擎 topup_voucher.contact_core 语义：
 *  TG handle 优先 → 邮箱 → 保守回退 casefold+去空白。两端判定必须同口径，
 *  否则「下单闸放行、履约端转人工」会白白多出人工件。 */
export function contactCore(s: unknown): string {
  const t = String(s ?? "").trim().toLowerCase();
  if (!t) return "";
  const email = t.match(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/);
  if (email) return email[0];
  const tg = t.match(/(?:t\.me\/|@)([a-z0-9_]{4,32})/);
  if (tg) return `@${tg[1]}`;
  return t.replace(/\s+/g, "");
}

function normFp(s: unknown): string {
  return String(s ?? "").trim().toUpperCase().replace(/[-\s]/g, "");
}

export type NewbieGateVerdict =
  | { ok: true }
  | { ok: false; error: "newbie_already_claimed" | "newbie_window_passed" };

export async function newbieOrderGate(input: {
  contact: string;
  fingerprint?: string;
}): Promise<NewbieGateVerdict> {
  const cc = contactCore(input.contact);
  const fp = normFp(input.fingerprint);
  try {
    // ① 每账号一次：扫已付/已开通的新人包单（pending 不算——弃单重下是合法路径，
    //    双单都真付钱的极端件由履约端终审兜住）。
    const orders = await listOrders();
    const prior = orders.some((o) => {
      if (o.sku_id !== NEWBIE_PACK.skuId && o.plan !== NEWBIE_PACK.key) return false;
      if (o.status !== "paid" && o.status !== "activated") return false;
      const oc = contactCore(o.contact);
      const of = normFp(o.fingerprint);
      return (cc && oc && oc === cc) || (fp && of && of === fp);
    });
    if (prior) return { ok: false, error: "newbie_already_claimed" };

    // ② 72h 窗：最早 claim 当注册锚（换机重装不刷新窗口）。
    let earliest = Infinity;
    if (fp) {
      const byFp = await getClaimByFingerprint(normalizeFingerprint(input.fingerprint));
      if (byFp) earliest = Math.min(earliest, Date.parse(byFp.createdAt) || Infinity);
    }
    if (cc) {
      const claims = await listClaims({ limit: 5000 });
      for (const c of claims) {
        if (contactCore(c.contact) === cc) {
          earliest = Math.min(earliest, Date.parse(c.createdAt) || Infinity);
        }
      }
    }
    if (earliest !== Infinity && Date.now() > earliest + NEWBIE_PACK.windowHours * 3600e3) {
      return { ok: false, error: "newbie_window_passed" };
    }
    return { ok: true };
  } catch {
    return { ok: true }; // fail-open：预检闸的任何故障都不该挡住付款
  }
}
