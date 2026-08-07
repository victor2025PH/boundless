"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { Send, Tag } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import { NAV_PRICING, navLabel } from "@/lib/nav";
import { COOKIE_CONSENT_KEY, COOKIE_DECIDED_EVENT } from "./CookieConsent";
import { getLocal } from "@/lib/safe-storage";

export default function StickyCTA() {
  const { t, lang } = useLang();
  const { isMiniApp } = useTelegram();
  const pathname = usePathname();
  const [show, setShow] = useState(false);
  // Cookie 横幅与粘性条同在屏幕底部：横幅未处理前让位（否则互相叠压，价格入口被盖住）。
  const [cookiePending, setCookiePending] = useState(false);

  useEffect(() => {
    // 阈值 200px（原 600px）：移动端价格入口是最高价值动线，第一屏轻滚动即出现；
    // 不做 0px 常驻是避免与首屏 Hero 的「查看套餐与价格」按钮同屏双写。
    const onScroll = () => setShow(window.scrollY > 200);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    setCookiePending(!getLocal(COOKIE_CONSENT_KEY));
    const onDecided = () => setCookiePending(false);
    window.addEventListener(COOKIE_DECIDED_EVENT, onDecided);
    return () => window.removeEventListener(COOKIE_DECIDED_EVENT, onDecided);
  }, []);

  // 注意：早退分支必须放在所有 hooks 之后（React hooks 规则；旧版写在 useEffect 前）。
  if (isMiniApp) return null;

  // 价格入口与顶栏同源（lib/nav.ts）：文案「看价格」、落点 /order 全端一致。
  //（小语种页 /ko /ja 没有本语言下单页，走英文。）
  const isZhRoute = !pathname?.match(/^\/(en|ko|ja)(\/|$)/);
  const pricingHref = isZhRoute ? NAV_PRICING.path! : `/en${NAV_PRICING.path!}`;
  // 已在下单页时价格按钮是自链接，只保留咨询 CTA。
  const onOrderPage = !!pathname?.includes("/order");

  return (
    <div
      className={`sticky-cta fixed inset-x-0 bottom-0 z-[var(--z-sticky)] transition-transform duration-300 lg:hidden ${
        show && !cookiePending ? "translate-y-0" : "translate-y-full"
      }`}
    >
      <div className="glass flex items-center gap-2 border-t border-white/10 px-3 py-2.5 pb-[calc(0.625rem+env(safe-area-inset-bottom))]">
        {!onOrderPage && (
          <a
            href={pricingHref}
            onClick={() => track("cta_click", { where: "sticky_pricing" })}
            className="sticky-cta-secondary flex min-h-[44px] flex-1 items-center justify-center gap-1.5 rounded-full border border-white/15 py-2.5 text-sm font-medium text-slate-200"
          >
            <Tag className="h-4 w-4" />
            {navLabel(NAV_PRICING, lang)}
          </a>
        )}
        <a
          href={CONTACT_URL}
          target="_blank"
          rel="noreferrer"
          onClick={() => track("cta_click", { where: "sticky_mobile" })}
          className="cta-fx flex min-h-[44px] flex-[1.4] items-center justify-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet py-2.5 text-sm font-semibold text-ink-950"
        >
          <Send className="h-4 w-4" />
          {t.nav.cta}
        </a>
      </div>
    </div>
  );
}
