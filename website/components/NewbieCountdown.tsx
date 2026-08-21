"use client";

// 新人 6U 真倒计时（/order 金卡与 /pricing 海报共用）。
// 铁律（实施 50）：匿名访客绝不出假倒计时——只有拿到注册时间锚（?reg_ts= 深链，
// 桌面海报 CTA 追加；localStorage bl-reg-ts 续存）才渲染真钟；锚无效 = 什么都不出。
// 钟是纯展示：资格终审在履约端 72h 窗（锚=最早 trial claim），改参数骗不到加赠，
// 只会在下单时收到人话拒单文案。
import { useEffect, useState } from "react";
import { track } from "@/lib/track";
import { NEWBIE_PACK } from "@/lib/chatx-pricing";

/** 注册时间锚解析（秒 / 毫秒兼容）：必须是过去时间且 30 天内——垃圾值 / 陈年值
 *  不进倒计时语义，返回 0（= 不显示倒计时，绝不显示一个错的钟）。 */
export function parseRegTs(raw: string | null): number {
  const n = Number(raw || 0);
  if (!Number.isFinite(n) || n <= 0) return 0;
  const ms = n > 1e12 ? n : n * 1000;
  const age = Date.now() - ms;
  if (age < 0 || age > 30 * 86400e3) return 0;
  return ms;
}

/** localStorage 续存读取（bl-reg-ts；SSR / 隐私模式安全）。 */
export function readStoredRegTs(): number {
  try {
    return parseRegTs(localStorage.getItem("bl-reg-ts"));
  } catch {
    return 0;
  }
}

/** 剩余 >0：红色紧迫钟 HH:MM:SS（每秒 tick 只重渲染本组件）；
 *  超窗：诚实黄字（与服务端拒单文案同口径，引导首充档）。 */
export default function NewbieCountdown({ regTs, zh }: { regTs: number; zh: boolean }) {
  const deadline = regTs + NEWBIE_PACK.windowHours * 3_600_000;
  const [left, setLeft] = useState(() => deadline - Date.now());
  useEffect(() => {
    const id = setInterval(() => setLeft(deadline - Date.now()), 1000);
    return () => clearInterval(id);
  }, [deadline]);
  useEffect(() => {
    // 挂载各打一次曝光（剩余小时分桶 / 超窗），供漏斗判断「倒计时有没有催单效果」
    if (deadline - Date.now() > 0) {
      track("order_newbie_countdown", { hours_left: Math.floor((deadline - Date.now()) / 3_600_000) });
    } else {
      track("order_newbie_window_passed_view", {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (left <= 0) {
    return (
      <span className="inline-flex w-fit items-center gap-1 rounded-full bg-amber-300/10 px-2.5 py-1 text-[11px] text-amber-300">
        {zh ? "6U 窗口已过 · 首充仍享最高 +40%" : "6U window passed — first top-up still earns up to +40%"}
      </span>
    );
  }
  const pad = (x: number) => String(x).padStart(2, "0");
  const h = Math.floor(left / 3_600_000);
  const m = Math.floor(left / 60_000) % 60;
  const s = Math.floor(left / 1_000) % 60;
  return (
    <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-rose-400/15 px-2.5 py-1 text-xs font-semibold tabular-nums text-rose-300">
      ⏳ {zh ? "限时剩余" : "Ends in"} {pad(h)}:{pad(m)}:{pad(s)}
    </span>
  );
}
