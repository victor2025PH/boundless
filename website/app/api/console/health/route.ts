// GET /api/console/health —— 五机台账（静态）+ 可选本机 localhost 探测。
// ?probe=1：对 localhost 已知端口短超时探测；内网 192.168.x 不扫（仅内网可见）。
// 真实五机探活留给运维 tools/cluster_ping.ps1；viewer+ 可读（requireConsole）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { getMachineMesh, isPrivateLanIp, probeLocalEndpoints } from "@/lib/machine-mesh";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const mesh = getMachineMesh();
    const machines = mesh.map((m) => ({
      ...m,
      lanOnly: isPrivateLanIp(m.ip),
      primaryPorts: m.services.filter((s) => s.port != null).map((s) => s.port as number),
    }));
    const wantProbe = ["1", "true"].includes(req.nextUrl.searchParams.get("probe") ?? "");
    const probe = wantProbe
      ? {
          local: await probeLocalEndpoints(mesh),
          note: "仅探测本机 localhost；五机内网请用 tools/cluster_ping.ps1（见 docs/MACHINE_SSH.md）",
        }
      : null;
    return NextResponse.json({
      ok: true,
      generatedAt: new Date().toISOString(),
      machines,
      probe,
      docs: {
        ssh: "docs/MACHINE_SSH.md",
        source: "deploy/machines.json（website 内置副本 lib/machine-mesh.ts）",
        ping: "tools/cluster_ping.ps1",
      },
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
