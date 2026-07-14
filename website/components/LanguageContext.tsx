"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { content, Dict, Lang } from "@/lib/content";
import { getLocal, setLocal } from "@/lib/safe-storage";

interface LanguageContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  toggle: () => void;
  t: Dict;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

/** The `/en` route forces English so the SSR'd HTML is independently indexable.
 *  小语种落地页（/ko /ja）正文自带对应语言内容，全局 Chrome（悬浮球/Cookie/页脚）固定用英文字典。 */
const EXTRA_LOCALE_PREFIXES = ["/ko", "/ja"];

function extraLocaleOf(pathname: string | null): string | null {
  if (!pathname) return null;
  for (const p of EXTRA_LOCALE_PREFIXES) {
    if (pathname === p || pathname.startsWith(`${p}/`)) return p.slice(1);
  }
  return null;
}

function routeLangOf(pathname: string | null): Lang | null {
  if (!pathname) return null;
  if (pathname === "/en" || pathname.startsWith("/en/")) return "en";
  if (extraLocaleOf(pathname)) return "en";
  return null;
}

/** 拥有 zh/en 双路由的营销页根路径（"" = 首页）。落地页/条款页切语言时走 URL 前缀互换，
 *  保证分享链接与 SEO 语言一致；其余路由（/admin /app 等）仅切换字典。 */
const DUAL_LOCALE_BASES = new Set(["", "/voice", "/face", "/interpreting", "/privacy", "/terms", "/order", "/download", "/videos"]);

function dualLocaleBase(pathname: string | null): string | null {
  if (!pathname) return null;
  const base = pathname === "/en" ? "" : pathname.startsWith("/en/") ? pathname.slice(3) : pathname === "/" ? "" : pathname;
  return DUAL_LOCALE_BASES.has(base) ? base : null;
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const routeLang = routeLangOf(pathname);
  // SSR + first client render both honor the route locale -> no hydration mismatch on /en.
  const [lang, setLangState] = useState<Lang>(routeLang ?? "zh");

  useEffect(() => {
    if (routeLang) {
      setLangState(routeLang);
      if (typeof document !== "undefined") {
        // 小语种页面 html lang 保持对应语言（正文语言），其余按字典语言。
        const extra = extraLocaleOf(pathname);
        document.documentElement.lang = extra ?? (routeLang === "zh" ? "zh-CN" : "en");
      }
      return;
    }
    // Non-/en routes: honor saved preference (hl-lang, legacy yt-lang) then browser language.
    const saved = (getLocal("hl-lang") ?? getLocal("yt-lang")) as Lang | null;
    if (saved === "zh" || saved === "en") {
      setLangState(saved);
    } else if (typeof navigator !== "undefined" && navigator.language.startsWith("en")) {
      setLangState("en");
    }
  }, [routeLang]);

  const setLang = (next: Lang) => {
    setLangState(next);
    setLocal("hl-lang", next);
    if (typeof document !== "undefined") document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
  };

  const toggle = () => {
    const next: Lang = lang === "zh" ? "en" : "zh";
    // On dual-locale marketing routes, reflect locale in the URL (shareable + crawlable).
    const base = dualLocaleBase(pathname);
    if (base !== null) {
      setLocal("hl-lang", next);
      router.push(next === "en" ? `/en${base}` || "/en" : base || "/");
      return;
    }
    setLang(next);
  };

  return (
    <LanguageContext.Provider value={{ lang, setLang, toggle, t: content[lang] }}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLang() {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLang must be used within LanguageProvider");
  return ctx;
}
