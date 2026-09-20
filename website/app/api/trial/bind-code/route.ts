import { NextRequest, NextResponse } from "next/server";
import { getClaim, issueBindCode } from "@/lib/trial-claim-store";
import { TELEGRAM_HANDLE, CONTACT_URL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/trial/bind-code —— 取「加客服领额度」的一次性绑定码 + 深链。
 *
 * body: `{ claim_id }` → `{ ok, bind_code, telegram_url, whatsapp_url, redeemed }`
 *
 * 为什么要绑定码：客服要知道「面前这个人对应哪台机器」，才能把额度发到对的地方。
 * 让用户手抄机器指纹既难念又易错，还等于把内部标识满世界发；一次性短码
 * （BC-XXXX-XXXX，字母表剔除 0/O/1/I/L）念得出、打得对、且只对应一条 claim。
 *
 * 深链把码**预填进对话框**：用户点开就是一句写好的话，按发送即可——「一次点击」
 * 是这条增长链能不能跑通的关键，让用户手动复制粘贴会掉一大截转化。
 *
 * 幂等：同一 claim 永远同一个码（store 层保证）。重复调用只是把它再取一次。
 */

const WA_NUMBER = (process.env.NEXT_PUBLIC_WHATSAPP_NUMBER || "").replace(/[^0-9]/g, "");

function prefillText(code: string): string {
  // 客服侧一眼能认：带产品名 + 码本身。刻意不带机器指纹（内部标识不外发）。
  return `ChatX 领取字符额度：${code}`;
}

export async function POST(req: NextRequest) {
  try {
    const data = await req.json().catch(() => ({}));
    const claimId = String(data?.claim_id || "").trim();
    if (!claimId) {
      return NextResponse.json({ ok: false, error: "claim_id_required" }, { status: 400 });
    }
    const exists = await getClaim(claimId);
    if (!exists) {
      return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
    }
    const claim = (await issueBindCode(claimId)) || exists;
    const code = claim.bindCode || "";
    const text = encodeURIComponent(prefillText(code));
    return NextResponse.json({
      ok: true,
      bind_code: code,
      redeemed: !!claim.bindRedeemedAt,
      telegram_url: `${CONTACT_URL}?text=${text}`,
      telegram_handle: `@${TELEGRAM_HANDLE}`,
      // 未配 WhatsApp 客服号时返回空串：客户端据此隐藏该按钮，
      // 而不是给一个点了打不开的死链。
      whatsapp_url: WA_NUMBER ? `https://wa.me/${WA_NUMBER}?text=${text}` : "",
    });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
