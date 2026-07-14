"use client";

import { Download, ShoppingCart, Sparkles } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";
import { TIERS } from "@/lib/avatarhub-pricing";

/** 首页 → 客户端购买/下载 的引流横幅：自助漏斗入口（下载试用 → 订阅），与人工咨询并行。 */
export default function ClientAppCTA() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const from = TIERS.find((t) => t.monthly > 0)?.monthly ?? 39;

  return (
    <section id="client" className="relative py-20">
      <div className="pointer-events-none absolute left-1/2 top-10 h-72 w-72 -translate-x-1/2 rounded-full bg-neon-violet/15 blur-[120px]" />
      <div className="relative mx-auto max-w-5xl px-5">
        <Reveal>
          <div className="glass overflow-hidden rounded-3xl border border-neon-cyan/20 px-8 py-10 text-center md:px-14">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
              <Sparkles className="h-3.5 w-3.5" />
              {zh ? "AvatarHub 客户端 · 14 天免费试用" : "AvatarHub client · 14-day free trial"}
            </span>
            <h2 className="mt-4 text-2xl font-bold text-white md:text-3xl">
              {zh ? "下载客户端，本地跑通你的第一个数字人" : "Download the client and run your first digital human locally"}
            </h2>
            <p className="mx-auto mt-3 max-w-2xl text-sm text-slate-400">
              {zh
                ? `声音克隆 · 实时换脸 · 数字人直播 · 克隆音同传，全部本地部署数据不出机房。会员 ${from} USDT/月起，年付送 2 个月 + 首年 8 折。`
                : `Voice cloning, live face swap, digital-human streaming and interpreting — all local, data on-prem. Plans from ${from} USDT/mo; annual gets 2 months free + 20% off year one.`}
            </p>
            <div className="mt-7 flex flex-wrap items-center justify-center gap-4">
              <a
                href={zh ? "/download" : "/en/download"}
                onClick={() => track("cta_click", { where: "home_client_download" })}
                className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 text-sm font-medium text-ink-950 transition hover:opacity-90"
              >
                <Download className="h-4 w-4" />
                {zh ? "免费下载客户端" : "Download free"}
              </a>
              <a
                href={zh ? "/order" : "/en/order"}
                onClick={() => track("cta_click", { where: "home_client_order" })}
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                <ShoppingCart className="h-4 w-4" />
                {zh ? "查看套餐与价格" : "Plans & pricing"}
              </a>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
