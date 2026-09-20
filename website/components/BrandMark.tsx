// 无界科技 BOUNDLESS 品牌标识：∞ 破框主形（透明底）。资产由 brand-assets 管线
// （build_brand_assets.py → sync_brand_targets.py）从 keyed 母版生成并同步（boundless-mark-256.png）。
// 这里用 next/image 渲染 256px 版：生产期自动出 AVIF/WebP、按需缩放，导航/页脚轻量。

import Image from "next/image";

export default function BrandMark({
  className = "h-9 w-9",
  rounded = false,
}: {
  className?: string;
  rounded?: boolean;
}) {
  return (
    <span
      className={`relative inline-flex shrink-0 items-center justify-center ${rounded ? "rounded-[22%] bg-[#05060f] p-[6%]" : ""} ${className}`}
      role="img"
      aria-label="无界科技 BOUNDLESS"
    >
      <Image
        src="/brand/logos/boundless-mark-256.png"
        alt="无界科技 BOUNDLESS"
        width={72}
        height={72}
        className="h-full w-full object-contain"
        priority
        draggable={false}
      />
    </span>
  );
}
