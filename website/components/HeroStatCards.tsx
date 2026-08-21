"use client";

// 首屏关键数字数据卡（/order 与 /pricing 共用；数据源 lib/order-hero.ts 单源派生）。
// dynamic="activation" 的卡会用 /api/order/stats 的近 60 天实测 p50 到账时长替换
// 运营口径常量——真数字比承诺更可信；样本不足 / 接口失败一律回落常量（fail-open）。
import { useEffect, useState } from "react";
import CountUp from "./fx/CountUp";
import type { OrderHeroStat } from "@/lib/order-hero";

/** 模块级缓存：/order 与 /pricing 各渲染一份数据卡，同会话只打一次 stats 接口。
 *  undefined = 未取过；null = 取过但无实测值（样本不足 / 接口失败）。 */
let measuredCache: number | null | undefined;

function useActivationP50(enabled: boolean): number | null {
  const [v, setV] = useState<number | null>(measuredCache ?? null);
  useEffect(() => {
    if (!enabled) return;
    if (measuredCache !== undefined) {
      setV(measuredCache);
      return;
    }
    let alive = true;
    fetch("/api/order/stats")
      .then((r) => r.json())
      .then((j: { ok?: boolean; activation_p50_min?: number | null }) => {
        measuredCache =
          j?.ok && typeof j.activation_p50_min === "number" && j.activation_p50_min > 0
            ? Math.max(1, Math.round(j.activation_p50_min))
            : null;
        if (alive) setV(measuredCache);
      })
      .catch(() => {
        measuredCache = null;
      });
    return () => {
      alive = false;
    };
  }, [enabled]);
  return v;
}

export default function HeroStatCards({ stats, zh }: { stats: OrderHeroStat[]; zh: boolean }) {
  const wantsMeasured = stats.some((s) => s.dynamic === "activation");
  const measured = useActivationP50(wantsMeasured);
  return (
    <div className="mx-auto grid w-full max-w-3xl grid-cols-2 gap-3 md:grid-cols-4">
      {stats.map((s) => {
        const isMeasured = s.dynamic === "activation" && measured !== null;
        const value = isMeasured ? String(measured) : s.value;
        const label = zh ? s.label.zh : s.label.en;
        return (
          <div key={s.label.en} className="glass card-hover rounded-2xl px-3 py-4 text-center">
            <div className="text-gradient text-2xl font-bold tabular-nums md:text-3xl">
              <CountUp
                value={value}
                suffix={s.suffix ? (zh ? s.suffix.zh : s.suffix.en) : ""}
                grouping={s.grouping}
                duration={1.1}
              />
            </div>
            <div className="mt-1 text-[11px] leading-snug text-slate-400 md:text-xs">
              {label}
              {isMeasured && (
                <span className="ml-1 text-[10px] text-emerald-400/90" title={zh ? "近 60 天已开通订单的中位到账时长" : "Median payment→activation time, last 60 days"}>
                  {zh ? "· 实测" : "· measured"}
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
