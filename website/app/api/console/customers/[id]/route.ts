// /api/console/customers/[id] —— 客户 360（GET）、客户动作（POST）与主档编辑（PATCH）。
// GET：客户行 + identities + 名下 orders/licenses/leads/personas（按 customer_id 过滤）+ 审计流水。
// POST：{ action: "attach_identity", kind, value } —— 幂等挂身份；冲突（已属他人）返回 409；
//       { action: "detach_identity", identity_id } —— 解绑身份（不回滚历史归属）。
// PATCH：{ display_name?, primary_contact?, notes? } —— 编辑主档人工字段（只更新传入键）。
// RBAC：GET viewer+；POST/PATCH admin+（audit actor="console:<username>"）。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireConsole } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import {
  IDENTITY_KINDS,
  attachIdentity,
  detachIdentity,
  getLedgerDb,
  listLeads,
  listLicenses,
  listOrders,
  updateCustomerProfile,
  type IdentityKind,
  type UpdateCustomerInput,
} from "@/lib/ledger";
import { listPersonas } from "@/lib/personas";
import { getCustomerById, listAuditForCustomer, listIdentitiesByCustomer } from "@/app/console/data";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest, { params }: { params: { id: string } }) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const customer = getCustomerById(params.id);
    if (!customer) {
      return NextResponse.json({ error: "customer not found" }, { status: 404 });
    }
    return NextResponse.json({
      ok: true,
      customer,
      identities: listIdentitiesByCustomer(customer.id),
      orders: listOrders({ customerId: customer.id, limit: 200 }).rows,
      licenses: listLicenses({ customerId: customer.id, limit: 200 }).rows,
      leads: listLeads({ customerId: customer.id, limit: 200 }).rows,
      personas: listPersonas({ customerId: customer.id, limit: 200 }).rows,
      audit: listAuditForCustomer(customer.id),
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

export async function POST(req: NextRequest, { params }: { params: { id: string } }) {
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
  const action = String(body.action ?? "");
  const actor = `console:${user.username}`;

  if (action === "detach_identity") {
    const identityId = Number(body.identity_id);
    if (!Number.isInteger(identityId) || identityId <= 0) {
      return NextResponse.json({ error: "identity_id required" }, { status: 400 });
    }
    try {
      const customer = getCustomerById(params.id);
      if (!customer) {
        return NextResponse.json({ error: "customer not found" }, { status: 404 });
      }
      const ok = detachIdentity(customer.id, identityId, getLedgerDb(), actor);
      if (!ok) {
        return NextResponse.json({ error: "identity not found on this customer" }, { status: 404 });
      }
      return NextResponse.json({ ok: true });
    } catch (e) {
      return NextResponse.json({ error: String(e) }, { status: 500 });
    }
  }

  if (action !== "attach_identity") {
    return NextResponse.json({ error: `unknown action: ${action}` }, { status: 400 });
  }
  const kind = String(body.kind ?? "") as IdentityKind;
  const value = String(body.value ?? "").trim();
  if (!IDENTITY_KINDS.includes(kind)) {
    return NextResponse.json({ error: `kind must be one of ${IDENTITY_KINDS.join("/")}` }, { status: 400 });
  }
  if (!value) {
    return NextResponse.json({ error: "value required" }, { status: 400 });
  }
  try {
    const customer = getCustomerById(params.id);
    if (!customer) {
      return NextResponse.json({ error: "customer not found" }, { status: 404 });
    }
    const result = attachIdentity(customer.id, kind, value, getLedgerDb(), actor);
    if (!result.ok) {
      return NextResponse.json(
        { error: "identity already belongs to another customer", ...result },
        { status: 409 }
      );
    }
    return NextResponse.json({ ...result, ok: true });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

export async function PATCH(req: NextRequest, { params }: { params: { id: string } }) {
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
  const input: UpdateCustomerInput = {};
  for (const key of ["display_name", "primary_contact", "notes"] as const) {
    if (body[key] !== undefined) input[key] = body[key] == null ? null : String(body[key]);
  }
  if (Object.keys(input).length === 0) {
    return NextResponse.json({ error: "nothing to update (display_name / primary_contact / notes)" }, { status: 400 });
  }
  // 显示名是列表/归属控件的主要检索键，不允许清成空
  if (input.display_name !== undefined && !String(input.display_name ?? "").trim()) {
    return NextResponse.json({ error: "display_name cannot be empty" }, { status: 400 });
  }
  try {
    const customer = getCustomerById(params.id);
    if (!customer) {
      return NextResponse.json({ error: "customer not found" }, { status: 404 });
    }
    const ok = updateCustomerProfile(customer.id, input, getLedgerDb(), `console:${user.username}`);
    return NextResponse.json({ ok });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
