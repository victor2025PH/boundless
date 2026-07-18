// /console 登出：撤销 sessions 表里的当前会话（写 audit session.revoke）并清 cookie。
import { NextRequest, NextResponse } from "next/server";
import { CONSOLE_COOKIE } from "@/lib/console-auth";
import { revokeSession, verifySession } from "@/lib/console-users";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const token = req.cookies.get(CONSOLE_COOKIE)?.value;
  if (token) {
    try {
      const v = verifySession(token);
      revokeSession(token, undefined, v ? `console:${v.user.username}` : "console");
    } catch {
      // 账本不可用时也照样清 cookie
    }
  }
  const res = NextResponse.json({ ok: true });
  res.cookies.set(CONSOLE_COOKIE, "", {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  return res;
}
