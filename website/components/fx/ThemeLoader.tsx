"use client";

import { useEffect } from "react";

/** 运行时活动皮肤:空闲时拉取 /api/fx-theme(浏览器缓存 2 分钟),
 *  非空则覆盖 <html data-theme>,空字符串表示回到默认青紫。
 *  首帧仍按构建时 NEXT_PUBLIC_FX_THEME 渲染,二者可并存,运行时优先。 */
export default function ThemeLoader() {
  useEffect(() => {
    const id = window.setTimeout(async () => {
      try {
        const res = await fetch("/api/fx-theme");
        if (!res.ok) return;
        const { theme } = (await res.json()) as { theme?: string };
        if (theme) document.documentElement.setAttribute("data-theme", theme);
        else document.documentElement.removeAttribute("data-theme");
      } catch {
        /* 网络异常保持现状 */
      }
    }, 1200); // 避开首屏关键请求窗口
    return () => window.clearTimeout(id);
  }, []);

  return null;
}
