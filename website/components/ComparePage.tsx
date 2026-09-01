"use client";

import Link from "next/link";
import { Check, ChevronRight, Download, ShoppingCart } from "lucide-react";
import { useLang } from "./LanguageContext";
import Navbar from "./Navbar";
import Footer from "./Footer";
import { track } from "@/lib/track";
import { compareSpecs, type CompareSpec } from "@/lib/compare-content";

/** 竞品对比页共享壳（WS-1 2026-08-17）。
 *  措辞纪律见 lib/compare-content.ts 顶注：我方列=已交付实况，竞品列=其官网公开
 *  资料的一般性理解 + 全文免责。布局对齐 LegalShell 的窄栏阅读风格。
 *
 *  实施78 P0-1（2026-08-28）补齐「最小转化件」——这些页是 AI 引用我们的主要入口，
 *  此前只有一句「← 返回首页」：没有导航壳、没有 logo、首屏没有 CTA（CTA 全在对比表与
 *  FAQ 之后）。从 AI 答案直达的陌生读者多数只看首屏，看完无处可去就是纯漏斗。故补：
 *  Navbar/Footer（知道这是谁的站、能去别处）+ 面包屑（回枢纽页）+ 首屏轻量 CTA
 *  + 页尾「相关对比」互链（既服务读者，也给站内链接结构）。 */
