// /console/customers/[id]：客户 360 —— 本后台的灵魂页面。
// 基本信息 + 身份标识（admin+ 可添加）+ 名下订单/授权/留资三分区 + 审计流水。
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, ScrollText } from "lucide-react";
import { listLeads, listLicenses, listOrders } from "@/lib/ledger";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { getCustomerById, listAuditForCustomer, listIdentitiesByCustomer } from "../../data";
import { AttachIdentityForm } from "../../ui";
import {
  Card,
  DataTable,
  EmptyState,
  ExpiryCell,
  LeadStatusBadge,
  OrderStatusBadge,
  SectionTitle,
  SystemBadge,
  Td,
  fmtAmount,
  fmtDateTime,
} from "../../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const KIND_STYLE: Record<string, string> = {
  contact: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  tg: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  email: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  phone: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  fingerprint: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

export default function Customer360Page({ params }: { params: { id: string } }) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const customer = getCustomerById(params.id);
  if (!customer) notFound();

  const identities = listIdentitiesByCustomer(customer.id);
  const orders = listOrders({ customerId: customer.id, limit: 200 }).rows;
  const licenses = listLicenses({ customerId: customer.id, limit: 200 }).rows;
  const leads = listLeads({ customerId: customer.id, limit: 200 }).rows;
  const audit = listAuditForCustomer(customer.id);

  const paidTotal = orders
    .filter((o) => o.status === "paid" || o.status === "activated")
    .reduce((sum, o) => sum + (o.pay_amount ?? o.amount ?? 0), 0);

  const info: [string, React.ReactNode][] = [
    ["客户 ID", <span key="id" className="font-mono text-xs text-slate-300">{customer.id}</span>],
    ["显示名", customer.display_name || "（未命名）"],
    ["主联系方式", customer.primary_contact || "—"],
    ["TG 用户", customer.tg_user_id || "—"],
    ["来源", customer.source || "—"],
    ["创建 / 更新", `${fmtDateTime(customer.created_at)} / ${fmtDateTime(customer.updated_at)}`],
  ];

  return (
    <div className="space-y-5">
      <div>
        <Link
          href="/console/customers"
          className="mb-3 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-amber-300"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回客户列表
        </Link>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h1 className="text-xl font-bold text-white">
            {customer.display_name || "（未命名客户）"}
            <span className="ml-3 align-middle text-xs font-normal text-slate-500">客户 360</span>
          </h1>
          <div className="flex gap-4 text-xs text-slate-400">
            <span>
              成交额 <b className="text-amber-300">{paidTotal ? paidTotal.toFixed(2) : "0"}</b>
            </span>
            <span>
              订单 <b className="text-slate-200">{orders.length}</b>
            </span>
            <span>
              授权 <b className="text-slate-200">{licenses.length}</b>
            </span>
            <span>
              留资 <b className="text-slate-200">{leads.length}</b>
            </span>
          </div>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <SectionTitle>基本信息</SectionTitle>
          <dl className="grid grid-cols-[7rem_1fr] gap-y-2 text-sm">
            {info.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-xs leading-6 text-slate-500">{k}</dt>
                <dd className="leading-6 text-slate-200">{v}</dd>
              </div>
            ))}
          </dl>
          {customer.notes && (
            <p className="mt-3 rounded-lg bg-slate-800/60 p-3 text-xs leading-relaxed text-slate-300">{customer.notes}</p>
          )}
        </Card>

        <Card>
          <SectionTitle count={identities.length}>身份标识</SectionTitle>
          <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
            身份是自动归属的钥匙：挂上 contact / tg / fingerprint 后，新订单与留资会按标识自动归到本客户。
          </p>
          {identities.length > 0 && (
            <ul className="mb-4 space-y-1.5">
              {identities.map((it) => (
                <li key={it.id} className="flex items-center gap-2 text-sm">
                  <span
                    className={`inline-block w-24 shrink-0 rounded-full border px-2 py-0.5 text-center font-mono text-[11px] ${
                      KIND_STYLE[it.kind] ?? KIND_STYLE.fingerprint
                    }`}
                  >
                    {it.kind}
                  </span>
                  <span className="break-all font-mono text-xs text-slate-200">{it.value}</span>
                  <span className="ml-auto shrink-0 text-[11px] text-slate-600">{fmtDateTime(it.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
          {canWrite ? (
            <AttachIdentityForm customerId={customer.id} />
          ) : (
            <p className="text-[11px] text-slate-600">viewer 只读：挂身份操作需 admin 及以上角色。</p>
          )}
        </Card>
      </div>

      <Card>
        <SectionTitle count={orders.length}>名下订单</SectionTitle>
        {orders.length === 0 ? (
          <p className="text-xs text-slate-500">
            暂无归属订单 —— 到 <Link href="/console/orders" className="text-amber-300 hover:underline">订单台账</Link>
            对未归属的行点「归属客户」。
          </p>
        ) : (
          <DataTable head={["来源单号", "产品 / 方案", "金额", "状态", "联系方式", "创建时间"]}>
            {orders.map((o) => (
              <tr key={o.id} className="hover:bg-slate-800/40">
                <Td className="font-mono text-xs text-slate-300">{o.source_key}</Td>
                <Td className="text-xs text-slate-300">
                  {[o.product_id, o.plan, o.edition, o.period].filter(Boolean).join(" / ") || "—"}
                </Td>
                <Td className="text-xs text-slate-200">{fmtAmount(o.amount, o.pay_amount, o.currency)}</Td>
                <Td>
                  <OrderStatusBadge status={o.status} />
                </Td>
                <Td className="text-xs text-slate-400">{o.contact || "—"}</Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(o.created_at)}</Td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <Card>
        <SectionTitle count={licenses.length}>名下授权</SectionTitle>
        {licenses.length === 0 ? (
          <p className="text-xs text-slate-500">
            暂无归属授权 —— 到 <Link href="/console/licenses" className="text-amber-300 hover:underline">授权台账</Link>
            归属，或先用 scripts/ledger-import-licenses.mjs 导入。
          </p>
        ) : (
          <DataTable head={["系统", "授权号", "产品 / 方案", "席位", "到期", "状态"]}>
            {licenses.map((l) => (
              <tr key={l.id} className="hover:bg-slate-800/40">
                <Td>
                  <SystemBadge system={l.source_system} />
                </Td>
                <Td className="font-mono text-xs text-slate-300">{l.source_key}</Td>
                <Td className="text-xs text-slate-300">
                  {[l.product_id, l.plan, l.edition].filter(Boolean).join(" / ") || "—"}
                </Td>
                <Td className="text-xs text-slate-400">{l.seats ?? "—"}</Td>
                <Td className="text-xs">
                  <ExpiryCell expiresAt={l.expires_at} />
                </Td>
                <Td className="text-xs text-slate-400">{l.status || "—"}</Td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <Card>
        <SectionTitle count={leads.length}>名下留资</SectionTitle>
        {leads.length === 0 ? (
          <p className="text-xs text-slate-500">
            暂无归属留资 —— 到 <Link href="/console/leads" className="text-amber-300 hover:underline">留资列表</Link>
            做客户归并（日常跟进仍在 /admin）。
          </p>
        ) : (
          <DataTable head={["来源键", "称呼", "联系方式", "意向", "状态", "最近活跃"]}>
            {leads.map((l) => (
              <tr key={l.source_key} className="hover:bg-slate-800/40">
                <Td className="font-mono text-xs text-slate-300">{l.source_key}</Td>
                <Td className="text-xs text-slate-300">{l.name || "—"}</Td>
                <Td className="text-xs text-slate-300">{l.contact || "—"}</Td>
                <Td className="max-w-[200px] truncate text-xs text-slate-400">
                  <span title={l.interest ?? undefined}>{l.interest || "—"}</span>
                </Td>
                <Td>
                  <LeadStatusBadge status={l.status} />
                </Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(l.last_seen)}</Td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <Card>
        <SectionTitle count={audit.length}>审计流水</SectionTitle>
        <p className="mb-3 flex items-start gap-1.5 text-[11px] leading-relaxed text-slate-500">
          <ScrollText className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          本页所有变更（建档 / 挂身份 / 归属）都会写入 audit 表；此处展示与该客户相关的最近 30 条，作合规追溯用。
        </p>
        {audit.length === 0 ? (
          <EmptyState title="暂无审计记录" hints={["对该客户做任何归属 / 身份操作后会在此留痕。"]} />
        ) : (
          <ul className="space-y-2">
            {audit.map((a) => (
              <li key={a.id} className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5 text-xs">
                <span className="shrink-0 font-mono text-slate-600">{fmtDateTime(a.ts)}</span>
                <span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-amber-300/80">
                  {a.actor ?? "system"}
                </span>
                <span className="shrink-0 font-medium text-slate-200">{a.action}</span>
                <span className="text-slate-500">
                  {a.entity ? `${a.entity}:${a.entity_id ?? ""}` : ""}
                </span>
                {a.detail && (
                  <span className="max-w-full truncate font-mono text-[10px] text-slate-600" title={a.detail}>
                    {a.detail}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
