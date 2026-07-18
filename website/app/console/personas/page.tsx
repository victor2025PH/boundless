// /console/personas：人设注册表 —— 一个数字身份四个槽位（face/voice/prompt/knowledge），
// 贯穿获客→承接→变现。列表：槽位点亮态、状态徽章、归属客户、授权产品数、搜索+状态筛选。
import Link from "next/link";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { getPersonaStats, listPersonas } from "@/lib/personas";
import { getCustomerById } from "../data";
import {
  Card,
  Code,
  CustomerLink,
  DataTable,
  EmptyState,
  FilterSubmit,
  PageHeader,
  Pager,
  PersonaSlotCells,
  PersonaStatusBadge,
  ShortId,
  SystemBadge,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;
const STATUS_CHOICES = [
  { value: "", label: "全部状态" },
  { value: "active", label: "在用 active" },
  { value: "archived", label: "已归档 archived" },
  { value: "purge_pending", label: "清除中 purge_pending" },
  { value: "purged", label: "已清除 purged" },
] as const;

export default function PersonasPage({
  searchParams,
}: {
  searchParams: { q?: string; status?: string; offset?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const q = searchParams.q?.trim() || undefined;
  const status = searchParams.status?.trim() || undefined;
  const offset = Math.max(0, Number(searchParams.offset) || 0);

  const { rows, total } = listPersonas({ q, status, limit: LIMIT, offset });
  const stats = getPersonaStats();

  const nameById = new Map<string, string | null>();
  for (const p of rows) {
    if (p.customer_id && !nameById.has(p.customer_id)) {
      nameById.set(p.customer_id, getCustomerById(p.customer_id)?.display_name ?? null);
    }
  }

  const hasFilter = !!(q || status);

  return (
    <div>
      <PageHeader
        title="人设"
        desc={
          <>
            人设总线注册表：同一个数字身份（face 形象 / voice 声纹 / prompt 话术 / knowledge 知识库
            四槽位）贯穿获客→承接→变现。只存元数据与指纹，资产本体在各引擎侧；全域清除经
            purge 协议逐引擎下发并回执。
          </>
        }
        actions={
          <div className="flex gap-4 text-xs text-slate-400">
            <span>
              共 <b className="text-slate-200">{stats.total}</b> 个
            </span>
            <span>
              清除中 <b className={stats.purgePending ? "text-amber-300" : "text-slate-200"}>{stats.purgePending}</b>
            </span>
            <span>
              槽位 <b className="text-slate-200">
                脸{stats.slots.face}·声{stats.slots.voice}·话{stats.slots.prompt}·知{stats.slots.knowledge}
              </b>
            </span>
          </div>
        }
      />

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索来源键 / 显示名 / 人设 ID"
          className={`${filterInputCls} w-64`}
        />
        <select name="status" defaultValue={status ?? ""} className={filterInputCls}>
          {STATUS_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <FilterSubmit />
        {hasFilter && (
          <Link href="/console/personas" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={hasFilter ? "没有匹配的人设" : "人设注册表还是空的"}
          hints={
            hasFilter
              ? ["调整搜索或状态筛选试试。"]
              : [
                  <span key="export">
                    先在引擎侧生成人设导出 JSON（
                    <Code>{`{"version":1,"source_system":"avatarhub","personas":[{source_key, slots:{face:{present:true,fingerprint:"…"},…}}]}`}</Code>
                    ）；
                  </span>,
                  <span key="import">
                    再运行 <Code>node scripts/ledger-import-personas.mjs &lt;导出json&gt;</Code>{" "}
                    导入（幂等，可重复执行；同格式 .jsonl 逐行也支持）。
                  </span>,
                  "导入不建客户档案：归属客户在本页详情里人工操作。",
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["人设", "来源", "槽位", "状态", "归属客户", "授权产品", "创建时间", ""]}>
            {rows.map((p) => (
              <tr key={p.id} className="hover:bg-slate-800/40">
                <Td>
                  <Link href={`/console/personas/${p.id}`} className="font-medium text-amber-300 hover:underline">
                    {p.display_name || "（未命名）"}
                  </Link>
                  <div className="mt-0.5">
                    <ShortId id={p.id} />
                  </div>
                </Td>
                <Td>
                  <SystemBadge system={p.source_system} />
                  <div className="mt-0.5 font-mono text-[11px] text-slate-500">{p.source_key}</div>
                </Td>
                <Td>
                  <PersonaSlotCells
                    face={!!p.slot_face}
                    voice={!!p.slot_voice}
                    prompt={!!p.slot_prompt}
                    knowledge={!!p.slot_knowledge}
                  />
                </Td>
                <Td>
                  <PersonaStatusBadge status={p.status} />
                </Td>
                <Td>
                  {p.customer_id ? (
                    <CustomerLink customerId={p.customer_id} label={nameById.get(p.customer_id)} />
                  ) : (
                    <span className="text-xs text-slate-600">未归属</span>
                  )}
                </Td>
                <Td className="text-xs tabular-nums text-slate-300">{p.grant_count}</Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(p.created_at)}</Td>
                <Td>
                  <Link
                    href={`/console/personas/${p.id}`}
                    className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-amber-500/60 hover:text-amber-300"
                  >
                    详情 →
                  </Link>
                </Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Pager basePath="/console/personas" params={{ q, status }} total={total} limit={LIMIT} offset={offset} />
    </div>
  );
}
