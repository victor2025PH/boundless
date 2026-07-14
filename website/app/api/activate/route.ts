import { NextRequest, NextResponse } from "next/server";
import { getOrder } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 客户端「在线激活」端点：POST { code, fingerprint } → 返回该订单已签发的授权。
//
// 安全模型（关键）：本端点【绝不签发】授权，只把厂商机（Ed25519 私钥留在本地）
// 早已通过 fulfill_orders.py 签好、回填在 order.code 里的授权原样取回。私钥永不上服务器，
// 服务器被攻破也伪造不出授权；且授权本身绑机器指纹，客户端还会用内置公钥复验。
//
// code = 订单号（AH-YYYYMMDD-XXXX，短，即客户下单回执/TG 通知里的单号），
// 取代「粘贴一长串 base64」的旧动线。fingerprint 与下单时登记的指纹须一致（防串号取授权）。

function clean(v: unknown, max: number) {
  return String(v ?? "").trim().slice(0, max);
}

// order.code 是「整份签名授权 JSON 的 base64」。解回对象给客户端（其 activate_from_text 直接吃对象/字符串皆可）。
function decodeLicense(code: string): unknown | null {
  try {
    return JSON.parse(Buffer.from(code, "base64").toString("utf-8"));
  } catch {
    return null;
  }
}

export async function POST(req: NextRequest) {
  let data: Record<string, unknown> = {};
  try {
    data = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }
  const code = clean(data?.code, 60).toUpperCase();
  const fingerprint = clean(data?.fingerprint, 128);

  if (!/^AH-\d{8}-[A-Z0-9]{4,10}$/i.test(code)) {
    return NextResponse.json(
      { ok: false, error: "兑换码格式不对，请填订单号（形如 AH-20260713-ABCD）。" },
      { status: 400 },
    );
  }

  const o = await getOrder(code);
  if (!o) {
    return NextResponse.json({ ok: false, error: "未找到该订单号，请核对后重试。" }, { status: 404 });
  }
  if (o.status !== "activated" || !o.code) {
    const hint =
      o.status === "paid"
        ? "订单已到账，正在自动开通（通常几分钟）。请稍后再试。"
        : o.status === "pending"
          ? "订单尚未完成付款，付款到账后即可自助激活。"
          : "订单尚未开通，请联系客服。";
    return NextResponse.json({ ok: false, error: hint }, { status: 409 });
  }
  // 指纹绑定校验：下单登记过指纹的，激活机器须一致（授权本就绑该指纹，换机激活也会本地失败，双保险）。
  if (o.fingerprint && fingerprint && o.fingerprint !== fingerprint) {
    return NextResponse.json(
      { ok: false, error: "该订单绑定的机器与当前设备不一致，请在下单登记的机器上激活，或联系客服换绑。" },
      { status: 403 },
    );
  }
  const license = decodeLicense(o.code);
  if (!license) {
    return NextResponse.json(
      { ok: false, error: "授权数据异常，请联系客服。" },
      { status: 500 },
    );
  }
  return NextResponse.json({ ok: true, license });
}
