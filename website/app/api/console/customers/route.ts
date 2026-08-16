// /api/console/customers —— 客户列表（GET ?q=&test=1）与创建（POST）。
// ?test=1 → includeTest：把测试/演练数据（is_test=1）一并带出，默认排除。
// POST body: { display_name, primary_contact?, notes?, identity?: { kind, value } }
// identity 可选：创建后立即挂一条身份标识（如 contact），便于后续订单/留资自动归属。
// RBAC：GET viewer+；POST admin+（audit actor="console:<username>"）。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireConsole } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import {
  IDENTITY_KINDS,
  attachIdentity,
  createCustomer,
  getLedgerDb,
  listCustomers,
  type AttachIdentityResult,
  type IdentityKind,
} from "@/lib/ledger";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const result = listCustomers({
      q: sp.get("q") ?? undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
      offset: sp.get("offset") ? Number(sp.get("offset")) : undefined,
      includeTest: sp.get("test") === "1",
    });
    return NextResponse.json({ ok: true, ...result });
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
  const actor = `console:${user.username}`;
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const displayName = String(body.display_name ?? "").trim();
  if (!displayName) {
    return NextResponse.json({ error: "display_name required" }, { status: 400 });
  }
  try {
    const db = getLedgerDb();
    const customer = createCustomer(
      {
        display_name: displayName,
        primary_contact: body.primary_contact != null ? String(body.primary_contact) : null,
        notes: body.notes != null ? String(body.notes) : null,
        source: "console",
      },
      db,
      actor
    );
    // 可选：同时挂初始身份标识（失败不回滚客户创建，把结果如实带回）
    let identity: (AttachIdentityResult & { kind: string; value: string }) | null = null;
    const ident = body.identity as { kind?: unknown; value?: unknown } | undefined;
    if (ident && ident.kind != null && String(ident.value ?? "").trim()) {
      const kind = String(ident.kind) as IdentityKind;
      if (!IDENTITY_KINDS.includes(kind)) {
        return NextResponse.json({ ok: true, customer, identity: null, identityError: `bad kind: ${kind}` });
      }
      identity = { ...attachIdentity(customer.id, kind, String(ident.value), db, actor), kind, value: String(ident.value) };
    }
    return NextResponse.json({ ok: true, customer, identity });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
