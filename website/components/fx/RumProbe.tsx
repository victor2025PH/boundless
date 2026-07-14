"use client";

import { useEffect } from "react";
import { track } from "@/lib/track";

/** 真实用户性能采样(RUM):首屏动画落定后(7s)静默采 3s 帧间隔 + 长任务,
 *  连同设备信息一次性上报 track("rum")。用真实分布校准 data-fx 分档阈值,
 *  把"猜测的降级策略"变成"实测的降级策略"。每会话最多一次;
 *  页面不可见或疑似 rAF 节流(<5fps)时丢弃,不污染数据。 */
export default function RumProbe() {
  useEffect(() => {
    try {
      if (sessionStorage.getItem("ml_rum")) return;
    } catch {
      return;
    }

    let longCount = 0;
    let blocked = 0;
    let po: PerformanceObserver | null = null;
    try {
      po = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          longCount++;
          blocked += e.duration;
        }
      });
      po.observe({ type: "longtask", buffered: true });
    } catch {
      po = null;
    }

    // LCP:验证"入场动画是否推迟了最大内容渲染"(Reveal 初始 opacity:0 的代价)
    let lcp = 0;
    let lcpPo: PerformanceObserver | null = null;
    try {
      lcpPo = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) lcp = Math.max(lcp, e.startTime);
      });
      lcpPo.observe({ type: "largest-contentful-paint", buffered: true });
    } catch {
      lcpPo = null;
    }

    let raf = 0;
    let timer = 0;
    const cleanup = () => {
      po?.disconnect();
      lcpPo?.disconnect();
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
    };

    const sample = () => {
      if (document.hidden) {
        cleanup();
        return;
      }
      const frames: number[] = [];
      let last = performance.now();
      const t0 = last;
      const loop = (now: number) => {
        frames.push(now - last);
        last = now;
        if (now - t0 < 3000) {
          raf = requestAnimationFrame(loop);
          return;
        }
        // 遮挡节流(帧数过少)或采样中途切后台:数据无意义,丢弃
        if (!document.hidden && frames.length >= 15) {
          frames.sort((a, b) => a - b);
          const avg = frames.reduce((s, v) => s + v, 0) / frames.length;
          const p95 = frames[Math.floor(frames.length * 0.95)];
          try {
            sessionStorage.setItem("ml_rum", "1");
          } catch {}
          const nav = navigator as Navigator & { deviceMemory?: number };
          track("rum", {
            fps: Math.round(1000 / avg),
            p95: Math.round(p95 * 10) / 10,
            long: longCount,
            blocked: Math.round(blocked),
            lcp: lcp ? Math.round(lcp) : null,
                  tier: document.documentElement.getAttribute("data-fx") || "?",
                  mode: document.documentElement.getAttribute("data-mode") === "day" ? "day" : "night",
            dm: nav.deviceMemory ?? null,
            hc: navigator.hardwareConcurrency ?? null,
            dpr: Math.round(window.devicePixelRatio * 100) / 100,
            vw: window.innerWidth,
            reduced: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 : 0,
            coarse: window.matchMedia("(pointer: coarse)").matches ? 1 : 0,
          });
        }
        cleanup();
      };
      raf = requestAnimationFrame(loop);
    };

    timer = window.setTimeout(sample, 7000);
    return cleanup;
  }, []);

  return null;
}
