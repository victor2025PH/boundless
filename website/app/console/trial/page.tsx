// /console/trial：试用领取台账 + 客服核销绑定码（加客服送 10 万字符的人工环节）。
//
// 客服在这里做的唯一一件事：把用户报来的绑定码贴进去，点核销。真凭证由厂商机
// （私钥离线）签发后回填，用户端后台轮询自动入账——客服不接触任何密钥。
// 台账与客户体系已打通：联系方式自动比对 identities 解析归属（核销时强信号自动
// 建档，见 /api/console/trial-redeem），并给出「试用→付费」转化读数。
import Link from "next/link";
import { Cloud, Gift, HeartPulse, KeyRound, Users } from "lucide-react";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { gatewayDayStats } from "@/lib/ai-gateway";
import { poolStats as tgPoolStats } from "@/lib/tg-cred-pool";
import { classifyStrongContact, normIdentityValue } from "@/lib/ledger";
import { claimStats, getClaim, listClaims } from "@/lib/trial-claim-store";
import { listReferrals, referralAggregate } from "@/lib/referral-store";
import { readTrialFunnel } from "@/lib/trial-funnel";
import { getCustomerById, identityCustomerMap, paidCustomerIdSet } from "../data";
import {
  Card,
  CustomerLink,
  DataTable,
  EmptyState,
  FilterSubmit,
  PageHeader,
  Pager,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../parts";
import { ReferralApproveButton, TrialRedeemPanel } from "./ui";
import { UnquarantineButton } from "../ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const DEFAULT_GIFT = Number(process.env.TRIAL_GIFT_CHARS || 100000);
const LIMIT = 50;
const STATUS_CHOICES = [
  { value: "", label: "全部状态" },
  { value: "pending", label: "待签发" },
  { value: "issued", label: "已激活" },
  { value: "rejected", label: "已驳回" },
] as const;

/** 心跳判读：厂商机是签发链上唯一的人工/单点环节，它停了整条链静默失效。 */
function fulfillerHealth(lastSeen: string, oldestPendingMin: number) {
  if (!lastSeen) {
    return { tone: "text-slate-500", label: "从未连接", hint: "履约脚本还没跑起来" };
  }
  const min = Math.floor((Date.now() - Date.parse(lastSeen)) / 60000);
  if (!Number.isFinite(min)) {
    return { tone: "text-slate-500", label: "未知", hint: "" };
  }
  const ago = min < 1 ? "刚刚" : min < 60 ? `${min} 分钟前` : `${Math.floor(min / 60)} 小时前`;
  // 阈值取 10 分钟：脚本默认 60s 一轮，连续十轮没来必然不是抖动。
  if (min >= 10) {
    return {
      tone: "text-red-400",
      label: `已失联 ${ago}`,
      hint: oldestPendingMin > 0 ? `有用户已等 ${oldestPendingMin} 分钟拿不到授权` : "暂无待办，但链路已断",
    };
  }
  return { tone: "text-emerald-400", label: `正常 · ${ago}`, hint: "" };
}

export default async function TrialPage({
  searchParams,
}: {
  searchParams: { q?: string; status?: string; offset?: string };
}) {
  // 核销是写操作（API 侧 admin+）：viewer 不渲染核销面板，页里外口径一致，
  // 不再出现「看得见表单、提交必 401」的裂缝。
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canRedeem = roleAtLeast(me.role, "admin");

  const [stats, rows, funnel, gw, refAgg, refFlagged] = await Promise.all([
    claimStats(),
    listClaims({ limit: 500 }),
    readTrialFunnel(7),
    gatewayDayStats().catch(() => null),
    referralAggregate().catch(() => null),
    listReferrals({ status: "flagged", limit: 50 }).catch(() => []),
  ]);
  // flagged 行的双方联系方式（≤50 次主键查询，人审要看得见「谁邀了谁」）
  const flaggedRows = await Promise.all(
    (refFlagged || []).map(async (r) => {
      const [inviter, invitee] = await Promise.all([
        getClaim(r.inviterClaimId),
        getClaim(r.inviteeClaimId),
      ]);
      return {
        ...r,
        inviterContact: inviter?.contact || "—",
        inviteeContact: invitee?.contact || "—",
        inviteeUsed: invitee?.usedChars || 0,
      };
    })
  );
  let tgPool: ReturnType<typeof tgPoolStats> | null = null;
  try {
    tgPool = tgPoolStats();
  } catch {
    tgPool = null;
  }

  const q = searchParams.q?.trim().toLowerCase() || undefined;
  const statusFilter = (["pending", "issued", "rejected"] as const).find(
    (s) => s === searchParams.status?.trim()
  );
  const offset = Math.max(0, Number(searchParams.offset) || 0);

  // 领取 → 客户主档解析：联系方式归一成 identities 同款键查映射，再叠成交客户
  // 集合即得「试用→付费」。全部只读；解析不到按未归并处理，不猜。
  const idMap = identityCustomerMap();
  const paidIds = paidCustomerIdSet();
  const resolved = rows.map((c) => {
    let customerId: string | null = null;
    for (const k of contactKeys(c.contact)) {
      const cid = idMap.get(k);
      if (cid) {
        customerId = cid;
        break;
      }
    }
    return { ...c, customerId, paid: !!customerId && paidIds.has(customerId) };
  });
  const converted = resolved.filter((c) => c.paid).length;

  const health = fulfillerHealth(stats.fulfillerLastSeen, stats.oldestPendingMin);
  const waiting = resolved.filter((c) => c.bindRedeemedAt && !c.topupVoucher).length;

  let recent = resolved.slice().reverse();
  if (statusFilter) recent = recent.filter((c) => c.status === statusFilter);
  if (q) {
    recent = recent.filter(
      (c) => c.contact.toLowerCase().includes(q) || (c.bindCode ?? "").toLowerCase().includes(q)
    );
  }
  const total = recent.length;
  const pageRows = recent.slice(offset, offset + LIMIT);

  // 本页行的客户显示名（≤LIMIT 次主键查询，与订单页同模式）
  const nameById = new Map<string, string | null>();
  for (const c of pageRows) {
    if (c.customerId && !nameById.has(c.customerId)) {
      nameById.set(c.customerId, getCustomerById(c.customerId)?.display_name ?? null);
    }
  }

  return (
    <div>
      <PageHeader
        title="试用与赠量"
        desc={
          <>
            <Gift className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            用户在客户端「注册领免费额度」（100 万字符 · 无期限）建单，厂商机离线签发后
            自动激活。客服在这里
            <span className="font-medium text-amber-300/90"> 核销绑定码</span>
            为用户追加赠送字符（数额可调）——凭证由厂商机签，客服不接触任何密钥。
          </>
        }
      />

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="总领取" value={stats.total} />
        <Stat label="待签发" value={stats.pending} tone={stats.pending > 0 ? "amber" : undefined} />
        <Stat label="已激活" value={stats.issued} />
        <Stat label="待发赠量" value={waiting} tone={waiting > 0 ? "amber" : undefined} />
        <Stat
          label="试用→付费"
          value={converted}
          tone={converted > 0 ? "emerald" : undefined}
          sub={stats.total > 0 ? `${Math.round((converted * 100) / stats.total)}%` : undefined}
        />
      </div>

      <Card className="mb-4 border-ink-700 bg-ink-900/40 !py-3">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <HeartPulse className={`h-4 w-4 ${health.tone}`} />
          <span className="text-slate-400">履约端（厂商机）：</span>
          <span className={`font-medium ${health.tone}`}>{health.label}</span>
          {health.hint && <span className="text-slate-500">— {health.hint}</span>}
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
          履约脚本每轮都会来取待办，所以「最后活跃」就是签发链的存活信号。失联时用户
          点了领取会一直停在「正在签发」——先去厂商机确认 fulfill_trial.py 还在跑。
        </p>
      </Card>

      {gw && (
        <Card className="mb-4 border-ink-700 bg-ink-900/40 !py-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <span className="inline-flex items-center gap-1.5">
              <Cloud className={`h-4 w-4 ${gw.enabled ? "text-emerald-400" : "text-slate-500"}`} />
              <span className="text-slate-400">AI 试用网关：</span>
              <span className={`font-medium ${gw.enabled ? "text-emerald-400" : "text-slate-500"}`}>
                {gw.enabled ? "运行中" : "未启用（缺 DEEPSEEK_API_KEY / AI_GATEWAY_SECRET）"}
              </span>
            </span>
            <span className="text-slate-400">
              今日 <span className="font-medium text-white">{gw.machines}</span> 台机器 ·{" "}
              <span className="font-medium text-white">{gw.chars.toLocaleString()}</span> 字符
            </span>
            <span className="text-slate-500">
              全局水位 {gw.global_budget > 0 ? Math.round((gw.chars * 100) / gw.global_budget) : 0}%
              （预算 {gw.global_budget.toLocaleString()} / 单机 {gw.machine_budget.toLocaleString()}）
            </span>
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
            设备令牌只发给本页台账里的指纹（领取试用=接入前置）；云 Key 只在服务端。
            全局水位到 100% 时所有试用机收到「通道繁忙」，不影响已购授权用户。
          </p>
        </Card>
      )}

      {tgPool && tgPool.enabled && (
        <Card className="mb-4 border-ink-700 bg-ink-900/40 !py-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <span className="inline-flex items-center gap-1.5">
              <KeyRound className="h-4 w-4 text-emerald-400" />
              <span className="text-slate-400">Telegram 凭据池：</span>
              <span className="font-medium text-white">
                {tgPool.total_used}/{tgPool.total_cap} 号位
              </span>
              {tgPool.quarantined_groups > 0 && (
                <span className="rounded bg-rose-500/15 px-1.5 py-0.5 text-[10px] font-medium text-rose-300">
                  {tgPool.quarantined_groups} 组已隔离
                </span>
              )}
            </span>
            {tgPool.groups.map((g) => (
              <span key={g.api_id_tail}
                className={`inline-flex items-center gap-1 ${g.quarantined ? "text-rose-300/90" : "text-slate-500"}`}>
                {g.name}(…{g.api_id_tail}) {g.used}/{g.max}
                {g.has_proxy && (
                  <span className="rounded bg-sky-500/15 px-1 text-[10px] text-sky-300" title="该组随凭据下发出口代理">出口</span>
                )}
                {g.quarantined && <UnquarantineButton apiId={g.api_id} name={g.name} />}
              </span>
            ))}
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
            公网用户登录 Telegram 时按机器指纹粘定分到一组集团 api_id（同机恒定），
            无需自己去 my.telegram.org 申请。某组占满前自动优先空闲组；全满则新用户回落自备凭据。
            被多台机器举报 API_ID_INVALID 的组会自动隔离停发（红标）——先跑 tg_cred_probe
            核实真伪：真废换池、误报解除。带「出口」的组优先派给直连不通的大陆机器。
          </p>
        </Card>
      )}

      {funnel.machines.welcome > 0 && (
        <Card className="mb-4 border-ink-700 bg-ink-900/40 !py-3">
          <div className="mb-2 text-[11px] text-slate-500">
            首启向导漏斗（近 {funnel.days} 天 · 按机器去重）
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <FunnelStep label="看到欢迎页" value={funnel.machines.welcome} />
            <FunnelArrow />
            <FunnelStep
              label="提交领取"
              value={funnel.machines.claim_submit}
              rate={funnel.rates.claimSubmit}
            />
            <FunnelArrow />
            <FunnelStep label="领取成功" value={funnel.machines.claim_ok} rate={funnel.rates.claimOk} />
            <FunnelArrow />
            <FunnelStep label="完成进入" value={funnel.machines.done} rate={funnel.rates.done} />
            <span className="ml-2 text-slate-600">
              跳过 {funnel.machines.claim_skip} · 打开客服码 {funnel.machines.gift_open}
            </span>
          </div>
        </Card>
      )}

      {refAgg && (refAgg.codes > 0 || refAgg.registered > 0) && (
        <Card className="mb-4 border-ink-700 bg-ink-900/40 !py-3">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] text-slate-500">
            <Users className="h-3.5 w-3.5" />
            邀请裂变（好友注册双方各得字符；被邀请方消耗达标才给邀请人发奖）
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <span className="text-slate-400">
              发码 <span className="font-medium text-white">{refAgg.codes}</span>
            </span>
            <span className="text-slate-400">
              归因注册 <span className="font-medium text-white">{refAgg.registered}</span>
            </span>
            <span className="text-slate-400">
              消耗达标 <span className="font-medium text-white">{refAgg.qualified}</span>
            </span>
            <span className="text-slate-400">
              见面礼已发 <span className="font-medium text-emerald-300">{refAgg.invitee_rewarded}</span>
            </span>
            <span className="text-slate-400">
              邀请奖已发 <span className="font-medium text-emerald-300">{refAgg.inviter_rewarded}</span>
            </span>
            <span className="text-slate-400">
              累计赠出 <span className="font-medium text-white">{refAgg.chars_granted.toLocaleString()}</span> 字符
            </span>
            {refAgg.flagged > 0 && (
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-300">
                {refAgg.flagged} 条待人审
              </span>
            )}
          </div>
          {flaggedRows.length > 0 && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wide text-slate-600">
                    <th className="py-1 pr-3">时间</th>
                    <th className="py-1 pr-3">邀请人</th>
                    <th className="py-1 pr-3">被邀请人</th>
                    <th className="py-1 pr-3">拦截原因</th>
                    <th className="py-1 pr-3">被邀方消耗</th>
                    <th className="py-1">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {flaggedRows.map((r) => (
                    <tr key={r.id} className="border-t border-ink-800">
                      <td className="py-1.5 pr-3 font-mono text-[11px] text-slate-500">
                        {fmtDateTime(r.createdAt)}
                      </td>
                      <td className="py-1.5 pr-3 text-slate-300">{r.inviterContact}</td>
                      <td className="py-1.5 pr-3 text-slate-300">{r.inviteeContact}</td>
                      <td className="py-1.5 pr-3 text-amber-300/90">
                        {r.flagReason === "ip_cluster" ? "同 IP 聚集" : r.flagReason || "—"}
                      </td>
                      <td className="py-1.5 pr-3 text-slate-400">
                        {(r.inviteeUsed || 0).toLocaleString()}
                      </td>
                      <td className="py-1.5">
                        {canRedeem ? <ReferralApproveButton id={r.id} /> : <span className="text-slate-600">需 admin</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 text-[10px] leading-relaxed text-slate-600">
                flagged = 防刷启发命中（同邀请人名下多台被邀请机器共用出口 IP）。宿舍/公司
                同网段装机是真实场景，确认后放行即可；放行后厂商机下一轮自动发奖。
              </p>
            </div>
          )}
        </Card>
      )}

      {canRedeem ? (
        <TrialRedeemPanel defaultChars={DEFAULT_GIFT} />
      ) : (
        <p className="mb-5 rounded-xl border border-ink-700 bg-ink-900/40 p-3 text-[11px] text-slate-500">
          viewer 只读：核销绑定码需 admin 及以上角色。
        </p>
      )}

      <form method="GET" className="mb-3 flex flex-wrap items-center gap-2">
        <select name="status" defaultValue={statusFilter ?? ""} className={filterInputCls}>
          {STATUS_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <input
          type="search"
          name="q"
          defaultValue={searchParams.q ?? ""}
          placeholder="搜索联系方式 / 绑定码"
          className={`${filterInputCls} w-56`}
        />
        <FilterSubmit />
        {(q || statusFilter) && (
          <Link href="/console/trial" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {pageRows.length === 0 ? (
        <EmptyState
          title={q || statusFilter ? "没有匹配的领取记录" : "还没有人领取试用"}
          hints={
            q || statusFilter
              ? ["调整搜索或状态筛选试试。"]
              : [<>用户在桌面端首启向导点「免费领取」后，这里会出现记录。</>]
          }
        />
      ) : (
        <>
          <DataTable head={["时间", "联系方式", "状态", "绑定码", "核销", "赠量", "客户"]}>
            {pageRows.map((c) => (
              <tr key={c.id} className="hover:bg-ink-700/40">
                <Td className="whitespace-nowrap font-mono text-xs text-slate-500">
                  {fmtDateTime(c.createdAt)}
                </Td>
                <Td className="text-xs text-slate-200">
                  {c.contact || <span className="text-slate-600">—</span>}
                  <span className="ml-1.5 text-[10px] text-slate-600">{c.contactKind}</span>
                </Td>
                <Td>
                  <StatusChip status={c.status} />
                </Td>
                <Td className="font-mono text-xs text-amber-300/80">{c.bindCode || "—"}</Td>
                <Td className="whitespace-nowrap text-xs text-slate-400">
                  {c.bindRedeemedAt ? fmtDateTime(c.bindRedeemedAt) : "—"}
                </Td>
                <Td className="whitespace-nowrap text-xs">
                  {c.topupVoucher ? (
                    <span className="text-emerald-400">
                      已发 {(c.bindChars || 0).toLocaleString()}
                    </span>
                  ) : c.bindRedeemedAt ? (
                    <span className="text-amber-300/90">待厂商机签发</span>
                  ) : (
                    <span className="text-slate-600">—</span>
                  )}
                </Td>
                <Td>
                  {c.customerId ? (
                    <span className="inline-flex items-center gap-1.5">
                      <CustomerLink customerId={c.customerId} label={nameById.get(c.customerId)} />
                      {c.paid && (
                        <span className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-300">
                          已付费
                        </span>
                      )}
                    </span>
                  ) : (
                    <span className="text-xs text-slate-600">—</span>
                  )}
                </Td>
              </tr>
            ))}
          </DataTable>
          <Pager
            basePath="/console/trial"
            params={{ q: searchParams.q?.trim() || undefined, status: statusFilter }}
            total={total}
            limit={LIMIT}
            offset={offset}
          />
          <p className="mt-3 text-[11px] text-slate-500">
            共 {total} 条匹配 · 客户列 = 联系方式与客户身份标识自动比对（核销时强信号自动建档）
            · 机器指纹不在此展示（用户报码即可核销，指纹属排障信息）。
          </p>
        </>
      )}
    </div>
  );
}

/** 领取联系方式 → identities 查询键（`kind|value`，与 ledger.normIdentityValue 同口径）。
 *  强信号（@handle / email / 电话）额外补 classifyStrongContact 的规范键——
 *  ensureCustomer* 建档时两种键都可能挂，这里对称展开才能都命中。 */
function contactKeys(contact: string): string[] {
  const t = (contact || "").trim();
  if (!t) return [];
  const keys = new Set<string>([`contact|${normIdentityValue("contact", t)}`]);
  const strong = classifyStrongContact(t);
  if (strong) keys.add(`${strong.kind}|${strong.value}`);
  return [...keys];
}

function FunnelStep({ label, value, rate }: { label: string; value: number; rate?: number }) {
  return (
    <span className="text-slate-300">
      {label} <span className="font-semibold text-slate-100">{value}</span>
      {rate != null && (
        <span className="ml-0.5 text-[10px] text-slate-500">({Math.round(rate * 100)}%)</span>
      )}
    </span>
  );
}

function FunnelArrow() {
  return <span className="text-slate-600">→</span>;
}

function Stat({
  label,
  value,
  tone,
  sub,
}: {
  label: string;
  value: number;
  tone?: "amber" | "emerald";
  sub?: string;
}) {
  return (
    <Card className="border-ink-700 bg-ink-900/40 !py-3">
      <div className="text-[11px] text-slate-500">{label}</div>
      <div
        className={`mt-1 text-2xl font-semibold ${
          tone === "amber" ? "text-amber-300" : tone === "emerald" ? "text-emerald-300" : "text-slate-100"
        }`}
      >
        {value}
        {sub && <span className="ml-1.5 text-xs font-normal text-slate-500">{sub}</span>}
      </div>
    </Card>
  );
}

function StatusChip({ status }: { status: string }) {
  const map: Record<string, string> = {
    pending: "bg-amber-500/15 text-amber-300",
    issued: "bg-emerald-500/15 text-emerald-400",
    rejected: "bg-red-500/15 text-red-400",
  };
  const zh: Record<string, string> = { pending: "待签发", issued: "已激活", rejected: "已驳回" };
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] ${map[status] || "bg-ink-700 text-slate-400"}`}>
      {zh[status] || status}
    </span>
  );
}
