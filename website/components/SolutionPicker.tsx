"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowRight,
  Bot,
  Building2,
  Languages,
  Mic,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  User,
  Users,
  type LucideIcon,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";

type GoalId = "translate" | "autochat" | "voice" | "private";
type SizeId = "solo" | "team" | "org";

const GOAL_ICONS: Record<GoalId, LucideIcon> = {
  translate: Languages,
  autochat: Bot,
  voice: Mic,
  private: ShieldCheck,
};

const SIZE_ICONS: Record<SizeId, LucideIcon> = {
  solo: User,
  team: Users,
  org: Building2,
};

interface GoalOption {
  id: GoalId;
  label: string;
  desc: string;
}

interface SizeOption {
  id: SizeId;
  label: string;
}

interface ResultCopy {
  name: string;
  reason: string;
  primary: { label: string; href: string };
  secondary: { label: string; href: string };
}

interface PickerCopy {
  badge: string;
  title: string;
  subtitle: string;
  stepQ1: string;
  stepQ2: string;
  q1: string;
  q2: string;
  back: string;
  goals: GoalOption[];
  sizes: SizeOption[];
  resultBadge: string;
  tierLabel: string;
  restart: string;
  tiers: Record<SizeId, string>;
  results: Record<GoalId, ResultCopy>;
}

const COPY: Record<"zh" | "en", PickerCopy> = {
  zh: {
    badge: "30 秒选型",
    title: "30 秒找到你的方案",
    subtitle: "回答两个问题，直接给你推荐产品和套餐。",
    stepQ1: "第 1 / 2 题",
    stepQ2: "第 2 / 2 题",
    q1: "你现在最想解决什么？",
    q2: "团队规模？",
    back: "返回上一题",
    goals: [
      { id: "translate", label: "跨语言聊单难", desc: "客户语言不通，沟通被翻译卡住" },
      { id: "autochat", label: "消息多回不过来", desc: "想让 AI 自动跟单、促成成交" },
      { id: "voice", label: "需要声音克隆 / 多语配音", desc: "定制音色，多语种合成即用" },
      { id: "private", label: "数据敏感，必须私有部署", desc: "模型与数据留在自己服务器" },
    ],
    sizes: [
      { id: "solo", label: "个人 / 小团队" },
      { id: "team", label: "3-10 人" },
      { id: "org", label: "10 人以上" },
    ],
    resultBadge: "推荐方案",
    tierLabel: "推荐档位：",
    restart: "重新选择",
    tiers: {
      solo: "入门 / 体验档",
      team: "团队档",
      org: "旗舰 / 企业档",
    },
    results: {
      translate: {
        name: "通译 LingoX",
        reason: "多平台双向拟人翻译 + 统一收件箱，不会外语也能全球接单。",
        primary: { label: "查看通译 LingoX", href: "/interpreting" },
        secondary: { label: "查看价格", href: "#pricing" },
      },
      autochat: {
        name: "智聊 ChatX",
        reason: "AI 按你的人设 7×24 答疑跟单，关键节点一键人工接管。",
        primary: { label: "看 AI 成交演示", href: "#autochat" },
        secondary: { label: "查看套餐", href: "#pricing" },
      },
      voice: {
        name: "幻声 VoiceX",
        reason: "几十秒样本克隆音色，多语种合成即用。",
        primary: { label: "了解幻声 VoiceX", href: "/voice" },
        secondary: { label: "查看价格", href: "#pricing" },
      },
      private: {
        name: "无界底座 · 私有化部署",
        reason: "大模型与全部数据留在你自己的服务器，可微调可审计。",
        primary: { label: "联系方案顾问", href: "#contact" },
        secondary: { label: "查看价格", href: "#pricing" },
      },
    },
  },
  en: {
    badge: "30-sec picker",
    title: "Find your fit in 30 seconds",
    subtitle: "Answer two questions and get a product and plan recommendation.",
    stepQ1: "Question 1 / 2",
    stepQ2: "Question 2 / 2",
    q1: "What do you want to solve first?",
    q2: "Team size?",
    back: "Back",
    goals: [
      { id: "translate", label: "Language barrier with customers", desc: "Selling across languages is slow and lossy" },
      { id: "autochat", label: "Too many messages to answer", desc: "Want AI to follow up and close deals" },
      { id: "voice", label: "Need voice cloning / multilingual dubbing", desc: "Custom voices, multilingual synthesis" },
      { id: "private", label: "Data-sensitive, must self-host", desc: "Models and data stay on your servers" },
    ],
    sizes: [
      { id: "solo", label: "Solo / small team" },
      { id: "team", label: "3-10 people" },
      { id: "org", label: "10+ people" },
    ],
    resultBadge: "Our pick for you",
    tierLabel: "Suggested tier: ",
    restart: "Start over",
    tiers: {
      solo: "Starter / trial",
      team: "Team",
      org: "Flagship / Enterprise",
    },
    results: {
      translate: {
        name: "LingoX · Chat Translation",
        reason: "Human-like two-way translation across platforms with a unified inbox — serve global customers without speaking their language.",
        primary: { label: "See LingoX", href: "/en/interpreting" },
        secondary: { label: "See pricing", href: "#pricing" },
      },
      autochat: {
        name: "ChatX · AI Closing",
        reason: "AI answers and follows up in your persona 24/7; take over with one click at key moments.",
        primary: { label: "See the AI closing demo", href: "#autochat" },
        secondary: { label: "See plans", href: "#pricing" },
      },
      voice: {
        name: "VoiceX · Voice Cloning",
        reason: "Clone a voice from seconds of audio; multilingual synthesis ready to use.",
        primary: { label: "Explore VoiceX", href: "/en/voice" },
        secondary: { label: "See pricing", href: "#pricing" },
      },
      private: {
        name: "BOUNDLESS Core · Private Deployment",
        reason: "LLMs and all data stay on your own servers — fine-tunable and auditable.",
        primary: { label: "Talk to a solutions advisor", href: "#contact" },
        secondary: { label: "See pricing", href: "#pricing" },
      },
    },
  },
};

