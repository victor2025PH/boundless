"use client";

// 首页套餐区（2026-08-21 充值唯一化改版）：订阅停售 → 卡片改充值形状
// （免费开始 / 新人 6U / 200U 主推 / 1000U / 企业面议，数据派生自 chatx-pricing.ts），
// 月/年切换随订阅一并下线——充值全部一次性，切换器只会制造困惑。
import { Check } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BorderBeam from "./fx/BorderBeam";
import Magnetic from "./fx/Magnetic";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";

export default function Plans() {
  const { t, lang } = useLang();
  // 档位卡直达自助下单（到账自动开通）；免费开始走 href 直达下载页；
  // 没配 plan/href 的档位（企业）回落 Telegram 客服。
  const orderHref = (plan?: string, href?: string) =>
    href ? href : plan ? `${lang === "zh" ? "" : "/en"}/order?plan=${plan}` : CONTACT_URL;

  return (
    <div className="mx-auto max-w-7xl px-5">
      <div className="mb-8 text-center">
        <h3 className="text-2xl font-bold text-white md:text-3xl">{t.plans.title}</h3>
        <p className="mx-auto mt-3 max-w-xl text-slate-400">{t.plans.subtitle}</p>
      </div>

      <div className="grid items-stretch gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {t.plans.items.map((p, i) => {
          const isFree = !p.plan && !!p.href;
          return (
            <Reveal key={p.name} delay={i * 0.08} className="h-full">
              <div
                className={`plan-card card-hover relative flex h-full flex-col overflow-hidden rounded-2xl border p-6 ${
                  p.highlight ? "plan-featured border-transparent" : "border-white/10 bg-ink-900/60"
                }`}
              >
                {p.highlight && <BorderBeam />}
                {p.highlight && (
                  <span className="absolute right-4 top-4 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-2.5 py-0.5 text-[11px] font-semibold text-ink-950">
                    {t.plans.popular}
                  </span>
                )}

                <h4 className="text-lg font-semibold text-white">{p.name}</h4>
                <p className="mt-1 text-xs text-slate-400">{p.desc}</p>

                <div className="mt-5 flex items-end gap-1.5">
                  <span className="text-4xl font-bold tabular-nums text-white">{p.price}</span>
                  <span className="mb-1 text-sm text-slate-400">{p.unit}</span>
                </div>

                <ul className="mt-6 flex-1 space-y-2.5">
                  {p.features.map((f) => (
                    <li key={f} className="flex items-start gap-2 text-sm text-slate-300">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-neon-cyan" />
                      {f}
                    </li>
                  ))}
                </ul>

                <Magnetic className="mt-6 w-full">
                  <a
                    href={orderHref(p.plan, p.href)}
                    {...(p.plan || p.href ? {} : { target: "_blank", rel: "noreferrer" })}
                    onClick={() => track("cta_click", { where: "plans", which: p.plan ?? (isFree ? "free" : "contact") })}
                    className={`block w-full rounded-full px-5 py-2.5 text-center text-sm font-medium transition ${
                      p.highlight
                        ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950 hover:opacity-90"
                        : "plan-ghost-btn border border-white/15 text-slate-200 hover:border-neon-cyan/50 hover:text-white"
                    }`}
                  >
                    {t.plans.cta}
                  </a>
                </Magnetic>
              </div>
            </Reveal>
          );
        })}
      </div>

      <p className="mt-5 text-center text-xs text-slate-500">{t.plans.note}</p>

      {/* 完整价格页入口：Token 费率表 + 用量计算器 + FAQ */}
      <div className="mt-4 text-center">
        <a
          href={`${lang === "zh" ? "" : "/en"}/pricing`}
          onClick={() => track("cta_click", { where: "plans_full_pricing" })}
          className="text-sm text-neon-cyan transition hover:underline"
        >
          {t.plans.fullPricing}
        </a>
      </div>
    </div>
  );
}
