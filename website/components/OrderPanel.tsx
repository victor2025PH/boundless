"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import { QRCodeSVG } from "qrcode.react";
import { BadgeCheck, Check, Copy, KeyRound, ShieldCheck, Sparkles, Timer, Wallet, X } from "lucide-react";
import { useLang } from "./LanguageContext";
import OrderStatusLookup from "./OrderStatusLookup";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";
import { BRAND_FILM } from "@/lib/film";
import { BOT_HANDLE, CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";
import {
  ACCESSORIES,
  CAPABILITY_TIERS,
  HARDWARE,
  REMOTE_INSTALL,
  SHOWCASE_VIDEOS,
  TIERS,
  USDT_ADDR,
  studioTier,
  tierPrice,
  tierPriceLabel,
  tierShortName,
  type Period,
} from "@/lib/avatarhub-pricing";
import {
  CHATX_FREE_HINT,
  FAMILIES,
  familyDefaultTier,
  familyOfPlan,
  familyTiers,
  resolvePlanAlias,
  type LineTier,
  type OrderFamily,
} from "@/lib/order-lines";

const fmt = (n: number) => n.toLocaleString("en-US");

/** 交付形态（仅智聊 ChatX 主套餐）：installed=装机授权码（默认）；hosted=云端托管实例。 */
type Delivery = "installed" | "hosted";

/** 家族通用价格：一次性商品（Token 包）不随周期变价；其余走 tierPrice（年付 ×10）；
 *  按坐席档（团队版/工作台）= 单价 × 坐席数。 */
function linePrice(t: LineTier, period: Period, seats = 1): number {
  const unit = t.oneTime ? t.monthly : tierPrice(t, period);
  return t.perSeat ? unit * seats : unit;
}

/** 坐席数夹取（档位切换/深链共用）。 */
function clampSeats(t: LineTier, n: number): number {
  if (!t.perSeat) return 1;
  return Math.min(t.perSeat.max, Math.max(t.perSeat.min, Math.round(n) || t.perSeat.min));
}

export default function OrderPanel() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [family, setFamily] = useState<OrderFamily>("avatarhub");
  const [period, setPeriod] = useState<Period>("monthly");
  const [selected, setSelected] = useState("pro");
  // 坐席数（仅 perSeat 档生效；档位切换按各档下限重置，深链 ?seats= 预填）
  const [seats, setSeats] = useState(2);
  // 交付形态：仅 chatx 产品线可选「云端托管」；切走产品线自动回落装机，绝不让
  // 非托管 SKU 带 hosted 标提交（履约分流以此字段为准）。
  const [delivery, setDelivery] = useState<Delivery>("installed");
  const [checkout, setCheckout] = useState(false);
  const [prefillFp, setPrefillFp] = useState("");
  // 会话归因串（AI 坐席聊天里发的下单链接带 ?ref=<会话id>）：静默随单提交，
  // 供 chengjie 引擎按 ref 自动结算营销目标；对用户不可见不可编辑。
  const [prefillRef, setPrefillRef] = useState("");
  // 深链预填完成前不写回 URL，避免首帧用默认 pro 盖掉 ?plan=autochat-entry。
  const urlReady = useRef(false);

  const tiers = useMemo(() => familyTiers(family, TIERS), [family]);
  const tier = useMemo<LineTier>(
    () => tiers.find((t) => t.key === selected) ?? tiers[0],
    [tiers, selected],
  );
  const familyMeta = FAMILIES.find((f) => f.key === family) ?? FAMILIES[0];

  // 深链预填：/order?plan=<档位|offer id>&period=quarterly|annual&fp=<指纹>——plan 跨三条产品线检索，
  // 命中即自动切到所属产品线（产品页「购买」按钮带参直达，见 config.example.yaml shop_url 注）。
  // useLayoutEffect（非 useEffect）：家族切换必须发生在 framer-motion 注册视口观察器之前，
  // 否则挂载后立刻换掉子树会让全页 whileInView 观察失效 → 面板整段卡在 opacity:0（实测）。
  useLayoutEffect(() => {
    const q = new URLSearchParams(window.location.search);
    // 停售档深链平移（LEGACY_PLAN_MAP）：老链接落到承接档，绝不落空
    const plan = resolvePlanAlias(q.get("plan") || "");
    const fam = plan ? familyOfPlan(plan, TIERS) : undefined;
    if (plan && fam) {
      setFamily(fam);
      setSelected(plan);
      const tiersOfFam = familyTiers(fam, TIERS);
      const t = tiersOfFam.find((x) => x.key === plan);
      if (t?.perSeat) setSeats(clampSeats(t, Number(q.get("seats")) || t.perSeat.min));
    }
    const p = q.get("period");
    if (p === "annual" || p === "quarterly") setPeriod(p);
    // 深链 ?delivery=hosted：产品页「云端托管」入口 / AI 坐席链接直达托管下单
    if (q.get("delivery") === "hosted" && (!plan || fam === "chatx")) setDelivery("hosted");
    const fp = q.get("fp");
    if (fp) setPrefillFp(fp.slice(0, 128));
    const ref = q.get("ref");
    if (ref) setPrefillRef(ref.slice(0, 160));
    else {
      // 跨页归因兜底：AI 链接可能先落首页（试算器锚点），root layout 已把
      // ?ref 暂存 localStorage（bl-ref）——7 天内进下单页仍能归因到会话。
      try {
        const v = localStorage.getItem("bl-ref") || "";
        const ts = Number(localStorage.getItem("bl-ref-ts") || 0);
        if (v && ts && Date.now() - ts < 7 * 86400e3) setPrefillRef(v.slice(0, 160));
      } catch {}
    }
    urlReady.current = true;
  }, []);

  // 产品线切走 chatx → 托管选择失效回落装机（hosted 只对 chatx 主套餐有意义）
  useEffect(() => {
    if (family !== "chatx" && delivery === "hosted") setDelivery("installed");
  }, [family, delivery]);

  // 档位切换 → 坐席数按新档下限/上限夹取（非按坐席档归 1）
  useEffect(() => {
    setSeats((s) => clampSeats(tier, s));
  }, [tier]);

  // 选档/周期/交付 → URL 同步（replaceState，可分享/刷新不丢；保留 fp/check 等其它参数）。
  useEffect(() => {
    if (!urlReady.current || typeof window === "undefined") return;
    const u = new URL(window.location.href);
    u.searchParams.set("plan", selected);
    if (period !== "monthly" && !tier.oneTime) u.searchParams.set("period", period);
    else u.searchParams.delete("period");
    if (family === "chatx" && delivery === "hosted") u.searchParams.set("delivery", "hosted");
    else u.searchParams.delete("delivery");
    if (tier.perSeat) u.searchParams.set("seats", String(seats));
    else u.searchParams.delete("seats");
    const next = u.pathname + u.search + u.hash;
    if (next !== window.location.pathname + window.location.search + window.location.hash) {
      window.history.replaceState(null, "", next);
    }
  }, [selected, period, tier.oneTime, tier.perSeat, family, delivery, seats]);

  return (
    <section className="relative pb-24 pt-32">
      <div className="pointer-events-none absolute left-1/4 top-24 h-80 w-80 rounded-full bg-neon-violet/15 blur-[130px]" />
      <div className="pointer-events-none absolute right-1/4 top-96 h-72 w-72 rounded-full bg-neon-cyan/10 blur-[120px]" />

      <div className="relative mx-auto max-w-7xl px-5">
        {/* ── 标题 ── */}
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            <Sparkles className="h-3.5 w-3.5" />
            {zh ? "全程 USDT 结算 · 本地部署数据不出机房" : "Settled in USDT · local deployment, data stays on-prem"}
          </span>
          <h1 className="mt-4 text-3xl font-bold text-white md:text-5xl">
            {zh ? "购买与下单" : "Plans & Ordering"}
          </h1>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh ? familyMeta.blurb.zh : familyMeta.blurb.en}
            {family === "avatarhub" && (
              <>
                {" "}
                <a href={zh ? "/download" : "/en/download"} className="text-neon-cyan hover:underline">
                  {zh ? "前往下载客户端 →" : "Download the client →"}
                </a>
              </>
            )}
          </p>
        </Reveal>

        {/* ── 产品线切换（幻境 STUDIO / 智聊 / 通译）——eager：首屏关键交互不依赖滚动显现 ── */}
        <Reveal eager className="mt-8 flex justify-center">
          <div className="glass inline-flex max-w-full flex-wrap justify-center rounded-full border border-white/10 p-1">
            {FAMILIES.map((f) => (
              <button
                key={f.key}
                onClick={() => {
                  setFamily(f.key);
                  setSelected(familyDefaultTier(f.key, TIERS));
                  track("order_family", { family: f.key });
                }}
                className={`rounded-full px-5 py-2 text-sm transition ${
                  family === f.key
                    ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                    : "text-slate-300 hover:text-white"
                }`}
              >
                {zh ? f.tab.zh : f.tab.en}
              </button>
            ))}
          </div>
        </Reveal>

        {/* ── 翻译免费化公告（2026-08-19 定价改版；通译 tab 专属） ── */}
        {family === "lingox" && (
          <Reveal eager className="mx-auto mt-6 max-w-3xl">
            <div className="glass rounded-2xl border border-emerald-400/30 bg-emerald-400/[0.06] px-5 py-3.5 text-sm leading-relaxed text-slate-300">
              <span className="mr-1.5">🎉</span>
              {zh ? (
                <>
                  <b className="text-emerald-300">标准翻译已永久免费、不限字符</b>
                  ——下载智聊 ChatX 即用，无需购买。原「字符包 / 团队 / 专业」套餐停售：存量订阅服务到期，
                  字符包未用完的字符按 <b className="text-emerald-300">150 万字符 = 60,000 Token</b> 免费换发（只多不少）。
                  专业翻译（术语锁定 / DeepL 认证 / 多模态）按 Token 计量，见「Token 包」。{" "}
                  <a href="/download/chatx" className="text-neon-cyan hover:underline">
                    下载智聊 ChatX →
                  </a>
                </>
              ) : (
                <>
                  <b className="text-emerald-300">Standard translation is now free forever with unlimited characters</b>
                  {" "}— just download ChatX. Legacy char packs & plans are discontinued: active subscriptions run to term, and
                  unused char-pack balances convert to <b className="text-emerald-300">60,000 tokens per 1.5M chars</b> (always in your favor).
                  Pro translation (term-lock / certified DeepL / multimodal) meters in tokens — see Token packs.{" "}
                  <a href="/en/download/chatx" className="text-neon-cyan hover:underline">
                    Download ChatX →
                  </a>
                </>
              )}
            </div>
          </Reveal>
        )}

        {/* ── 免费版指引（智聊 tab 专属）：免费档不出购买卡，下载即用 ── */}
        {family === "chatx" && (
          <Reveal eager className="mx-auto mt-6 max-w-3xl">
            <div className="glass rounded-2xl border border-neon-cyan/25 bg-neon-cyan/[0.05] px-5 py-3.5 text-sm leading-relaxed text-slate-300">
              <span className="mr-1.5">🆓</span>
              {zh ? CHATX_FREE_HINT.zh : CHATX_FREE_HINT.en}{" "}
              <a href={zh ? "/download/chatx" : "/en/download/chatx"} className="text-neon-cyan hover:underline">
                {zh ? "免费下载 →" : "Download free →"}
              </a>
            </div>
          </Reveal>
        )}

        {/* ── 月付 / 季付 / 年付 ── */}
        <Reveal eager className="mt-6 flex flex-col items-center gap-3">
          <div className="glass inline-flex rounded-full border border-white/10 p-1">
            {(["monthly", "quarterly", "annual"] as Period[]).map((p) => (
              <button
                key={p}
                onClick={() => {
                  setPeriod(p);
                  track("order_period", { period: p });
                }}
                className={`rounded-full px-5 py-2 text-sm transition ${
                  period === p
                    ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                    : "text-slate-300 hover:text-white"
                }`}
              >
                {p === "monthly"
                  ? zh ? "月付" : "Monthly"
                  : p === "quarterly"
                    ? zh ? "季付" : "Quarterly"
                    : family === "avatarhub"
                      ? zh ? "年付" : "Annual"
                      : zh ? "年付 · 送 2 个月" : "Annual · 2 months free"}
              </button>
            ))}
          </div>
          {tier.oneTime && (
            <p className="text-xs text-slate-500">
              {zh
                ? "Token 包为一次性加购（12 个月有效），价格不受月/季/年付切换影响"
                : "Token packs are one-time (valid 12 months) — the period toggle does not change the price"}
            </p>
          )}
        </Reveal>

        {/* ── 交付方式（仅智聊 ChatX）：装机授权码 vs 云端托管实例 ── */}
        {family === "chatx" && (
          <Reveal eager className="mt-4 flex flex-col items-center gap-2">
            <div className="glass inline-flex rounded-full border border-white/10 p-1">
              {(
                [
                  { key: "installed", zh: "💻 装机版 · 自己电脑", en: "💻 Self-hosted install" },
                  { key: "hosted", zh: "☁️ 云端托管 · 开通即用", en: "☁️ Cloud hosted · instant" },
                ] as const
              ).map((d) => (
                <button
                  key={d.key}
                  onClick={() => {
                    setDelivery(d.key);
                    track("order_delivery", { delivery: d.key });
                  }}
                  className={`rounded-full px-5 py-2 text-sm transition ${
                    delivery === d.key
                      ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                      : "text-slate-300 hover:text-white"
                  }`}
                >
                  {zh ? d.zh : d.en}
                </button>
              ))}
            </div>
            <p className="max-w-xl text-center text-xs text-slate-500">
              {delivery === "hosted"
                ? zh
                  ? "我们代管服务器与部署：到账后自动开通你的独立实例，浏览器登录即用，数据按客户隔离；价格与装机版相同。"
                  : "We run the server for you — an isolated instance is provisioned automatically after payment. Log in from your browser; same price as self-hosted."
                : zh
                  ? "授权码激活，部署在你自己的电脑 / 服务器上，数据不出机房。"
                  : "License-code activation on your own machine — data stays on-prem."}
            </p>
          </Reveal>
        )}

        {/* ── 套餐卡片 ── */}
        <div
          className={`mt-10 grid gap-5 sm:grid-cols-2 ${
            tiers.length >= 5 ? "xl:grid-cols-5" : "mx-auto max-w-4xl lg:grid-cols-3"
          }`}
        >
          {tiers.map((t, i) => (
            <TierCard
              key={t.key}
              tier={t}
              zh={zh}
              period={period}
              selected={selected === t.key}
              delay={i * 0.05}
              onSelect={() => {
                setSelected(t.key);
                track("order_tier", { tier: t.key });
              }}
            />
          ))}
        </div>

        {/* ── 结算条 ── */}
        <Reveal eager className="mt-8">
          <div className="glass flex flex-wrap items-center gap-x-8 gap-y-3 rounded-2xl border border-white/10 px-6 py-4">
            <div>
              <div className="text-xs text-slate-500">{zh ? "已选套餐" : "Selected plan"}</div>
              <div className="font-semibold text-white">{zh ? tier.name.zh : tier.name.en}</div>
            </div>
            {tier.perSeat && (
              <div>
                <div className="text-xs text-slate-500">
                  {zh ? `坐席数（最少 ${tier.perSeat.min} 席）` : `Seats (min ${tier.perSeat.min})`}
                </div>
                <div className="mt-0.5 inline-flex items-center gap-2">
                  <button
                    onClick={() => setSeats((s) => clampSeats(tier, s - 1))}
                    disabled={seats <= tier.perSeat.min}
                    aria-label={zh ? "减少坐席" : "Fewer seats"}
                    className="flex h-7 w-7 items-center justify-center rounded-full border border-white/15 text-slate-300 transition hover:border-neon-cyan/50 hover:text-white disabled:opacity-30"
                  >
                    −
                  </button>
                  <span className="min-w-[3.5rem] text-center font-semibold tabular-nums text-white">
                    {seats} {zh ? "席" : seats > 1 ? "seats" : "seat"}
                  </span>
                  <button
                    onClick={() => setSeats((s) => clampSeats(tier, s + 1))}
                    disabled={seats >= tier.perSeat.max}
                    aria-label={zh ? "增加坐席" : "More seats"}
                    className="flex h-7 w-7 items-center justify-center rounded-full border border-white/15 text-slate-300 transition hover:border-neon-cyan/50 hover:text-white disabled:opacity-30"
                  >
                    +
                  </button>
                </div>
              </div>
            )}
            <div>
              <div className="text-xs text-slate-500">{zh ? "应付金额" : "Total"}</div>
              <div className="font-semibold text-neon-cyan">
                {tier.custom
                  ? zh
                    ? "定制报价 · 按需方案"
                    : "Custom quote"
                  : tier.monthly === 0
                    ? zh
                      ? "免费"
                      : "Free"
                    : tier.oneTime
                      ? `${fmt(tier.monthly)} USD${zh ? " · 一次性" : " · one-time"}`
                      : `${fmt(linePrice(tier, period, seats))} USD / ${
                          period === "monthly" ? (zh ? "月" : "mo") : period === "quarterly" ? (zh ? "季" : "qtr") : zh ? "年" : "yr"
                        }`}
              </div>
              {tier.perSeat && (
                <div className="text-[11px] text-slate-500">
                  {zh
                    ? `${fmt(tierPrice(tier, period))} × ${seats} 席`
                    : `${fmt(tierPrice(tier, period))} × ${seats} seats`}
                </div>
              )}
            </div>
            {tier.custom ? (
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("order_contact_sales", { tier: tier.key })}
                className="ml-auto rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
              >
                {zh ? "咨询客服 · 获取报价方案" : "Contact sales for a quote"}
              </a>
            ) : (
              <button
                onClick={() => {
                  setCheckout(true);
                  track("order_open_checkout", { tier: tier.key, period });
                }}
                className="ml-auto rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
              >
                {zh ? "立即下单 · USDT 结算" : "Order now · pay in USDT"}
              </button>
            )}
          </div>
        </Reveal>

        {/* ── 以下大区块（演示 / 硬件 / 配件 / 代部署 / 信任卡）均为
              幻境 STUDIO 本机算力产品专属；ChatX / LingoX 的能力详情走各自产品页 ── */}
        {family === "avatarhub" && (
          <>
        {/* ── 品牌片薄横幅：付款犹豫时的信任压缩包，不挤价格表（点击去 /film） ── */}
        <Reveal className="mt-14">
          <Link
            href={zh ? "/film" : "/en/film"}
            className="glass group flex items-center gap-4 rounded-2xl border border-emerald-300/25 p-3 pr-5 transition hover:border-emerald-300/50"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={zh ? BRAND_FILM.poster.zh : BRAND_FILM.poster.en}
              alt=""
              className="h-20 w-36 shrink-0 rounded-xl object-cover"
            />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-white">
                {zh ? "3 分钟品牌片：六个功能真机实测" : "The 3-minute film: six features, real engine output"}
              </div>
              <p className="mt-1 truncate text-xs text-slate-400">
                {zh
                  ? "没有摄影师、配音员和翻译——连主持人也是引擎做的"
                  : "No camera crew, no voice actor, no translator — even the host is engine-made"}
              </p>
            </div>
            <span className="shrink-0 rounded-full bg-neon-blue px-4 py-2 text-xs font-semibold text-white transition group-hover:brightness-110">
              ▶ {zh ? BRAND_FILM.durationLabel.zh : BRAND_FILM.durationLabel.en}
            </span>
          </Link>
        </Reveal>

        {/* ── 效果演示（真实引擎输出优先；未就绪的显示制作中占位） ── */}
        <ShowcaseGrid zh={zh} />

        {/* ── 能力 → 档位对照：拿着产品名（幻影/幻声/通传…）来找价的人在这里对上号
              （2026-08-07 实录工单「看不到幻影的价格」；数据源 CAPABILITY_TIERS 与
              TIERS.feats 同步维护，档位名/价格全派生零手写） ── */}
        <PricingTable
          title={zh ? "我要的功能在哪个档？" : "Which tier has what I need?"}
          subtitle={zh ? "按产品线找价格：能力包含在会员档位里，不单独计价" : "Find pricing by product line — capabilities ship inside membership tiers"}
          head={zh ? ["我想要的能力", "产品线", "所在档位", "挂牌价 (USD)"] : ["Capability", "Product line", "Tier", "List price (USD)"]}
          rows={CAPABILITY_TIERS.map((c) => {
            const t = studioTier(c.tierKey);
            const tierLabel = `${tierShortName(t, zh ? "zh" : "en")}${zh ? "起" : "+"}`;
            const price = tierPriceLabel(t, zh ? "zh" : "en") + (c.note ? `（${zh ? c.note.zh : c.note.en}）` : "");
            return [zh ? c.need.zh : c.need.en, zh ? c.product.zh : c.product.en, tierLabel, price];
          })}
          highlightCol={3}
        />

        {/* ── 部署版本 × 最低配置（设备自备，我们协助部署） ── */}
        <PricingTable
          title={zh ? "部署版本与最低配置" : "Editions & minimum hardware"}
          subtitle={zh ? "设备自备 · NVIDIA 显卡 + Win10/11 · 首次部署按档下载 11–35GB 模型" : "Your hardware · NVIDIA GPU + Win10/11 · 11–35GB models on first install"}
          head={zh ? ["版本", "显卡（最低）", "内存", "硬盘", "可流畅运行"] : ["Edition", "GPU (min)", "RAM", "Disk", "Runs smoothly"]}
          rows={HARDWARE.map((h) => [zh ? h.tier.zh : h.tier.en, h.gpu, h.ram, h.disk, zh ? h.can.zh : h.can.en])}
          highlightCol={1}
        />

        {/* ── 配件推荐 ── */}
        <PricingTable
          title={zh ? "外设与配件推荐" : "Recommended accessories"}
          subtitle={zh ? "按需选配 · 光线与收音质量直接决定换脸贴合度与克隆音质" : "Optional · lighting & audio quality drive swap fit and clone fidelity"}
          head={zh ? ["类别", "入门之选", "专业之选", "说明"] : ["Category", "Entry pick", "Pro pick", "Notes"]}
          rows={ACCESSORIES.map((a) => [zh ? a.cat.zh : a.cat.en, zh ? a.entry.zh : a.entry.en, zh ? a.pro.zh : a.pro.en, zh ? a.note.zh : a.note.en])}
          highlightCol={2}
        />

        {/* ── 私有化部署 / 企业定制：统一走旗舰版咨询客服，不再挂固定价目表 ── */}
        <Reveal className="mt-14">
          <div className="glass flex flex-wrap items-center gap-4 rounded-2xl border border-neon-violet/25 px-6 py-5">
            <KeyRound className="h-8 w-8 shrink-0 text-neon-violet" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold text-white">
                {zh ? "私有化部署 · 企业定制（旗舰版）" : "Private deployment · enterprise customization (Flagship)"}
              </div>
              <p className="mt-1 text-sm text-slate-400">
                {zh
                  ? "私有部署、定制开发与所有专业私域服务按需组合，方案与报价一对一评估——请咨询客服获取报价方案。"
                  : "Private deployment, custom development and full pro private-domain services, scoped case by case — contact sales for a tailored quote."}
              </p>
            </div>
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "order_flagship_quote" })}
              className="rounded-full border border-neon-violet/40 px-5 py-2 text-sm text-violet-300 transition hover:bg-neon-violet/10"
            >
              {zh ? "咨询客服获取报价" : "Contact sales"}
            </a>
          </div>
        </Reveal>

        {/* ── 远程代部署 ── */}
        <Reveal className="mt-10">
          <div className="glass flex flex-wrap items-center gap-4 rounded-2xl border border-neon-cyan/20 px-6 py-5">
            <ShieldCheck className="h-8 w-8 shrink-0 text-neon-cyan" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold text-white">
                {zh ? REMOTE_INSTALL.name.zh : REMOTE_INSTALL.name.en}
                <span className="ml-2 text-neon-cyan">{REMOTE_INSTALL.price} USD</span>
              </div>
              <p className="mt-1 text-sm text-slate-400">{zh ? REMOTE_INSTALL.desc.zh : REMOTE_INSTALL.desc.en}</p>
            </div>
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "order_remote_install" })}
              className="rounded-full border border-neon-cyan/40 px-5 py-2 text-sm text-neon-cyan transition hover:bg-neon-cyan/10"
            >
              {zh ? "预约代部署" : "Book install"}
            </a>
          </div>
        </Reveal>

        {/* ── 信任区块：先试后买 / 密码学授权 / 到账自动核销 / 数据不出机房 ── */}
        <Reveal className="mt-12">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {(
              [
                {
                  icon: Timer,
                  title: zh ? "先试后买" : "Try before you buy",
                  desc: zh
                    ? "免费版换脸直接用（输出带水印），效果满意再升级付费档；下载即用，无需信用卡。"
                    : "The Free plan does face swap out of the box (watermarked). Upgrade only when you're satisfied — no card required.",
                },
                {
                  icon: KeyRound,
                  title: zh ? "密码学授权" : "Cryptographic licensing",
                  desc: zh
                    ? "Ed25519 签名 + 机器指纹绑定，兑换码在线激活即刻生效，不怕丢单。"
                    : "Ed25519-signed licenses bound to your machine; redeem codes activate instantly.",
                },
                {
                  icon: Wallet,
                  title: zh ? "到账自动核销" : "Auto payment matching",
                  desc: zh
                    ? "每单唯一识别尾数，链上到账自动对单开通；进度随时自助可查。"
                    : "Unique cent suffix per order — on-chain payments match automatically, status self-serve.",
                },
                {
                  icon: BadgeCheck,
                  title: zh ? "数据不出机房" : "Data stays on-prem",
                  desc: zh
                    ? "全部推理在你本机/内网运行，素材与产出不上传；产出带 C2PA 凭证可验真。"
                    : "All inference runs on your hardware; nothing uploads. Outputs carry C2PA credentials.",
                },
              ] as const
            ).map((c) => (
              <div key={c.title} className="glass rounded-2xl border border-white/10 p-5">
                <c.icon className="h-6 w-6 text-neon-cyan" />
                <div className="mt-3 text-sm font-semibold text-white">{c.title}</div>
                <p className="mt-1.5 text-xs leading-relaxed text-slate-400">{c.desc}</p>
              </div>
            ))}
          </div>
        </Reveal>
          </>
        )}

        {/* ── 订单进度自助查询 ── */}
        <OrderStatusLookup />

        <p className="mx-auto mt-10 max-w-3xl text-center text-xs leading-relaxed text-slate-500">
          {family === "avatarhub"
            ? zh
              ? "会员等级对应引擎授权档（trial / standard / pro / enterprise），到账后由激活服务器按机器指纹签发 Ed25519 签名授权。产出默认带 C2PA 内容凭证 + 不可见水印可验真；克隆需本人合法授权，禁止用于冒充 / 诈骗。下单前请向客服核对最新收款地址。"
              : "Plan tiers map to engine license editions (trial / standard / pro / enterprise). Licenses are Ed25519-signed against your machine fingerprint after payment. Outputs carry C2PA credentials + invisible watermark; cloning requires the subject's consent. Always verify the payment address with support before sending."
            : zh
              ? "到账后自动签发 Ed25519 签名授权码并按联系方式送达，在后台「会员中心」粘贴即激活；Token 包凭证同样在会员中心兑换（绑定下单联系方式，防串号）。下单前请向客服核对最新收款地址。"
              : "After payment an Ed25519-signed license code is issued automatically and delivered to your contact — paste it in the admin Membership Center to activate. Token-pack vouchers redeem the same way (bound to your order contact). Always verify the payment address with support before sending."}
        </p>
      </div>

      {/* ── 结算弹窗 ── */}
      <AnimatePresence>
        {checkout && (
          <CheckoutModal
            zh={zh}
            tier={tier}
            period={period}
            family={family}
            seats={tier.perSeat ? seats : 1}
            delivery={family === "chatx" ? delivery : "installed"}
            initialFp={prefillFp}
            attributionRef={prefillRef}
            onClose={() => setCheckout(false)}
          />
        )}
      </AnimatePresence>
    </section>
  );
}

