// /api/console/personas/[id] —— 人设详情（GET）与人设动作（POST）。
// GET：persona 行 + grants（含已撤销）+ purge 指令进度 + 归属客户行。viewer+。
// POST（admin+，audit actor="console:<username>"）：
//   { action: "grant",  product_id }          —— 授权产品（幂等；purge 中/已清除冻结）
//   { action: "revoke", product_id }          —— 撤销授权（幂等）
//   { action: "purge" }                       —— 发起全域清除（status→purge_pending，
//                                                逐引擎下发 persona_purges 指令）
//   { action: "assign_customer", customer_id }—— 归属客户（可改归属）
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireConsole } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { getLedgerDb } from "@/lib/ledger";
import {
  assignPersonaCustomer,
  getPersona,
  grantProduct,
  requestPurge,
  revokeProduct,
} from "@/lib/personas";
import { getCustomerById } from "@/app/console/data";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest, { params }: { params: { id: string } }) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const detail = getPersona(params.id);
    if (!detail) {
      return NextResponse.json({ error: "persona not found" }, { status: 404 });
    }
    const customer = detail.persona.customer_id ? getCustomerById(detail.persona.customer_id) : null;
    return NextResponse.json({ ok: true, ...detail, customer });
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
  const actor = `console:${user.username}`;
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const action = String(body.action ?? "");
  try {
    const db = getLedgerDb();
    if (action === "grant" || action === "revoke") {
      const productId = String(body.product_id ?? "").trim();
      if (!productId) {
        return NextResponse.json({ error: "product_id required" }, { status: 400 });
      }
      const result =
        action === "grant"
          ? grantProduct(params.id, productId, db, actor)
          : revokeProduct(params.id, productId, db, actor);
      if (!result.ok) {
        const status = result.error?.includes("not found") ? 404 : 409;
        return NextResponse.json({ error: result.error }, { status });
      }
      return NextResponse.json({ ok: true, action, product_id: productId, existed: result.existed ?? false });
    }
    if (action === "purge") {
      const result = requestPurge(params.id, actor, db);
      if (!result.ok) {
        const status = result.error?.includes("not found") ? 404 : 409;
        return NextResponse.json({ error: result.error }, { status });
      }
      return NextResponse.json({ ok: true, action, targets: result.targets });
    }
    if (action === "assign_customer") {
      const customerId = String(body.customer_id ?? "").trim();
      if (!customerId) {
        return NextResponse.json({ error: "customer_id required" }, { status: 400 });
      }
      const ok = assignPersonaCustomer(params.id, customerId, db, actor);
      if (!ok) {
        return NextResponse.json({ error: "assign failed: persona or customer not found" }, { status: 404 });
      }
      return NextResponse.json({ ok: true, action, customer_id: customerId });
    }
    return NextResponse.json({ error: `unknown action: ${action}` }, { status: 400 });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
