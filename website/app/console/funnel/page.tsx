// /console/funnel：激活漏斗（装机 → 领试用 → 派发凭据 → 首账号在线 → 首条出站）
// + 48h 同期群激活率 + 挽回名单（P2-⑪）。
// 回答「新装用户卡在哪一步」——2026-08-10 API_ID_INVALID 事故中这个问题无数据可答。
// 口径与诚实声明见 lib/activation-funnel.ts。
import { Filter, PhoneOutgoing } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import {
  activationFunnel, cohortActivation, winbackList, type FunnelWindow,
} from "@/lib/activation-funnel";
import { trialPaidFunnel, type TrialPaidWindow } from "@/lib/trial-paid-funnel";
import { newbieFunnel, type NewbieFunnelWindow } from "@/lib/newbie-funnel";
import { Card, DataTable, EmptyState, PageHeader, SectionTitle, Td, fmtDateTime } from "../parts";
import { WinbackContactButton, WinbackCopyButton } from "../ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const STAGES: Array<{ key: keyof Pick<FunnelWindow,
  "installed" | "claimed" | "dispatched" | "account_online" | "first_reply">;
  label: string; hint: string }> = [
  { key: "installed", label: "开机（装了在跑）", hint: "beacon boot 心跳，按机器指纹去重" },
  { key: "claimed", label: "领了试用", hint: "trial-claims 台账（留了联系方式）" },
  { key: "dispatched", label: "派发了 TG 凭据", hint: "tg_cred_assign 粘定表" },
  { key: "account_online", label: "首个账号接入", hint: "客户端 milestone 回传（0.2.7+ / 1.0.20+）" },
  { key: "first_reply", label: "发出首条消息", hint: "客户端 milestone 回传（10 分钟新鲜闸防历史导入）" },
];