const FADE = {
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -10 },
  transition: { duration: 0.25, ease: "easeOut" },
} as const;

/** 30 秒选型器：两步单选（痛点 → 团队规模）确定性映射到产品 + 套餐档位，纯前端无 API。 */
export default function SolutionPicker() {
  const { lang } = useLang();
  const c = COPY[lang];
  const [goal, setGoal] = useState<GoalId | null>(null);
  const [size, setSize] = useState<SizeId | null>(null);

  const step: "q1" | "q2" | "result" = goal === null ? "q1" : size === null ? "q2" : "result";

  const pickSize = (s: SizeId) => {
    setSize(s);
    if (goal) {
      try {
        track("picker_result", { goal, size: s });
      } catch {
        /* 埋点绝不阻塞交互 */
      }
    }
  };

  const restart = () => {
    setGoal(null);
    setSize(null);
  };

  return (
    <section id="picker" className="relative py-20">
      <div className="pointer-events-none absolute left-1/2 top-10 h-72 w-72 -translate-x-1/2 rounded-full bg-neon-violet/10 blur-[120px]" />
      <div className="relative mx-auto max-w-5xl px-5">
        <Reveal className="mb-10 text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs font-medium text-neon-cyan">
            <Sparkles className="h-3.5 w-3.5" />
            {c.badge}
          </span>
          <h2 className="mt-4 text-3xl font-bold text-white md:text-4xl">{c.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{c.subtitle}</p>
        </Reveal>

        <Reveal delay={0.1}>
          <div className="glass rounded-3xl border border-white/10 p-6 md:p-8">
            <AnimatePresence mode="wait" initial={false}>
              {step === "q1" && (
                <motion.div key="q1" {...FADE}>
                  <div className="mb-5 flex items-center justify-between">
                    <h3 className="text-lg font-semibold text-white md:text-xl">{c.q1}</h3>
                    <span className="shrink-0 text-xs text-slate-500">{c.stepQ1}</span>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    {c.goals.map((g) => {
                      const Icon = GOAL_ICONS[g.id];
                      return (
                        <button
                          key={g.id}
                          type="button"
                          onClick={() => setGoal(g.id)}
                          className="group flex items-start gap-4 rounded-2xl border border-white/10 bg-ink-900/60 p-5 text-left transition hover:border-neon-cyan/40 hover:bg-ink-900/80"
                        >
                          <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                            <Icon className="h-5 w-5" />
                          </span>
                          <span>
                            <span className="block font-medium text-white group-hover:text-neon-cyan">{g.label}</span>
                            <span className="mt-1 block text-sm text-slate-400">{g.desc}</span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </motion.div>
              )}

              {step === "q2" && goal && (
                <motion.div key="q2" {...FADE}>
                  <div className="mb-5 flex items-center justify-between">
                    <h3 className="text-lg font-semibold text-white md:text-xl">{c.q2}</h3>
                    <span className="shrink-0 text-xs text-slate-500">{c.stepQ2}</span>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-3">
                    {c.sizes.map((s) => {
                      const Icon = SIZE_ICONS[s.id];
                      return (
                        <button
                          key={s.id}
                          type="button"
                          onClick={() => pickSize(s.id)}
                          className="group flex flex-col items-center gap-3 rounded-2xl border border-white/10 bg-ink-900/60 p-6 transition hover:border-neon-cyan/40 hover:bg-ink-900/80"
                        >
                          <span className="grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                            <Icon className="h-5 w-5" />
                          </span>
                          <span className="font-medium text-white group-hover:text-neon-cyan">{s.label}</span>
                        </button>
                      );
                    })}
                  </div>
                  <button
                    type="button"
                    onClick={() => setGoal(null)}
                    className="mt-5 inline-flex items-center gap-1.5 text-xs text-slate-500 transition hover:text-slate-300"
                  >
                    <RotateCcw className="h-3.5 w-3.5" />
                    {c.back}
                  </button>
                </motion.div>
              )}

              {step === "result" && goal && size && (
                <motion.div key="result" {...FADE}>
                  {(() => {
                    const r = c.results[goal];
                    const Icon = GOAL_ICONS[goal];
                    return (
                      <div className="flex flex-col gap-6">
                        <div className="flex items-start gap-4">
                          <span className="grid h-14 w-14 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-neon-cyan/25 to-neon-violet/25 text-neon-cyan">
                            <Icon className="h-7 w-7" />
                          </span>
                          <div>
                            <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-violet/30 bg-neon-violet/10 px-2.5 py-0.5 text-[11px] font-medium text-neon-violet">
                              <Sparkles className="h-3 w-3" />
                              {c.resultBadge}
                            </span>
                            <h3 className="mt-2 text-2xl font-bold text-white">{r.name}</h3>
                            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-400">{r.reason}</p>
                            <span className="mt-3 inline-block rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
                              {c.tierLabel}
                              {c.tiers[size]}
                            </span>
                          </div>
                        </div>
                        <div className="flex flex-wrap items-center gap-3">
                          <a
                            href={r.primary.href}
                            className="cta-fx group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
                          >
                            {r.primary.label}
                            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                          </a>
                          <a
                            href={r.secondary.href}
                            className="inline-flex items-center rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
                          >
                            {r.secondary.label}
                          </a>
                          <button
                            type="button"
                            onClick={restart}
                            className="inline-flex items-center gap-1.5 text-xs text-slate-500 transition hover:text-slate-300"
                          >
                            <RotateCcw className="h-3.5 w-3.5" />
                            {c.restart}
                          </button>
                        </div>
                      </div>
                    );
                  })()}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
