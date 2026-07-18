// /console/personas/[id]：人设详情 —— 四槽位卡（指纹摘要/版本）、授权矩阵（7 产品，
// admin+ 可切换）、归属客户、purge 进度表、危险区（全域清除，admin+）。
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, Flame } from "lucide-react";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { listCustomers } from "@/lib/ledger";
import {
  PERSONA_PRODUCT_IDS,
  PRODUCT_ENGINE_MAP,
  computePurgeTargets,
  getPersona,
  type PersonaSlot,
} from "@/lib/personas";
import { getCustomerById } from "../../data";
import {
  Card,
  CustomerLink,
  PERSONA_SLOT_META,
  PersonaStatusBadge,
  SectionTitle,
  SystemBadge,
  fmtDateTime,
} from "../../parts";
import {
  AssignPersonaCustomerControl,
  GrantToggle,
  PersonaPurgeButton,
  type PersonaCustomerOption,
} from "../ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const PRODUCT_LABEL: Record<string, string> = {
  zhituo: "智拓 ReachX",
  zhiliao: "智聊 ChatX",
  tongyi: "通译 LingoX",
  tongchuan: "通传 VoxX",
  huansheng: "幻声 VoiceX",
  huanying: "幻影 LiveX",
  huanyan: "幻颜 FaceX",
};

interface SlotDetail {
  present?: boolean;
  fingerprint?: string;
  ref?: string;
  version?: string;
}

/** slots_detail JSON → {face|voice|prompt|knowledge: SlotDetail, _meta?}。坏 JSON 按空处理。 */
function parseSlotsDetail(raw: string | null): {
  slots: Partial<Record<PersonaSlot, SlotDetail>>;
  customerNameHint: string | null;
} {
  if (!raw) return { slots: {}, customerNameHint: null };
  try {
    const obj = JSON.parse(raw) as Record<string, unknown>;
    const slots: Partial<Record<PersonaSlot, SlotDetail>> = {};
    for (const key of ["face", "voice", "prompt", "knowledge"] as PersonaSlot[]) {
      const v = obj[key];
      if (v && typeof v === "object" && !Array.isArray(v)) slots[key] = v as SlotDetail;
    }
    const meta = obj._meta as { customer_name?: unknown } | undefined;
    const hint =
      meta && typeof meta === "object" && meta.customer_name != null ? String(meta.customer_name) : null;
    return { slots, customerNameHint: hint };
  } catch {
    return { slots: {}, customerNameHint: null };
  }
}

function parseTags(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.map(String) : [];
  } catch {
    return [];
  }
}

/** 长指纹缩短显示（前 12 位 + …），title 提供完整值。 */
function FingerprintDigest({ value }: { value: string }) {
  const short = value.length > 16 ? `${value.slice(0, 12)}…` : value;
  return (
    <span title={value} className="break-all font-mono text-xs text-slate-200">
      {short}
    </span>
  );
}

