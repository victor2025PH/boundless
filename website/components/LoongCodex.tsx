"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { motion, useMotionValue } from "framer-motion";
import { QRCodeCanvas } from "qrcode.react";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";
import { BOT_HANDLE } from "@/lib/site";
import { EveBot, DemonForm, type Skin } from "./AISprite";
import { LoongForm } from "./forms/LoongForm";
import { drawLoongCard } from "@/lib/loong-card";
import type { DragonState } from "@/lib/dragon-store";

/**
 * 图鉴 · 七星聚，龙行无界：个人收集落地页（形态展厅 / 北斗进度 / 月令龙鳞 / 成就 / 助力分享）。
 * 与 RobotStage 同源渲染三形态，锁定态压暗剪影 + 解锁线索；页面 noindex（内容因人而异）。
 */

const GOLD = "#f5c542";

const DIPPER: Array<[number, number]> = [
  [14, 14], [10, 36], [28, 46], [36, 22], [56, 30], [73, 40], [90, 52],
];
const DIPPER_PATH = "M36 22 L14 14 L10 36 L28 46 L36 22 L56 30 L73 40 L90 52";
const STARS_ZH = ["天枢", "天璇", "天玑", "天权", "玉衡", "开阳", "摇光"];
const STARS_EN = ["Dubhe", "Merak", "Phecda", "Megrez", "Alioth", "Mizar", "Alkaid"];

const T = {
  zh: {
    title: "图鉴 · 七星聚，龙行无界",
    sub: "每日到访点亮北斗，七星聚齐召唤界龙——这里是你的收集档案。",
    forms: "形态图鉴",
    normalName: "EVE · 司星者",
    normalDesc: "界龙留在人间的司星者，替你看守北斗。",
    demonName: "堕天使 EVE",
    demonDesc: "被唤醒过的暗面。",
    demonHint: "线索：对官网右下角的小家伙，快速连点 7 次…",
    loongName: "祥龙金鳞",
    loongDesc: "七星之愿的赐福，金瞳鹿角。",
    loongHint: "线索：七日之诚，七星之聚。",
    locked: "未解锁",
    wear: "穿戴此形态",
    worn: "已穿戴 · 回首页看看",
    dipper: "我的北斗",
    dipperEmpty: "还没有点亮任何星珠——回首页找 EVE 收下今天这颗。",
    scales: "月令龙鳞",
    scalesDesc: (n: number) => `已集 ${n}/3 枚 · 三鳞可兑「界龙之约」（年付 8 折 + 定制绿色通道）`,
    grandClaim: "三鳞大成 · 兑换界龙之约",
    grandDesc: "年付 8 折 + 定制方案绿色通道",
    grandCode: "界龙之约（兑换码）",
    toTg: "到 Telegram 领取",
    achv: "成就",
    achvFirst: "初次点星",
    achvSummon: "七星召唤",
    achvPerfect: "北斗正位（连续 7 天）",
    achvGrand: "界龙之约",
    achvHelper: (n: number) => `点星人 ×${n}`,
    achvSummonN: (n: number) => `累计召唤 ${n} 次`,
    share: "召唤同伴",
    shareDesc: "把助力链接发给朋友：TA 打开即为你点亮今日星珠，TA 自己的也会入袋。",
    shareBtn: "复制助力链接",
    shareCopied: "已复制 ✦",
    cardTitle: "我的星图卡",
    cardCopy: "复制星图文案",
    cardCopied: "星图文案已复制 ✦",
    tgSync: "在 Telegram 同步收珠",
    backHome: "← 回首页收星珠",
  },
  en: {
    title: "Codex · Seven Stars, Boundless Loong",
    sub: "Visit daily to light the Dipper; seven stars summon the Loong. This is your collection.",
    forms: "Forms",
    normalName: "EVE · Star Keeper",
    normalDesc: "The Loong's keeper of stars among us.",
    demonName: "Fallen EVE",
    demonDesc: "The dark side, once awakened.",
    demonHint: "Hint: tap the little bot 7 times, fast…",
    loongName: "Golden Loong",
    loongDesc: "Blessing of the seven-star wish.",
    loongHint: "Hint: seven days of devotion.",
    locked: "Locked",
    wear: "Wear this form",
    worn: "Worn · see it on the home page",
    dipper: "My Big Dipper",
    dipperEmpty: "No pearls yet — visit the home page and collect today's.",
    scales: "Monthly Scales",
    scalesDesc: (n: number) => `${n}/3 collected · 3 scales redeem the Loong's Covenant (20% off yearly)`,
    grandClaim: "Redeem the Loong's Covenant",
    grandDesc: "20% off yearly + fast-track custom solutions",
    grandCode: "The Loong's Covenant (code)",
    toTg: "Redeem on Telegram",
    achv: "Achievements",
    achvFirst: "First Pearl",
    achvSummon: "Seven-Star Summon",
    achvPerfect: "Perfect Dipper (7-day streak)",
    achvGrand: "The Loong's Covenant",
    achvHelper: (n: number) => `Star Lighter ×${n}`,
    achvSummonN: (n: number) => `${n} summons total`,
    share: "Summon Allies",
    shareDesc: "Share your assist link: opening it lights today's pearl for you — and for them.",
    shareBtn: "Copy assist link",
    shareCopied: "Copied ✦",
    cardTitle: "My Star Card",
    cardCopy: "Copy card text",
    cardCopied: "Card text copied ✦",
    tgSync: "Sync on Telegram",
    backHome: "← Collect pearls on home",
  },
};

