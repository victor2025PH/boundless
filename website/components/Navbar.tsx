"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { Menu, X, Languages, ChevronDown, Download, ArrowRight, Tag } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CONTACT_URL, localePath } from "@/lib/site";
import { track } from "@/lib/track";
import BrandMark from "./BrandMark";
import ModeToggle from "./ModeToggle";
import { BRAND, CATEGORIES, CATEGORY_ORDER, type ProductKey } from "@/lib/brand";
import { CATEGORY_UI } from "@/lib/categoryUi";
import { PRODUCT_LANDING, PRODUCT_ANCHOR, publicProductsInCategory } from "./productMeta";
import ProductIcon from "./ProductIcon";
import { CLIENT_APPS, CLIENT_COVERED_PRODUCTS } from "@/lib/downloads";
import { NAV_PRICING, NAV_CONTACT, NAV_BRAND, NAV_SLOT_ORDER, navLabel, type NavSlot } from "@/lib/nav";

export default function Navbar() {
  const { t, lang, toggle } = useLang();
  const { isMiniApp } = useTelegram();
  const pathname = usePathname();
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState("");
  // 产品 / 下载下拉：hover（鼠标）+ click（触屏/键盘）双模式。纯 :hover 在触屏上打不开，
  // 鼠标用户点击也无反馈——两类用户都会感知为「点击没有响应」。
  const [prodOpen, setProdOpen] = useState(false);
  const prodRef = useRef<HTMLDivElement>(null);
  const [dlOpen, setDlOpen] = useState(false);
  const dlRef = useRef<HTMLDivElement>(null);

  // 点击面板外 / Esc 关闭（两个下拉共用一套监听）
  useEffect(() => {
    if (!prodOpen && !dlOpen) return;
    const onDown = (e: PointerEvent) => {
      if (!prodRef.current?.contains(e.target as Node)) setProdOpen(false);
      if (!dlRef.current?.contains(e.target as Node)) setDlOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setProdOpen(false);
        setDlOpen(false);
      }
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [prodOpen, dlOpen]);

  // 路由变化（跳到产品落地页 / 下载页）后收起
  useEffect(() => {
    setProdOpen(false);
    setDlOpen(false);
    setOpen(false);
  }, [pathname]);

  // 移动抽屉打开期间：锁滚动 + Esc 关闭 + <html data-nav-open>（CSS 据此隐藏
  // 低层浮动元素：AI 聊天挑逗气泡 / 底部粘性条，防止叠在抽屉上）。
  // 旋转/放大到桌面断点时自动关闭，避免 overflow 锁残留在 md+ 视口。
  useEffect(() => {
    if (!open) return;
    const root = document.documentElement;
    root.setAttribute("data-nav-open", "1");
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const mq = window.matchMedia("(min-width: 768px)");
    const onMq = () => {
      if (mq.matches) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    mq.addEventListener("change", onMq);
    return () => {
      root.removeAttribute("data-nav-open");
      document.body.style.overflow = prevOverflow;
      document.removeEventListener("keydown", onKey);
      mq.removeEventListener("change", onMq);
    };
  }, [open]);

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
    // 仅观察仍存在的首页 section（#realtime/#showcase/#engage/#pricing 已随首页收敛下线）
    const ids = ["translate", "autochat", "contact"];
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

  // ===== 菜单数据（单一事实源 lib/nav.ts；桌面横排与移动抽屉共用 NAV_SLOT_ORDER）=====
  const pricingHref = localePath(lang, NAV_PRICING.path!);
  const brandHref = localePath(lang, NAV_BRAND.path!);
  const contactHref = anchor(NAV_CONTACT.anchor!);
  const onOrderPage = pathname.includes("/order");
  const onBrandPage = pathname.includes("/brand");
  // 所有客户端下载页路径（含 /en 前缀版）都包含 "/download" 片段
  const onDownloadPage = pathname.includes("/download");
  const publicClients = CLIENT_APPS.filter((app) => !app.gated);

  const activeBar = (
    <span className="absolute -bottom-1.5 left-0 h-0.5 w-full rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet" />
  );

  /** 桌面横排的一个槽位 */
  function renderDesktopSlot(slot: NavSlot) {
    switch (slot) {
      case "products":
        return (
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
                      {/* 实施78 P0-2：中文页「智连 GROWTH」双写助记，英文页只留英文名——
                          反向双写（「GROWTH 智连」）对不识汉字的访客是纯噪音。与
                          BrandShowcase 的三系标题同一口径，两处要一起改。 */}
                      <div className={`mb-2 border-b border-white/5 pb-1.5 text-xs font-semibold ${ui.label}`}>
                        {lang === "zh" ? cc.zh : cc.en}
                        {lang === "zh" && <span className="ml-1 font-normal text-slate-500">{cc.en}</span>}
                      </div>
                      <div className="flex flex-col gap-0.5">
                        {publicProductsInCategory(cat).map((key) => {
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
                              <ProductIcon
                                product={key}
                                size={28}
                                alt=""
                                className="h-7 w-7 shrink-0 object-contain opacity-90 transition group-hover/item:opacity-100"
                              />
                              <span className="min-w-0 flex-1">
                                <span className="block text-sm text-slate-200 group-hover/item:text-white">
                                  {lang === "zh" ? (
                                    <>
                                      {p.zh}
                                      <span className={`ml-1.5 text-xs ${CATEGORY_UI[cat].enName}`}>{p.en}</span>
                                    </>
                                  ) : (
                                    p.en
                                  )}
                                </span>
                                <span className="block truncate text-[11px] text-slate-500">{p.scene[lang]}</span>
                              </span>
                              {CLIENT_COVERED_PRODUCTS.has(key) && (
                                <span
                                  title={lang === "zh" ? "提供桌面客户端" : "Desktop client available"}
                                  className="shrink-0"
                                >
                                  <Download
                                    aria-hidden
                                    className="h-3 w-3 text-slate-600 transition group-hover/item:text-neon-cyan"
                                  />
                                </span>
                              )}
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
        );
      case "pricing":
        return (
          <a
            href={pricingHref}
            onClick={() => track("cta_click", { where: "nav_pricing" })}
            className={`relative text-sm transition-colors hover:text-white ${
              onOrderPage ? "text-white" : "text-slate-300"
            }`}
          >
            {navLabel(NAV_PRICING, lang)}
            {onOrderPage && activeBar}
          </a>
        );
      case "download":
        return (
          <div ref={dlRef} className="group relative">
            <button
              onClick={() => setDlOpen((v) => !v)}
              aria-expanded={dlOpen}
              aria-haspopup="menu"
              className={`relative inline-flex items-center gap-1 text-sm transition-colors hover:text-white ${
                onDownloadPage ? "text-white" : "text-slate-300"
              }`}
            >
              {lang === "zh" ? "下载" : "Download"}
              <ChevronDown
                className={`h-3.5 w-3.5 opacity-70 transition-transform group-hover:rotate-180 ${dlOpen ? "rotate-180" : ""}`}
              />
              {onDownloadPage && activeBar}
            </button>
            <div
              className={`absolute left-1/2 top-full z-50 -translate-x-1/2 pt-3 transition duration-150 group-hover:visible group-hover:opacity-100 ${
                dlOpen ? "visible opacity-100" : "invisible opacity-0"
              }`}
            >
              <div className="glass w-[340px] rounded-2xl border border-white/10 p-2">
                {publicClients.map((app) => (
                  <a
                    key={app.key}
                    href={localePath(lang, app.page)}
                    rel={app.gated ? "nofollow" : undefined}
                    onClick={() => {
                      setDlOpen(false);
                      track("download_menu_click", { client: app.key });
                    }}
                    className="group/dl flex items-center gap-2.5 rounded-lg px-2.5 py-2 transition hover:bg-white/5"
                  >
                    {app.productIcon ? (
                      <ProductIcon
                        product={app.productIcon}
                        size={28}
                        alt=""
                        className="h-7 w-7 shrink-0 object-contain opacity-90 transition group-hover/dl:opacity-100"
                      />
                    ) : (
                      <BrandMark className="h-7 w-7 shrink-0" />
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm text-slate-200 group-hover/dl:text-white">{app.name[lang]}</span>
                      <span className="block truncate text-[11px] text-slate-500">{app.tagline[lang]}</span>
                    </span>
                    {/* 版本号刻意不进下拉：构建时常量会落后于运行时 manifest（页面内有权威版本） */}
                    <Download className="h-3.5 w-3.5 shrink-0 text-slate-600 transition group-hover/dl:text-neon-cyan" />
                  </a>
                ))}
                <div className="mt-1 border-t border-white/5 pt-1">
                  <a
                    href={localePath(lang, "/download")}
                    onClick={() => {
                      setDlOpen(false);
                      track("download_menu_click", { client: "hub" });
                    }}
                    className="flex items-center justify-center gap-1.5 rounded-lg px-2.5 py-2 text-xs font-medium text-neon-cyan transition hover:bg-white/5"
                  >
                    <Download className="h-3.5 w-3.5" />
                    {lang === "zh" ? "打开下载中心" : "Open Download Center"}
                    <ArrowRight className="h-3.5 w-3.5" />
                  </a>
                </div>
              </div>
            </div>
          </div>
        );
      case "contact":
        return (
          <a
            href={contactHref}
            className={`relative text-sm transition-colors hover:text-white ${
              active === "contact" ? "text-white" : "text-slate-300"
            }`}
          >
            {navLabel(NAV_CONTACT, lang)}
            {active === "contact" && activeBar}
          </a>
        );
      case "brand":
        return (
          <a
            href={brandHref}
            className={`relative text-sm transition-colors hover:text-white ${
              onBrandPage ? "text-white" : "text-slate-300"
            }`}
          >
            {navLabel(NAV_BRAND, lang)}
            {onBrandPage && activeBar}
          </a>
        );
    }
  }

  /** 移动抽屉的一个槽位（与桌面同一 NAV_SLOT_ORDER，跨端同序同名） */
  function renderDrawerSlot(slot: NavSlot) {
    switch (slot) {
      case "products":
        return (
          <div className="rounded-xl bg-white/[0.03] p-2">
            {CATEGORY_ORDER.map((cat) => (
              <div key={cat} className="mb-2 last:mb-0">
                <div className={`px-2 py-1 text-xs font-semibold ${CATEGORY_UI[cat].label}`}>
                  {lang === "zh" ? CATEGORIES[cat].zh : CATEGORIES[cat].en}
                  {lang === "zh" && (
                    <span className="ml-1.5 font-normal text-slate-600">{CATEGORIES[cat].en}</span>
                  )}
                </div>
                <div className="flex flex-col">
                  {publicProductsInCategory(cat).map((key) => {
                    const p = BRAND.products[key];
                    return (
                      <a
                        key={key}
                        href={productHref(key)}
                        onClick={() => {
                          setOpen(false);
                          track("product_click", { key, where: "drawer" });
                        }}
                        className="flex min-h-[44px] items-center gap-2.5 rounded-lg px-2 py-2 text-sm text-slate-300 transition hover:bg-white/5 hover:text-white"
                      >
                        <ProductIcon product={key} size={24} alt="" className="h-6 w-6 shrink-0 object-contain" />
                        <span className="min-w-0 flex-1">
                          {lang === "zh" ? p.zh : p.en}
                          <span className="ml-1.5 text-[11px] text-slate-500">{p.scene[lang]}</span>
                        </span>
                        {CLIENT_COVERED_PRODUCTS.has(key) && (
                          <Download aria-hidden className="h-3 w-3 shrink-0 text-slate-600" />
                        )}
                      </a>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        );
      case "pricing":
        // 价格是移动端最高价值入口：高亮行，不与普通菜单项混在一起
        return (
          <a
            href={pricingHref}
            onClick={() => {
              setOpen(false);
              track("cta_click", { where: "nav_pricing_drawer" });
            }}
            className="flex min-h-[48px] items-center gap-2.5 rounded-xl border border-neon-cyan/35 bg-neon-cyan/10 px-3.5 py-3 text-sm font-medium text-neon-cyan transition hover:bg-neon-cyan/15"
          >
            <Tag className="h-4 w-4 shrink-0" />
            <span className="flex-1">{navLabel(NAV_PRICING, lang)}</span>
            <ArrowRight className="h-4 w-4 shrink-0 opacity-70" />
          </a>
        );
      case "download":
        return (
          <div className="rounded-xl bg-white/[0.03] p-2">
            <div className="flex items-center gap-1.5 px-2 py-1 text-xs font-semibold text-slate-400">
              <Download className="h-3.5 w-3.5" />
              {lang === "zh" ? "客户端下载" : "Downloads"}
            </div>
            <div className="flex flex-col">
              {publicClients.map((app) => (
                <a
                  key={app.key}
                  href={localePath(lang, app.page)}
                  rel={app.gated ? "nofollow" : undefined}
                  onClick={() => {
                    setOpen(false);
                    track("download_menu_click", { client: app.key, where: "mobile" });
                  }}
                  className="flex min-h-[44px] items-center gap-2.5 rounded-lg px-2 py-2 text-sm text-slate-300 transition hover:bg-white/5 hover:text-white"
                >
                  {app.productIcon ? (
                    <ProductIcon product={app.productIcon} size={24} alt="" className="h-6 w-6 shrink-0 object-contain" />
                  ) : (
                    <BrandMark className="h-6 w-6 shrink-0" />
                  )}
                  <span className="flex-1">{app.name[lang]}</span>
                  <Download className="h-3.5 w-3.5 shrink-0 text-slate-600" />
                </a>
              ))}
            </div>
          </div>
        );
      case "contact":
        return (
          <a
            href={contactHref}
            onClick={() => setOpen(false)}
            className="flex min-h-[44px] items-center rounded-xl px-3.5 py-2.5 text-sm text-slate-300 transition hover:bg-white/5 hover:text-white"
          >
            {navLabel(NAV_CONTACT, lang)}
          </a>
        );
      case "brand":
        return (
          <a
            href={brandHref}
            onClick={() => setOpen(false)}
            className={`flex min-h-[44px] items-center rounded-xl px-3.5 py-2.5 text-sm transition hover:bg-white/5 hover:text-white ${
              onBrandPage ? "bg-white/5 text-white" : "text-slate-300"
            }`}
          >
            {navLabel(NAV_BRAND, lang)}
          </a>
        );
    }
  }

  const ctaHref = isMiniApp ? "#contact" : CONTACT_URL;
  const ctaExternal = !isMiniApp;

  return (
    <>
      <header
        className={`fixed inset-x-0 top-0 z-[var(--z-nav)] transition-all ${
          scrolled ? "glass" : "bg-transparent"
        }`}
      >
        {/* 滚动后顶栏收窄（py-4 → py-2.5），把注意力还给内容 */}
        <nav
          className={`mx-auto flex max-w-7xl items-center justify-between px-5 transition-[padding] duration-300 ${
            scrolled ? "py-2.5" : "py-4"
          }`}
        >
          <a href={onHome ? "#top" : home} className="flex min-w-0 items-center gap-2">
            <BrandMark className="h-9 w-9 shrink-0" />
            {/* 实施78 P0-2：英文路由只出 BOUNDLESS（纯拉丁字形），中文路由保持双写 */}
            <span className="truncate text-lg font-semibold tracking-wide text-white">
              {lang === "zh" ? (
                <>
                  {BRAND.company.zh} <span className="hidden text-slate-400 sm:inline">{BRAND.company.en}</span>
                </>
              ) : (
                BRAND.company.en
              )}
            </span>
          </a>

          <div className="hidden items-center gap-8 md:flex">
            {NAV_SLOT_ORDER.map((slot) => (
              <div key={slot} className="contents">
                {renderDesktopSlot(slot)}
              </div>
            ))}
          </div>

          <div className="flex items-center gap-2.5 md:gap-3">
            {/* 语言/主题切换是低频操作：移动端收进抽屉，把顶栏位置让给「看价格」直达 */}
            <ModeToggle className="hidden md:flex" />
            <button
              onClick={toggle}
              className="hidden items-center gap-1.5 rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-300 transition hover:border-neon-cyan/50 hover:text-white md:flex"
              aria-label="switch language"
            >
              <Languages className="h-4 w-4" />
              {lang === "zh" ? "EN" : "中文"}
            </button>
            <a
              href={ctaHref}
              target={ctaExternal ? "_blank" : undefined}
              rel={ctaExternal ? "noreferrer" : undefined}
              onClick={() => track("cta_click", { where: isMiniApp ? "nav_miniapp" : "nav" })}
              className="hidden rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-sm font-medium text-ink-950 transition hover:opacity-90 md:inline-block"
            >
              {t.nav.cta}
            </a>
            {/* 移动端顶栏直达「看价格」：最高商业价值入口不再只活在汉堡第 9 项 */}
            <a
              href={pricingHref}
              onClick={() => track("cta_click", { where: "nav_pricing_mobile" })}
              className="inline-flex min-h-[36px] items-center gap-1 rounded-full border border-neon-cyan/40 bg-neon-cyan/10 px-3 py-1.5 text-xs font-medium text-neon-cyan transition hover:bg-neon-cyan/20 md:hidden"
            >
              <Tag className="h-3.5 w-3.5" />
              {navLabel(NAV_PRICING, lang)}
            </a>
            <button
              className="grid h-10 w-10 place-items-center text-slate-200 md:hidden"
              onClick={() => setOpen((v) => !v)}
              aria-label={open ? "close menu" : "menu"}
              aria-expanded={open}
            >
              {open ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
            </button>
          </div>
        </nav>
      </header>

      {/* 移动全屏抽屉：作为 header 的兄弟节点渲染——header.glass 的 backdrop-filter
          会成为 fixed 后代的包含块，嵌在里面会把 inset-0 算成 header 的尺寸。 */}
      {open && (
        <div
          className="fixed inset-0 z-[var(--z-drawer)] md:hidden"
          role="dialog"
          aria-modal="true"
          aria-label={lang === "zh" ? "网站菜单" : "Site menu"}
        >
          <div className="nav-drawer flex h-full flex-col bg-ink-950/95 backdrop-blur-2xl">
            <div className="flex items-center justify-between px-5 py-3">
              <a href={onHome ? "#top" : home} onClick={() => setOpen(false)} className="flex items-center gap-2">
                <BrandMark className="h-8 w-8" />
                <span className="text-base font-semibold text-white">
                  {lang === "zh" ? (
                    <>
                      {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
                    </>
                  ) : (
                    BRAND.company.en
                  )}
                </span>
              </a>
              <button
                className="grid h-10 w-10 place-items-center rounded-full text-slate-300 transition hover:bg-white/5 hover:text-white"
                onClick={() => setOpen(false)}
                aria-label="close menu"
              >
                <X className="h-6 w-6" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-4 pb-4">
              <div className="flex flex-col gap-2">
                {NAV_SLOT_ORDER.map((slot, i) => (
                  <div key={slot} className="nav-drawer-item" style={{ animationDelay: `${i * 45}ms` }}>
                    {renderDrawerSlot(slot)}
                  </div>
                ))}
                {/* 设置行：语言 / 主题（从顶栏移入，低频操作不占顶栏位） */}
                <div
                  className="nav-drawer-item mt-1 flex items-center justify-between rounded-xl bg-white/[0.03] px-3.5 py-2.5"
                  style={{ animationDelay: `${NAV_SLOT_ORDER.length * 45}ms` }}
                >
                  <span className="text-xs text-slate-500">{lang === "zh" ? "语言与外观" : "Language & theme"}</span>
                  <div className="flex items-center gap-2.5">
                    <button
                      onClick={toggle}
                      className="flex min-h-[36px] items-center gap-1.5 rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-300 transition hover:border-neon-cyan/50 hover:text-white"
                      aria-label="switch language"
                    >
                      <Languages className="h-4 w-4" />
                      {lang === "zh" ? "EN" : "中文"}
                    </button>
                    <ModeToggle />
                  </div>
                </div>
              </div>
            </div>

            <div className="border-t border-white/10 p-4 pb-[calc(1rem+env(safe-area-inset-bottom))]">
              <a
                href={ctaHref}
                target={ctaExternal ? "_blank" : undefined}
                rel={ctaExternal ? "noreferrer" : undefined}
                onClick={() => {
                  setOpen(false);
                  track("cta_click", { where: "nav_drawer" });
                }}
                className="flex min-h-[48px] items-center justify-center rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-3 text-center text-sm font-semibold text-ink-950"
              >
                {t.nav.cta}
              </a>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
