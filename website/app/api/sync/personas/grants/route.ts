// /api/sync/personas/grants —— 人设跨产品授权清单的引擎同步通道（只读，机器对机器）。
//
// ⚠️ 鉴权与权限边界：Authorization: Bearer <EVENT_INGEST_KEY>（与 /api/collect、
// /api/sync/personas/purges 同一把机器级密钥）。该 key 在本路由**只能** GET 拉取本
// 引擎 source_system 下 active persona 的 grants（source_key / product_id / status）。
// 响应不含客户数据（customer_id / display_name / 联系方式一概不带）；不能写授权、
// 不能读人设本体——授权管理权只在 /console。
//
// 用途：引擎侧 fetch_grants.py 拉清单写入本地缓存，供 platform/identity/grant_gate
// 运行时软门控（默认 warn 放行 + 审计；PERSONA_GRANT_ENFORCE=1 才拒绝）。断网时用
// 本地缓存，不挡业务。契约见 platform/identity/PERSONA_BUS.md §4.1。
import { NextRequest, NextResponse } from "next/server";
import {
  listActiveGrantsForSystem,
  PERSONA_SOURCE_SYSTEMS,
  type PersonaSourceSystem,
} from "@/lib/personas";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 与 /api/collect、/api/sync/personas/purges 相同的机器密钥校验。 */
function checkAuth(req: NextRequest): NextResponse | null {
  const key = (process.env.EVENT_INGEST_KEY || "").trim();
  if (!key) {
    return NextResponse.json(
      {
        error: "sync_not_configured",
        message:
          "服务端未配置 EVENT_INGEST_KEY，人设同步通道不可用。请在部署环境设置该密钥后重试（与 /api/collect 同一把机器密钥）。",
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

function isSourceSystem(v: string): v is PersonaSourceSystem {
  return (PERSONA_SOURCE_SYSTEMS as readonly string[]).includes(v);
}

export async function GET(req: NextRequest) {
  const denied = checkAuth(req);
  if (denied) return denied;
  const system = (req.nextUrl.searchParams.get("system") || "").trim();
  if (!system) {
    return NextResponse.json(
      { error: "system query param required (e.g. ?system=avatarhub)" },
      { status: 400 }
    );
  }
  if (!isSourceSystem(system)) {
    return NextResponse.json(
      {
        error: "invalid_system",
        message: `system must be one of: ${PERSONA_SOURCE_SYSTEMS.join("|")}`,
      },
      { status: 400 }
    );
  }
  try {
    const grants = listActiveGrantsForSystem(system);
    return NextResponse.json({
      ok: true,
      system,
      count: grants.length,
      grants,
    });
  } catch (e) {
    return NextResponse.json({ error: "internal", message: String(e) }, { status: 500 });
  }
}
