"use client";

import { useEffect, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { Menu, X } from "lucide-react";
import { useLang } from "./LanguageContext";
import { BRAND, CATEGORIES, CATEGORY_ORDER, productsInCategory, type ProductKey } from "@/lib/brand";
import { CATEGORY_UI } from "@/lib/categoryUi";
import { PRODUCT_IMG, PRODUCT_LANDING, PRODUCT_ANCHOR } from "./productMeta";
import { localePath } from "@/lib/site";
import { track } from "@/lib/track";
import type { LandingKey } from "@/lib/landingContent";

/** 落地页顶栏焦点：官方 LandingKey + 智连系 /growth 页。 */
export type LandingNavFocus = LandingKey | "growth";

const LANDING_PRODUCT: Record<LandingNavFocus, ProductKey[]> = {
  voice: ["voicex"],
  face: ["facex", "livex"],
  interpreting: ["lingox", "voxx"],
  growth: ["reachx", "chatx"],
};

function useProductHref(lang: "zh" | "en") {
  return (key: ProductKey) => {
    const landing = PRODUCT_LANDING[key];
    if (landing) return localePath(lang, landing);
    return localePath(lang, "/") + PRODUCT_ANCHOR[key];
  };
}

function FamilyChips({
  product,
  onNavigate,
}: {
  product: LandingNavFocus;
  onNavigate?: () => void;
}) {
  const { lang } = useLang();
  const activeKeys = new Set(LANDING_PRODUCT[product] ?? []);
  const hrefOf = useProductHref(lang);

  return (
    <>
      {CATEGORY_ORDER.map((cat) => {
        const ui = CATEGORY_UI[cat];
        const cc = CATEGORIES[cat];
        const items = productsInCategory(cat);
        const catActive = items.some((k) => activeKeys.has(k));
        return (
          <div key={cat} className="flex flex-wrap items-center gap-1 pr-3">
            <span
              className={`shrink-0 text-[10px] font-semibold uppercase tracking-wider ${
                catActive ? ui.label : "text-slate-600"
              }`}
            >
              {lang === "zh" ? cc.zh : cc.en}
            </span>
            {items.map((key) => {
              const p = BRAND.products[key];
              const on = activeKeys.has(key);
              return (
                <Link
                  key={key}
                  href={hrefOf(key)}
                  onClick={() => {
                    track("product_click", { key, where: "landing_nav" });
                    onNavigate?.();
                  }}
                  className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px] transition ${
                    on
                      ? `${ui.chip} border-current`
                      : "border-transparent text-slate-400 hover:border-white/10 hover:bg-white/5 hover:text-white"
                  }`}
                  aria-current={on ? "page" : undefined}
                >
                  <Image
                    src={PRODUCT_IMG[key]}
                    alt=""
                    width={16}
                    height={16}
                    className="h-4 w-4 object-contain"
                    draggable={false}
                  />
                  {lang === "zh" ? p.zh : p.en}
                </Link>
              );
            })}
          </div>
        );
      })}
    </>
  );
}

export default function LandingFamilyNav({ product }: { product: LandingNavFocus }) {
  const { lang } = useLang();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <>
      {/* 桌面：横滑芯片条 */}
      <div className="mx-auto hidden max-w-6xl items-center gap-1 overflow-x-auto px-5 pb-2 pt-1 md:flex">
        <FamilyChips product={product} />
      </div>

      {/* 移动：产品抽屉入口 */}
      <div className="flex items-center justify-between gap-2 px-5 pb-2 pt-0.5 md:hidden">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/[0.03] px-3 py-1.5 text-xs text-slate-300"
          aria-expanded={open}
        >
          <Menu className="h-3.5 w-3.5" />
          {lang === "zh" ? "全部产品" : "All products"}
        </button>
        <span className="truncate text-[11px] text-slate-500">
          {lang === "zh" ? "当前页可跳转其他产品线" : "Jump to other product lines"}
        </span>
      </div>

      {open && (
        <div className="fixed inset-0 z-[60] md:hidden" role="dialog" aria-modal="true">
          <button
            type="button"
            className="absolute inset-0 bg-ink-950/70 backdrop-blur-sm"
            aria-label="close"
            onClick={() => setOpen(false)}
          />
          <div className="absolute inset-x-0 bottom-0 max-h-[75vh] overflow-y-auto rounded-t-3xl border border-white/10 bg-ink-900 p-5 shadow-2xl">
            <div className="mb-4 flex items-center justify-between">
              <p className="text-sm font-semibold text-white">
                {lang === "zh" ? "三系七产品" : "Three families · seven lines"}
              </p>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-full border border-white/10 p-2 text-slate-300"
                aria-label="close"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex flex-col gap-4">
              <FamilyChips product={product} onNavigate={() => setOpen(false)} />
            </div>
          </div>
        </div>
      )}
    </>
  );
}