export default function PersonaDetailPage({ params }: { params: { id: string } }) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const detail = getPersona(params.id);
  if (!detail) notFound();
  const { persona, grants, purges } = detail;

  const customer = persona.customer_id ? getCustomerById(persona.customer_id) : null;
  const { slots, customerNameHint } = parseSlotsDetail(persona.slots_detail);
  const tags = parseTags(persona.tags);

  const activeGrantIds = new Set(grants.filter((g) => g.revoked_at === null).map((g) => g.product_id));
  const frozen = persona.status === "purge_pending" || persona.status === "purged";
  const purgeTargets = computePurgeTargets(persona, grants);
  const grantedLabels = [...activeGrantIds].map((p) => PRODUCT_LABEL[p] ?? p);

  const customerOptions: PersonaCustomerOption[] = listCustomers({ limit: 500 }).rows.map((c) => ({
    id: c.id,
    label: `${c.display_name || "（未命名）"}${c.primary_contact ? ` · ${c.primary_contact}` : ""}`,
  }));

  const slotFlags: Record<PersonaSlot, boolean> = {
    face: !!persona.slot_face,
    voice: !!persona.slot_voice,
    prompt: !!persona.slot_prompt,
    knowledge: !!persona.slot_knowledge,
  };

  return (
    <div className="space-y-5">
      <div>
        <Link
          href="/console/personas"
          className="mb-3 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-amber-300"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回人设列表
        </Link>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h1 className="flex flex-wrap items-center gap-3 text-xl font-bold text-white">
            {persona.display_name || "（未命名人设）"}
            <PersonaStatusBadge status={persona.status} />
          </h1>
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
            <span className="flex items-center gap-1.5">
              来源 <SystemBadge system={persona.source_system} />
              <span className="font-mono text-slate-300">{persona.source_key}</span>
            </span>
            <span className="font-mono text-slate-600" title={persona.id}>
              {persona.id}
            </span>
          </div>
        </div>
        {tags.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {tags.map((t) => (
              <span key={t} className="rounded-full bg-slate-800 px-2 py-0.5 text-[11px] text-slate-300">
                #{t}
              </span>
            ))}
          </div>
        )}
      </div>

      <div>
        <SectionTitle>四槽位</SectionTitle>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {PERSONA_SLOT_META.map(({ key, label, Icon, lit }) => {
            const on = slotFlags[key];
            const d = slots[key] ?? {};
            return (
              <Card key={key} className={on ? "" : "opacity-60"}>
                <div className="mb-2 flex items-center gap-2">
                  <span
                    className={`inline-flex h-8 w-8 items-center justify-center rounded-lg border ${
                      on ? lit : "border-slate-800 bg-slate-900/60 text-slate-700"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                  <div className="leading-tight">
                    <p className="text-sm font-semibold text-white">{label.split(" ")[0]}</p>
                    <p className="text-[10px] text-slate-500">{label.split(" ").slice(1).join(" ")}</p>
                  </div>
                </div>
                {on ? (
                  <dl className="space-y-1 text-xs">
                    <div className="flex gap-2">
                      <dt className="w-10 shrink-0 text-slate-500">指纹</dt>
                      <dd>{d.fingerprint ? <FingerprintDigest value={d.fingerprint} /> : <span className="text-slate-600">—</span>}</dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-10 shrink-0 text-slate-500">版本</dt>
                      <dd className="text-slate-300">{d.version || "—"}</dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-10 shrink-0 text-slate-500">引用</dt>
                      <dd className="break-all font-mono text-[11px] text-slate-400">{d.ref || "—"}</dd>
                    </div>
                  </dl>
                ) : (
                  <p className="text-xs text-slate-600">未配置</p>
                )}
              </Card>
            );
          })}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <SectionTitle count={activeGrantIds.size}>授权矩阵</SectionTitle>
          <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
            人设授权给哪些产品使用（授权只是使用许可的登记；资产分发仍走各引擎自己的通道）。
            {frozen && <b className="text-amber-300">当前状态下授权已冻结。</b>}
          </p>
          <ul className="divide-y divide-slate-800/70">
            {PERSONA_PRODUCT_IDS.map((pid) => (
              <li key={pid} className="flex items-center justify-between gap-2 py-2">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-slate-200">{PRODUCT_LABEL[pid]}</span>
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                    {PRODUCT_ENGINE_MAP[pid]}
                  </span>
                </div>
                <GrantToggle
                  personaId={persona.id}
                  item={{
                    productId: pid,
                    label: PRODUCT_LABEL[pid],
                    engine: PRODUCT_ENGINE_MAP[pid],
                    granted: activeGrantIds.has(pid),
                  }}
                  canWrite={canWrite}
                  frozen={frozen}
                />
              </li>
            ))}
          </ul>
        </Card>

        <div className="space-y-4">
          <Card>
            <SectionTitle>归属客户</SectionTitle>
            {customer ? (
              <p className="text-sm text-slate-200">
                <CustomerLink customerId={customer.id} label={customer.display_name} />
                {customer.primary_contact && (
                  <span className="ml-2 text-xs text-slate-500">{customer.primary_contact}</span>
                )}
              </p>
            ) : (
              <p className="text-xs text-slate-500">尚未归属客户。</p>
            )}
            {customerNameHint && !customer && (
              <p className="mt-2 rounded-lg bg-slate-800/60 p-2 text-[11px] text-slate-400">
                导入件带的归属线索：<b className="text-slate-300">{customerNameHint}</b>（仅供参考，未自动建档）
              </p>
            )}
            {canWrite ? (
              <div className="mt-3">
                <AssignPersonaCustomerControl personaId={persona.id} customers={customerOptions} />
              </div>
            ) : (
              <p className="mt-3 text-[11px] text-slate-600">viewer 只读：归属操作需 admin 及以上角色。</p>
            )}
          </Card>

          <Card>
            <SectionTitle>档案</SectionTitle>
            <dl className="grid grid-cols-[7rem_1fr] gap-y-2 text-sm">
              <dt className="text-xs leading-6 text-slate-500">创建 / 更新</dt>
              <dd className="leading-6 text-slate-300">
                {fmtDateTime(persona.created_at)} / {fmtDateTime(persona.updated_at)}
              </dd>
              <dt className="text-xs leading-6 text-slate-500">最近同步</dt>
              <dd className="leading-6 text-slate-300">{fmtDateTime(persona.synced_at)}</dd>
            </dl>
          </Card>
        </div>
      </div>

      {purges.length > 0 && (
        <Card>
          <SectionTitle count={purges.length}>清除进度</SectionTitle>
          <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
            各引擎经 /api/sync/personas/purges 轮询指令、删除本地资产后回执；全部回执后人设自动置「已清除」。
            全部人设的指令积压见{" "}
            <Link href="/console/personas/purges" className="text-amber-300/90 underline-offset-2 hover:underline">
              清除队列监控 →
            </Link>
          </p>
          <div className="overflow-x-auto rounded-xl border border-slate-800">
            <table className="w-full min-w-max text-left text-sm">
              <thead>
                <tr className="border-b border-slate-800 bg-slate-900/80 text-[11px] uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2.5 font-medium">目标引擎</th>
                  <th className="px-3 py-2.5 font-medium">下发时间</th>
                  <th className="px-3 py-2.5 font-medium">回执状态</th>
                  <th className="px-3 py-2.5 font-medium">回执详情</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70">
                {purges.map((p) => (
                  <tr key={p.id} className="hover:bg-slate-800/40">
                    <td className="px-3 py-2.5">
                      <SystemBadge system={p.target_system} />
                    </td>
                    <td className="px-3 py-2.5 text-xs text-slate-400">{fmtDateTime(p.requested_at)}</td>
                    <td className="px-3 py-2.5 text-xs">
                      {p.acked_at ? (
                        <span className="font-medium text-emerald-300">已回执 · {fmtDateTime(p.acked_at)}</span>
                      ) : (
                        <span className="text-amber-300">待回执</span>
                      )}
                    </td>
                    <td className="max-w-[280px] truncate px-3 py-2.5 font-mono text-[11px] text-slate-500">
                      <span title={p.ack_detail ?? undefined}>{p.ack_detail || "—"}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {canWrite && !frozen && (
        <Card className="border-rose-500/30">
          <SectionTitle>
            <span className="flex items-center gap-1.5 text-rose-300">
              <Flame className="h-4 w-4" />
              危险区
            </span>
          </SectionTitle>
          <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
            全域清除：向该人设的来源引擎与所有授权产品的承载引擎下发资产删除指令（脸模/声纹/话术/知识库），
            全部回执后状态置「已清除」，且不会被后续同步复活。合规删除请求（客户要求抹除数字分身）走这里。
          </p>
          <PersonaPurgeButton
            personaId={persona.id}
            displayName={persona.display_name || persona.source_key}
            targets={purgeTargets}
            grantedProducts={grantedLabels}
          />
        </Card>
      )}
    </div>
  );
}
