"use client";

// 全站「新人 6U 大礼包」浮动海报（2026-08-21 充值唯一化配套）：
//  - 桌面宽度专属（md+）：移动端底部已有 StickyCTA，再叠角标海报是骚扰；
//  - /pricing 与 /order 不出（价格页有整幅海报位、下单页正在掏钱别打岔）；
//  - 频控走 localStorage：关闭后 7 天内不再出现；点 CTA 视同关闭；
//  - 左下角落位（右下是 AIChat 悬浮客服的地盘）；6 秒延迟入场，不抢首屏；
//  - 不放倒计时——网页端拿不到访客注册时间，假紧迫感是信任自杀；
//    真实 72h 倒计时在桌面端弹窗（campaigns feed 驱动）。
import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Gift, X } from "lucide-react";
import { track } from "@/lib/track";
import { NEWBIE_PACK, rechargeUnitPrice, tokenRate } from "@/lib/chatx-pricing";
import { overlayPolicy } from "@/lib/overlay-policy";

const LS_KEY = "bl-newbie-poster-dismissed-ts";
const MUTE_DAYS = 7;
const SHOW_DELAY_MS = 6000;

const fmt = (n: number) => n.toLocaleString("en-US");

export default function NewbieFloatingPoster() {
  const pathname = usePathname() || "/";
  const [show, setShow] = useState(false);
  const zh = !pathname.startsWith("/en");

  // 排除页：价格页（有整幅海报）/ 下单页（勿打断支付）/ 小程序视图 / 素材舞台
  // + 实施78 P0-3：国际路由与 GEO 落地页由 lib/overlay-policy 统一判定（促销浮层对西方
  //   B2B 买家是可信度减分项，对 AI 直达的陌生读者是抢戏）——判定收在一处，别在此另写正则。
  const excluded =
    !overlayPolicy(pathname).promo ||
    /^\/(en\/)?(pricing|order)/.test(pathname) ||
    pathname.startsWith("/app") ||
    pathname.startsWith("/robot-stage");

  useEffect(() => {
    if (excluded) return;
    try {
      const ts = Number(localStorage.getItem(LS_KEY) || 0);
      if (ts && Date.now() - ts < MUTE_DAYS * 86400e3) return;
    } catch {
      /* localStorage 不可用（隐私模式）：宁静默不骚扰 */
      return;
    }
    const timer = setTimeout(() => {
      setShow(true);
      track("poster_view", { id: "newbie-6u", surface: "web_floating" });
    }, SHOW_DELAY_MS);
    return () => clearTimeout(timer);
  }, [excluded]);

  if (excluded || !show) return null;

  const mute = () => {
    try {
      localStorage.setItem(LS_KEY, String(Date.now()));
    } catch {
      /* 存不了就存不了，本会话内不再出现 */
    }
    setShow(false);
  };

  const aiReplies = Math.round(NEWBIE_PACK.tokens / tokenRate("ai_reply").tokens);

  return (
    <div className="fixed bottom-5 left-5 z-40 hidden w-[300px] md:block" role="dialog" aria-label={zh ? "新人礼包" : "Newcomer offer"}>
      <div className="relative overflow-hidden rounded-2xl border border-amber-300/40 bg-gradient-to-br from-[#241a33] via-[#141122] to-[#12233a] p-4 shadow-[0_8px_40px_rgba(0,0,0,0.5)]">
        <div className="pointer-events-none absolute -right-10 -top-12 h-36 w-36 rounded-full bg-amber-300/15 blur-[50px]" />
        <button
          onClick={() => {
            track("poster_dismiss", { id: "newbie-6u", surface: "web_floating" });
            mute();
          }}
          aria-label={zh ? "关闭" : "Close"}
          className="absolute right-2.5 top-2.5 rounded-full p-1 text-slate-500 transition hover:bg-white/10 hover:text-white"
        >
          <X className="h-4 w-4" />
        </button>

        <div className="flex items-center gap-2 text-xs font-medium text-amber-300">
          <Gift className="h-4 w-4" />
          {zh ? `新人专享 · 注册 ${NEWBIE_PACK.windowHours} 小时内` : `Newcomers · within ${NEWBIE_PACK.windowHours}h of signup`}
        </div>
        <div className="mt-2.5 flex items-end gap-2">
          <span className="text-3xl font-bold tabular-nums text-white">{NEWBIE_PACK.price}U</span>
          <span className="pb-0.5 text-lg font-bold text-amber-300">→</span>
          <span className="text-2xl font-bold tabular-nums text-amber-300">{fmt(NEWBIE_PACK.tokens)}</span>
          <span className="pb-1 text-[11px] text-slate-400">Token</span>
        </div>
        <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400">
          {zh
            ? `双倍到账（$${rechargeUnitPrice(NEWBIE_PACK.price, NEWBIE_PACK.tokens)}/千）≈ ${fmt(aiReplies)} 条 AI 回复；每账号一次，不占首充加赠资格。`
            : `Double rate ($${rechargeUnitPrice(NEWBIE_PACK.price, NEWBIE_PACK.tokens)}/1k) ≈ ${fmt(aiReplies)} AI replies. Once per account; keeps your first-top-up bonus.`}
        </p>
        <Link
          href={`${zh ? "" : "/en"}/order?plan=${NEWBIE_PACK.key}`}
          onClick={() => {
            track("poster_click", { id: "newbie-6u", surface: "web_floating" });
            mute();
          }}
          className="mt-3 block rounded-full bg-gradient-to-r from-amber-300 to-amber-400 py-2 text-center text-xs font-semibold text-ink-950 transition hover:opacity-90"
        >
          {zh ? `立即 ${NEWBIE_PACK.price}U 领取` : `Claim for ${NEWBIE_PACK.price}U`}
        </Link>
      </div>
    </div>
  );
}
