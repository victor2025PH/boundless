// /console/health：五机台账只读页。v1 不真 SSH；可选 ?probe=1 探测本机 localhost。
// 内网 IP 标明「仅内网可见」；真实五机探活链到 docs/MACHINE_SSH.md / cluster_ping。
import Link from "next/link";
import { Activity, Server } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import {
  getMachineMesh,
  isPrivateLanIp,
  probeLocalEndpoints,
  type LocalProbeResult,
} from "@/lib/machine-mesh";
import { Card, Code, DataTable, PageHeader, SectionTitle, Td } from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ROLE_LABEL: Record<string, string> = {
  dev: "开发机",
  compute: "算力节点",
};

export default async function HealthPage({
  searchParams,
}: {
  searchParams: { probe?: string };
}) {
  if (!hasConsoleSession()) return null;

  const mesh = getMachineMesh();
  const wantProbe = ["1", "true"].includes(searchParams.probe ?? "");
  let localProbe: LocalProbeResult[] | null = null;
  if (wantProbe) {
    localProbe = await probeLocalEndpoints(mesh);
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="五机健康"
        desc={
          <>
            台账来自 <Code>lib/machine-mesh.ts</Code>（同步自 <Code>deploy/machines.json</Code>）。
            本页不 SSH；内网地址从公网不可达。运维探活请用{" "}
            <Code>tools/cluster_ping.ps1</Code>，说明见仓库{" "}
            <Code>docs/MACHINE_SSH.md</Code>。
          </>
        }
        actions={
          <form method="GET">
            <input type="hidden" name="probe" value="1" />
            <button
              type="submit"
              className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-300 hover:bg-amber-500/20"
            >
              <Activity className="h-3.5 w-3.5" />
              探测本机 localhost
            </button>
          </form>
        }
      />

      <Card>
        <SectionTitle count={mesh.length}>五机台账</SectionTitle>
        <DataTable head={["机器", "角色", "IP", "主品牌", "主服务 / 端口", "产品", "可见性"]}>
          {mesh.map((m) => {
            const lanOnly = isPrivateLanIp(m.ip);
            const ports = m.services.length
              ? m.services
                  .map((s) => (s.port != null ? `${s.name}:${s.port}` : s.name))
                  .join(" · ")
              : "—";
            return (
              <tr key={m.id} className="hover:bg-slate-800/40">
                <Td>
                  <div className="flex items-center gap-2">
                    <Server className="h-4 w-4 shrink-0 text-amber-400/70" />
                    <div>
                      <p className="text-sm font-semibold text-white">{m.zh}</p>
                      <p className="font-mono text-[10px] text-slate-500">{m.id}</p>
                    </div>
                  </div>
                </Td>
                <Td className="text-xs text-slate-300">{ROLE_LABEL[m.role] ?? m.role}</Td>
                <Td className="font-mono text-xs text-slate-200">{m.ip}</Td>
                <Td className="font-mono text-xs text-amber-300/80">{m.primaryBrand || "—"}</Td>
                <Td className="max-w-[280px] font-mono text-[11px] text-slate-400">
                  <span className="block truncate" title={ports}>
                    {ports}
                  </span>
                  {m.note && (
                    <span className="mt-0.5 block truncate text-[10px] text-slate-600" title={m.note}>
                      {m.note}
                    </span>
                  )}
                </Td>
                <Td className="text-xs text-slate-400">{m.products.join(" / ") || "—"}</Td>
                <Td>
                  {lanOnly ? (
                    <span className="rounded-full border border-slate-600 bg-slate-800/80 px-2 py-0.5 text-[10px] font-medium text-slate-400">
                      仅内网可见
                    </span>
                  ) : (
                    <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-300">
                      可达探测
                    </span>
                  )}
                </Td>
              </tr>
            );
          })}
        </DataTable>
      </Card>

      {localProbe && (
        <Card>
          <SectionTitle count={localProbe.length}>本机探测结果</SectionTitle>
          <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
            仅请求 <Code>127.0.0.1</Code> 上的已知端口与本站健康端点；不能代表五机真实状态。
            {wantProbe && (
              <Link href="/console/health" className="ml-2 text-amber-300 underline-offset-2 hover:underline">
                清除探测
              </Link>
            )}
          </p>
          <DataTable head={["目标", "结果", "耗时", "详情"]}>
            {localProbe.map((p) => (
              <tr key={p.target} className="hover:bg-slate-800/40">
                <Td className="font-mono text-xs text-slate-300">{p.target}</Td>
                <Td>
                  {p.ok ? (
                    <span className="text-xs font-medium text-emerald-300">ok</span>
                  ) : (
                    <span className="text-xs font-medium text-slate-500">unreachable</span>
                  )}
                </Td>
                <Td className="font-mono text-xs tabular-nums text-slate-400">{p.ms}ms</Td>
                <Td className="font-mono text-[11px] text-slate-500">{p.detail}</Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Card className="border-slate-800">
        <SectionTitle>运维探活</SectionTitle>
        <ul className="space-y-1.5 text-xs leading-relaxed text-slate-400">
          <li>
            API：<Code>GET /api/console/health</Code>
            （可选 <Code>?probe=1</Code>；头 <Code>x-console-key</Code>）
          </li>
          <li>
            脚本：仓库内 <Code>powershell -File tools\cluster_ping.ps1</Code>
          </li>
          <li>
            台账源：<Code>deploy/machines.json</Code> · 覆盖可用环境变量{" "}
            <Code>MACHINE_MESH_JSON</Code>
          </li>
        </ul>
      </Card>
    </div>
  );
}
