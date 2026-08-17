"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  motion,
  animate,
  useSpring,
  useVelocity,
  useAnimationFrame,
  useTransform,
  useScroll,
  useMotionTemplate,
  useMotionValue,
  AnimatePresence,
  type Variants,
  type MotionValue,
} from "framer-motion";
import { useReducedMotionSafe } from "@/components/fx/useReducedMotionSafe";
import { Activity } from "lucide-react";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";
import BatSwarm, { type BatFlight } from "./BatSwarm";
import {
  LOONG_APPLY_SKIN,
  LOONG_CEREMONY,
  LOONG_TEASER,
  type LoongApplySkinDetail,
  type LoongCeremonyDetail,
  type LoongTeaserDetail,
} from "@/lib/loong-events";
import { attnActive, DOCK_EVENT, type DockDetail } from "@/lib/dock";
import {
  NewsHologram,
  DemonEmbers,
  buildBodyVariants,
  type BotMode,
  type DemonProps,
  type EyeExpr,
} from "./forms/formShared";
import { LoongForm } from "./forms/LoongForm";
import { EveBot } from "./forms/EveBot";
import { DemonForm } from "./forms/DemonForm";
import { SKIN, type Skin } from "./forms/formShared";

export type { BotMode, Skin } from "./forms/formShared";
export { SKIN } from "./forms/formShared";
export { LoongForm };
export { EveBot } from "./forms/EveBot";
export { DemonForm } from "./forms/DemonForm";

/**
 * 无界科技 · 会飞的 AI 机器人（EveBot）。
 * 桌面端：漂浮/飞行（带俯仰与着陆回弹）/表情/资讯全息/眼神跟随；悬停 → 低位五指挥手问好。
 * 移动端：轻量版（缩放 55%，仅待机动画与播报，无飞行/避让）。
 * 点击机器人 → 派发 `bl:open-chat` 打开 AI 客服；点击全息播报 → 带该版块种子问题开客服。
 * 碰撞避让零逐帧 DOM 读取。调试：URL 加 ?robot=idle_news 等可锁定姿态。
 */

/** 机器人容器尺寸（px），与 w-32 h-44 保持一致 */
const BOT_W = 128;
const BOT_H = 176;

/**
 * 休息位（相对视口右下角，px）。bottom=160 经矩形推算：
 * md 断点客服按钮避让区（含 18px padding）上缘在视口底部 154px 处，
 * 机器人下缘 160px > 154px，静止时避让弹簧不再持续发力（原 bottom-24 恒被推挤）。
 */
const HOME = { right: 24, bottom: 160 };


/** 通用资讯池（未识别到特定版块时使用） */
const NEWS = {
  zh: ["扫描出海获客机会…", "AI 拟人翻译已就绪…", "多号矩阵 7×24 运转中…", "监测实时换脸链路…", "分析客户成交意向…", "同步 6 大产品能力…", "私有部署 · 数据不出网…", "自动跟单催单进行中…"],
  en: ["Scanning lead-gen ops…", "Human-like translation ready…", "Multi-account matrix 24/7…", "Monitoring live face-swap…", "Analyzing buyer intent…", "Syncing 6 product lines…", "Private deploy · off-net…", "Auto follow-up running…"],
};

/** 场景化资讯池：随访客正在浏览的版块切换话术（IntersectionObserver 感知） */
const SECTION_NEWS: Record<"zh" | "en", Record<string, string[]>> = {
  zh: {
    autochat: ["AI 正在自动接待询盘…", "拟人回复 · 客户无感知…", "自动成交流程演示中…"],
    products: ["6 大引擎能力已就绪…", "翻译 · 换脸 · 矩阵一站集成…", "挑一个引擎试试？"],
    pricing: ["按需订阅 · 支持私有化…", "算一算你的获客 ROI…", "方案可按业务定制…"],
    cases: ["实测数据 · 转化提升显著…", "看看同行的用法…"],
    proof: ["真实交付截图在此…", "数据不注水 · 可复核…"],
    contact: ["留下需求 · 1 对 1 方案…", "工程师在线 · 随时可聊…"],
  },
  en: {
    autochat: ["AI answering inquiries live…", "Human-like replies, seamless…", "Auto-closing demo running…"],
    products: ["6 engines ready to deploy…", "Translate · Swap · Matrix in one…", "Pick an engine to try?"],
    pricing: ["Subscribe or self-host…", "Estimate your lead-gen ROI…", "Plans tailored to your ops…"],
    cases: ["Field-tested conversion lift…", "See how peers use it…"],
    proof: ["Real delivery screenshots…", "Verifiable numbers only…"],
    contact: ["Leave a brief, get a plan…", "Engineers online now…"],
  },
};

