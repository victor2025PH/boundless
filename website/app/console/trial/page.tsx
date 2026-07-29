// /console/trial：试用领取台账 + 客服核销绑定码（加客服送 10 万字符的人工环节）。
//
// 客服在这里做的唯一一件事：把用户报来的绑定码贴进去，点核销。真凭证由厂商机
// （私钥离线）签发后回填，用户端后台轮询自动入账——客服不接触任何密钥。
import { Cloud, Gift, HeartPulse, KeyRound } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import { gatewayDayStats } from "@/lib/ai-gateway";
import { poolStats as tgPoolStats } from "@/lib/tg-cred-pool";
import { claimStats, listClaims } from "@/lib/trial-claim-store";
import { readTrialFunnel } from "@/lib/trial-funnel";
import { Card, DataTable, EmptyState, PageHeader, Td, fmtDateTime } from "../parts";
import { TrialRedeemPanel } from "./ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const DEFAULT_GIFT = Number(process.env.TRIAL_GIFT_CHARS || 100000);

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

export default async function TrialPage() {
  if (!hasConsoleSession()) return null;

  const [stats, rows, funnel, gw] = await Promise.all([
    claimStats(),
    listClaims({ limit: 200 }),
    readTrialFunnel(7),
    gatewayDayStats().catch(() => null),
  ]);
  let tgPool: ReturnType<typeof tgPoolStats> | null = null;
  try {
    tgPool = tgPoolStats();
  } catch {
    tgPool = null;
  }
  const recent = rows.slice().reverse();
  const health = fulfillerHealth(stats.fulfillerLastSeen, stats.oldestPendingMin);
  const waiting = recent.filter((c) => c.bindRedeemedAt && !c.topupVoucher).length;

  return (
    <div>
      <PageHeader
        title="试用与赠量"
        desc={
          <>
            <Gift className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            用户在客户端「注册领 7 天」建单，厂商机离线签发后自动激活。客服在这里
            <span className="font-medium text-amber-300/90"> 核销绑定码</span>
            ，为用户追加赠送字符——凭证由厂商机签，客服不接触任何密钥。
          </>
        }
      />

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="总领取" value={stats.total} />
        <Stat label="待签发" value={stats.pending} tone={stats.pending > 0 ? "amber" : undefined} />
        <Stat label="已激活" value={stats.issued} />
        <Stat label="待发赠量" value={waiting} tone={waiting > 0 ? "amber" : undefined} />
      </div>

      <Card className="mb-4 border-slate-800 bg-slate-900/40 !py-3">
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
        <Card className="mb-4 border-slate-800 bg-slate-900/40 !py-3">
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
        <Card className="mb-4 border-slate-800 bg-slate-900/40 !py-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <span className="inline-flex items-center gap-1.5">
              <KeyRound className="h-4 w-4 text-emerald-400" />
              <span className="text-slate-400">Telegram 凭据池：</span>
              <span className="font-medium text-white">
                {tgPool.total_used}/{tgPool.total_cap} 号位
              </span>
            </span>
            {tgPool.groups.map((g) => (
              <span key={g.api_id_tail} className="text-slate-500">
                {g.name}(…{g.api_id_tail}) {g.used}/{g.max}
              </span>
            ))}
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
            公网用户登录 Telegram 时按机器指纹粘定分到一组集团 api_id（同机恒定），
            无需自己去 my.telegram.org 申请。某组占满前自动优先空闲组；全满则新用户回落自备凭据。
          </p>
        </Card>
      )}

      {funnel.machines.welcome > 0 && (
        <Card className="mb-4 border-slate-800 bg-slate-900/40 !py-3">
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

      <TrialRedeemPanel defaultChars={DEFAULT_GIFT} />

      {recent.length === 0 ? (
        <EmptyState
          title="还没有人领取试用"
          hints={[<>用户在桌面端首启向导点「免费领取」后，这里会出现记录。</>]}
        />
      ) : (
        <>
          <DataTable head={["时间", "联系方式", "状态", "绑定码", "核销", "赠量"]}>
            {recent.map((c) => (
              <tr key={c.id} className="hover:bg-slate-800/40">
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
              </tr>
            ))}
          </DataTable>
          <p className="mt-3 text-[11px] text-slate-500">
            共 {recent.length} 条 · 机器指纹不在此展示（用户报码即可核销，指纹属排障信息）。
          </p>
        </>
      )}
    </div>
  );
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

function Stat({ label, value, tone }: { label: string; value: number; tone?: "amber" }) {
  return (
    <Card className="border-slate-800 bg-slate-900/40 !py-3">
      <div className="text-[11px] text-slate-500">{label}</div>
      <div
        className={`mt-1 text-2xl font-semibold ${
          tone === "amber" ? "text-amber-300" : "text-slate-100"
        }`}
      >
        {value}
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
    <span className={`rounded px-1.5 py-0.5 text-[10px] ${map[status] || "bg-slate-800 text-slate-400"}`}>
      {zh[status] || status}
    </span>
  );
}
