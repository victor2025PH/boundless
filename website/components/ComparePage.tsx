"use client";

import Link from "next/link";
import { ArrowLeft, Check, Download, ShoppingCart } from "lucide-react";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";
import type { CompareSpec } from "@/lib/compare-content";

/** 竞品对比页共享壳（WS-1 2026-08-17）。
 *  措辞纪律见 lib/compare-content.ts 顶注：我方列=已交付实况，竞品列=其官网公开
 *  资料的一般性理解 + 全文免责。布局对齐 LegalShell 的窄栏阅读风格。 */
export default function ComparePage({ spec }: { spec: CompareSpec }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const pick = (c: { zh: string; en: string }) => (zh ? c.zh : c.en);

  return (
    <main className="relative min-h-screen px-5 py-16">
      <div className="mx-auto max-w-4xl">
        <Link
          href={zh ? "/" : "/en"}
          className="inline-flex items-center gap-1.5 text-sm text-slate-400 transition hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          {zh ? "返回首页" : "Back to home"}
        </Link>

        <h1 className="mt-6 text-3xl font-bold tracking-tight text-white md:text-4xl">
          {pick(spec.tagline)}
        </h1>
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
                <th className="w-[44%] px-4 py-3 font-semibold text-neon-cyan">
                  智聊 ChatX
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
            href={zh ? "/order?plan=team" : "/en/order?plan=team"}
            onClick={() => track("cta_click", { where: `compare_${spec.slug}_order` })}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
          >
            <ShoppingCart className="h-4 w-4" />
            {zh ? "查看套餐与价格" : "Plans & pricing"}
          </a>
        </div>
      </div>
    </main>
  );
}
