"use client";

/** 旋转描边光束。默认青→紫品牌色；gold=琥珀金变体（新人礼包 / 大额档语义色）。 */
export default function BorderBeam({ gold }: { gold?: boolean }) {
  return (
    <span
      aria-hidden
      className={`border-beam ${gold ? "border-beam-gold " : ""}pointer-events-none absolute inset-0 rounded-[inherit]`}
    />
  );
}