function TierCard({
  tier: t,
  zh,
  period,
  selected,
  delay,
  onSelect,
}: {
  tier: LineTier;
  zh: boolean;
  period: Period;
  selected: boolean;
  delay: number;
  onSelect: () => void;
}) {
  const price = linePrice(t, period);
  const seatSuffix = t.perSeat ? (zh ? " / 坐席" : " / seat") : "";
  const unit = t.oneTime
    ? zh
      ? "USD · 一次性"
      : "USD one-time"
    : (period === "monthly"
        ? zh
          ? "USD / 月"
          : "USD / mo"
        : period === "quarterly"
          ? zh
            ? "USD / 季"
            : "USD / qtr"
          : zh
            ? "USD / 年"
            : "USD / yr") + seatSuffix;
  return (
    <Reveal eager delay={delay} className="h-full">
      <button
        onClick={onSelect}
        className={`relative flex h-full w-full flex-col rounded-2xl border p-5 text-left transition ${
          selected
            ? "border-neon-cyan/60 bg-ink-800/80 ring-breathe"
            : "border-white/10 bg-ink-900/60 hover:border-neon-cyan/30"
        }`}
      >
        {t.hot && (
          <span className="absolute -top-2.5 right-4 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-2.5 py-0.5 text-[11px] font-medium text-ink-950">
            {zh ? "最受欢迎" : "Most popular"}
          </span>
        )}
        <div className="font-semibold text-white">{zh ? t.name.zh : t.name.en}</div>
        <div className="mt-0.5 text-xs text-slate-500">
          {zh ? t.audience.zh : t.audience.en} · {t.edition}
        </div>
        <div className="mt-4">
          {t.custom ? (
            <>
              <span className="text-3xl font-bold text-white">{zh ? "定制报价" : "Custom"}</span>
              <span className="ml-1.5 text-xs text-slate-500">{zh ? "咨询客服" : "contact sales"}</span>
            </>
          ) : t.monthly === 0 ? (
            <>
              <span className="text-3xl font-bold text-white">{zh ? "免费" : "Free"}</span>
              <span className="ml-1.5 text-xs text-slate-500">{zh ? "带水印" : "watermarked"}</span>
            </>
          ) : (
            <>
              <span className="text-3xl font-bold text-white">{fmt(price)}</span>
              <span className="ml-1.5 text-xs text-slate-500">{unit}</span>
            </>
          )}
        </div>
        <ul className="mt-4 flex-1 space-y-2">
          {(zh ? t.feats.zh : t.feats.en).map((f) => (
            <li key={f} className="flex items-start gap-2 text-sm text-slate-300">
              <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-neon-cyan" />
              {f}
            </li>
          ))}
        </ul>
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
    </Reveal>
  );
}

