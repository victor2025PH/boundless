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
import { Crown, Gift } from "lucide-react";
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
  VIP_REPEAT_BONUS_TIERS,
  rechargeBaseTokens,
  rechargeFirstTokens,
  rechargeUnitPrice,
  rechargeValidMonths,
  tokenRate,
  type RechargeTier,
} from "@/lib/chatx-pricing";

const fmt = (n: number) => n.toLocaleString("en-US");

/** 首充 / 复充视角：卡片主数字、结算明细、吸底条同口径切换（OrderPanel 持有状态）。 */
export type CreditView = "first" | "repeat";

export default function RechargeOrderZone({
  zh,
  selected,
  view = "first",
  regTs = 0,
  onViewChange,
  onSelect,
}: {
  zh: boolean;
  /** 当前选中档 key（与 OrderPanel 的 selected 状态同源） */
  selected: string;
  /** 首充 / 复充视角（默认首充；复充老客切换后全区数字换复充口径——诚实的数字才留得住老客） */
  view?: CreditView;
  /** 注册时间锚（ms，来自 ?reg_ts= 深链 / localStorage）：>0 时金卡出 72h 真倒计时，
   *  超窗出诚实提示引导首充档；0 = 匿名访客，不出任何倒计时（绝不做假倒计时）。 */
  regTs?: number;
  onViewChange?: (v: CreditView) => void;
  onSelect: (key: string) => void;
}) {
  // 选中仪式感：仅用户主动点选时爆一次粒子（初始默认选中不爆）
  const [burst, setBurst] = useState<{ key: string; n: number }>({ key: "", n: 0 });
  // 移动端档位折叠：单列 8 卡太长，默认露 4 档；深链选中折叠区档位时自动展开
  const [showAllM, setShowAllM] = useState(false);
  useEffect(() => {
    if (RECHARGE_TIERS.findIndex((t) => t.key === selected) >= 4) setShowAllM(true);
  }, [selected]);
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
      {/* （原「一次性计费徽章行」已删：三条信息全部上移进首屏 Hero 的动态徽章与数据卡，
          同屏重复三遍只会稀释新人金卡的视觉焦点。） */}

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
              <span className="flex flex-wrap items-center gap-2">
                <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-amber-300/15 px-3 py-1 text-xs font-medium text-amber-300">
                  <Gift className="h-3.5 w-3.5" />
                  {zh
                    ? `新人专享 · 注册 ${NEWBIE_PACK.windowHours} 小时内 · 每账号一次`
                    : `Newcomers only · within ${NEWBIE_PACK.windowHours}h of signup · once per account`}
                </span>
                {regTs > 0 && <NewbieCountdown regTs={regTs} zh={zh} />}
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

      {/* ── 首充加赠能量阶梯（选档联动 + 升档提示）／复充视角换 VIP 累充条 ── */}
      <Reveal eager className="mt-9">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <div className="text-sm font-semibold text-white">
            {view === "first"
              ? zh
                ? "首充加赠阶梯 · 每人一次"
                : "First-top-up bonus ladder — once per person"
              : zh
                ? "VIP 累充加赠 · 自动生效"
                : "VIP loyalty bonus — applies automatically"}
          </div>
          {onViewChange && (
            <div className="glass inline-flex rounded-full border border-white/10 p-0.5">
              {(["first", "repeat"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => onViewChange(v)}
                  aria-pressed={view === v}
                  className={`rounded-full px-3.5 py-1 text-xs transition ${
                    view === v
                      ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                      : "text-slate-400 hover:text-white"
                  }`}
                >
                  {v === "first" ? (zh ? "🎁 我是首充" : "🎁 First top-up") : zh ? "🔄 我是复充" : "🔄 Repeat"}
                </button>
              ))}
            </div>
          )}
        </div>
        {view === "first" ? (
          <BonusLadder
            zh={zh}
            selectedKey={selected}
            onSelect={(k) => pick(k, "ladder")}
            onUpsell={(k) => pick(k, "upsell")}
          />
        ) : (
          <VipLoyaltyStrip zh={zh} />
        )}
      </Reveal>

      {/* ── 七档充值卡（4+3 网格；差异信息前置；移动端默认露 4 档可展开） ── */}
      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {RECHARGE_TIERS.map((t, i) => (
          <RechargeCard
            key={t.key}
            tier={t}
            zh={zh}
            view={view}
            delay={i * 0.04}
            selected={selected === t.key}
            showBurst={burst.key === t.key ? burst.n : 0}
            onPick={() => pick(t.key, "card")}
            className={i >= 4 && !showAllM ? "hidden sm:block" : undefined}
          />
        ))}
      </div>
      {!showAllM && (
        <div className="mt-4 flex justify-center sm:hidden">
          <button
            type="button"
            onClick={() => {
              setShowAllM(true);
              track("order_show_all_tiers", {});
            }}
            className="inline-flex items-center gap-1 rounded-full border border-white/15 px-4 py-2 text-xs text-slate-300 transition hover:border-neon-cyan/50 hover:text-white"
          >
            {zh ? `展开全部 ${RECHARGE_TIERS.length} 个充值档 ▾` : `Show all ${RECHARGE_TIERS.length} tiers ▾`}
          </button>
        </div>
      )}

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

