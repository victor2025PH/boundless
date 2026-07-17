"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";
import { BOT_HANDLE } from "@/lib/site";
import LoongCeremony from "./LoongCeremony";
import LoongGhost from "./LoongGhost";
import { LoongHero } from "./forms/LoongForm";
import { dispatchApplySkin, dispatchLoongCeremony, dispatchLoongTeaser } from "@/lib/loong-events";
import type { DragonState, WishKind } from "@/lib/dragon-store";

/**
 * 「七星聚 · 龙行无界」星珠玩法前端。
 * - 每日到访：机器人旁浮现今日星珠，点击收下（服务端记账，UTC+8 幂等）；
 * - 北斗托盘：7 星进度 + 连续/剩余天状态，断签不清零；
 * - 集齐召唤：canvas 掠空 + 同源剪影收束 → 祈愿三选一；
 * - 领取：trial/gift 出兑换码 + TG 深链，skin 即时换装。
 * 与 AISprite 经 lib/loong-events 解耦通信。
 */

const STARS: Record<"zh" | "en", string[]> = {
  zh: ["天枢", "天璇", "天玑", "天权", "玉衡", "开阳", "摇光"],
  en: ["Dubhe", "Merak", "Phecda", "Megrez", "Alioth", "Mizar", "Alkaid"],
};

/** 北斗七星布局（viewBox 100×64）：斗魁四星 + 斗杓三星 */
const DIPPER: Array<[number, number]> = [
  [14, 14], // 天枢
  [10, 36], // 天璇
  [28, 46], // 天玑
  [36, 22], // 天权
  [56, 30], // 玉衡
  [73, 40], // 开阳
  [90, 52], // 摇光
];
/** 连线顺序：魁口闭合 + 杓柄延伸 */
const DIPPER_PATH = "M36 22 L14 14 L10 36 L28 46 L36 22 L56 30 L73 40 L90 52";

const GOLD = "#f5c542";

type Phase = "constellation" | "wish" | "result";

const T = {
  zh: {
    todayPearl: "今日星珠",
    collect: "收下",
    collected: (star: string, n: number) => `「${star}」归位（${n}/7）`,
    oneMore: "只差一颗，明日界龙将至",
    trayTitle: "七星聚 · 龙行无界",
    rules: "每日到访点亮一星 · 断签不清零 · 30 天内集齐 · 连续 7 天达成「北斗正位」有加成",
    summon: "北斗正位 · 召唤界龙",
    ceremonyTitle: "七星既聚 · 龙行无界",
    ceremonySub: "界龙衔珠而至，赐你一愿",
    wishTrial: "愿 · 体验",
    wishTrialDesc: (left: number) => `通译/智聊/幻声等任选 1 款 · 30 天全功能（本月剩 ${left} 份）`,
    wishTrialFull: "本月名额已满，下月朔日再来",
    wishSkin: "愿 · 龙鳞",
    wishSkinDesc: "永久解锁「祥龙金鳞」形态，金瞳鹿角",
    wishGift: "愿 · 机缘",
    wishGiftDesc: "专属礼包：年付约 85 折券 + 优先对接通道 + 下轮彩蛋线索",
    perfect: "✨ 北斗正位达成：额外解锁祥龙金鳞",
    codeLabel: "界龙的信物（兑换码）",
    toTg: "到 Telegram 领取",
    copy: "复制",
    copied: "已复制",
    skinDone: "祥龙金鳞已解锁并换装",
    wearLoong: "切换祥龙形态",
    skinEve: "司星者",
    skinLoong: "祥龙",
    teaser3: "三颗星珠已归位…界龙剪影初现",
    guide: "每日一珠 · 七星唤龙",
    guideDismiss: "知道了",
    statusCollected: (n: number) => `已集 ${n}/7`,
    statusStreak: (n: number) => `连续 ${n} 天`,
    statusDaysLeft: (n: number) => (n > 0 ? `本轮剩 ${n} 天` : "新一轮待开启"),
    rulesToggle: "玩法说明",
    loadFail: "星图暂时失联",
    retry: "重试",
    lightFxNote: "已为你开启轻量演出",
    resultShare: "邀请好友点星",
    resultShareDone: "链接已复制 ✦",
    close: "关闭",
    wished: "本轮愿望已达成",
    newCycle: "明日再来，开启新一轮七星",
    sprint: (left: number) => `差 ${left} 颗召唤界龙`,
    comeback: (n: number) => `星珠还亮着（${n}/7）· 界龙还在等你`,
    share: "复制助力链接",
    shareCopied: "已复制 · 发给朋友帮你点亮今日星珠",
    shareHint: (n: number) => `好友点开链接可代点今日星珠（本周期还可 ${n} 次）`,
    tgSync: "在 Telegram 同步收珠",
    assistOk: (n: number) => `已为好友点亮星珠（TA 已 ${n}/7）✦`,
    assistBoth: (n: number, m: number) => `已为好友点亮（TA ${n}/7）· 你的今日星珠也入袋了（${m}/7）✦`,
    assistSelfPearl: (m: number) => `你的今日星珠已入袋（${m}/7）✦`,
    assistFail: { self: "这是你自己的链接哦", already: "TA 今天的星珠已经亮了", cap: "TA 本周期的助力次数已用完", blocked: "TA 已集齐，等 TA 先许愿" } as Record<string, string>,
    skip: "跳过 ›",
    scalesTitle: "月令龙鳞",
    scalesHint: "每完成一轮七星得一枚（每月至多一枚），集 3 枚兑「界龙之约」",
    grandClaim: "三鳞大成 · 兑换界龙之约",
    grandDesc: "年付 8 折 + 定制方案绿色通道",
    grandCode: "界龙之约（兑换码）",
  },
  en: {
    todayPearl: "Today's pearl",
    collect: "Collect",
    collected: (star: string, n: number) => `"${star}" aligned (${n}/7)`,
    oneMore: "One more — the Loong arrives tomorrow",
    trayTitle: "Seven Stars · Boundless Loong",
    rules: "Visit daily to light a star · no reset on missed days · finish within 30 days · 7-day streak earns the Perfect bonus",
    summon: "Summon the Loong",
    ceremonyTitle: "Seven Stars Aligned",
    ceremonySub: "The Boundless Loong grants you one wish",
    wishTrial: "Wish · Trial",
    wishTrialDesc: (left: number) => `Any one product (Lingo/Chat/Voice…) · 30-day full access (${left} left this month)`,
    wishTrialFull: "Monthly quota reached — come back next month",
    wishSkin: "Wish · Scales",
    wishSkinDesc: "Permanently unlock the Golden Loong form",
    wishGift: "Wish · Fortune",
    wishGiftDesc: "Pack: ~15% off yearly + priority lane + next-cycle egg hint",
    perfect: "✨ Perfect Dipper: Golden Loong form unlocked as bonus",
    codeLabel: "The Loong's token (redeem code)",
    toTg: "Redeem on Telegram",
    copy: "Copy",
    copied: "Copied",
    skinDone: "Golden Loong form unlocked & applied",
    wearLoong: "Wear the Loong form",
    skinEve: "Star Keeper",
    skinLoong: "Loong",
    teaser3: "Three pearls lit… the Loong's silhouette stirs",
    guide: "One pearl a day · seven summon the Loong",
    guideDismiss: "Got it",
    statusCollected: (n: number) => `${n}/7 lit`,
    statusStreak: (n: number) => `${n}-day streak`,
    statusDaysLeft: (n: number) => (n > 0 ? `${n}d left` : "New cycle soon"),
    rulesToggle: "How it works",
    loadFail: "Star map offline",
    retry: "Retry",
    lightFxNote: "Lightweight ceremony on",
    resultShare: "Invite a friend",
    resultShareDone: "Link copied ✦",
    close: "Close",
    wished: "Wish granted this cycle",
    newCycle: "Return tomorrow to start a new cycle",
    sprint: (left: number) => `${left} more to summon the Loong`,
    comeback: (n: number) => `Your pearls are still lit (${n}/7) — the Loong awaits`,
    share: "Copy assist link",
    shareCopied: "Copied — a friend can light today's pearl for you",
    shareHint: (n: number) => `Friends can light today's pearl for you (${n} assists left this cycle)`,
    tgSync: "Sync on Telegram",
    assistOk: (n: number) => `You lit a pearl for your friend (now ${n}/7) ✦`,
    assistBoth: (n: number, m: number) => `Lit a pearl for your friend (${n}/7) — yours landed too (${m}/7) ✦`,
    assistSelfPearl: (m: number) => `Your pearl for today landed (${m}/7) ✦`,
    assistFail: { self: "That's your own link", already: "Their pearl is already lit today", cap: "Their assist quota is used up", blocked: "They've collected all 7 — waiting on their wish" } as Record<string, string>,
    skip: "Skip ›",
    scalesTitle: "Monthly Scales",
    scalesHint: "One scale per completed cycle (max 1/month) — 3 scales redeem the Loong's Covenant",
    grandClaim: "Redeem the Loong's Covenant",
    grandDesc: "20% off yearly + fast-track custom solutions",
    grandCode: "The Loong's Covenant (code)",
  },
};

