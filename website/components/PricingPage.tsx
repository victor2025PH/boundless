"use client";

// /pricing 统一报价页（2026-08-19 Token 定价改版新增）：
// 定位=「决策页」——档位对比 / Token 计价透明表 / 用量计算器 / 竞品对照 / FAQ；
// 一切购买 CTA 深链 /order（结算机器不重复造）。数字零手写，全部派生自
// lib/chatx-pricing.ts（改价只改那里）；幻境 STUDIO 档位仍在 /order 的 STUDIO Tab，
// 本页只放一张跳转卡（本机算力产品不进 Token 体系，防口径混淆）。
import { useMemo, useState } from "react";
import Link from "next/link";
import { Check, Cpu, Gift, Languages, MessageCircle, Sparkles, Wallet, Zap } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BorderBeam from "./fx/BorderBeam";
import { track } from "@/lib/track";
import { CONTACT_URL } from "@/lib/site";
import { STUDIO_PAID_FROM } from "@/lib/avatarhub-pricing";
import {
  AVG_VOICE_CHARS,
  CHATX_PLANS,
  FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY,
  LINGOX_WORKBENCH,
  SIGNUP_BONUS_TOKENS,
  TOKEN_PACKS,
  TOKEN_PACK_VALID_MONTHS,
  TOKEN_RATES,
  annualTotal,
  comparePlanCosts,
  estimateMonthlyTokens,
  packBonusPct,
  packUnitPrice,
  recommendPlan,
  tokenRate,
  type ChatxPlan,
} from "@/lib/chatx-pricing";

const fmt = (n: number) => n.toLocaleString("en-US");

type Billing = "monthly" | "annual";

