// 集团控制台（/console）鉴权 —— P2 实名账号 + RBAC（viewer < admin < master）。
//
// ⚠️ 集团后台是皇冠资产（客户/订单/授权全量数据），最小暴露原则依旧：
//   - 页面登录只认 users 表账号（用户名+密码，lib/console-users.ts scrypt 散列）；
//     登录成功发 32 字节随机 session token，cookie `console_session`（httpOnly、12h）
//     存原始 token，库里只存 sha256(token)（sessions 表，见 schema v2）。
//   - 保留 `x-console-key` 头通道（值 === CONSOLE_KEY 时放行，视为内置 master），
//     供脚本/巡检免账号调用；users 表建立后页面口令登录彻底关闭，此头是口令仅存的用途。
//   - 引导：users 表为空时 login 接口开放 bootstrap（CONSOLE_KEY + 用户名 + 密码建首个
//     master 并直接登录）；建成后 bootstrap 通道自动关闭。
//   - 生产仍建议反代层配 IP 白名单；CONSOLE_KEY 不要与 ADMIN_KEY 共用。

import { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { countUsers, roleAtLeast, verifySession, type ConsoleRole } from "./console-users";

export const CONSOLE_COOKIE = "console_session";

/** 控制台 key 集合：CONSOLE_KEY 优先；未配置时回退 ADMIN_KEY → TELEGRAM_SETUP_KEY。
 *  现仅用于 x-console-key 头通道与 bootstrap 引导，页面登录不再收口令。 */
export function consoleKeys(): string[] {
  const key =
    process.env.CONSOLE_KEY || process.env.ADMIN_KEY || process.env.TELEGRAM_SETUP_KEY;
  return key ? [key] : [];
}

/** 控制台 key 是否已配置（bootstrap 引导与脚本头通道依赖它）。 */
export function consoleConfigured(): boolean {
  return consoleKeys().length > 0;
}

/** users 表是否还没有任何账号（登录卡据此切换「初始化主账号」表单）。
 *  账本不可用时按“空”处理——引导表单本身还有 CONSOLE_KEY 把关。 */
export function consoleUsersEmpty(): boolean {
  try {
    return countUsers() === 0;
  } catch {
    return true;
  }
}

// ── 请求主体识别 ────────────────────────────────────────────────────
export interface ConsoleAuthUser {
  /** key 头通道无对应账号，为 null。 */
  userId: string | null;
  username: string;
  role: ConsoleRole;
  via: "session" | "key";
}

/** API 请求 → 当前控制台主体：
 *  1) `x-console-key` 头 === CONSOLE_KEY → 内置 master（脚本/巡检通道）；
 *  2) cookie console_session 的 session token 通过 verifySession（未撤销/未过期/账号未禁用）。
 *  两者都不中返回 null。 */
export function getConsoleUser(req: NextRequest): ConsoleAuthUser | null {
  const headerKey = req.headers.get("x-console-key");
  if (headerKey) {
    const keys = consoleKeys();
    if (keys.length && keys.includes(headerKey)) {
      return { userId: null, username: "console-key", role: "master", via: "key" };
    }
  }
  const token = req.cookies.get(CONSOLE_COOKIE)?.value;
  if (token) {
    try {
      const v = verifySession(token);
      if (v) return { userId: v.user.id, username: v.user.username, role: v.user.role, via: "session" };
    } catch {
      // 账本不可用 → 视为未登录（fail closed）
    }
  }
  return null;
}

/** API 路由守卫：所有 /api/console/** 处理函数第一行调用（任意角色，GET 类够用）。 */
export function requireConsole(req: NextRequest): boolean {
  return getConsoleUser(req) !== null;
}

/** 角色守卫：viewer < admin < master。写操作用 requireRole(req, "admin")。 */
export function requireRole(req: NextRequest, minRole: ConsoleRole): boolean {
  const u = getConsoleUser(req);
  return !!u && roleAtLeast(u.role, minRole);
}

// ── 服务端组件（layout/page）────────────────────────────────────────
/** 读 cookies() 里的 session token → 当前登录用户；未登录/失效返回 null。 */
export function getConsoleSessionUser(): ConsoleAuthUser | null {
  const token = cookies().get(CONSOLE_COOKIE)?.value;
  if (!token) return null;
  try {
    const v = verifySession(token);
    if (!v) return null;
    return { userId: v.user.id, username: v.user.username, role: v.user.role, via: "session" };
  } catch {
    return null;
  }
}

/** 服务端组件会话判定（兼容旧签名）：sessions 表校验通过才算已登录。 */
export function hasConsoleSession(): boolean {
  return getConsoleSessionUser() !== null;
}
