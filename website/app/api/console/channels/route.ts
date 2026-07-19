// /api/console/channels —— 渠道账号台账（schema v5：channel_accounts）。
// GET ?platform=&status=&q=&limit=&offset=：登记清单 + 平台/状态统计；viewer+。
// POST { platform, label, handle?, instance?, purpose?, holder?, status?, session_ref?, notes? }：
//   登记新账号（admin+，audit actor="console:<username>"）。
// PATCH { id, action?: "set_status", status } 软状态变更；
//       { id, ...资料字段 } 更新登记信息（含 status 时一并软变更）。admin+。
// session_ref 只收登录态位置的纯文本备注，任何密钥/凭据本体不进台账。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireConsole } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import {
  CHANNEL_PLATFORMS,
  CHANNEL_STATUSES,
  createChannelAccount,
  getChannelAccountStats,
  isChannelPlatform,
  isChannelStatus,
  listChannelAccounts,
  setChannelAccountStatus,
  updateChannelAccount,
} from "@/lib/channels";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const platform = sp.get("platform") ?? undefined;
    if (platform && !isChannelPlatform(platform)) {
      return NextResponse.json(
        { error: `unknown platform: ${platform} (expect ${CHANNEL_PLATFORMS.join("|")})` },
        { status: 400 }
      );
    }
    const status = sp.get("status") ?? undefined;
    if (status && !isChannelStatus(status)) {
      return NextResponse.json(
        { error: `unknown status: ${status} (expect ${CHANNEL_STATUSES.join("|")})` },
        { status: 400 }
      );
    }
    const result = listChannelAccounts({
      platform,
      status,
      q: sp.get("q") ?? undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
      offset: sp.get("offset") ? Number(sp.get("offset")) : undefined,
    });
    return NextResponse.json({ ok: true, ...result, stats: getChannelAccountStats() });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const user = getConsoleUser(req);
  if (!user) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!roleAtLeast(user.role, "admin")) {
    return NextResponse.json({ error: "forbidden: admin role required" }, { status: 403 });
  }
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const platform = String(body.platform ?? "").trim();
  const label = String(body.label ?? "").trim();
  if (!platform || !label) {
    return NextResponse.json({ error: "platform, label required" }, { status: 400 });
  }
  try {
    const account = createChannelAccount(
      {
        platform,
        label,
        handle: body.handle != null ? String(body.handle) : null,
        instance: body.instance != null ? String(body.instance) : null,
        purpose: body.purpose != null ? String(body.purpose) : null,
        holder: body.holder != null ? String(body.holder) : null,
        status: body.status != null ? String(body.status) : null,
        session_ref: body.session_ref != null ? String(body.session_ref) : null,
        notes: body.notes != null ? String(body.notes) : null,
      },
      undefined,
      `console:${user.username}`
    );
    return NextResponse.json({ ok: true, account });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (e instanceof TypeError) {
      return NextResponse.json({ error: msg }, { status: 400 });
    }
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}

export async function PATCH(req: NextRequest) {
  const user = getConsoleUser(req);
  if (!user) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!roleAtLeast(user.role, "admin")) {
    return NextResponse.json({ error: "forbidden: admin role required" }, { status: 403 });
  }
  const actor = `console:${user.username}`;
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const id = String(body.id ?? "").trim();
  if (!id) {
    return NextResponse.json({ error: "id required" }, { status: 400 });
  }
  try {
    // 纯状态变更：{ id, action: "set_status", status }（列表行内快捷操作走这里）
    if (body.action === "set_status") {
      const status = String(body.status ?? "").trim();
      if (!status) {
        return NextResponse.json({ error: "status required" }, { status: 400 });
      }
      const result = setChannelAccountStatus(id, status, undefined, actor);
      if (!result.ok) {
        const code = result.error?.includes("not found") ? 404 : 400;
        return NextResponse.json({ error: result.error }, { status: code });
      }
      return NextResponse.json({ ok: true, account: result.row, unchanged: result.unchanged ?? false });
    }
    // 资料更新：{ id, ...字段 }；带 status 时一并软变更（编辑表单一次提交）
    let account = updateChannelAccount(
      id,
      {
        ...(body.platform !== undefined ? { platform: String(body.platform) } : {}),
        ...(body.label !== undefined ? { label: String(body.label) } : {}),
        ...(body.handle !== undefined ? { handle: body.handle == null ? null : String(body.handle) } : {}),
        ...(body.instance !== undefined ? { instance: String(body.instance) } : {}),
        ...(body.purpose !== undefined ? { purpose: String(body.purpose) } : {}),
        ...(body.holder !== undefined ? { holder: body.holder == null ? null : String(body.holder) } : {}),
        ...(body.session_ref !== undefined
          ? { session_ref: body.session_ref == null ? null : String(body.session_ref) }
          : {}),
        ...(body.notes !== undefined ? { notes: body.notes == null ? null : String(body.notes) } : {}),
      },
      undefined,
      actor
    );
    if (!account) {
      return NextResponse.json({ error: "channel account not found" }, { status: 404 });
    }
    if (body.status !== undefined) {
      const result = setChannelAccountStatus(id, String(body.status ?? "").trim(), undefined, actor);
      if (!result.ok) {
        return NextResponse.json({ error: result.error }, { status: 400 });
      }
      account = result.row ?? account;
    }
    return NextResponse.json({ ok: true, account });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (e instanceof TypeError) {
      return NextResponse.json({ error: msg }, { status: 400 });
    }
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