function applySkin(skin: "loong" | "normal" | "demon") {
  dispatchApplySkin(skin);
}

function daysLeft(endsAt: number): number {
  if (!endsAt) return 0;
  return Math.max(0, Math.ceil((endsAt - Date.now()) / 86400000));
}

export default function DragonQuest() {
  const { lang } = useLang();
  const reduced = useReducedMotion() ?? false;
  const t = T[lang === "zh" ? "zh" : "en"];
  const stars = STARS[lang === "zh" ? "zh" : "en"];

  const [st, setSt] = useState<DragonState | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [open, setOpen] = useState(false); // 进度面板
  const [ceremony, setCeremony] = useState(false); // 召唤仪式
  const [flight, setFlight] = useState(false); // canvas 界龙演出阶段
  const [phase, setPhase] = useState<Phase>("constellation");
  const [copied, setCopied] = useState(false);
  const [shareCopied, setShareCopied] = useState(false);
  const [tokens, setTokens] = useState<{ share: string; tg: string } | null>(null);
  const [grandCode, setGrandCode] = useState<string | null>(null);
  const [lowFx, setLowFx] = useState(false);
  const [skinPref, setSkinPref] = useState<"normal" | "loong">("normal");
  const [loadError, setLoadError] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [ghostOn, setGhostOn] = useState(false);
  const [flightGhost, setFlightGhost] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const offerTracked = useRef(false);
  const comebackShown = useRef(false);
  const teaser3Shown = useRef(false);

  const loadState = useCallback(async (attempt = 0): Promise<boolean> => {
    try {
      const r = await fetch("/api/dragon");
      const d = await r.json();
      if (!d?.ok) throw new Error("bad");
      setSt(d.state as DragonState);
      if (d.share && d.tg) setTokens({ share: d.share, tg: d.tg });
      setLoadError(false);
      return true;
    } catch {
      if (attempt < 1) {
        await new Promise((x) => setTimeout(x, 900));
        return loadState(1);
      }
      setLoadError(true);
      return false;
    }
  }, []);

  /* 首屏：尽快拉状态（intro 已由 session 跳过）；失败可重试 */
  useEffect(() => {
    setLowFx(document.documentElement.getAttribute("data-fx") === "low");
    let alive = true;
    const timer = setTimeout(() => {
      if (!alive) return;
      loadState().then((ok) => {
        if (!ok || !alive) return;
        try {
          if (!sessionStorage.getItem("bl-dragon-guide")) setShowGuide(true);
        } catch {
          setShowGuide(true);
        }
      });
    }, 400);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [loadState]);

  /* 好友助力落地：?xz=<令牌> → 为分享人点亮今日星珠
     - 令牌先落 session，再清 URL（Strict Mode 双挂载仍能读到）
     - 同令牌只 POST 一次（done 标记防重复核销） */
  useEffect(() => {
    let tok = "";
    try {
      const q = new URLSearchParams(window.location.search);
      const fromUrl = q.get("xz") ?? "";
      if (fromUrl) {
        sessionStorage.setItem("bl-dragon-xz", fromUrl);
        q.delete("xz");
        const rest = q.toString();
        window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
      }
      tok = sessionStorage.getItem("bl-dragon-xz") ?? "";
      if (!tok) return;
      if (sessionStorage.getItem("bl-dragon-xz-done") === tok) {
        sessionStorage.removeItem("bl-dragon-xz");
        return;
      }
      sessionStorage.setItem("bl-dragon-xz-done", tok);
      sessionStorage.removeItem("bl-dragon-xz");
    } catch {
      return;
    }
    const zh = document.documentElement.lang !== "en";
    const tt = T[zh ? "zh" : "en"];
    fetch("/api/dragon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "assist", token: tok }),
    })
      .then((r) => r.json())
      .then((r) => {
        track("dragon_assist", { ok: !!r?.ok, reason: r?.reason ?? "", helper: r?.helperIndex ?? 0 });
        const gotOwn = (r?.helperIndex ?? 0) > 0;
        if (r?.ok) showToastRef.current(gotOwn ? tt.assistBoth(r.collected, r.helperCollected) : tt.assistOk(r.collected));
        else if (gotOwn) showToastRef.current(tt.assistSelfPearl(r.helperCollected));
        else if (r?.reason && tt.assistFail[r.reason]) showToastRef.current(tt.assistFail[r.reason]);
        if (gotOwn || r?.ok) {
          fetch("/api/dragon")
            .then((x) => x.json())
            .then((d) => {
              if (d?.ok) {
                setSt(d.state as DragonState);
                if (d.share && d.tg) setTokens({ share: d.share, tg: d.tg });
              }
            })
            .catch(() => {});
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (st && !st.todayCollected && !offerTracked.current) {
      offerTracked.current = true;
      track("dragon_offer_impression", { collected: st.collected });
    }
  }, [st]);

  /* 托盘换装：读本地偏好，与 AISprite / 图鉴同步 */
  useEffect(() => {
    try {
      if (localStorage.getItem("bl-skin-pref") === "loong") setSkinPref("loong");
    } catch {}
    const onApply = (e: Event) => {
      const s = (e as CustomEvent<{ skin?: string }>).detail?.skin;
      if (s === "loong" || s === "normal") setSkinPref(s);
    };
    window.addEventListener("bl:apply-skin", onApply);
    return () => window.removeEventListener("bl:apply-skin", onApply);
  }, []);

  /* 回访唤回：中断 ≥3 天但星珠还在 → 提示"没清零，接着集" */
  useEffect(() => {
    if (!st || comebackShown.current) return;
    if (st.collected === 0 || st.todayCollected || st.canSummon || st.wish) return;
    const last = st.days[st.days.length - 1];
    if (!last) return;
    const gapDays = (Date.now() - Date.parse(last)) / 86400000;
    if (gapDays >= 3) {
      comebackShown.current = true;
      showToast(t.comeback(st.collected));
      track("dragon_comeback", { collected: st.collected, gap: Math.floor(gapDays) });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st]);

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
  }, []);

  const showToast = (text: string) => {
    setToast(text);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 3600);
  };
  const showToastRef = useRef(showToast);
  showToastRef.current = showToast;

  /* 打开召唤：真召唤时刻（可许愿）且非降级环境 → 先放 canvas 界龙演出，收尾再出祈愿卡 */
  const openCeremony = (target: Phase) => {
    setPhase(target);
    const playFlight = target !== "result" && !reduced && !lowFx;
    setFlight(playFlight);
    setFlightGhost(false);
    setCeremony(true);
    dispatchLoongCeremony(true);
    if (playFlight) {
      track("dragon_flight", {});
      /* 演出后半段（canvas 掠空 2.4s 起）叠入真·祥龙本尊，收束「同龙现身」 */
      setTimeout(() => setFlightGhost(true), 2400);
    } else if (target !== "result" && (reduced || lowFx)) {
      showToast(t.lightFxNote);
    }
  };

  const closeCeremony = () => {
    setCeremony(false);
    setFlight(false);
    setFlightGhost(false);
    dispatchLoongCeremony(false);
  };

  const collect = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await fetch("/api/dragon", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "collect" }),
      }).then((x) => x.json());
      if (r?.state) setSt(r.state as DragonState);
      if (r?.ok && !r.already && r.index > 0) {
        track("dragon_pearl", { index: r.index, streak: r.state?.streak ?? 0 });
        showToast(t.collected(stars[Math.min(r.index, 7) - 1], r.index));
        if (r.index >= 7) {
          openCeremony("constellation");
          track("dragon_summon_open", {});
        } else if (r.index === 6) {
          setTimeout(() => showToast(t.oneMore), 3800);
        } else if (r.index === 3 && !teaser3Shown.current) {
          let already = false;
          try {
            already = sessionStorage.getItem("bl-dragon-teaser-3") === "1";
            if (!already) sessionStorage.setItem("bl-dragon-teaser-3", "1");
          } catch {}
          teaser3Shown.current = true;
          if (!already) {
            setTimeout(() => {
              showToast(t.teaser3);
              setGhostOn(true);
              dispatchLoongTeaser({
                collected: 3,
                text: lang === "zh" ? "三颗星珠已归位…界龙剪影初现…" : "Three pearls lit… the Loong stirs…",
              });
              track("dragon_teaser_impression", { collected: 3 });
              setTimeout(() => setGhostOn(false), 2800);
            }, 1200);
          }
        }
      }
    } catch {
      /* 网络失败静默，明日再来 */
    } finally {
      setBusy(false);
    }
  }, [busy, stars, t, lang]);

  const claimGrandReward = useCallback(async () => {
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
        track("dragon_grand", {});
      }
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  }, [busy]);

  const wish = useCallback(
    async (kind: WishKind) => {
      if (busy) return;
      setBusy(true);
      try {
        const r = await fetch("/api/dragon", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "wish", kind }),
        }).then((x) => x.json());
        if (r?.state) setSt(r.state as DragonState);
        if (r?.ok) {
          track("dragon_wish", { kind, perfect: r.state?.perfect ?? false });
          if (kind === "skin" || r.state?.loongSkin) applySkin("loong");
          setPhase("result");
        } else if (r?.reason === "already_wished") {
          setPhase("result");
        }
      } catch {
        /* ignore */
      } finally {
        setBusy(false);
      }
    },
    [busy]
  );

  if (!st) {
    if (!loadError) return null;
    return (
      <div className="pointer-events-none fixed bottom-[172px] right-[106px] z-[60] md:bottom-[128px] md:right-[172px]">
        <div className="pointer-events-auto flex items-center gap-2 rounded-full border border-amber-300/30 bg-[#0a0b14]/92 px-3 py-1.5 text-[11px] text-amber-100/90 shadow-lg backdrop-blur">
          <span>{t.loadFail}</span>
          <button
            type="button"
            className="rounded-full bg-amber-300/20 px-2 py-0.5 font-semibold text-amber-200 hover:bg-amber-300/30"
            onClick={() => {
              setLoadError(false);
              loadState();
            }}
          >
            {t.retry}
          </button>
        </div>
      </div>
    );
  }

  const offerVisible = !st.todayCollected && !st.canSummon;
  const nextStar = stars[Math.min(st.collected, 6)];
  const tgLink = st.code ? `https://t.me/${BOT_HANDLE}?start=loong_${st.code}` : "";
  const leftDays = daysLeft(st.cycleEndsAt);

  const copyAssistLink = () => {
    if (!tokens) return;
    try {
      const u = new URL(window.location.pathname, window.location.origin);
      u.searchParams.set("xz", tokens.share);
      u.searchParams.set("utm_source", "share");
      u.searchParams.set("utm_medium", "assist");
      navigator.clipboard?.writeText(u.toString());
      setShareCopied(true);
      setTimeout(() => setShareCopied(false), 2200);
      track("dragon_share_copy", { collected: st.collected, from: "result" });
    } catch {}
  };

  /* ---------- 子视图 ---------- */

  const Constellation = ({ compact = false, lit }: { compact?: boolean; lit: number }) => (
    <svg viewBox="0 0 100 64" className={compact ? "h-16 w-full" : "h-28 w-full"} fill="none" aria-hidden>
      <motion.path
        d={DIPPER_PATH}
        stroke={GOLD}
        strokeOpacity="0.45"
        strokeWidth="0.8"
        strokeDasharray="2 2.6"
        initial={reduced ? undefined : { pathLength: 0 }}
        animate={{ pathLength: lit >= 7 ? 1 : lit / 7 }}
        transition={{ duration: reduced ? 0 : 1.2, ease: "easeInOut" }}
      />
      {DIPPER.map(([x, y], i) => {
        const on = i < lit;
        return (
          <g key={i}>
            <motion.circle
              cx={x}
              cy={y}
              r={on ? 2.4 : 1.7}
              fill={on ? GOLD : "rgba(255,255,255,0.16)"}
              stroke={on ? GOLD : "rgba(255,255,255,0.3)"}
              strokeWidth="0.5"
              initial={false}
              animate={on && !reduced ? { opacity: [0.7, 1, 0.7], scale: [1, 1.18, 1] } : { opacity: on ? 1 : 0.8 }}
              transition={on && !reduced ? { repeat: Infinity, duration: 2.4 + i * 0.3, ease: "easeInOut" } : undefined}
              style={on ? { filter: `drop-shadow(0 0 2.5px ${GOLD})` } : undefined}
            />
            {!compact && (
              <text x={x} y={y + 8.5} textAnchor="middle" fontSize="4.4" fill={on ? "#f3dfa4" : "rgba(255,255,255,0.35)"}>
                {stars[i]}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );

  /** 界龙掠空：金色蛇躯沿弧线一次性画出，龙首衔珠收尾（P0 用 SVG 描边动画，P1 升级 canvas 长龙） */
  const LoongFlight = () => (
    <svg viewBox="0 0 340 120" className="pointer-events-none absolute inset-x-0 top-2 h-24 w-full" fill="none" aria-hidden>
      <defs>
        <linearGradient id="loong-body" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#38bdf8" stopOpacity="0" />
          <stop offset="35%" stopColor="#7dd3fc" />
          <stop offset="70%" stopColor={GOLD} />
          <stop offset="100%" stopColor="#fff1c2" />
        </linearGradient>
      </defs>
      <motion.path
        d="M-10 96 C 50 20, 120 116, 190 56 S 300 8, 322 40"
        stroke="url(#loong-body)"
        strokeWidth="7"
        strokeLinecap="round"
        initial={{ pathLength: 0, opacity: 0.2 }}
        animate={{ pathLength: 1, opacity: 1 }}
        transition={{ duration: reduced ? 0 : 1.6, ease: "easeInOut" }}
        style={{ filter: `drop-shadow(0 0 6px ${GOLD}66)` }}
      />
      {/* 鳞光：沿身细线 */}
      <motion.path
        d="M-10 96 C 50 20, 120 116, 190 56 S 300 8, 322 40"
        stroke="#fffbe8"
        strokeOpacity="0.5"
        strokeWidth="1.4"
        strokeDasharray="3 7"
        initial={{ pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ duration: reduced ? 0 : 1.6, ease: "easeInOut" }}
      />
      {/* 龙首 + 衔珠（路径终点 322,40 处） */}
      <motion.g
        initial={{ opacity: 0, scale: 0.6 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ delay: reduced ? 0 : 1.35, duration: 0.35, ease: "backOut" }}
        style={{ transformOrigin: "322px 40px" }}
      >
        {/* 头 */}
        <path d="M322 40 C316 30 320 24 328 26 C334 28 338 34 336 40 C334 46 326 46 322 40 Z" fill="#ffe9ad" stroke="#caa14e" strokeWidth="1" />
        {/* 鹿角 */}
        <path d="M327 26 C326 20 328 15 332 12 M330 27 C331 21 335 18 339 17" stroke="#caa14e" strokeWidth="1.4" fill="none" strokeLinecap="round" />
        {/* 眼 */}
        <circle cx="330" cy="33" r="1.4" fill="#7c2d12" />
        {/* 须 */}
        <path d="M335 41 C339 43 341 47 340 51 M333 43 C334 47 333 51 330 53" stroke="#eab308" strokeWidth="0.9" fill="none" strokeLinecap="round" />
        {/* 衔珠 */}
        <motion.circle
          cx="340"
          cy="37"
          r="4"
          fill={GOLD}
          animate={reduced ? undefined : { opacity: [0.75, 1, 0.75], scale: [1, 1.12, 1] }}
          transition={{ repeat: Infinity, duration: 1.8, ease: "easeInOut" }}
          style={{ filter: `drop-shadow(0 0 5px ${GOLD})`, transformOrigin: "340px 37px" }}
        />
      </motion.g>
    </svg>
  );

  const WishCards = () => (
    <div className="mt-3 grid gap-2">
      {(
        [
          { kind: "trial" as WishKind, name: t.wishTrial, desc: st.trialLeft > 0 ? t.wishTrialDesc(st.trialLeft) : t.wishTrialFull, disabled: st.trialLeft <= 0, icon: "🗝️" },
          { kind: "skin" as WishKind, name: t.wishSkin, desc: t.wishSkinDesc, disabled: false, icon: "🐉" },
          { kind: "gift" as WishKind, name: t.wishGift, desc: t.wishGiftDesc, disabled: false, icon: "🎁" },
        ]
      ).map((c) => (
        <button
          key={c.kind}
          type="button"
          disabled={c.disabled || busy}
          onClick={() => wish(c.kind)}
          className={`group flex items-center gap-3 rounded-xl border p-3 text-left transition ${
            c.disabled
              ? "cursor-not-allowed border-white/10 bg-white/[0.03] opacity-50"
              : "border-amber-300/25 bg-amber-300/[0.06] hover:border-amber-300/60 hover:bg-amber-300/[0.12]"
          }`}
        >
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full border border-amber-300/30 bg-black/40 text-lg">{c.icon}</span>
          <span className="min-w-0">
            <span className="block text-sm font-bold text-amber-100">{c.name}</span>
            <span className="block text-xs leading-snug text-zinc-400">{c.desc}</span>
          </span>
        </button>
      ))}
    </div>
  );

  /* 月令龙鳞区块（面板与祈愿结果页共用）：三槽进度 + 大成兑换 + 兑换码 */
  const ScalesBlock = () => {
    if (!(st.scales > 0 || st.grandReady || grandCode)) return null;
    return (
      <div className="mt-3 border-t border-white/10 pt-2.5">
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-[11px] font-bold text-amber-200/90">{t.scalesTitle}</span>
          <span className="flex gap-1.5">
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="grid h-5 w-5 place-items-center rounded-full border text-[10px]"
                style={
                  i < Math.min(3, st.scales)
                    ? { borderColor: `${GOLD}88`, background: `${GOLD}22`, color: GOLD, boxShadow: `0 0 6px ${GOLD}55` }
                    : { borderColor: "rgba(255,255,255,0.14)", color: "rgba(255,255,255,0.25)" }
                }
              >
                鳞
              </span>
            ))}
          </span>
        </div>
        <div className="text-[10px] leading-relaxed text-zinc-600">{t.scalesHint}</div>
        {grandCode ? (
          <div className="dragon-grand-code mt-2 rounded-lg border border-amber-300/30 bg-black/50 p-2.5">
            <div className="text-[10px] uppercase tracking-wider text-zinc-500">{t.grandCode}</div>
            <code className="mt-0.5 block font-mono text-sm font-bold tracking-wider text-amber-200">{grandCode}</code>
            <a
              href={`https://t.me/${BOT_HANDLE}?start=loong_${grandCode}`}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => track("dragon_tg_click", { kind: "grand" })}
              className="mt-1.5 inline-block rounded-full bg-gradient-to-r from-amber-400 to-yellow-300 px-3 py-1 text-xs font-bold text-black"
            >
              ✈️ {t.toTg}
            </a>
          </div>
        ) : st.grandReady ? (
          <button
            type="button"
            disabled={busy}
            onClick={claimGrandReward}
            className="dragon-grand-btn mt-2 w-full rounded-lg bg-gradient-to-r from-amber-400 to-yellow-300 px-2.5 py-1.5 text-xs font-bold text-black shadow-[0_0_14px_rgba(245,197,66,0.35)] hover:brightness-105"
          >
            🐲 {t.grandClaim}
            <span className="block text-[10px] font-medium opacity-80">{t.grandDesc}</span>
          </button>
        ) : null}
      </div>
    );
  };

  const ResultView = () => (
    <div className="mt-3">
      {st.perfect && <div className="mb-2 text-xs font-medium text-amber-300">{t.perfect}</div>}
      {st.wish === "skin" ? (
        <div className="rounded-xl border border-amber-300/30 bg-amber-300/[0.08] p-4 text-sm text-amber-100">
          {t.skinDone} 🐉
        </div>
      ) : st.code ? (
        <div className="rounded-xl border border-amber-300/30 bg-black/50 p-4">
          <div className="text-[11px] uppercase tracking-wider text-zinc-500">{t.codeLabel}</div>
          <div className="mt-1 flex items-center gap-2">
            <code className="rounded bg-white/10 px-2 py-1 font-mono text-base font-bold tracking-wider text-amber-200">{st.code}</code>
            <button
              type="button"
              className="rounded border border-white/15 px-2 py-1 text-xs text-zinc-300 hover:bg-white/10"
              onClick={() => {
                try {
                  navigator.clipboard?.writeText(st.code ?? "");
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1600);
                } catch {}
              }}
            >
              {copied ? t.copied : t.copy}
            </button>
          </div>
          <a
            href={tgLink}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => track("dragon_tg_click", { kind: st.wish })}
            className="dragon-tg-link mt-3 inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-amber-400 to-yellow-300 px-4 py-2 text-sm font-bold text-black shadow-[0_0_18px_rgba(245,197,66,0.35)] hover:brightness-105"
          >
            ✈️ {t.toTg}
          </a>
        </div>
      ) : (
        <div className="rounded-xl border border-white/10 bg-white/[0.04] p-4 text-sm text-zinc-300">{t.wished}</div>
      )}
      {/* 高潮瞬间主推裂变：有助力余量时优先邀请 */}
      {tokens && st.assistsLeft > 0 && (
        <button
          type="button"
          className="dragon-share-btn mt-3 w-full rounded-xl border border-amber-300/40 bg-amber-300/15 px-3 py-2.5 text-sm font-bold text-amber-100 hover:bg-amber-300/25"
          onClick={copyAssistLink}
        >
          🔗 {shareCopied ? t.resultShareDone : t.resultShare}
        </button>
      )}
      {st.loongSkin && st.wish !== "skin" && (
        <button
          type="button"
          className="mt-2 text-xs text-amber-300/90 underline-offset-2 hover:underline"
          onClick={() => applySkin("loong")}
        >
          {t.wearLoong} →
        </button>
      )}
      <ScalesBlock />
      <div className="mt-3 flex flex-wrap items-center gap-3 text-[11px] text-zinc-500">
        <span>{t.newCycle}</span>
        <a href="/loong" onClick={() => track("dragon_codex_open", { from: "result" })} className="text-amber-300/90 underline-offset-2 hover:underline">
          {lang === "zh" ? "我的图鉴" : "My codex"} →
        </a>
      </div>
    </div>
  );

  return (
    <>
      {/* ── 悬浮簇：今日星珠 + 北斗托盘。放机器人左侧下方——头顶区留给全息播报面板
          （bottom≈345..435 是播报区，放那会互相遮挡且拦截播报点击） ── */}
      <div className="pointer-events-none fixed bottom-[172px] right-[106px] z-[60] flex flex-col items-end gap-2 md:bottom-[128px] md:right-[172px]">
        <AnimatePresence>
          {showGuide && offerVisible && (
            <motion.div
              className="dragon-guide pointer-events-auto flex max-w-[200px] items-center gap-2 rounded-full border border-amber-300/35 bg-[#120e08]/92 py-1 pl-3 pr-1.5 text-[11px] text-amber-100 shadow-[0_0_16px_rgba(245,197,66,0.2)] backdrop-blur"
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
            >
              <span className="leading-snug">{t.guide}</span>
              <button
                type="button"
                className="shrink-0 rounded-full bg-amber-300/20 px-2 py-0.5 font-semibold text-amber-200 hover:bg-amber-300/30"
                onClick={() => {
                  setShowGuide(false);
                  try {
                    sessionStorage.setItem("bl-dragon-guide", "1");
                  } catch {}
                  track("dragon_guide_dismiss", {});
                }}
              >
                {t.guideDismiss}
              </button>
            </motion.div>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {offerVisible && (
            <motion.button
              type="button"
              aria-label={`${t.todayPearl} · ${nextStar}`}
              className="dragon-pearl-offer pointer-events-auto flex items-center gap-2 rounded-full border border-amber-300/45 bg-[#120e08]/88 py-1.5 pl-1.5 pr-3 shadow-[0_4px_18px_rgba(0,0,0,0.45),0_0_14px_rgba(245,197,66,0.18)] backdrop-blur-md hover:border-amber-300/80"
              initial={{ opacity: 0, y: 8, scale: 0.86 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, scale: 0.5, y: -14, transition: { duration: 0.35 } }}
              whileHover={{ scale: 1.04 }}
              whileTap={{ scale: 0.94 }}
              onClick={collect}
              disabled={busy}
            >
              <motion.span
                className="grid h-7 w-7 place-items-center rounded-full"
                style={{ background: `radial-gradient(circle at 34% 30%, #fffbe8 0%, ${GOLD} 55%, #b8860b 100%)`, boxShadow: `0 0 10px ${GOLD}88` }}
                animate={reduced ? undefined : { scale: [1, 1.1, 1] }}
                transition={{ repeat: Infinity, duration: 2, ease: "easeInOut" }}
              >
                <span className="text-[10px]">✦</span>
              </motion.span>
              <span className="text-left leading-tight">
                <span className="block text-[10px] text-zinc-400">
                  {st.collected >= 5 ? t.sprint(7 - st.collected) : t.todayPearl}
                </span>
                <span className="block text-xs font-bold text-amber-200">{nextStar} · {t.collect}</span>
              </span>
            </motion.button>
          )}
        </AnimatePresence>

        {(st.collected > 0 || st.summons > 0) && (
          <div className="pointer-events-auto flex items-center gap-1">
            <button
              type="button"
              aria-label={t.trayTitle}
              className="dragon-tray flex items-center gap-1.5 rounded-full border border-amber-300/25 bg-[#120e08]/88 px-2.5 py-1.5 backdrop-blur-md hover:border-amber-300/55"
              onClick={() => {
                if (st.canSummon) {
                  openCeremony("constellation");
                  track("dragon_summon_open", {});
                } else if (st.wish) {
                  openCeremony("result");
                } else {
                  setOpen((v) => !v);
                }
              }}
            >
              {Array.from({ length: 7 }, (_, i) => (
                <span
                  key={i}
                  className="block h-1.5 w-1.5 rounded-full"
                  style={
                    i < st.collected
                      ? { background: GOLD, boxShadow: `0 0 4px ${GOLD}` }
                      : { background: "rgba(255,255,255,0.18)" }
                  }
                />
              ))}
              {st.canSummon && <span className="ml-1 text-[10px] font-bold text-amber-300 animate-pulse">🐉</span>}
            </button>
            {/* 解锁后托盘旁一键换装（比只藏在面板里更可发现） */}
            {st.loongSkin && (
              <button
                type="button"
                aria-label={skinPref === "loong" ? t.skinEve : t.skinLoong}
                title={skinPref === "loong" ? t.skinEve : t.skinLoong}
                className="dragon-tray-skin grid h-8 w-8 place-items-center rounded-full border border-amber-300/30 bg-[#120e08]/88 text-sm hover:border-amber-300/60"
                onClick={() => {
                  const next = skinPref === "loong" ? "normal" : "loong";
                  applySkin(next);
                  setSkinPref(next);
                  track("dragon_tray_skin_switch", { skin: next, from: "pill" });
                }}
              >
                {skinPref === "loong" ? "✦" : "🐉"}
              </button>
            )}
          </div>
        )}

        {/* 收珠 toast */}
        <AnimatePresence>
          {toast && (
            <motion.div
              className="pointer-events-none rounded-lg border border-amber-300/30 bg-[#120e08]/92 px-3 py-1.5 text-xs font-medium text-amber-100 backdrop-blur"
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
            >
              {toast}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* 第 3 珠真剪影：叠在精灵附近，兑现「剪影初现」文案 */}
      <AnimatePresence>
        {ghostOn && (
          <motion.div
            className="pointer-events-none fixed bottom-[200px] right-[8px] z-[55] w-28 md:bottom-[160px] md:right-[48px] md:w-36"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <LoongGhost className="h-full w-full" />
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── 进度面板（未集齐时点托盘展开） ── */}
      <AnimatePresence>
        {open && !ceremony && (
          <motion.div
            className="dragon-tray-panel fixed bottom-[224px] right-2 z-[70] w-64 rounded-2xl border border-amber-300/20 bg-gradient-to-b from-[#16110a]/96 to-[#0a0b14]/96 p-4 shadow-[0_12px_40px_rgba(0,0,0,0.55),0_0_28px_rgba(245,197,66,0.08)] backdrop-blur-xl md:bottom-[186px] md:right-[172px]"
            initial={{ opacity: 0, y: 10, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.95 }}
          >
            <div className="mb-1 flex items-center justify-between">
              <div className="text-xs font-bold tracking-wide text-amber-200">{t.trayTitle}</div>
              <button type="button" className="text-zinc-500 hover:text-zinc-300" aria-label={t.close} onClick={() => setOpen(false)}>
                ✕
              </button>
            </div>
            {/* 状态条：已集 / 连续 / 剩余天 —— 解决「规则厚但进度看不见」 */}
            <div className="mb-2 flex flex-wrap gap-1.5 text-[10px]">
              <span className="rounded-full border border-amber-300/25 bg-amber-300/10 px-2 py-0.5 font-semibold text-amber-200">
                {t.statusCollected(st.collected)}
              </span>
              <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-zinc-300">
                {t.statusStreak(st.streak)}
              </span>
              {st.collected > 0 && (
                <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-zinc-300">
                  {t.statusDaysLeft(leftDays)}
                </span>
              )}
            </div>
            <Constellation lit={st.collected} />
            <button
              type="button"
              className="mt-1 text-[10px] text-zinc-500 underline-offset-2 hover:text-zinc-300 hover:underline"
              onClick={() => setRulesOpen((v) => !v)}
            >
              {t.rulesToggle} {rulesOpen ? "▴" : "▾"}
            </button>
            {rulesOpen && <div className="mt-1 text-[11px] leading-relaxed text-zinc-500">{t.rules}</div>}

            {/* 月令龙鳞：三鳞兑「界龙之约」 */}
            <ScalesBlock />

            {/* 快捷换装：已解锁祥龙时可在托盘内司星者 ↔ 祥龙 */}
            {st.loongSkin && (
              <div className="mt-3 flex items-center gap-1 rounded-lg border border-white/10 bg-black/40 p-1">
                {(
                  [
                    { id: "normal" as const, label: t.skinEve },
                    { id: "loong" as const, label: t.skinLoong },
                  ] as const
                ).map((opt) => {
                  const on = skinPref === opt.id;
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      className={`flex-1 rounded-md px-2 py-1.5 text-[11px] font-semibold transition ${
                        on ? "bg-amber-300/20 text-amber-200" : "text-zinc-500 hover:text-zinc-300"
                      }`}
                      onClick={() => {
                        if (skinPref === opt.id) return;
                        applySkin(opt.id);
                        setSkinPref(opt.id);
                        track("dragon_tray_skin_switch", { skin: opt.id });
                      }}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            )}

            {tokens && (
              <div className="mt-3 space-y-1.5 border-t border-white/10 pt-2.5">
                {st.assistsLeft > 0 && (
                  <>
                    <button
                      type="button"
                      className="dragon-share-btn w-full rounded-lg border border-amber-300/30 bg-amber-300/[0.07] px-2.5 py-1.5 text-left text-xs font-medium text-amber-200 hover:bg-amber-300/[0.14]"
                      onClick={() => {
                        try {
                          const u = new URL(window.location.pathname, window.location.origin);
                          u.searchParams.set("xz", tokens.share);
                          u.searchParams.set("utm_source", "share");
                          u.searchParams.set("utm_medium", "assist");
                          navigator.clipboard?.writeText(u.toString());
                          setShareCopied(true);
                          setTimeout(() => setShareCopied(false), 2200);
                          track("dragon_share_copy", { collected: st.collected });
                        } catch {}
                      }}
                    >
                      🔗 {shareCopied ? t.shareCopied : t.share}
                    </button>
                    <div className="text-[10px] leading-relaxed text-zinc-600">{t.shareHint(st.assistsLeft)}</div>
                  </>
                )}
                <a
                  href={`https://t.me/${BOT_HANDLE}?start=${tokens.tg}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => track("dragon_tg_sync_click", { bound: st.tgBound })}
                  className="block text-[11px] text-sky-300/90 underline-offset-2 hover:underline"
                >
                  ✈️ {t.tgSync}{st.tgBound ? " ✓" : ""}
                </a>
                <a
                  href="/loong"
                  onClick={() => track("dragon_codex_open", { from: "tray" })}
                  className="dragon-codex-link block text-[11px] text-amber-300/90 underline-offset-2 hover:underline"
                >
                  📖 {lang === "zh" ? "查看我的图鉴" : "My codex"} →
                </a>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── 召唤仪式：canvas 界龙演出 → 祈愿 / 结果 ── */}
      <AnimatePresence>
        {ceremony && (
          <motion.div
            className="fixed inset-0 z-[300] grid place-items-center bg-black/75 p-4 backdrop-blur-sm"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={(e) => {
              if (e.target !== e.currentTarget) return;
              if (flight) {
                setFlight(false);
                setFlightGhost(false);
                track("dragon_flight_skip", {});
              } else {
                closeCeremony();
              }
            }}
          >
            {/* 界龙掠空 + 后半段同源剪影收束 */}
            {flight && (
              <>
                <LoongCeremony
                  onDone={() => {
                    setFlight(false);
                    setFlightGhost(false);
                  }}
                />
                {/* 收束高潮：canvas 长龙掠空后，真·祥龙本尊金光现身（同一 IP 收束） */}
                <AnimatePresence>
                  {flightGhost && (
                    <motion.div
                      className="pointer-events-none absolute inset-0 grid place-items-center"
                      initial={{ opacity: 0, scale: 0.7 }}
                      animate={{ opacity: 1, scale: 1 }}
                      exit={{ opacity: 0, scale: 1.06 }}
                      transition={{ duration: 0.55, ease: "backOut" }}
                    >
                      <div className="relative">
                        <motion.div
                          className="absolute left-1/2 top-1/2 -z-10 h-72 w-72 -translate-x-1/2 -translate-y-1/2 rounded-full"
                          style={{ background: `radial-gradient(circle, ${GOLD}33 0%, ${GOLD}14 45%, transparent 70%)` }}
                          animate={{ scale: [0.9, 1.12, 0.98], opacity: [0.5, 1, 0.85] }}
                          transition={{ duration: 1.3, ease: "easeOut" }}
                        />
                        <LoongHero mode="flying" className="scale-[1.35] md:scale-[1.7]" />
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
                <motion.div
                  className="pointer-events-none absolute inset-x-0 bottom-[16%] text-center"
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.9, duration: 0.7 }}
                >
                  <div className="text-2xl font-black tracking-[0.2em] text-amber-200" style={{ textShadow: "0 0 24px rgba(245,197,66,0.6)" }}>
                    {t.ceremonyTitle}
                  </div>
                  <div className="mt-1 text-sm text-amber-100/70">{t.ceremonySub}</div>
                </motion.div>
                <button
                  type="button"
                  className="absolute right-5 top-5 rounded-full border border-white/20 bg-black/40 px-3 py-1 text-xs text-zinc-300 backdrop-blur hover:bg-black/60"
                  onClick={(e) => {
                    e.stopPropagation();
                    setFlight(false);
                    setFlightGhost(false);
                    track("dragon_flight_skip", {});
                  }}
                >
                  {t.skip}
                </button>
              </>
            )}

            {!flight && (
              <motion.div
                className="relative w-full max-w-md overflow-hidden rounded-2xl border border-amber-300/25 bg-gradient-to-b from-[#101322] to-[#0a0b14] p-5 shadow-[0_0_60px_rgba(245,197,66,0.15)]"
                initial={{ opacity: 0, scale: 0.92, y: 14 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.94, y: 10 }}
              >
                <button
                  type="button"
                  className="absolute right-3 top-3 z-10 text-zinc-500 hover:text-zinc-200"
                  aria-label={t.close}
                  onClick={closeCeremony}
                >
                  ✕
                </button>

                {/* 降级环境：同源剪影 + 轻量 SVG，仪式感不缺席且与吉祥物同画种 */}
                {phase !== "result" && (reduced || lowFx) && (
                  <div className="relative mx-auto mb-1 flex h-28 w-full items-center justify-center">
                    <LoongGhost className="absolute h-28 w-20" pulse={false} />
                    <LoongFlight />
                  </div>
                )}

                {/* 祈愿卡顶部：同龙现身（canvas 结束后强化识别） */}
                {phase !== "result" && !reduced && !lowFx && (
                  <div className="mb-1 flex justify-center">
                    <LoongGhost className="h-16 w-12" pulse={false} />
                  </div>
                )}

                <div className="pt-1">
                  <div className="text-center">
                    <div className="text-lg font-black tracking-wide text-amber-200">{t.ceremonyTitle}</div>
                    <div className="mt-0.5 text-xs text-zinc-400">{t.ceremonySub}</div>
                  </div>
                  <Constellation compact lit={st.collected >= 7 ? 7 : st.collected} />

                  {phase === "result" || st.wish ? (
                    <ResultView />
                  ) : st.canSummon ? (
                    <WishCards />
                  ) : null}
                </div>
              </motion.div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
