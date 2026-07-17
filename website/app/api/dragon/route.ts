import { NextRequest, NextResponse } from "next/server";
import {
  assistPearl,
  claimGrand,
  collectPearl,
  getDragonState,
  makeWish,
  newVisitorToken,
  packStartToken,
  parseStartToken,
  tokenOf,
  verifyVisitorToken,
  type WishKind,
} from "@/lib/dragon-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * 「七星聚 · 龙行无界」API。
 * GET  → 当前访客状态（无记录不落库）；首访签发 HMAC 签名的 httpOnly 访客 cookie。
 * POST { action: "collect" }            → 收下今日星珠（服务端按 UTC+8 日界幂等）。
 * POST { action: "wish", kind }         → 集齐后祈愿三选一；trial/gift 签发 LOONG- 兑换码。
 *
 * 安全：珠/愿全部服务端记账；vid cookie 带 HMAC 签名，伪造不过验签即视为新访客，
 * 无法冒领他人进度；trial 月度配额在存储层封顶。
 */

const COOKIE = "bl_vid";
/** Chrome 对 cookie 寿命的上限约 400 天 */
const COOKIE_MAX_AGE = 399 * 86400;

/** 仅非生产环境：x-dragon-now 头模拟"现在"，供七日流程自动化回归（生产无此后门） */
function nowOf(req: NextRequest): number {
  if (process.env.NODE_ENV !== "production") {
    const h = Number(req.headers.get("x-dragon-now"));
    if (Number.isFinite(h) && h > 0) return h;
  }
  return Date.now();
}

function withVisitor(req: NextRequest): { vid: string; setCookie: string | null } {
  const existing = verifyVisitorToken(req.cookies.get(COOKIE)?.value);
  if (existing) return { vid: existing, setCookie: null };
  const { vid, token } = newVisitorToken();
  return { vid, setCookie: token };
}

function attach(res: NextResponse, setCookie: string | null) {
  if (setCookie) {
    res.cookies.set(COOKIE, setCookie, {
      httpOnly: true,
      sameSite: "lax",
      secure: process.env.NODE_ENV === "production",
      maxAge: COOKIE_MAX_AGE,
      path: "/",
    });
  }
  return res;
}

export async function GET(req: NextRequest) {
  const { vid, setCookie } = withVisitor(req);
  const state = await getDragonState(vid, nowOf(req));
  /* share=分享助力链接参数（?xz=），tg=TG 深链 /start 载荷——都是本访客自己的签名令牌 */
  return attach(
    NextResponse.json({ ok: true, state, share: tokenOf(vid), tg: packStartToken(vid) }),
    setCookie,
  );
}

export async function POST(req: NextRequest) {
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }
  const { vid, setCookie } = withVisitor(req);
  const now = nowOf(req);
  const action = String(body.action ?? "");

  if (action === "collect") {
    const r = await collectPearl(vid, now);
    return attach(NextResponse.json(r), setCookie);
  }

  if (action === "wish") {
    const kind = String(body.kind ?? "") as WishKind;
    if (!["trial", "skin", "gift"].includes(kind)) {
      return NextResponse.json({ ok: false, error: "bad_kind" }, { status: 400 });
    }
    const r = await makeWish(vid, kind, now);
    return attach(NextResponse.json(r), setCookie);
  }

  /* 三枚月鳞兑「界龙之约」大奖码 */
  if (action === "grand") {
    const r = await claimGrand(vid, now);
    return attach(NextResponse.json(r), setCookie);
  }

  /* 好友助力：来访者带着分享令牌（?xz= 参数，"vid.sig" 或 TG 载荷格式）为分享人点亮今日星珠 */
  if (action === "assist") {
    const raw = String(body.token ?? "");
    const sharer = verifyVisitorToken(raw) ?? parseStartToken(raw);
    if (!sharer) return NextResponse.json({ ok: false, error: "bad_token" }, { status: 400 });
    const r = await assistPearl(sharer, vid, now);
    return attach(NextResponse.json(r), setCookie);
  }

  return NextResponse.json({ ok: false, error: "bad_action" }, { status: 400 });
}