/** 静态舞台上的一只形态（复用站内同源组件，缩小展示） */
function FormStage({ skin, locked }: { skin: Skin; locked: boolean }) {
  const zero = useMotionValue(0);
  const one = useMotionValue(1);
  const pool = useMotionValue(0.45);
  const common = {
    mode: "idle_base" as const,
    isHovered: false,
    newsText: "",
    newsCta: "",
    scrollTilt: zero,
    flightRotate: zero,
    gazeX: zero,
    gazeY: zero,
    squashY: one,
    shadowOpacity: pool,
    onNewsCta: () => {},
    reduced: locked, // 锁定态静止（剪影不需要动画开销）
  };
  return (
    <div
      className="pointer-events-none origin-top scale-[0.82]"
      style={locked ? { filter: "brightness(0.16) saturate(0.3)" } : undefined}
      aria-hidden
    >
      {skin === "demon" ? <DemonForm {...common} /> : skin === "loong" ? <LoongForm {...common} /> : <EveBot {...common} skin={skin} />}
    </div>
  );
}

export default function LoongCodex() {
  const { lang } = useLang();
  const t = T[lang === "zh" ? "zh" : "en"];
  const stars = lang === "zh" ? STARS_ZH : STARS_EN;

  const [st, setSt] = useState<DragonState | null>(null);
  const [tokens, setTokens] = useState<{ share: string; tg: string } | null>(null);
  const [demonSeen, setDemonSeen] = useState(false);
  const [pref, setPref] = useState<string>("normal");
  const [copied, setCopied] = useState(false);
  const [cardCopied, setCardCopied] = useState(false);
  const [cardSaved, setCardSaved] = useState(false);
  const [grandCode, setGrandCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const qrBoxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    track("dragon_codex_view", {});
    try {
      setDemonSeen(localStorage.getItem("bl-demon-seen") === "1");
      setPref(localStorage.getItem("bl-skin-pref") ?? "normal");
    } catch {}
    fetch("/api/dragon")
      .then((r) => r.json())
      .then((d) => {
        if (d?.ok) {
          setSt(d.state as DragonState);
          if (d.share && d.tg) setTokens({ share: d.share, tg: d.tg });
        }
      })
      .catch(() => {});
  }, []);

  const wearLoong = () => {
    try {
      localStorage.setItem("bl-skin-pref", "loong");
      sessionStorage.removeItem("bl-sprite-skin");
    } catch {}
    setPref("loong");
    track("dragon_codex_wear", { skin: "loong" });
  };

  const copyShare = () => {
    if (!tokens) return;
    try {
      const u = new URL("/", window.location.origin);
      u.searchParams.set("xz", tokens.share);
      u.searchParams.set("utm_source", "share");
      u.searchParams.set("utm_medium", "codex");
      navigator.clipboard?.writeText(u.toString());
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
      track("dragon_share_copy", { from: "codex" });
    } catch {}
  };

  const copyStarCard = () => {
    const n = st?.collected ?? 0;
    const bar = `${"●".repeat(n)}${"○".repeat(Math.max(0, 7 - n))}`;
    const zh = lang === "zh";
    const lines = [
      zh ? "🐉 七星聚 · 龙行无界" : "🐉 Seven Stars · Boundless Loong",
      zh ? `北斗进度 ${n}/7  ${bar}` : `Dipper ${n}/7  ${bar}`,
      zh ? `累计召唤 ${st?.summons ?? 0} · 月鳞 ${st?.scales ?? 0}/3` : `Summons ${st?.summons ?? 0} · Scales ${st?.scales ?? 0}/3`,
      st?.loongSkin ? (zh ? "形态：祥龙金鳞已解锁" : "Form: Golden Loong unlocked") : zh ? "形态：司星者守北斗" : "Form: Star Keeper",
      "",
      zh ? `一起来点星 → ${typeof window !== "undefined" ? window.location.origin : ""}/loong` : `Join me → ${typeof window !== "undefined" ? window.location.origin : ""}/loong`,
    ];
    try {
      navigator.clipboard?.writeText(lines.join("\n"));
      setCardCopied(true);
      setTimeout(() => setCardCopied(false), 2000);
      track("dragon_share_copy", { from: "codex_card", collected: n });
    } catch {}
  };

  /** 星图卡 PNG：canvas 现画现存（1080×1350），QR 从隐藏 QRCodeCanvas 取图 */
  const saveStarCard = () => {
    try {
      const qr = qrBoxRef.current?.querySelector("canvas") ?? null;
      const c = document.createElement("canvas");
      drawLoongCard(c, {
        lang: lang === "zh" ? "zh" : "en",
        collected,
        summons: st?.summons ?? 0,
        scales: st?.scales ?? 0,
        loongUnlocked,
        siteOrigin: window.location.origin,
        stars,
        qrCanvas: qr,
      });
      c.toBlob((blob) => {
        if (!blob) return;
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `loong-star-card-${collected}of7.png`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        setCardSaved(true);
        setTimeout(() => setCardSaved(false), 2200);
        track("dragon_share_card_save", { collected });
      }, "image/png");
    } catch {
      /* canvas 不可用则静默（文案卡仍可用） */
    }
  };

  const claimGrand = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await fetch("/api/dragon", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "grand" }),
      }).then((x) => x.json());
      if (r?.state) setSt(r.state as DragonState);
      if (r?.ok && r.code) {
        setGrandCode(r.code as string);
        track("dragon_grand_claim", { from: "codex" });
      }
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };

  const loongUnlocked = !!st?.loongSkin;
  const collected = st?.collected ?? 0;

  const achievements: Array<{ name: string; on: boolean }> = [
    { name: t.achvFirst, on: (st?.collected ?? 0) > 0 || (st?.summons ?? 0) > 0 },
    { name: t.achvSummon, on: (st?.summons ?? 0) >= 1 },
    { name: t.achvPerfect, on: (st?.bestStreak ?? 0) >= 7 },
    { name: t.achvGrand, on: (st?.grandClaims ?? 0) >= 1 },
  ];
  if ((st?.assistsGiven ?? 0) > 0) achievements.push({ name: t.achvHelper(st!.assistsGiven), on: true });
  if ((st?.summons ?? 0) > 1) achievements.push({ name: t.achvSummonN(st!.summons), on: true });

  const forms: Array<{ skin: Skin; name: string; desc: string; hint?: string; unlocked: boolean; wearable: boolean }> = [
    { skin: "normal", name: t.normalName, desc: t.normalDesc, unlocked: true, wearable: false },
    { skin: "demon", name: t.demonName, desc: t.demonDesc, hint: t.demonHint, unlocked: demonSeen, wearable: false },
    { skin: "loong", name: t.loongName, desc: t.loongDesc, hint: t.loongHint, unlocked: loongUnlocked, wearable: true },
  ];

  return (
    <main className="min-h-screen bg-[#05060f] pb-20 text-white">
      {/* 顶栏 */}
      <div className="mx-auto flex max-w-4xl items-center justify-between px-5 pb-2 pt-6">
        <Link href="/" className="text-sm text-zinc-400 transition hover:text-amber-200">
          {t.backHome}
        </Link>
        <span className="text-[11px] tracking-[0.25em] text-zinc-600">BOUNDLESS · LOONG CODEX</span>
      </div>

      <div className="mx-auto max-w-4xl px-5">
        {/* 标题 */}
        <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
          <h1 className="mt-6 text-2xl font-black tracking-wide text-amber-200 md:text-3xl">{t.title}</h1>
          <p className="mt-2 text-sm text-zinc-400">{t.sub}</p>
        </motion.div>

        {/* 我的北斗 */}
        <section className="mt-8 rounded-2xl border border-white/10 bg-white/[0.03] p-5">
          <h2 className="text-sm font-bold tracking-wide text-amber-200/90">{t.dipper}</h2>
          <div className="mx-auto mt-2 max-w-md">
            <svg viewBox="0 0 100 64" className="w-full" fill="none" aria-hidden>
              <path d={DIPPER_PATH} stroke={GOLD} strokeOpacity="0.4" strokeWidth="0.8" strokeDasharray="2 2.6" />
              {DIPPER.map(([x, y], i) => {
                const on = i < collected;
                return (
                  <g key={i}>
                    <circle
                      cx={x}
                      cy={y}
                      r={on ? 2.6 : 1.8}
                      fill={on ? GOLD : "rgba(255,255,255,0.15)"}
                      style={on ? { filter: `drop-shadow(0 0 3px ${GOLD})` } : undefined}
                    />
                    <text x={x} y={y + 8.5} textAnchor="middle" fontSize="4.2" fill={on ? "#f3dfa4" : "rgba(255,255,255,0.3)"}>
                      {stars[i]}
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>
          <div className="mt-1 text-center text-xs text-zinc-500">
            {collected > 0 ? `${collected}/7` : t.dipperEmpty}
          </div>
        </section>

        {/* 形态展厅 */}
        <section className="mt-6">
          <h2 className="mb-3 text-sm font-bold tracking-wide text-amber-200/90">{t.forms}</h2>
          <div className="grid gap-4 sm:grid-cols-3">
            {forms.map((f) => (
              <div key={f.skin} className="codex-form relative overflow-hidden rounded-2xl border border-white/10 bg-gradient-to-b from-white/[0.05] to-transparent p-4" data-skin={f.skin} data-unlocked={f.unlocked ? "1" : "0"}>
                <div className="flex h-48 items-center justify-center">
                  <FormStage skin={f.skin} locked={!f.unlocked} />
                  {!f.unlocked && (
                    <span className="absolute top-4 right-4 rounded-full border border-white/15 bg-black/60 px-2 py-0.5 text-[10px] text-zinc-400">
                      🔒 {t.locked}
                    </span>
                  )}
                </div>
                <div className="mt-2 text-sm font-bold text-zinc-100">{f.unlocked ? f.name : "？？？"}</div>
                <div className="mt-0.5 min-h-[2.2em] text-xs leading-snug text-zinc-500">
                  {f.unlocked ? f.desc : f.hint}
                </div>
                {f.wearable && f.unlocked && (
                  <button
                    type="button"
                    onClick={wearLoong}
                    disabled={pref === "loong"}
                    className="codex-wear-btn mt-2 w-full rounded-lg border border-amber-300/40 bg-amber-300/10 px-2 py-1.5 text-xs font-bold text-amber-200 transition hover:bg-amber-300/20 disabled:opacity-60"
                  >
                    {pref === "loong" ? t.worn : t.wear}
                  </button>
                )}
              </div>
            ))}
          </div>
        </section>

        {/* 月令龙鳞 + 成就 */}
        <div className="mt-6 grid gap-4 md:grid-cols-2">
          <section className="rounded-2xl border border-white/10 bg-white/[0.03] p-5">
            <h2 className="text-sm font-bold tracking-wide text-amber-200/90">{t.scales}</h2>
            <div className="mt-3 flex gap-3">
              {[0, 1, 2].map((i) => (
                <span
                  key={i}
                  className="grid h-10 w-10 place-items-center rounded-full border text-sm"
                  style={
                    i < Math.min(3, st?.scales ?? 0)
                      ? { borderColor: `${GOLD}88`, background: `${GOLD}1e`, color: GOLD, boxShadow: `0 0 10px ${GOLD}44` }
                      : { borderColor: "rgba(255,255,255,0.12)", color: "rgba(255,255,255,0.2)" }
                  }
                >
                  鳞
                </span>
              ))}
            </div>
            <p className="mt-2 text-xs leading-relaxed text-zinc-500">{t.scalesDesc(st?.scales ?? 0)}</p>
            {grandCode ? (
              <div className="mt-3 rounded-xl border border-amber-300/30 bg-black/40 p-3">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500">{t.grandCode}</div>
                <code className="mt-1 block font-mono text-sm font-bold tracking-wider text-amber-200">{grandCode}</code>
                <a
                  href={`https://t.me/${BOT_HANDLE}?start=loong_${grandCode}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => track("dragon_tg_click", { kind: "grand", from: "codex" })}
                  className="mt-2 inline-block rounded-full bg-gradient-to-r from-amber-400 to-yellow-300 px-3 py-1 text-xs font-bold text-black"
                >
                  ✈️ {t.toTg}
                </a>
              </div>
            ) : st?.grandReady ? (
              <button
                type="button"
                disabled={busy}
                onClick={claimGrand}
                className="dragon-grand-btn mt-3 w-full rounded-xl bg-gradient-to-r from-amber-400 to-yellow-300 px-3 py-2 text-xs font-bold text-black shadow-[0_0_14px_rgba(245,197,66,0.35)] hover:brightness-105 disabled:opacity-60"
              >
                🐲 {t.grandClaim}
                <span className="block text-[10px] font-medium opacity-80">{t.grandDesc}</span>
              </button>
            ) : null}
          </section>

          <section className="rounded-2xl border border-white/10 bg-white/[0.03] p-5">
            <h2 className="text-sm font-bold tracking-wide text-amber-200/90">{t.achv}</h2>
            <ul className="mt-3 space-y-1.5">
              {achievements.map((a) => (
                <li key={a.name} className="flex items-center gap-2 text-xs">
                  <span className={a.on ? "text-amber-300" : "text-zinc-700"}>{a.on ? "✦" : "○"}</span>
                  <span className={a.on ? "text-zinc-200" : "text-zinc-600"}>{a.name}</span>
                </li>
              ))}
            </ul>
          </section>
        </div>

        {/* 星图分享卡（文案卡，便于发群/频道） */}
        <section className="codex-share-card mt-6 overflow-hidden rounded-2xl border border-amber-300/25 bg-gradient-to-br from-[#1a1408] via-[#0d1018] to-[#0a0b14] p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-bold tracking-wide text-amber-200">{t.cardTitle}</h2>
              <p className="mt-1 text-xs text-zinc-500">
                {lang === "zh" ? "复制后可贴到频道 / 群 / 朋友圈" : "Copy & paste to channel / group / stories"}
              </p>
            </div>
            <span className="text-2xl" aria-hidden>
              🐉
            </span>
          </div>
          <div className="mt-4 flex items-center gap-1.5">
            {Array.from({ length: 7 }, (_, i) => (
              <span
                key={i}
                className="h-2.5 w-2.5 rounded-full"
                style={
                  i < collected
                    ? { background: GOLD, boxShadow: `0 0 6px ${GOLD}` }
                    : { background: "rgba(255,255,255,0.12)" }
                }
              />
            ))}
            <span className="ml-2 text-xs font-semibold text-amber-200/90">{collected}/7</span>
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2 text-center text-[11px]">
            <div className="rounded-lg bg-white/[0.04] px-2 py-2">
              <div className="text-zinc-500">{lang === "zh" ? "召唤" : "Summons"}</div>
              <div className="mt-0.5 font-bold text-amber-200">{st?.summons ?? 0}</div>
            </div>
            <div className="rounded-lg bg-white/[0.04] px-2 py-2">
              <div className="text-zinc-500">{lang === "zh" ? "月鳞" : "Scales"}</div>
              <div className="mt-0.5 font-bold text-amber-200">{st?.scales ?? 0}/3</div>
            </div>
            <div className="rounded-lg bg-white/[0.04] px-2 py-2">
              <div className="text-zinc-500">{lang === "zh" ? "形态" : "Form"}</div>
              <div className="mt-0.5 font-bold text-amber-200">{loongUnlocked ? (lang === "zh" ? "祥龙" : "Loong") : "EVE"}</div>
            </div>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={saveStarCard}
              className="codex-card-save rounded-full bg-gradient-to-r from-amber-400 to-yellow-300 px-4 py-2 text-sm font-bold text-black shadow-[0_0_14px_rgba(245,197,66,0.3)] hover:brightness-105"
            >
              {cardSaved ? (lang === "zh" ? "已保存 ✦" : "Saved ✦") : lang === "zh" ? "保存星图卡" : "Save card PNG"}
            </button>
            <button
              type="button"
              onClick={copyStarCard}
              className="rounded-full border border-amber-300/35 bg-amber-300/10 px-4 py-2 text-sm font-semibold text-amber-100 hover:bg-amber-300/18"
            >
              {cardCopied ? t.cardCopied : t.cardCopy}
            </button>
          </div>
          {/* 隐藏 QR：只作为星图卡素材源，不直接展示 */}
          <div ref={qrBoxRef} className="hidden" aria-hidden>
            <QRCodeCanvas value={`${typeof window !== "undefined" ? window.location.origin : ""}/loong?utm_source=share&utm_medium=card`} size={220} bgColor="#fffdf4" fgColor="#171208" />
          </div>
        </section>

        {/* 分享/同步 */}
        <section className="mt-6 rounded-2xl border border-amber-300/20 bg-amber-300/[0.05] p-5">
          <h2 className="text-sm font-bold tracking-wide text-amber-200/90">{t.share}</h2>
          <p className="mt-1 text-xs leading-relaxed text-zinc-400">{t.shareDesc}</p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={copyShare}
              disabled={!tokens}
              className="rounded-full bg-gradient-to-r from-amber-400 to-yellow-300 px-4 py-2 text-sm font-bold text-black shadow-[0_0_16px_rgba(245,197,66,0.3)] hover:brightness-105 disabled:opacity-50"
            >
              🔗 {copied ? t.shareCopied : t.shareBtn}
            </button>
            {tokens && (
              <a
                href={`https://t.me/${BOT_HANDLE}?start=${tokens.tg}`}
                target="_blank"
                rel="noopener noreferrer"
                onClick={() => track("dragon_tg_sync_click", { from: "codex" })}
                className="text-xs text-sky-300/90 underline-offset-2 hover:underline"
              >
                ✈️ {t.tgSync}{st?.tgBound ? " ✓" : ""}
              </a>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
