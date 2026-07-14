"use client";

import { useEffect } from "react";

/** 锚点跳转空间跃迁:委托监听全站同页 hash 链接点击,向背景发 bl-warp 冲刺事件
 *  (TechBackground 的 rAF 管线将星空/网格短暂加速),同时给星层一个 0.9s 增亮脉冲。
 *  把"页面跳转"演绎成"空间跃迁";reduced-motion 与低配挡位不启用。 */
export default function WarpNav() {
  useEffect(() => {
    if (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      document.documentElement.getAttribute("data-fx") === "low"
    )
      return;

    let t = 0;
    const onClick = (e: MouseEvent) => {
      const a = (e.target as Element | null)?.closest?.('a[href*="#"]') as HTMLAnchorElement | null;
      if (!a) return;
      const href = a.getAttribute("href") || "";
      const hash = href.slice(href.indexOf("#") + 1);
      if (!hash) return;
      // 只处理本页锚点(跨页链接走整页导航,无跃迁意义)
      try {
        if (new URL(a.href, location.href).pathname !== location.pathname) return;
      } catch {
        return;
      }
      const el = document.getElementById(hash);
      if (!el) return;
      const dir = el.getBoundingClientRect().top > 80 ? 1 : -1;
      window.dispatchEvent(new CustomEvent("bl-warp", { detail: { dir } }));
      document.documentElement.classList.add("fx-warp");
      window.clearTimeout(t);
      t = window.setTimeout(() => document.documentElement.classList.remove("fx-warp"), 900);
    };

    document.addEventListener("click", onClick);
    return () => {
      document.removeEventListener("click", onClick);
      window.clearTimeout(t);
      document.documentElement.classList.remove("fx-warp");
    };
  }, []);

  return null;
}