function FunnelBlock({ w }: { w: FunnelWindow }) {
  const base = Math.max(1, w.installed);
  return (
    <Card>
      <SectionTitle>近 {w.days} 天</SectionTitle>
      <div className="mt-3 space-y-2">
        {STAGES.map((s) => {
          const n = w[s.key];
          const pct = Math.min(100, Math.round((n / base) * 100));
          return (
            <div key={s.key}>
              <div className="flex items-baseline justify-between text-[12px]">
                <span className="text-slate-300">{s.label}</span>
                <span className="tabular-nums text-white">
                  {n}
                  {s.key !== "installed" && w.installed > 0 && (
                    <span className="ml-1 text-[11px] text-slate-500">
                      {Math.round((n / base) * 1000) / 10}%
                    </span>
                  )}
                </span>
              </div>
              <div className="mt-1 h-2 rounded bg-ink-900">
                <div
                  className="h-2 rounded bg-amber-400/70"
                  style={{ width: `${n > 0 ? Math.max(pct, 2) : 0}%` }}
                />
              </div>
              <div className="mt-0.5 text-[10.5px] text-slate-600">{s.hint}</div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

export default async function FunnelPage() {
  if (!hasConsoleSession()) return null;
  const [w7, w30, cohort, winback, p7, p30, n7, n30] = await Promise.all([
    activationFunnel(7), activationFunnel(30), cohortActivation(7), winbackList(7),
    trialPaidFunnel(7), trialPaidFunnel(30),
    newbieFunnel(7), newbieFunnel(30),
  ]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="激活漏斗"
        desc={
          <>
            <Filter className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            装机 → 领试用 → 派发凭据 → <span className="font-medium text-amber-300/90">首账号接入 → 首条出站</span>。
            活跃口径（窗口内发生即计入）；后两段依赖 1.0.20+ 客户端回传，
            旧版机器只出现在前三段。
          </>
        }
      />
      {!w30.logs_present ? (
        <EmptyState
          title="还没有客户端回传数据"
          hints={[
            <>client-logs.jsonl 尚不存在——还没有桌面端联网启动过，或 beacon 链未通。</>,
            <>装机后启动一次即产生 boot 心跳；接入账号 / 发消息里程碑需 1.0.20+ 客户端。</>,
          ]}
        />
      ) : (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            <FunnelBlock w={w7} />
            <FunnelBlock w={w30} />
          </div>

          {/* P1-4（2026-08-18）：商业段——「试用→付费 ≥30%」的读数面。
              cohort 口径：窗口内领试用，转化看截至现在（转化天然滞后于领取）；
              付费真相=集团库 orders.paid_at（licenses 表刻意不当付费证据，
              只经 identities 参与身份归并 join）。 */}
          <div className="grid gap-4 lg:grid-cols-2">
            {[p7, p30].map((p: TrialPaidWindow) => (
              <Card key={p.days}>
                <SectionTitle>💰 试用→付费（近 {p.days} 天领取的同期群）</SectionTitle>
                {p.claimed > 0 ? (
                  <div className="mt-2 text-sm text-slate-300">
                    <span className={`text-xl font-bold tabular-nums ${
                      (p.rate ?? 0) >= 30 ? "text-emerald-300" : "text-white"}`}>
                      {p.rate ?? 0}%
                    </span>
                    <span className="ml-2 text-slate-500">
                      {p.paid}/{p.claimed} 已转化（目标 ≥30%）
                    </span>
                    <div className="mt-1 text-[11px] text-slate-600">
                      按指纹 {p.matched.fingerprint} · 按联系方式 {p.matched.contact} ·
                      经身份归并 {p.matched.identity}
                      {!p.ledger_present && " · ⚠ 集团库不可读，本读数不可信"}
                    </div>
                  </div>
                ) : (
                  <div className="mt-2 text-[12px] text-slate-500">
                    窗口内暂无试用领取样本。
                  </div>
                )}
              </Card>
            ))}
          </div>

          {/* 实施50 P1：新人 6U 海报漏斗——曝光/点击是官网口径（桌面端弹窗曝光在引擎
              ui-event-trend poster6u_ 前缀），订单/已付是全渠道口径（订单台账全域唯一）。 */}
          <div className="grid gap-4 lg:grid-cols-2">
            {[n7, n30].map((n: NewbieFunnelWindow) => (
              <Card key={n.days}>
                <SectionTitle>🎁 新人 6U 海报（近 {n.days} 天）</SectionTitle>
                <div className="mt-2 flex flex-wrap items-baseline gap-x-5 gap-y-1 text-sm text-slate-300">
                  <span>官网曝光 <b className="tabular-nums text-white">{n.poster_views}</b></span>
                  <span>点击 <b className="tabular-nums text-white">{n.poster_clicks}</b></span>
                  <span>下单 <b className="tabular-nums text-white">{n.orders_created}</b></span>
                  <span>已付 <b className="tabular-nums text-emerald-300">{n.orders_paid}</b></span>
                  {n.click_to_paid !== null && (
                    <span className="text-slate-500">点击→已付 {n.click_to_paid}%</span>
                  )}
                </div>
                <div className="mt-1 text-[10.5px] text-slate-600">
                  曝光/点击=官网海报；下单/已付=全渠道（含桌面弹窗引流，剔 e2e 测试单）。
                  桌面端曝光看引擎 ui-event-trend（poster6u_ 前缀）。
                </div>
              </Card>
            ))}
          </div>

          <Card>
            <SectionTitle>48 小时同期群激活率（近 7 天领试用、已满观察期、新版客户端）</SectionTitle>
            {cohort.evaluated ? (
              <div className="mt-2 text-sm text-slate-300">
                <span className="text-xl font-bold text-white tabular-nums">{cohort.pct}%</span>
                <span className="ml-2 text-slate-500">
                  {cohort.activated_48h}/{cohort.cohort} 在领取后 48h 内接入了首个账号
                </span>
              </div>
            ) : (
              <div className="mt-2 text-[12px] text-slate-500">
                暂无可判样本（需要 1.0.20+ 客户端领试用且满 48h 观察期）。
              </div>
            )}
          </Card>

          <Card>
            <div className="flex items-center justify-between">
              <SectionTitle>
                <PhoneOutgoing className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
                挽回名单（近 7 天领了试用、未接入账号）
              </SectionTitle>
              {winback.length > 0 && <WinbackCopyButton rows={winback} />}
            </div>
            <p className="mt-1 text-[11px] text-slate-500">
              联系方式来自试用台账。「旧版」机器判不出接入态（无里程碑回传），外呼前先请对方升级/确认。
              标记「已联系」避免重复外呼（不影响名单去留——接入后自动移出）。
            </p>
            {!winback.length ? (
              <div className="mt-2 text-[12px] text-slate-500">窗口内没有滞留用户——都接入了，或没人领试用。</div>
            ) : (
              <div className="mt-2">
                <DataTable head={["领取时间", "联系方式", "指纹", "版本", "派发凭据", "跟进"]}>
                  {winback.map((r) => (
                    <tr key={r.fp} className="border-t border-ink-800">
                      <Td>{fmtDateTime(r.claimedAt)}</Td>
                      <Td>
                        <span className="text-white">{r.contact}</span>
                        <span className="ml-1 text-[10px] text-slate-500">{r.contactKind}</span>
                      </Td>
                      <Td><span className="font-mono text-[11px]">{r.fp}</span></Td>
                      <Td>{r.ver || "?"}{!r.capable && (
                        <span className="ml-1 rounded bg-ink-800 px-1 text-[10px] text-slate-400">旧版</span>
                      )}</Td>
                      <Td>{r.dispatched ? "✓" : "—"}</Td>
                      <Td>
                        <div className="flex items-center gap-2">
                          <WinbackContactButton fp={r.fp} contacted={!!r.contactedAt} />
                          {r.contactedAt && (
                            <span className="text-[10px] text-slate-500" title={`${r.contactedBy || ""} @ ${fmtDateTime(r.contactedAt)}`}>
                              {r.contactedBy || "已联系"}
                            </span>
                          )}
                        </div>
                      </Td>
                    </tr>
                  ))}
                </DataTable>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
