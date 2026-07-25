import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";
import { ArrowRight, Download } from "lucide-react";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { CONTACT_URL } from "@/lib/site";
import { MATRIXX } from "@/lib/matrixxContent";

// 合规隔离（lib/isolation.ts）：/matrix 为 gated 页，主站不收录，仅供直达访问。
// 原因与 /face 不同——不是 deepfake 类监管风险，是引擎侧既有市场材料涉及的客户画像
// 风险（详见融合方案文档 §9.24），处理方式与 /face 对齐：仅 noindex，不隐藏导航/矩阵。
export const metadata: Metadata = {
  title: "智控 MatrixX · Telegram 矩阵化运营 | 无界科技 BOUNDLESS",
  description:
    "智控 MatrixX：Telegram 多账号矩阵化运营，搜索发现 / 群监控 / 成员提取 / 消息群发防封 / AI 团队自动回复，规模化增长不失控。本地部署、数据不出网、免显卡。",
  robots: { index: false, follow: false },
  alternates: {
    canonical: "/matrix",
    languages: { "zh-CN": "/matrix", en: "/en/matrix", "x-default": "/matrix" },
  },
  openGraph: {
    type: "website",
    url: "/matrix",
    title: "智控 MatrixX · 矩阵运营之界 | 无界科技 BOUNDLESS",
    description: "多账号矩阵统一调度，AI 团队 24 小时协作，规模化增长不失控。",
    siteName: "无界科技 BOUNDLESS",
  },
};

const lang = "zh" as const;

export default function MatrixLanding() {
  const h = MATRIXX.hero;
  return (
    <>
      <Navbar />
      <main className="relative min-h-screen overflow-hidden bg-ink-950 text-white">
        <div className="pointer-events-none absolute inset-0">
          <div className="absolute -top-40 left-1/2 h-[480px] w-[480px] -translate-x-1/2 rounded-full bg-neon-violet/20 blur-[140px]" />
          <div className="absolute top-1/3 -left-40 h-[360px] w-[360px] rounded-full bg-neon-cyan/15 blur-[120px]" />
        </div>

        <div className="relative mx-auto max-w-5xl px-5 pb-24 pt-28">
          {/* Hero */}
          <section className="text-center">
            <div className="mx-auto mb-6 flex items-center justify-center gap-3">
              <Image src="/brand/products/matrixx.png" alt="智控 MatrixX" width={56} height={56} />
              <span className="text-2xl font-bold tracking-wide">
                智控 <span className="text-slate-400">MatrixX</span>
              </span>
            </div>
            <p className="mb-3 text-xs font-medium uppercase tracking-[0.3em] text-neon-cyan">{h.kicker[lang]}</p>
            <h1 className="bg-gradient-to-r from-neon-cyan via-white to-neon-violet bg-clip-text text-4xl font-black leading-tight text-transparent sm:text-6xl">
              {h.title[lang]}
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-base leading-relaxed text-slate-300 sm:text-lg">
              {h.subtitle[lang]}
            </p>
            <p className="mx-auto mt-4 text-xs text-slate-500">{h.trustline[lang]}</p>
            <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
              <Link
                href="/matrix/download"
                className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
              >
                <Download className="h-4 w-4" />
                免费下载客户端
              </Link>
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-white/30"
              >
                Telegram 咨询方案
                <ArrowRight className="h-4 w-4 transition group-hover:translate-x-0.5" />
              </a>
            </div>
          </section>

          {/* 核心能力 */}
          <section className="mt-20">
            <h2 className="text-center text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">核心能力</h2>
            <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {MATRIXX.caps.map((c) => (
                <div key={c.title[lang]} className="rounded-2xl border border-white/10 bg-white/[0.03] p-6">
                  <h3 className="text-lg font-bold text-white">{c.title[lang]}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-400">{c.desc[lang]}</p>
                </div>
              ))}
            </div>
          </section>

          {/* 适用场景 */}
          <section className="mt-16 rounded-2xl border border-white/10 bg-white/[0.02] p-8">
            <h2 className="text-center text-lg font-semibold text-white">{MATRIXX.scenarios.title[lang]}</h2>
            <div className="mx-auto mt-5 grid max-w-3xl gap-3 sm:grid-cols-2">
              {MATRIXX.scenarios.items.map((s) => (
                <div key={s[lang]} className="flex items-start gap-2 text-sm leading-relaxed text-slate-300">
                  <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-neon-cyan/80" />
                  {s[lang]}
                </div>
              ))}
            </div>
          </section>

          {/* 底部 CTA */}
          <section className="mx-auto mt-16 max-w-xl rounded-3xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-8 text-center">
            <h3 className="text-xl font-bold text-white">立即下载，跑通你的第一个 Telegram 运营矩阵</h3>
            <p className="mt-2 text-sm text-slate-400">
              本地运行、数据不出网、免显卡，注册即用；作为智连系的独立部署产品，可按团队规模与场景定制。
            </p>
            <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
              <Link
                href="/matrix/download"
                className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
              >
                <Download className="h-4 w-4" />
                下载客户端
              </Link>
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-white/30"
              >
                联系我们
              </a>
            </div>
          </section>
        </div>
      </main>
      <Footer />
    </>
  );
}
