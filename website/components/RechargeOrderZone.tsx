"use client";

// /order「智聊 ChatX · 充值」家族专属渲染区（2026-08 充值定价页美化）：
// 通用 TierCard 的 bullet 模板把充值档的数字叙事抹平了——本组件做三件事：
//   ① 差异信息前置：首充到账大数字（CountUp）+ 加赠发光徽章 + 单价 + 价值翻译行
//      （≈ 多少条 AI 回复），共性条款（有效期/同一钱包/凭证）下沉为区尾一行；
//   ② 新人 6U 从网格末位抽出为置顶金色横幅（ShineCard 3D 倾斜 + 光泽跟随）；
//   ③ 首充加赠能量阶梯（BonusLadder）选档联动 + 升档提示。
// 数字零手写：全部派生自 lib/chatx-pricing.ts 单源；选中态 key 仍是 recharge-*
// —— 深链 ?plan= 与埋点维度（order_tier）零变化。StickyOrderBar 见文件尾。
import { useEffect, useState } from "react";
import { Crown, Gift, ShieldCheck, Wallet, Zap } from "lucide-react";
import Reveal from "./fx/Reveal";
import BorderBeam from "./fx/BorderBeam";
import Burst from "./fx/Burst";
import CountUp from "./fx/CountUp";
import ShineCard from "./fx/ShineCard";
import BonusLadder from "./BonusLadder";
import { track } from "@/lib/track";
import {
  BONUS_VALID_MONTHS,
  NEWBIE_PACK,
  RECHARGE_TIERS,
  RECHARGE_TOKENS_PER_USD,
  VIP_REPEAT_BONUS_TIERS,
  rechargeBaseTokens,
  rechargeFirstTokens,
  rechargeUnitPrice,
  rechargeValidMonths,
  tokenRate,
  type RechargeTier,
} from "@/lib/chatx-pricing";

const fmt = (n: number) => n.toLocaleString("en-US");