/** 效果演示网格：只渲染 ready=true 的条目（ready=false = 下架，整卡不出现，也不出「制作中」占位）。 */
function ShowcaseGrid({ zh }: { zh: boolean }) {
  const live = SHOWCASE_VIDEOS.filter((v) => v.ready);
  if (live.length === 0) return null;
  return (
    <Reveal className="mt-14">
      <div className="mb-4 flex items-baseline gap-3">
        <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "效果演示" : "See it in action"}</h2>
        <span className="text-xs text-slate-500">
          {zh ? "所有能力均在本机运行 · 标「真实输出」的即引擎实录" : "Everything runs locally · items marked 'real output' are engine recordings"}
        </span>
      </div>
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
        {live.map((v) => {
          const src = !zh && v.srcEn ? v.srcEn : v.src;
          const poster = !zh && v.posterEn ? v.posterEn : v.poster;
          return (
            <div key={v.key} className="glass overflow-hidden rounded-2xl border border-white/10">
              <div className="relative">
                <video
                  controls
                  preload="none"
                  poster={poster}
                  src={src}
                  className="aspect-video w-full bg-ink-950 object-cover"
                  onPlay={(e) => {
                    // 每会话每条只记一次"开播"(暂停续播不重复计数),带语言维度
                    const el = e.currentTarget;
                    if (el.dataset.played) return;
                    el.dataset.played = "1";
                    track("showcase_play", { key: v.key, lang: zh ? "zh" : "en" });
                  }}
                  onTimeUpdate={(e) => {
                    // 25/50/75 进度里程碑:与 ended 一起可算每条片的观看深度漏斗
                    const el = e.currentTarget;
                    if (!el.duration) return;
                    const q = Math.floor((el.currentTime / el.duration) * 4);
                    const prev = Number(el.dataset.q || 0);
                    if (q > prev && q < 4) {
                      el.dataset.q = String(q);
                      track("showcase_progress", { key: v.key, pct: q * 25, lang: zh ? "zh" : "en" });
                    }
                  }}
                  onEnded={() => track("showcase_done", { key: v.key, lang: zh ? "zh" : "en" })}
                />
                <span
                  className={`pointer-events-none absolute left-3 top-3 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                    v.real ? "bg-emerald-400/90 text-ink-950" : "bg-neon-violet/90 text-white"
                  }`}
                >
                  {v.real ? (zh ? "✓ 真实引擎输出" : "✓ Real engine output") : zh ? "概念演示" : "Concept demo"}
                </span>
              </div>
              <div className="p-4">
                <div className="text-sm font-semibold text-white">{zh ? v.title.zh : v.title.en}</div>
                <p className="mt-1 text-xs leading-relaxed text-slate-400">{zh ? v.desc.zh : v.desc.en}</p>
              </div>
            </div>
          );
        })}
      </div>
    </Reveal>
  );
}

