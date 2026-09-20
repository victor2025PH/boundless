import Image from "next/image";
import type { CSSProperties } from "react";
import type { ProductKey } from "@/lib/brand";
import { PRODUCT_IMG, PRODUCT_OPTICAL_SCALE } from "@/components/productMeta";

/**
 * 产品图标唯一渲染入口：统一吃 PRODUCT_OPTICAL_SCALE，避免各页面手写 Image
 * 漏接光学补偿（导航 / 落地页 / 品牌页曾各自直出 PNG，感观大小再次分叉）。
 *
 * 展示尺寸由 className（如 h-12 w-12）控制；size 只喂给 next/image 的 intrinsic 宽高。
 */
export default function ProductIcon({
  product,
  size = 48,
  className = "h-12 w-12 object-contain",
  alt = "",
  priority = false,
  optical = true,
  imgStyle,
}: {
  product: ProductKey;
  size?: number;
  className?: string;
  alt?: string;
  priority?: boolean;
  optical?: boolean;
  imgStyle?: CSSProperties;
}) {
  const scale = optical ? (PRODUCT_OPTICAL_SCALE[product] ?? 1) : 1;

  return (
    <span className="inline-grid shrink-0 place-items-center" data-product-icon={product}>
      <span
        className="inline-grid place-items-center"
        style={scale !== 1 ? { transform: `scale(${scale})` } : undefined}
      >
        <Image
          src={PRODUCT_IMG[product]}
          alt={alt}
          width={size}
          height={size}
          className={className}
          style={imgStyle}
          draggable={false}
          priority={priority}
        />
      </span>
    </span>
  );
}
