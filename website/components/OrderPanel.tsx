"use client";

import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { QRCodeSVG } from "qrcode.react";
import { BadgeCheck, Check, Clapperboard, Copy, KeyRound, ShieldCheck, Sparkles, Timer, Wallet, X } from "lucide-react";
import { useLang } from "./LanguageContext";
import OrderStatusLookup from "./OrderStatusLookup";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";
import { BOT_HANDLE, CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";
import {
  ACCESSORIES,
  FIRST_YEAR_PROMO,
  HARDWARE,
  LICENSES,
  REMOTE_INSTALL,
  SHOWCASE_VIDEOS,
  TIERS,
  USDT_ADDR,
  tierPrice,
  type Period,
  type Tier,
} from "@/lib/avatarhub-pricing";

const fmt = (n: number) => n.toLocaleString("en-US");

export default function OrderPanel() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [period, setPeriod] = useState<Period>("monthly");
  const [selected, setSelected] = useState("pro");
  const [checkout, setCheckout] = useState(false);
  const [prefillFp, setPrefillFp] = useState("");

  const tier = useMemo(() => TIERS.find((t) => t.key === selected) ?? TIERS[3], [selected]);

  // 深链预填：/order?plan=pro&period=annual&fp=<指纹>（客户端内"购买"按钮可带参跳转，绑机零输入）
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const plan = q.get("plan");
    if (plan && TIERS.some((t) => t.key === plan)) setSelected(plan);
    if (q.get("period") === "annual") setPeriod("annual");
    const fp = q.get("fp");
    if (fp) setPrefillFp(fp.slice(0, 128));
  }, []);

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
            {zh
              ? "引擎跑在你自己的设备上——我们不卖算力，所以不按字符、张数、时长计费，用量不限。设备自备（下方有配置与配件清单），我们协助部署；到账后按机器指纹签发授权，客户端一键激活。"
              : "The engine runs on your own hardware — we don't sell compute, so there's no per-character or per-minute metering. Bring your device (specs below), we help you deploy; licenses are issued against your machine fingerprint after payment."}
            {" "}
            <a href={zh ? "/download" : "/en/download"} className="text-neon-cyan hover:underline">
              {zh ? "前往下载客户端 →" : "Download the client →"}
            </a>
          </p>
        </Reveal>

        {/* ── 月付 / 年付 ── */}
        <Reveal className="mt-10 flex flex-col items-center gap-3">
          <div className="glass inline-flex rounded-full border border-white/10 p-1">
            {(["monthly", "annual"] as Period[]).map((p) => (
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
                {p === "monthly" ? (zh ? "月付" : "Monthly") : zh ? `年付 · 送 2 个月` : "Annual · 2 months free"}
              </button>
            ))}
          </div>
          <AnimatePresence>
            {period === "annual" && (
              <motion.span
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                className="rounded-full border border-neon-pink/30 bg-neon-pink/10 px-3 py-1 text-xs text-neon-pink"
              >
                {zh ? "🎁 限时首年 8 折 · 送 2 个月" : "🎁 First year 20% off · 2 months free"}
              </motion.span>
            )}
          </AnimatePresence>
        </Reveal>

        {/* ── 套餐卡片 ── */}
        <div className="mt-10 grid gap-5 sm:grid-cols-2 xl:grid-cols-5">
          {TIERS.map((t, i) => (
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
        <Reveal className="mt-8">
          <div className="glass flex flex-wrap items-center gap-x-8 gap-y-3 rounded-2xl border border-white/10 px-6 py-4">
            <div>
              <div className="text-xs text-slate-500">{zh ? "已选套餐" : "Selected plan"}</div>
              <div className="font-semibold text-white">{zh ? tier.name.zh : tier.name.en}</div>
            </div>
            <div>
              <div className="text-xs text-slate-500">{zh ? "应付金额" : "Total"}</div>
              <div className="font-semibold text-neon-cyan">
                {tier.monthly === 0
                  ? zh
                    ? "免费 · 14 天"
                    : "Free · 14 days"
                  : `${fmt(tierPrice(tier, period))} USD / ${period === "monthly" ? (zh ? "月" : "mo") : zh ? "年" : "yr"}`}
              </div>
            </div>
            <button
              onClick={() => {
                setCheckout(true);
                track("order_open_checkout", { tier: tier.key, period });
              }}
              className="ml-auto rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
            >
              {zh ? "立即下单 · USDT 结算" : "Order now · pay in USDT"}
            </button>
          </div>
        </Reveal>

        {/* ── 效果演示（真实引擎输出优先；未就绪的显示制作中占位） ── */}
        <ShowcaseGrid zh={zh} />

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

        {/* ── 私有授权 ── */}
        <PricingTable
          title={zh ? "本地私有部署授权" : "Private deployment licenses"}
          subtitle={zh ? "一次性 / 年费 · USD" : "One-off / yearly · USD"}
          head={zh ? ["方案", "授权档", "年费", "买断", "说明"] : ["Plan", "Edition", "Yearly", "Buyout", "Details"]}
          rows={LICENSES.map((l) => [
            zh ? l.name.zh : l.name.en,
            l.edition,
            zh ? l.yearly.zh : l.yearly.en,
            zh ? l.buyout.zh : l.buyout.en,
            zh ? l.desc.zh : l.desc.en,
          ])}
          highlightCol={2}
        />

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
                    ? "14 天免费试用不收一分钱，效果满意再付费；下载即用，无需信用卡。"
                    : "14-day free trial, no card required. Pay only when you're satisfied.",
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

        {/* ── 订单进度自助查询 ── */}
        <OrderStatusLookup />

        <p className="mx-auto mt-10 max-w-3xl text-center text-xs leading-relaxed text-slate-500">
          {zh
            ? "会员等级对应引擎授权档（trial / standard / pro / enterprise），到账后由激活服务器按机器指纹签发 Ed25519 签名授权。产出默认带 C2PA 内容凭证 + 不可见水印可验真；克隆需本人合法授权，禁止用于冒充 / 诈骗。下单前请向客服核对最新收款地址。"
            : "Plan tiers map to engine license editions (trial / standard / pro / enterprise). Licenses are Ed25519-signed against your machine fingerprint after payment. Outputs carry C2PA credentials + invisible watermark; cloning requires the subject's consent. Always verify the payment address with support before sending."}
        </p>
      </div>

      {/* ── 结算弹窗 ── */}
      <AnimatePresence>
        {checkout && (
          <CheckoutModal zh={zh} tier={tier} period={period} initialFp={prefillFp} onClose={() => setCheckout(false)} />
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
  tier: Tier;
  zh: boolean;
  period: Period;
  selected: boolean;
  delay: number;
  onSelect: () => void;
}) {
  const price = tierPrice(t, period);
  const unit = period === "monthly" ? (zh ? "USD / 月" : "USD / mo") : zh ? "USD / 年" : "USD / yr";
  return (
    <Reveal delay={delay} className="h-full">
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
          {t.monthly === 0 ? (
            <>
              <span className="text-3xl font-bold text-white">{zh ? "免费" : "Free"}</span>
              <span className="ml-1.5 text-xs text-slate-500">{zh ? "14 天" : "14 days"}</span>
            </>
          ) : (
            <>
              <span className="text-3xl font-bold text-white">{fmt(price)}</span>
              <span className="ml-1.5 text-xs text-slate-500">{unit}</span>
            </>
          )}
        </div>
        {period === "annual" && t.monthly > 0 && (
          <div className="mt-1 text-xs text-neon-pink">
            {zh ? "首年 8 折 ≈ " : "1st year ≈ "}
            {fmt(Math.round(price * FIRST_YEAR_PROMO))} USD
          </div>
        )}
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

/** 效果演示网格：ready 的视频直接内嵌播放（优先真实引擎输出），未就绪的显示「制作中」占位。 */
function ShowcaseGrid({ zh }: { zh: boolean }) {
  return (
    <Reveal className="mt-14">
      <div className="mb-4 flex items-baseline gap-3">
        <h2 className="text-xl font-bold text-white md:text-2xl">{zh ? "效果演示" : "See it in action"}</h2>
        <span className="text-xs text-slate-500">
          {zh ? "所有能力均在本机运行 · 标「真实输出」的即引擎实录" : "Everything runs locally · items marked 'real output' are engine recordings"}
        </span>
      </div>
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
        {SHOWCASE_VIDEOS.map((v) => {
          const src = !zh && v.srcEn ? v.srcEn : v.src;
          const poster = !zh && v.posterEn ? v.posterEn : v.poster;
          return (
            <div key={v.key} className="glass overflow-hidden rounded-2xl border border-white/10">
              {v.ready ? (
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
              ) : (
                <div className="relative grid aspect-video w-full place-items-center bg-gradient-to-br from-ink-900 via-ink-950 to-ink-900">
                  <div className="flex flex-col items-center gap-2 text-slate-600">
                    <Clapperboard className="h-8 w-8" />
                    <span className="text-xs">{zh ? "演示视频制作中" : "Demo video coming soon"}</span>
                  </div>
                  <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgba(34,211,238,0.08),transparent_60%)]" />
                </div>
              )}
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
  initialFp,
  onClose,
}: {
  zh: boolean;
  tier: Tier;
  period: Period;
  initialFp: string;
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
  const price = tierPrice(tier, period);
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
          period,
          amount: price,
          contact: contact.trim(),
          fingerprint: fp.trim(),
          lang: zh ? "zh" : "en",
          method,
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
          <Row
            k={zh ? "计费周期" : "Billing"}
            v={
              tier.monthly === 0
                ? zh
                  ? "免费试用 14 天"
                  : "14-day free trial"
                : period === "monthly"
                  ? zh
                    ? "按月订阅"
                    : "Monthly"
                  : zh
                    ? `按年订阅（送 2 个月）`
                    : "Annual (2 months free)"
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
        <label className="mt-3 block text-xs text-slate-400">
          {zh ? "机器指纹（选填，绑机签发授权用）" : "Machine fingerprint (optional)"}
        </label>
        <input
          value={fp}
          onChange={(e) => setFp(e.target.value)}
          placeholder={zh ? "客户端「设置 → 授权」中复制，或留空" : "Copy from client Settings → License, or leave empty"}
          className="mt-1.5 w-full rounded-xl border border-white/10 bg-ink-950/60 px-4 py-2.5 text-sm text-white placeholder-slate-600 outline-none transition focus:border-neon-cyan/50"
        />

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