/** 点击全息播报时带进客服的种子问题：把“被动曝光”直接变成对话线索 */
const SECTION_SEED: Record<"zh" | "en", Record<string, string>> = {
  zh: {
    top: "介绍一下你们的核心能力和适合我的方案",
    autochat: "AI 自动成交聊天怎么部署？怎么收费？",
    products: "帮我介绍下你们 6 大产品能力分别解决什么问题",
    pricing: "帮我算一下价格方案和获客 ROI",
    cases: "有哪些实测案例和转化数据？",
    proof: "交付数据和真实截图能详细讲讲吗？",
    contact: "我想要 1 对 1 定制方案，怎么对接？",
  },
  en: {
    top: "Give me an overview of your core capabilities and the right plan for me",
    autochat: "How do I deploy AI auto-closing chat, and what does it cost?",
    products: "Walk me through your 6 product lines and what each solves",
    pricing: "Help me estimate pricing and lead-gen ROI",
    cases: "What field-tested cases and conversion data do you have?",
    proof: "Can you detail your delivery data and real screenshots?",
    contact: "I want a tailored 1-on-1 plan — how do we start?",
  },
};

/** 挥手问候等一次性行为的会话级标记 */
const GREET_KEY = "bl-sprite-greeted";
/** 恶魔彩蛋皮肤的会话级持久化标记（刷新/翻页保持，关标签页即恢复默认） */
const SKIN_KEY = "bl-sprite-skin";
/** 常驻皮肤偏好（localStorage，跨会话）：龙珠彩蛋解锁的祥龙金鳞等；恶魔仍是会话级彩蛋 */
const SKIN_PREF_KEY = "bl-skin-pref";
/** 解锁彩蛋所需连击次数 + 连击有效窗口（ms） */
const DEMON_CLICKS = 7;
const COMBO_WINDOW = 1400;
/** 单击开客服的去抖时长：短于此的连续点击算“连击”，不重复开客服，也不让面板盖住机器人 */
const OPEN_DEBOUNCE = 300;

/** 点击这些元素不触发机器人飞行（避免干扰正常交互） */
const FLY_IGNORE = "a,button,input,textarea,select,label,[role='button'],[data-robot-avoid='true'],.ai-sprite-container";

/** 调试用的可锁定姿态白名单（URL ?robot=idle_news 等），供视觉回归与联调 */
const DEBUG_MODES: BotMode[] = ["idle_wave", "idle_news", "idle_dance", "idle_scan", "idle_spin", "flying", "falling"];

