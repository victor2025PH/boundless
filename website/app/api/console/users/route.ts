// /api/console/users —— 控制台账号管理，全部动作仅 master。
// GET：用户列表（不含散列字段）。
// POST：建用户 { username, password, role, display_name? }。
// PATCH：{ id, action: "set_role" | "set_enabled" | "reset_password", role? | enabled? | password? }。
// 安全底线：不能禁用/降级最后一个 enabled master（先查再改）；
// 禁用与重置密码会立即撤销目标用户全部会话（lib/console-users.ts 内保证）。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser } from "@/lib/console-auth";
import {
  countEnabledMasters,
  createUser,
  getUserById,
  isConsoleRole,
  listUsers,
  roleAtLeast,
  setEnabled,
  setPassword,
  setRole,
  toPublicUser,
} from "@/lib/console-users";
import { getLedgerDb } from "@/lib/ledger";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function guardMaster(req: NextRequest): { actor: string } | NextResponse {
  const user = getConsoleUser(req);
  if (!user) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!roleAtLeast(user.role, "master")) {
    return NextResponse.json({ error: "forbidden: master role required" }, { status: 403 });
  }
  return { actor: `console:${user.username}` };
}

export async function GET(req: NextRequest) {
  const guard = guardMaster(req);
  if (guard instanceof NextResponse) return guard;
  try {
    return NextResponse.json({ ok: true, users: listUsers() });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const guard = guardMaster(req);
  if (guard instanceof NextResponse) return guard;
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const role = String(body.role ?? "viewer");
  if (!isConsoleRole(role)) {
    return NextResponse.json({ error: "role must be one of master/admin/viewer" }, { status: 400 });
  }
  try {
    const user = createUser(
      {
        username: String(body.username ?? ""),
        password: String(body.password ?? ""),
        role,
        display_name: body.display_name != null ? String(body.display_name) : null,
      },
      getLedgerDb(),
      guard.actor
    );
    return NextResponse.json({ ok: true, user });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    const status = msg.includes("already exists") ? 409 : 400;
    return NextResponse.json({ error: msg }, { status });
  }
}

export async function PATCH(req: NextRequest) {
  const guard = guardMaster(req);
  if (guard instanceof NextResponse) return guard;
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const id = String(body.id ?? "").trim();
  const action = String(body.action ?? "");
  if (!id) {
    return NextResponse.json({ error: "id required" }, { status: 400 });
  }
  try {
    const db = getLedgerDb();
    const target = getUserById(id, db);
    if (!target) {
      return NextResponse.json({ error: "user not found" }, { status: 404 });
    }
    const isLastEnabledMaster =
      target.role === "master" && !!target.enabled && countEnabledMasters(target.id, db) === 0;

    if (action === "set_role") {
      const role = String(body.role ?? "");
      if (!isConsoleRole(role)) {
        return NextResponse.json({ error: "role must be one of master/admin/viewer" }, { status: 400 });
      }
      if (role !== "master" && isLastEnabledMaster) {
        return NextResponse.json({ error: "cannot demote the last enabled master" }, { status: 409 });
      }
      setRole(target.id, role, db, guard.actor);
    } else if (action === "set_enabled") {
      const enabled = body.enabled === true || body.enabled === 1 || body.enabled === "1";
      if (!enabled && isLastEnabledMaster) {
        return NextResponse.json({ error: "cannot disable the last enabled master" }, { status: 409 });
      }
      setEnabled(target.id, enabled, db, guard.actor);
    } else if (action === "reset_password") {
      setPassword(target.id, String(body.password ?? ""), db, guard.actor);
    } else {
      return NextResponse.json({ error: `unknown action: ${action}` }, { status: 400 });
    }
    const updated = getUserById(target.id, db);
    return NextResponse.json({ ok: true, user: updated ? toPublicUser(updated) : null });
  } catch (e) {
    return NextResponse.json({ error: e instanceof Error ? e.message : String(e) }, { status: 400 });
  }
}
