"use client";

// /pricing 统一报价页（2026-08-21 充值唯一化改版）：
// 定位=「决策页」——一句话计费：免费开始，充多少用多少，不订阅。
//   ① 免费开始横条（获客入口，不是「订阅第一档」）；
//   ② 新人 6U 大礼包互动海报（3D 倾斜 + 光泽，网页端不放倒计时——匿名访客拿不到
//      注册时间，假倒计时是信任自杀；真实倒计时在桌面端弹窗，见 campaigns feed）；
//   ③ 充值八档专区（首充加赠阶梯到 +40%，大额档带服务权益）；
//   ④ 企业双卡（合作年框 / 私有化部署，lead-based 面议）；
//   ⑤ Token 费率表 + 用量计算器（输出改「推荐充值档 + 续航月数」口径）。
// 一切购买 CTA 深链 /order（结算机器不重复造）。数字零手写，全部派生自
// lib/chatx-pricing.ts（改价只改那里）；幻境 STUDIO 档位仍在 /order 的 STUDIO Tab，
// 本页只放一张跳转卡（本机算力产品不进 Token 体系，防口径混淆）。
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Building2, Check, Cpu, Gift, Languages, MessageCircle, Server, Sparkles, Wallet, Zap } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BorderBeam from "./fx/BorderBeam";
import CountUp from "./fx/CountUp";
import MatrixRain from "./fx/MatrixRain";
import ShineCard from "./fx/ShineCard";
import BonusLadder from "./BonusLadder";
import HeroStatCards from "./HeroStatCards";
import NewbieCountdown, { readStoredRegTs } from "./NewbieCountdown";
import RotatingPerk from "./RotatingPerk";
import { track } from "@/lib/track";
import { CONTACT_URL } from "@/lib/site";
import { faqItems } from "@/lib/pricing-faq";
import { STUDIO_PAID_FROM } from "@/lib/avatarhub-pricing";
import { ORDER_HERO } from "@/lib/order-hero";
import {
  AVG_VOICE_CHARS,
  BONUS_VALID_MONTHS,
  CHATX_FREE,
  ENTERPRISE_TRACKS,
  FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY,
  LINGOX_WORKBENCH,
  NEWBIE_PACK,
  RECHARGE_TIERS,
  RECHARGE_TOKENS_PER_USD,
  SIGNUP_BONUS_TOKENS,
  TOKEN_RATES,
  VIP_REPEAT_BONUS_TIERS,
  estimateMonthlyTokens,
  nextRechargeTier,
  rechargeBaseTokens,
  rechargeFirstTokens,
  rechargeMarginalPerK,
  rechargeOnlyMonthlyCost,
  rechargeUnitPrice,
  rechargeValidMonths,
  recommendRechargeTier,
  tokenRate,
} from "@/lib/chatx-pricing";

const fmt = (n: number) => n.toLocaleString("en-US");
const MAX_PCT = RECHARGE_TIERS[RECHARGE_TIERS.length - 1].firstBonusPct;

