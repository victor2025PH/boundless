"use client";

import Link from "next/link";
import { useLang } from "./LanguageContext";
import BrandMark from "./BrandMark";
import { BRAND } from "@/lib/brand";
import { NAV_PRICING, navLabel } from "@/lib/nav";
import { CONTACT_EMAIL, CONTACT_EMAIL_URL, CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";

export default function Footer() {
  const { t, lang } = useLang();
  const year = new Date().getFullYear();
  const zh = lang === "zh";
  // 锚点带上首页前缀：Footer 也出现在 /voice 等落地页，纯 #hash 在那里会失效
  const home = zh ? "/" : "/en";
  // 主导航：价格项与顶栏/粘性条同源（lib/nav.ts，「看价格」→ /order），其余标签仍取
  // content.ts footer.links；#showcase/#about/#pricing section 已随首页收敛下线。
  const mainLinks = [
    { href: `${home}#products`, label: t.footer.links[0] },
    { href: zh ? NAV_PRICING.path! : `/en${NAV_PRICING.path!}`, label: navLabel(NAV_PRICING, lang) },
    // 实施78 P1-6（2026-08-28）：/compare 枢纽页此前**站内零入口**——AI 会直达具体对比子页，
    // 真人从首页却找不到对比内容（信息架构断层 X5）。页脚是最稳的常驻入口，同时给站内链接
    // 结构（AI 也吃链接图）；标签刻意用「对比选型 / Comparisons」而不是「竞品对比」（后者
    // 像在替对手做广告）。
    { href: zh ? "/compare" : "/en/compare", label: zh ? "对比选型" : "Comparisons" },
    { href: zh ? "/brand" : "/en/brand", label: t.footer.links[2] },
    { href: `${home}#contact`, label: t.footer.links[3] },
  ];
  // 合规隔离（lib/isolation.ts）：gated 线（幻颜/智控的落地页）不出现在页脚，页面仅供直达。
  const landingLinks = [
    { href: zh ? "/voice" : "/en/voice", label: zh ? "幻声 · 声音克隆" : "VoiceX · Voice cloning" },
    { href: zh ? "/interpreting" : "/en/interpreting", label: zh ? "通传 · 克隆音同传" : "VoxX · Interpreting" },
    { href: zh ? "/download" : "/en/download", label: zh ? "下载客户端" : "Download client" },
    { href: zh ? "/manual" : "/en/manual", label: zh ? "使用手册" : "User manual" },
    { href: zh ? "/order" : "/en/order", label: zh ? "购买与下单" : "Plans & ordering" },
    { href: zh ? "/enterprise" : "/en/enterprise", label: zh ? "企业服务" : "Enterprise" },
    { href: zh ? "/videos" : "/en/videos", label: zh ? "视频中心" : "Video hub" },
    // 2026-09-17：智聊 12 集视频教程（learning 入口，与「使用手册」(STUDIO) 并列）
    { href: zh ? "/chatx/tutorials" : "/en/chatx/tutorials", label: zh ? "智聊视频教程" : "ChatX tutorials" },
  ];

  return (
    <footer className="border-t border-white/5 bg-ink-900/40">
      <div className="mx-auto max-w-7xl px-5 py-12">
        <div className="flex flex-col items-start justify-between gap-8 md:flex-row">
          <div className="max-w-sm">
            <div className="flex items-center gap-2">
              <BrandMark className="h-8 w-8" />
              {/* 实施78 P0-2：英文路由只出 BOUNDLESS（与 Navbar/BrandShowcase 同口径） */}
              <span className="font-semibold text-white">
                {zh ? `${BRAND.company.zh} ${BRAND.company.en}` : BRAND.company.en}
              </span>
            </div>
            <p className="mt-4 text-xs leading-relaxed text-slate-500">
              <span className="font-medium text-slate-400">{t.footer.disclaimerTitle}：</span>
              {t.footer.disclaimer}
            </p>
            {/* 服务提供方身份 + 可写信的联系方式（实施78 P0-6 / D9）：此前全站联系方式只有
                Telegram，而我们把 EU AI Act 第 50 条「标明 provider 身份」当卖点，说不圆；
                西方 B2B 买家也习惯先看有没有邮箱。邮箱按 lib/site.CONTACT_EMAIL 判空——
                没开通就只出 Telegram，绝不写一个会退信的地址。 */}
            <p className="mt-3 text-xs leading-relaxed text-slate-500">
              <span className="font-medium text-slate-400">
                {zh ? "服务提供方" : "Service provider"}：
              </span>
              {zh ? `${BRAND.company.zh} ${BRAND.company.en}` : BRAND.company.en}
              {" · "}
              <a href={CONTACT_URL} target="_blank" rel="noopener" className="transition hover:text-slate-300">
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

          <div className="flex flex-col gap-4">
            <nav className="flex flex-wrap gap-x-8 gap-y-2">
              {mainLinks.map((l) => (
                <a
                  key={l.label}
                  href={l.href}
                  className="text-sm text-slate-400 transition hover:text-white"
                >
                  {l.label}
                </a>
              ))}
            </nav>
            <nav className="flex flex-wrap gap-x-8 gap-y-2">
              {landingLinks.map((l) => (
                <Link key={l.href} href={l.href} className="text-sm text-slate-500 transition hover:text-neon-cyan">
                  {l.label}
                </Link>
              ))}
            </nav>
          </div>
        </div>

        <div className="mt-10 flex flex-col items-center gap-3 border-t border-white/5 pt-6 text-center text-xs text-slate-600">
          <div className="flex items-center gap-4">
            <Link href={zh ? "/privacy" : "/en/privacy"} className="transition hover:text-slate-300">
              {zh ? "隐私政策" : "Privacy"}
            </Link>
            <span className="text-slate-700">·</span>
            <Link href={zh ? "/terms" : "/en/terms"} className="transition hover:text-slate-300">
              {zh ? "服务条款" : "Terms"}
            </Link>
            <span className="text-slate-700">·</span>
            <Link href={zh ? "/compliance" : "/en/compliance"} className="transition hover:text-slate-300">
              {zh ? "合规能力" : "Compliance"}
            </Link>
          </div>
          <div>© {year} {t.footer.rights}</div>
        </div>
      </div>
    </footer>
  );
}
