"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { Menu, X, Languages, ChevronDown } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CONTACT_URL, localePath } from "@/lib/site";
import { track } from "@/lib/track";
import BrandMark from "./BrandMark";
import ModeToggle from "./ModeToggle";
import { BRAND, CATEGORIES, CATEGORY_ORDER, productsInCategory, type ProductKey } from "@/lib/brand";
import { CATEGORY_UI } from "@/lib/categoryUi";
import { PRODUCT_LANDING, PRODUCT_ANCHOR, PRODUCT_IMG } from "./productMeta";
import { abVariant, abExpose, NAV_BUY, type AbVariant } from "@/lib/ab";

export default function Navbar() {
  const { t, lang, toggle } = useLang();
  const { isMiniApp } = useTelegram();
  const pathname = usePathname();
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState("");
  const [buyVariant, setBuyVariant] = useState<AbVariant>("a");
  // 产品下拉：hover（鼠标）+ click（触屏/键盘）双模式。纯 :hover 在触屏上打不开，
  // 鼠标用户点击也无反馈——两类用户都会感知为「点击没有响应」。
  const [prodOpen, setProdOpen] = useState(false);
  const prodRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const v = abVariant("nav_buy");
    setBuyVariant(v);
    abExpose("nav_buy", v);
  }, []);

  // 点击面板外 / Esc 关闭
  useEffect(() => {
    if (!prodOpen) return;
    const onDown = (e: PointerEvent) => {
      if (!prodRef.current?.contains(e.target as Node)) setProdOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setProdOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [prodOpen]);

  // 路由变化（跳到产品落地页）后收起
  useEffect(() => {
    setProdOpen(false);
  }, [pathname]);

  // 锚点仅在首页有效；子页面（/order /download 等）跳回对应语言首页的锚点。
  const home = lang === "zh" ? "/" : "/en";
  const onHome = pathname === "/" || pathname === "/en";
  const anchor = (hash: string) => (onHome ? hash : `${home}${hash}`);

  // 产品跳转：有独立落地页跳落地页（按语言前缀），否则回退首页锚点。
  const productHref = (key: ProductKey) => {
    const landing = PRODUCT_LANDING[key];
    if (landing) return localePath(lang, landing);
    return anchor(PRODUCT_ANCHOR[key]);
  };

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 20);
    onScroll();
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const ids = ["translate", "autochat", "realtime", "showcase", "engage", "pricing", "contact"];
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) setActive(e.target.id);
        });
      },
      { rootMargin: "-45% 0px -50% 0px" }
    );
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, []);

  const buy = NAV_BUY[buyVariant];
  const buyHref = lang === "zh" ? buy.zhPath : buy.enPath;
  const buyLabel = lang === "zh" ? buy.zhLabel : buy.enLabel;

  const links = [
    { href: anchor("#translate"), label: t.nav.translate, id: "translate" },
    { href: anchor("#autochat"), label: t.nav.autochat, id: "autochat" },
    { href: anchor("#realtime"), label: t.nav.demo, id: "realtime" },
    { href: anchor("#engage"), label: t.nav.engage, id: "engage" },
    {
      href: buyHref,
      label: buyLabel,
      id: "buy",
      onClick: () => track("cta_click", { where: "nav_buy", ab: buyVariant }),
    },
    { href: lang === "zh" ? "/download" : "/en/download", label: lang === "zh" ? "下载" : "Download", id: "download" },
    { href: anchor("#contact"), label: t.nav.contact, id: "contact" },
  ];

  return (
    <header
      className={`fixed inset-x-0 top-0 z-50 transition-all ${
        scrolled ? "glass" : "bg-transparent"
      }`}
    >
      <nav className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4">
        <a href={onHome ? "#top" : home} className="flex items-center gap-2">
          <BrandMark className="h-9 w-9" />
          <span className="text-lg font-semibold tracking-wide text-white">
            {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
          </span>
        </a>

        <div className="hidden items-center gap-8 md:flex">
          {/* 产品 · 三系下拉（智连 / 幻境 / 通达）：hover 或 click 均可展开 */}
          <div ref={prodRef} className="group relative">
            <button
              onClick={() => setProdOpen((v) => !v)}
              aria-expanded={prodOpen}
              aria-haspopup="menu"
              className="inline-flex items-center gap-1 text-sm text-slate-300 transition-colors hover:text-white"
            >
              {lang === "zh" ? "产品" : "Products"}
              <ChevronDown
                className={`h-3.5 w-3.5 opacity-70 transition-transform group-hover:rotate-180 ${prodOpen ? "rotate-180" : ""}`}
              />
            </button>
            <div
              className={`absolute left-1/2 top-full z-50 -translate-x-1/2 pt-3 transition duration-150 group-hover:visible group-hover:opacity-100 ${
                prodOpen ? "visible opacity-100" : "invisible opacity-0"
              }`}
            >
              <div className="glass grid w-[640px] grid-cols-3 gap-3 rounded-2xl border border-white/10 p-4">
                {CATEGORY_ORDER.map((cat) => {
                  const cc = CATEGORIES[cat];
                  const ui = CATEGORY_UI[cat];
                  return (
                    <div key={cat}>
                      <div className={`mb-2 border-b border-white/5 pb-1.5 text-xs font-semibold ${ui.label}`}>
                        {lang === "zh" ? cc.zh : cc.en}
                        <span className="ml-1 font-normal text-slate-500">{lang === "zh" ? cc.en : cc.zh}</span>
                      </div>
                      <div className="flex flex-col gap-0.5">
                        {productsInCategory(cat).map((key) => {
                          const p = BRAND.products[key];
                          return (
                            <a
                              key={key}
                              href={productHref(key)}
                              onClick={() => {
                                setProdOpen(false);
                                track("product_click", { key, where: "nav" });
                              }}
                              className="group/item flex items-center gap-2.5 rounded-lg px-2 py-1.5 transition hover:bg-white/5"
                            >
                              <Image
                                src={PRODUCT_IMG[key]}
                                alt=""
                                width={28}
                                height={28}
                                className="h-7 w-7 shrink-0 object-contain opacity-90 transition group-hover/item:opacity-100"
                                draggable={false}
                              />
                              <span className="min-w-0">
                                <span className="block text-sm text-slate-200 group-hover/item:text-white">
                                  {p.zh}
                                  <span className="ml-1.5 text-xs text-slate-500">{p.en}</span>
                                </span>
                                <span className="block truncate text-[11px] text-slate-500">{p.scene[lang]}</span>
                              </span>
                            </a>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {links.map((l) => {
            const isOn = l.href.includes("#")
              ? active === l.href.split("#")[1]
              : pathname === l.href || pathname === l.href.split("#")[0];
            return (
              <a
                key={l.id}
                href={l.href}
                onClick={"onClick" in l ? l.onClick : undefined}
                className={`relative text-sm transition-colors hover:text-white ${
                  isOn ? "text-white" : "text-slate-300"
                }`}
              >
                {l.label}
                {isOn && (
                  <span className="absolute -bottom-1.5 left-0 h-0.5 w-full rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet" />
                )}
              </a>
            );
          })}
          <a href={localePath(lang, "/brand")} className="relative text-sm text-slate-300 transition-colors hover:text-white">
            {lang === "zh" ? "品牌" : "Brand"}
          </a>
        </div>

        <div className="flex items-center gap-3">
          <ModeToggle />
          <button
            onClick={toggle}
            className="flex items-center gap-1.5 rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-300 transition hover:border-neon-cyan/50 hover:text-white"
            aria-label="switch language"
          >
            <Languages className="h-4 w-4" />
            {lang === "zh" ? "EN" : "中文"}
          </button>
          {isMiniApp ? (
            <a
              href="#contact"
              onClick={() => track("cta_click", { where: "nav_miniapp" })}
              className="hidden rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-sm font-medium text-ink-950 transition hover:opacity-90 md:inline-block"
            >
              {t.nav.cta}
            </a>
          ) : (
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "nav" })}
              className="hidden rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-sm font-medium text-ink-950 transition hover:opacity-90 md:inline-block"
            >
              {t.nav.cta}
            </a>
          )}
          <button
            className="text-slate-200 md:hidden"
            onClick={() => setOpen((v) => !v)}
            aria-label="menu"
          >
            {open ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
          </button>
        </div>
      </nav>

      {open && (
        <div className="glass border-t border-white/5 md:hidden">
          <div className="flex flex-col gap-1 px-5 py-3">
            {/* 产品 · 按三系分组 */}
            <div className="mb-1 rounded-lg bg-white/[0.02] p-2">
              {CATEGORY_ORDER.map((cat) => (
                <div key={cat} className="mb-2 last:mb-0">
                  <div className={`px-1 py-1 text-xs font-semibold ${CATEGORY_UI[cat].label}`}>
                    {lang === "zh" ? CATEGORIES[cat].zh : CATEGORIES[cat].en}
                  </div>
                  <div className="flex flex-col gap-0.5">
                    {productsInCategory(cat).map((key) => {
                      const p = BRAND.products[key];
                      return (
                        <a
                          key={key}
                          href={productHref(key)}
                          onClick={() => setOpen(false)}
                          className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-slate-300 hover:bg-white/5 hover:text-white"
                        >
                          <Image
                            src={PRODUCT_IMG[key]}
                            alt=""
                            width={22}
                            height={22}
                            className="h-5 w-5 object-contain"
                            draggable={false}
                          />
                          <span>
                            {p.zh}
                            <span className="ml-1.5 text-[11px] text-slate-500">{p.scene[lang]}</span>
                          </span>
                        </a>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
            {links.map((l) => (
              <a
                key={l.id}
                href={l.href}
                onClick={() => {
                  if ("onClick" in l && l.onClick) l.onClick();
                  setOpen(false);
                }}
                className="rounded-lg px-3 py-2 text-sm text-slate-300 hover:bg-white/5 hover:text-white"
              >
                {l.label}
              </a>
            ))}
            <a
              href={localePath(lang, "/brand")}
              onClick={() => setOpen(false)}
              className="rounded-lg px-3 py-2 text-sm text-slate-300 hover:bg-white/5 hover:text-white"
            >
              {lang === "zh" ? "品牌" : "Brand"}
            </a>
            <a
              href={isMiniApp ? "#contact" : CONTACT_URL}
              target={isMiniApp ? undefined : "_blank"}
              rel={isMiniApp ? undefined : "noreferrer"}
              className="mt-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-center text-sm font-medium text-ink-950"
            >
              {t.nav.cta}
            </a>
          </div>
        </div>
      )}
    </header>
  );
}