export default function ComparePage({ spec }: { spec: CompareSpec }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const pick = (c: { zh: string; en: string }) => (zh ? c.zh : c.en);
  const L = (p: string) => (zh ? p : `/en${p}`);
  // 相关对比＝除本页外的全部对比页，顺序取注册表原序（lib/compare-content.ts 单一事实源）
  const others = Object.values(compareSpecs).filter((s) => s.slug !== spec.slug);

  return (
    <>
      <Navbar />
      <main className="relative min-h-screen px-5 pb-24 pt-32">
      <div className="mx-auto max-w-4xl">
        {/* 面包屑：AI 直达子页的读者靠它回到枢纽页横向比较，比单向「返回首页」有用 */}
        <nav aria-label={zh ? "面包屑" : "Breadcrumb"} className="flex items-center gap-1.5 text-sm text-slate-400">
          <Link href={L("/")} className="transition hover:text-white">
            {zh ? "首页" : "Home"}
          </Link>
          <ChevronRight className="h-3.5 w-3.5 text-slate-600" />
          <Link href={L("/compare")} className="transition hover:text-white">
            {zh ? "对比选型" : "Comparisons"}
          </Link>
          <ChevronRight className="h-3.5 w-3.5 text-slate-600" />
          <span className="text-slate-300">{spec.name}</span>
        </nav>

        <h1 className="mt-6 text-3xl font-bold tracking-tight text-white md:text-4xl">
          {pick(spec.tagline)}
        </h1>

        {/* GEO 答案胶囊（实施77 渠道四）：开篇直答供 AI 引擎整段引用 */}
        <div className="mt-6 rounded-2xl border border-neon-cyan/25 bg-neon-cyan/[0.06] px-5 py-4">
          <p className="text-sm font-medium leading-relaxed text-slate-200">{pick(spec.answer)}</p>
        </div>

        {/* 首屏 CTA（P0-1）：胶囊读完就是决策点，此处给轻量入口；重量级 CTA 仍在页尾。
            刻意用文字链而非大按钮——对比页的可信度来自克制，首屏塞大按钮会像软文。 */}
        <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
          <a
            href={L("/download/chatx")}
            onClick={() => track("cta_click", { where: `compare_${spec.slug}_hero_dl` })}
            className="inline-flex items-center gap-1.5 font-medium text-neon-cyan underline-offset-4 transition hover:underline"
          >
            <Download className="h-4 w-4" />
            {zh ? "免费下载智聊 ChatX" : "Download ChatX free"}
          </a>
          <a
            href={L("/pricing")}
            onClick={() => track("cta_click", { where: `compare_${spec.slug}_hero_pricing` })}
            className="text-slate-400 underline-offset-4 transition hover:text-white hover:underline"
          >
            {zh ? "看定价 →" : "See pricing →"}
          </a>
        </div>

        <div className="mt-5 space-y-3">
          {(zh ? spec.intro.zh : spec.intro.en).map((p, i) => (
            <p key={i} className="text-sm leading-relaxed text-slate-400">
              {p}
            </p>
          ))}
        </div>

        <div className="mt-10 overflow-x-auto rounded-2xl border border-white/10">
          <table className="w-full min-w-[640px] border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-white/10 bg-white/[0.04] text-xs uppercase tracking-wider text-slate-400">
                <th className="w-[18%] px-4 py-3 font-medium">
                  {zh ? "维度" : "Dimension"}
                </th>
                {/* P0-2：表头此前硬编「智聊 ChatX」，英文页对比表里出现中文列名 */}
                <th className="w-[44%] px-4 py-3 font-semibold text-neon-cyan">
                  {zh ? "智聊 ChatX" : "ChatX"}
                </th>
                <th className="px-4 py-3 font-medium">{spec.name}</th>
              </tr>
            </thead>
            <tbody>
              {spec.rows.map((r, i) => (
                <tr key={i} className="border-b border-white/5 align-top last:border-b-0">
                  <td className="px-4 py-4 font-medium text-white">{pick(r.dim)}</td>
                  <td className="px-4 py-4 text-slate-300">
                    <span className="flex gap-2">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                      <span>{pick(r.us)}</span>
                    </span>
                  </td>
                  <td className="px-4 py-4 text-slate-500">{pick(r.them)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="mt-4 text-xs leading-relaxed text-slate-600">{pick(spec.disclaimer)}</p>

        {/* GEO FAQ：问题式标题 + 自包含答案（与服务端注入的 FAQPage JSON-LD 同源） */}
        <section className="mt-12">
          <h2 className="text-xl font-semibold text-white">
            {zh ? "常见问题" : "Frequently asked questions"}
          </h2>
          <div className="mt-4 space-y-6">
            {spec.faq.map((f, i) => (
              <div key={i}>
                <h3 className="text-sm font-semibold text-slate-200">{pick(f.q)}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-slate-400">{pick(f.a)}</p>
              </div>
            ))}
          </div>
        </section>

        {/* 相关对比互链（P0-1）：读者要横向比，AI 也吃站内链接结构——两头都受益 */}
        {others.length > 0 && (
          <section className="mt-12">
            <h2 className="text-xl font-semibold text-white">
              {zh ? "也在比较这些？" : "Also comparing these?"}
            </h2>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {others.map((o) => (
                <Link
                  key={o.slug}
                  href={L(`/compare/${o.slug}`)}
                  onClick={() => track("cta_click", { where: `compare_${spec.slug}_rel_${o.slug}` })}
                  className="group rounded-xl border border-white/10 px-4 py-3 transition hover:border-neon-cyan/40"
                >
                  <span className="flex items-center justify-between gap-2 text-sm font-medium text-slate-200">
                    {zh ? `智聊 ChatX vs ${o.name}` : `ChatX vs ${o.name}`}
                    <ChevronRight className="h-4 w-4 shrink-0 text-slate-500 transition group-hover:text-neon-cyan" />
                  </span>
                  <span className="mt-1 block text-xs leading-relaxed text-slate-500">{pick(o.answer).slice(0, 88)}…</span>
                </Link>
              ))}
              <Link
                href={L("/compare")}
                onClick={() => track("cta_click", { where: `compare_${spec.slug}_rel_hub` })}
                className="group rounded-xl border border-dashed border-white/10 px-4 py-3 transition hover:border-neon-cyan/40"
              >
                <span className="flex items-center justify-between gap-2 text-sm font-medium text-slate-300">
                  {zh ? "五款工具选型指南（枢纽页）" : "Five-tool selection guide (hub)"}
                  <ChevronRight className="h-4 w-4 shrink-0 text-slate-500 transition group-hover:text-neon-cyan" />
                </span>
              </Link>
            </div>
          </section>
        )}

        <div className="mt-10 flex flex-wrap items-center gap-4">
          <a
            href={zh ? "/download/chatx" : "/en/download/chatx"}
            onClick={() => track("cta_click", { where: `compare_${spec.slug}_trial` })}
            className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 text-sm font-medium text-ink-950 transition hover:opacity-90"
          >
            <Download className="h-4 w-4" />
            {zh ? "免费下载试用（自带体验额度）" : "Free trial download"}
          </a>
          <a
            href={zh ? "/pricing" : "/en/pricing"}
            onClick={() => track("cta_click", { where: `compare_${spec.slug}_order` })}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
          >
            <ShoppingCart className="h-4 w-4" />
            {zh ? "查看套餐与价格" : "Plans & pricing"}
          </a>
          {/* P2（2026-08-18）：合规是对 respond.io/SaleSmartly 的差异化维度——表格里
              有「合规工具」行，这里给可点的深读入口（埋点分 slug 归因）。 */}
          <a
            href={zh ? "/compliance" : "/en/compliance"}
            onClick={() => track("cta_click", { where: `compare_${spec.slug}_compliance` })}
            className="inline-flex items-center gap-1.5 text-sm text-slate-400 underline-offset-4 transition hover:text-neon-cyan hover:underline"
          >
            {zh ? "合规能力详情（EU AI Act / SB 243）→" : "Compliance capabilities (EU AI Act / SB 243) →"}
          </a>
        </div>
      </div>
      </main>
      <Footer />
    </>
  );
}