export default function PricingPage() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [billing, setBilling] = useState<Billing>("monthly");
  const en = zh ? "" : "/en";

  const orderHref = (plan: string, extra = "") =>
    `${en}/order?plan=${plan}${billing === "annual" ? "&period=annual" : ""}${extra}`;

  return (
    <section className="relative pb-24 pt-32">
      <div className="pointer-events-none absolute left-1/4 top-24 h-80 w-80 rounded-full bg-neon-violet/15 blur-[130px]" />
      <div className="pointer-events-none absolute right-1/4 top-[30rem] h-72 w-72 rounded-full bg-neon-cyan/10 blur-[120px]" />

      <div className="relative mx-auto max-w-7xl px-5">
        {/* ── Hero：核心价格主张 ── */}
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-300/30 bg-emerald-300/10 px-3 py-1 text-xs text-emerald-300">
            <Languages className="h-3.5 w-3.5" />
            {zh ? "2026 新价格体系 · 标准翻译永久免费" : "New 2026 pricing · standard translation free forever"}
          </span>
          <h1 className="mx-auto mt-4 max-w-3xl text-3xl font-bold leading-tight text-white md:text-5xl">
            {zh ? (
              <>
                翻译永久免费，
                <span className="bg-gradient-to-r from-neon-cyan to-neon-violet bg-clip-text text-transparent">只为成交付费</span>
              </>
            ) : (
              <>
                Translation free forever.
                <span className="bg-gradient-to-r from-neon-cyan to-neon-violet bg-clip-text text-transparent"> Pay only to close.</span>
              </>
            )}
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-slate-400">
            {zh
              ? `多平台聊天标准翻译不限字符、所有档位免费含；AI 回复 / 专业翻译 / 克隆语音 / AI 配图按公示 Token 费率计量——订阅含每月 Token，超出买 Token 包，用尽自动降级到免费引擎，永不断线。注册即送 ${fmt(SIGNUP_BONUS_TOKENS)} 体验 Token。`
              : `Unlimited standard chat translation in every plan, free. AI replies, pro translation, cloned voice and AI images meter at published token rates — plans include monthly tokens, packs top you up, and exhausted wallets degrade gracefully to free engines. ${fmt(SIGNUP_BONUS_TOKENS)} bonus tokens on signup.`}
          </p>
        </Reveal>

        {/* ── 月/年切换 ── */}
        <Reveal eager className="mt-8 flex justify-center">
          <div className="glass inline-flex items-center rounded-full border border-white/10 p-1 text-sm">
            {(["monthly", "annual"] as Billing[]).map((b) => (
              <button
                key={b}
                onClick={() => {
                  setBilling(b);
                  track("pricing_billing", { billing: b });
                }}
                className={`flex items-center gap-2 rounded-full px-5 py-2 transition ${
                  billing === b
                    ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                    : "text-slate-300 hover:text-white"
                }`}
              >
                {b === "monthly" ? (zh ? "月付" : "Monthly") : zh ? "年付" : "Annual"}
                {b === "annual" && (
                  <span
                    className={`rounded-full px-1.5 py-0.5 text-[11px] ${
                      billing === "annual" ? "bg-ink-950/20 text-ink-950" : "bg-neon-cyan/15 text-neon-cyan"
                    }`}
                  >
                    {zh ? "省 2 个月" : "2 months free"}
                  </span>
                )}
              </button>
            ))}
          </div>
        </Reveal>

        {/* ── 五档卡片 ── */}
        <div className="mt-10 grid gap-5 sm:grid-cols-2 xl:grid-cols-5">
          {CHATX_PLANS.map((p, i) => (
            <PlanCard key={p.key} plan={p} zh={zh} billing={billing} delay={i * 0.05} orderHref={orderHref} />
          ))}
        </div>

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
              ? `扣减顺序：先扣订阅当月含量（月底清零），再扣 Token 包（${TOKEN_PACK_VALID_MONTHS} 个月有效，先过期先扣）；同一钱包跨智聊 / 通译通用。`
              : `Spend order: plan allowance first (resets monthly), then packs (valid ${TOKEN_PACK_VALID_MONTHS} months, earliest expiry first). One wallet across ChatX & LingoX.`}
          </p>
        </Reveal>

        {/* ── Token 包 ── */}
        <Reveal className="mt-16">
          <div className="mb-4 flex flex-wrap items-baseline gap-3">
            <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "Token 包 · 跨产品通用" : "Token packs — one shared wallet"}</h2>
            <span className="text-xs text-slate-500">
              {zh ? "一次性购买，12 个月有效；按量版用户预充即用，订阅用户超量加购" : "One-time top-ups, valid 12 months; Flex users prepay, subscribers top up"}
            </span>
          </div>
          <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-4">
            {TOKEN_PACKS.map((p, i) => (
              <Reveal eager key={p.key} delay={i * 0.05} className="h-full">
                <div
                  className={`relative flex h-full flex-col rounded-2xl border p-5 ${
                    p.hot ? "border-neon-cyan/50 bg-ink-800/80" : "border-white/10 bg-ink-900/60"
                  }`}
                >
                  {p.hot && (
                    <span className="absolute -top-2.5 right-4 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-2.5 py-0.5 text-[11px] font-medium text-ink-950">
                      {zh ? "最受欢迎" : "Most popular"}
                    </span>
                  )}
                  <div className="flex items-center gap-2">
                    <Wallet className="h-4 w-4 text-neon-cyan" />
                    <span className="font-semibold text-white">{p.name[lang]}</span>
                  </div>
                  <div className="mt-3">
                    <span className="text-3xl font-bold tabular-nums text-white">{p.price}</span>
                    <span className="ml-1.5 text-xs text-slate-500">USD</span>
                  </div>
                  <div className="mt-1 text-sm text-neon-cyan">{fmt(p.tokens)} Token</div>
                  <div className="mt-3 flex-1 space-y-1.5 text-xs text-slate-400">
                    <div>≈ ${packUnitPrice(p)} / {zh ? "千 Token" : "1k tokens"}</div>
                    <div>
                      {packBonusPct(p) > 0
                        ? zh
                          ? `比体验包多送 ${packBonusPct(p)}%`
                          : `+${packBonusPct(p)}% vs starter pack`
                        : zh
                          ? "入门首选 · 试深浅"
                          : "First top-up pick"}
                    </div>
                    <div>{zh ? `${TOKEN_PACK_VALID_MONTHS} 个月有效` : `Valid ${TOKEN_PACK_VALID_MONTHS} months`}</div>
                  </div>
                  <Link
                    href={orderHref(p.key)}
                    onClick={() => track("pricing_pack_cta", { pack: p.key })}
                    className={`mt-4 rounded-full py-2 text-center text-sm transition ${
                      p.hot
                        ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950 hover:opacity-90"
                        : "border border-white/15 text-slate-200 hover:border-neon-cyan/50 hover:text-white"
                    }`}
                  >
                    {zh ? "购买此包" : "Buy this pack"}
                  </Link>
                </div>
              </Reveal>
            ))}
          </div>
        </Reveal>

        {/* ── 用量计算器 ── */}
        <UsageCalculator zh={zh} billing={billing} orderHref={orderHref} />

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
                <Link href={orderHref(LINGOX_WORKBENCH.key)} className="text-neon-cyan hover:underline" onClick={() => track("pricing_workbench_cta", {})}>
                  订阅工作台 →
                </Link>
              </>
            ) : (
              <>
                Translation-only team needing shared seats? <b className="text-white">Workbench: {LINGOX_WORKBENCH.monthly} USD/seat/mo</b> —
                multi-seat inbox, customer journey, funnel counter.{" "}
                <Link href={orderHref(LINGOX_WORKBENCH.key)} className="text-neon-cyan hover:underline" onClick={() => track("pricing_workbench_cta", {})}>
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
                      price: zh ? "免费版 $0 · 个人版 $39/月" : "Free $0 · Personal $39/mo",
                      ai: zh ? "全档含 · 按 Token 透明计量" : "Included · transparent token metering",
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
                  <tr key={r.name} className={`border-t border-white/5 ${"self" in r && r.self ? "bg-neon-cyan/[0.05]" : ""}`}>
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
                {zh ? "先免费用起来，再决定要不要付费" : "Start free — decide later"}
              </div>
              <p className="mt-1 text-sm text-slate-400">
                {zh
                  ? `免费版翻译不限量 + 每月 1,000 Token，注册再送 ${fmt(SIGNUP_BONUS_TOKENS)}；拿不准选哪档，用上面的计算器或找客服。`
                  : `Free plan: unlimited translation + 1,000 tokens/mo, plus ${fmt(SIGNUP_BONUS_TOKENS)} on signup. Unsure? Use the calculator above or ping support.`}
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

/* ── 档位卡 ── */

function PlanCard({
  plan: p,
  zh,
  billing,
  delay,
  orderHref,
}: {
  plan: ChatxPlan;
  zh: boolean;
  billing: Billing;
  delay: number;
  orderHref: (plan: string, extra?: string) => string;
}) {
  const lang = zh ? "zh" : "en";
  const annual = billing === "annual";
  const seatSfx = p.perSeat ? (zh ? " / 坐席" : "/seat") : "";
  const price = p.wallet || p.monthly === 0 ? 0 : annual ? annualTotal(p.monthly) : p.monthly;
  const unit = annual ? (zh ? `USD / 年${seatSfx}` : `USD/yr${seatSfx}`) : zh ? `USD / 月${seatSfx}` : `USD/mo${seatSfx}`;
  const href =
    p.key === "autochat-free"
      ? zh
        ? "/download/chatx"
        : "/en/download/chatx"
      : p.wallet
        ? orderHref("token-pack-m")
        : orderHref(p.key, p.perSeat ? `&seats=${p.perSeat.min}` : "");
  const cta =
    p.key === "autochat-free"
      ? zh
        ? "免费下载"
        : "Download free"
      : p.wallet
        ? zh
          ? "预充 Token 包"
          : "Top up tokens"
        : p.custom
          ? zh
            ? "咨询报价"
            : "Get a quote"
          : zh
            ? "选择此档"
            : "Choose plan";
  return (
    <Reveal eager delay={delay} className="h-full">
      <div
        className={`relative flex h-full flex-col overflow-hidden rounded-2xl border p-5 ${
          p.hot ? "border-transparent bg-ink-800/80" : "border-white/10 bg-ink-900/60"
        }`}
      >
        {p.hot && <BorderBeam />}
        {p.hot && (
          <span className="absolute right-4 top-4 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-2.5 py-0.5 text-[11px] font-semibold text-ink-950">
            {zh ? "最受欢迎" : "Most popular"}
          </span>
        )}
        <div className="font-semibold text-white">{zh ? p.name.zh : p.name.en}</div>
        <div className="mt-0.5 min-h-[2rem] text-xs text-slate-500">{p.audience[lang]}</div>
        <div className="mt-3">
          {p.wallet ? (
            <>
              <span className="text-3xl font-bold tabular-nums text-white">0</span>
              <span className="ml-1.5 text-xs text-slate-500">{zh ? "月费 · 按用量" : "monthly · usage-based"}</span>
            </>
          ) : p.monthly === 0 ? (
            <>
              <span className="text-3xl font-bold text-white">{zh ? "免费" : "Free"}</span>
              <span className="ml-1.5 text-xs text-slate-500">{zh ? "永久" : "forever"}</span>
            </>
          ) : (
            <>
              <span className="text-3xl font-bold tabular-nums text-white">{fmt(price)}</span>
              <span className="ml-1.5 text-xs text-slate-500">{unit}</span>
            </>
          )}
          {p.perSeat && (
            <div className="mt-0.5 text-[11px] text-slate-500">{zh ? `最少 ${p.perSeat.min} 席起` : `min ${p.perSeat.min} seats`}</div>
          )}
        </div>
        <ul className="mt-4 flex-1 space-y-2">
          {p.feats[lang].map((f) => (
            <li key={f} className="flex items-start gap-2 text-xs leading-relaxed text-slate-300">
              <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-neon-cyan" />
              {f}
            </li>
          ))}
        </ul>
        <Link
          href={href}
          onClick={() => track("pricing_plan_cta", { plan: p.key, billing })}
          className={`mt-5 rounded-full py-2 text-center text-sm transition ${
            p.hot
              ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950 hover:opacity-90"
              : "border border-white/15 text-slate-200 hover:border-neon-cyan/50 hover:text-white"
          }`}
        >
          {cta}
        </Link>
      </div>
    </Reveal>
  );
}

/* ── 用量计算器 ── */

function UsageCalculator({
  zh,
  billing,
  orderHref,
}: {
  zh: boolean;
  billing: Billing;
  orderHref: (plan: string, extra?: string) => string;
}) {
  const [replies, setReplies] = useState(100);
  const [proK, setProK] = useState(100);
  const [voiceMsgs, setVoiceMsgs] = useState(20);
  const [images, setImages] = useState(10);
  const [seats, setSeats] = useState(1);

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
  const costs = useMemo(() => comparePlanCosts(tokens, seats), [tokens, seats]);
  const best = useMemo(() => recommendPlan(tokens, seats), [tokens, seats]);

  const planName = (key: string) => {
    const p = CHATX_PLANS.find((x) => x.key === key);
    return p ? (zh ? p.name.zh.split(" ")[0] : p.name.en.split(" (")[0]) : key;
  };

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
    {
      label: zh ? "团队坐席数" : "Team seats",
      value: seats,
      set: setSeats,
      max: 20,
      step: 1,
      hint: zh ? "1 = 单人（个人/按量档可选）" : "1 = solo (Personal / Flex eligible)",
    },
  ];

  return (
    <Reveal className="mt-16">
      <div className="mb-4 flex flex-wrap items-baseline gap-3">
        <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "算一算：我该选哪档" : "Estimate your plan"}</h2>
        <span className="text-xs text-slate-500">
          {zh ? "拖滑块看月用量与各档月成本，推荐档实时高亮" : "Drag the sliders — monthly tokens and per-plan costs update live"}
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
          <div className="rounded-xl border border-white/10 bg-ink-950/50 px-5 py-4">
            <div className="text-xs text-slate-500">{zh ? "预计月用量" : "Estimated monthly usage"}</div>
            <div className="mt-1 text-3xl font-bold tabular-nums text-white">
              {fmt(tokens)} <span className="text-sm font-normal text-slate-500">Token / {zh ? "月" : "mo"}</span>
            </div>
          </div>
          <div className="mt-4 space-y-2">
            {costs
              .filter((c) => c.planKey !== "autochat-flagship" || best.planKey === "autochat-flagship")
              .map((c) => {
                const recommended = c.planKey === best.planKey;
                return (
                  <div
                    key={c.planKey}
                    className={`flex items-center justify-between gap-3 rounded-xl border px-4 py-2.5 text-sm ${
                      recommended ? "border-neon-cyan/50 bg-neon-cyan/[0.07]" : "border-white/10"
                    } ${c.fits ? "" : "opacity-40"}`}
                  >
                    <div className="flex items-center gap-2">
                      <span className={recommended ? "font-semibold text-neon-cyan" : "text-slate-300"}>{planName(c.planKey)}</span>
                      {c.seats > 1 && <span className="text-[11px] text-slate-500">× {c.seats} {zh ? "席" : "seats"}</span>}
                      {recommended && (
                        <span className="rounded-full bg-neon-cyan/20 px-2 py-0.5 text-[10px] font-medium text-neon-cyan">
                          {zh ? "推荐" : "Best fit"}
                        </span>
                      )}
                      {!c.fits && <span className="text-[10px] text-slate-500">{zh ? "坐席不够" : "not enough seats"}</span>}
                    </div>
                    <div className="text-right">
                      <span className={`font-semibold tabular-nums ${recommended ? "text-neon-cyan" : "text-white"}`}>
                        ${fmt(Math.round(c.total))}
                      </span>
                      <span className="ml-1 text-[11px] text-slate-500">/{zh ? "月" : "mo"}</span>
                      {c.topUp > 0 && (
                        <div className="text-[10px] text-slate-500">
                          {zh ? `含加购 Token ≈ $${fmt(Math.round(c.topUp))}` : `incl. ~$${fmt(Math.round(c.topUp))} top-ups`}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
          </div>
          <Link
            href={
              best.planKey === "autochat-free"
                ? zh
                  ? "/download/chatx"
                  : "/en/download/chatx"
                : best.planKey === "autochat-flex"
                  ? orderHref("token-pack-m")
                  : orderHref(best.planKey, best.seats > 1 ? `&seats=${best.seats}` : "")
            }
            onClick={() => track("pricing_calc_cta", { plan: best.planKey, tokens, seats, billing })}
            className="mt-4 block rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet py-2.5 text-center text-sm font-medium text-ink-950 transition hover:opacity-90"
          >
            {zh ? `按推荐去下单：${planName(best.planKey)}` : `Order the best fit: ${planName(best.planKey)}`}
          </Link>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
            {zh
              ? "估算口径与计费口径同一张费率表；标准翻译不计入用量。超出含量按标准包单价折算加购成本。"
              : "Estimates use the same published rate table as billing; standard translation costs nothing. Overages priced at standard-pack unit rate."}
          </p>
        </div>
      </div>
    </Reveal>
  );
}

/* ── FAQ 数据 ── */

function faqItems(zh: boolean): { q: string; a: string }[] {
  return zh
    ? [
        {
          q: "Token 是什么？会不会像话费一样偷偷扣光？",
          a: `Token 是全站统一的 AI 用量单位，每个动作的消耗全部公示在上方费率表（如 AI 回复 10 Token/条）；后台会员中心实时显示余额与流水，余额低于 20% 会主动提醒。订阅含量当月有效，Token 包 ${TOKEN_PACK_VALID_MONTHS} 个月有效。`,
        },
        {
          q: "Token 用完了会断线吗？",
          a: "不会。用尽后 AI 回复自动切换到本地免费模型、专业翻译降级为标准翻译（仍然免费不限量），会话永不中断；补充任意 Token 包立即恢复完整能力。",
        },
        {
          q: "标准翻译真的免费？上限在哪里？",
          a: `真的免费、不限档位：内置引擎多平台双向翻译不限字符，仅设防滥用的公平使用上限（${(FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY / 10000).toFixed(0)} 万字符/日/授权，正常团队远用不到）。术语锁定、翻译记忆、DeepL 认证、图片语音翻译属专业翻译，按 Token 计量。`,
        },
        {
          q: "个人版和团队版怎么选？",
          a: "一个人用选个人版（3 账号全平台 + 每月 30,000 Token）；两人以上选团队版，按坐席计价（49 USD/坐席/月，最少 2 席），每席带 5 个账号与 50,000 Token 入池共享，另有权限、审计与团队看板。",
        },
        {
          q: "按量版（Flex）适合谁？",
          a: "用量波动大或想先小额试水的用户：0 月费，功能对齐个人版，预充任意 Token 包即可开用，用多少扣多少，Token 12 个月有效。",
        },
        {
          q: "我是老客户（入门版 / 团队版 / 字符包），怎么办？",
          a: "存量订阅按原价服务到期，到期续费自动享受新档位；字符包未用完的字符按 150 万字符 = 60,000 Token 免费换发（价值只多不少）。有疑问随时找客服核对。",
        },
        {
          q: "年付怎么算？",
          a: "全线统一：年付 = 月价 × 10，即送 2 个月；季付 = 月价 × 3。不再有其它折扣口径。",
        },
        {
          q: "支持哪些支付方式？发票 / 对公怎么办？",
          a: "自助下单支持 USDT（TRC20）与银行卡（Stripe）；到账自动开通。企业对公、大额或定制方案请联系官方 Telegram 客服。",
        },
      ]
    : [
        {
          q: "What exactly is a token? Will it drain silently?",
          a: `Tokens are the single usage unit across the product. Every action's cost is published in the rate table above (e.g. an AI reply costs 10). The membership center shows your live balance and ledger, and we alert you below 20%. Plan allowances reset monthly; packs stay valid ${TOKEN_PACK_VALID_MONTHS} months.`,
        },
        {
          q: "What happens when tokens run out?",
          a: "Nothing breaks. AI replies fall back to the free local model and pro translation degrades to standard translation (still free and unlimited). Top up any pack to restore full capability instantly.",
        },
        {
          q: "Is standard translation really free? Where's the catch?",
          a: `Free in every plan: built-in engine, two-way, unlimited characters — with only an anti-abuse fair-use cap of ${(FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY / 1000000).toFixed(0)}M chars/day per license. Term-lock, memory, certified DeepL and multimodal translate are pro features metered in tokens.`,
        },
        {
          q: "Personal or Team?",
          a: "Solo → Personal (3 accounts, all platforms, 30,000 tokens/mo). Two or more people → Team, priced per seat ($49/seat/mo, 2-seat minimum) with 5 accounts and 50,000 pooled tokens per seat, plus roles, audit and team dashboards.",
        },
        {
          q: "Who is Flex for?",
          a: "Spiky or trial usage: no monthly fee, Personal-level features, prepay any token pack and spend as you go. Tokens stay valid 12 months.",
        },
        {
          q: "I'm on a legacy plan (Entry / Team / char pack) — what now?",
          a: "Active subscriptions run to term at the old price; renewals move to the new plans. Unused char-pack balances convert free at 1.5M chars = 60,000 tokens — always in your favor. Ping support with any question.",
        },
        {
          q: "How does annual billing work?",
          a: "One formula everywhere: annual = 10 × monthly (2 months free); quarterly = 3 × monthly. No other discount schemes.",
        },
        {
          q: "Payment methods? Invoices?",
          a: "Self-serve checkout takes USDT (TRC20) and cards (Stripe), with automatic activation. For corporate invoicing or large deals, contact official Telegram support.",
        },
      ];
}