function PricingTable({
  title,
  subtitle,
  head,
  rows,
  highlightCol,
}: {
  title: string;
  subtitle: string;
  head: string[];
  rows: string[][];
  highlightCol: number;
}) {
  return (
    <Reveal className="mt-14">
      <div className="mb-4 flex items-baseline gap-3">
        <h2 className="text-xl font-bold text-white md:text-2xl">{title}</h2>
        <span className="text-xs text-slate-500">{subtitle}</span>
      </div>
      <div className="overflow-x-auto rounded-2xl border border-white/10 bg-ink-900/60">
        <table className="w-full min-w-[560px] text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              {head.map((h) => (
                <th key={h} className="px-5 py-3 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-t border-white/5">
                {r.map((c, j) => (
                  <td
                    key={j}
                    className={`px-5 py-2.5 ${
                      j === highlightCol
                        ? "whitespace-nowrap font-semibold text-neon-cyan"
                        : j === 0
                          ? "text-slate-300"
                          : "text-slate-400"
                    }`}
                  >
                    {c}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Reveal>
  );
}

/** /api/payment/methods 的浏览器可见子集（见 lib/payment-settings.ts getPublicPaymentSettings）。 */
interface PayMethodsInfo {
  usdt?: { enabled?: boolean; address?: string };
  card?: { enabled?: boolean; provider?: string; publishableKey?: string; currency?: string };
  cardSecretConfigured?: boolean;
}

function CheckoutModal({
  zh,
  tier,
  period,
  family,
  seats = 1,
  delivery = "installed",
  initialFp,
  attributionRef = "",
  onClose,
}: {
  zh: boolean;
  tier: LineTier;
  period: Period;
  family: OrderFamily;
  /** 坐席数（仅 perSeat 档 >1；应付 = 单价 × seats）。 */
  seats?: number;
  /** 交付形态（仅 chatx 可为 hosted）：hosted 单走托管开通守护，不签装机授权码。 */
  delivery?: Delivery;
  initialFp: string;
  /** 会话归因串（?ref=…，AI 坐席链接带入）：静默随单提交，不渲染任何 UI。 */
  attributionRef?: string;
  onClose: () => void;
}) {
  const [contact, setContact] = useState("");
  const [fp, setFp] = useState(initialFp);
  const [state, setState] = useState<"idle" | "busy" | "ok" | "err">("idle");
  const [orderId, setOrderId] = useState("");
  const [payAmount, setPayAmount] = useState(0);
  const [copied, setCopied] = useState<"" | "addr" | "amount" | "id">("");
  // 可用支付方式（后台可配）：拉不到就保持 null → 只走 USDT 老流程，绝不阻断下单。
  const [pay, setPay] = useState<PayMethodsInfo | null>(null);
  const [method, setMethod] = useState<"usdt" | "card">("usdt");
  const [cardNotice, setCardNotice] = useState(false);
  const price = linePrice(tier, period, seats);
  // 卡通道可用 = 后台启用 + 服务器已配 Stripe Secret；免费档（price=0）无可扣金额，不给卡入口。
  const cardAvailable = !!pay?.card?.enabled && pay?.cardSecretConfigured !== false && price > 0;
  const usdtAvailable = !pay || pay.usdt?.enabled !== false;
  const showToggle = cardAvailable && usdtAvailable;
  const cardCurrency = pay?.card?.currency || "USD";
  const amountUnit = method === "card" ? cardCurrency : "USDT";
  // 收款地址：后台设置优先，未配置回落到构建期环境变量（保持老行为）。
  const usdtAddr = pay?.usdt?.address || USDT_ADDR;

  useEffect(() => {
    let alive = true;
    fetch("/api/payment/methods")
      .then((r) => r.json())
      .then((j: { ok?: boolean } & PayMethodsInfo) => {
        if (!alive || !j?.ok) return;
        setPay(j);
        // 只开了卡（USDT 被关）时默认选卡；其余情况保持 USDT 默认。
        if (j.card?.enabled && j.cardSecretConfigured !== false && j.usdt?.enabled === false && price > 0) {
          setMethod("card");
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 卡支付：为刚创建的订单换取 Stripe Checkout 跳转链接。true = 正在跳转离站。 */
  const startCardCheckout = async (id: string): Promise<boolean> => {
    if (!id) return false;
    try {
      const r = await fetch("/api/payment/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order_id: id }),
      });
      const j = await r.json();
      if (j?.ok && j.url) {
        track("order_card_redirect", { order: id });
        window.location.href = j.url;
        return true;
      }
    } catch {
      /* fall through → USDT 回落 */
    }
    return false;
  };

  const submit = async () => {
    if (!contact.trim()) {
      setState("err");
      return;
    }
    setState("busy");
    try {
      const r = await fetch("/api/order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan: tier.key,
          edition: tier.edition,
          // 一次性商品（charpack）不随周期——履约走 topup 凭证通道不读 period，
          // 送 "onetime" 让台账/TG 通知语义诚实。
          period: tier.oneTime ? "onetime" : period,
          amount: price,
          contact: contact.trim(),
          fingerprint: fp.trim(),
          lang: zh ? "zh" : "en",
          method,
          // 托管交付标：只在显式选托管时携带（缺省=装机，与历史单同语义）
          ...(delivery === "hosted" ? { delivery: "hosted" } : {}),
          // 坐席数：仅按坐席档携带（履约按席数签发；amount 已是 单价×seats）
          ...(tier.perSeat ? { seats } : {}),
          // 会话归因串（AI 坐席链接 ?ref=…）：有值才带，供营销目标自动结算
          ...(attributionRef ? { ref: attributionRef } : {}),
        }),
      });
      const j = await r.json();
      if (j?.ok) {
        setOrderId(j.order_id || "");
        setPayAmount(Number(j.pay_amount) || price);
        track("order_submitted", { tier: tier.key, period, amount: price, method });
        if (method === "card") {
          const redirecting = await startCardCheckout(String(j.order_id || ""));
          if (redirecting) return; // 离站去 Stripe，保持 busy 态直到页面卸载
          // 卡通道未就绪（not_configured / Stripe 报错）→ 提示并回落 USDT 展示
          setCardNotice(true);
          setMethod("usdt");
        }
        setState("ok");
      } else {
        setState("err");
      }
    } catch {
      setState("err");
    }
  };

  const copy = (what: "addr" | "amount" | "id", text: string) => {
    navigator.clipboard?.writeText(text);
    setCopied(what);
    setTimeout(() => setCopied(""), 1200);
  };

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[80] flex items-center justify-center bg-ink-950/80 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ opacity: 0, y: 24, scale: 0.97 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 16, scale: 0.97 }}
        transition={{ duration: 0.25 }}
        className="glass max-h-[92vh] w-full max-w-lg overflow-auto rounded-2xl border border-white/10 p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-lg font-semibold text-white">
              {method === "card"
                ? zh
                  ? "确认订单 · 银行卡支付"
                  : "Confirm order · Card"
                : zh
                  ? "确认订单 · USDT 结算"
                  : "Confirm order · USDT"}
            </h3>
            <p className="mt-0.5 text-xs text-slate-500">
              {method === "card"
                ? zh
                  ? "提交订单后跳转 Stripe 安全支付页完成付款"
                  : "Submit to continue to Stripe secure checkout"
                : zh
                  ? "核对信息后按地址付款，客服 ≈5 分钟内为你开通"
                  : "Pay to the address below; activation within ~5 minutes"}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-500 transition hover:text-white" aria-label="close">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="mt-4 space-y-1 text-sm">
          <Row k={zh ? "套餐" : "Plan"} v={`${zh ? tier.name.zh : tier.name.en} (${tier.edition})`} />
          {tier.perSeat && (
            <Row
              k={zh ? "坐席数" : "Seats"}
              v={zh ? `${seats} 席 × ${fmt(tierPrice(tier, period))} USD` : `${seats} × ${fmt(tierPrice(tier, period))} USD`}
            />
          )}
          {family === "chatx" && (
            <Row
              k={zh ? "交付方式" : "Delivery"}
              v={
                delivery === "hosted"
                  ? zh ? "☁️ 云端托管（开通即用）" : "☁️ Cloud hosted"
                  : zh ? "💻 装机版（授权码激活）" : "💻 Self-hosted install"
              }
            />
          )}
          <Row
            k={zh ? "计费周期" : "Billing"}
            v={
              tier.monthly === 0
                ? zh
                  ? "免费版（输出带水印）"
                  : "Free plan (watermarked output)"
                : tier.oneTime
                  ? zh
                    ? "一次性加购（不续费）"
                    : "One-time purchase"
                  : period === "monthly"
                    ? zh
                      ? "按月订阅"
                      : "Monthly"
                    : period === "quarterly"
                      ? zh
                        ? "按季订阅"
                        : "Quarterly"
                      : zh
                        ? "按年订阅"
                        : "Annual"
            }
          />
          <Row
            k={zh ? "应付金额" : "Total"}
            v={tier.monthly === 0 ? `0 ${amountUnit}` : `${fmt(price)} ${amountUnit}`}
            accent
          />
        </div>

        {showToggle && (
          <div className="mt-4">
            <div className="text-xs text-slate-400">{zh ? "支付方式" : "Payment method"}</div>
            <div className="mt-1.5 inline-flex rounded-full border border-white/10 p-1">
              {(["usdt", "card"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => {
                    setMethod(m);
                    track("order_pay_method", { method: m });
                  }}
                  className={`rounded-full px-4 py-1.5 text-xs transition ${
                    method === m
                      ? "bg-gradient-to-r from-neon-cyan to-neon-violet font-medium text-ink-950"
                      : "text-slate-300 hover:text-white"
                  }`}
                >
                  {m === "usdt" ? "USDT (TRC20)" : zh ? "银行卡 · Card" : "Card (Stripe)"}
                </button>
              ))}
            </div>
          </div>
        )}

        {cardNotice && (
          <div className="mt-4 rounded-xl border border-neon-pink/30 bg-neon-pink/10 px-3 py-2.5 text-xs text-slate-200">
            {zh
              ? "银行卡支付即将开通，暂请使用 USDT 转账，或联系客服协助付款。"
              : "Card payment is coming soon — please pay with USDT or contact support."}
          </div>
        )}

        <label className="mt-5 block text-xs text-slate-400">
          {zh ? "联系方式（Telegram / 邮箱，用于开通通知）*" : "Contact (Telegram / email) *"}
        </label>
        <input
          value={contact}
          onChange={(e) => setContact(e.target.value)}
          placeholder={zh ? "@yourname 或 you@email.com" : "@yourname or you@email.com"}
          className="mt-1.5 w-full rounded-xl border border-white/10 bg-ink-950/60 px-4 py-2.5 text-sm text-white placeholder-slate-600 outline-none transition focus:border-neon-cyan/50"
        />
        {family === "avatarhub" ? (
          <>
            <label className="mt-3 block text-xs text-slate-400">
              {zh ? "机器指纹（选填，绑机签发授权用）" : "Machine fingerprint (optional)"}
            </label>
            <input
              value={fp}
              onChange={(e) => setFp(e.target.value)}
              placeholder={zh ? "客户端「设置 → 授权」中复制，或留空" : "Copy from client Settings → License, or leave empty"}
              className="mt-1.5 w-full rounded-xl border border-white/10 bg-ink-950/60 px-4 py-2.5 text-sm text-white placeholder-slate-600 outline-none transition focus:border-neon-cyan/50"
            />
          </>
        ) : (
          <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
            {tier.oneTime
              ? zh
                ? "Token 包凭证绑定此联系方式；已有订阅的请与订阅联系方式一致（同一 Telegram / 邮箱即可，写法不必逐字相同）。"
                : "The token-pack voucher is bound to this contact — if you have a subscription, use the same Telegram / email (formatting may differ)."
              : delivery === "hosted"
                ? zh
                  ? "到账后自动开通你的独立云端实例，专属网址与登录账号按此联系方式送达（订单进度页也可自取），浏览器直接使用，无需安装。"
                  : "After payment your isolated cloud instance is provisioned automatically — the URL and login account are delivered to this contact (also self-serve on the status page). Nothing to install."
                : zh
                  ? "授权码将按此联系方式送达，激活后绑定到你的实例，无需机器指纹。"
                  : "Your license code is delivered to this contact and binds to your instance — no machine fingerprint needed."}
          </p>
        )}

        {method === "usdt" ? (
          <>
            <div className="mt-4 text-xs text-slate-400">{zh ? "USDT 收款地址（TRC20）：" : "USDT address (TRC20):"}</div>
            {usdtAddr ? (
              <div className="mt-1.5 flex items-center gap-3 rounded-xl border border-white/10 bg-ink-950/60 px-3 py-2.5">
                <div className="shrink-0 rounded-lg bg-white p-1.5">
                  <QRCodeSVG value={usdtAddr} size={72} />
                </div>
                <span className="break-all font-mono text-xs text-slate-300">{usdtAddr}</span>
                <button
                  onClick={() => copy("addr", usdtAddr)}
                  className="ml-auto flex shrink-0 items-center gap-1 rounded-full border border-white/15 px-2.5 py-1 text-[11px] text-slate-300 transition hover:border-neon-cyan/50 hover:text-white"
                >
                  <Copy className="h-3 w-3" />
                  {copied === "addr" ? (zh ? "已复制" : "Copied") : zh ? "复制" : "Copy"}
                </button>
              </div>
            ) : (
              <div className="mt-1.5 rounded-xl border border-neon-pink/20 bg-neon-pink/5 px-3 py-2.5 text-xs text-slate-300">
                {zh ? (
                  <>提交订单后请联系 <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">Telegram 客服 {TELEGRAM_DISPLAY}</a> 获取当期收款地址（防伪冒）。</>
                ) : (
                  <>After submitting, contact <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">support {TELEGRAM_DISPLAY}</a> for the current payment address.</>
                )}
              </div>
            )}
          </>
        ) : (
          <div className="mt-4 rounded-xl border border-white/10 bg-ink-950/60 px-3 py-2.5 text-xs text-slate-300">
            {zh
              ? `提交订单后将跳转 Stripe 安全支付页，支持 Visa / Mastercard 等主流银行卡，按 ${cardCurrency} 结算。`
              : `After submitting you'll be redirected to Stripe secure checkout (Visa / Mastercard and more), settled in ${cardCurrency}.`}
          </div>
        )}

        {state === "ok" && (
          <div className="mt-4 rounded-xl border border-neon-cyan/30 bg-neon-cyan/10 px-4 py-3 text-sm text-slate-200">
            <div>
              ✅ {zh ? "订单已创建" : "Order created"}
              {orderId && (
                <>
                  {" "}
                  <b className="text-neon-cyan">{orderId}</b>
                  <button
                    onClick={() => copy("id", orderId)}
                    className="ml-2 rounded-full border border-white/15 px-2 py-0.5 text-[11px] text-slate-300 transition hover:text-white"
                  >
                    {copied === "id" ? (zh ? "已复制" : "Copied") : zh ? "复制单号" : "Copy ID"}
                  </button>
                </>
              )}
            </div>
            {payAmount > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {zh ? "请精确转账 " : "Transfer exactly "}
                <b className="text-lg text-neon-cyan">{payAmount} USDT</b>
                <button
                  onClick={() => copy("amount", String(payAmount))}
                  className="rounded-full border border-white/15 px-2 py-0.5 text-[11px] text-slate-300 transition hover:text-white"
                >
                  {copied === "amount" ? (zh ? "已复制" : "Copied") : zh ? "复制金额" : "Copy amount"}
                </button>
              </div>
            )}
            <p className="mt-1.5 text-xs text-slate-400">
              {zh
                ? "金额的小数尾数是你的订单识别码，精确转账即可自动对上账。到账开通后按单号可随时查询进度："
                : "The decimal cents identify your order — transfer the exact amount for automatic matching. Track progress anytime:"}
              {orderId && (
                <a className="ml-1 text-neon-cyan hover:underline" href={`/order?check=${orderId}`}>
                  {zh ? "查询进度 →" : "Check status →"}
                </a>
              )}
            </p>
            {orderId && (
              <a
                href={`https://t.me/${BOT_HANDLE}?start=${orderId}`}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("order_tg_bind", { order: orderId })}
                className="mt-3 flex items-center justify-center gap-2 rounded-full bg-[#229ED9] px-4 py-2.5 text-sm font-medium text-white transition hover:opacity-90"
              >
                🔔 {zh ? "在 Telegram 接收开通通知（推荐）" : "Get activation alerts on Telegram"}
              </a>
            )}
            <p className="mt-1.5 text-center text-[11px] text-slate-500">
              {zh
                ? "点上方绑定后，到账、开通、临期都会自动私信你，无需守着页面。"
                : "Bind once — payment, activation and renewal alerts arrive in your Telegram."}
            </p>
          </div>
        )}
        {state === "err" && (
          <div className="mt-4 rounded-xl border border-neon-pink/30 bg-neon-pink/10 px-4 py-3 text-sm text-slate-200">
            {zh ? (
              <>{contact.trim() ? "提交失败，请稍后重试，或" : "请填写联系方式，或"}直接联系 <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">Telegram 客服</a> 下单。</>
            ) : (
              <>{contact.trim() ? "Submission failed — retry or" : "Contact is required, or"} order via <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">Telegram support</a>.</>
            )}
          </div>
        )}

        <div className="mt-5 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="rounded-full border border-white/15 px-5 py-2 text-sm text-slate-300 transition hover:text-white"
          >
            {zh ? "取消" : "Cancel"}
          </button>
          <button
            onClick={submit}
            disabled={state === "busy"}
            className="rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2 text-sm font-medium text-ink-950 transition hover:opacity-90 disabled:opacity-50"
          >
            {state === "busy"
              ? zh
                ? "提交中…"
                : "Submitting…"
              : method === "card"
                ? zh
                  ? "提交订单 · 前往支付"
                  : "Submit · go to payment"
                : zh
                  ? "我已付款 · 提交订单"
                  : "Paid · submit order"}
          </button>
        </div>

        <p className="mt-4 text-xs text-slate-600">
          {zh ? (
            <>大额合作、私有部署与定制请直接联系 <a className="text-slate-400 hover:text-neon-cyan" href={CONTACT_URL} target="_blank" rel="noreferrer">Telegram 客服 {TELEGRAM_DISPLAY}</a>。</>
          ) : (
            <>For enterprise deals and private deployment, contact <a className="text-slate-400 hover:text-neon-cyan" href={CONTACT_URL} target="_blank" rel="noreferrer">{TELEGRAM_DISPLAY}</a> directly.</>
          )}
        </p>
      </motion.div>
    </motion.div>
  );
}

function Row({ k, v, accent }: { k: string; v: string; accent?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-dashed border-white/5 py-1.5 last:border-0">
      <span className="text-slate-500">{k}</span>
      <b className={accent ? "text-neon-cyan" : "text-white"}>{v}</b>
    </div>
  );
}