export default function RechargeOrderZone({
  zh,
  selected,
  onSelect,
}: {
  zh: boolean;
  /** 当前选中档 key（与 OrderPanel 的 selected 状态同源） */
  selected: string;
  onSelect: (key: string) => void;
}) {
  // 选中仪式感：仅用户主动点选时爆一次粒子（初始默认选中不爆）
  const [burst, setBurst] = useState<{ key: string; n: number }>({ key: "", n: 0 });
  const pick = (key: string, via: "card" | "ladder" | "upsell" | "newbie") => {
    if (key !== selected) setBurst((b) => ({ key, n: b.n + 1 }));
    onSelect(key);
    if (via === "newbie") track("order_newbie_banner", {});
    if (via === "upsell") track("order_ladder_upsell", { to: key });
  };

  const aiReplyTokens = tokenRate("ai_reply").tokens;
  const newbieSel = selected === NEWBIE_PACK.key;
  const newbieReplies = Math.round(NEWBIE_PACK.tokens / aiReplyTokens);
  const vipTop = VIP_REPEAT_BONUS_TIERS[VIP_REPEAT_BONUS_TIERS.length - 1].pct;

  return (
    <>
      {/* ── 一次性计费徽章行（替代对充值档无意义的月/季/年切换器） ── */}
      <Reveal eager className="mt-6 flex justify-center">
        <div className="glass inline-flex max-w-full flex-wrap items-center justify-center gap-x-5 gap-y-1.5 rounded-full border border-white/10 px-5 py-2 text-xs text-slate-300">
          <span className="flex items-center gap-1.5">
            <Zap className="h-3.5 w-3.5 shrink-0 text-neon-cyan" />
            {zh ? "一次性充值 · 不订阅" : "One-time top-up · no subscription"}
          </span>
          <span className="flex items-center gap-1.5">
            <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-300" />
            {zh ? "永不自动扣费" : "Never auto-charged"}
          </span>
          <span className="flex items-center gap-1.5">
            <Wallet className="h-3.5 w-3.5 shrink-0 text-neon-violet" />
            {zh ? `1U = ${fmt(RECHARGE_TOKENS_PER_USD)} Token` : `1U = ${fmt(RECHARGE_TOKENS_PER_USD)} tokens`}
          </span>
        </div>
      </Reveal>

      {/* ── 新人 6U 大礼包 · 置顶金色横幅（keep-dark：日间模式仍是深色广告牌） ── */}
      <Reveal eager className="mt-8">
        <ShineCard
          shineRgb="252,211,77"
          className={`keep-dark relative rounded-3xl border bg-gradient-to-br from-[#241a33] via-ink-900 to-[#12233a] ${
            newbieSel ? "border-amber-300/70 shadow-[0_0_40px_-10px_rgba(252,211,77,0.35)]" : "border-amber-300/35"
          }`}
        >
          {newbieSel && <BorderBeam gold />}
          <div className="pointer-events-none absolute -right-20 -top-24 h-64 w-64 rounded-full bg-amber-300/10 blur-[90px]" />
          <button
            type="button"
            onClick={() => pick(NEWBIE_PACK.key, "newbie")}
            aria-pressed={newbieSel}
            className="relative flex w-full flex-wrap items-center gap-x-8 gap-y-4 px-6 py-6 text-left md:px-9"
          >
            {newbieSel && burst.key === NEWBIE_PACK.key && burst.n > 0 && <Burst key={burst.n} />}
            <div className="flex min-w-[230px] flex-1 flex-col">
              <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-amber-300/15 px-3 py-1 text-xs font-medium text-amber-300">
                <Gift className="h-3.5 w-3.5" />
                {zh
                  ? `新人专享 · 注册 ${NEWBIE_PACK.windowHours} 小时内 · 每账号一次`
                  : `Newcomers only · within ${NEWBIE_PACK.windowHours}h of signup · once per account`}
              </span>
              <div className="mt-3 flex flex-wrap items-end gap-x-3 gap-y-1">
                <span className="text-4xl font-bold tabular-nums text-white md:text-5xl">{NEWBIE_PACK.price}U</span>
                <span className="pb-1 text-xl font-bold text-amber-300">→</span>
                <span className="text-3xl font-bold tabular-nums text-amber-300 md:text-4xl">
                  <CountUp value={String(NEWBIE_PACK.tokens)} grouping />
                </span>
                <span className="pb-1 text-sm text-slate-400">Token</span>
              </div>
              <p className="mt-2 max-w-xl text-xs leading-relaxed text-slate-300 md:text-sm">
                {zh
                  ? `双倍到账（$${rechargeUnitPrice(NEWBIE_PACK.price, NEWBIE_PACK.tokens)}/千 · 全场最低单价）≈ ${fmt(newbieReplies)} 条 AI 回复；不占用首充加赠资格。`
                  : `Double rate ($${rechargeUnitPrice(NEWBIE_PACK.price, NEWBIE_PACK.tokens)}/1k — the lowest unit price here) ≈ ${fmt(newbieReplies)} AI replies. Doesn't consume your first-top-up bonus.`}
              </p>
            </div>
            <span
              className={`shrink-0 rounded-full px-6 py-2.5 text-center text-sm font-semibold transition ${
                newbieSel
                  ? "bg-gradient-to-r from-amber-300 to-amber-400 text-ink-950 shadow-[0_0_24px_rgba(252,211,77,0.3)]"
                  : "border border-amber-300/50 text-amber-300"
              }`}
            >
              {newbieSel ? (zh ? "已选择 ✓" : "Selected ✓") : zh ? `选择 ${NEWBIE_PACK.price}U 礼包` : `Pick the ${NEWBIE_PACK.price}U pack`}
            </span>
          </button>
        </ShineCard>
      </Reveal>

      {/* ── 首充加赠能量阶梯（选档联动 + 升档提示） ── */}
      <Reveal eager className="mt-9">
        <div className="mb-3 flex items-baseline justify-between gap-3">
          <div className="text-sm font-semibold text-white">
            {zh ? "首充加赠阶梯 · 每人一次" : "First-top-up bonus ladder — once per person"}
          </div>
          <div className="hidden text-[11px] text-slate-500 sm:block">
            {zh ? "充得越多，加赠越高、单价越低" : "Bigger tiers, bigger bonus, lower unit price"}
          </div>
        </div>
        <BonusLadder
          zh={zh}
          selectedKey={selected}
          onSelect={(k) => pick(k, "ladder")}
          onUpsell={(k) => pick(k, "upsell")}
        />
      </Reveal>

      {/* ── 七档充值卡（4+3 网格；差异信息前置） ── */}
      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {RECHARGE_TIERS.map((t, i) => (
          <RechargeCard
            key={t.key}
            tier={t}
            zh={zh}
            delay={i * 0.04}
            selected={selected === t.key}
            showBurst={burst.key === t.key ? burst.n : 0}
            onPick={() => pick(t.key, "card")}
          />
        ))}
      </div>

      {/* ── 共性条款下沉（原每张卡重复 2 条 × 8 卡 → 全区一处） ── */}
      <Reveal eager className="mt-6">
        <p className="mx-auto max-w-4xl text-center text-[11px] leading-relaxed text-slate-500">
          {zh
            ? `实付 Token 12 个月有效（500U 及以上档 24 个月）· 赠送部分 ${BONUS_VALID_MONTHS} 个月且先扣 · 跨智聊 / 通译同一钱包 · 会员中心粘贴凭证即到账`
            : `Paid tokens valid 12 months (24 for 500U+) · bonus tokens ${BONUS_VALID_MONTHS} months, spend first · one wallet across ChatX & LingoX · redeem in the membership center`}
        </p>
        <p className="mx-auto mt-1.5 flex max-w-4xl flex-wrap items-center justify-center gap-x-4 gap-y-1 text-center text-[11px] leading-relaxed text-slate-500">
          <span className="inline-flex items-center gap-1 text-violet-300/90">
            <Crown className="h-3 w-3" />
            {zh ? "VIP 累充复充加赠" : "VIP loyalty on repeats"}
          </span>
          {VIP_REPEAT_BONUS_TIERS.map((v) => (
            <span key={v.fromUsd} className="tabular-nums">
              {zh ? `累计 ≥ ${fmt(v.fromUsd)}U → 复充 +${v.pct}%` : `lifetime ≥ ${fmt(v.fromUsd)}U → +${v.pct}%`}
            </span>
          ))}
          <span>{zh ? "自动生效 · 与首充加赠不叠加" : "applies automatically · doesn't stack with the first-top-up bonus"}</span>
        </p>
      </Reveal>
    </>
  );
}

