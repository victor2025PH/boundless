"use client";

import { useMemo, useRef, useState } from "react";
import { useLang } from "./LanguageContext";
import { GLOSSARY } from "@/lib/manualContent";
import { track } from "@/lib/track";

/**
 * 教程/手册富文本渲染器（数据侧标记见 lib/manualContent.ts 头注释）：
 * - `**文本**` → 品牌青色加粗强调（按钮名、菜单路径、关键数值）
 * - `[[术语]]` → 虚线下划线术语，悬停（桌面）或点击（移动端）弹出解释气泡
 * 未命中术语表的 [[..]] 自动降级为纯文本，数据写错不至于崩页面。
 */
export default function RichText({ text }: { text: string }) {
  const { lang } = useLang();

  const defs = useMemo(() => {
    const m = new Map<string, string>();
    for (const g of GLOSSARY) m.set(g.term[lang], g.def[lang]);
    return m;
  }, [lang]);

  const parts = useMemo(() => text.split(/(\*\*.+?\*\*|\[\[.+?\]\])/g).filter(Boolean), [text]);

  return (
    <>
      {parts.map((p, i) => {
        if (p.startsWith("**") && p.endsWith("**")) {
          return (
            <strong key={i} className="font-semibold text-neon-cyan">
              {p.slice(2, -2)}
            </strong>
          );
        }
        if (p.startsWith("[[") && p.endsWith("]]")) {
          const term = p.slice(2, -2);
          const def = defs.get(term);
          if (!def) return <span key={i}>{term}</span>;
          return <Term key={i} term={term} def={def} />;
        }
        return <span key={i}>{p}</span>;
      })}
    </>
  );
}

/** 术语 + 解释气泡：桌面悬停、移动端点击，失焦自动关闭；每次会话每术语只上报一次曝光 */
function Term({ term, def }: { term: string; def: string }) {
  const [open, setOpen] = useState(false);
  const trackedRef = useRef(false);

  function show() {
    setOpen(true);
    if (!trackedRef.current) {
      trackedRef.current = true;
      track("term_view", { term });
    }
  }

  return (
    <span className="relative inline-block">
      <button
        type="button"
        aria-expanded={open}
        onMouseEnter={show}
        onMouseLeave={() => setOpen(false)}
        onClick={() => (open ? setOpen(false) : show())}
        onBlur={() => setOpen(false)}
        className="cursor-help rounded-sm font-medium text-slate-200 underline decoration-neon-violet/60 decoration-dotted underline-offset-4 transition hover:text-neon-violet"
      >
        {term}
      </button>
      {open && (
        <span
          role="tooltip"
          className="no-print absolute bottom-full left-1/2 z-30 mb-2 block w-[min(280px,72vw)] -translate-x-1/2 rounded-xl border border-neon-violet/30 bg-ink-900/95 p-3 text-left text-xs font-normal leading-relaxed text-slate-300 shadow-2xl backdrop-blur"
        >
          <span className="mb-1 block font-semibold text-neon-violet">{term}</span>
          {def}
        </span>
      )}
    </span>
  );
}
