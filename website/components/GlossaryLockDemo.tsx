"use client";

import { useEffect, useState } from "react";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";
import { Lock, Unlock, Plus, Trash2 } from "lucide-react";

/** 通译术语锁定小演示：展示「有术语表 vs 无术语表」的翻译差，不调 API，纯前端对照。 */
const SEED = {
  zh: [
    { src: "BOUNDLESS Engine", locked: "BOUNDLESS Engine", unlocked: "无边界引擎" },
    { src: "美团", locked: "Meituan", unlocked: "Beautiful Group" },
    { src: "智聊 ChatX", locked: "ChatX", unlocked: "Smart Chat" },
  ],
  en: [
    { src: "BOUNDLESS Engine", locked: "BOUNDLESS Engine", unlocked: "Unbounded Engine" },
    { src: "Meituan", locked: "美团", unlocked: "Beautiful Group" },
    { src: "ChatX", locked: "智聊 ChatX", unlocked: "Smart Chat X" },
  ],
} as const;

const COPY = {
  zh: {
    title: "术语锁定 · 当场对比",
    sub: "专有名词进术语表后，翻译不再「自作聪明」。试着加一条你的品牌词。",
    locked: "有术语表",
    unlocked: "无术语表（易翻车）",
    src: "原文",
    addPh: "输入专有名词，如：无界科技",
    addBtn: "锁定",
    hint: "演示为对照示意；正式环境术语表可导入 CSV / API。",
  },
  en: {
    title: "Glossary lock · side-by-side",
    sub: "Once a proper noun is locked, translation stops inventing. Add one of your brand terms.",
    locked: "With glossary",
    unlocked: "Without (breaks)",
    src: "Source",
    addPh: "Add a term, e.g. BOUNDLESS",
    addBtn: "Lock",
    hint: "Illustrative demo; production glossaries import via CSV / API.",
  },
} as const;

type Row = { src: string; locked: string; unlocked: string };

export default function GlossaryLockDemo() {
  const { lang } = useLang();
  const c = COPY[lang];
  const [rows, setRows] = useState<Row[]>(() => [...SEED[lang]]);
  const [draft, setDraft] = useState("");

  // 切语言时重置种子（避免中英混表）
  useEffect(() => {
    setRows([...SEED[lang]]);
  }, [lang]);

  const add = () => {
    const src = draft.trim();
    if (!src || rows.some((r) => r.src === src)) return;
    // 锁定侧保持原文；未锁定侧用粗糙「翻译」模拟翻车
    const unlocked =
      lang === "zh"
        ? src.replace(/BOUNDLESS|无界/gi, "无边界").replace(/X$/i, "艾克斯") || `${src}（乱译）`
        : src.replace(/BOUNDLESS|无界/gi, "Unbounded").replace(/X$/i, "-Ex") || `Wrong-${src}`;
    setRows((prev) => [...prev, { src, locked: src, unlocked }]);
    setDraft("");
    track("glossary_demo_add", { len: src.length });
  };

  const remove = (src: string) => {
    setRows((prev) => prev.filter((r) => r.src !== src));
    track("glossary_demo_remove", { len: src.length });
  };

  return (
    <div className="rounded-3xl border border-white/10 bg-ink-900/50 p-5">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-white">{c.title}</h3>
          <p className="mt-1 text-xs leading-relaxed text-slate-400">{c.sub}</p>
        </div>
        <span className="shrink-0 rounded-full border border-amber-400/25 bg-amber-400/10 px-2.5 py-0.5 text-[10px] font-medium text-amber-300">
          <Lock className="mr-1 inline h-3 w-3" />
          LingoX
        </span>
      </div>

      <div className="overflow-hidden rounded-2xl border border-white/10">
        <div className="grid grid-cols-[1.1fr_1fr_1fr] gap-px bg-white/10 text-[10px] font-medium uppercase tracking-wide text-slate-400">
          <div className="bg-ink-950/80 px-3 py-2">{c.src}</div>
          <div className="flex items-center gap-1 bg-emerald-500/10 px-3 py-2 text-emerald-300">
            <Lock className="h-3 w-3" />
            {c.locked}
          </div>
          <div className="flex items-center gap-1 bg-rose-500/10 px-3 py-2 text-rose-300">
            <Unlock className="h-3 w-3" />
            {c.unlocked}
          </div>
        </div>
        {rows.map((r) => (
          <div key={r.src} className="grid grid-cols-[1.1fr_1fr_1fr] gap-px bg-white/5 text-sm">
            <div className="flex items-center justify-between gap-2 bg-ink-950/60 px-3 py-2.5 text-slate-200">
              <span className="truncate font-medium">{r.src}</span>
              <button
                type="button"
                onClick={() => remove(r.src)}
                className="shrink-0 rounded p-1 text-slate-500 transition hover:bg-white/5 hover:text-rose-300"
                aria-label="remove"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="bg-emerald-500/[0.06] px-3 py-2.5 font-medium text-emerald-200">{r.locked}</div>
            <div className="bg-rose-500/[0.06] px-3 py-2.5 text-rose-200/90 line-through decoration-rose-400/40">
              {r.unlocked}
            </div>
          </div>
        ))}
      </div>

      <form
        className="mt-3 flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={c.addPh}
          maxLength={40}
          className="flex-1 rounded-full border border-white/10 bg-ink-950/60 px-4 py-2.5 text-sm text-white placeholder:text-slate-500 outline-none focus:border-amber-400/40"
        />
        <button
          type="submit"
          disabled={!draft.trim()}
          className="inline-flex items-center justify-center gap-1.5 rounded-full border border-amber-400/30 bg-amber-400/10 px-5 py-2.5 text-sm font-medium text-amber-200 transition hover:bg-amber-400/20 disabled:opacity-40"
        >
          <Plus className="h-4 w-4" />
          {c.addBtn}
        </button>
      </form>
      <p className="mt-3 text-[11px] text-slate-500">{c.hint}</p>
    </div>
  );
}
