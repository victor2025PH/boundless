// /console/customers/[id]：客户 360 —— 本后台的灵魂页面。
// 基本信息 + 身份标识（admin+ 可添加）+ 名下订单/授权/人设/留资分区 + 跨售商机 + 审计流水。
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, ScrollText, Sparkles } from "lucide-react";
import { listLeads, listLicenses, listOrders } from "@/lib/ledger";
import { listOpportunities } from "@/lib/opportunities";
import { IDENTITY_KIND_LABEL, PRODUCT_LABEL, lbl, opportunityPriority, seatsLabel } from "../../labels";
import { productDisplay, tierLabel } from "../../sku-names";
import { listPersonas } from "@/lib/personas";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { getCustomerById, listAuditForCustomer, listIdentitiesByCustomer } from "../../data";
import {
  AttachIdentityForm,
  DetachIdentityButton,
  EditCustomerForm,
  OpportunityActions,
  OpportunityLogBadge,
} from "../../ui";
import {
  Card,
  DataTable,
  EmptyState,
  ExpiryCell,
  LeadStatusBadge,
  LicenseStatusBadge,
  OpportunityKindBadge,
  OrderStatusBadge,
  PersonaSlotCells,
  PersonaStatusBadge,
  SectionTitle,
  SystemBadge,
  Td,
  TestBadge,
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
  // 客户 360 不隐藏测试行（includeTest）：运营看该客户的全部足迹，测试行由徽章标注
  const orders = listOrders({ customerId: customer.id, limit: 200, includeTest: true }).rows;
  const licenses = listLicenses({ customerId: customer.id, limit: 200, includeTest: true }).rows;
  const leads = listLeads({ customerId: customer.id, limit: 200, includeTest: true }).rows;
  const personas = listPersonas({ customerId: customer.id, limit: 200 }).rows;
  const opportunities = listOpportunities({ customerId: customer.id, limit: 50 });
  const audit = listAuditForCustomer(customer.id);

  // 成交额按币种分组（挂牌 USD / 结算 USDT 并存，直接求和会把不同币种混在一起）
  const paidByCurrency = new Map<string, number>();
  for (const o of orders) {
    if (o.status !== "paid" && o.status !== "activated") continue;
    const cur = o.currency ?? "";
    paidByCurrency.set(cur, (paidByCurrency.get(cur) ?? 0) + (o.pay_amount ?? o.amount ?? 0));
  }
  const paidTotalText = paidByCurrency.size
    ? [...paidByCurrency.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([cur, amt]) => `${Math.round(amt * 100) / 100} ${cur || "(未标币种)"}`)
        .join(" + ")
    : "0";

  const info: [string, React.ReactNode][] = [
    ["客户 ID", <span key="id" className="font-mono text-xs text-slate-300">{customer.id}</span>],
    ["显示名", customer.display_name || "（未命名）"],
    ["主联系方式", customer.primary_contact || "—"],
    ["Telegram 用户", customer.tg_user_id || "—"],
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
            {customer.is_test === 1 && <TestBadge className="ml-2 align-middle" />}
            <span className="ml-3 align-middle text-xs font-normal text-slate-500">客户 360</span>
          </h1>
          <div className="flex gap-4 text-xs text-slate-400">
            <span>
              成交额 <b className="text-amber-300">{paidTotalText}</b>
            </span>
            <span>
              订单 <b className="text-slate-200">{orders.length}</b>
            </span>
            <span>
              授权 <b className="text-slate-200">{licenses.length}</b>
            </span>
            <span>
              人设 <b className="text-slate-200">{personas.length}</b>
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
            <p className="mt-3 rounded-lg bg-ink-700/60 p-3 text-xs leading-relaxed text-slate-300">{customer.notes}</p>
          )}
          {canWrite && (
            <div className="mt-3">
              <EditCustomerForm
                customerId={customer.id}
                initial={{
                  display_name: customer.display_name,
                  primary_contact: customer.primary_contact,
                  notes: customer.notes,
                }}
              />
            </div>
          )}
        </Card>

        <Card>
          <SectionTitle count={identities.length}>身份标识</SectionTitle>
          <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
            身份是自动归属的钥匙：挂上联系方式 / Telegram / 设备指纹后，新订单与留资会按标识自动归到本客户。
          </p>
          {identities.length > 0 && (
            <ul className="mb-4 space-y-1.5">
              {identities.map((it) => (
                <li key={it.id} className="flex items-center gap-2 text-sm">
                  <span
                    className={`inline-block w-24 shrink-0 rounded-full border px-2 py-0.5 text-center text-[11px] ${
                      KIND_STYLE[it.kind] ?? KIND_STYLE.fingerprint
                    }`}
                    title={it.kind}
                  >
                    {lbl(IDENTITY_KIND_LABEL, it.kind)}
                  </span>
                  <span className="break-all font-mono text-xs text-slate-200">{it.value}</span>
                  <span className="ml-auto shrink-0 text-[11px] text-slate-600">{fmtDateTime(it.created_at)}</span>
                  {canWrite && (
                    <span className="shrink-0">
                      <DetachIdentityButton customerId={customer.id} identityId={it.id} />
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {canWrite ? (
            <AttachIdentityForm customerId={customer.id} />
          ) : (
            <p className="text-[11px] text-slate-400">当前账号只读：挂身份需要运营或主账号。</p>
          )}
        </Card>
      </div>

      <Card>
        <SectionTitle count={orders.length}>名下订单</SectionTitle>
        {orders.length === 0 ? (
          <p className="text-xs text-slate-500">
            暂无归属订单 —— 到 <Link href="/console/orders" className="text-crown-300 hover:underline">订单台账</Link>
            对未归属的行点「归属客户」。
          </p>
        ) : (
          <DataTable head={["来源单号", "产品", "档位", "金额", "状态", "联系方式", "创建时间"]}>
            {orders.map((o) => {
              const product = productDisplay({ product_id: o.product_id, sku_id: o.sku_id }, "—");
              const tier = tierLabel({ sku_id: o.sku_id, plan: o.plan, edition: o.edition, period: o.period });
              return (
              <tr key={o.id}>
                <Td className="font-mono text-xs text-slate-300">
                  {o.source_key}
                  {o.is_test === 1 && <TestBadge className="ml-1.5" />}
                </Td>
                <Td className="text-xs text-slate-200" title={product.title}>{product.text}</Td>
                <Td className="text-xs text-slate-300" title={tier.title}>{tier.text}</Td>
                <Td className="text-xs text-slate-200">{fmtAmount(o.amount, o.pay_amount, o.currency)}</Td>
                <Td>
                  <OrderStatusBadge status={o.status} />
                </Td>
                <Td className="text-xs text-slate-400">{o.contact || "—"}</Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(o.created_at)}</Td>
              </tr>
              );
            })}
          </DataTable>
        )}
      </Card>

      <Card>
        <SectionTitle count={licenses.length}>名下授权</SectionTitle>
        {licenses.length === 0 ? (
          <p className="text-xs text-slate-500">
            暂无归属授权 —— 到 <Link href="/console/licenses" className="text-crown-300 hover:underline">授权台账</Link>
            归属。
          </p>
        ) : (
          <DataTable head={["产品", "授权号", "档位", "席位", "到期", "状态"]}>
            {licenses.map((l) => {
              const product = productDisplay(
                { product_id: l.product_id, sku_id: l.sku_id },
                l.source_system
              );
              const tier = tierLabel({ sku_id: l.sku_id, plan: l.plan, edition: l.edition });
              return (
              <tr key={l.id}>
                <Td>
                  <span title={product.title} className="text-xs text-slate-200">{product.text}</span>
                  {l.is_test === 1 && <TestBadge className="ml-1.5" />}
                </Td>
                <Td className="font-mono text-xs text-slate-300">{l.source_key}</Td>
                <Td className="text-xs text-slate-300" title={tier.title}>{tier.text}</Td>
                <Td className="text-xs tabular-nums text-slate-400">{seatsLabel(l.seats, l.source_system)}</Td>
                <Td className="text-xs">
                  <ExpiryCell expiresAt={l.expires_at} />
                </Td>
                <Td>
                  <LicenseStatusBadge status={l.status} />
                </Td>
              </tr>
              );
            })}
          </DataTable>
        )}
      </Card>

      <Card>
        <SectionTitle count={personas.length}>名下人设</SectionTitle>
        {personas.length === 0 ? (
          <p className="text-xs text-slate-500">
            暂无归属人设 —— 到 <Link href="/console/personas" className="text-amber-300 hover:underline">人设注册表</Link>
            的详情页做「归属客户」。
          </p>
        ) : (
          <DataTable head={["人设", "来源", "槽位", "状态", "创建时间", ""]}>
            {personas.map((p) => (
              <tr key={p.id} className="hover:bg-ink-700/40">
                <Td>
                  <Link href={`/console/personas/${p.id}`} className="text-xs font-medium text-amber-300 hover:underline">
                    {p.display_name || "（未命名）"}
                  </Link>
                </Td>
                <Td>
                  <SystemBadge system={p.source_system} />
                  <span className="ml-2 font-mono text-[11px] text-slate-500">{p.source_key}</span>
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
        )}
      </Card>

      <Card className={opportunities.length > 0 ? "border-violet-500/40" : ""}>
        <SectionTitle count={opportunities.length}>该客户的跨售商机</SectionTitle>
        {opportunities.length === 0 ? (
          <p className="flex items-center gap-1.5 text-xs text-slate-500">
            <Sparkles className="h-3.5 w-3.5" />
            暂无商机信号 —— 名下人设点亮槽位、订单/授权入账后由规则引擎自动推导。
          </p>
        ) : (
          <>
            <DataTable head={["类型", "从 → 到", "理由", "优先级", "跟进"]}>
              {opportunities.map((o) => {
                const pri = opportunityPriority(o.signalValue);
                return (
                <tr key={o.oppKey}>
                  <Td>
                    <OpportunityKindBadge kind={o.kind} />
                  </Td>
                  <Td className="text-xs text-slate-300">
                    <span>{lbl(PRODUCT_LABEL, o.fromProduct)}</span>
                    <span className="mx-1.5 text-slate-500">→</span>
                    <span className="text-crown-300">{lbl(PRODUCT_LABEL, o.toProduct)}</span>
                  </Td>
                  <Td className="max-w-[360px] text-xs text-slate-400">
                    <span className="block truncate" title={o.reason}>{o.reason}</span>
                  </Td>
                  <Td>
                    <span title={`信号值 ${o.signalValue}`} className={`text-xs font-semibold ${pri.tone === "danger" ? "text-rose-300" : pri.tone === "warning" ? "text-amber-300" : "text-slate-300"}`}>
                      {pri.label}
                    </span>
                  </Td>
                  <Td>
                    <span className="inline-flex items-center gap-1.5">
                      <OpportunityLogBadge log={o.log} />
                      {canWrite && (
                        <OpportunityActions
                          oppKey={o.oppKey}
                          kind={o.kind}
                          customerId={o.customerId}
                          toProduct={o.toProduct}
                          log={o.log}
                        />
                      )}
                    </span>
                  </Td>
                </tr>
                );
              })}
            </DataTable>
            <p className="mt-2.5 text-[11px] leading-relaxed text-slate-500">
              商机由客户档案、订单和授权自动算出。「跟进」会下调优先级，「赢单 / 忽略」默认隐藏。
              {!canWrite && " 当前账号只读：标记需要运营或主账号。"}
            </p>
          </>
        )}
      </Card>

      <Card>
        <SectionTitle count={leads.length}>名下留资</SectionTitle>
        {leads.length === 0 ? (
          <p className="text-xs text-slate-500">
            暂无归属留资 —— 到 <Link href="/console/leads" className="text-amber-300 hover:underline">留资列表</Link>
            做客户归并（日常跟进仍在官网后台）。
          </p>
        ) : (
          <DataTable head={["来源键", "称呼", "联系方式", "意向", "状态", "最近活跃"]}>
            {leads.map((l) => (
              <tr key={l.source_key} className="hover:bg-ink-700/40">
                <Td className="font-mono text-xs text-slate-300">
                  {l.source_key}
                  {l.is_test === 1 && <TestBadge className="ml-1.5" />}
                </Td>
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
                <span className="shrink-0 rounded bg-ink-700 px-1.5 py-0.5 font-mono text-[10px] text-amber-300/80">
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