/* ── 单张充值卡：主数字=首充到账（CountUp）+ 价值翻译行 + 大额档权益徽章 ── */

function RechargeCard({
  tier: t,
  zh,
  selected,
  showBurst,
  delay,
  onPick,
}: {
  tier: RechargeTier;
  zh: boolean;
  selected: boolean;
  /** >0 时按该代际重放一次选中粒子爆发 */
  showBurst: number;
  delay: number;
  onPick: () => void;
}) {
  const first = rechargeFirstTokens(t);
  const base = rechargeBaseTokens(t);
  const replies = Math.round(first / tokenRate("ai_reply").tokens);
  const months = rechargeValidMonths(t);

  return (
    <Reveal eager delay={delay} className="h-full">
      <ShineCard
        maxTiltX={4}
        maxTiltY={5}
        shineRgb={t.perks ? "252,211,77" : "103,232,249"}
        shineOpacity={0.1}
        className={`relative h-full rounded-2xl border ${
          selected
            ? `ring-breathe ${t.hot ? "plan-featured border-transparent" : "border-neon-cyan/60 bg-ink-800/80"}`
            : t.hot
              ? "plan-featured border-transparent"
              : "border-white/10 bg-ink-900/60 hover:border-neon-cyan/30"
        }`}
      >
        {t.hot && <BorderBeam />}
        {t.perks && selected && <BorderBeam gold />}
        {/* 大额档金色「披肩」：常驻用静态渐变，旋转描边只在选中时上（防三环互抢） */}
        {t.perks && (
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 top-0 h-20 rounded-t-2xl bg-gradient-to-b from-amber-300/[0.12] to-transparent"
          />
        )}
        {t.hot && (
          <span className="absolute -top-2.5 right-4 z-10 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-2.5 py-0.5 text-[11px] font-semibold text-ink-950">
            {zh ? "最受欢迎" : "Most popular"}
          </span>
        )}
        {selected && showBurst > 0 && <Burst key={showBurst} />}
        <button
          type="button"
          onClick={onPick}
          aria-pressed={selected}
          className="relative flex h-full w-full flex-col p-5 text-left"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="text-sm font-semibold text-white">{zh ? t.name.zh : t.name.en}</div>
            {t.firstBonusPct > 0 ? (
              <span className="shrink-0 rounded-full bg-neon-cyan/15 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-neon-cyan shadow-[0_0_12px_rgba(34,211,238,0.25)]">
                {zh ? `首充 +${t.firstBonusPct}%` : `First +${t.firstBonusPct}%`}
              </span>
            ) : (
              <span className="shrink-0 rounded-full border border-white/15 px-2 py-0.5 text-[10px] text-slate-500">
                {zh ? "基准价" : "base rate"}
              </span>
            )}
          </div>

          <div className="mt-4">
            <div className="text-[11px] text-slate-500">{zh ? "首充到账" : "First top-up credits"}</div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              <span className="text-gradient text-2xl font-bold tabular-nums md:text-3xl">
                <CountUp value={String(first)} grouping />
              </span>
              <span className="text-xs text-slate-500">Token</span>
            </div>
            <div className="mt-1 text-xs tabular-nums text-slate-400">
              {fmt(t.price)} USD{zh ? " · 一次性" : " one-time"} · ${rechargeUnitPrice(t.price, first)}/{zh ? "千" : "1k"}
            </div>
          </div>

          <ul className="mt-4 flex-1 space-y-1.5 text-xs text-slate-300">
            <li className="tabular-nums">{zh ? `≈ ${fmt(replies)} 条 AI 回复` : `≈ ${fmt(replies)} AI replies`}</li>
            <li className="tabular-nums">
              {zh ? `复充 ${fmt(base)} 起 · VIP 最高 +` : `Repeat from ${fmt(base)} · VIP up to +`}
              {VIP_REPEAT_BONUS_TIERS[VIP_REPEAT_BONUS_TIERS.length - 1].pct}%
            </li>
            <li>{zh ? `实付 ${months} 个月有效` : `Paid tokens valid ${months} months`}</li>
          </ul>

          {t.perks && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {(zh ? t.perks.zh : t.perks.en).map((p) => (
                <span
                  key={p}
                  className="rounded-full border border-amber-300/30 bg-amber-300/10 px-2 py-0.5 text-[10px] text-amber-300"
                >
                  {p}
                </span>
              ))}
            </div>
          )}

          <div
            className={`mt-5 rounded-full py-2 text-center text-sm transition ${
              selected
                ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                : "border border-white/15 text-slate-300"
            }`}
          >
            {selected ? (zh ? "已选择" : "Selected") : zh ? "选择" : "Select"}
          </div>
        </button>
      </ShineCard>
    </Reveal>
  );
}

