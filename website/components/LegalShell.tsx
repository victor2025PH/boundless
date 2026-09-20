"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useLang } from "./LanguageContext";
import { BRAND } from "@/lib/brand";
import { CONTACT_EMAIL, CONTACT_EMAIL_URL, CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";

export interface LegalSection {
  h: { zh: string; en: string };
  p: { zh: string[]; en: string[] };
}

export default function LegalShell({
  title,
  updated,
  sections,
}: {
  title: { zh: string; en: string };
  updated: string;
  sections: LegalSection[];
}) {
  const { lang } = useLang();
  const zh = lang === "zh";

  return (
    <main className="relative min-h-screen px-5 py-16">
      <div className="mx-auto max-w-3xl">
        <Link
          href={zh ? "/" : "/en"}
          className="inline-flex items-center gap-1.5 text-sm text-slate-400 transition hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          {zh ? "返回首页" : "Back to home"}
        </Link>

        <h1 className="mt-6 text-3xl font-bold tracking-tight text-white md:text-4xl">
          {zh ? title.zh : title.en}
        </h1>
        <p className="mt-2 text-xs text-slate-500">
          {zh ? "最后更新" : "Last updated"}: {updated}
        </p>

        <div className="mt-10 space-y-8">
          {sections.map((s, i) => (
            <section key={i}>
              <h2 className="text-lg font-semibold text-white">{zh ? s.h.zh : s.h.en}</h2>
              <div className="mt-2 space-y-2">
                {(zh ? s.p.zh : s.p.en).map((para, j) => (
                  <p key={j} className="text-sm leading-relaxed text-slate-400">
                    {para}
                  </p>
                ))}
              </div>
            </section>
          ))}
        </div>

        {/* 服务提供方身份（实施78 D9）。本壳**不渲染营销页脚**，所以 8 个法务页
            （合规能力 / 危机公示 / 隐私 / 条款，各中英两版）此前一个联系方式都没有——
            而这些页恰恰是最该标明主体的地方：EU AI Act 第 50 条要求标明 provider 身份，
            隐私政策也需要一个能写信的 data controller。
            刻意只加这一行身份+联系，不塞整个页脚：法务页要的是主体可识别，不是导航与营销位。
            口径与 components/Footer.tsx 的同一段严格一致（同一 BRAND.company 事实源、
            同一判空逻辑）——法律声明在两处说法不同比缺失更糟。
            /en 侧只用英文主体名，别把中文带进英文页（会打破 /en 的 CJK 门禁）。 */}
        <div className="mt-14 border-t border-white/10 pt-6">
          <p className="text-xs leading-relaxed text-slate-500">
            <span className="font-medium text-slate-400">
              {zh ? "服务提供方：" : "Service provider: "}
            </span>
            {zh ? `${BRAND.company.zh} ${BRAND.company.en}` : BRAND.company.en}
            {" · "}
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="transition hover:text-slate-300"
            >
              {TELEGRAM_DISPLAY}
            </a>
            {CONTACT_EMAIL && (
              <>
                {" · "}
                <a href={CONTACT_EMAIL_URL} className="transition hover:text-slate-300">
                  {CONTACT_EMAIL}
                </a>
              </>
            )}
          </p>
        </div>
      </div>
    </main>
  );
}
