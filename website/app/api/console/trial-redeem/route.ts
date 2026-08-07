import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireRole } from "@/lib/console-auth";
import {
  attachIdentity,
  classifyStrongContact,
  createCustomer,
  getLedgerDb,
  isTestSignal,
  linkCustomer,
  normIdentityValue,
} from "@/lib/ledger";
import { claimStats, listClaims, redeemBindCode } from "@/lib/trial-claim-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * 客服核销「加客服送额度」的绑定码。
 *
 *   GET  /api/console/trial-redeem            → 最近的领取/核销台账 + 概览
 *   POST /api/console/trial-redeem {code, chars?}  → 核销一个绑定码
 *
 * 鉴权：console `admin` 角色（与 /api/console/licenses 同口径）。
 *
 * 核销**只标状态与应赠字符数，不签凭证**——真加量凭证由厂商机用同一把私钥签好后经
 * `/api/admin/trial-claims` 回填，客户端再自动兑换。客服手上因此永远没有可直接变现的
 * 凭证，误操作/账号被盗的损失面只到「多标一条待发」。
 *
 * 幂等：重复核销返回 `already_redeemed: true` 而非报错。客服手滑点两次不该变成两笔赠量。
 */

/** 默认赠量：加客服送 10 万字符（运营口径，可用 env 调）。 */
const DEFAULT_GIFT_CHARS = Number(process.env.TRIAL_GIFT_CHARS || 100000);

export async function GET(req: NextRequest) {
  if (!requireRole(req, "admin")) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const limit = Number(req.nextUrl.searchParams.get("limit") || 50);
  const claims = await listClaims({ limit });
  return NextResponse.json({
    ok: true,
    default_chars: DEFAULT_GIFT_CHARS,
    stats: await claimStats(),
    rows: claims
      .slice()
      .reverse()
      .map((c) => ({
        id: c.id,
        contact: c.contact,
        contact_kind: c.contactKind,
        bind_code: c.bindCode || "",
        created_at: c.createdAt,
        status: c.status,
        bind_redeemed_at: c.bindRedeemedAt || "",
        bind_chars: c.bindChars || 0,
        topup_ready: !!c.topupVoucher,
      })),
  });
}

export async function POST(req: NextRequest) {
  if (!requireRole(req, "admin")) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const user = getConsoleUser(req);
  const data = await req.json().catch(() => ({}));
  const code = String(data?.code || "");
  const chars = Number(data?.chars || DEFAULT_GIFT_CHARS);
  const res = await redeemBindCode(code, { chars, by: user ? `console:${user.username}` : "console" });
  if (!res.ok) {
    return NextResponse.json(
      { ok: false, error: res.reason },
      { status: res.reason === "bad_code" ? 400 : 404 },
    );
  }
  // 核销 = 用户真人来找过客服：把强信号联系方式归并进集团客户体系（弱信号/自由
  // 文本不建档，沿用 ensureCustomerForOrder/Lead 的哲学）。best-effort——账本
  // 不可用绝不影响核销主链路；重复核销不重复建档（linkCustomer 先查后建 + 幂等挂身份）。
  if (!res.alreadyRedeemed) {
    try {
      ensureCustomerForTrialContact(res.claim.contact, user ? `console:${user.username}` : "console");
    } catch {
      /* 账本异常不阻断核销 */
    }
  }
  return NextResponse.json({
    ok: true,
    already_redeemed: res.alreadyRedeemed,
    contact: res.claim.contact,
    chars: res.claim.bindChars || chars,
    // 提示客服：凭证还要等厂商机签，不是点完就到账
    pending_voucher: !res.claim.topupVoucher,
  });
}

/** 试用核销的客户归并：强信号（@handle / email / 电话）先匹配已有身份，未命中建档
 *  并挂身份（规范键 + 原文 contact 键，与 ensureCustomerForLead 同款双挂）。
 *  返回 customer_id；弱信号返回 null 不建档。 */
function ensureCustomerForTrialContact(contact: string, actor: string): string | null {
  const strong = classifyStrongContact(contact);
  if (!strong) return null;
  const db = getLedgerDb();
  let cid = linkCustomer(strong.kind, strong.value, db);
  if (!cid) {
    cid = createCustomer(
      {
        display_name: strong.display,
        primary_contact: contact,
        source: "auto:trial-redeem",
        notes: "试用核销自动建档（强信号）",
        is_test: isTestSignal(contact) ? 1 : 0,
      },
      db,
      actor
    ).id;
  }
  attachIdentity(cid, strong.kind, strong.value, db, actor);
  const rawNorm = normIdentityValue("contact", contact);
  if (rawNorm && rawNorm !== strong.value) attachIdentity(cid, "contact", contact, db, actor);
  return cid;
}
