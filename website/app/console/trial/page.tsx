// /console/trial：试用领取台账 + 客服核销绑定码（加客服送 10 万字符的人工环节）。
//
// 客服在这里做的唯一一件事：把用户报来的绑定码贴进去，点核销。真凭证由厂商机
// （私钥离线）签发后回填，用户端后台轮询自动入账——客服不接触任何密钥。
import { Gift, HeartPulse } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import { claimStats, listClaims } from "@/lib/trial-claim-store";
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

  const [stats, rows] = await Promise.all([claimStats(), listClaims({ limit: 200 })]);
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
