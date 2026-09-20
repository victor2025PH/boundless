"use client";

// 首充加赠「能量阶梯」：档位=发光节点，选中档点亮轨道到对应节点并脉冲，
// 下方实时给「再充 X 升档多得 Z Token」的升档提示——把 +5%→+40% 的抬单叙事
// 从每张卡的小字升格为页面级可视化。/order 与 /pricing 共用本组件（单源取数）。
import { Fragment } from "react";
import {
  RECHARGE_TIERS,
  nextRechargeTier,
  rechargeFirstTokens,
  rechargeUnitPrice,
} from "@/lib/chatx-pricing";

const fmt = (n: number) => n.toLocaleString("en-US");

export default function BonusLadder({
  zh,
  selectedKey,
  onSelect,
  onUpsell,
  className,
}: {
  zh: boolean;
  /** 当前选中档 key（recharge-*）；不在 RECHARGE_TIERS 内（如新人包）= 轨道全暗 */
  selectedKey: string;
  /** 点击节点选档 */
  onSelect?: (key: string) => void;
  /** 点击升档提示（缺省回落 onSelect；调用方可另计埋点） */
  onUpsell?: (key: string) => void;
  className?: string;
}) {
  const sel = RECHARGE_TIERS.find((t) => t.key === selectedKey) ?? null;
  const next = sel ? nextRechargeTier(sel) : null;
  const topTier = RECHARGE_TIERS[RECHARGE_TIERS.length - 1];

  return (
    <div className={className}>
      {/* 轨道：节点 + 连接段（标签绝对定位在节点下方，不参与行高） */}
      <div className="relative pb-6">
        <span aria-hidden className="ladder-sweep" />
        <div className="flex items-center">
          {RECHARGE_TIERS.map((t, i) => {
            const lit = !!sel && t.price <= sel.price;
            const isSel = !!sel && t.key === sel.key;
            return (
              <Fragment key={t.key}>
                {i > 0 && (
                  <div
                    aria-hidden
                    className={`mx-1 h-1 flex-1 rounded-full transition-colors duration-300 md:mx-1.5 md:h-1.5 ${
                      lit
                        ? "bg-gradient-to-r from-neon-cyan to-neon-violet shadow-[0_0_10px_rgba(34,211,238,0.35)]"
                        : "bg-white/10"
                    }`}
                  />
                )}
                <div className="relative flex flex-col items-center">
                  <button
                    type="button"
                    onClick={onSelect ? () => onSelect(t.key) : undefined}
                    aria-label={zh ? `选择充值 ${t.price}U 档` : `Select the ${t.price}U tier`}
                    aria-pressed={isSel}
                    className={`flex h-8 w-8 items-center justify-center rounded-full text-[8px] font-semibold tabular-nums transition md:h-10 md:w-10 md:text-[10px] ${
                      lit
                        ? "bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950 shadow-[0_0_14px_rgba(34,211,238,0.4)]"
                        : "border border-white/15 bg-ink-950/60 text-slate-500 hover:border-neon-cyan/60 hover:text-slate-200"
                    } ${isSel ? "ladder-node-sel scale-110" : ""}`}
                  >
                    +{t.firstBonusPct}%
                  </button>
                  <span
                    className={`pointer-events-none absolute top-full mt-1 whitespace-nowrap text-[9px] tabular-nums md:text-[10px] ${
                      isSel ? "font-semibold text-neon-cyan" : lit ? "text-slate-300" : "text-slate-600"
                    }`}
                  >
                    {t.price}U
                  </span>
                </div>
              </Fragment>
            );
          })}
        </div>
      </div>

      {/* 升档提示：有下一档给钩子；到顶档报单价新低；选中新人包给「不占首充资格」注解 */}
      <div aria-live="polite" className="mt-1.5 min-h-[1rem] text-[11px] leading-relaxed text-slate-500">
        {sel && next && (
          <>
            {zh ? (
              <>
                再充 <b className="tabular-nums text-neon-cyan">{fmt(next.price - sel.price)}U</b> 升到 +{next.firstBonusPct}% 档，首充多得{" "}
                <b className="tabular-nums text-neon-cyan">{fmt(rechargeFirstTokens(next) - rechargeFirstTokens(sel))}</b> Token
              </>
            ) : (
              <>
                Top up <b className="tabular-nums text-neon-cyan">{fmt(next.price - sel.price)}U</b> more for the +{next.firstBonusPct}% tier —{" "}
                <b className="tabular-nums text-neon-cyan">{fmt(rechargeFirstTokens(next) - rechargeFirstTokens(sel))}</b> extra tokens on your first top-up
              </>
            )}
            {(onUpsell ?? onSelect) && (
              <button
                type="button"
                onClick={() => (onUpsell ?? onSelect)!(next.key)}
                className="ml-2 rounded-full border border-neon-cyan/40 px-2 py-0.5 text-[10px] text-neon-cyan transition hover:bg-neon-cyan/10"
              >
                {zh ? `升到 ${fmt(next.price)}U →` : `Jump to ${fmt(next.price)}U →`}
              </button>
            )}
          </>
        )}
        {sel && !next && (
          <>
            {zh
              ? `已到顶档 +${sel.firstBonusPct}%，单价低至 $${rechargeUnitPrice(sel.price, rechargeFirstTokens(sel))}/千 Token——全场最低`
              : `Top tier reached (+${sel.firstBonusPct}%) — effective rate down to $${rechargeUnitPrice(sel.price, rechargeFirstTokens(sel))}/1k tokens, the lowest here`}
          </>
        )}
        {!sel && (
          <>
            {zh
              ? `新人包不占用首充加赠资格——之后首笔正常充值仍享 +${RECHARGE_TIERS[1].firstBonusPct}%~+${topTier.firstBonusPct}% 阶梯`
              : `The newcomer pack doesn't consume your first-top-up bonus — your first regular top-up still earns +${RECHARGE_TIERS[1].firstBonusPct}%–${topTier.firstBonusPct}%`}
          </>
        )}
      </div>
    </div>
  );
}
