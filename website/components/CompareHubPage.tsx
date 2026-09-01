"use client";

import Link from "next/link";
import { ArrowRight, Check, ChevronRight, Download, ShoppingCart } from "lucide-react";
import { useLang } from "./LanguageContext";
import Navbar from "./Navbar";
import Footer from "./Footer";
import { track } from "@/lib/track";
import { compareHub } from "@/lib/compare-content";

/** /compare 枢纽页壳（实施77 渠道四）：选型指南 + 工具速览 + 对比页入口。
 *  措辞纪律与 ComparePage 一致（lib/compare-content.ts 顶注）。
 *  实施78 P0-1：与 ComparePage 同批补 Navbar/Footer/面包屑/首屏 CTA（理由见该文件顶注）。 */
export default function CompareHubPage() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const pick = (c: { zh: string; en: string }) => (zh ? c.zh : c.en);
  const L = (p: string) => (zh ? p : `/en${p}`);

  return (
    <>
      <Navbar />
      <main className="relative min-h-screen px-5 pb-24 pt-32">
      <div className="mx-auto max-w-4xl">
        <nav aria-label={zh ? "面包屑" : "Breadcrumb"} className="flex items-center gap-1.5 text-sm text-slate-400">
          <Link href={L("/")} className="transition hover:text-white">
            {zh ? "首页" : "Home"}
          </Link>
          <ChevronRight className="h-3.5 w-3.5 text-slate-600" />
          <span className="text-slate-300">{zh ? "对比选型" : "Comparisons"}</span>
        </nav>

        <h1 className="mt-6 text-3xl font-bold tracking-tight text-white md:text-4xl">
          {pick(compareHub.title)}
        </h1>

        {/* GEO 答案胶囊 */}
        <div className="mt-6 rounded-2xl border border-neon-cyan/25 bg-neon-cyan/[0.06] px-5 py-4">
          <p className="text-sm font-medium leading-relaxed text-slate-200">{pick(compareHub.answer)}</p>
        </div>

        {/* 首屏 CTA（P0-1，与 ComparePage 同款克制处理） */}
        <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
          <a
            href={L("/download/chatx")}
            onClick={() => track("cta_click", { where: "compare_hub_hero_dl" })}
            className="inline-flex items-center gap-1.5 font-medium text-neon-cyan underline-offset-4 transition hover:underline"
          >
            <Download className="h-4 w-4" />
            {zh ? "免费下载智聊 ChatX" : "Download ChatX free"}
          </a>
          <a
            href={L("/pricing")}
            onClick={() => track("cta_click", { where: "compare_hub_hero_pricing" })}
            className="text-slate-400 underline-offset-4 transition hover:text-white hover:underline"
          >
            {zh ? "看定价 →" : "See pricing →"}
          </a>
        </div>

        <div className="mt-5 space-y-3">
          {(zh ? compareHub.intro.zh : compareHub.intro.en).map((p, i) => (
            <p key={i} className="text-sm leading-relaxed text-slate-400">
              {p}
            </p>
          ))}
        </div>

        {/* 五维选型框架 */}
        <h2 className="mt-12 text-xl font-semibold text-white">
          {zh ? "五维选型框架（按重要性排序）" : "The five-dimension framework (by importance)"}
        </h2>
        <div className="mt-4 overflow-x-auto rounded-2xl border border-white/10">
          <table className="w-full min-w-[560px] border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-white/10 bg-white/[0.04] text-xs uppercase tracking-wider text-slate-400">
                <th className="w-[24%] px-4 py-3 font-medium">{zh ? "维度" : "Dimension"}</th>
                <th className="px-4 py-3 font-medium">{zh ? "怎么自查" : "How to self-check"}</th>
              </tr>
            </thead>
            <tbody>
              {compareHub.framework.map((r, i) => (
                <tr key={i} className="border-b border-white/5 align-top last:border-b-0">
                  <td className="px-4 py-4 font-medium text-white">{pick(r.dim)}</td>
                  <td className="px-4 py-4 text-slate-300">{pick(r.check)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* 工具速览 + 对比页入口 */}
        <h2 className="mt-12 text-xl font-semibold text-white">
          {zh ? "五款工具速览" : "Five tools at a glance"}
        </h2>
        <div className="mt-4 overflow-x-auto rounded-2xl border border-white/10">
          <table className="w-full min-w-[640px] border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-white/10 bg-white/[0.04] text-xs uppercase tracking-wider text-slate-400">
                <th className="w-[16%] px-4 py-3 font-medium">{zh ? "工具" : "Tool"}</th>
                <th className="w-[46%] px-4 py-3 font-medium">{zh ? "一句话定位" : "Positioning"}</th>
                <th className="px-4 py-3 font-medium">{zh ? "适合谁" : "Best fit"}</th>
                <th className="w-[12%] px-4 py-3 font-medium">{zh ? "对比" : "Compare"}</th>
              </tr>
            </thead>
            <tbody>
              {compareHub.tools.map((t, i) => (
                <tr key={i} className="border-b border-white/5 align-top last:border-b-0">
                  <td className="px-4 py-4 font-medium text-white">
                    {/* P0-2：我方行中文页用 nameZh（智聊 ChatX），英文页只出拉丁名 */}
                    {t.compareHref ? (
                      t.name
                    ) : (
                      <span className="text-neon-cyan">{(zh && t.nameZh) || t.name}</span>
                    )}
                  </td>
                  <td className="px-4 py-4 text-slate-300">
                    {t.compareHref ? (
                      pick(t.positioning)
                    ) : (
                      <span className="flex gap-2">
                        <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                        <span>{pick(t.positioning)}</span>
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-4 text-slate-400">{pick(t.bestFor)}</td>
                  <td className="px-4 py-4">
                    {t.compareHref ? (
                      <Link
                        href={zh ? t.compareHref : `/en${t.compareHref}`}
                        onClick={() => track("cta_click", { where: `compare_hub_${t.compareHref!.split("/").pop()}` })}
                        className="inline-flex items-center gap-1 text-neon-cyan underline-offset-4 transition hover:underline"
                      >
                        {zh ? "详细对比" : "Details"}
                        <ArrowRight className="h-3.5 w-3.5" />
                      </Link>
                    ) : (
                      <span className="text-slate-600">{zh ? "本站" : "This site"}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="mt-4 text-xs leading-relaxed text-slate-600">{pick(compareHub.disclaimer)}</p>

        {/* FAQ（与服务端注入的 FAQPage JSON-LD 同源） */}
        <section className="mt-12">
          <h2 className="text-xl font-semibold text-white">
            {zh ? "常见问题" : "Frequently asked questions"}
          </h2>
          <div className="mt-4 space-y-6">
            {compareHub.faq.map((f, i) => (
              <div key={i}>
                <h3 className="text-sm font-semibold text-slate-200">{pick(f.q)}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-slate-400">{pick(f.a)}</p>
              </div>
            ))}
          </div>
        </section>

        <div className="mt-10 flex flex-wrap items-center gap-4">
          <a
            href={zh ? "/download/chatx" : "/en/download/chatx"}
            onClick={() => track("cta_click", { where: "compare_hub_trial" })}
            className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 text-sm font-medium text-ink-950 transition hover:opacity-90"
          >
            <Download className="h-4 w-4" />
            {zh ? "免费下载智聊（标准翻译不限量）" : "Download ChatX free"}
          </a>
          <a
            href={zh ? "/pricing" : "/en/pricing"}
            onClick={() => track("cta_click", { where: "compare_hub_pricing" })}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
          >
            <ShoppingCart className="h-4 w-4" />
            {zh ? "查看定价（免费开始 + 按量充值）" : "Pricing (free start + pay-as-you-go)"}
          </a>
        </div>
      </div>
      </main>
      <Footer />
    </>
  );
}
