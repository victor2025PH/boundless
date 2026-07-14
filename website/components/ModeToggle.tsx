"use client";

import { useEffect, useState } from "react";
import { Sun, Moon } from "lucide-react";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";

const COPY = {
  zh: { toDay: "切换到白天模式", toNight: "切换到夜间模式", day: "白天模式", night: "夜间模式" },
  en: { toDay: "Switch to day mode", toNight: "Switch to night mode", day: "Day mode", night: "Night mode" },
} as const;

/** 白天/夜间模式开关(Navbar 常驻):
 *  - 首帧模式由 layout 内联脚本按 localStorage(bl-mode) > 系统 prefers-color-scheme 判定;
 *  - 用户未手动选择过时,系统亮暗切换会实时跟随;点击按钮即"手动接管"并永久记忆;
 *  - 切换瞬间给 <html> 挂 mode-switching,让全站颜色以 0.45s 渐变过渡(样式见 globals.css)。 */
export default function ModeToggle({ className }: { className?: string }) {
  const { lang } = useLang();
  const c = COPY[lang];
  const [day, setDay] = useState<boolean | null>(null);

  useEffect(() => {
    setDay(document.documentElement.getAttribute("data-mode") === "day");

    // 跟随系统:仅当用户从未手动选择时
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onSystem = () => {
      try {
        if (localStorage.getItem("bl-mode")) return;
      } catch {
        return;
      }
      apply(mq.matches, false);
    };
    mq.addEventListener("change", onSystem);
    return () => mq.removeEventListener("change", onSystem);
  }, []);

  function apply(next: boolean, remember: boolean) {
    const root = document.documentElement;
    root.classList.add("mode-switching");
    window.setTimeout(() => root.classList.remove("mode-switching"), 600);
    if (next) root.setAttribute("data-mode", "day");
    else root.removeAttribute("data-mode");
    setDay(next);
    if (remember) {
      try {
        localStorage.setItem("bl-mode", next ? "day" : "night");
      } catch {}
    }
  }

  const onClick = () => {
    const next = !day;
    apply(next, true);
    track("mode_toggle", { mode: next ? "day" : "night" });
  };

  return (
    <button
      onClick={onClick}
      className={`flex h-8 w-8 items-center justify-center rounded-full border border-white/10 text-slate-300 transition hover:border-neon-cyan/50 hover:text-white ${className ?? ""}`}
      aria-label={day ? c.toNight : c.toDay}
      title={day ? c.night : c.day}
    >
      {/* 显示"点击后将进入"的模式:夜间亮太阳,白天亮月亮 */}
      {day ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
    </button>
  );
}