export default function PricingPage() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const en = zh ? "" : "/en";

  /** 一次性商品（充值档）深链：无周期参数。 */
  const rechargeHref = (plan: string) => `${en}/order?plan=${plan}`;

  return (
    <section className="order-hero relative pb-24 pt-28">
      {/* ── 首屏背景层（与 /order 同款视觉语言与日间规则：数字雨 + 极光 + 网格 + 扫描线；
            底部渐隐不压内容；数字雨仅 md+ 且夜间——.order-rain 日间整层隐藏） ── */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-[620px] overflow-hidden [mask-image:linear-gradient(#000_70%,transparent)]"
      >
        <MatrixRain className="order-rain absolute inset-0 hidden h-full w-full opacity-[0.11] md:block [mask-image:radial-gradient(ellipse_72%_68%_at_50%_26%,#000_8%,transparent_74%)]" />
        <div className="hero-aurora absolute left-1/2 top-[-12%] h-[52vmax] w-[52vmax] -translate-x-1/2 rounded-full opacity-45" />
        <div className="order-grid absolute inset-0" />
        <div className="hero-scan absolute inset-x-0 top-0 h-36 opacity-25" />
      </div>
      <div className="pointer-events-none absolute left-1/4 top-24 h-80 w-80 rounded-full bg-neon-violet/15 blur-[130px]" />
      <div className="pointer-events-none absolute right-1/4 top-[30rem] h-72 w-72 rounded-full bg-neon-cyan/10 blur-[120px]" />

      <div className="relative mx-auto max-w-7xl px-5">
        {/* ── Hero：核心价格主张（决策页语义，与 /order 的「告别订阅时代」互补不重句） ── */}
        <Reveal eager className="text-center">
          <div className="text-[11px] font-semibold uppercase tracking-[0.34em] text-slate-500">
            {zh ? "价格与计费" : "Pricing & Billing"}
          </div>
          <div className="mt-4 flex justify-center">
            <span className="inline-flex items-center gap-2 rounded-full border border-emerald-300/30 bg-emerald-300/10 px-4 py-1.5 text-xs text-emerald-300">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
              </span>
              {zh ? "2026 充值计费 · 不订阅 · 标准翻译永久免费" : "2026 top-up billing · no subscription · translation free forever"}
            </span>
          </div>
          <h1 className="mx-auto mt-5 max-w-3xl text-4xl font-black leading-[1.15] tracking-tight text-white md:text-6xl">
            <span className="block">{zh ? "免费开始" : "Start free"}</span>
            <span className="text-gradient block">{zh ? "充多少，用多少" : "Top up as you go"}</span>
          </h1>
          <RotatingPerk items={ORDER_HERO.tokens.rotating} zh={zh} />
          <p className="mx-auto mt-4 max-w-2xl text-sm leading-relaxed text-slate-400 md:text-base">
            {zh
              ? `没有月费、没有档位墙：坐席、账号、平台、AI 人设、克隆音色、API 全部开放。1U = ${fmt(RECHARGE_TOKENS_PER_USD)} Token，首笔充值最高加赠 +${MAX_PCT}%；标准翻译永久免费不限字符，Token 用尽自动降级免费引擎，永不断线。`
              : `No monthly fee, no feature walls: seats, accounts, platforms, AI personas, cloned voices and API — all unlocked. 1U = ${fmt(RECHARGE_TOKENS_PER_USD)} tokens with up to +${MAX_PCT}% on your first top-up. Standard translation stays free and unlimited; exhausted wallets degrade gracefully, never offline.`}
          </p>
        </Reveal>

        {/* ── 关键数字数据卡（与 /order 同源同款；到账时长卡带实测替换） ── */}
        <Reveal eager delay={0.1} className="mt-7">
          <HeroStatCards stats={ORDER_HERO.tokens.stats} zh={zh} />
        </Reveal>

        {/* ── 免费开始横条（获客入口——所有付费档的共同起点） ── */}
        <Reveal eager className="mt-10">
          <div className="glass flex flex-wrap items-center gap-x-8 gap-y-3 rounded-2xl border border-emerald-300/25 px-6 py-5">
            <div className="flex items-center gap-3">
              <Gift className="h-7 w-7 shrink-0 text-emerald-300" />
              <div>
                <div className="font-semibold text-white">{zh ? "免费开始 · 下载即用" : "Start free — download & go"}</div>
                <div className="text-xs text-slate-500">{zh ? "不填卡、不订阅" : "No card, no subscription"}</div>
              </div>
            </div>
            <ul className="flex min-w-0 flex-1 flex-wrap gap-x-6 gap-y-1.5 text-xs text-slate-300">
              <li className="flex items-center gap-1.5"><Check className="h-3.5 w-3.5 shrink-0 text-emerald-300" />{zh ? `标准翻译永久免费（公平使用 ${fmt(FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY)} 字符/日）` : `Standard translation free forever (fair use ${fmt(FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY)} chars/day)`}</li>
              <li className="flex items-center gap-1.5"><Check className="h-3.5 w-3.5 shrink-0 text-emerald-300" />{zh ? `每月 ${fmt(CHATX_FREE.tokensMonthly)} Token + 注册再送 ${fmt(SIGNUP_BONUS_TOKENS)}` : `${fmt(CHATX_FREE.tokensMonthly)} tokens/mo + ${fmt(SIGNUP_BONUS_TOKENS)} on signup`}</li>
              <li className="flex items-center gap-1.5"><Check className="h-3.5 w-3.5 shrink-0 text-emerald-300" />{zh ? "全功能开放（1 个聊天账号防滥用）" : "All features (1 chat account anti-abuse cap)"}</li>
            </ul>
            <Link
              href={`${en}/download/chatx`}
              onClick={() => track("pricing_free_start", {})}
              className="shrink-0 rounded-full border border-emerald-300/50 px-6 py-2 text-sm text-emerald-300 transition hover:bg-emerald-300/10"
            >
              {zh ? "免费下载 →" : "Download free →"}
            </Link>
          </div>
        </Reveal>

        {/* ── 新人 6U 大礼包 · 互动海报 ── */}
        <NewbiePoster zh={zh} rechargeHref={rechargeHref} />

        {/* ── 充值专区（唯一付费通道） ── */}
        <RechargeZone zh={zh} rechargeHref={rechargeHref} />

        {/* ── 企业：合作年框 / 私有化部署 ── */}
        <EnterpriseZone zh={zh} />

        {/* ── STUDIO 跳转卡（本机算力产品不进 Token 体系） ── */}
        <Reveal className="mt-6">
          <Link
            href={`${en}/order`}
            onClick={() => track("pricing_studio_jump", {})}
            className="glass group flex flex-wrap items-center gap-4 rounded-2xl border border-white/10 px-6 py-4 transition hover:border-neon-violet/40"
          >
            <Cpu className="h-7 w-7 shrink-0 text-neon-violet" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-white">
                {zh ? "找换脸 / 数字人 / 变声 / 同传？→ 幻境 STUDIO 会员" : "Face swap / digital human / voice changer / interpreting? → STUDIO plans"}
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                {zh
                  ? `引擎跑在你自己的设备上，不按 Token/字符计费、用量不限：免费换脸起步，付费 ${STUDIO_PAID_FROM} USD/月起`
                  : `Runs on your own hardware — no token metering, unlimited usage. Free face swap to start; paid from ${STUDIO_PAID_FROM} USD/mo`}
              </p>
            </div>
            <span className="shrink-0 rounded-full border border-neon-violet/40 px-4 py-1.5 text-xs text-violet-300 transition group-hover:bg-neon-violet/10">
              {zh ? "看 STUDIO 五档 →" : "See STUDIO tiers →"}
            </span>
          </Link>
        </Reveal>

        {/* ── Token 计价透明表 ── */}
        <Reveal className="mt-16">
          <div className="mb-4 flex flex-wrap items-baseline gap-3">
            <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "Token 计价 · 全公开" : "Token rates — fully published"}</h2>
            <span className="text-xs text-slate-500">
              {zh
                ? "每个动作花多少 Token 提前写死在这里；用尽不断线，自动降级到免费引擎"
                : "Every action's token cost is published here. Exhausted wallets never go offline — we degrade to free engines."}
            </span>
          </div>
          <div className="overflow-x-auto rounded-2xl border border-white/10 bg-ink-900/60">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500">
                  <th className="px-5 py-3 font-medium">{zh ? "动作" : "Action"}</th>
                  <th className="px-5 py-3 font-medium">{zh ? "计量单位" : "Unit"}</th>
                  <th className="px-5 py-3 font-medium">{zh ? "Token 消耗" : "Tokens"}</th>
                  <th className="px-5 py-3 font-medium">{zh ? "说明" : "Notes"}</th>
                </tr>
              </thead>
              <tbody>
                {TOKEN_RATES.map((r) => (
                  <tr key={r.key} className="border-t border-white/5">
                    <td className="px-5 py-2.5 text-slate-300">{r.action[lang]}</td>
                    <td className="px-5 py-2.5 text-slate-400">{r.unit[lang]}</td>
                    <td className="whitespace-nowrap px-5 py-2.5 font-semibold text-neon-cyan">
                      {r.tokens === 0 ? (zh ? "0 · 免费" : "0 · free") : `${r.tokens} Token`}
                    </td>
                    <td className="px-5 py-2.5 text-xs text-slate-500">{r.note ? r.note[lang] : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            {zh
              ? `扣减顺序：先扣赠送 Token（${BONUS_VALID_MONTHS} 个月有效），再扣充值实付（12 个月有效，500U 及以上档 24 个月）；存量订阅履约期内，订阅含量在赠送之后、实付之前扣。同一钱包跨智聊 / 通译通用。`
              : `Spend order: bonus tokens first (valid ${BONUS_VALID_MONTHS} months), then paid top-ups (valid 12 months, 24 for 500U+); legacy subscription allowances, while active, spend between the two. One wallet across ChatX & LingoX.`}
          </p>
        </Reveal>

        {/* ── 用量计算器（充值口径） ── */}
        <UsageCalculator zh={zh} rechargeHref={rechargeHref} />

        {/* ── 免费翻译 · 三档翻译能力 ── */}
        <Reveal className="mt-16">
          <div className="mb-4 flex flex-wrap items-baseline gap-3">
            <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "翻译：免费与专业的边界" : "Translation: free vs. pro"}</h2>
            <span className="text-xs text-slate-500">
              {zh ? "标准翻译白送，专业需求才计量——边界提前说清楚" : "Standard is free; only pro workflows meter — the line is published upfront"}
            </span>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {(
              [
                {
                  icon: Gift,
                  title: zh ? "标准翻译 · 永久免费" : "Standard · free forever",
                  desc: zh
                    ? `内置翻译引擎，多平台双向、不限字符（公平使用 ${fmt(FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY)} 字符/日/授权）；安装包提供可选离线翻译组件，弱网也能用。`
                    : `Built-in engine, two-way across platforms, unlimited characters (fair use ${fmt(FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY)}/day). Optional offline component in the installer.`,
                  accent: "emerald" as const,
                },
                {
                  icon: Zap,
                  title: zh ? `专业翻译 · ${tokenRate("pro_translate").tokens} Token/千字符` : `Pro · ${tokenRate("pro_translate").tokens} tokens/1k chars`,
                  desc: zh
                    ? "术语锁定、翻译记忆、图片 / 语音多模态翻译、置信度徽章——跨境成交级的口径一致性。"
                    : "Term-lock glossary, translation memory, image/voice multimodal translate and confidence badges — closing-grade consistency.",
                  accent: "cyan" as const,
                },
                {
                  icon: Sparkles,
                  title: zh ? `认证翻译 · ${tokenRate("deepl_translate").tokens} Token/千字符` : `Certified · ${tokenRate("deepl_translate").tokens} tokens/1k chars`,
                  desc: zh
                    ? "DeepL 认证引擎按需调用，适合合同 / 售后凭证等高价值内容；对照：DeepL 官方 API 约 $25/百万字符另加月费。"
                    : "Certified DeepL engine on demand for contracts and high-stakes content. Reference: DeepL API runs ~$25/1M chars plus base fees.",
                  accent: "violet" as const,
                },
              ] as const
            ).map((c) => (
              <div key={c.title} className="glass rounded-2xl border border-white/10 p-5">
                <c.icon
                  className={`h-6 w-6 ${
                    c.accent === "emerald" ? "text-emerald-300" : c.accent === "cyan" ? "text-neon-cyan" : "text-neon-violet"
                  }`}
                />
                <div className="mt-3 text-sm font-semibold text-white">{c.title}</div>
                <p className="mt-1.5 text-xs leading-relaxed text-slate-400">{c.desc}</p>
              </div>
            ))}
          </div>
          <div className="mt-4 rounded-2xl border border-white/10 bg-ink-900/60 px-5 py-3.5 text-xs leading-relaxed text-slate-400">
            {zh ? (
              <>
                纯翻译团队要多坐席协作？<b className="text-white">翻译工作台 {LINGOX_WORKBENCH.monthly} USD/坐席/月</b>
                ：多坐席统一收件箱 · 客户 journey · 漏斗计数。{" "}
                <Link href={rechargeHref(LINGOX_WORKBENCH.key)} className="text-neon-cyan hover:underline" onClick={() => track("pricing_workbench_cta", {})}>
                  订阅工作台 →
                </Link>
              </>
            ) : (
              <>
                Translation-only team needing shared seats? <b className="text-white">Workbench: {LINGOX_WORKBENCH.monthly} USD/seat/mo</b> —
                multi-seat inbox, customer journey, funnel counter.{" "}
                <Link href={rechargeHref(LINGOX_WORKBENCH.key)} className="text-neon-cyan hover:underline" onClick={() => track("pricing_workbench_cta", {})}>
                  Subscribe →
                </Link>
              </>
            )}
          </div>
        </Reveal>

        {/* ── 竞品对照（锚定） ── */}
        <Reveal className="mt-16">
          <div className="mb-4 flex flex-wrap items-baseline gap-3">
            <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "跟同类工具怎么比" : "How we compare"}</h2>
            <span className="text-xs text-slate-500">
              {zh ? "公开挂牌价对照（2026-08 官网口径，仅供参考，以各家页面为准）" : "Public list prices, checked 2026-08 — verify on each vendor's site"}
            </span>
          </div>
          <div className="overflow-x-auto rounded-2xl border border-white/10 bg-ink-900/60">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500">
                  <th className="px-5 py-3 font-medium">{zh ? "产品" : "Product"}</th>
                  <th className="px-5 py-3 font-medium">{zh ? "起步价" : "Entry price"}</th>
                  <th className="px-5 py-3 font-medium">{zh ? "AI 客服" : "AI agent"}</th>
                  <th className="px-5 py-3 font-medium">{zh ? "聊天翻译" : "Chat translation"}</th>
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    {
                      name: zh ? "无界 智聊 ChatX（本站）" : "BOUNDLESS ChatX (this site)",
                      price: zh ? "免费开始 · 新人 6U · 充值 50U 起（无月费）" : "Free start · 6U newcomer pack · top up from 50U (no monthly fee)",
                      ai: zh ? "全功能开放 · 按 Token 透明计量" : "All features included · transparent token metering",
                      xlate: zh ? "标准翻译免费不限量" : "Standard translation free & unlimited",
                      self: true,
                    },
                    {
                      name: "ChatGPT Plus / Business",
                      price: zh ? "$20/月 · $25/席/月" : "$20/mo · $25/seat/mo",
                      ai: zh ? "通用 AI（非多平台客服收件箱）" : "General AI (no omnichannel inbox)",
                      xlate: zh ? "无聊天工作台翻译" : "No chat-workspace translation",
                    },
                    {
                      name: "respond.io",
                      price: zh ? "$79-159/月 起" : "From $79-159/mo",
                      ai: zh ? "AI 另计 $0.07/次解决" : "AI billed $0.07/resolution",
                      xlate: zh ? "翻译能力有限" : "Limited translation",
                    },
                    {
                      name: "SleekFlow",
                      price: zh ? "$149/月 起（3 席）" : "From $149/mo (3 users)",
                      ai: zh ? "高级 AI 走企业版" : "Advanced AI on Enterprise",
                      xlate: zh ? "翻译能力有限" : "Limited translation",
                    },
                    {
                      name: "Intercom Fin",
                      price: zh ? "席位 $29-132 + AI 按次" : "Seats $29-132 + usage",
                      ai: zh ? "$0.99/结果（月低消 50 次）" : "$0.99/outcome (50/mo min)",
                      xlate: "—",
                    },
                    {
                      name: "DeepL",
                      price: zh ? "个人 $8.74/月 · API $25/百万字符" : "$8.74/mo · API $25/1M chars",
                      ai: "—",
                      xlate: zh ? "仅翻译（无客服工作台）" : "Translation only (no inbox)",
                    },
                  ] as const
                ).map((r) => (
                  <tr
                    key={r.name}
                    className={`border-t border-white/5 ${
                      "self" in r && r.self ? "border-l-2 border-l-neon-cyan/70 bg-neon-cyan/[0.07]" : ""
                    }`}
                  >
                    <td className={`px-5 py-2.5 ${"self" in r && r.self ? "font-semibold text-neon-cyan" : "text-slate-300"}`}>{r.name}</td>
                    <td className="px-5 py-2.5 text-slate-300">{r.price}</td>
                    <td className="px-5 py-2.5 text-slate-400">{r.ai}</td>
                    <td className="px-5 py-2.5 text-slate-400">{r.xlate}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Reveal>

        {/* ── FAQ ── */}
        <Reveal className="mt-16">
          <h2 className="mb-4 text-xl font-bold text-white md:text-2xl">{zh ? "关于新价格的常见问题" : "Pricing FAQ"}</h2>
          <div className="space-y-3">
            {faqItems(zh).map((f) => (
              <details key={f.q} className="glass group rounded-2xl border border-white/10 px-5 py-4 open:border-neon-cyan/30">
                <summary className="cursor-pointer list-none text-sm font-medium text-slate-200 transition group-open:text-white">
                  {f.q}
                </summary>
                <p className="mt-2 text-sm leading-relaxed text-slate-400">{f.a}</p>
              </details>
            ))}
          </div>
        </Reveal>

        {/* ── 底部 CTA ── */}
        <Reveal className="mt-16">
          <div className="glass flex flex-wrap items-center gap-4 rounded-2xl border border-neon-cyan/25 px-6 py-6">
            <MessageCircle className="h-8 w-8 shrink-0 text-neon-cyan" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold text-white">
                {zh ? "先免费用起来，需要 AI 用量再充值" : "Start free — top up when you need more AI"}
              </div>
              <p className="mt-1 text-sm text-slate-400">
                {zh
                  ? `下载即用：全功能 + 翻译不限量 + 每月 ${fmt(CHATX_FREE.tokensMonthly)} Token，注册再送 ${fmt(SIGNUP_BONUS_TOKENS)}；新人 ${NEWBIE_PACK.price}U 大礼包 ${fmt(NEWBIE_PACK.tokens)} Token 随时接上。`
                  : `Download & go: every feature + unlimited translation + ${fmt(CHATX_FREE.tokensMonthly)} tokens/mo, plus ${fmt(SIGNUP_BONUS_TOKENS)} on signup. The ${NEWBIE_PACK.price}U newcomer pack (${fmt(NEWBIE_PACK.tokens)} tokens) is one click away.`}
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap gap-3">
              <Link
                href={`${en}/download/chatx`}
                onClick={() => track("pricing_bottom_download", {})}
                className="rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
              >
                {zh ? "免费下载智聊 ChatX" : "Download ChatX free"}
              </Link>
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("pricing_bottom_contact", {})}
                className="rounded-full border border-white/15 px-6 py-2.5 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                {zh ? "咨询客服" : "Contact support"}
              </a>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

/* ── 新人 6U 大礼包 · 互动海报（ShineCard：3D 倾斜 + 光泽跟随，reduced-motion 自动静态；
      keep-dark：日间模式保持深色广告牌，文字不被覆盖表翻黑） ── */

function NewbiePoster({ zh, rechargeHref }: { zh: boolean; rechargeHref: (plan: string) => string }) {
  const viewed = useRef(false);
  // 真倒计时锚：仅 localStorage 续存（用户从 /order?reg_ts= 深链来过才有）——
  // /pricing 无深链参数场景，匿名访客依旧零倒计时（假倒计时红线不变）。
  const [regTs, setRegTs] = useState(0);

  useEffect(() => {
    if (!viewed.current) {
      viewed.current = true;
      track("pricing_newbie_poster_view", {});
    }
    setRegTs(readStoredRegTs());
  }, []);

  const aiReplies = Math.round(NEWBIE_PACK.tokens / tokenRate("ai_reply").tokens);

  return (
    <Reveal eager className="mt-6">
      <ShineCard
        shineRgb="252,211,77"
        className="keep-dark relative rounded-3xl border border-amber-300/35 bg-gradient-to-br from-[#241a33] via-ink-900 to-[#12233a]"
      >
        <BorderBeam gold />
        <div className="pointer-events-none absolute -right-20 -top-24 h-72 w-72 rounded-full bg-amber-300/10 blur-[90px]" />

        <div className="relative flex flex-wrap items-center gap-x-10 gap-y-6 px-7 py-8 md:px-10">
          <div className="flex min-w-[240px] flex-1 flex-col">
            <span className="flex flex-wrap items-center gap-2">
              <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-amber-300/15 px-3 py-1 text-xs font-medium text-amber-300">
                <Gift className="h-3.5 w-3.5" />
                {zh ? `新人专享 · 注册 ${NEWBIE_PACK.windowHours} 小时内 · 每账号一次` : `Newcomers only · within ${NEWBIE_PACK.windowHours}h of signup · once per account`}
              </span>
              {regTs > 0 && <NewbieCountdown regTs={regTs} zh={zh} />}
            </span>
            <div className="mt-4 flex flex-wrap items-end gap-x-4 gap-y-2">
              <span className="text-5xl font-bold tabular-nums text-white md:text-6xl">{NEWBIE_PACK.price}U</span>
              <span className="pb-1 text-2xl font-bold text-amber-300">→</span>
              <span className="text-4xl font-bold tabular-nums text-amber-300 md:text-5xl">
                <CountUp value={String(NEWBIE_PACK.tokens)} grouping />
              </span>
              <span className="pb-1.5 text-sm text-slate-400">Token</span>
            </div>
            <p className="mt-3 max-w-xl text-sm leading-relaxed text-slate-300">
              {zh
                ? `双倍到账（$${rechargeUnitPrice(NEWBIE_PACK.price, NEWBIE_PACK.tokens)}/千 Token，全场最低单价）≈ ${fmt(aiReplies)} 条 AI 回复；不占用首充加赠资格——之后首笔正常充值仍享 +5%~+${MAX_PCT}% 阶梯。`
                : `Double rate ($${rechargeUnitPrice(NEWBIE_PACK.price, NEWBIE_PACK.tokens)}/1k — the lowest unit price here) ≈ ${fmt(aiReplies)} AI replies. It doesn't consume your first-top-up bonus of +5%–${MAX_PCT}%.`}
            </p>
          </div>
          <div className="flex shrink-0 flex-col items-stretch gap-2.5">
            <Link
              href={rechargeHref(NEWBIE_PACK.key)}
              onClick={() => track("pricing_newbie_poster_cta", {})}
              className="cta-fx rounded-full bg-gradient-to-r from-amber-300 to-amber-400 px-8 py-3 text-center text-sm font-semibold text-ink-950 shadow-[0_0_28px_rgba(252,211,77,0.25)] transition hover:opacity-90"
            >
              {zh ? `立即 ${NEWBIE_PACK.price}U 领取 ${fmt(NEWBIE_PACK.tokens)} Token` : `Claim ${fmt(NEWBIE_PACK.tokens)} tokens for ${NEWBIE_PACK.price}U`}
            </Link>
            <span className="text-center text-[11px] text-slate-500">
              {zh ? "已下载？桌面端启动弹窗里有同款入口与真实倒计时" : "Already installed? The desktop popup carries the same offer with a live countdown"}
            </span>
          </div>
        </div>
      </ShineCard>
    </Reveal>
  );
}

/* ── 充值专区（唯一付费通道：8 档 + 首充加赠阶梯 + 大额档权益） ── */

function RechargeZone({ zh, rechargeHref }: { zh: boolean; rechargeHref: (plan: string) => string }) {
  const [sel, setSel] = useState<string>(RECHARGE_TIERS.find((t) => t.hot)?.key ?? RECHARGE_TIERS[0].key);
  const tier = RECHARGE_TIERS.find((t) => t.key === sel) ?? RECHARGE_TIERS[0];
  const base = rechargeBaseTokens(tier);
  const first = rechargeFirstTokens(tier);

  return (
    <Reveal className="mt-16">
      <div className="mb-6 flex items-center gap-4">
        <div className="h-px flex-1 bg-white/10" />
        <div className="text-center">
          <div className="text-lg font-bold text-white md:text-xl">
            {zh ? "Token 充值 · 唯一付费方式" : "Token top-ups — the only way to pay"}
          </div>
          <div className="mt-0.5 text-xs text-slate-500">
            {zh
              ? `1U = ${fmt(RECHARGE_TOKENS_PER_USD)} Token · 首笔充值一次性加赠最高 +${MAX_PCT}%（每人一次）· 充得越多单价越低`
              : `1U = ${fmt(RECHARGE_TOKENS_PER_USD)} tokens · first top-up earns up to +${MAX_PCT}% once · bigger tiers, lower unit price`}
          </div>
        </div>
        <div className="h-px flex-1 bg-white/10" />
      </div>

      <div className="glass rounded-2xl border border-white/10 p-6">
        <div className="flex flex-wrap items-center gap-2">
          {RECHARGE_TIERS.map((t) => (
            <button
              key={t.key}
              onClick={() => {
                setSel(t.key);
                track("pricing_recharge_tier", { tier: t.key });
              }}
              className={`rounded-full px-4 py-1.5 text-sm transition ${
                sel === t.key
                  ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                  : "border border-white/15 text-slate-300 hover:border-neon-cyan/50 hover:text-white"
              }`}
            >
              {t.price}U
              {t.firstBonusPct > 0 && (
                <span className={`ml-1 text-[11px] ${sel === t.key ? "text-ink-950/80" : "text-neon-cyan"}`}>+{t.firstBonusPct}%</span>
              )}
            </button>
          ))}
        </div>

        {/* 首充加赠能量阶梯（与 /order 同款组件：节点可点选档 + 升档提示 + 选中脉冲） */}
        <BonusLadder
          zh={zh}
          selectedKey={sel}
          className="mt-6"
          onSelect={(k) => {
            setSel(k);
            track("pricing_recharge_tier", { tier: k, via: "ladder" });
          }}
        />

        {/* 到账明细：首充 / 复充两个口径同时给，资格由到账时核验（页面不假装知道） */}
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <div className="rounded-xl border border-neon-cyan/30 bg-neon-cyan/[0.06] px-4 py-3">
            <div className="text-xs text-slate-400">
              {zh ? "首充到账（每人一次）" : "First top-up (once per person)"}
              {tier.firstBonusPct > 0 && (
                <span className="ml-1.5 rounded-full bg-neon-cyan/20 px-1.5 py-0.5 text-[10px] font-medium text-neon-cyan">
                  +{tier.firstBonusPct}%
                </span>
              )}
            </div>
            <div className="mt-1 text-2xl font-bold tabular-nums text-neon-cyan">
              <CountUp value={String(first)} grouping duration={0.9} />
            </div>
            <div className="text-[11px] text-slate-500">≈ ${rechargeUnitPrice(tier.price, first)}/{zh ? "千 Token" : "1k tokens"}</div>
          </div>
          <div className="rounded-xl border border-white/10 bg-ink-950/50 px-4 py-3">
            <div className="text-xs text-slate-400">{zh ? "复充到账（基准价起）" : "Repeat top-up (from base rate)"}</div>
            <div className="mt-1 text-2xl font-bold tabular-nums text-white">
              <CountUp value={String(base)} grouping duration={0.9} />
            </div>
            <div className="text-[11px] text-slate-500">≈ ${rechargeUnitPrice(tier.price, base)}/{zh ? "千 Token" : "1k tokens"}</div>
          </div>
        </div>

        {/* VIP 累充等级（实施50 P2）：复充不再是纯基准价——累计充值越多，之后每笔复充加赠越高 */}
        <div className="mt-3 rounded-xl border border-neon-violet/25 bg-neon-violet/[0.05] px-4 py-3">
          <div className="text-xs font-medium text-violet-300">
            {zh ? "👑 VIP 累充等级 · 复充自动加赠" : "👑 VIP loyalty tiers — repeat top-ups earn bonuses"}
          </div>
          <ul className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-300">
            {VIP_REPEAT_BONUS_TIERS.map((t) => (
              <li key={t.fromUsd} className="flex items-center gap-1.5">
                <Check className="h-3.5 w-3.5 shrink-0 text-violet-300" />
                {zh ? `累计充值 ≥ ${fmt(t.fromUsd)}U → 复充 +${t.pct}%` : `Lifetime top-ups ≥ ${fmt(t.fromUsd)}U → +${t.pct}% on every repeat`}
              </li>
            ))}
          </ul>
          <div className="mt-1 text-[11px] text-slate-500">
            {zh ? "按累计已付金额自动生效，无需申请；与首充加赠不叠加（首单走首充阶梯）。" : "Applies automatically by lifetime paid total; doesn't stack with the first-top-up bonus (your first order uses the first-charge ladder)."}
          </div>
        </div>

        {/* 大额档服务权益（≥5000U；单源在 chatx-pricing.RechargeTier.perks） */}
        {tier.perks && (
          <div className="mt-3 rounded-xl border border-amber-300/25 bg-amber-300/[0.05] px-4 py-3">
            <div className="text-xs font-medium text-amber-300">{zh ? "本档附带服务权益" : "Service perks in this tier"}</div>
            <ul className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-300">
              {(zh ? tier.perks.zh : tier.perks.en).map((p) => (
                <li key={p} className="flex items-center gap-1.5">
                  <Check className="h-3.5 w-3.5 shrink-0 text-amber-300" />
                  {p}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Link
            href={rechargeHref(tier.key)}
            onClick={() => track("pricing_recharge_cta", { tier: tier.key })}
            className="cta-fx rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
          >
            {zh ? `充值 ${fmt(tier.price)}U` : `Top up ${fmt(tier.price)}U`}
          </Link>
          <span className="text-[11px] leading-relaxed text-slate-500">
            {zh
              ? `实付 Token ${rechargeValidMonths(tier)} 个月有效 · 赠送部分 ${BONUS_VALID_MONTHS} 个月且先扣 · 跨智聊/通译同一钱包`
              : `Paid tokens valid ${rechargeValidMonths(tier)} months · bonus ${BONUS_VALID_MONTHS} months, spends first · one wallet across products`}
          </span>
          <a
            href="#enterprise"
            onClick={() => track("pricing_enterprise_jump", { from: tier.key })}
            className="ml-auto text-xs text-amber-300/90 transition hover:text-amber-300 hover:underline"
          >
            {zh ? "年用量超过 10000U？看企业合作 →" : "Beyond 10000U a year? Enterprise →"}
          </a>
        </div>

        <p className="mt-3 border-t border-white/5 pt-3 text-[11px] leading-relaxed text-slate-600">
          {zh
            ? `首充规则：仅首笔充值享受加赠，按档位一次性发放（100U +5% · 200U +10% · 500U +20% · 1000U +30% · 5000U +35% · 10000U +40%），到账时按 账号 + 支付指纹 核验（每人一次）；退款回收加赠部分。50U 为最低充值额；新人 ${NEWBIE_PACK.price}U 大礼包不占用首充资格。`
            : `First top-up rules: the bonus applies to your first top-up only, granted once per person (100U +5% · 200U +10% · 500U +20% · 1000U +30% · 5000U +35% · 10000U +40%), verified by account + payment fingerprint at credit time; refunds reclaim the bonus. 50U minimum; the ${NEWBIE_PACK.price}U newcomer pack doesn't consume this.`}
        </p>
      </div>
    </Reveal>
  );
}

/* ── 企业双卡（合作年框 / 私有化部署——lead-based 面议，CTA=联系商务） ── */

function EnterpriseZone({ zh }: { zh: boolean }) {
  const enBase = zh ? "" : "/en";
  return (
    <Reveal className="mt-10">
      <div id="enterprise" className="grid gap-5 md:grid-cols-2">
        {ENTERPRISE_TRACKS.map((t) => (
          <div
            key={t.key}
            className="relative flex flex-col overflow-hidden rounded-2xl border border-amber-300/25 bg-gradient-to-br from-ink-900 to-[#1a1626] p-6"
          >
            <div className="pointer-events-none absolute -right-16 -top-20 h-56 w-56 rounded-full bg-amber-300/[0.07] blur-[80px]" />
            {t.key === "enterprise-coop" ? (
              <Building2 className="h-7 w-7 text-amber-300" />
            ) : (
              <Server className="h-7 w-7 text-amber-300" />
            )}
            <div className="mt-3 flex flex-wrap items-baseline gap-x-3">
              <span className="font-semibold text-white">{zh ? t.name.zh : t.name.en}</span>
              <span className="rounded-full bg-amber-300/15 px-2 py-0.5 text-[11px] text-amber-300">{zh ? "面议" : "Custom quote"}</span>
            </div>
            <p className="mt-1 text-xs text-slate-400">{zh ? t.tagline.zh : t.tagline.en}</p>
            <ul className="mt-4 flex-1 space-y-2">
              {(zh ? t.points.zh : t.points.en).map((p) => (
                <li key={p} className="flex items-start gap-2 text-xs leading-relaxed text-slate-300">
                  <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" />
                  {p}
                </li>
              ))}
            </ul>
            {/* 主 CTA 进 /enterprise 独立页（叙事+站内表单，企业买家不一定用 TG）；
                TG 直连降为次级小链接。 */}
            <Link
              href={`${enBase}/enterprise`}
              onClick={() => track("pricing_enterprise_cta", { track: t.key })}
              className="mt-5 rounded-full border border-amber-300/50 py-2 text-center text-sm text-amber-300 transition hover:bg-amber-300/10"
            >
              {zh ? "了解企业服务 · 提交需求" : "Enterprise details · submit request"}
            </Link>
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("pricing_enterprise_tg", { track: t.key })}
              className="mt-2 text-center text-[11px] text-slate-500 transition hover:text-slate-300"
            >
              {zh ? "或 Telegram 直连商务 →" : "or talk on Telegram →"}
            </a>
          </div>
        ))}
      </div>
    </Reveal>
  );
}

/* ── 用量计算器（充值口径：月成本 + 推荐档续航） ── */

function UsageCalculator({ zh, rechargeHref }: { zh: boolean; rechargeHref: (plan: string) => string }) {
  const [replies, setReplies] = useState(100);
  const [proK, setProK] = useState(100);
  const [voiceMsgs, setVoiceMsgs] = useState(20);
  const [images, setImages] = useState(10);

  const tokens = useMemo(
    () =>
      estimateMonthlyTokens({
        repliesPerDay: replies,
        proTranslateKCharsPerMonth: proK,
        voiceMsgsPerDay: voiceMsgs,
        imagesPerMonth: images,
      }),
    [replies, proK, voiceMsgs, images],
  );
  const monthlyCost = useMemo(() => rechargeOnlyMonthlyCost(tokens), [tokens]);
  const advice = useMemo(() => recommendRechargeTier(tokens), [tokens]);
  const next = useMemo(() => nextRechargeTier(advice.tier), [advice]);

  const sliders: {
    label: string;
    value: number;
    set: (n: number) => void;
    max: number;
    step: number;
    hint: string;
  }[] = [
    {
      label: zh ? "日均 AI 回复（条）" : "AI replies / day",
      value: replies,
      set: setReplies,
      max: 2000,
      step: 10,
      hint: zh ? `${tokenRate("ai_reply").tokens} Token/条` : `${tokenRate("ai_reply").tokens} tokens each`,
    },
    {
      label: zh ? "月均专业翻译（千字符）" : "Pro translation / mo (1k chars)",
      value: proK,
      set: setProK,
      max: 3000,
      step: 10,
      hint: zh ? `${tokenRate("pro_translate").tokens} Token/千字符 · 标准翻译不耗` : `${tokenRate("pro_translate").tokens} tokens/1k · standard is free`,
    },
    {
      label: zh ? "日均克隆语音（条）" : "Voice messages / day",
      value: voiceMsgs,
      set: setVoiceMsgs,
      max: 500,
      step: 5,
      hint: zh ? `按均 ${AVG_VOICE_CHARS} 字/条折算` : `assumes ~${AVG_VOICE_CHARS} chars each`,
    },
    {
      label: zh ? "月均 AI 配图（张）" : "AI images / mo",
      value: images,
      set: setImages,
      max: 300,
      step: 5,
      hint: zh ? `${tokenRate("ai_image").tokens} Token/张` : `${tokenRate("ai_image").tokens} tokens each`,
    },
  ];

  const monthsLabel = (m: number) => (m === Infinity ? (zh ? "按需" : "as needed") : zh ? `约 ${m} 个月` : `~${m} mo`);

  return (
    <Reveal className="mt-16">
      <div className="mb-4 flex flex-wrap items-baseline gap-3">
        <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "算一算：该充哪一档" : "Estimate: which tier to top up"}</h2>
        <span className="text-xs text-slate-500">
          {zh ? "拖滑块看月用量；右侧给出月成本与推荐充值档的续航" : "Drag the sliders — monthly cost and the recommended tier's runway update live"}
        </span>
      </div>
      <div className="glass grid gap-8 rounded-2xl border border-white/10 p-6 lg:grid-cols-[1.1fr_1fr]">
        <div className="space-y-5">
          {sliders.map((s) => (
            <div key={s.label}>
              <div className="flex items-baseline justify-between gap-3">
                <label className="text-sm text-slate-300">{s.label}</label>
                <span className="font-semibold tabular-nums text-neon-cyan">{fmt(s.value)}</span>
              </div>
              <input
                type="range"
                min={0}
                max={s.max}
                step={s.step}
                value={s.value}
                onChange={(e) => s.set(Number(e.target.value))}
                className="mt-1.5 w-full accent-cyan-400"
              />
              <div className="text-[11px] text-slate-600">{s.hint}</div>
            </div>
          ))}
        </div>
        <div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-xl border border-white/10 bg-ink-950/50 px-5 py-4">
              <div className="text-xs text-slate-500">{zh ? "预计月用量" : "Monthly usage"}</div>
              <div className="mt-1 text-2xl font-bold tabular-nums text-white">
                {fmt(tokens)} <span className="text-xs font-normal text-slate-500">Token</span>
              </div>
            </div>
            <div className="rounded-xl border border-white/10 bg-ink-950/50 px-5 py-4">
              <div className="text-xs text-slate-500">{zh ? "月成本（基准价）" : "Monthly cost (base rate)"}</div>
              <div className="mt-1 text-2xl font-bold tabular-nums text-white">
                ${fmt(Math.round(monthlyCost))}
                <span className="ml-1 text-xs font-normal text-slate-500">/{zh ? "月" : "mo"}</span>
              </div>
            </div>
          </div>

          <div className="mt-4 rounded-xl border border-neon-cyan/40 bg-neon-cyan/[0.07] px-5 py-4">
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <Wallet className="h-3.5 w-3.5 text-neon-cyan" />
              {zh ? "推荐充值档" : "Recommended tier"}
              <span className="rounded-full bg-neon-cyan/20 px-2 py-0.5 text-[10px] font-medium text-neon-cyan">
                {advice.tier.firstBonusPct > 0 ? `+${advice.tier.firstBonusPct}%` : zh ? "起充档" : "starter"}
              </span>
            </div>
            <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3">
              <span className="text-2xl font-bold tabular-nums text-neon-cyan">{fmt(advice.tier.price)}U</span>
              <span className="text-xs text-slate-400">
                {zh
                  ? `首充可用 ${monthsLabel(advice.monthsFirst)} · 复充 ${monthsLabel(advice.monthsRepeat)}`
                  : `first top-up lasts ${monthsLabel(advice.monthsFirst)} · repeat ${monthsLabel(advice.monthsRepeat)}`}
              </span>
            </div>
            {next && (
              <div className="mt-1 text-[11px] text-slate-500">
                {zh
                  ? `预算宽裕可直上 ${fmt(next.price)}U（+${next.firstBonusPct}%），单价降到 $${rechargeUnitPrice(next.price, rechargeFirstTokens(next))}/千`
                  : `With more budget, jump to ${fmt(next.price)}U (+${next.firstBonusPct}%) at $${rechargeUnitPrice(next.price, rechargeFirstTokens(next))}/1k`}
              </div>
            )}
          </div>

          <Link
            href={rechargeHref(advice.tier.key)}
            onClick={() => track("pricing_calc_cta", { tier: advice.tier.key, tokens })}
            className="cta-fx mt-4 block rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet py-2.5 text-center text-sm font-medium text-ink-950 transition hover:opacity-90"
          >
            {zh ? `按推荐充值 ${fmt(advice.tier.price)}U` : `Top up ${fmt(advice.tier.price)}U as recommended`}
          </Link>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
            {zh
              ? `估算口径与计费口径同一张费率表（基准 $${rechargeMarginalPerK()}/千 Token）；标准翻译不计入用量；首充加赠让实际单价更低。`
              : `Estimates use the same published rate table as billing (base $${rechargeMarginalPerK()}/1k tokens); standard translation costs nothing; the first-top-up bonus lowers your effective rate further.`}
          </p>
        </div>
      </div>
    </Reveal>
  );
}
