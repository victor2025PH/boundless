// /console 登录：实名账号（username + password）→ session token → httpOnly cookie（12h）。
// 引导通道：users 表为空时接受 { bootstrap: true, key: CONSOLE_KEY, username, password }
// 创建首个 master 并直接登录；users 非空后 bootstrap 与口令登录一律关闭
//（CONSOLE_KEY 仅剩 x-console-key 头通道，供脚本/巡检）。
// 防爆破沿用单 IP 滑动窗口（10 分钟内最多 8 次失败）。
// ⚠️ 皇冠资产入口：生产必须独立设置 CONSOLE_KEY 并在反代层配 IP 白名单。
import { NextRequest, NextResponse } from "next/server";
import { CONSOLE_COOKIE, consoleKeys } from "@/lib/console-auth";
import {
  SESSION_TTL_MS,
  countUsers,
  createSession,
  createUser,
  touchLogin,
  verifyPassword,
  type ConsoleUserPublic,
} from "@/lib/console-users";
import { getLedgerDb, writeAudit } from "@/lib/ledger";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 暴力破解防护：单 IP 滑动窗口(10 分钟内最多 8 次失败)。单实例内存态即可。
const WINDOW_MS = 10 * 60 * 1000;
const MAX_FAILS = 8;
const fails = new Map<string, number[]>();

function clientIp(req: NextRequest): string {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) return xff.split(",")[0].trim();
  return req.headers.get("x-real-ip") || "unknown";
}

function tooMany(ip: string): boolean {
  const now = Date.now();
  const arr = (fails.get(ip) || []).filter((t) => now - t < WINDOW_MS);
  fails.set(ip, arr);
  return arr.length >= MAX_FAILS;
}

function recordFail(ip: string) {
  const now = Date.now();
  const arr = (fails.get(ip) || []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  fails.set(ip, arr);
  // 轻量清理，防内存无限增长
  if (fails.size > 5000) {
    for (const [k, v] of fails) {
      if (v.every((t) => now - t >= WINDOW_MS)) fails.delete(k);
    }
  }
}

/** 登录成功统一出口：建会话 + 记 last_login + audit user.login + set cookie。 */
function loginOk(user: ConsoleUserPublic, req: NextRequest, ip: string): NextResponse {
  const db = getLedgerDb();
  touchLogin(user.id, db);
  writeAudit(
    {
      actor: `console:${user.username}`,
      action: "user.login",
      entity: "user",
      entity_id: user.id,
      detail: { role: user.role, ip },
    },
    db
  );
  const session = createSession(user.id, { ip, ua: req.headers.get("user-agent") }, db);
  const res = NextResponse.json({
    ok: true,
    user: { id: user.id, username: user.username, role: user.role, display_name: user.display_name },
  });
  res.cookies.set(CONSOLE_COOKIE, session.token, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_TTL_MS / 1000, // 12h，与 sessions.expires_at 一致
  });
  return res;
}

export async function POST(req: NextRequest) {
  const ip = clientIp(req);
  if (tooMany(ip)) {
    return NextResponse.json(
      { error: "too many attempts, try again later" },
      { status: 429, headers: { "Retry-After": "600" } },
    );
  }

  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    // ignore malformed body
  }

  let db;
  try {
    db = getLedgerDb();
  } catch (e) {
    return NextResponse.json({ error: `ledger unavailable: ${String(e)}` }, { status: 500 });
  }
  const usersEmpty = countUsers(db) === 0;

  // ── 引导通道：users 空 + CONSOLE_KEY 正确 → 建首个 master 并登录 ──
  if (body.bootstrap === true) {
    if (!usersEmpty) {
      recordFail(ip);
      return NextResponse.json({ error: "bootstrap closed: users already exist" }, { status: 403 });
    }
    const keys = consoleKeys();
    if (!keys.length) {
      return NextResponse.json({ error: "server not configured" }, { status: 500 });
    }
    const key = String(body.key ?? "");
    if (!key || !keys.includes(key)) {
      recordFail(ip);
      return NextResponse.json({ error: "invalid key" }, { status: 401 });
    }
    try {
      const user = createUser(
        {
          username: String(body.username ?? ""),
          password: String(body.password ?? ""),
          role: "master",
          display_name: body.display_name != null ? String(body.display_name) : null,
        },
        db,
        `console:${String(body.username ?? "").trim().toLowerCase()}`,
        "user.bootstrap"
      );
      return loginOk(user, req, ip);
    } catch (e) {
      return NextResponse.json({ error: e instanceof Error ? e.message : String(e) }, { status: 400 });
    }
  }

  // ── 账号登录（唯一的页面登录通道；旧 {key} 口令登录已关闭）──────
  const username = String(body.username ?? "").trim();
  const password = String(body.password ?? "");
  if (usersEmpty) {
    // 前端登录卡按 usersEmpty 显示引导表单；此处兜底提示
    return NextResponse.json({ error: "no users yet: bootstrap first" }, { status: 409 });
  }
  if (!username || !password) {
    recordFail(ip);
    return NextResponse.json({ error: "invalid credentials" }, { status: 401 });
  }
  const user = verifyPassword(username, password, db);
  if (!user) {
    recordFail(ip);
    return NextResponse.json({ error: "invalid credentials" }, { status: 401 });
  }
  if (!user.enabled) {
    recordFail(ip);
    return NextResponse.json({ error: "account disabled" }, { status: 403 });
  }
  return loginOk(
    {
      id: user.id,
      username: user.username,
      role: user.role,
      display_name: user.display_name,
      enabled: true,
      created_at: user.created_at,
      last_login: user.last_login,
    },
    req,
    ip
  );
}
