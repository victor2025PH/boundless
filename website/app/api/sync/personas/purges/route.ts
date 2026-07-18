// /api/sync/personas/purges —— 人设全域清除的引擎同步通道（机器对机器）。
//
// ⚠️ 鉴权与权限边界：Authorization: Bearer <EVENT_INGEST_KEY>（与 /api/collect 同一把
// 机器级密钥，最小权限通道）。该 key 在本路由**只能**：
//   1. GET  拉取本引擎名下未 ack 的清除指令（persona_id / source_key / 槽位布尔）；
//   2. POST 回执单条指令（{ purge_id, detail? }）。
// 响应不含客户数据（customer_id / display_name / 联系方式一概不带）；也不能读人设
// 列表、不能发起清除——发起权只在 /console（admin+ 实名账号）。
//
// 协议时序：console 发起 purge（status→purge_pending，逐引擎插指令行）→ 引擎轮询
// GET ?system=<自己> → 本地删除资产后 POST ack → 全部 target ack 后集团侧自动置
// status=purged。指令 ack 幂等：重复 ack 返回 already=true，首次回执保留。
import { NextRequest, NextResponse } from "next/server";
import { ackPurge, listPendingPurges } from "@/lib/personas";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 与 /api/collect 相同的机器密钥校验：503 未配置 / 401 不匹配 / null 通过。 */
function checkAuth(req: NextRequest): NextResponse | null {
  const key = (process.env.EVENT_INGEST_KEY || "").trim();
  if (!key) {
    return NextResponse.json(
      {
        error: "sync_not_configured",
        message: "服务端未配置 EVENT_INGEST_KEY，人设同步通道不可用。请在部署环境设置该密钥后重试（与 /api/collect 同一把机器密钥）。",
      },
      { status: 503 }
    );
  }
  const auth = req.headers.get("authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!given || given !== key) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  return null;
}

export async function GET(req: NextRequest) {
  const denied = checkAuth(req);
  if (denied) return denied;
  const system = (req.nextUrl.searchParams.get("system") || "").trim();
  if (!system) {
    return NextResponse.json({ error: "system query param required (e.g. ?system=avatarhub)" }, { status: 400 });
  }
  try {
    const purges = listPendingPurges(system);
    return NextResponse.json({ ok: true, system, count: purges.length, purges });
  } catch (e) {
    return NextResponse.json({ error: "internal", message: String(e) }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const denied = checkAuth(req);
  if (denied) return denied;
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid_json", message: "请求体必须形如 { purge_id, detail? }" }, { status: 400 });
  }
  const purgeId = Number(body.purge_id);
  if (!Number.isInteger(purgeId) || purgeId <= 0) {
    return NextResponse.json({ error: "purge_id must be a positive integer" }, { status: 400 });
  }
  try {
    const result = ackPurge(purgeId, body.detail, undefined, "sync:engine");
    if (!result.ok) {
      return NextResponse.json({ error: result.error }, { status: 404 });
    }
    return NextResponse.json({
      ok: true,
      purge_id: result.purgeId,
      persona_id: result.personaId,
      target_system: result.targetSystem,
      already: result.already ?? false,
      all_acked: result.allAcked ?? false,
      persona_status: result.personaStatus ?? null,
    });
  } catch (e) {
    return NextResponse.json({ error: "internal", message: String(e) }, { status: 500 });
  }
}