/* ── 吸底结算条：已选档 + 到账数（CountUp 重滚）+ 信任行 + cta-fx CTA ──
 * 挂载期在 <html> 打 data-order-bar，全局移动粘性条（.sticky-cta）自动让位。 */

export function StickyOrderBar({
  zh,
  selected,
  hidden,
  onOrder,
}: {
  zh: boolean;
  selected: string;
  /** 结算弹窗打开期间隐藏（滑出屏幕），避免叠在弹窗底部 */
  hidden: boolean;
  onOrder: () => void;
}) {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setShown(true));
    return () => cancelAnimationFrame(id);
  }, []);
  useEffect(() => {
    document.documentElement.setAttribute("data-order-bar", "1");
    return () => document.documentElement.removeAttribute("data-order-bar");
  }, []);

  const tier = RECHARGE_TIERS.find((t) => t.key === selected) ?? null;
  const isNewbie = selected === NEWBIE_PACK.key;
  if (!tier && !isNewbie) return null;

  const name = isNewbie
    ? zh
      ? `${NEWBIE_PACK.name.zh} ${NEWBIE_PACK.price}U`
      : `${NEWBIE_PACK.name.en} ${NEWBIE_PACK.price}U`
    : zh
      ? tier!.name.zh
      : tier!.name.en;
  const tokens = isNewbie ? NEWBIE_PACK.tokens : rechargeFirstTokens(tier!);
  const price = isNewbie ? NEWBIE_PACK.price : tier!.price;

  return (
    <div
      className={`order-bar fixed inset-x-0 bottom-0 z-[var(--z-sticky)] ${
        shown && !hidden ? "translate-y-0" : "translate-y-full"
      }`}
    >
      <div className="glass border-t border-white/10 px-4 py-2.5 pb-[calc(0.625rem+env(safe-area-inset-bottom))]">
        <div className="mx-auto flex max-w-7xl items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs text-slate-300">
              <span className="font-medium text-white">{name}</span>
              <span className="mx-1.5 text-slate-600">·</span>
              {zh ? "到账 " : "credits "}
              <b className="tabular-nums text-neon-cyan">
                <CountUp value={String(tokens)} grouping duration={0.9} />
              </b>{" "}
              Token
            </div>
            <div className="mt-0.5 hidden text-[10px] text-slate-500 md:block">
              {zh
                ? "到账自动开通 ≈5 分钟 · USDT / 银行卡 · 单号随时可查 · 用尽自动降级永不断线"
                : "Auto-activation in ~5 min · USDT / card · self-serve order tracking · graceful fallback, never offline"}
            </div>
          </div>
          <button
            onClick={onOrder}
            className="cta-fx flex min-h-[44px] shrink-0 items-center justify-center rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-semibold text-ink-950 transition hover:opacity-90"
          >
            {zh ? `立即下单 ${fmt(price)}U` : `Order now — ${fmt(price)}U`}
          </button>
        </div>
      </div>
    </div>
  );
}