/* ── 新人 6U 真倒计时（仅 reg_ts 锚存在时渲染；state 隔离，每秒 tick 不重渲染金卡）──
 * 剩余 >0：红色紧迫钟 HH:MM:SS；超窗：诚实黄字（口径与服务端拒单文案一致，引导首充档）。
 * 纯展示——资格终审在履约端 72h 窗（锚=最早 trial claim），改参数骗不到加赠。 */

function NewbieCountdown({ regTs, zh }: { regTs: number; zh: boolean }) {
  const deadline = regTs + NEWBIE_PACK.windowHours * 3_600_000;
  const [left, setLeft] = useState(() => deadline - Date.now());
  useEffect(() => {
    const id = setInterval(() => setLeft(deadline - Date.now()), 1000);
    return () => clearInterval(id);
  }, [deadline]);
  useEffect(() => {
    // 挂载各打一次曝光（剩余小时分桶 / 超窗），供漏斗判断「倒计时有没有催单效果」
    if (deadline - Date.now() > 0) {
      track("order_newbie_countdown", { hours_left: Math.floor((deadline - Date.now()) / 3_600_000) });
    } else {
      track("order_newbie_window_passed_view", {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (left <= 0) {
    return (
      <span className="inline-flex w-fit items-center gap-1 rounded-full bg-amber-300/10 px-2.5 py-1 text-[11px] text-amber-300">
        {zh ? "6U 窗口已过 · 首充仍享最高 +40%" : "6U window passed — first top-up still earns up to +40%"}
      </span>
    );
  }
  const pad = (x: number) => String(x).padStart(2, "0");
  const h = Math.floor(left / 3_600_000);
  const m = Math.floor(left / 60_000) % 60;
  const s = Math.floor(left / 1_000) % 60;
  return (
    <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-rose-400/15 px-2.5 py-1 text-xs font-semibold tabular-nums text-rose-300">
      ⏳ {zh ? "限时剩余" : "Ends in"} {pad(h)}:{pad(m)}:{pad(s)}
    </span>
  );
}

/* ── VIP 累充加赠条（复充视角替换首充阶梯）：三档门槛可视化，数据单源 ── */

function VipLoyaltyStrip({ zh }: { zh: boolean }) {
  return (
    <div className="glass rounded-2xl border border-white/10 px-5 py-4">
      <div className="flex flex-wrap items-center gap-x-7 gap-y-2.5">
        {VIP_REPEAT_BONUS_TIERS.map((v) => (
          <div key={v.fromUsd} className="flex items-center gap-2">
            <Crown className="h-4 w-4 shrink-0 text-violet-300" />
            <span className="text-sm tabular-nums text-slate-300">
              {zh ? `累计 ≥ ${fmt(v.fromUsd)}U` : `lifetime ≥ ${fmt(v.fromUsd)}U`}
            </span>
            <span className="rounded-full bg-neon-violet/15 px-2 py-0.5 text-xs font-semibold tabular-nums text-violet-300">
              {zh ? `复充 +${v.pct}%` : `+${v.pct}% repeats`}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
        {zh
          ? "按累计已付充值自动生效，无需申请；与首充加赠不叠加。下方卡片主数字已切换为复充口径。"
          : "Applies automatically by lifetime paid top-ups — no signup, doesn't stack with the first-top-up bonus. Card figures below now show repeat-rate credits."}
      </p>
    </div>
  );
}

/* ── 单张充值卡：主数字=到账 Token（随首充/复充视角，CountUp）+ 价值翻译行 + 大额档权益徽章 ── */

function RechargeCard({
  tier: t,
  zh,
  view,
  selected,
  showBurst,
  delay,
  onPick,
  className,
}: {
  tier: RechargeTier;
  zh: boolean;
  view: CreditView;
  selected: boolean;
  /** >0 时按该代际重放一次选中粒子爆发 */
  showBurst: number;
  delay: number;
  onPick: () => void;
  /** 移动端折叠用（hidden sm:block） */
  className?: string;
}) {
  const first = rechargeFirstTokens(t);
  const base = rechargeBaseTokens(t);
  const shown = view === "repeat" ? base : first;
  const replies = Math.round(shown / tokenRate("ai_reply").tokens);
  const months = rechargeValidMonths(t);
  const vipTop = VIP_REPEAT_BONUS_TIERS[VIP_REPEAT_BONUS_TIERS.length - 1].pct;

  return (
    <Reveal eager delay={delay} className={`h-full ${className ?? ""}`}>
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
            {view === "repeat" ? (
              <span className="shrink-0 rounded-full bg-neon-violet/15 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-violet-300">
                {zh ? `VIP 最高 +${vipTop}%` : `VIP up to +${vipTop}%`}
              </span>
            ) : t.firstBonusPct > 0 ? (
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
            <div className="text-[11px] text-slate-500">
              {view === "repeat" ? (zh ? "复充到账" : "Repeat top-up credits") : zh ? "首充到账" : "First top-up credits"}
            </div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              <span className="text-gradient text-2xl font-bold tabular-nums md:text-3xl">
                <CountUp value={String(shown)} grouping />
              </span>
              <span className="text-xs text-slate-500">Token</span>
            </div>
            <div className="mt-1 text-xs tabular-nums text-slate-400">
              {fmt(t.price)} USD{zh ? " · 一次性" : " one-time"} · ${rechargeUnitPrice(t.price, shown)}/{zh ? "千" : "1k"}
            </div>
          </div>

          <ul className="mt-4 flex-1 space-y-1.5 text-xs text-slate-300">
            <li className="tabular-nums">{zh ? `≈ ${fmt(replies)} 条 AI 回复` : `≈ ${fmt(replies)} AI replies`}</li>
            <li className="tabular-nums">
              {view === "repeat"
                ? zh
                  ? `VIP 累充自动加赠 · 最高 +${vipTop}%`
                  : `VIP loyalty adds up to +${vipTop}% automatically`
                : zh
                  ? `复充 ${fmt(base)} 起 · VIP 最高 +${vipTop}%`
                  : `Repeat from ${fmt(base)} · VIP up to +${vipTop}%`}
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
  view = "first",
  hidden,
  onOrder,
}: {
  zh: boolean;
  selected: string;
  /** 首充 / 复充视角：到账数与卡片区同口径（防「条上一个数、卡上另一个数」） */
  view?: CreditView;
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
  const tokens = isNewbie
    ? NEWBIE_PACK.tokens
    : view === "repeat"
      ? rechargeBaseTokens(tier!)
      : rechargeFirstTokens(tier!);
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