export default function AISprite() {
  const { lang } = useLang();
  // 水合安全版：入场 initial 按 reduced 分叉，SSR/首帧必须一致（forms 系列经 props 透传共用）
  const reduced = useReducedMotionSafe();
  const { scrollY } = useScroll();
  const [mode, setMode] = useState<BotMode>("idle_base");
  const [newsText, setNewsText] = useState("");
  const [newsCta, setNewsCta] = useState("");
  const [questNews, setQuestNews] = useState(false);
  const [ceremonyActive, setCeremonyActive] = useState(false);
  const ceremonyPauseRef = useRef(false);
  /* 互动坞占用：聊天面板打开 → 精灵淡出让位；任意浮层活跃 → 播报让位 */
  const [chatDock, setChatDock] = useState(false);
  const dockBusyRef = useRef(false);
  const [isHovered, setIsHovered] = useState(false);
  /* SSR 先按桌面渲染，挂载后由 matchMedia 校正；移动端走轻量行为分级 */
  const [isDesktop, setIsDesktop] = useState(true);
  /* 低配挡位：复用 layout 首帧判定的 html[data-fx]，与全站背景特效同步降档 */
  const [lowFx, setLowFx] = useState(false);
  /* 隐藏彩蛋：连点 7 次切换恶魔皮肤（纯外观）。transforming=变身闪光；charge=蓄力预告强度 0..1 */
  const [skin, setSkin] = useState<Skin>("normal");
  const [transforming, setTransforming] = useState(false);
  const [charge, setCharge] = useState(0);
  /* 蝙蝠群：飞行/变身时的消散→群飞→聚合层；dissolved 时隐藏 DOM 本体，让蝠群接管 */
  const [batFlight, setBatFlight] = useState<BatFlight | null>(null);
  const [dissolved, setDissolved] = useState(false);
  const batIdRef = useRef(0);
  const skinRef = useRef<Skin>("normal");
  skinRef.current = skin;
  const isHoveredRef = useRef(false);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 768px)");
    const apply = () => setIsDesktop(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    setLowFx(document.documentElement.getAttribute("data-fx") === "low");
    const combo = comboRef.current;
    return () => {
      mq.removeEventListener("change", apply);
      if (combo.timer) clearTimeout(combo.timer);
      if (combo.openTimer) clearTimeout(combo.openTimer);
    };
  }, []);

  /* ---- 运动值：全部走 MotionValue 直驱，动画期间零 React 重渲染 ---- */
  const springScrollVelocity = useVelocity(scrollY);
  const clickX = useSpring(0, { stiffness: 60, damping: 15 });
  const clickY = useSpring(0, { stiffness: 60, damping: 15 });
  const flightVelX = useVelocity(clickX);
  const flightVelY = useVelocity(clickY);
  const avoidX = useSpring(0, { stiffness: 100, damping: 20 });
  const avoidY = useSpring(0, { stiffness: 100, damping: 20 });
  const rawDragY = useTransform(springScrollVelocity, [-3000, 3000], [-150, 150]);
  const smoothDragY = useSpring(rawDragY, { stiffness: 100, damping: 20 });
  const zeroMV = useMotionValue(0);
  /* 滚动俯仰 + 飞行垂直俯仰：叠加为总俯仰角（rotateX） */
  const tiltRaw = useTransform(springScrollVelocity, (v) => Math.max(Math.min(v * 0.05, 30), -30));
  const tiltSpring = useSpring(tiltRaw, { stiffness: 200, damping: 28 });
  const pitchRaw = useTransform(flightVelY, (v) => Math.max(Math.min(-v * 0.02, 14), -14));
  const pitchSpring = useSpring(pitchRaw, { stiffness: 140, damping: 18 });
  const totalTilt = useTransform([tiltSpring, pitchSpring], (vals) => (vals[0] as number) + (vals[1] as number));
  const flightRotateRaw = useTransform(flightVelX, (v) => Math.max(Math.min(v * 0.05, 30), -30));
  const flightRotate = useSpring(flightRotateRaw, { stiffness: 140, damping: 18 });
  const gazeX = useSpring(0, { stiffness: 120, damping: 16 });
  const gazeY = useSpring(0, { stiffness: 120, damping: 16 });
  /* 着陆回弹（scaleY），由 rAF 在飞行/坠落结束瞬间触发一次 */
  const squashY = useMotionValue(1);
  /* 悬浮光池亮度：离家越远（飞行/拖拽）越暗；desktopFlag 让移动端忽略拖拽项 */
  const desktopFlag = useMotionValue(1);
  useEffect(() => {
    desktopFlag.set(!reduced && isDesktop ? 1 : 0);
  }, [reduced, isDesktop, desktopFlag]);
  const shadowOpacity = useTransform([clickX, clickY, smoothDragY, desktopFlag], (vals) => {
    const [x, y, d, f] = vals as number[];
    const away = Math.hypot(x, y) * 0.9 + Math.abs(d * f);
    return Math.max(0.1, 0.55 - away / 320);
  });
  const dragTerm = reduced || !isDesktop ? zeroMV : smoothDragY;
  const combinedY = useMotionTemplate`calc(${clickY}px + ${dragTerm}px + ${avoidY}px)`;
  const combinedX = useMotionTemplate`calc(${clickX}px + ${avoidX}px)`;

  /* ---- 缓存：休息位坐标 + 避让区矩形，rAF 内零 DOM 读取 ---- */
  const homeRef = useRef({ left: 0, top: 0 });
  const avoidRectsRef = useRef<Array<{ l: number; t: number; r: number; b: number }>>([]);
  const currentSectionRef = useRef<string>("top");
  const greetUntilRef = useRef(0);
  const homeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastHoverTrackRef = useRef(0);
  const lastFlyTrackRef = useRef(0);
  const forcedModeRef = useRef<BotMode | null>(null);
  /* 连击彩蛋：count 计数、timer 连击窗口、openTimer 单击开客服去抖 */
  const comboRef = useRef<{ count: number; timer: ReturnType<typeof setTimeout> | null; openTimer: ReturnType<typeof setTimeout> | null }>({ count: 0, timer: null, openTimer: null });

  /* ---- 调试模式：?robot=idle_news 锁定姿态；?skin=demon 直接进恶魔态，供视觉回归与联调 ---- */
  useEffect(() => {
    try {
      const q = new URLSearchParams(window.location.search);
      const m = q.get("robot") as BotMode | null;
      if (m && DEBUG_MODES.includes(m)) {
        forcedModeRef.current = m;
        if (m === "idle_news") setNewsText((NEWS[lang] ?? NEWS.en)[0]);
        setMode(m);
      }
      /* 恢复皮肤：恶魔=会话级彩蛋（sessionStorage）优先，祥龙=常驻偏好（localStorage）；
         ?skin=demon|loong 供调试/回归强制进入 */
      const forceSkin = q.get("skin");
      let persisted = "";
      let pref = "";
      try {
        persisted = sessionStorage.getItem(SKIN_KEY) ?? "";
      } catch {}
      try {
        pref = localStorage.getItem(SKIN_PREF_KEY) ?? "";
      } catch {}
      if (forceSkin === "demon" || persisted === "demon") setSkin("demon");
      else if (forceSkin === "loong" || pref === "loong") setSkin("loong");
    } catch {}
  }, [lang]);

  /* 龙珠彩蛋等外部入口切换皮肤 */
  useEffect(() => {
    const onApply = (e: Event) => {
      const s = (e as CustomEvent<LoongApplySkinDetail>).detail?.skin;
      if (s === "loong" || s === "normal") {
        setSkin(s);
        try {
          localStorage.setItem(SKIN_PREF_KEY, s);
        } catch {}
        try {
          sessionStorage.removeItem(SKIN_KEY);
        } catch {}
      } else if (s === "demon") {
        setSkin("demon");
        try {
          sessionStorage.setItem(SKIN_KEY, "demon");
        } catch {}
      }
    };
    window.addEventListener(LOONG_APPLY_SKIN, onApply);
    return () => window.removeEventListener(LOONG_APPLY_SKIN, onApply);
  }, []);

  /* 第 3 珠龙影预告：全息播报 + 任务态 CTA（打开星图，不抢销售咨询） */
  useEffect(() => {
    const onTeaser = (e: Event) => {
      if (reduced || document.hidden) return;
      if (forcedModeRef.current || dockBusyRef.current) return;
      /* 台上有自动气泡（星珠/招呼）时不抢戏——龙影预告让位本轮 */
      if (attnActive()) return;
      const detail = (e as CustomEvent<LoongTeaserDetail>).detail;
      const txt =
        detail?.text ??
        (lang === "zh" ? "三颗星珠已归位…界龙剪影初现…" : "Three pearls lit… the Loong stirs…");
      setNewsText(txt);
      setNewsCta(lang === "zh" ? "打开星图 →" : "Open star map →");
      setQuestNews(true);
      setMode("idle_news");
      track("sprite_news_impression", { text: txt, section: "dragon_teaser" });
      setTimeout(() => {
        setMode((p) => (p === "idle_news" ? "idle_base" : p));
        setQuestNews(false);
        setNewsCta("");
      }, 5200);
    };
    window.addEventListener(LOONG_TEASER, onTeaser);
    return () => window.removeEventListener(LOONG_TEASER, onTeaser);
  }, [reduced, lang]);

  /* 召唤仪式期间暂停精灵游动，把帧预算让给 canvas */
  useEffect(() => {
    const onCer = (e: Event) => {
      const active = !!(e as CustomEvent<LoongCeremonyDetail>).detail?.active;
      ceremonyPauseRef.current = active;
      setCeremonyActive(active);
    };
    window.addEventListener(LOONG_CEREMONY, onCer);
    return () => window.removeEventListener(LOONG_CEREMONY, onCer);
  }, []);

  /* 互动坞：聊天开 → 本体淡出（面板有 AI 头像，不需要双形象）；
     任意浮层（聊天/龙珠托盘面板）活跃 → 全息播报让位（正在播的立刻收回） */
  useEffect(() => {
    const dockState = { chat: false, "dragon-panel": false };
    const onDock = (e: Event) => {
      const d = (e as CustomEvent<DockDetail>).detail;
      if (!d?.source) return;
      dockState[d.source] = d.active;
      setChatDock(dockState.chat);
      dockBusyRef.current = dockState.chat || dockState["dragon-panel"];
      if (dockBusyRef.current) {
        setMode((p) => (p === "idle_news" ? "idle_base" : p));
      }
    };
    window.addEventListener(DOCK_EVENT, onDock);
    return () => window.removeEventListener(DOCK_EVENT, onDock);
  }, []);

  useEffect(() => {
    const refreshHome = () => {
      homeRef.current = { left: window.innerWidth - HOME.right - BOT_W, top: window.innerHeight - HOME.bottom - BOT_H };
    };
    const refreshRects = () => {
      const els = document.querySelectorAll('[data-robot-avoid="true"]');
      const arr: Array<{ l: number; t: number; r: number; b: number }> = [];
      els.forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) arr.push({ l: r.left, t: r.top, r: r.right, b: r.bottom });
      });
      avoidRectsRef.current = arr;
    };
    refreshHome();
    refreshRects();
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        refreshRects();
      });
    };
    const onResize = () => {
      refreshHome();
      onScroll();
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onResize);
    const requery = setInterval(() => {
      if (!document.hidden) refreshRects();
    }, 1500);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onResize);
      clearInterval(requery);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  /* ---- 感知当前浏览版块，供场景化播报选池与种子问题 ---- */
  useEffect(() => {
    const ids = ["top", "autochat", "products", "pricing", "cases", "proof", "contact"];
    const els = ids.map((id) => document.getElementById(id)).filter((el): el is HTMLElement => !!el);
    if (!els.length) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) currentSectionRef.current = e.target.id;
      },
      { rootMargin: "-35% 0px -45% 0px" }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  /* ---- 行为调度：每 6s 随机小动作；隐藏页 / 高速滚动 / 悬停 / 调试锁定时静默 ---- */
  useEffect(() => {
    if (reduced) return;
    const loop = setInterval(() => {
      if (forcedModeRef.current || dockBusyRef.current) return;
      if (document.hidden || isHoveredRef.current) return;
      if (Math.abs(springScrollVelocity.get()) > 100 || Math.abs(flightVelX.get()) > 10) return;
      const rand = Math.random();
      let next: BotMode = "idle_base";
      if (rand > 0.95) next = "idle_spin";
      else if (rand > 0.9) next = "idle_dance";
      else if (rand > 0.8) next = "idle_wave";
      else if (rand > 0.65) {
        /* 全息播报不占号但避让：台上有气泡时本轮改做普通待机 */
        if (attnActive()) return;
        next = "idle_news";
        const pool = SECTION_NEWS[lang]?.[currentSectionRef.current] ?? NEWS[lang] ?? NEWS.en;
        const txt = pool[Math.floor(Math.random() * pool.length)];
        setNewsText(txt);
        track("sprite_news_impression", { text: txt, section: currentSectionRef.current });
      } else if (rand > 0.5) next = "idle_scan";
      if (next === "idle_wave") greetUntilRef.current = Date.now() + 3000;
      setMode(next);
      if (next !== "idle_base") {
        const dur = next === "idle_dance" ? 3600 : next === "idle_news" ? 5000 : next === "idle_spin" ? 1500 : 3000;
        setTimeout(() => setMode((p) => (p === next ? "idle_base" : p)), dur);
      }
    }, 6000);
    return () => clearInterval(loop);
  }, [reduced, lang, springScrollVelocity, flightVelX]);

  /* ---- 进场问好：入场动画落定后自动挥手一次（每会话一次；开场页存在时等它退场） ---- */
  useEffect(() => {
    if (reduced) return;
    try {
      if (sessionStorage.getItem(GREET_KEY)) return;
    } catch {}
    let fired = false;
    const timers: Array<ReturnType<typeof setTimeout>> = [];
    const greet = () => {
      if (fired) return;
      fired = true;
      timers.push(
        setTimeout(() => {
          if (isHoveredRef.current || forcedModeRef.current) return;
          greetUntilRef.current = Date.now() + 2800;
          setMode("idle_wave");
          track("sprite_greet");
          try {
            sessionStorage.setItem(GREET_KEY, "1");
          } catch {}
          timers.push(setTimeout(() => setMode((p) => (p === "idle_wave" ? "idle_base" : p)), 2800));
        }, 1600)
      );
    };
    let introShowing = false;
    try {
      introShowing = !sessionStorage.getItem("bl-intro-seen");
    } catch {}
    const onIntroEnter = () => greet();
    if (introShowing) {
      window.addEventListener("bl-intro-entered", onIntroEnter, { once: true });
      timers.push(setTimeout(greet, 15000)); // 兜底：事件丢失也保证问好
    } else {
      timers.push(setTimeout(greet, 1200)); // 等入场弹簧基本落定再问好
    }
    return () => {
      window.removeEventListener("bl-intro-entered", onIntroEnter);
      timers.forEach(clearTimeout);
    };
  }, [reduced]);

  /* ---- 点击页面空白处 → 飞过去；8s 无事自动飞回休息位（桌面端专属） ---- */
  useEffect(() => {
    if (reduced || !isDesktop) return;
    const handleClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest(FLY_IGNORE)) return;
      if (window.getSelection()?.toString()) return;
      const cx = homeRef.current.left + BOT_W / 2;
      const cy = homeRef.current.top + BOT_H / 2;
      /* 恶魔态：化作蝙蝠群飞过去（本体在蝠群飞行期间隐身、无声滑到终点，到达后重现） */
      if (batsEnabled()) {
        const from = spriteCenter();
        flyAsBats(from.x, from.y, e.clientX, e.clientY);
      }
      clickX.set(e.clientX - cx);
      clickY.set(e.clientY - cy);
      const now = Date.now();
      if (now - lastFlyTrackRef.current > 5000) {
        lastFlyTrackRef.current = now;
        track("sprite_fly", { skin: skinRef.current });
      }
      if (homeTimerRef.current) clearTimeout(homeTimerRef.current);
      homeTimerRef.current = setTimeout(() => {
        if (batsEnabled()) {
          const from = spriteCenter();
          flyAsBats(from.x, from.y, homeRef.current.left + BOT_W / 2, homeRef.current.top + BOT_H / 2);
        }
        clickX.set(0);
        clickY.set(0);
      }, 8000);
    };
    window.addEventListener("click", handleClick);
    return () => {
      window.removeEventListener("click", handleClick);
      if (homeTimerRef.current) clearTimeout(homeTimerRef.current);
    };
  }, [clickX, clickY, reduced, isDesktop]);

  /* ---- 眼神跟随：rAF 节流的指针追踪（纯数学推导机器人位置，无 DOM 读取，桌面端专属） ---- */
  useEffect(() => {
    if (reduced || !isDesktop) return;
    let raf = 0;
    const onMove = (e: PointerEvent) => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const cx = homeRef.current.left + clickX.get() + avoidX.get() + BOT_W / 2;
        const cy = homeRef.current.top + clickY.get() + smoothDragY.get() + avoidY.get() + BOT_H / 2 - 40;
        gazeX.set(Math.max(-2.4, Math.min(2.4, (e.clientX - cx) / 160)));
        gazeY.set(Math.max(-1.6, Math.min(1.6, (e.clientY - cy) / 200)));
      });
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [reduced, isDesktop, clickX, clickY, smoothDragY, avoidX, avoidY, gazeX, gazeY]);

  /**
   * 避让推挤：机器人包围盒 vs 避让区（含 padding），沿穿透较浅的轴推出。
   * 比原先的圆形近似更贴合矩形按钮，且不再逐帧 gBCR。
   */
  const resolveAvoid = (rx: number, ry: number) => {
    const PAD = 18;
    let ax = 0;
    let ay = 0;
    let hit = false;
    for (const rc of avoidRectsRef.current) {
      const l = rc.l - PAD;
      const t = rc.t - PAD;
      const r = rc.r + PAD;
      const b = rc.b + PAD;
      const ox = Math.min(rx + BOT_W, r) - Math.max(rx, l);
      const oy = Math.min(ry + BOT_H, b) - Math.max(ry, t);
      if (ox <= 0 || oy <= 0) continue;
      hit = true;
      if (ox < oy) ax += (rx + BOT_W / 2 < (l + r) / 2 ? -1 : 1) * (ox + 8);
      else ay += (ry + BOT_H / 2 < (t + b) / 2 ? -1 : 1) * (oy + 8);
    }
    return { hit, ax, ay };
  };

  /* ---- 逐帧状态机：滚动/飞行触发姿态切换 + 避让 + 着陆回弹（桌面端） ---- */
  useAnimationFrame(() => {
    const hovered = isHoveredRef.current;
    const forced = forcedModeRef.current;
    if (!reduced && isDesktop) {
      const v = springScrollVelocity.get();
      const speed = Math.hypot(flightVelX.get(), flightVelY.get());
      const rx = homeRef.current.left + clickX.get() + avoidX.get();
      const ry = homeRef.current.top + clickY.get() + smoothDragY.get() + avoidY.get();
      const col = resolveAvoid(rx, ry);
      avoidX.set(col.hit ? col.ax : 0);
      avoidY.set(col.hit ? col.ay : 0);
      if (!forced) {
        const SCROLL_TH = 500;
        const FLIGHT_TH = 50;
        if (v > SCROLL_TH) {
          if (mode !== "falling") setMode("falling");
          return;
        }
        if (v < -SCROLL_TH || speed > FLIGHT_TH) {
          if (mode !== "flying") setMode("flying");
          return;
        }
        if (mode === "falling" || mode === "flying") {
          setMode(hovered ? "idle_wave" : "idle_base");
          /* 着陆缓冲：一次挤压-回弹，速度归零的瞬间落地更有“重量感” */
          animate(squashY, [1, 0.9, 1.045, 1], { duration: 0.55, times: [0, 0.35, 0.7, 1], ease: "easeOut" });
          return;
        }
      }
    }
    if (forced) return;
    if (hovered) {
      if (mode !== "idle_wave") setMode("idle_wave");
    } else if (mode === "idle_wave" && Date.now() > greetUntilRef.current) {
      /* 悬停结束即收手（原实现会对着空气挥到下个调度周期） */
      setMode("idle_base");
    }
  });

  /** 打开 AI 客服（点击 / 键盘 / 全息面板均汇聚于此；后端与回答完全不受皮肤影响） */
  const openChatEvent = (from: string, seed?: string) => {
    window.dispatchEvent(new CustomEvent("bl:open-chat", { detail: { from, seed } }));
  };

  /** 当前机器人中心的视口坐标（供蝙蝠群起终点计算） */
  const spriteCenter = () => ({
    x: homeRef.current.left + clickX.get() + avoidX.get() + BOT_W / 2,
    y: homeRef.current.top + clickY.get() + smoothDragY.get() + avoidY.get() + BOT_H / 2,
  });

  /** 蝙蝠群是否可用：仅恶魔态 + 桌面 + 非降级 */
  const batsEnabled = () => skinRef.current === "demon" && isDesktop && !reduced && !lowFx;

  /** 触发一次蝙蝠群飞行：隐藏 DOM 本体，蝠群从 from 飞到 to，到达后本体在终点重现 */
  const flyAsBats = (fromX: number, fromY: number, toX: number, toY: number) => {
    setDissolved(true);
    batIdRef.current += 1;
    setBatFlight({ id: batIdRef.current, fromX, fromY, toX, toY });
  };

  /** 切换恶魔皮肤：纯外观，持久化到本会话；变身瞬间用“原地爆散→聚合”的蝙蝠群揭示。
   *  净化时回到常驻偏好皮肤（已解锁祥龙则回祥龙，而非硬回 normal）。 */
  const toggleDemon = () => {
    let pref: Skin = "normal";
    try {
      if (localStorage.getItem(SKIN_PREF_KEY) === "loong") pref = "loong";
    } catch {}
    const next: Skin = skinRef.current === "demon" ? pref : "demon";
    const canBats = isDesktop && !reduced && !lowFx;
    setSkin(next);
    if (!reduced) {
      setTransforming(true);
      setTimeout(() => setTransforming(false), 700);
    }
    if (next === "demon" && canBats) {
      const c = spriteCenter();
      flyAsBats(c.x, c.y, c.x, c.y); // 原地爆散再聚合成恶魔
    }
    try {
      if (next === "demon") sessionStorage.setItem(SKIN_KEY, "demon");
      else sessionStorage.removeItem(SKIN_KEY);
    } catch {}
    /* 图鉴解锁标记：见过恶魔形态就永久点亮图鉴卡（形态本身仍是会话级彩蛋） */
    if (next === "demon") {
      try {
        localStorage.setItem("bl-demon-seen", "1");
      } catch {}
    }
    track(next === "demon" ? "sprite_demon_unlock" : "sprite_demon_revert");
  };

  /**
   * 机器人点击：连击去抖。短窗口内累计到 7 次 → 切换彩蛋皮肤（不开客服）；
   * 否则去抖 300ms 后开一次客服（连击期间面板不弹出，避免盖住机器人导致连不满）。
   * 第 3 击起给蓄力预告（charge），让“即将变身”可被感知。
   */
  const handleRobotClick = () => {
    const c = comboRef.current;
    c.count += 1;
    if (c.timer) clearTimeout(c.timer);
    c.timer = setTimeout(() => {
      c.count = 0;
      setCharge(0);
    }, COMBO_WINDOW);
    if (c.openTimer) {
      clearTimeout(c.openTimer);
      c.openTimer = null;
    }
    if (c.count >= DEMON_CLICKS) {
      if (c.timer) clearTimeout(c.timer);
      c.count = 0;
      setCharge(0);
      toggleDemon();
      return;
    }
    if (c.count >= 3) setCharge((c.count - 2) / (DEMON_CLICKS - 2)); // 3→1/5 … 6→4/5
    c.openTimer = setTimeout(() => {
      c.openTimer = null;
      track("ai_sprite_click", { mode, skin: skinRef.current });
      openChatEvent(skinRef.current === "demon" ? "sprite_demon" : "sprite");
    }, OPEN_DEBOUNCE);
  };

  const handleNewsCta = () => {
    const section = currentSectionRef.current;
    if (questNews) {
      track("sprite_news_click", { section: "dragon_teaser", text: newsText });
      track("dragon_codex_open", { from: "hologram_teaser" });
      window.location.href = "/loong";
      return;
    }
    const seedPool = SECTION_SEED[lang] ?? SECTION_SEED.en;
    track("sprite_news_click", { section, text: newsText });
    openChatEvent(skinRef.current === "demon" ? "hologram_demon" : "hologram", seedPool[section] ?? seedPool.top);
  };

  const defaultNewsCta = lang === "zh" ? "点我 · 立即咨询 →" : "Tap me to chat →";
  const activeNewsCta = newsCta || defaultNewsCta;

  return (
    <>
    {/* 蝙蝠群覆盖层：仅在有飞行时激活；到达后清空并让本体重现 */}
    <BatSwarm flight={batFlight} count={46} onArrive={() => setDissolved(false)} />
    <motion.div
      className={`fixed bottom-40 right-3 md:right-6 [perspective:1000px] pointer-events-none ai-sprite-container transition-opacity duration-300 ${
        chatDock ? "!pointer-events-none opacity-0" : ""
      }`}
      style={{ zIndex: isHovered ? 100 : 50, x: combinedX, y: combinedY }}
      aria-hidden={chatDock}
    >
      {/* 响应式体型：移动端缩到 55%（轻量版），桌面端原尺寸；
          缩放放在独立节点上，避免与 framer 的 transform 写入互相覆盖 */}
      <div className="origin-bottom-right scale-[0.55] md:scale-100">
        <motion.div
          className={`cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-neon-cyan/60 rounded-[2.5rem] ${
            chatDock ? "pointer-events-none" : "pointer-events-auto"
          }`}
          role="button"
          tabIndex={chatDock ? -1 : 0}
          aria-label={lang === "zh" ? "打开 AI 客服对话" : "Open AI chat"}
          onMouseEnter={() => {
            setIsHovered(true);
            isHoveredRef.current = true;
            const now = Date.now();
            if (now - lastHoverTrackRef.current > 5000) {
              lastHoverTrackRef.current = now;
              track("sprite_hover");
            }
          }}
          onMouseLeave={() => {
            setIsHovered(false);
            isHoveredRef.current = false;
          }}
          onFocus={() => {
            setIsHovered(true);
            isHoveredRef.current = true;
          }}
          onBlur={() => {
            setIsHovered(false);
            isHoveredRef.current = false;
          }}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            handleRobotClick();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              handleRobotClick();
            }
          }}
          whileHover={{ scale: 1.1 }}
          whileTap={{ scale: 0.95 }}
          initial={reduced ? { opacity: 0 } : { y: 200, opacity: 0 }}
          animate={reduced ? { opacity: 1 } : { y: 0, opacity: 1 }}
          transition={{ type: "spring", stiffness: 50, damping: 20, delay: 0.6 }}
        >
          {/* 悬停提示气泡：给“可点击开客服”一个明确的转化引导（恶魔态换皮而不换转化目标） */}
          <AnimatePresence>
            {isHovered && (
              <motion.div
                className="pointer-events-none absolute right-full top-8 mr-1 whitespace-nowrap rounded-full border px-3 py-1.5 text-xs font-medium shadow-lg backdrop-blur"
                style={
                  skin === "demon"
                    ? { borderColor: "rgba(244,63,94,0.4)", background: "rgba(30,10,16,0.9)", color: "#fb7185" }
                    : { borderColor: "rgba(34,211,238,0.3)", background: "rgba(10,12,27,0.9)", color: "#22d3ee" }
                }
                initial={{ opacity: 0, x: 6, scale: 0.9 }}
                animate={{ opacity: 1, x: 0, scale: 1 }}
                exit={{ opacity: 0, x: 4, scale: 0.95 }}
                transition={{ duration: 0.18 }}
              >
                {skin === "demon"
                  ? lang === "zh"
                    ? "堕天使 · 找我唠唠"
                    : "Dark mode · let's chat"
                  : lang === "zh"
                    ? "点我 · AI 客服"
                    : "Chat with AI"}
              </motion.div>
            )}
          </AnimatePresence>

          {/* 蓄力预告 + 变身闪光：给隐藏彩蛋一个可被感知的“即将触发”与高潮反馈 */}
          <motion.div
            className="pointer-events-none absolute inset-0 -z-0"
            animate={
              reduced
                ? undefined
                : transforming
                  ? { rotate: [0, -6, 6, -4, 4, 0], scale: [1, 1.12, 1] }
                  : charge > 0
                    ? { rotate: [0, -charge * 3, charge * 3, 0] }
                    : { rotate: 0, scale: 1 }
            }
            transition={transforming ? { duration: 0.6 } : { duration: 0.28 }}
          />
          <AnimatePresence>
            {transforming && !reduced && (
              <motion.div
                className="pointer-events-none absolute left-1/2 top-1/2 -z-0 h-40 w-40 -translate-x-1/2 -translate-y-1/2 rounded-full"
                style={{ background: `radial-gradient(circle, ${skin === "demon" ? "rgba(244,63,94,0.55)" : "rgba(34,211,238,0.5)"} 0%, transparent 70%)` }}
                initial={{ scale: 0.2, opacity: 0.9 }}
                animate={{ scale: 2.2, opacity: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.7, ease: "easeOut" }}
              />
            )}
          </AnimatePresence>
          {/* 蓄力火花提示：第 3 击起冒红星，暗示“再点会有事发生” */}
          <AnimatePresence>
            {charge > 0 && skin === "normal" && (
              <motion.div
                className="pointer-events-none absolute left-1/2 top-2 -translate-x-1/2 text-sm"
                initial={{ opacity: 0, y: 4, scale: 0.6 }}
                animate={{ opacity: charge, y: -6 - charge * 8, scale: 0.7 + charge * 0.5 }}
                exit={{ opacity: 0 }}
              >
                <span style={{ filter: `drop-shadow(0 0 ${2 + charge * 6}px #f43f5e)` }}>✦</span>
              </motion.div>
            )}
          </AnimatePresence>

          {/* 蝙蝠群飞行期间“碎裂溶解”隐身（模糊+微胀，读作化成蝙蝠），到达后清晰重现 */}
          <motion.div
            style={{ willChange: "opacity, filter, transform" }}
            animate={dissolved ? { opacity: 0, scale: 1.14, filter: "blur(7px)" } : { opacity: 1, scale: 1, filter: "blur(0px)" }}
            transition={{ duration: dissolved ? 0.24 : 0.32, ease: dissolved ? "easeIn" : "easeOut" }}
          >
            {skin === "demon" ? (
              <DemonForm
                mode={mode}
                isHovered={isHovered}
                newsText={newsText}
                newsCta={activeNewsCta}
                scrollTilt={reduced || !isDesktop ? zeroMV : totalTilt}
                flightRotate={reduced || !isDesktop ? zeroMV : flightRotate}
                gazeX={gazeX}
                gazeY={gazeY}
                squashY={squashY}
                shadowOpacity={shadowOpacity}
                onNewsCta={handleNewsCta}
                reduced={reduced}
                lowFx={lowFx || ceremonyActive}
                revealed={!dissolved}
              />
            ) : skin === "loong" ? (
              <LoongForm
                mode={mode}
                isHovered={isHovered}
                newsText={newsText}
                newsCta={activeNewsCta}
                scrollTilt={reduced || !isDesktop ? zeroMV : totalTilt}
                flightRotate={reduced || !isDesktop ? zeroMV : flightRotate}
                gazeX={gazeX}
                gazeY={gazeY}
                squashY={squashY}
                shadowOpacity={shadowOpacity}
                onNewsCta={handleNewsCta}
                reduced={reduced}
                lowFx={lowFx || ceremonyActive}
              />
            ) : (
              <EveBot
                mode={mode}
                isHovered={isHovered}
                newsText={newsText}
                newsCta={activeNewsCta}
                scrollTilt={reduced || !isDesktop ? zeroMV : totalTilt}
                flightRotate={reduced || !isDesktop ? zeroMV : flightRotate}
                gazeX={gazeX}
                gazeY={gazeY}
                squashY={squashY}
                shadowOpacity={shadowOpacity}
                onNewsCta={handleNewsCta}
                reduced={reduced}
                lowFx={lowFx || ceremonyActive}
                skin={skin}
              />
            )}
          </motion.div>

          {/* 恶魔态一键净化：悬停时浮现，点击变回正常形态（不触发开客服/连击） */}
          <AnimatePresence>
            {skin === "demon" && isHovered && (
              <motion.button
                type="button"
                aria-label={lang === "zh" ? "恢复正常形态" : "Restore normal form"}
                className="pointer-events-auto absolute -top-1 right-0 z-10 grid h-6 w-6 place-items-center rounded-full border border-rose-400/40 bg-ink-900/90 text-xs shadow-lg backdrop-blur"
                initial={{ opacity: 0, scale: 0.6 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.6 }}
                onMouseEnter={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  if (comboRef.current.openTimer) {
                    clearTimeout(comboRef.current.openTimer);
                    comboRef.current.openTimer = null;
                  }
                  comboRef.current.count = 0;
                  toggleDemon();
                }}
              >
                <span aria-hidden>😇</span>
              </motion.button>
            )}
          </AnimatePresence>
        </motion.div>
      </div>
    </motion.div>
    </>
  );
}
