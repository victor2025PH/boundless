"use client";

import { useEffect, useState, type JSX } from "react";
import { BarChart3 } from "lucide-react";
import type { ChatxDailyReport, DailyFeatures, DailySrcRow, Insight } from "@/lib/chatx-report";
import { personaZh } from "@/lib/chatx-persona";
import { fmtDur } from "@/lib/fmt-dur";

/**
 * ChatX 广告日报卡（/admin 下载 tab）：日 × 来源的进人 / 落地 / 点击 / 下载人、对话、互动时长、IP、推送回话 + 投放诊断。
 * 数据来自 /api/admin/chatx-daily（lib/chatx-report.ts），与每日推给管理员的 Telegram 日报同源。
 */
export default function ChatxDailyCard() {
  const [days, setDays] = useState(7);
  const [data, setData] = useState<ChatxDailyReport | null>(null);
  const [err, setErr] = useState("");
  const [view, setView] = useState<"day" | "src">("day");
  const [openDay, setOpenDay] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setErr("");
    fetch(`/api/admin/chatx-daily?days=${days}`, { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as ChatxDailyReport;
      })
      .then((d) => {
        if (!alive) return;
        setData(d);
        setOpenDay((cur) => cur ?? d.totals.find((t) => t.starts || t.landUsers || t.msgs)?.day ?? d.totals[0]?.day ?? null);
      })
      .catch((e: unknown) => alive && setErr(e instanceof Error ? e.message : "load failed"));
    return () => {
      alive = false;
    };
  }, [days]);

  const pct = (a: number, b: number) => (b > 0 ? `${Math.round((a / b) * 100)}%` : "–");
  const levelCls: Record<Insight["level"], string> = {
    good: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
    warn: "border-amber-500/40 bg-amber-500/10 text-amber-200",
    bad: "border-rose-500/40 bg-rose-500/10 text-rose-200",
    info: "border-slate-700 bg-slate-800/60 text-slate-300",
  };

  const Row = ({ r, label }: { r: Omit<DailySrcRow, "day" | "src">; label: string }) => (
    <tr className="border-t border-slate-800/60 text-slate-300">
      <td className="py-1 font-mono">{label}</td>
      <td className="py-1 text-right">
        {r.users}
        {r.newUsers !== r.users && <span className="text-slate-500"> /{r.newUsers}新</span>}
      </td>
      <td className="py-1 text-right text-violet-300">{r.landUsers}</td>
      <td className="py-1 text-right text-emerald-300">{r.clickUsers}</td>
      <td className="py-1 text-right font-medium text-rose-300">{r.dlUsers}</td>
      <td className="py-1 text-right text-slate-400">{pct(r.dlUsers, r.users)}</td>
      <td className="py-1 text-right text-cyan-300">
        {r.chatUsers}
        <span className="text-slate-500">/{r.msgs}</span>
        {r.voiceMsgs > 0 && <span className="text-slate-500" title="其中语音">🎙{r.voiceMsgs}</span>}
      </td>
      <td className="py-1 text-right text-slate-400" title={`${r.sessions} 段互动，合计 ${fmtDur(r.durSec)}`}>
        {r.sessions ? fmtDur(r.avgDurSec) : "–"}
      </td>
      <td className="py-1 text-right text-slate-400" title={r.topIps.map((x) => `${x.ip} ×${x.n}`).join("\n")}>
        {r.ips}
        {r.topIps[0] && r.topIps[0].n >= 3 && <span className="text-amber-300" title={`${r.topIps[0].ip} 出现 ${r.topIps[0].n} 次`}> ⚠</span>}
      </td>
      <td className="py-1 text-right text-slate-400">{r.leads || "–"}</td>
      <td className="py-1 text-right text-slate-400">{r.pushSent ? `${r.pushReplied}/${r.pushSent}` : "–"}</td>
    </tr>
  );

  const Head = ({ first }: { first: string }) => (
    <thead>
      <tr className="text-left text-slate-500">
        <th className="py-1 font-normal">{first}</th>
        <th className="py-1 text-right font-normal" title="进 bot 人数（去重）/ 其中首次">进人</th>
        <th className="py-1 text-right font-normal" title="到落地页人数">落地</th>
        <th className="py-1 text-right font-normal" title="点了下载按钮的人数">点击</th>
        <th className="py-1 text-right font-normal" title="真实请求安装包的人数">下载人</th>
        <th className="py-1 text-right font-normal" title="下载人 / 进人">人转化</th>
        <th className="py-1 text-right font-normal" title="聊过的人数 / 用户消息条数（按钮和命令不算）">对话</th>
        <th className="py-1 text-right font-normal" title="平均每段互动时长（同一人 30 分钟内的连续行为算一段）">时长</th>
        <th className="py-1 text-right font-normal" title="落地 / 安装包请求的去重来源 IP 数；⚠ = 同一 IP ≥3 次">IP</th>
        <th className="py-1 text-right font-normal">留资</th>
        <th className="py-1 text-right font-normal" title="主动推送：24h 内回话人数 / 发出条数">推送</th>
      </tr>
    </thead>
  );

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-400">
        <span className="flex items-center gap-1.5">
          <BarChart3 className="h-3.5 w-3.5 text-cyan-300" />
          ChatX 广告日报（按人去重 · 每天 09:00 同一份推给管理员）
        </span>
        <span className="flex items-center gap-1">
          {[7, 14, 30].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`rounded px-2 py-0.5 ${days === d ? "bg-cyan-500 text-slate-950" : "bg-slate-800 text-slate-400 hover:text-slate-200"}`}
            >
              {d}天
            </button>
          ))}
          <span className="mx-1 text-slate-700">|</span>
          {(["day", "src"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`rounded px-2 py-0.5 ${view === v ? "bg-slate-200 text-slate-950" : "bg-slate-800 text-slate-400 hover:text-slate-200"}`}
            >
              {v === "day" ? "按天" : "按来源"}
            </button>
          ))}
        </span>
      </div>

      {err && <div className="text-[11px] text-rose-300">加载失败：{err}</div>}
      {!data && !err && <div className="text-[11px] text-slate-500">加载中…</div>}

      {data && (
        <>
          {data.insights.length > 0 && (
            <div className="mb-3 grid gap-1.5 md:grid-cols-2">
              {data.insights.map((i, k) => (
                <div key={k} className={`rounded-lg border px-2.5 py-1.5 text-[11px] leading-relaxed ${levelCls[i.level]}`}>
                  {i.text}
                </div>
              ))}
            </div>
          )}

          {view === "day" ? (
            <table className="w-full text-[11px]">
              <Head first="日期" />
              <tbody>
                {data.totals.map((t) => (
                  <DayRows key={t.day} t={t} rows={data.rows.filter((r) => r.day === t.day)} open={openDay === t.day} onToggle={() => setOpenDay(openDay === t.day ? null : t.day)} Row={Row} />
                ))}
              </tbody>
            </table>
          ) : (
            <table className="w-full text-[11px]">
              <Head first="来源" />
              <tbody>
                {data.bySrc.map((s) => (
                  <Row key={s.src} r={s} label={`${s.src}${s.days > 1 ? ` (${s.days}d)` : ""}`} />
                ))}
                {data.bySrc.length === 0 && (
                  <tr>
                    <td colSpan={11} className="py-3 text-center text-slate-500">
                      窗口内没有 ChatX 数据
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}

          <FeatureTable features={data.features} />
        </>
      )}
    </div>
  );
}

const sum = (m: Record<string, number>) => Object.values(m).reduce((a, b) => a + b, 0);
const failText = (m: Record<string, number>) =>
  Object.entries(m)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${k} ${n}`)
    .join(" / ");

/** 语音 / 人设 / 推送的日功能面（不分来源）；窗口内全零就不渲染。 */
function FeatureTable({ features }: { features: DailyFeatures[] }) {
  const used = features.filter((f) => f.voiceIn || f.voiceOut || sum(f.personaMsgs) || f.pushFail || f.cloneStart || sum(f.voicePicks ?? {}));
  if (!used.length) return null;
  const personas = Array.from(new Set(used.flatMap((f) => Object.keys(f.personaMsgs))));
  const rate = (fail: number, total: number) => (total ? `${Math.round((fail / total) * 100)}%` : "–");
  return (
    <div className="mt-3 border-t border-slate-800 pt-2">
      <div className="mb-1 text-[11px] text-slate-500">语音 / 人设 / 推送（不分来源；失败原因悬停可看）</div>
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-slate-500">
            <th className="py-1 font-normal">日期</th>
            <th className="py-1 text-right font-normal" title="用户发来的语音：转写成功 / 总条数（人数）">🎙 收到</th>
            <th className="py-1 text-right font-normal" title="bot 回的语音：发出成功 / 尝试次数（人数）">🔊 回出</th>
            <th className="py-1 text-right font-normal" title="语音回复失败率；悬停看原因（tts_failed=中继，user_quota/global_quota/cooldown=配额挡住）">失败率</th>
            <th className="py-1 text-right font-normal" title="成功发出的语音累计时长">时长</th>
            <th className="py-1 text-right font-normal" title="/voices 选音色次数；悬停看选了哪些">🎧 选音色</th>
            <th className="py-1 text-right font-normal" title="克隆自己的声音：成功 / 开始（同意数）；悬停看拒收原因和来源">🧬 克隆</th>
            {personas.map((p) => (
              <th key={p} className="py-1 text-right font-normal" title={`${personaZh(p)} 人设下：句数 / 人数；↑ 当天切到该人设的次数`}>
                🎭 {personaZh(p)}
              </th>
            ))}
            <th className="py-1 text-right font-normal" title="主动推送发送失败条数（拉黑 / 停用，已自动退订）">推送失败</th>
          </tr>
        </thead>
        <tbody>
          {features.map((f) => {
            const empty = !f.voiceIn && !f.voiceOut && !sum(f.personaMsgs) && !f.pushFail && !f.cloneStart && !sum(f.voicePicks ?? {});
            const outFail = f.voiceOut - f.voiceOutOk;
            const inFail = f.voiceIn - f.voiceInOk;
            return (
              <tr key={f.day} className={`border-t border-slate-800/60 ${empty ? "text-slate-600" : "text-slate-300"}`}>
                <td className="py-1 font-mono">{f.day.slice(5)}</td>
                <td className="py-1 text-right" title={inFail ? failText(f.voiceInFail) : undefined}>
                  {f.voiceIn ? (
                    <>
                      <span className={inFail ? "text-amber-300" : ""}>{f.voiceInOk}</span>
                      <span className="text-slate-500">/{f.voiceIn}</span> <span className="text-slate-500">({f.voiceInUsers}人)</span>
                    </>
                  ) : (
                    "–"
                  )}
                </td>
                <td className="py-1 text-right" title={outFail ? failText(f.voiceOutFail) : undefined}>
                  {f.voiceOut ? (
                    <>
                      <span className="text-cyan-300">{f.voiceOutOk}</span>
                      <span className="text-slate-500">/{f.voiceOut}</span> <span className="text-slate-500">({f.voiceOutUsers}人)</span>
                    </>
                  ) : (
                    "–"
                  )}
                </td>
                <td className={`py-1 text-right ${outFail && outFail / f.voiceOut >= 0.2 ? "text-rose-300" : outFail ? "text-amber-300" : "text-slate-400"}`} title={outFail ? failText(f.voiceOutFail) : undefined}>
                  {f.voiceOut ? rate(outFail, f.voiceOut) : "–"}
                </td>
                <td className="py-1 text-right text-slate-400">{f.voiceOutSec ? fmtDur(Math.round(f.voiceOutSec)) : "–"}</td>
                <td className="py-1 text-right" title={sum(f.voicePicks ?? {}) ? failText(f.voicePicks) : undefined}>
                  {sum(f.voicePicks ?? {}) || "–"}
                </td>
                <td className="py-1 text-right" title={f.cloneStart ? `拒收：${failText(f.cloneFail ?? {}) || "无"}；成功来源：${failText(f.cloneOkBySrc ?? {}) || "无"}` : undefined}>
                  {f.cloneStart ? (
                    <>
                      <span className="text-emerald-300">{f.cloneOk}</span>
                      <span className="text-slate-500">/{f.cloneStart}</span> <span className="text-slate-500">({f.cloneConsent}同意)</span>
                    </>
                  ) : (
                    "–"
                  )}
                </td>
                {personas.map((p) => (
                  <td key={p} className="py-1 text-right">
                    {f.personaMsgs[p] ? (
                      <>
                        {f.personaMsgs[p]}
                        <span className="text-slate-500">/{f.personaUsers[p] ?? 0}人</span>
                        {f.personaSwitches[p] ? <span className="text-violet-300" title="当天切到该人设次数"> ↑{f.personaSwitches[p]}</span> : null}
                      </>
                    ) : f.personaSwitches[p] ? (
                      <span className="text-violet-300">↑{f.personaSwitches[p]}</span>
                    ) : (
                      "–"
                    )}
                  </td>
                ))}
                <td className="py-1 text-right text-slate-400">{f.pushFail || "–"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DayRows({
  t,
  rows,
  open,
  onToggle,
  Row,
}: {
  t: ChatxDailyReport["totals"][number];
  rows: DailySrcRow[];
  open: boolean;
  onToggle: () => void;
  Row: (p: { r: Omit<DailySrcRow, "day" | "src">; label: string }) => JSX.Element;
}) {
  const empty = !t.starts && !t.landUsers && !t.dlUsers && !t.msgs;
  return (
    <>
      <tr onClick={onToggle} className={`cursor-pointer border-t border-slate-800 ${empty ? "text-slate-600" : "text-slate-200"} hover:bg-slate-900/60`}>
        <td className="py-1 font-mono">
          <span className="mr-1 inline-block w-3 text-slate-500">{rows.length ? (open ? "▾" : "▸") : ""}</span>
          {t.day.slice(5)}
        </td>
        <td className="py-1 text-right">
          {t.users}
          {t.newUsers !== t.users && <span className="text-slate-500"> /{t.newUsers}新</span>}
        </td>
        <td className="py-1 text-right text-violet-300">{t.landUsers}</td>
        <td className="py-1 text-right text-emerald-300">{t.clickUsers}</td>
        <td className="py-1 text-right font-medium text-rose-300">{t.dlUsers}</td>
        <td className="py-1 text-right text-slate-400">{t.users ? `${Math.round((t.dlUsers / t.users) * 100)}%` : "–"}</td>
        <td className="py-1 text-right text-cyan-300">
          {t.chatUsers}
          <span className="text-slate-500">/{t.msgs}</span>
        </td>
        <td className="py-1 text-right text-slate-400">{t.sessions ? fmtDur(t.avgDurSec) : "–"}</td>
        <td className="py-1 text-right text-slate-400">{t.ips}</td>
        <td className="py-1 text-right text-slate-400">{t.leads || "–"}</td>
        <td className="py-1 text-right text-slate-400">{t.pushSent ? `${t.pushReplied}/${t.pushSent}` : "–"}</td>
      </tr>
      {open && rows.map((r) => <Row key={`${r.day}/${r.src}`} r={r} label={`  ${r.src}`} />)}
    </>
  );
}
